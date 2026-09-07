#!/usr/bin/env python3
"""Banco di prova del recupero 'prese accese, ASIAIR spento' (2026-08-26).

Non tocca nulla di reale: ASIAIR, KASA, tetto e Telegram sono finti e l'agente
gira in dry-run. Verifica SOLO la decisione: quando il ciclo di corrente deve
scattare e quando NON deve.
"""
import io
import logging
import sys
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
# vedi test_plan_stale: il marker di shutdown manuale REALE e' piu' recente
# della notte simulata e bloccherebbe lo startup al T-lead (2026-09-06)
CFG["manual_shutdown_file"] = "/nonexistent/manual_shutdown.json"
TZ = ZoneInfo(CFG["timezone_observatory"])
FILES = {k: Path("/opt/sfro-agent") / v for k, v in CFG.get("files", {}).items()}

# n_start della notte del 26/8 = 21:02:56 CDT
N_START = datetime(2026, 8, 26, 21, 2, 56, tzinfo=TZ)
N_END = datetime(2026, 8, 27, 5, 55, 0, tzinfo=TZ)


def fake_nautical(now, loc_cfg, tz, depression=12.0):
    """La notte del 26/8 fissata a mano, invece di ricalcolarla dal config.

    Qui si prova la DECISIONE del ciclo di corrente, non l'astronomia: legare il
    test alle coordinate del config lo rende falsamente dipendente da quelle
    (con coordinate segnaposto la finestra cade altrove e gli scenari 10-12
    fallivano pur essendo il codice giusto)."""
    return (now >= N_START, N_START, N_END, "2026-08-26")


A.nautical_window = fake_nautical

_real_dt = A.datetime


def fake_clock(when):
    class FakeDT(_real_dt):
        @classmethod
        def now(cls, tz=None):
            return when
    A.datetime = FakeDT


class FakeKasa:
    def __init__(self):
        self.calls = []

    def set_outlet(self, dev, oid, on):
        self.calls.append((oid, on))


class FakeAsiair:
    available = True

    def __init__(self, cfg):
        self.host = cfg.get("host", "")

    def snapshot(self):
        return None                      # muto: e' il caso in esame

    def connect_all(self):
        return False, "muto"

    def missing_devices(self, snap):
        return []

    def all_connected(self, snap):
        return False


def run(name, *, minuti_dopo_nautica, silenzio_min, vpn_up=True, plug="ON",
        stato=None, atteso):
    """Un ciclo dell'agente nelle condizioni date. `atteso` = stringa che deve
    (o non deve, se prefissata da '!') comparire nel log."""
    when = N_START + timedelta(minutes=minuti_dopo_nautica)
    fake_clock(when)
    kasa = FakeKasa()
    managed = [{"alias": "Asiair", "id": "id-asiair", "state": 1},
               {"alias": "Mount", "id": "id-mount", "state": 1}]
    A._kasa_connect = lambda *a, **k: (kasa, {"alias": "strip"}, managed, plug, None)
    A.parse_alpaca = lambda *a, **k: "OPEN"
    A.http_get_json = lambda *a, **k: {}
    A.ping = lambda h: False
    A.vpn_diagnose = lambda h, p="": {
        "asiair_up": False, "vpn_up": vpn_up,
        "cause": "ASIAIR spento o in avvio (VPN attiva)" if vpn_up
                 else "VPN NON attiva (rete SFRO irraggiungibile)"}
    A.AsiairControl = FakeAsiair
    A.Telegram = lambda *a, **k: None

    state = {"night_id": "2026-08-25"}
    if silenzio_min is not None:
        state["asiair_unreach_night"] = "2026-08-26"
        state["asiair_unreach_since"] = (when - timedelta(minutes=silenzio_min)).isoformat()
    state.update(stato or {})

    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    A.log.addHandler(h)
    A.log.setLevel(logging.INFO)
    try:
        A.run_cycle(CFG, state, FILES, dry_run=True)
    finally:
        A.log.removeHandler(h)
        A.datetime = _real_dt
    out = buf.getvalue()

    nega = atteso.startswith("!")
    ago = atteso[1:] if nega else atteso
    ok = (ago not in out) if nega else (ago in out)
    print(f"[{'OK ' if ok else 'KO '}] {name}")
    if not ok:
        print("      atteso:", atteso)
        print("      log:", " | ".join(l for l in out.splitlines() if l.strip()))
    return ok, state, kasa


CICLO = "avrei ciclato la presa 'Asiair'"
res = []

res.append(run("1. muto da 6', VPN su, dentro finestra -> CICLA",
               minuti_dopo_nautica=5, silenzio_min=6, atteso=CICLO)[0])
res.append(run("2. muto da 3' -> NON cicla (soglia 5')",
               minuti_dopo_nautica=5, silenzio_min=3, atteso="!" + CICLO)[0])
res.append(run("3. VPN GIU' -> NON cicla mai",
               minuti_dopo_nautica=5, silenzio_min=20, vpn_up=False,
               atteso="!" + CICLO)[0])
res.append(run("4. notte avanzata (+45') -> fuori finestra, NON cicla",
               minuti_dopo_nautica=45, silenzio_min=20, atteso="!" + CICLO)[0])
res.append(run("5. gia' ciclato stanotte -> NON ripete",
               minuti_dopo_nautica=10, silenzio_min=20,
               stato={"power_cycle_night_id": "2026-08-26",
                      "power_cycle_ts": (N_START + timedelta(minutes=8)).isoformat()},
               atteso="!" + CICLO)[0])
res.append(run("6. T-3 (prima della nautica) -> conteggio armato, non cicla",
               minuti_dopo_nautica=-3, silenzio_min=20, atteso="!" + CICLO)[0])

# 7. verdetto: ciclo fatto 7 minuti fa e ASIAIR ancora muto
ok, st, _ = run("7. dopo il ciclo tace ancora -> errore esplicito",
                minuti_dopo_nautica=12, silenzio_min=7,
                stato={"power_cycle_night_id": "2026-08-26",
                       "power_cycle_ts": (N_START + timedelta(minutes=5)).isoformat()},
                atteso="Ciclo di corrente eseguito alle")
res.append(ok)
res.append(st.get("power_cycle_failed") is True)
print(f"[{'OK ' if res[-1] else 'KO '}] 7b. il verdetto resta marcato (niente ripetizioni)")

# 8. verdetto gia' dato -> silenzio
res.append(run("8. verdetto gia' dato -> non lo ripete",
               minuti_dopo_nautica=20, silenzio_min=15,
               stato={"power_cycle_night_id": "2026-08-26",
                      "power_cycle_ts": (N_START + timedelta(minutes=5)).isoformat(),
                      "power_cycle_failed": True},
               atteso="!Ciclo di corrente eseguito alle")[0])

# 9. prima osservazione muta: arma il conteggio e basta
ok, st, _ = run("9. prima osservazione muta -> arma il conteggio",
                minuti_dopo_nautica=1, silenzio_min=None, atteso="!" + CICLO)
res.append(ok and st.get("asiair_unreach_since") is not None)
print(f"[{'OK ' if res[-1] else 'KO '}] 9b. asiair_unreach_since scritto in stato")


# --------------------------------------------------------------------------- #
# Esecuzione VERA del ciclo di corrente (dry_run=False), con KASA/Telegram finti
# --------------------------------------------------------------------------- #
import time as _time                                            # noqa: E402
import types                                                    # noqa: E402


def esegui(name, *, mount_state=1, rompi_spegnimento=False):
    when = N_START + timedelta(minutes=5)
    fake_clock(when)
    kasa = FakeKasa()
    if rompi_spegnimento:
        def boom(dev, oid, on):
            raise RuntimeError("cloud TP-Link giù")
        kasa.set_outlet = boom
    managed = [{"alias": "Asiair", "id": "id-asiair", "state": 1},
               {"alias": "Mount", "id": "id-mount", "state": mount_state}]
    msgs, dormito = [], []
    A._kasa_connect = lambda *a, **k: (kasa, {"alias": "strip"}, managed, "ON", None)
    A.parse_alpaca = lambda *a, **k: "OPEN"
    A.http_get_json = lambda *a, **k: {}
    A.ping = lambda h: False
    A.vpn_diagnose = lambda h, p="": {"asiair_up": False, "vpn_up": True, "cause": ""}
    A.AsiairControl = FakeAsiair
    A.Telegram = lambda *a, **k: types.SimpleNamespace(
        send=lambda t, kb=None: msgs.append(t))
    A.time = types.SimpleNamespace(time=_time.time, sleep=lambda s: dormito.append(s))

    state = {"night_id": "2026-08-25",
             "asiair_unreach_night": "2026-08-26",
             "asiair_unreach_since": (when - timedelta(minutes=6)).isoformat()}
    try:
        A.run_cycle(CFG, state, FILES, dry_run=False)
    finally:
        A.datetime = _real_dt
        A.time = _time
    print(f"--- {name}")
    print("    comandi KASA:", kasa.calls if not rompi_spegnimento else "(spegnimento fallito)")
    print("    attese     :", dormito)
    for m in msgs:
        print("    TG:", m.replace("\n", " / "))
    return kasa, state, msgs, dormito


k, st, msgs, dorm = esegui("10. Mount GIA' ACCESO: si cicla solo l'Asiair")
res.append(k.calls == [("id-asiair", False), ("id-asiair", True)])
print(f"[{'OK ' if res[-1] else 'KO '}] 10a. il Mount non viene mai toccato")
res.append(dorm == [30.0] and st.get("power_cycle_night_id") == "2026-08-26")
print(f"[{'OK ' if res[-1] else 'KO '}] 10b. 30s di attesa e ciclo marcato per la notte")
res.append(len(msgs) >= 2 and "tolgo corrente" in msgs[-2] and "riaccesa" in msgs[-1])
print(f"[{'OK ' if res[-1] else 'KO '}] 10c. avviso su Telegram a inizio E fine")

k, st, msgs, dorm = esegui("11. Mount SPENTO: si riaccende insieme all'Asiair",
                           mount_state=0)
res.append(k.calls == [("id-asiair", False), ("id-asiair", True), ("id-mount", True)])
print(f"[{'OK ' if res[-1] else 'KO '}] 11a. Asiair ciclato, Mount acceso (mai spento)")

k, st, msgs, dorm = esegui("12. KASA in errore: il rig resta com'è",
                           rompi_spegnimento=True)
res.append(dorm == [] and st.get("power_cycle_night_id") is None
           and any("fallito" in m for m in msgs))
print(f"[{'OK ' if res[-1] else 'KO '}] 12a. niente attesa, niente marca, errore segnalato")

print("\nESITO:", "TUTTO OK" if all(res) else "CI SONO FALLIMENTI",
      f"({sum(res)}/{len(res)})")
sys.exit(0 if all(res) else 1)
