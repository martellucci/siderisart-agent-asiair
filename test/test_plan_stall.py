"""Test OFFLINE del watchdog "piano senza progressi" (2026-08-17) e della
correzione dei FALSI POSITIVI introdotta il 2026-09-07.

Il watchdog esiste perche' la notte del 17/8 il piano risultava avviato mentre
il mount rifiutava 3162 goto di fila: zero frame per 2h15m e nessun avviso.
Guarda il residuo del piano (get_plan): se non scende per plan_stall_minutes,
avvisa.

Il falso positivo (segnalato dall'utente il 7/9, messaggio ricevuto alle 08:09
ora italiana subito dopo la riapertura del tetto): durante una pausa meteo il
residuo resta fermo per costruzione — non si riprende — ma il watchdog non
girava, quindi alla riapertura ritrovava lo stesso residuo e il timestamp di
prima della chiusura, e accusava un fermo lungo quanto la pausa.

Scenari:
  A) residuo che scende -> nessun allarme
  B) residuo fermo mentre il watchdog sorveglia -> allarme (il caso del 17/8
     DEVE continuare a scattare)
  C) pausa meteo + riapertura -> nessun allarme (il falso positivo)
  D) buco di sorveglianza senza meteo (agente fermo, VPN giu') -> nessun allarme
  E) dopo un buco il conteggio riparte da zero, non dal vecchio timestamp
"""
import sys
from pathlib import Path as _P
ROOT = _P(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import sfro_agent as A

A.time.sleep = lambda s: None
A.ping = lambda host: True
A.sync_busy = lambda sc: None    # mai il lock di produzione nei test
A.sync_pass = lambda sc, dry, cfg=None, **kw: {"files": 0}
A.load_kv_file = lambda p: {}
A.http_get_json = lambda url, http, session=None: {}

MSGS = []


class FakeTG:
    def __init__(self, *a, **k): pass
    def send(self, text, kb=None): MSGS.append(text)


A.Telegram = FakeTG
A._kasa_connect = lambda cfg, files, http, tu: (
    object(), {}, [{"alias": "Asiair", "state": 1}], "ON", None)
A._kasa_power_off_all = lambda kc, dev, dry: (1, [], None)

tz = ZoneInfo("America/Chicago")
NOW = [datetime(2026, 9, 6, 23, 0, tzinfo=tz)]
_real_dt = A.datetime


class FakeDT(_real_dt):
    @classmethod
    def now(cls, tz=None):
        return NOW[0].astimezone(tz) if tz else NOW[0]


A.datetime = FakeDT

ROOF = ["OPEN"]
LEFT = [3600]          # residuo del piano, in secondi
A.parse_alpaca = lambda data: ROOF[0]
A.AsiairControl.plan_left = lambda self: (True, LEFT[0], "")
A.AsiairControl.snapshot = lambda self: {
    "reachable": True, "capturing": True, "plan_started": True,
    "plan_name": "Sideris", "has_plan": True, "cam_open": True,
    "focuser_connected": True, "wheel_connected": True,
    "mount_connected": True, "guide_connected": True, "capture_state": "exposing"}
A.AsiairControl.teardown = lambda self, keep_cooler=False: {
    "stop": (True, ""), "home": (True, ""), "flat": (True, "")}
A.AsiairControl.start = lambda self, s=None: (True, "ok")
A.AsiairControl.reset_plan = lambda self: (True, "pulito")
A.AsiairControl.set_output = lambda self, t, v: (True, "")
A.AsiairControl.connect_all = lambda self: (True, "ok")


def fake_nw(now, loc, tz, dep=12.0):
    return True, now - timedelta(hours=3), now + timedelta(hours=3), "2026-09-06"


A.nautical_window = fake_nw

cfg = A.load_config(ROOT / "config.example.yaml")
cfg["session_log"]["enabled"] = False
cfg["startup"]["enabled"] = False
cfg["manual_shutdown_file"] = "/nonexistent/manual_shutdown.json"
FILES = {"telegram": "x", "kasa": "x", "asiair": "x"}
STATE = {"night_id": "2026-09-06", "nautical_announced": True}


def cyc(minutes=3, left=None, roof=None):
    """Un ciclo dell'agente `minutes` dopo il precedente."""
    NOW[0] = NOW[0] + timedelta(minutes=minutes)
    if left is not None:
        LEFT[0] = left
    if roof is not None:
        ROOF[0] = roof
    MSGS.clear()
    A.run_cycle(cfg, STATE, FILES, dry_run=False)
    SEEN.extend(MSGS)


SEEN = []


def stallo():
    """Vero se l'allarme e' comparso da quando SEEN e' stato azzerato: gli
    alert hanno un cooldown, quindi guardare solo l'ultimo ciclo non basta."""
    return any("SENZA PROGRESSI" in m for m in SEEN)


SEEN.clear()
# --- A) il piano avanza: nessun allarme ------------------------------------
cyc()                       # primo giro: fissa la base
for i in range(12):         # 36 min di ripresa regolare
    cyc(left=LEFT[0] - 300)
assert not stallo(), MSGS
assert STATE.get("plan_stall_left") == LEFT[0]
print("A) piano che avanza: nessun allarme OK")

SEEN.clear()
# --- B) residuo fermo sotto sorveglianza: l'allarme DEVE scattare -----------
fermo = LEFT[0]
for i in range(8):          # 24 min con il residuo invariato
    cyc(left=fermo)
assert stallo(), "il caso del 17/8 deve continuare a scattare"
assert any("~2" in m for m in SEEN), SEEN   # ~21..~24 min
print("B) stallo vero: allarme OK")

SEEN.clear()
# --- C) pausa meteo e riapertura: nessun falso positivo --------------------
STATE.pop("alerts", None)
cyc(left=fermo - 300)       # riparte, base pulita
assert not stallo()
cyc(roof="CLOSED")          # tetto chiuso: pausa meteo
assert STATE.get("weather_closed") is True, STATE
assert not STATE.get("plan_stall_ts"), "la pausa azzera il watchdog"
for i in range(8):          # 24 minuti di tetto chiuso
    cyc()
cyc(roof="OPEN")            # riapertura: il residuo e' ancora quello di prima
assert not stallo(), f"FALSO POSITIVO alla riapertura: {SEEN}"
cyc()
assert not stallo(), f"FALSO POSITIVO al ciclo dopo la riapertura: {SEEN}"
print("C) riapertura meteo: nessun falso positivo OK")

SEEN.clear()
# --- D) buco di sorveglianza (agente fermo / VPN giu') ---------------------
cyc(left=fermo - 600)
assert not stallo()
cyc(minutes=40)             # 40 min senza cicli, residuo invariato al ritorno
assert not stallo(), f"un buco di sorveglianza non e' uno stallo: {SEEN}"
print("D) buco di sorveglianza: nessun falso positivo OK")

SEEN.clear()
# --- E) dopo il buco il conteggio riparte da zero --------------------------
for i in range(5):          # 15 min di fermo VERO dopo il buco
    cyc()
assert not stallo(), "15 min < soglia: non deve ancora scattare"
for i in range(3):          # ...e a 24 min scatta
    cyc()
assert stallo(), SEEN
print("E) conteggio riavviato dopo il buco OK")

print("\nTUTTI I TEST WATCHDOG STALLO: OK")
