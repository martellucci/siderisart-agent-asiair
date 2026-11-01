"""Test OFFLINE del ROTATORE CAA nel flusso flat (richiesta utente 2026-10-24):
- flat di ogni (filtro, gain) all'angolo di ripresa di OGNI soggetto:
  soggetto1 L a 30°, soggetto2 L+H a 40° -> flat L30, L40, H40
- i due lati del meridian flip (30° e 210° in cielo) sono la stessa posizione;
  angoli entro 2° = uno solo (40° e 41°)
- rotazione PRIMA di ogni gruppo, angolo nel riquadro
- dark NON ruotati: un dark per ogni tempo di flat distinto (L 2.89 e 2.91 ->
  due autorun dark), tempi uguali -> uno solo
- rotatore illeggibile o rotazione fallita -> STOP a rig acceso
- caa_rotate: code 268 (occupato) ritentato, 'idle' anticipato ignorato,
  verifica finale sulla posizione meccanica
- sessioni giornaliere: ogni soggetto riceve i flat del SUO angolo
Nessun rig, nessuna rete.
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
# il sync dopo flat/dark salva lo stato PRIMA della copia (2026-10-13): MAI
# sullo state.json di produzione
import tempfile                                                   # noqa: E402
cfg["state_file"] = str(A.Path(tempfile.mkdtemp()) / "state.json")
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
    @staticmethod
    def light_angles(cfg, nid): return KB.get("angles", {})


A.SL = FakeSL

KB = dict(in_night=True, roof="OPEN", asiair_ping=True, night="2026-07-24",
          snap=None, plug="ON",
          filters_gains=[("G", 0), ("H", 100), ("L", 100), ("S", 100)],
          exps={0: 1.6, 2: 2.2, 4: 2.5, 5: 3.1},
          flats_status=(True, True, 3000, "12/30", ""),
          cooling=(True, 0.1, 0.0, True, ""), cooler_on_ok=(True, ""),
          cooler_off_ok=(True, ""),
          set_output_ok=(True, ""), brightness_ok=(True, ""),
          slots_ok=(True, 30, "slot ok"), gain_ok=(True, ""),
          start_flats_ok=(True, ""), shutdown_ok=(True, ""),
          shutdown_kills=True)

A.nautical_window = lambda now, loc, tz, dep=12: (KB["in_night"], None, None, KB["night"])
A.vpn_diagnose = lambda host, probe="": {"vpn_up": True, "cause": ""}
A.http_get_json = lambda u, h, session=None: {"Value": KB["roof"] == "OPEN", "ErrorNumber": 0}
A.sync_busy = lambda sc: None    # mai il lock di produzione nei test
A.sync_pass = lambda sc, dry, cfg=None, **kw: (CALLS.append("sync"),
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
A.AsiairControl.cooler_off = (
    lambda self: (CALLS.append("cooler_off"), KB["cooler_off_ok"])[1])
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
         "wheel_connected": True, "mount_connected": True, "guide_connected": True, "caa_connected": True,
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


# --- conversioni (misure live 24/10: home 52.4, reverse) --------------------
assert A.caa_sky_to_mech(12, 52.4) == 319.6 == A.caa_sky_to_mech(192, 52.4)
assert A.caa_sky_to_mech(2, 52.4) == 309.6
assert round(A.caa_mech_to_sky(319.4, 52.4), 1) == 11.8
assert A.angle_diff(359, 1) == -2 and A.angle_diff(1, 359) == 2
assert A.caa_clusters({337.6: 14, 347.6: 20, 348.6: 3}) == {
    337.6: 337.6, 347.6: 347.6, 348.6: 347.6}

# --- caa_rotate con un ASIAIR finto -----------------------------------------
_real_rotate = A.AsiairControl.caa_rotate


def fake_ac(seq_info, codes):
    ac = object.__new__(A.AsiairControl)
    ac.port, ac.timeout = 4700, 8
    infos, sent = list(seq_info), []
    ac.caa_info = lambda: infos.pop(0) if len(infos) > 1 else infos[0]
    ac._call1 = lambda port, m, p=None, timeout=None: (
        sent.append((m, p)), (codes.pop(0), None))[1]
    return ac, sent


def inf(mech, sky, state="idle"):
    return {"connected": True, "state": state, "mech": mech, "sky": sky,
            "home": 52.4, "reverse": True, "lo": 180.0, "hi": 360.0}


ac, sent = fake_ac([inf(319.4, 11.8), inf(315, 6, "moving"),
                    inf(310.1, 3.5),             # idle ANTICIPATO: non basta
                    inf(309.6, 2.0), inf(309.6, 2.0)], [268, 268, 0])
ok, det = _real_rotate(ac, 309.6)
assert ok and sent == [("move_caa", [2.0])] * 3, (ok, det, sent)
assert "ruotato a 309.6°" in det, det
ac, sent = fake_ac([inf(319.4, 11.8)], [])
assert _real_rotate(ac, 319.6)[0] and not sent          # gia' li'
ac, sent = fake_ac([inf(319.4, 11.8)], [])
ok, det = _real_rotate(ac, 139.6)
assert not ok and "fuori dal campo" in det and not sent, det
ac, sent = fake_ac([inf(319.4, 11.8), inf(300.0, 352.4)], [0])
ok, det = _real_rotate(ac, 309.6, max_s=0)
assert not ok and "non conclusa" in det, det
ac, sent = fake_ac([inf(319.4, 11.8), inf(305.0, 2.0)], [0])
ok, det = _real_rotate(ac, 309.6)
assert not ok and "arrivato a 305.0°" in det, det       # verifica meccanica
print("OK caa_rotate")

# --- flusso flat con il rotatore --------------------------------------------
KB.update(filters_gains=[("H", 100), ("L", 100)],
          angles={("L", 100): {30.0: 10, 210.0: 4, 40.0: 8},
                  ("H", 100): {40.0: 12, 41.0: 3}},
          caa=inf(319.4, 11.8), rot_ok=(True, "ruotato"),
          exps_seq=[{0: 2.89}, {5: 7.8}, {0: 2.91}])
A.AsiairControl.caa_info = lambda self: KB["caa"]
A.AsiairControl.caa_rotate = lambda self, mech, tol=2.0: (
    CALLS.append(f"rotate({mech})"), KB["rot_ok"])[1]
A.AsiairControl.read_flat_exps = lambda self, idxs: KB["exps_seq"].pop(0)


def fino_ai_flat(exps):
    state.clear()
    KB.update(exps_seq=list(exps), flats_status=(True, True, 3000, "12/30", ""))
    cyc("ripresa", now=datetime(2026, 7, 24, 12, 30, tzinfo=tz),
        in_night=True, snap=snap(started=True))
    state.update({"dew_samples": 40, "dew_margin_min": 6.0, "dew_wet_minutes": 0,
                  "dew_last": {"margin_c": 6.0, "temp_c": 21.0, "dewpoint_c": 8.0,
                               "ts": datetime(2026, 7, 24, 12, 35, tzinfo=tz).isoformat()}})
    cyc("alba", now=datetime(2026, 7, 24, 12, 40, tzinfo=tz), in_night=False)
    assert state.get("flat_stage") == "drying"
    cyc("primo gruppo", now=datetime(2026, 7, 24, 12, 46, tzinfo=tz))


FINE = (True, False, 0, None, "")
fino_ai_flat([{0: 2.89}, {5: 7.8}, {0: 2.91}])
assert state.get("flat_groups") == [[100, 50, [0], "L", 337.6, "30"],
                                    [100, 75, [5], "H", 347.6, "40"],
                                    [100, 50, [0], "L", 347.6, "40"]], state.get("flat_groups")
assert CALLS[:2] == ["rotate(337.6)", "gain(100)"], CALLS   # rotazione PRIMA
assert msg_has("💡 Pannello al 50% per L (gain 100) · angolo 30°: autorun flat"), MSGS
cyc("H a 40°", flats_status=FINE)
assert CALLS[0] == "rotate(347.6)" and "brightness(75)" in CALLS, CALLS
assert msg_has("per H (gain 100) · angolo 40°"), MSGS
cyc("L a 40°", flats_status=FINE)
assert CALLS[0] == "rotate(347.6)" and "brightness(50)" in CALLS, CALLS
assert msg_has("per L (gain 100) · angolo 40°"), MSGS
cyc("dark 1", flats_status=FINE)
assert state["flat_times"] == {"0:100:337.6": 2.89, "5:100:347.6": 7.8,
                               "0:100:347.6": 2.91}, state["flat_times"]
assert not any(c.startswith("rotate") for c in CALLS), CALLS   # dark: niente rotatore
assert "slots(dark,[0, 5],exp={0: 2.89, 5: 7.8})" in CALLS, CALLS
assert state["dark_groups"][1:] == [[100, {"0": 2.91}, "L"]], state["dark_groups"]
cyc("dark 2", flats_status=FINE)
assert "slots(dark,[0],exp={0: 2.91})" in CALLS, CALLS
assert msg_has("Dark flat L (gain 100) avviati"), MSGS
print("OK flusso: flat L30, H40, L40 + due dark")

# tempi uguali al centesimo -> un solo dark
fino_ai_flat([{0: 2.89}, {5: 7.8}, {0: 2.894}])
for _ in range(3):
    cyc("avanti", flats_status=FINE)
assert state["flat_stage"] == "darks" and state["dark_groups"] == [
    [100, {"0": 2.89, "5": 7.8}, "H+L"]], state["dark_groups"]

# senza angoli nei nomi (o rotatore spento) -> come prima, nessuna rotazione
KB["angles"] = {}
fino_ai_flat([{5: 7.8, 0: 2.89}])
assert state.get("flat_groups")[0] == [100, 75, [5], "H"], state.get("flat_groups")
assert not any(c.startswith("rotate") for c in CALLS), CALLS
KB["angles"] = {("L", 100): {30.0: 10}, ("H", 100): {40.0: 12}}

# rotatore illeggibile -> STOP a rig acceso, nessun flat
KB["caa"] = None
fino_ai_flat([])
assert state["flat_stage"] == "error" and "start_flats" not in CALLS, CALLS
assert msg_has("angoli del rotatore non determinabili"), MSGS
KB["caa"] = inf(319.4, 11.8)

# rotazione fallita -> STOP prima di toccare gain/pannello
KB["rot_ok"] = (False, "move_caa [20.0] code 268 (None)")
fino_ai_flat([])
assert state["flat_stage"] == "error" and CALLS == ["rotate(337.6)"], CALLS
assert msg_has("rotatore non portato all'angolo 30° per i flat L"), MSGS
print("OK errori: stop a rig acceso")

# --- sessioni giornaliere: flat del SUO angolo ------------------------------
import shutil                                                     # noqa: E402
import sfro_sessioni as S                                         # noqa: E402
TMP = A.Path(tempfile.mkdtemp(prefix="sess_rot_"))
SRC, DEST = TMP / "asiair_sfro" / "ASIAIR", TMP / "sessioni"
CFG = {"sync_module": {"nas_dest": str(TMP), "mount_point": "/x/asiair_sfro"},
       "sessioni_giornaliere": {"enabled": True, "dest": str(DEST),
                                "mode": "hardlink", "folder_timezone": "Europe/Rome"}}


def mk(rel, name):
    p = SRC / rel / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"fit")


B = "_Bin1_2600MM_{f}_gain100_20261024-{t}_{a}deg_0.0C_0001.fit"
mk("Plan/Light/S1/20261024", "Light_S1_300.0s" + B.format(f="L", t="030000", a=30))
mk("Plan/Light/S1/20261024", "Light_S1_300.0s" + B.format(f="L", t="070000", a=210))
mk("Plan/Light/S2/20261024", "Light_S2_300.0s" + B.format(f="L", t="040000", a=40))
mk("Plan/Light/S2/20261024", "Light_S2_300.0s" + B.format(f="H", t="050000", a=41))
for f, t, a in (("L", "120000", 30), ("L", "121000", 40), ("H", "122000", 40)):
    mk("Autorun/Flat/20261024", "Flat_2.9s" + B.format(f=f, t=t, a=a))
for f, t in (("L", "123000"), ("H", "124000")):
    mk("Autorun/Dark/20261024", "Dark_2.9s" + B.format(f=f, t=t, a=40))
r = S.aggiorna(CFG, nights=["20261024"])
ls = lambda rel: sorted(p.name.split("_gain")[0][-1] + p.name.split("deg_")[0].split("_")[-1]
                        for p in (DEST / rel).iterdir())
assert ls("S1/20261024/flat") == ["L30"], ls("S1/20261024/flat")
assert ls("S2/20261024/flat") == ["H40", "L40"], ls("S2/20261024/flat")
assert ls("S1/20261024/dark") == ["L40"], ls("S1/20261024/dark")   # dark: filtro+gain
assert ls("S2/20261024/dark") == ["H40", "L40"], ls("S2/20261024/dark")
assert S.same_angle(12, 192, 2) and S.same_angle(359, 1, 2) and not S.same_angle(30, 40, 2)
shutil.rmtree(TMP)
print("OK sessioni giornaliere per angolo")
print("OK test_flat_rotator")
