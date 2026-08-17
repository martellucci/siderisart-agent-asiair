"""Test OFFLINE dello STOP dell'ATTESA FLAT dal menu Telegram (richiesta utente
2026-08-15): durante i 30 minuti di asciugatura tra la fine del piano e i flat
si deve poter annullare tutto e riprendersi il rig.
Copre le due meta' del meccanismo:
  BOT   : bottone Stop Flat -> conferma -> «No» = il countdown prosegue,
          «Si'» = RICHIESTA scritta per l'agente; rifiuti quando non c'e'
          niente da interrompere o l'autorun flat/dark e' gia' partito
  AGENTE: richiesta presa in carico -> fase 'cancelled' SENZA toccare il rig,
          flat che non partono piu' a countdown scaduto, promemoria periodico
          di spegnere che RIPRENDE, nuova notte che riparte pulita
Nessun rig, nessuna rete: fake di AsiairControl/KASA/Telegram/sessionlog.
"""
import json
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from pathlib import Path as _P
ROOT = _P(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import sfro_agent as A

A.setup_logging("WARNING")
cfg = A.load_config(ROOT / "config.example.yaml")
TMP = Path(tempfile.mkdtemp(prefix="sfro-flatstop-"))
# tutti i file di scambio nella sandbox: il bot li SCRIVE davvero
cfg["manual_shutdown_file"] = str(TMP / "manual_shutdown.json")
cfg["manual_flat_request_file"] = str(TMP / "flat_request.json")
cfg["manual_flat_reply_file"] = str(TMP / "flat_reply.json")
cfg["flat_cancel_request_file"] = str(TMP / "flat_cancel.json")
cfg["state_file"] = str(TMP / "state.json")
CANC = Path(cfg["flat_cancel_request_file"])
assert cfg.get("flat_cancel_request_file"), "config: flat_cancel_request_file"
tz = ZoneInfo(cfg["timezone_observatory"])
files = {"telegram": A.Path("/no"), "kasa": A.Path("/no"), "asiair": A.Path("/no")}
state = {}

MSGS, CALLS = [], []
A.Telegram.send = lambda self, t, kb=None: MSGS.append(t)


class FakeSL:
    @staticmethod
    def ingest(cfg): return {"new": 2, "nights": ["2026-08-15"]}
    @staticmethod
    def finalize(cfg, nid, cause): CALLS.append(f"finalize({cause})"); return 1
    @staticmethod
    def push(cfg): return {}
    @staticmethod
    def build_report(cfg): return {}
    @staticmethod
    def update_and_push(cfg): return {"new": 0}
    @staticmethod
    def filters_gains_used(cfg, nid): return KB["filters_gains"]


A.SL = FakeSL

KB = dict(in_night=True, roof="OPEN", asiair_ping=True, night="2026-08-15",
          snap=None, plug="ON", filters_gains=[("H", 100)],
          exps={5: 3.1}, flats_status=(True, True, 3000, "12/30", ""),
          cooling=(True, 0.1, 0.0, True, ""), slots_ok=(True, 30, "slot ok"),
          gain_ok=(True, ""), brightness_ok=(True, ""), set_output_ok=(True, ""),
          start_flats_ok=(True, ""))

A.nautical_window = lambda now, loc, tz, dep=12: (KB["in_night"], None, None, KB["night"])
A.vpn_diagnose = lambda host, probe="": {"vpn_up": True, "asiair_up": KB["asiair_ping"],
                                         "cause": ""}
A.http_get_json = lambda u, h: {"Value": KB["roof"] == "OPEN", "ErrorNumber": 0}
A.sync_pass = lambda sc, dry: (CALLS.append("sync"),
                               {"ok": True, "files": 7, "error": None})[1]
A.time.sleep = lambda s: None

A.AsiairControl.snapshot = lambda self: KB["snap"]
A.AsiairControl.start = lambda self, snap=None: (CALLS.append("plan_start"), (True, ""))[1]
A.AsiairControl.reset_plan = lambda self: (CALLS.append("reset_plan"), (True, "azzerato"))[1]
A.AsiairControl.plan_left = lambda self: (True, 0, "1/1 target completati")
A.AsiairControl.get_wheel_names = lambda self: ["L", "R", "G", "B", "S", "H", "O"]
A.AsiairControl.set_camera_gain = (
    lambda self, g: (CALLS.append(f"gain({g})"), KB["gain_ok"])[1])
A.AsiairControl.camera_cooling = lambda self: KB["cooling"]
A.AsiairControl.cooler_on = (lambda self: (CALLS.append("cooler_on"), (True, ""))[1])
A.AsiairControl.ensure_anti_dew = (
    lambda self: (CALLS.append("anti_dew"), (True, "acceso"))[1])
A.AsiairControl.read_flat_exps = lambda self, idxs: {i: KB["exps"][i] for i in idxs}
A.AsiairControl.configure_autorun_slots = (
    lambda self, kind, idxs, max_exp=None, exp_by_filter=None:
    (CALLS.append(f"slots({kind},{sorted(idxs)})"), KB["slots_ok"])[1])
A.AsiairControl.set_flat_brightness = (
    lambda self, v: (CALLS.append(f"brightness({v})"), KB["brightness_ok"])[1])
A.AsiairControl.set_output = (
    lambda self, t, v: (CALLS.append(f"set_output({t},{v})"), KB["set_output_ok"])[1])
A.AsiairControl.start_flats = (
    lambda self: (CALLS.append("start_flats"), KB["start_flats_ok"])[1])
A.AsiairControl.flats_status = lambda self: KB["flats_status"]
A.AsiairControl.shutdown = lambda self: (CALLS.append("shutdown"), (True, ""))[1]
A.AsiairControl.connect_all = lambda self: (True, "5 device")


def fake_teardown(self, keep_cooler=False):
    CALLS.append(f"teardown(keep_cooler={keep_cooler})")
    td = {"stop": (True, ""), "home": (True, ""), "flat": (True, "")}
    if not keep_cooler:
        td["cooler"] = (True, "")
    return td


A.AsiairControl.teardown = fake_teardown
A._kasa_connect = lambda cfg, files, http, tu: (
    "kc", "dev", [{"alias": "Asiair", "id": "1", "state": 1}], KB["plug"], None)
A._kasa_power_off_all = lambda kc, dev, dry: (CALLS.append("kasa_off_all"),
                                              (3, [], None))[1]
A.ping = lambda host: KB["asiair_ping"]

NOW = [datetime(2026, 8, 15, 3, 0, tzinfo=tz)]
_real_dt = A.datetime


class FakeDT(_real_dt):
    @classmethod
    def now(cls, tz=None):
        return NOW[0].astimezone(tz) if tz else NOW[0]


A.datetime = FakeDT


def snap(started=True, **kw):
    s = {"reachable": True, "capturing": started, "plan_started": started,
         "plan_name": "Sideris", "cam_open": True, "focuser_connected": True,
         "wheel_connected": True, "mount_connected": True, "guide_connected": True,
         "has_plan": True, "flat_open": True}
    s.update(kw)
    return s


def cyc(label, now=None, **kw):
    if now is not None:
        NOW[0] = now
    KB.update(kw)
    MSGS.clear()
    CALLS.clear()
    A.run_cycle(cfg, state, files, dry_run=False)
    A.save_state(A.Path(cfg["state_file"]), state)   # come fa main()
    print(f"### {label}")
    for m in MSGS:
        print("   TG>", m.replace(chr(10), " | ")[:120])
    print("   calls:", CALLS or "-", "| stage:", state.get("flat_stage"))


def msg_has(frag):
    return any(frag in m for m in MSGS)


def no_flat_calls():
    """Nessun comando che faccia partire flat o dark."""
    return not [c for c in CALLS
                if c.startswith(("gain(", "slots(", "brightness(", "start_flats"))]


# ---------------------------------------------------------------- lato BOT ---
import sfro_telegram as T                                       # noqa: E402

BOT_MSGS = []
T.datetime = FakeDT
T.SfroBot.send = lambda self, t, keyboard=None, thread=None: BOT_MSGS.append(t)
bot = T.SfroBot(cfg)
bot.chat_id = "42"          # chat finta: le callback del test arrivano da qui
bot.api = lambda method, **p: BOT_MSGS.append(f"[api {method}]") or {"ok": True}


def bot_call(label, fn, *a):
    BOT_MSGS.clear()
    CALLS.clear()
    fn(*a)
    print(f"### {label}")
    for m in BOT_MSGS:
        print("   TG>", m.replace(chr(10), " | ")[:120])
    print("   calls:", CALLS or "-", "| stop-req:", CANC.exists())


def bot_has(frag):
    return any(frag in m for m in BOT_MSGS)


def set_stage(stage):
    A.save_state(A.Path(cfg["state_file"]), {"flat_stage": stage} if stage else {})


# B0: bottone e conferma esistono
assert any(b.get("callback_data") == "m:stopflat" for row in T.MENU_KB for b in row), \
    "bottone Stop Flat nel menu"
assert "stopflat" in T.CONFIRM_TEXT, "conferma Si'/No per lo Stop Flat"

# B1: niente in corso -> rifiuto, nessun file
set_stage(None)
bot_call("B1 nessuna attesa in corso", bot.do_stopflat, None)
assert bot_has("Niente da interrompere") and not CANC.exists(), BOT_MSGS

# B2: flat/dark GIA' partiti -> un autorun non si tocca
for st, frag in (("running", "FLAT sono già partiti"),
                 ("darks", "DARK sono già partiti"),
                 ("ask_shutdown", "aspettando la tua risposta"),
                 ("done", "già concluso")):
    set_stage(st)
    bot_call(f"B2 fase {st}", bot.do_stopflat, None)
    assert bot_has(frag) and not CANC.exists(), (st, BOT_MSGS)

# B3: durante l'ATTESA -> richiesta scritta per l'agente, NIENTE comandi al rig
set_stage("drying")
bot_call("B3 stop durante l'attesa", bot.do_stopflat, None)
assert CANC.exists(), "richiesta di stop scritta"
assert CALLS == [], f"il bot non deve comandare nulla all'ASIAIR: {CALLS}"
assert bot_has("Interruzione richiesta"), BOT_MSGS
assert json.loads(CANC.read_text())["source"] == "telegram"

# B4: seconda pressione con richiesta pendente -> non si duplica
bot_call("B4 doppia pressione", bot.do_stopflat, None)
assert bot_has("già richiesta"), BOT_MSGS
CANC.unlink()

# B5: il «No» della conferma NON scrive nulla e dice che il countdown prosegue
BOT_MSGS.clear()
bot.pending[7] = ("stopflat", time.time())
bot.on_callback({"id": "cb", "data": "c:no",
                 "message": {"chat": {"id": 42}, "message_id": 7}})
print("### B5 conferma «No»")
for m in BOT_MSGS:
    print("   TG>", m[:120])
assert bot_has("l'attesa flat prosegue"), BOT_MSGS
assert not CANC.exists(), "il «No» non deve scrivere la richiesta"
# le altre operazioni mantengono il messaggio di sempre
BOT_MSGS.clear()
bot.pending[8] = ("shutdown", time.time())
bot.on_callback({"id": "cb", "data": "c:no",
                 "message": {"chat": {"id": 42}, "message_id": 8}})
assert bot_has("Operazione annullata"), BOT_MSGS

# ------------------------------------------------------------- lato AGENTE ---
# A1: notte con ripresa in corso
state.clear()
cyc("A1 ripresa", now=datetime(2026, 8, 15, 3, 0, tzinfo=tz), snap=snap())
assert state["imaging_active"]

# A2: alba -> teardown, asciugatura e countdown
cyc("A2 alba -> attesa flat", now=datetime(2026, 8, 15, 11, 0, tzinfo=tz),
    in_night=False, snap=snap(started=False))
assert state["flat_stage"] == "drying", state["flat_stage"]
assert msg_has("Attesa Flats"), MSGS

# A3: countdown in corso, ancora nessuna richiesta
cyc("A3 countdown", now=datetime(2026, 8, 15, 11, 10, tzinfo=tz))
assert state["flat_stage"] == "drying" and msg_has("Attesa Flats"), MSGS

# A4: arriva lo STOP -> fase 'cancelled' e NESSUN comando al rig
CANC.write_text(json.dumps({"ts": "x", "source": "telegram"}))
cyc("A4 stop preso in carico", now=datetime(2026, 8, 15, 11, 15, tzinfo=tz))
assert not CANC.exists(), "richiesta consumata"
assert state["flat_stage"] == "cancelled", state["flat_stage"]
assert msg_has("Attesa flat INTERROTTA"), MSGS
assert not msg_has("Attesa Flats"), "niente countdown dopo lo stop"
assert CALLS == [], f"il rig non va toccato: {CALLS}"
assert state.get("flat_cancel_ts")
# il promemoria non si accavalla al messaggio di stop (che lo dice gia')
assert not msg_has("ancora ACCESI"), MSGS

# A5: countdown scaduto -> i flat NON partono piu'
cyc("A5 dopo i 30 minuti", now=datetime(2026, 8, 15, 11, 45, tzinfo=tz))
assert state["flat_stage"] == "cancelled", state["flat_stage"]
assert no_flat_calls(), CALLS
assert "shutdown" not in CALLS and "kasa_off_all" not in CALLS, CALLS

# A6: il PROMEMORIA periodico di spegnere RIPRENDE (richiesta esplicita utente)
cyc("A6 promemoria spegnimento", now=datetime(2026, 8, 15, 12, 0, tzinfo=tz))
assert msg_has("ancora ACCESI"), MSGS
assert no_flat_calls(), CALLS

# A7: STESSA notte, tetto aperto e device tutti collegati -> il piano NON
# riparte da solo (lo blocca teardown_done): il rig resta in carico all'utente
cyc("A7 nessun riavvio del piano", now=datetime(2026, 8, 15, 12, 30, tzinfo=tz),
    in_night=True, night="2026-08-15", roof="OPEN", snap=snap(started=False))
assert "plan_start" not in CALLS, CALLS
assert state["flat_stage"] == "cancelled", state["flat_stage"]
assert no_flat_calls(), CALLS

# A8: NUOVA notte -> stato flat azzerato e regole normali (scelta utente
# 2026-08-15: l'annullo vale per quella finestra flat, non per sempre)
cyc("A8 nuova notte", now=datetime(2026, 8, 16, 3, 0, tzinfo=tz),
    in_night=True, night="2026-08-16", snap=snap(started=False))
assert not state.get("flat_stage"), state.get("flat_stage")
assert not state.get("flat_cancel_ts")
assert "plan_start" in CALLS, CALLS

# A9: richiesta STANTIA (flat gia' partiti): consumata e ignorata, flat avanti
state.clear()
state.update({"night_id": "2026-08-16", "teardown_done": True,
              "flat_stage": "running", "flat_manual": False,
              "flat_started_ts": datetime(2026, 8, 16, 11, 0, tzinfo=tz).isoformat(),
              "flat_groups": [[100, 75, [5], "H"]], "flat_letters": {"5": "H"},
              "flat_times": {}})
CANC.write_text(json.dumps({"ts": "x", "source": "telegram"}))
cyc("A9 richiesta stantia con flat in corso",
    now=datetime(2026, 8, 16, 11, 10, tzinfo=tz), in_night=False,
    night="2026-08-16", flats_status=(True, True, 600, "12/30", ""))
assert not CANC.exists(), "richiesta consumata comunque"
assert state["flat_stage"] == "running", state["flat_stage"]
assert msg_has("ignorata") and msg_has("running"), MSGS
assert msg_has("in corso: posa 12/30"), MSGS   # i flat proseguono davvero

print("\nTUTTI I TEST FLAT-STOP: OK")
