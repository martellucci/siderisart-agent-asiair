"""Test OFFLINE della sequenza di avvio RIVISTA (specifica utente 2026-08-16):

  tetto APERTO -> T-10 dalla notte nautica: KASA on + connect_all + anti-dew +
  fascia + COOLER, e il piano NON parte -> all'inizio della notte nautica il
  ramo di notte trova i device gia' connessi e avvia il piano.

Niente ASIAIR e niente KASA. Il cuore del test e' negativo: verificare che a
T-10 `start()` non venga MAI chiamata (prima del 2026-08-16 lo era, e il piano
partiva con la camera calda e il cielo ancora chiaro).
"""
import sys
from datetime import timedelta

from pathlib import Path as _P
ROOT = _P(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import sfro_agent as A

A.time.sleep = lambda s: None
A.ping = lambda host: True
A.sync_pass = lambda sc, dry: {"files": 0}
A.load_kv_file = lambda p: {}

MSGS = []


class FakeTG:
    def __init__(self, *a, **k): pass
    def send(self, text, kb=None): MSGS.append(text)


A.Telegram = FakeTG

ROOF = "OPEN"
A.http_get_json = lambda url, http: {}
A.parse_alpaca = lambda data: ROOF

# KASA: prese SPENTE, cosi' il ramo T-10 deve accenderle davvero
MANAGED = [{"alias": "Asiair", "id": "1", "state": 0},
           {"alias": "Mount", "id": "2", "state": 0}]
REC = {"start": 0, "connect_all": 0, "prepare": 0, "kasa_on": 0}
A._kasa_connect = lambda cfg, files, http, tu: (object(), {}, MANAGED, "OFF", None)
A._kasa_power_on = lambda kc, dev, managed, dry: (
    REC.__setitem__("kasa_on", REC["kasa_on"] + 1), (True, [], None))[1]
A._kasa_power_off_all = lambda kc, dev, dry: (2, [], None)

CONNECTED = {"cam_open": True, "focuser_connected": True, "wheel_connected": True,
             "mount_connected": True, "guide_connected": True, "reachable": True}
SNAP = dict(CONNECTED, plan_started=False, capturing=False,
            plan_name="Piano SFRO", has_plan=True)
A.AsiairControl.snapshot = lambda self: dict(SNAP)


def fake_start(self, s=None):
    REC["start"] += 1
    return True, "prep"


A.AsiairControl.start = fake_start
A.AsiairControl.connect_all = lambda self: (
    REC.__setitem__("connect_all", REC["connect_all"] + 1),
    (True, "5/5 device in 24s"))[1]
# il prepare VERO va preso PRIMA di sostituirlo con lo stub, altrimenti la
# sezione B finirebbe per testare lo stub (ci sono cascato al primo giro)
VERO_PREPARE = A.AsiairControl.prepare
A.AsiairControl.prepare = lambda self: (
    REC.__setitem__("prepare", REC["prepare"] + 1),
    "anti-dew camera ON · fascia anticondensa al 100% · cooler ON (21.4°C → target 0.0°C)")[1]

cfg = A.load_config(ROOT / "config.example.yaml")
cfg["session_log"]["enabled"] = False
cfg["manual_shutdown_file"] = "/nonexistent/manual_shutdown.json"
FILES = {"telegram": "x", "kasa": "x", "asiair": "x"}

# finestra nautica pilotata: T_MINUS = minuti che mancano all'inizio notte
T_MINUS = 8
IN_NIGHT = False


def fake_nw(now, loc, tz, dep=12.0):
    ns = now + timedelta(minutes=T_MINUS) if not IN_NIGHT else now - timedelta(minutes=2)
    ne = ns + timedelta(hours=8)
    return IN_NIGHT, ns, ne, "2026-08-16"


A.nautical_window = fake_nw

ok = fail = 0


def check(nome, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  OK   {nome}")
    else:
        fail += 1
        print(f"  FAIL {nome} {extra}")


def cycle(state):
    return A.run_cycle(cfg, state, FILES, dry_run=False)


def azzera():
    REC.update(start=0, connect_all=0, prepare=0, kasa_on=0)
    MSGS.clear()


# ---------------------------------------------------------------- config
print("A) configurazione di produzione")
su = cfg.get("startup", {})
check("startup ABILITATO", su.get("enabled") is True, su.get("enabled"))
check("lead a 10 minuti", int(su.get("lead_minutes", 0)) == 10, su.get("lead_minutes"))

# ------------------------------------------------------------- prepare()
print("B) prepare(): tre passi, errori non bloccanti")
vero = VERO_PREPARE                     # catturato prima dello stub


class FakePrep:
    start_dew_heater_pct = 100

    def __init__(self, a=True, h=True, c=True):
        self.calls, self._a, self._h, self._c = [], a, h, c

    def ensure_anti_dew(self):
        self.calls.append("anti_dew"); return self._a, "era SPENTO: acceso ora"

    def set_output(self, t, v):
        self.calls.append(f"dew={v}"); return self._h, "" if self._h else "no uscita"

    def cooler_on(self):
        self.calls.append("cooler"); return self._c, "" if self._c else "code 107"

    def camera_cooling(self):
        return True, 21.4, 0.0, True, ""


f = FakePrep(); txt = vero(f)
check("ordine anti-dew -> fascia -> cooler",
      f.calls == ["anti_dew", "dew=100", "cooler"], f.calls)
check("il cooler e' nel testo", "cooler ON" in txt, txt)
check("riporta temperatura e target", "21.4" in txt and "0.0" in txt, txt)
check("cooler KO -> avviso, non eccezione",
      "⚠️ cooler NON acceso" in vero(FakePrep(c=False)))
check("tutti KO -> tre avvisi", vero(FakePrep(False, False, False)).count("⚠️") == 3)

# ------------------------------------------------- C) finestra T-10
print("C) T-8: inizializza e NON avvia il piano")
azzera()
S = {}
T_MINUS, IN_NIGHT, ROOF = 8, False, "OPEN"
cycle(S)
check("KASA accesa", REC["kasa_on"] == 1, REC)
check("device connessi", REC["connect_all"] == 1, REC)
check("prepare() chiamato (fascia + COOLER)", REC["prepare"] == 1, REC)
check("PIANO NON AVVIATO", REC["start"] == 0, REC)
check("il messaggio annuncia l'attesa",
      any("Piano in attesa" in m for m in MSGS), MSGS)
check("notte marcata inizializzata", S.get("startup_night_id") == "2026-08-16", S)

print("D) secondo ciclo dentro la finestra: non ripete")
azzera()
cycle(S)
check("non riaccende", REC["kasa_on"] == 0, REC)
check("non riconnette", REC["connect_all"] == 0, REC)
check("piano ancora fermo", REC["start"] == 0, REC)

print("E) T-20 (fuori finestra): non tocca nulla")
azzera()
T_MINUS = 20
cycle({})
check("niente accensione", REC["kasa_on"] == 0, REC)
check("niente connessione", REC["connect_all"] == 0, REC)

print("F) tetto CHIUSO a T-8: tutto resta spento")
azzera()
T_MINUS, ROOF = 8, "CLOSED"
cycle({})
check("KASA non accesa", REC["kasa_on"] == 0, REC)
check("device non connessi", REC["connect_all"] == 0, REC)
check("piano non avviato", REC["start"] == 0, REC)

print("G) inizio notte nautica: ORA il piano parte")
azzera()
ROOF, IN_NIGHT = "OPEN", True
S2 = dict(S)                      # stessa notte, gia' inizializzata al T-10
cycle(S2)
check("PIANO AVVIATO", REC["start"] == 1, REC)
check("non riconnette (erano gia' connessi)", REC["connect_all"] == 0, REC)
check("ripresa marcata attiva", S2.get("imaging_active") is True, S2)

print("H) notte iniziata ma device GIU': connette (fallback) e non avvia subito")
azzera()
SNAP.update(cam_open=False, mount_connected=False)
S3 = {"night_id": "2026-08-16"}
cycle(S3)
check("connect_all di fallback", REC["connect_all"] == 1, REC)
check("piano non avviato nello stesso ciclo", REC["start"] == 0, REC)
SNAP.update(cam_open=True, mount_connected=True)

print(f"\n{ok} ok, {fail} fail")
sys.exit(1 if fail else 0)
