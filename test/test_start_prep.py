"""Test OFFLINE del pre-avvio piano 2026-07-19:
- ensure_anti_dew: gia' acceso / spento->acceso / spento e riaccensione fallita
- start(): anti-dew + fascia anticondensa 100% chiamati PRIMA dell'avvio, esiti nel
  detail (non bloccanti anche se falliscono)
"""
import sys
from pathlib import Path as _P
ROOT = _P(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import sfro_agent as A

cfg = A.load_config(ROOT / "config.example.yaml")
ac_cfg = dict(cfg["asiair"])
assert ac_cfg.get("start_dew_heater_pct") == 100, "config start_dew_heater_pct"
ac_cfg["flat_panel"] = {"enabled": False}          # niente OF2 nel test
ac = A.AsiairControl(ac_cfg)
assert ac.start_dew_heater_pct == 100


class FakeClient:
    """Risposte scriptate per metodo; get_control_value consuma una COda."""
    def __init__(self, script):
        self.script = script          # {method: risposta | lista di risposte}
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def call(self, method, params, max_wait=None):
        self.calls.append((method, params))
        r = self.script.get(method, {"code": 0})
        if isinstance(r, list):
            r = r.pop(0) if r else {"code": 0}
        return r, None


def with_client(script):
    fc = FakeClient(script)
    ac._client = lambda: fc
    return fc


# --- ensure_anti_dew -------------------------------------------------------
fc = with_client({"get_control_value":
                  [{"code": 0, "result": {"name": "AntiDewHeater", "value": 1}}]})
ok, det = ac.ensure_anti_dew()
assert ok and det == "gia' acceso", (ok, det)
assert [m for m, _ in fc.calls] == ["get_control_value"], fc.calls

fc = with_client({"get_control_value":
                  [{"code": 0, "result": {"value": 0}},
                   {"code": 0, "result": {"value": 1}}],
                  "set_control_value": {"code": 0}})
ok, det = ac.ensure_anti_dew()
assert ok and det == "era SPENTO: acceso ora", (ok, det)
assert ("set_control_value", ["AntiDewHeater", 1]) in fc.calls, fc.calls

fc = with_client({"get_control_value":
                  [{"code": 0, "result": {"value": 0}},
                   {"code": 0, "result": {"value": 0}}],
                  "set_control_value": {"code": 0}})
ok, det = ac.ensure_anti_dew()
assert not ok and "riaccensione non risulta" in det, (ok, det)
print("ensure_anti_dew: OK")

# --- start(): pre-avvio nel detail ----------------------------------------
A.time.sleep = lambda s: None
snap_ok = {c: True for c in ("cam_open", "focuser_connected", "wheel_connected",
                             "mount_connected", "guide_connected")}
A.AsiairControl.missing_devices = lambda self, snap: []
A.AsiairControl.check_position = lambda self: (True, 31.5, -99.4, "")

PREP = []
A.AsiairControl.ensure_anti_dew = (
    lambda self: (PREP.append("antidew"), KB["antidew"])[1])
A.AsiairControl.set_output = (
    lambda self, t, v: (PREP.append(f"set_output({t},{v})"), KB["heater"])[1])
# COOLER nel pre-avvio dal 2026-08-16 (specifica utente): il piano non deve mai
# partire con la camera calda, nemmeno se l'inizializzazione al T-10 e' saltata.
A.AsiairControl.cooler_on = (
    lambda self: (PREP.append("cooler_on"), KB["cooler"])[1])
A.AsiairControl.camera_cooling = lambda self: (True, 21.4, 0.0, True, "")

def start_script():
    """Script di un avvio SANO. get_enabled_plan e' una coda: la prima risposta
    e' la lettura PRIMA del comando (dal 2026-08-27 start() la fa per capire se
    il flag era gia' sporco) e vale False; la seconda e' la verifica dopo
    start_exposure e vale True. Una funzione e non una costante: la coda si
    consuma, e ogni scenario ne vuole una nuova."""
    return {"set_page": {"code": 0},
            "start_exposure": {"code": 0},
            "get_enabled_plan": [
                {"code": 0, "result": [{"is_plan_started": False}]},
                {"code": 0, "result": [{"is_plan_started": True}]}]}

# 1) tutto ok: detail = esiti pre-avvio
KB = {"antidew": (True, "gia' acceso"), "heater": (True, ""), "cooler": (True, "")}
PREP.clear()
fc = with_client(start_script())
ok, det = ac.start(snap_ok)
assert ok, det
assert det == ("anti-dew camera ON (gia' acceso) · fascia anticondensa al 100% · "
               "cooler ON (21.4°C → target 0.0°C)"), det
assert PREP == ["antidew", "set_output(dew_heater,100)", "cooler_on"], PREP
i_start = [m for m, _ in fc.calls].index("start_exposure")
assert ("start_exposure", ["light"]) in fc.calls, fc.calls
print("start ok:", det)

# 2) pre-avvio in errore: il piano parte COMUNQUE, detail con i warning
KB = {"antidew": (False, "timeout"), "heater": (False, "boh"),
      "cooler": (False, "code 107")}
PREP.clear()
fc = with_client(start_script())
ok, det = ac.start(snap_ok)
assert ok, det
assert "⚠️ anti-dew camera NON verificato (timeout)" in det, det
assert "accendilo dall'app" in det, det
assert "⚠️ fascia anticondensa NON impostata (boh)" in det, det
assert "⚠️ cooler NON acceso (code 107)" in det, det
assert ("start_exposure", ["light"]) in fc.calls, "il piano deve partire comunque"
print("start con warning:", det)

# 3) FLAG GIA' ALTO e camera FERMA (notte 26→27): start_exposure torna code 0 e
#    is_plan_started era gia' true — non prova niente. start() deve FALLIRE, non
#    dichiarare avviato un piano fantasma.
KB = {"antidew": (True, ""), "heater": (True, ""), "cooler": (True, "")}
fc = with_client({"set_page": {"code": 0}, "start_exposure": {"code": 0},
                  "get_enabled_plan": {"code": 0,
                                       "result": [{"is_plan_started": True}]},
                  "get_app_state": {"code": 0,
                                    "result": {"capture": {"is_working": False,
                                                           "state": "idle"}}}})
ok, det = ac.start(snap_ok)
assert not ok, "flag sporco + camera ferma: NON e' avviato"
assert "flag di avvio gia' alto e camera ferma" in det, det
print("start con flag sporco:", det)

# 4) flag gia' alto ma la camera LAVORA davvero (o e' in FirstDelay): avviato
for cap in ({"is_working": True, "state": "expose"},
            {"is_working": False, "state": "first_delay"}):
    fc = with_client({"set_page": {"code": 0}, "start_exposure": {"code": 0},
                      "get_enabled_plan": {"code": 0,
                                           "result": [{"is_plan_started": True}]},
                      "get_app_state": {"code": 0, "result": {"capture": cap}}})
    ok, det = ac.start(snap_ok)
    assert ok, (cap, det)
print("start con flag alto ma camera al lavoro/FirstDelay: OK")

print("\nTUTTI I TEST START-PREP: OK")
