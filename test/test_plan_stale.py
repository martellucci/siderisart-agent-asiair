#!/usr/bin/env python3
"""Banco di prova dell'adozione del piano a inizio notte (correzione 2026-08-27).

La notte 26→27 e' andata persa cosi': l'ASIAIR riportava is_plan_started TRUE
(flag rimasto sporco dopo che il reset dell'alba era fallito su un rig morto)
ma non stava scattando nulla. L'agente ha "adottato" quel piano fantasma al
primo ciclo dentro la notte e non ha mai chiamato start(): pannello mai aperto,
prima posa alle 23:43 TX invece che alle ~21:10.

Qui si verifica CHI decide: quando il flag va creduto e quando no. Niente di
reale — ASIAIR, KASA, tetto e Telegram sono finti.
"""
import io
import logging
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from pathlib import Path as _P
ROOT = _P(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))   # si prova il codice NUOVO, prima del deploy
import sfro_agent as A  # noqa: E402

CFG = A.load_config(Path(str(ROOT / "config.example.yaml")))
CFG["session_log"]["enabled"] = False
CFG["sync_module"]["enabled"] = False
# marker di shutdown manuale FUORI dalla produzione (2026-09-06): il file vero
# porta la data dell'ultimo spegnimento dal bot e, essendo PIU' RECENTE della
# notte simulata qui, bloccava lo startup al T-lead facendo cadere lo scenario 9
# — un difetto del test, non del codice.
CFG["manual_shutdown_file"] = "/nonexistent/manual_shutdown.json"
TZ = ZoneInfo(CFG["timezone_observatory"])
FILES = {k: Path("/opt/sfro-agent") / v for k, v in CFG.get("files", {}).items()}

# crepuscolo nautico VERO della notte del 26/8 (quella persa), chiesto al codice
# stesso: hardcodarlo al secondo faceva cadere i cicli "a nautica + 0" appena
# fuori dalla finestra, nel ramo diurno.
_in, N_START, _e, _n = A.nautical_window(
    datetime(2026, 8, 26, 23, 0, tzinfo=TZ), CFG["location"], TZ,
    CFG.get("nautical", {}).get("depression", 12))

_real_dt = A.datetime


def fake_clock(when):
    class FakeDT(_real_dt):
        @classmethod
        def now(cls, tz=None):
            return when
    A.datetime = FakeDT


class FakeAsiair:
    """ASIAIR finto: lo snapshot lo detta il test, start/reset si annotano."""
    available = True
    last = None                      # ultima istanza creata (run_cycle ne fa una)

    def __init__(self, cfg):
        self.host = cfg.get("host", "")
        self.calls = []
        FakeAsiair.last = self
        self.snap = dict(FakeAsiair.SNAP)

    def snapshot(self):
        return dict(self.snap)

    def all_connected(self, snap):
        return not self.missing_devices(snap)

    def missing_devices(self, snap):
        return [] if snap.get("cam_open") else ["camera"]

    def plan_left(self):
        return True, 3600, "1 target"

    def reset_plan(self):
        self.calls.append("reset_plan")
        return True, "già azzerato"

    def start(self, snap=None):
        self.calls.append("start")
        return True, "cooler ON"

    def connect_all(self):
        self.calls.append("connect_all")
        return True, "5/5"

    def prepare(self):
        self.calls.append("prepare")
        return "cooler ON"


CONNESSI = {"reachable": True, "cam_open": True, "focuser_connected": True,
            "wheel_connected": True, "mount_connected": True,
            "guide_connected": True, "has_plan": True, "plan_name": "Sideris",
            "flat_open": False}


def ciclo(nome, *, snap, stato=None, dry=True, minuti_dopo_nautica=1, atteso):
    """Un ciclo dell'agente a tetto aperto con lo snapshot dato.
    `atteso` = stringa che deve comparire nel log ('!' davanti = NON deve)."""
    when = N_START + timedelta(minutes=minuti_dopo_nautica)
    fake_clock(when)
    FakeAsiair.SNAP = dict(CONNESSI, **snap)
    managed = [{"alias": "Asiair", "id": "id-asiair", "state": 1},
               {"alias": "Mount", "id": "id-mount", "state": 1}]
    A._kasa_connect = lambda *a, **k: (object(), {"alias": "strip"}, managed, "ON", None)
    A.parse_alpaca = lambda *a, **k: "OPEN"
    A.http_get_json = lambda *a, **k: {}
    A.ping = lambda h: True
    A.AsiairControl = FakeAsiair
    msgs = []
    A.Telegram = lambda *a, **k: types.SimpleNamespace(
        send=lambda t, kb=None: msgs.append(t))

    state = {"night_id": "2026-08-25", "startup_night_id": "2026-08-26"}
    state.update(stato or {})

    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    A.log.addHandler(h)
    A.log.setLevel(logging.INFO)
    try:
        A.run_cycle(CFG, state, FILES, dry_run=dry)
    finally:
        A.log.removeHandler(h)
        A.datetime = _real_dt
    out = buf.getvalue() + "\n".join(msgs)

    nega = atteso.startswith("!")
    ago = atteso[1:] if nega else atteso
    ok = (ago not in out) if nega else (ago in out)
    print(f"[{'OK ' if ok else 'KO '}] {nome}")
    if not ok:
        print("      atteso:", atteso)
        print("      visto :", " | ".join(l for l in out.splitlines() if l.strip()))
    return ok, state, FakeAsiair.last, msgs


AVVIO = "avrei avviato il piano"
res = []

# 1. IL GUASTO DEL 26→27: flag alto, camera ferma, stato pulito -> deve avviare
res.append(ciclo("1. flag sporco (avviato ma fermo) -> AVVIA",
                 snap={"plan_started": True, "capturing": False,
                       "capture_state": None},
                 atteso=AVVIO)[0])
res.append(ciclo("1b. …e prima resetta il piano",
                 snap={"plan_started": True, "capturing": False,
                       "capture_state": None},
                 atteso="previo reset")[0])

# 2. FirstDelay: il piano E' avviato davvero, sta solo aspettando -> non toccare
res.append(ciclo("2. FirstDelay -> NON riavvia (attesa programmata)",
                 snap={"plan_started": True, "capturing": False,
                       "capture_state": "first_delay"},
                 atteso="!" + AVVIO)[0])

# 3. piano che scatta davvero -> adottato, nessun avvio
res.append(ciclo("3. piano in ripresa -> lo adotta, niente start",
                 snap={"plan_started": True, "capturing": True,
                       "capture_state": "expose"},
                 atteso="!" + AVVIO)[0])

# 4. piano GIA' adottato e pausa fra due pose (dither/autofocus/flip):
#    il flag secco basta, non deve mai partire un secondo start()
res.append(ciclo("4. pausa fra due pose a piano adottato -> NON riavvia",
                 snap={"plan_started": True, "capturing": False,
                       "capture_state": "autofocus"},
                 stato={"night_id": N_START.date().isoformat(),
                        "imaging_active": True},
                 minuti_dopo_nautica=120, atteso="!" + AVVIO)[0])

# 5. fine piano gia' annunciata: la logica di fine notte non si tocca
res.append(ciclo("5. piano gia' fermato/annunciato -> NON riavvia",
                 snap={"plan_started": True, "capturing": False,
                       "capture_state": None},
                 stato={"night_id": N_START.date().isoformat(),
                        "error_notified": True,
                        "plan_stopped_ts": (N_START + timedelta(minutes=60)).isoformat()},
                 minuti_dopo_nautica=65, atteso="!" + AVVIO)[0])

# 6. device non connessi: nessun avvio, nessun reset
res.append(ciclo("6. device mancanti -> nessun avvio",
                 snap={"plan_started": True, "capturing": False,
                       "capture_state": None, "cam_open": False},
                 atteso="!" + AVVIO)[0])

# ---- esecuzione VERA (dry_run=False): ordine reset -> start e messaggio ----
ok, st, ac, msgs = ciclo("7. esecuzione: reset PRIMA di start",
                         snap={"plan_started": True, "capturing": False,
                               "capture_state": None},
                         dry=False, atteso="piano «Sideris» avviato")
res.append(ok)
res.append(ac.calls == ["reset_plan", "start"])
print(f"[{'OK ' if res[-1] else 'KO '}] 7b. ordine dei comandi: {ac.calls}")
res.append(any("risultava avviato ma fermo: resettato" in m for m in msgs))
print(f"[{'OK ' if res[-1] else 'KO '}] 7c. il messaggio Telegram dice del reset")
res.append(st.get("imaging_active") is True)
print(f"[{'OK ' if res[-1] else 'KO '}] 7d. piano adottato dopo l'avvio")

# 8. piano sano: nessun reset, solo start
ok, st, ac, msgs = ciclo("8. flag basso (piano fermo pulito) -> solo start",
                         snap={"plan_started": False, "capturing": False,
                               "capture_state": None},
                         dry=False, atteso="piano «Sideris» avviato")
res.append(ok)
res.append(ac.calls == ["start"])
print(f"[{'OK ' if res[-1] else 'KO '}] 8b. nessun reset inutile: {ac.calls}")

# 9. reset rimasto in sospeso dall'alba -> ritentato allo startup del T-lead
ok, st, ac, msgs = ciclo("9. reset in sospeso -> ritentato al T-lead",
                         snap={"plan_started": False, "capturing": False,
                               "capture_state": None},
                         stato={"plan_reset_failed": "2026-08-26T06:15:00-05:00",
                                "startup_night_id": "2026-08-25"},
                         dry=False, minuti_dopo_nautica=-5,
                         atteso="Reset del piano rimasto in sospeso")
res.append(ok)
res.append(st.get("plan_reset_failed") is None)
print(f"[{'OK ' if res[-1] else 'KO '}] 9b. la marca viene tolta dopo il reset riuscito")

print("\nESITO:", "TUTTO OK" if all(res) else "CI SONO FALLIMENTI",
      f"({sum(res)}/{len(res)})")
sys.exit(0 if all(res) else 1)
