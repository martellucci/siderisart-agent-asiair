"""Test OFFLINE del flusso flat/dark PER GAIN (richiesta utente 2026-07-24):
- luminosita' fisse LRGB 50% / SHO 75%
- gruppi flat per (gain camera, luminosita' pannello), gain impostato sulla
  camera (slot a gain default -10000)
- tempi AUTO letti dagli slot a fine gruppo (read_flat_exps)
- dark flat: un autorun PER GAIN, exp dello slot = tempo del flat corrispondente
- stesso filtro con due gain -> due passate flat e due dark
"""
import sys
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path as _P
ROOT = _P(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import sfro_agent as A

A.setup_logging("WARNING")
cfg = A.load_config(ROOT / "config.example.yaml")
cfg["manual_shutdown_file"] = "/nonexistent/manual_shutdown.json"
assert cfg["flat_flow"]["brightness_by_filter"] == {
    "L": 50, "R": 50, "G": 50, "B": 50, "S": 75, "H": 75, "O": 75}, \
    "config: LRGB 50 / SHO 75"
assert cfg["flat_flow"]["brightness_by_filter_gain0"] == {
    "R": 67, "G": 67, "B": 67}, \
    "config: RGB a gain 0 -> 67% (tetto ~15s del tempo AUTO, 2026-07-26)"
tz = ZoneInfo(cfg["timezone_observatory"])
files = {"telegram": A.Path("/no"), "kasa": A.Path("/no"), "asiair": A.Path("/no")}
state = {}

MSGS, CALLS = [], []
A.Telegram.send = lambda self, t, kb=None: MSGS.append(t)


class FakeSL:
    @staticmethod
    def ingest(cfg): return {"new": 0, "nights": []}
    @staticmethod
    def finalize(cfg, nid, cause): return 1
    @staticmethod
    def push(cfg): return {}
    @staticmethod
    def update_and_push(cfg): return {"new": 0}
    @staticmethod
    def filters_gains_used(cfg, nid): return KB["filters_gains"]


A.SL = FakeSL

KB = dict(in_night=True, roof="OPEN", asiair_ping=True, night="2026-07-24",
          snap=None, plug="ON",
          filters_gains=[("G", 0), ("H", 100), ("L", 100), ("S", 100)],
          exps={0: 1.6, 2: 2.2, 4: 2.5, 5: 3.1},
          flats_status=(True, True, 3000, "12/30", ""),
          cooling=(True, 0.1, 0.0, True, ""), cooler_on_ok=(True, ""),
          set_output_ok=(True, ""), brightness_ok=(True, ""),
          slots_ok=(True, 30, "slot ok"), gain_ok=(True, ""),
          start_flats_ok=(True, ""), shutdown_ok=(True, ""),
          shutdown_kills=True)

A.nautical_window = lambda now, loc, tz, dep=12: (KB["in_night"], None, None, KB["night"])
A.vpn_diagnose = lambda host, probe="": {"vpn_up": True, "cause": ""}
A.http_get_json = lambda u, h: {"Value": KB["roof"] == "OPEN", "ErrorNumber": 0}
A.sync_pass = lambda sc, dry: (CALLS.append("sync"),
                               {"ok": True, "files": 7, "error": None})[1]
A.time.sleep = lambda s: None

A.AsiairControl.snapshot = lambda self: KB["snap"]
A.AsiairControl.start = lambda self, snap=None: (CALLS.append("plan_start"), (True, ""))[1]
A.AsiairControl.reset_plan = lambda self: (True, "già azzerato")
A.AsiairControl.get_wheel_names = lambda self: ["L", "R", "G", "B", "S", "H", "O"]
A.AsiairControl.set_camera_gain = (
    lambda self, g: (CALLS.append(f"gain({g})"), KB["gain_ok"])[1])
A.AsiairControl.camera_cooling = lambda self: KB["cooling"]
A.AsiairControl.cooler_on = (
    lambda self: (CALLS.append("cooler_on"), KB["cooler_on_ok"])[1])
A.AsiairControl.ensure_anti_dew = (
    lambda self: (CALLS.append("anti_dew"), (True, "acceso"))[1])
A.AsiairControl.read_flat_exps = (
    lambda self, idxs: {i: KB["exps"][i] for i in idxs})
A.AsiairControl.configure_autorun_slots = (
    lambda self, kind, idxs, max_exp=None, exp_by_filter=None:
    (CALLS.append(f"slots({kind},{sorted(idxs)},exp={exp_by_filter and dict(sorted(exp_by_filter.items()))})"),
     KB["slots_ok"])[1])
A.AsiairControl.set_flat_brightness = (
    lambda self, v: (CALLS.append(f"brightness({v})"), KB["brightness_ok"])[1])
A.AsiairControl.set_output = (
    lambda self, t, v: (CALLS.append(f"set_output({t},{v})"), KB["set_output_ok"])[1])
A.AsiairControl.start_flats = (
    lambda self: (CALLS.append("start_flats"), KB["start_flats_ok"])[1])
A.AsiairControl.flats_status = lambda self: KB["flats_status"]
A.AsiairControl.shutdown = (
    lambda self: (CALLS.append("shutdown"), KB["shutdown_ok"])[1])


def fake_teardown(self, keep_cooler=False):
    CALLS.append(f"teardown(keep_cooler={keep_cooler})")
    td = {"stop": (True, ""), "home": (True, ""), "flat": (True, "")}
    if not keep_cooler:
        td["cooler"] = (True, "")
    return td


A.AsiairControl.teardown = fake_teardown
A._kasa_connect = lambda cfg, files, http, tu: (
    "kc", "dev", [{"alias": "Asiair", "id": "1", "state": 1},
                  {"alias": "Mount", "id": "2", "state": 1}], KB["plug"], None)
A._kasa_power_off_all = lambda kc, dev, dry: (CALLS.append("kasa_off_all"),
                                              (3, [], None))[1]


def fake_ping(host):
    if "shutdown" in CALLS and KB["shutdown_kills"]:
        return False
    return KB["asiair_ping"]


A.ping = fake_ping

NOW = [datetime(2026, 7, 24, 12, 30, tzinfo=tz)]
_real_dt = A.datetime


class FakeDT(_real_dt):
    @classmethod
    def now(cls, tz=None):
        return NOW[0].astimezone(tz) if tz else NOW[0]


A.datetime = FakeDT


def snap(started=False, **kw):
    s = {"reachable": True, "capturing": False, "plan_started": started,
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
    print(f"### {label}")
    for m in MSGS:
        print("   TG>", m.replace(chr(10), " | ")[:120])
    print("   calls:", CALLS or "-", "| stage:", state.get("flat_stage"))


def msg_has(frag):
    return any(frag in m for m in MSGS)


# N1: ripresa, poi alba -> teardown + drying
cyc("N1a ripresa", in_night=True, snap=snap(started=True))
assert state.get("imaging_active")
cyc("N1b alba -> teardown", now=datetime(2026, 7, 24, 12, 40, tzinfo=tz),
    in_night=False)
assert state.get("flat_stage") == "drying"

# N2: fine attesa -> PRIMO gruppo: S+H @ gain 100, pannello 75%
cyc("N2 gruppo (100,75)", now=datetime(2026, 7, 24, 13, 11, tzinfo=tz))
assert CALLS == ["gain(100)", "slots(flat,[4, 5],exp=None)", "brightness(75)",
                 "start_flats"], CALLS
assert msg_has("per H+S (gain 100)"), MSGS
assert state.get("flat_groups") == [[100, 75, [4, 5], "H+S"],
                                    [100, 50, [0], "L"],
                                    [0, 67, [2], "G"]], state.get("flat_groups")
assert state.get("flat_stage") == "running"

# N2b: autorun in corso -> messaggio di avanzamento (mai silente, 2026-07-24)
cyc("N2b avanzamento", flats_status=(True, True, 250, "12/30", ""))
assert msg_has("Flat H+S (gain 100) in corso: posa 12/30, ~5 min residui"), MSGS
assert not CALLS, CALLS

# N3: gruppo finito -> tempi auto letti, parte L @ gain 100 al 50%
cyc("N3 -> gruppo (100,50)", flats_status=(True, False, 0, None, ""))
assert msg_has("Flat H+S (gain 100) completati"), MSGS
assert msg_has("Tempi auto: S 2.5s · H 3.1s"), MSGS
assert state["flat_times"] == {"4:100": 2.5, "5:100": 3.1}, state["flat_times"]
assert "gain(100)" in CALLS and "brightness(50)" in CALLS, CALLS
assert msg_has("per L (gain 100)"), MSGS

# N4: -> gruppo G @ gain 0 al 67% (RGB a gain 0: tetto ~15s, 2026-07-26)
cyc("N4 -> gruppo (0,67)", flats_status=(True, False, 0, None, ""))
assert msg_has("Flat L (gain 100) completati"), MSGS
assert "gain(0)" in CALLS and "brightness(67)" in CALLS, CALLS
assert msg_has("per G (gain 0)"), MSGS
assert state["flat_times"]["0:100"] == 1.6

# N5: ultimo gruppo flat finito -> pannello 5, DARK gain 100 (H+L+S) con exp dei flat
cyc("N5 -> dark gain 100", flats_status=(True, False, 0, None, ""))
assert msg_has("Flat G (gain 0) completati"), MSGS
assert "brightness(5)" in CALLS, CALLS
assert "gain(100)" in CALLS, CALLS
assert "slots(dark,[0, 4, 5],exp={0: 1.6, 4: 2.5, 5: 3.1})" in CALLS, CALLS
assert msg_has("Dark flat H+L+S (gain 100) avviati"), MSGS
assert msg_has("Pose: L 1.6s \u00b7 S 2.5s \u00b7 H 3.1s"), MSGS
assert state.get("flat_stage") == "darks"
assert state["flat_times"]["2:0"] == 2.2

# N6: dark 100 finiti -> dark gain 0 (G) con exp del suo flat
cyc("N6 -> dark gain 0", flats_status=(True, False, 0, None, ""))
assert msg_has("Dark flat H+L+S (gain 100) completati"), MSGS
assert "gain(0)" in CALLS, CALLS
assert "slots(dark,[2],exp={2: 2.2})" in CALLS, CALLS
assert msg_has("Dark flat G (gain 0) avviati"), MSGS

# N7: tutti i dark finiti -> sync, fascia anticondensa 5% (spenta), shutdown, KASA off
cyc("N7 chiusura", flats_status=(True, False, 0, None, ""))
assert msg_has("Dark flat G (gain 0) completati"), MSGS
assert "sync" in CALLS and "set_output(dew_heater,5)" in CALLS, CALLS
i_sync, i_heat, i_shut = (CALLS.index("sync"),
                          CALLS.index("set_output(dew_heater,5)"),
                          CALLS.index("shutdown"))
assert i_sync < i_heat < i_shut, CALLS
assert "kasa_off_all" in CALLS and state.get("flat_stage") == "done"

# N8: STESSO filtro con DUE gain (G a 0 e 100) -> due passate flat e due dark
state.clear()
KB.update(filters_gains=[("G", 0), ("G", 100)], exps={2: 2.0},
          flats_status=(True, True, 3000, "12/30", ""))
cyc("N8a ripresa nuova notte", now=datetime(2026, 7, 25, 5, 0, tzinfo=tz),
    in_night=True, night="2026-07-25", snap=snap(started=True))
cyc("N8b alba", now=datetime(2026, 7, 25, 12, 40, tzinfo=tz), in_night=False)
cyc("N8c gruppo G@100", now=datetime(2026, 7, 25, 13, 11, tzinfo=tz))
assert state.get("flat_groups") == [[100, 50, [2], "G"], [0, 67, [2], "G"]], \
    state.get("flat_groups")
assert "gain(100)" in CALLS, CALLS
KB["exps"] = {2: 2.0}
cyc("N8d gruppo G@0", flats_status=(True, False, 0, None, ""))
assert "gain(0)" in CALLS, CALLS
assert state["flat_times"] == {"2:100": 2.0, "2:0": 2.0} or \
       state["flat_times"] == {"2:100": 2.0}, state["flat_times"]
# tempo AUTO con la coda di decimali che il 27/7 ha fermato il flusso
KB["exps"] = {2: 8.190001}   # il flat a gain 0 calcola un tempo diverso
cyc("N8e -> dark", flats_status=(True, False, 0, None, ""))
assert state["flat_times"] == {"2:100": 2.0, "2:0": 8.190001}, state["flat_times"]
assert "slots(dark,[2],exp={2: 2.0})" in CALLS, CALLS   # prima gain 100
cyc("N8f dark G@0", flats_status=(True, False, 0, None, ""))
# nello slot dark ci va il tempo del flat ARROTONDATO al CENTESIMO (2026-08-15:
# con 1 decimale diventava 8.2 e PixInsight non accoppiava flat e dark flat)
assert "slots(dark,[2],exp={2: 8.19})" in CALLS, CALLS
assert msg_has("Pose: G 8.19s"), MSGS
cyc("N8g chiusura", flats_status=(True, False, 0, None, ""))
assert state.get("flat_stage") == "done"

# N9: tempi flat NON leggibili -> STOP (rig acceso)
state.clear()
KB.update(filters_gains=[("L", 100)], exps={0: 1.6},
          flats_status=(True, True, 3000, "12/30", ""))
cyc("N9a ripresa", now=datetime(2026, 7, 26, 5, 0, tzinfo=tz),
    in_night=True, night="2026-07-26", snap=snap(started=True))
cyc("N9b alba", now=datetime(2026, 7, 26, 12, 40, tzinfo=tz), in_night=False)
cyc("N9c gruppo L", now=datetime(2026, 7, 26, 13, 11, tzinfo=tz))
A.AsiairControl.read_flat_exps = lambda self, idxs: None
cyc("N9d exp illeggibili -> STOP", flats_status=(True, False, 0, None, ""))
assert state.get("flat_stage") == "error", state.get("flat_stage")
assert msg_has("INTERROTTO"), MSGS
assert "shutdown" not in CALLS and "kasa_off_all" not in CALLS, CALLS

# N10: GATE TEMPERATURA — cooler spento e camera calda: riaccende cooler+
# anti-dew e ASPETTA; a temperatura raggiunta parte il gruppo
state.clear()
KB.update(filters_gains=[("L", 100)], exps={0: 1.6},
          flats_status=(True, True, 3000, "12/30", ""),
          cooling=(True, 24.3, 0.0, False, ""))
A.AsiairControl.read_flat_exps = lambda self, idxs: {i: KB["exps"][i] for i in idxs}
cyc("N10a ripresa", now=datetime(2026, 7, 27, 5, 0, tzinfo=tz),
    in_night=True, night="2026-07-27", snap=snap(started=True))
cyc("N10b alba", now=datetime(2026, 7, 27, 12, 40, tzinfo=tz), in_night=False)
cyc("N10c camera calda -> attesa", now=datetime(2026, 7, 27, 13, 11, tzinfo=tz))
assert "cooler_on" in CALLS and "anti_dew" in CALLS, CALLS
assert msg_has("Cooler era SPENTO"), MSGS
assert msg_has("in attesa del raffreddamento"), MSGS
assert "start_flats" not in CALLS and state.get("flat_stage") == "drying"
assert state.get("flat_cool_ts")
# ancora calda (cooler ora acceso): solo attesa, nessun nuovo cooler_on
cyc("N10d ancora calda", now=datetime(2026, 7, 27, 13, 16, tzinfo=tz),
    cooling=(True, 9.8, 0.0, True, ""))
assert "cooler_on" not in CALLS, CALLS
assert msg_has("in attesa del raffreddamento"), MSGS
# temperatura raggiunta -> parte il gruppo
cyc("N10e a temperatura -> flat", now=datetime(2026, 7, 27, 13, 21, tzinfo=tz),
    cooling=(True, 0.4, 0.0, True, ""))
assert "start_flats" in CALLS, CALLS
assert state.get("flat_stage") == "running"
assert not state.get("flat_cool_ts")

# N11: raffreddamento oltre il tempo massimo -> STOP (rig acceso)
state.clear()
KB.update(cooling=(True, 15.0, 0.0, True, ""),
          flats_status=(True, True, 3000, "12/30", ""))
cyc("N11a ripresa", now=datetime(2026, 7, 28, 5, 0, tzinfo=tz),
    in_night=True, night="2026-07-28", snap=snap(started=True))
cyc("N11b alba", now=datetime(2026, 7, 28, 12, 40, tzinfo=tz), in_night=False)
cyc("N11c attesa raffreddamento", now=datetime(2026, 7, 28, 13, 11, tzinfo=tz))
assert state.get("flat_cool_ts")
cyc("N11d timeout -> STOP", now=datetime(2026, 7, 28, 13, 45, tzinfo=tz))
assert state.get("flat_stage") == "error", state.get("flat_stage")
assert msg_has("NON a temperatura"), MSGS

print("\nTUTTI I TEST FLAT-GAIN-FLOW: OK")
