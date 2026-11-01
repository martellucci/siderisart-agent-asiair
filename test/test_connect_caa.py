"""Test OFFLINE: connect_all apre il rotatore CAA (incidente 2026-10-25).

La notte 24-25/10 l'avvio T-10 ha connesso camera, EAF, EFW, mount e guida ma
non il CAA, rimasto chiuso. Qui un ASIAIR finto risponde a tutti i metodi di
connect_all; il CAA parte chiuso e si apre solo con `open_caa [id]`.
"""
import sys

from pathlib import Path as _P
ROOT = _P(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import sfro_agent as A

A.time.sleep = lambda s: None


def fake(caa_list=({"name": "CAA", "id": 0},), caa_opens=True, required=None):
    ac = A.AsiairControl({"host": "127.0.0.1", "port": 4700, "guider_port": 4400,
                          "required_devices": required or
                          ["camera", "focuser", "wheel", "mount", "guide", "caa"]})
    ac.pi_clock_sync = lambda: (True, "orologio Pi ok")
    st = {"caa": "close", "mount": False, "guide": False}
    sent = []

    def call1(port, m, p=None, timeout=None):
        sent.append((m, p))
        if m == "get_connected_cameras":
            return 0, [{"name": "ZWO ASI2600MM" if port == 4700 else "ASI120MM", "id": 0}]
        if m in ("get_focuser_info", "get_wheel_state"):
            return 0, {"state": "idle"}
        if m == "get_caa_info":
            return 0, {"connected": list(caa_list), "state": {"state": st["caa"]}}
        if m == "get_caa_state":
            return 0, {"state": st["caa"]}
        if m == "open_caa":
            if caa_opens:
                st["caa"] = "idle"
            return 0, None
        if m == "get_connected":
            return 0, {"mount": st["mount"], "camera": st["guide"]}
        if m == "set_connected":
            st["mount" if "mount" in p[0] else "guide"] = True
            return 0, None
        if m == "scope_get_time":
            return 0, [A.datetime.now(A.ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S"), "1"]
        if m == "scope_get_info":
            return 0, {"Lat": 31.5, "Lon": -99.4}
        return 0, {}
    ac._call1 = call1
    return ac, sent, st


# A) CAA chiuso -> open_caa [0] e connessione completa
ac, sent, st = fake()
ok, det = ac.connect_all()
assert ok, det
assert ("open_caa", [0]) in sent, sent
assert st["caa"] == "idle"
assert det.startswith("6 device"), det
# ordine: dopo la ruota, prima del mount
nomi = [m for m, _ in sent]
assert nomi.index("open_caa") > nomi.index("get_wheel_state"), nomi
assert nomi.index("open_caa") < nomi.index("set_connected"), nomi
print("OK A) CAA chiuso aperto da connect_all")

# B) id preso dall'elenco dell'ASIAIR
ac, sent, _ = fake(caa_list=({"name": "CAA", "id": 3},))
assert ac.connect_all()[0] and ("open_caa", [3]) in sent, sent
print("OK B) id dall'elenco")

# C) elenco vuoto (da chiuso puo' esserlo): si prova lo 0
ac, sent, _ = fake(caa_list=())
assert ac.connect_all()[0] and ("open_caa", [0]) in sent, sent
print("OK C) elenco vuoto -> id 0")

# D) il CAA non si apre: errore esplicito, mount e guida non toccati
ac, sent, _ = fake(caa_opens=False)
A_time = A.time.time
t = [0.0]
A.time.time = lambda: t.__setitem__(0, t[0] + 1.0) or t[0]   # orologio veloce
try:
    ok, det = ac.connect_all()
finally:
    A.time.time = A_time
assert not ok and "CAA" in det, det
assert ("set_connected", [{"mount": True, "async": True}]) not in sent, sent
print("OK D) CAA che non si apre -> errore")

# E) CAA non richiesto in config: nessuna chiamata al rotatore
ac, sent, _ = fake(required=["camera", "focuser", "wheel", "mount", "guide"])
ok, det = ac.connect_all()
assert ok and not any(m.startswith(("open_caa", "get_caa")) for m, _ in sent), sent
assert det.startswith("5 device"), det
print("OK E) senza 'caa' in required_devices nulla cambia")

# F) caa_open / caa_info leggono lo STATO, non l'elenco 'connected'
ac, _, st = fake()
assert ac.caa_open() is False
st["caa"] = "moving"
assert ac.caa_open() is True
ac._call1 = lambda port, m, p=None, timeout=None: (0, {
    "connected": [{"name": "CAA", "id": 0}], "state": {"state": "close"},
    "solve_angle": {"current_angle": 11.8}, "home_solve_angle": 52.4,
    "angle_status": {"mechanical_angle": 319.4, "reverse": True,
                     "expected_range": "(180,360]"}})
inf = ac.caa_info()
assert inf is not None and inf["connected"] is False, inf
print("OK F) aperto = state idle/moving")

# G) missing_devices segnala il CAA chiuso
ac, _, _ = fake()
snap = {"cam_open": True, "focuser_connected": True, "wheel_connected": True,
        "mount_connected": True, "guide_connected": True, "caa_connected": False}
assert ac.missing_devices(snap) == ["CAA"], ac.missing_devices(snap)
print("OK G) missing_devices -> CAA")

print("TUTTI I TEST CONNECT-CAA: OK")
