"""Test OFFLINE dei FLAT/DARK MANUALI dal menu Telegram (richiesta utente
2026-08-01): maltempo -> si chiude la sessione e si fanno subito flat e dark.
Copre le due meta' del meccanismo:
  BOT   : bottone Flat/Dark -> conferma -> stop piano/autorun, teardown, reset,
          cooler, fascia anticondensa, RICHIESTA scritta per l'agente
  AGENTE: richiesta presa in carico -> sync+diario -> asciugatura -> flat ->
          dark -> sync -> DOMANDA sullo spegnimento (niente shutdown d'ufficio)
          -> risposta 'keep' (rig acceso) o 'shutdown' (eseguito dal bot)
Nessun rig, nessuna rete: fake di AsiairControl/KASA/Telegram/sessionlog.
"""
import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from pathlib import Path as _P
ROOT = _P(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import sfro_agent as A

A.setup_logging("WARNING")
cfg = A.load_config(ROOT / "config.example.yaml")
TMP = Path(tempfile.mkdtemp(prefix="sfro-flatman-"))
# marker dello shutdown manuale nella sandbox: il bot lo SCRIVE davvero e non
# deve sporcare ne' la produzione ne' gli altri test
cfg["manual_shutdown_file"] = str(TMP / "manual_shutdown.json")
cfg["manual_flat_request_file"] = str(TMP / "flat_request.json")
cfg["manual_flat_reply_file"] = str(TMP / "flat_reply.json")
cfg["state_file"] = str(TMP / "state.json")
REQ = Path(cfg["manual_flat_request_file"])
REP = Path(cfg["manual_flat_reply_file"])
assert cfg["flat_flow"].get("manual_ask_timeout_minutes"), \
    "config: manual_ask_timeout_minutes per i flat manuali"
tz = ZoneInfo(cfg["timezone_observatory"])
files = {"telegram": A.Path("/no"), "kasa": A.Path("/no"), "asiair": A.Path("/no")}
state = {}

MSGS, KBS, CALLS = [], [], []
A.Telegram.send = lambda self, t, kb=None: (MSGS.append(t), KBS.append(kb))


class FakeSL:
    @staticmethod
    def ingest(cfg): return {"new": 2, "nights": ["2026-08-01"]}
    @staticmethod
    def finalize(cfg, nid, cause): CALLS.append(f"finalize({cause})"); return 1
    @staticmethod
    def push(cfg): return {}
    @staticmethod
    def update_and_push(cfg): return {"new": 0}
    @staticmethod
    def filters_gains_used(cfg, nid): return KB["filters_gains"]


A.SL = FakeSL

KB = dict(in_night=True, roof="CLOSED", asiair_ping=True, night="2026-08-01",
          snap=None, plug="ON", filters_gains=[("H", 100), ("S", 100)],
          exps={4: 2.5, 5: 3.1}, flats_status=(True, True, 3000, "12/30", ""),
          cooling=(True, 0.1, 0.0, True, ""), cooler_on_ok=(True, ""),
          set_output_ok=(True, ""), brightness_ok=(True, ""),
          slots_ok=(True, 30, "slot ok"), gain_ok=(True, ""),
          start_flats_ok=(True, ""), shutdown_ok=(True, ""), shutdown_kills=True)

A.nautical_window = lambda now, loc, tz, dep=12: (KB["in_night"], None, None, KB["night"])
A.vpn_diagnose = lambda host, probe="": {"vpn_up": True, "asiair_up": KB["asiair_ping"],
                                         "cause": ""}
A.http_get_json = lambda u, h: {"Value": KB["roof"] == "OPEN", "ErrorNumber": 0}
A.sync_pass = lambda sc, dry: (CALLS.append("sync"),
                               {"ok": True, "files": 7, "error": None})[1]
A.time.sleep = lambda s: None

A.AsiairControl.snapshot = lambda self: KB["snap"]
A.AsiairControl.start = lambda self, snap=None: (CALLS.append("plan_start"), (True, ""))[1]
A.AsiairControl.stop = lambda self: (CALLS.append("stop"), True)[1]
A.AsiairControl.reset_plan = lambda self: (CALLS.append("reset_plan"), (True, "azzerato"))[1]
A.AsiairControl.get_wheel_names = lambda self: ["L", "R", "G", "B", "S", "H", "O"]
A.AsiairControl.set_camera_gain = (
    lambda self, g: (CALLS.append(f"gain({g})"), KB["gain_ok"])[1])
A.AsiairControl.camera_cooling = lambda self: KB["cooling"]
A.AsiairControl.cooler_on = (
    lambda self: (CALLS.append("cooler_on"), KB["cooler_on_ok"])[1])
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
A.AsiairControl.close_flat = lambda self: (CALLS.append("close_flat"), (True, ""))[1]
A.AsiairControl.shutdown = lambda self: (CALLS.append("shutdown"), KB["shutdown_ok"])[1]
A.AsiairControl.connect_all = lambda self: (True, "5 device")


def fake_teardown(self, keep_cooler=False):
    CALLS.append(f"teardown(keep_cooler={keep_cooler})")
    td = {"stop": (True, ""), "home": (True, ""), "flat": KB.get("close_ok", (True, ""))}
    if not keep_cooler:
        td["cooler"] = (True, "")
    return td


A.AsiairControl.teardown = fake_teardown
A._kasa_connect = lambda cfg, files, http, tu: (
    "kc", "dev", [{"alias": "Asiair", "id": "1", "state": 1}], KB["plug"], None)
A._kasa_power_off_all = lambda kc, dev, dry: (CALLS.append("kasa_off_all"),
                                              (3, [], None))[1]
A.ping = lambda host: False if ("shutdown" in CALLS and KB["shutdown_kills"]) \
    else KB["asiair_ping"]

NOW = [datetime(2026, 8, 1, 2, 0, tzinfo=tz)]
_real_dt = A.datetime


class FakeDT(_real_dt):
    @classmethod
    def now(cls, tz=None):
        return NOW[0].astimezone(tz) if tz else NOW[0]


A.datetime = FakeDT


def snap(started=False, **kw):
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


# ---------------------------------------------------------------- lato BOT ---
import sfro_telegram as T                                       # noqa: E402

BOT_MSGS = []
T.datetime = FakeDT          # anche il bot vive nell'orologio finto del test
T.SfroBot.send = lambda self, t, keyboard=None, thread=None: BOT_MSGS.append(t)
T.SfroBot.diagnose = lambda self, with_kasa=True: {
    "vpn_up": True, "kasa_ok": True, "kasa_err": None, "children": [],
    "plug": "ON", "kc": "kc", "dev": "dev", "managed": [],
    "asiair_ping": True, "asiair_rpc": KB.get("rpc_ok", True), "rpc_err": None}
bot = T.SfroBot(cfg)


def bot_call(label, fn, *a):
    BOT_MSGS.clear()
    CALLS.clear()
    fn(*a)
    print(f"### {label}")
    for m in BOT_MSGS:
        print("   TG>", m.replace(chr(10), " | ")[:120])
    print("   calls:", CALLS or "-", "| req:", REQ.exists(), "| rep:", REP.exists())


def bot_has(frag):
    return any(frag in m for m in BOT_MSGS)


# B0: menu e conferma esistono
assert any(b.get("callback_data") == "m:flatdark" for row in T.MENU_KB for b in row), \
    "bottone Flat/Dark nel menu"
assert "flatdark" in T.CONFIRM_TEXT, "conferma Si'/No per Flat/Dark"

# B1: piano in corso -> viene FERMATO, richiesta scritta per l'agente
KB["snap"] = snap(started=True)
bot_call("B1 bottone Flat/Dark (piano in corso)", bot.do_flatdark, None)
assert REQ.exists(), "richiesta per l'agente scritta"
assert bot_has("fermo piano «Sideris»"), BOT_MSGS
assert "teardown(keep_cooler=True)" in CALLS and "reset_plan" in CALLS, CALLS
assert "set_output(dew_heater,100)" in CALLS, CALLS
assert bot_has("Asciugatura 30 min"), BOT_MSGS
req = json.loads(REQ.read_text())
assert req["ask_shutdown"] and req["closed_ts"], req

# B1b: seconda pressione con richiesta gia' pendente -> non riparte
bot_call("B1b doppia pressione", bot.do_flatdark, None)
assert bot_has("già inviata"), BOT_MSGS

# ------------------------------------------------------------- lato AGENTE ---
# A1: l'agente prende in carico: sync light + diario + asciugatura
cyc("A1 presa in carico", now=datetime(2026, 8, 1, 2, 3, tzinfo=tz),
    roof="CLOSED", snap=snap())
assert not REQ.exists(), "richiesta consumata"
assert state["flat_stage"] == "drying" and state["flat_manual"], state
assert "sync" in CALLS and "finalize(flat_manuale)" in CALLS, CALLS
assert msg_has("Flat/Dark MANUALI presi in carico"), MSGS
assert msg_has("Attesa Flats"), MSGS
# il piano NON deve ripartire anche se il tetto si riapre durante l'attesa
cyc("A1b tetto riaperto durante l'asciugatura", roof="OPEN")
assert "plan_start" not in CALLS, CALLS
assert state["flat_stage"] == "drying"

# A2: fine asciugatura -> flat H+S (gain 100, pannello 75%)
cyc("A2 flat", now=datetime(2026, 8, 1, 2, 35, tzinfo=tz), roof="CLOSED")
assert CALLS == ["gain(100)", "slots(flat,[4, 5])", "brightness(75)",
                 "start_flats"], CALLS
assert state["flat_stage"] == "running"

# A3: flat finiti -> dark
cyc("A3 dark", flats_status=(True, False, 0, None, ""))
assert "slots(dark,[4, 5])" in CALLS, CALLS
assert state["flat_stage"] == "darks"

# A4: dark finiti -> sync e DOMANDA (niente shutdown automatico!)
cyc("A4 domanda spegnimento", flats_status=(True, False, 0, None, ""))
assert "sync" in CALLS, CALLS
assert "shutdown" not in CALLS and "kasa_off_all" not in CALLS, CALLS
assert "set_output(dew_heater,5)" not in CALLS, CALLS   # la fascia anticondensa la mette il bot
assert state["flat_stage"] == "ask_shutdown", state["flat_stage"]
assert msg_has("Spengo l'ASIAIR e tolgo corrente"), MSGS
kb = [k for k in KBS if k]
assert kb and kb[-1][0][0]["callback_data"] == "fs:yes" \
    and kb[-1][0][1]["callback_data"] == "fs:no", kb

# A5: nessuna risposta -> ridomanda al ciclo successivo (mai silente)
cyc("A5 promemoria", now=datetime(2026, 8, 1, 2, 50, tzinfo=tz))
assert msg_has("Aspetto ancora la risposta"), MSGS
assert state["flat_stage"] == "ask_shutdown"

# ----------------------------------------------------- risposta 'NO' dal bot --
assert bot.flat_stage() == "ask_shutdown", bot.flat_stage()
bot_call("A6a bottone 'No, lascia acceso'", bot.do_flat_keep, None)
assert bot_has("rig lasciato ACCESO") and REP.exists(), BOT_MSGS
cyc("A6 risposta: lascia acceso", now=datetime(2026, 8, 1, 2, 55, tzinfo=tz))
assert state["flat_stage"] == "done", state["flat_stage"]
assert msg_has("rig lasciato ACCESO"), MSGS
assert not REP.exists(), "risposta consumata"
assert "shutdown" not in CALLS and "kasa_off_all" not in CALLS, CALLS

# ------------------------------------------------ risposta 'SI'' dal bot ------
# si rifa' il giro fino alla domanda, poi si risponde 'spegni'
state.clear()
REQ.write_text(json.dumps({"ts": "x", "closed_ts": "2026-08-02T02:00:00-05:00",
                           "source": "telegram", "ask_shutdown": True,
                           "cause": "flat_manuale"}))
KB.update(night="2026-08-02", flats_status=(True, True, 3000, "1/30", ""))
cyc("C1 nuova richiesta", now=datetime(2026, 8, 2, 2, 5, tzinfo=tz), snap=snap())
cyc("C2 flat", now=datetime(2026, 8, 2, 2, 40, tzinfo=tz))
cyc("C3 dark", flats_status=(True, False, 0, None, ""))
cyc("C4 domanda", flats_status=(True, False, 0, None, ""))
assert state["flat_stage"] == "ask_shutdown"

# il bot esegue lo spegnimento: prima marca 'in corso' (l'agente tace)
bot.flat_reply("shutdown", False, False, "in corso")
cyc("C5 spegnimento in corso -> agente in silenzio")
assert not MSGS, MSGS
assert state["flat_stage"] == "ask_shutdown"

bot_call("C6 do_flat_shutdown (bot)", bot.do_flat_shutdown, None)
assert "set_output(dew_heater,5)" in CALLS, CALLS      # fascia anticondensa a riposo
assert "shutdown" in CALLS and "kasa_off_all" in CALLS, CALLS
assert CALLS.index("set_output(dew_heater,5)") < CALLS.index("shutdown"), CALLS
assert bot_has("Shutdown COMPLETATO"), BOT_MSGS
rep = json.loads(REP.read_text())
assert rep == {**rep, "answer": "shutdown", "done": True, "ok": True}, rep

cyc("C7 agente chiude il flusso")
assert state["flat_stage"] == "done", state["flat_stage"]
assert msg_has("ASIAIR spento e prese OFF"), MSGS

# E: nessuna risposta entro il timeout -> non si spegne niente, rig ACCESO
state.clear()
state.update({"night_id": "2026-08-03", "flat_stage": "ask_shutdown",
              "flat_manual": True, "flat_ask_shutdown": True,
              "flat_ask_ts": datetime(2026, 8, 3, 2, 0, tzinfo=tz).isoformat()})
cyc("E timeout senza risposta", now=datetime(2026, 8, 3, 3, 5, tzinfo=tz),
    night="2026-08-03")
assert state["flat_stage"] == "done", state["flat_stage"]
assert msg_has("Nessuna risposta") and msg_has("rig lasciato ACCESO"), MSGS
assert "shutdown" not in CALLS and "kasa_off_all" not in CALLS, CALLS

# ------------------------------------------------- casi di rifiuto del bot ----
# D1: pannello NON chiuso -> flat annullati e richiesta rimossa
state.clear()
CALLS.clear()
KB.update(close_ok=(False, "OF2 non risponde"), snap=snap())
bot_call("D1 OF2 non chiuso", bot.do_flatdark, None)
assert not REQ.exists(), "richiesta ritirata"
assert bot_has("pannello OF2 NON chiuso"), BOT_MSGS
KB["close_ok"] = (True, "")

# D2: ASIAIR in errore -> niente richiesta
KB["rpc_ok"] = False
bot_call("D2 ASIAIR non pronto", bot.do_flatdark, None)
assert not REQ.exists() and bot_has("sistema non pronto"), BOT_MSGS
KB["rpc_ok"] = True

# D3: cooler spento -> acceso + anti-dew, poi si prosegue
KB.update(cooling=(True, 12.4, 0.0, False, ""))
bot_call("D3 cooler spento", bot.do_flatdark, None)
assert "cooler_on" in CALLS and "anti_dew" in CALLS, CALLS
assert bot_has("Cooler era SPENTO"), BOT_MSGS
assert REQ.exists()
REQ.unlink()

# D4: flusso flat gia' in corso -> non riparte
A.save_state(A.Path(cfg["state_file"]), {"flat_stage": "running"})
bot_call("D4 flusso gia' in corso", bot.do_flatdark, None)
assert bot_has("GIÀ in corso") and not REQ.exists(), BOT_MSGS

print("\nTUTTI GLI SCENARI OK")
