"""Test OFFLINE dell'incidente 2026-11-25: flat dati per finiti nella pausa tra
due pose e dark flat respinti con 206 "capture is active".

Alle 12:54:01 il ciclo ha letto is_working falso nella pausa di ~1 s tra le
pose 28 e 29 di 30 flat H (~17 s residui, sotto la soglia di 60): flat
"completati", pannello giu', reset per i dark -> code 206 -> flusso fermo. Il
flat 30 porta l'ora 12:54:13. Qui si verifica che:
  A) un "fermo" seguito da cattura ripresa sia letto come IN CORSO;
  B) un fermo confermato resti un fermo;
  C) il reset ritenti il 206 e poi proceda;
  D) un 206 che non passa esca dopo l'attesa massima, col testo dell'ASIAIR;
  E) un altro codice esca subito, senza ritentare;
  F) configure_autorun_slots e start_flats usino il reset con attesa.
Nessun ASIAIR: client finto, orologio finto.
"""
import sys
from pathlib import Path as _P
ROOT = _P(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import sfro_agent as A

cfg = A.load_config(ROOT / "config.example.yaml")
ac = A.AsiairControl(dict(cfg["asiair"]))
assert ac.idle_confirm == 6 and ac.busy_wait == 120 and ac.busy_retry == 5

CLOCK = [1000.0]
SLEEPS = []


def fake_sleep(s):
    SLEEPS.append(s)
    CLOCK[0] += s


A.time.sleep = fake_sleep
A.time.monotonic = lambda: CLOCK[0]


def state(working, left, done, total=30):
    return {"code": 0, "result": {"capture": {
        "is_working": working, "exposure_mode": "autosave",
        "frame_summary": {"complete_num": done, "total": total}}}}


class FakeClient:
    """Risposte scriptate per metodo (lista consumata in ordine; l'ultima
    voce resta)."""
    def __init__(self, script):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def call(self, method, params=None, max_wait=None):
        self.calls.append(method)
        lst = self.script.get(method)
        if not lst:
            return {"code": 0, "result": None}, None
        v = lst.pop(0) if len(lst) > 1 else lst[0]
        return v, None


def con(script):
    fc = FakeClient(script)
    ac._client = lambda: fc
    return fc


fails = []


def check(name, cond, info=""):
    print(("OK   " if cond else "FAIL ") + name + (f"  [{info}]" if info else ""))
    if not cond:
        fails.append(name)


# --- A) pausa tra due pose: fermo, poi riletto in cattura -> in corso --------
SLEEPS.clear()
fc = con({"get_app_state": [state(False, 17, 28), state(True, 9, 29)],
          "get_target_sequences": [{"code": 0, "result": {"left_time_sec": 17}},
                                   {"code": 0, "result": {"left_time_sec": 9}}]})
ok, working, left, prog, det = ac.flats_status()
check("A pausa tra pose = in corso", ok and working and prog == "29/30",
      f"{working} {left} {prog}")
check("A riletto dopo 6 s", SLEEPS == [6], SLEEPS)

# --- B) fine vera: fermo confermato -----------------------------------------
SLEEPS.clear()
fc = con({"get_app_state": [state(False, 0, 30)],
          "get_target_sequences": [{"code": 0, "result": {"left_time_sec": 0}}]})
ok, working, left, prog, det = ac.flats_status()
check("B fine confermata", ok and not working and left == 0 and prog == "30/30",
      f"{working} {left} {prog}")
check("B una sola riletta", SLEEPS == [6] and fc.calls.count("get_app_state") == 2,
      f"{SLEEPS} {fc.calls}")

# --- in cattura: nessuna attesa ---------------------------------------------
SLEEPS.clear()
con({"get_app_state": [state(True, 200, 10)],
     "get_target_sequences": [{"code": 0, "result": {"left_time_sec": 200}}]})
ok, working, *_ = ac.flats_status()
check("in cattura: nessuna riletta", working and SLEEPS == [], SLEEPS)

# --- C) reset: 206, 206, poi 0 ----------------------------------------------
SLEEPS.clear()
seq = [(206, "capture is active"), (206, "capture is active"), (0, None)]
ok, det = ac._reset_progress(lambda: seq.pop(0))
check("C 206 ritentato e poi OK", ok and SLEEPS == [5, 5], f"{ok} {det} {SLEEPS}")

# --- D) 206 che non passa: esce dopo ~120 s col testo -----------------------
SLEEPS.clear()
ok, det = ac._reset_progress(lambda: (206, "capture is active"))
check("D 206 persistente -> errore", not ok and sum(SLEEPS) >= 120
      and sum(SLEEPS) <= 125, f"attesa {sum(SLEEPS)}")
check("D messaggio col testo ASIAIR", "capture is active" in det
      and "dopo 120s" in det, det)

# --- E) altro codice: subito, senza attese ----------------------------------
SLEEPS.clear()
ok, det = ac._reset_progress(lambda: (224, "cannot edit"))
check("E code 224 subito", not ok and SLEEPS == [] and "224: cannot edit" in det, det)
ok, det = ac._reset_progress(lambda: (None, "timeout"))
check("E errore di trasporto subito", not ok and SLEEPS == [], det)

# --- F1) configure_autorun_slots: reset 206 poi OK, slot dark configurati ----
SLEEPS.clear()
SLOT = {"filter": 5, "suffix": "", "repeat": 30, "id": 7, "enable": False,
        "autoexp": False, "gain": -10000, "exp": 8.7, "bin": 1, "type": "dark",
        "capture_index": 0}
SEQ_EN = {"slots": [dict(SLOT, enable=True)], "left_time_sec": 261}
calls = []
reset_seq = [(206, "capture is active"), (0, None)]


def fake_call1(port, method, params=None, timeout=None):
    calls.append(method)
    if method == "get_target_sequences":
        n = calls.count("get_target_sequences")
        return 0, ({"slots": [SLOT], "left_time_sec": 0} if n == 1 else SEQ_EN)
    if method == "reset_sequence_progress":
        return reset_seq.pop(0)
    return 0, None


ac._call1 = fake_call1
ok, nfr, det = ac.configure_autorun_slots("dark", {5}, 30, {5: 8.7})
check("F1 slot dark configurati dopo il 206", ok and nfr == 30 and SLEEPS == [5],
      f"{ok} {nfr} {det} {SLEEPS}")
check("F1 reset prima degli edit",
      calls.index("reset_sequence_progress") < calls.index("set_sequence"), calls)

# --- F2) start_flats: reset 206 poi OK, autorun partito ----------------------
SLEEPS.clear()
fc = con({"set_page": [{"code": 0}],
          "reset_sequence_progress": [{"code": 206, "error": "capture is active"},
                                      {"code": 0}],
          "get_target_sequences": [{"code": 0, "result": {"left_time_sec": 261}},
                                   {"code": 0, "result": {"left_time_sec": 250}}],
          "start_exposure": [{"code": 0}],
          "get_app_state": [state(True, 250, 1)]})
ok, det = ac.start_flats()
check("F2 start_flats dopo il 206", ok and SLEEPS[:1] == [5], f"{ok} {det} {SLEEPS}")

# --- F3) start_flats: 206 che non passa -> non avvia niente ------------------
SLEEPS.clear()
fc = con({"set_page": [{"code": 0}],
          "reset_sequence_progress": [{"code": 206, "error": "capture is active"}]})
ok, det = ac.start_flats()
check("F3 206 persistente: niente start", not ok and "start_exposure" not in fc.calls
      and "capture is active" in det, det)

print()
if fails:
    print(f"FALLITI {len(fails)}: {fails}")
    sys.exit(1)
print("TUTTI OK")
