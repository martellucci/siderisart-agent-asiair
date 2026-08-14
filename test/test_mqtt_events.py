"""Test OFFLINE della telemetria da EVENTI PUSH 4700 (2026-08-12) e di
get_power_supply. Niente ASIAIR e niente broker: il listener viene puntato su
un server TCP finto in locale che spinge gli stessi eventi catturati dal vivo.
"""
import json
import socket
import sys
import threading
import time

from pathlib import Path as _P
ROOT = _P(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import sfro_agent as A
import sfro_mqtt as M

cfg = A.load_config(ROOT / "config.example.yaml")
pub = M.Publisher(cfg, dry=True)

# --- A) forma dei payload dagli eventi -------------------------------------
EVENTI = [
    {"Event": "Version", "firmware_ver_string": "13.41"},
    {"Event": "PiStatus", "temp": 41.3, "is_undervolt": False,
     "is_over_current": False, "is_overtemp": False},
    {"Event": "Exposure", "state": "downloading", "page": "plan"},
    {"Event": "SaveImage", "state": "start"},                   # senza filename
    {"Event": "SaveImage", "state": "complete", "filename": "Light_300s_0061.fit"},
    {"Event": "Sequence", "state": "frame_complete", "progress": {
        "cur_plan": {"total": 98, "lapse": 61},
        "cur_target": {"target_name": "Rotten Fish Nebula"},
        "cur_seq": {"frame_type": "light"}}},
]
for e in EVENTI:
    pub._apply_event(e)

pi = pub._pi_payload()
assert pi["temp_c"] == 41.3 and pi["undervolt"] is False, pi
assert pi["overcurrent"] is False and pi["overtemp"] is False, pi
assert pi["firmware"] == "13.41", pi
print("A) PiStatus -> payload pi:", json.dumps(pi, ensure_ascii=False))

# --- B) session: gli eventi sovrascrivono il poll, non lo cancellano -------
POLL = {"target": "vecchio", "frame_done": 0, "frame_total": 98, "progress_pct": 0,
        "seq_type": "light", "exp_s": 300, "gain": 100, "plan_started": True}
pub.last_session = dict(POLL)
live = pub._session_live()
assert live["frame_done"] == 61 and live["progress_pct"] == 62, live
assert live["target"] == "Rotten Fish Nebula", live
assert live["exp_state"] == "downloading" and live["last_file"].endswith("0061.fit"), live
assert live["gain"] == 100 and live["exp_s"] == 300, "campi del poll persi"
print("B) session live:", json.dumps(live, ensure_ascii=False))

# un Sequence senza contatori NON deve azzerare quelli buoni
pub._apply_event({"Event": "Sequence", "progress": {"cur_plan": {}}})
live2 = pub._session_live()
assert live2["frame_done"] == 61 and live2["frame_total"] == 98, live2
print("B2) progress vuoto -> contatori del poll preservati")

# eventi vecchi (box spento): niente pubblicazione, il topic resta del poll
pub.ev_ts = time.time() - pub.ev_fresh - 1
assert pub._pi_payload() is None, "pi pubblicato con eventi scaduti"
assert pub._session_live() is None, "session pubblicata con eventi scaduti"
print("C) eventi scaduti -> nessuna pubblicazione (niente valori vecchi)")

# senza poll alle spalle non si inventa nulla
pub.ev_ts, pub.last_session = time.time(), {}
assert pub._session_live() is None
print("D) nessun poll alle spalle -> nessuna session")

# --- E) power: nomi dall'ordine di pi_output_get2 --------------------------
RAILS = [[12.096, 0.70355], [12.201, 0.0302], [0.021, 0.0],
         [12.2325, 0.145647], [12.2535, 1.8786]]      # letti dal vivo sul box
OUTS = [{"type": "camera"}, {"type": "other"}, {"type": "flat_panel"},
        {"type": "dew_heater"}]
p = M.Publisher._power_payload(RAILS, OUTS)
assert p["dew_heater_a"] == 0.146 and p["camera_a"] == 0.704, p
assert p["flat_panel_a"] == 0.0 and p["flat_panel_v"] == 0.02, p   # uscita spenta
assert p["input_a"] == 1.879, p                    # la coppia in piu' = ingresso
assert p["input_v"] == 12.25 and 33 < p["total_w"] < 34, p
assert M.Publisher._power_payload([], [])["total_w"] == 0.0
assert M.Publisher._power_payload([["x", None], [1]], [])["total_w"] == 0.0
print("E) power:", json.dumps(p, ensure_ascii=False))

# --- F) il listener vero contro un ASIAIR finto ----------------------------
srv = socket.socket()
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", 0))
srv.listen(1)
port = srv.getsockname()[1]


ricevuti = []          # cosa il listener MANDA al box (deve essere solo il battito)


def fake_asiair():
    c, _ = srv.accept()
    c.sendall(b'{"Event":"Version","firmware_ver_string":"13.41"}\r\n')
    c.sendall(b'roba non-JSON che deve essere ignorata\r\n')
    # messaggio spezzato in due pacchetti: il buffer deve ricomporlo
    c.sendall(b'{"Event":"PiStatus","temp":39.5,"is_undervolt":tr')
    time.sleep(0.2)
    c.sendall(b'ue,"is_over_current":false,"is_overtemp":false}\r\n')
    c.sendall(b'{"jsonrpc":"2.0","method":"test_connection","id":7,"code":0}\r\n')
    # resta in ascolto per registrare i battiti in arrivo
    c.settimeout(0.5)
    t0 = time.time()
    while time.time() - t0 < 2.5:
        try:
            d = c.recv(4096)
        except socket.timeout:
            continue
        if not d:
            break
        for riga in d.decode("utf-8", "ignore").splitlines():
            if riga.strip():
                ricevuti.append(json.loads(riga))
    time.sleep(0.2)
    c.close()


pub2 = M.Publisher(cfg, dry=True)
pub2.host_asiair, pub2.imager_port = "127.0.0.1", port
pub2.hb_interval = 0.5          # battito accelerato per il test
threading.Thread(target=fake_asiair, daemon=True).start()
threading.Thread(target=pub2.event_listener, daemon=True).start()
t0 = time.time()
while time.time() - t0 < 5 and not pub2.ev.get("pi"):
    time.sleep(0.1)
time.sleep(1.5)                 # lascia passare qualche battito
pub2.stop()
srv.close()
pi2 = pub2._pi_payload()
assert pi2 and pi2["temp_c"] == 39.5, pi2
assert pi2["undervolt"] is True, "flag undervolt non letto"
assert pi2["firmware"] == "13.41", pi2
print("F) listener su ASIAIR finto (riga spezzata + spazzatura + risposta con id):",
      json.dumps(pi2, ensure_ascii=False))

# --- G) il battito: unica cosa che il listener invia, e non e' un comando ----
metodi = {m.get("method") for m in ricevuti}
assert ricevuti, "nessun battito inviato: heartbeat non funzionante"
assert metodi == {"test_connection"}, f"il listener ha inviato altro: {metodi}"
assert all(m.get("params") == [] for m in ricevuti), ricevuti
print(f"G) battito: {len(ricevuti)} test_connection inviati, nient'altro")

# --- H) HFR: si prende il minimo della curva, ma non da corse abortite -------
CURVA = [[14056, 6.135143], [14006, 2.944], [13996, 2.830725], [13966, 4.411]]
casi = [
    ("corsa buona", {"auto_focus": {"result": {"points": CURVA}}}, 2.83, 13996),
    ("corsa abortita", {"auto_focus": {"result": {"points": CURVA, "error": "aborted"}}}, None, None),
    ("nessuna curva", {"auto_focus": {"result": {}}}, None, None),
    ("campo assente", {}, None, None),
]
for nome, aps, atteso_hfr, atteso_pos in casi:
    r = M.Publisher._focus_quality(aps)
    assert r["hfr"] == atteso_hfr and r["hfr_pos"] == atteso_pos, (nome, r)
print("H) HFR dalla curva a V:", json.dumps(
    M.Publisher._focus_quality(casi[0][1]), ensure_ascii=False),
    "· abortita ->", M.Publisher._focus_quality(casi[1][1])["af_error"])

print("\nTUTTI I TEST MQTT-EVENTI: OK")
