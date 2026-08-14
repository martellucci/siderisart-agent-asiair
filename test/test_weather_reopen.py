"""Test OFFLINE del recupero chiusura meteo + sicurezza alba (2026-07-24).
Scenari:
  A) piano in corso -> tetto CHIUSO (meteo): PAUSA (cooler mantenuto), niente
     reset/finalize, weather_closed.
  B) tetto RIAPERTO, setup fermo: riavvio automatico del piano (una volta).
  C) tetto RIAPERTO ma AUTORUN in corso: non tocca nulla, avviso una volta;
     poi autorun finito -> promemoria manuale.
  D) alba con pausa meteo mai riaperta -> flat come di consueto.
  E) Req3: dopo pausa+ripresa, teardown_done ripulito -> l'alba fa teardown+flat.
"""
import sys
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

MANAGED = [{"alias": "Asiair", "state": 1}, {"alias": "Mount", "state": 1}]
A._kasa_connect = lambda cfg, files, http, tu: (object(), {}, MANAGED, "ON", None)
A._kasa_power_off_all = lambda kc, dev, dry: (2, [], None)

# roof + snapshot controllati per ciclo
ROOF = "OPEN"
SNAP = {}
A.http_get_json = lambda url, http: {}
A.parse_alpaca = lambda data: ROOF

CONNECTED = {"cam_open": True, "focuser_connected": True, "wheel_connected": True,
             "mount_connected": True, "guide_connected": True, "reachable": True}

def snap(plan=False, capt=False, name="M31"):
    d = dict(CONNECTED)
    d.update({"plan_started": plan, "capturing": capt, "plan_name": name,
              "has_plan": True})
    return d

A.AsiairControl.snapshot = lambda self: SNAP

REC = {"start": [], "teardown": [], "reset": 0, "outputs": []}
def fake_start(self, s=None):
    REC["start"].append((s or {}).get("plan_name"))
    return True, "anti-dew camera ON (gia' acceso) · fascia anticondensa al 100%"
A.AsiairControl.start = fake_start
def fake_teardown(self, keep_cooler=False):
    REC["teardown"].append(keep_cooler)
    d = {"stop": (True, ""), "home": (True, ""), "flat": (True, "")}
    if not keep_cooler:
        d["cooler"] = (True, "")
    return d
A.AsiairControl.teardown = fake_teardown
A.AsiairControl.reset_plan = lambda self: (REC.__setitem__("reset", REC["reset"] + 1), (True, "pulito"))[1]
A.AsiairControl.set_output = lambda self, t, v: (REC["outputs"].append((t, v)), (True, ""))[1]
A.AsiairControl.connect_all = lambda self: (True, "ok")

# nautical_window controllato
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
TZ = ZoneInfo("America/Chicago")
IN_NIGHT = True
def fake_nw(now, loc, tz, dep=12.0):
    ns = now - timedelta(hours=1)
    ne = now + timedelta(hours=1) if IN_NIGHT else now - timedelta(minutes=5)
    return IN_NIGHT, ns, ne, "2026-07-24"
A.nautical_window = fake_nw

cfg = A.load_config(ROOT / "config.example.yaml")
cfg["session_log"]["enabled"] = False    # niente Drive nel test
cfg["startup"]["enabled"] = False
# marker shutdown manuale: puntare a un file INESISTENTE, altrimenti il test
# dipende dall'ora reale (uno shutdown dal bot dentro la finestra della notte
# finta blocca la sonda d'alba e fa fallire lo scenario F)
cfg["manual_shutdown_file"] = "/nonexistent/manual_shutdown.json"
FILES = {"telegram": "x", "kasa": "x", "asiair": "x"}

def cycle():
    return A.run_cycle(cfg, STATE, FILES, dry_run=False)

def last(n=1):
    return "\n---\n".join(MSGS[-n:])

# ===================== A) piano in corso, poi chiusura meteo =====================
STATE = {"night_id": "2026-07-24", "nautical_announced": True}
ROOF, SNAP, IN_NIGHT = "OPEN", snap(plan=True), True
cycle()
assert STATE.get("imaging_active") is True, STATE
assert STATE.get("plan_name") == "M31"

MSGS.clear(); REC["teardown"].clear()
ROOF = "CLOSED"
cycle()
assert STATE.get("weather_closed") is True, STATE
assert STATE.get("imaging_active") is False
assert STATE.get("teardown_done") is True
assert REC["teardown"] == [True], "cooler MANTENUTO (keep_cooler=True)"
assert REC["reset"] == 0, "niente reset piano sulla pausa meteo"
assert any("PAUSA" in m and "riapertura" in m for m in MSGS), MSGS
print("A) pausa meteo OK")

# ===================== B) riapertura con setup fermo -> riavvio =====================
MSGS.clear(); REC["start"].clear()
ROOF, SNAP = "OPEN", snap(plan=False, capt=False)
cycle()
assert REC["start"] == ["M31"], REC["start"]
assert STATE.get("weather_closed") is None, "recupero completato"
assert STATE.get("imaging_active") is True
assert STATE.get("teardown_done") is None, "teardown_done ripulito (Req3)"
assert any("RIPRESO" in m for m in MSGS), MSGS
print("B) riavvio automatico OK")

# E) ... e ora l'alba deve fare teardown+flat (teardown_done era ripulito)
MSGS.clear(); REC["teardown"].clear()
IN_NIGHT = False
ROOF, SNAP = "OPEN", snap(plan=True)   # piano ancora in corso all'alba
# prima riadotta il piano in un ciclo notturno? no: passiamo diretti all'alba
cycle()
assert STATE.get("flat_stage") == "drying", STATE
assert REC["teardown"] == [True], "teardown alba con cooler tenuto per i flat"
assert REC["reset"] >= 1, "reset piano all'alba"
print("E) alba: teardown+flat dopo ripresa OK")

# ===================== C) riapertura con AUTORUN manuale in corso =====================
MSGS.clear(); REC["start"].clear()
STATE = {"night_id": "2026-07-24", "nautical_announced": True,
         "weather_closed": True, "teardown_done": True, "imaging_active": False}
IN_NIGHT = True
ROOF, SNAP = "OPEN", snap(plan=False, capt=True)   # autorun in corso
cycle()
assert REC["start"] == [], "NON deve avviare nulla con autorun in corso"
assert STATE.get("weather_manual") is True
assert any("autorun in corso" in m for m in MSGS), MSGS
# stesso stato, secondo ciclo: NON deve ripetere l'avviso
MSGS.clear()
cycle()
assert not any("autorun in corso" in m for m in MSGS), "avviso una sola volta"
# autorun finito -> setup fermo -> promemoria manuale (weather_manual resta True)
MSGS.clear()
SNAP = snap(plan=False, capt=False)
cycle()
assert REC["start"] == [], "riavvio NON automatico dopo attivita' manuale"
assert any("puoi avviare la ripresa a mano" in m for m in MSGS), MSGS
print("C) autorun manuale + promemoria OK")

# ===================== D) pausa meteo mai riaperta -> flat all'alba =====================
MSGS.clear(); REC["teardown"].clear(); REC["reset"] = 0
STATE = {"night_id": "2026-07-24", "weather_closed": True,
         "teardown_done": True, "imaging_active": False}
IN_NIGHT = False
ROOF = "CLOSED"
cycle()
assert STATE.get("flat_stage") == "drying", STATE
assert REC["teardown"] == [True], "flat alba con cooler tenuto"
assert STATE.get("weather_closed") is None, "flag meteo azzerato all'alba"
assert any("pausa meteo" in m for m in MSGS), MSGS
print("D) pausa mai riaperta -> flat all'alba OK")

# ============= F) alba: piano avviato A MANO (mai adottato) -> STOP =============
MSGS.clear(); REC["teardown"].clear(); REC["reset"] = 0
STATE = {"night_id": "2026-07-24"}          # nessun imaging_active/teardown_done
IN_NIGHT = False
ROOF, SNAP = "OPEN", snap(plan=True)        # piano in corso, agente ignaro
cycle()
assert REC["teardown"] == [True], "piano manuale all'alba: teardown dovuto"
assert STATE.get("flat_stage") == "drying", STATE
assert STATE.get("dawn_plan_checked") is True
assert any("ANCORA IN CORSO" in m for m in MSGS), MSGS
print("F) alba: piano manuale fermato OK")

# ============= G) alba: AUTORUN in corso -> NON toccare =============
MSGS.clear(); REC["teardown"].clear()
STATE = {"night_id": "2026-07-24"}
SNAP = snap(plan=False, capt=True)          # autorun manuale (es. flat)
cycle()
assert REC["teardown"] == [], "autorun all'alba NON va fermato"
assert STATE.get("flat_stage") is None
assert STATE.get("dawn_plan_checked") is True
# secondo ciclo: la sonda non si ripete (una per notte)
SNAP = snap(plan=True)                      # anche se ora ci fosse un piano...
cycle()
assert REC["teardown"] == [], "sonda alba una sola volta per notte"
print("G) alba: autorun non toccato, sonda una volta OK")

# ===== H) BUG 2026-07-25: riapertura con is_plan_started STALE (true a piano
# fermo) -> NON deve adottare in silenzio: deve riavviare (weather_recovery) =====
MSGS.clear(); REC["start"].clear(); REC["teardown"].clear()
STATE = {"night_id": "2026-07-24", "nautical_announced": True,
         "weather_closed": True, "teardown_done": True, "imaging_active": False,
         "plan_name": "M31"}
IN_NIGHT = True
ROOF, SNAP = "OPEN", snap(plan=True, capt=False)   # flag stale: piano FERMO
cycle()
assert REC["start"] == ["M31"], f"doveva riavviare (flag stale): {REC['start']}"
assert STATE.get("weather_closed") is None
assert STATE.get("imaging_active") is True
assert any("RIPRESO" in m for m in MSGS), MSGS
print("H) flag stale a tetto riaperto -> riavvio (bug 2026-07-25) OK")

# ===== I) riapertura con piano DAVVERO in ripresa -> adozione CON avviso =====
MSGS.clear(); REC["start"].clear()
STATE = {"night_id": "2026-07-24", "nautical_announced": True,
         "weather_closed": True, "teardown_done": True, "imaging_active": False}
ROOF, SNAP = "OPEN", snap(plan=True, capt=True)    # in posa per davvero
cycle()
assert REC["start"] == [], "gia' in ripresa: NON deve riavviare"
assert STATE.get("weather_closed") is None
assert STATE.get("imaging_active") is True
assert STATE.get("teardown_done") is None
assert any("riadotto" in m for m in MSGS), f"adozione mai silenziosa: {MSGS}"
print("I) adozione piano gia' in ripresa con avviso OK")

# ===== J) tetto ANCORA CHIUSO, cattura in corso (riavvio manuale o ripresa
# autonoma): NON toccare nulla, solo AVVISO; alla riapertura -> adozione =====
MSGS.clear(); REC["start"].clear(); REC["teardown"].clear()
STATE = {"night_id": "2026-07-24", "nautical_announced": True,
         "weather_closed": True, "teardown_done": True, "imaging_active": False}
ROOF, SNAP = "CLOSED", snap(plan=True, capt=True)  # cattura sotto tetto chiuso
cycle()
assert REC["teardown"] == [], f"attivita' manuale: NON va toccata: {REC['teardown']}"
assert STATE.get("weather_closed") is True, "pausa meteo resta attiva"
assert any("CATTURA" in m and "Non tocco nulla" in m for m in MSGS), MSGS
# secondo ciclo entro il cooldown: nessuna ripetizione dell'avviso
MSGS.clear()
cycle()
assert MSGS == [], f"alert con cooldown, niente spam: {MSGS}"
# cattura finita -> l'alert si riarma; alla riapertura col piano in ripresa
# vera -> ADOZIONE con avviso (niente doppio start)
MSGS.clear(); REC["start"].clear()
ROOF, SNAP = "OPEN", snap(plan=True, capt=True)
cycle()
assert REC["start"] == [], "piano gia' in ripresa: nessun riavvio"
assert STATE.get("imaging_active") is True
assert any("riadotto" in m for m in MSGS), MSGS
print("J) cattura sotto tetto chiuso: solo avviso, poi adozione OK")

print("\nTUTTI I TEST WEATHER-REOPEN: OK")
