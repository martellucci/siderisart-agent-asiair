"""Test OFFLINE delle letture che possono MENTIRE (2026-08-12): ritentativo su
pi_output_get2 (pannello flat E fascia anticondensa) e valori mancanti che
prima diventavano zeri plausibili.

Il 12/8 UNA sola risposta di pi_output_get2 priva dell'uscita 'flat_panel' ha
mandato in errore il teardown all'ULTIMO passo (piano gia' fermo, mount gia' in
home), annullando la sessione di flat e lasciando pannello aperto e rig acceso.
Qui si verifica che una risposta storta venga ritentata e che il messaggio dica
cosa e' successo davvero. Nessun ASIAIR: client finto, attese azzerate.
"""
import sys
from pathlib import Path as _P
ROOT = _P(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import sfro_agent as A

cfg = A.load_config(ROOT / "config.example.yaml")
ac_cfg = dict(cfg["asiair"])
ac = A.AsiairControl(ac_cfg)
assert ac.out_read_tries == 3, ac.out_read_tries
A.time.sleep = lambda s: None                  # niente attese vere nel test

PANNELLO = {"type": "flat_panel", "state": False, "value": 5.0, "is_pwm": True}
BUONA = [{"type": "camera", "state": True, "value": 100.0, "is_pwm": False},
         {"type": "other", "state": True, "value": 100.0, "is_pwm": False},
         PANNELLO,
         {"type": "dew_heater", "state": True, "value": 100.0, "is_pwm": True}]
MONCA = [o for o in BUONA if o["type"] != "flat_panel"]      # il guasto del 12/8


class FakeClient:
    """Risposte scriptate a pi_output_get2; registra le chiamate."""
    def __init__(self, letture):
        self.letture = list(letture)     # ogni voce: lista uscite, dict, o Exception
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def call(self, method, params=None, max_wait=None):
        self.calls.append(method)
        if method != "pi_output_get2":
            return {"code": 0, "result": None}, None
        v = self.letture.pop(0) if self.letture else BUONA
        if isinstance(v, Exception):
            raise v
        if isinstance(v, dict):
            return v, None                       # risposta d'errore grezza
        return {"code": 0, "result": v}, None


def con(letture):
    fc = FakeClient(letture)
    ac._client = lambda: fc
    return fc

# --- A) una risposta monca e poi buona: si recupera, il pannello si chiude ---
fc = con([MONCA, BUONA])
ok, det = ac.close_flat()
assert ok and det == "", (ok, det)
assert fc.calls.count("pi_output_get2") == 2, fc.calls
assert "pi_output_set2" in fc.calls, "non ha scritto la chiusura"
print("A) risposta monca -> ritenta e chiude:", fc.calls)

# --- B) due storte (monca + errore) e poi buona: sempre recuperato ----------
fc = con([MONCA, {"code": 500, "error": "internal", "result": None}, BUONA])
ok, det = ac.close_flat()
assert ok and det == "", (ok, det)
assert fc.calls.count("pi_output_get2") == 3, fc.calls
print("B) monca + errore -> recuperato al terzo tentativo")

# --- C) sempre monca: fallisce, ma DICE cosa ha visto -----------------------
fc = con([MONCA, MONCA, MONCA])
ok, det = ac.close_flat()
assert not ok, (ok, det)
assert "manca 'flat_panel'" in det and "3 uscite" in det, det
assert "camera" in det and "dew_heater" in det, det      # elenca cosa c'era
assert "pi_output_set2" not in fc.calls, "non deve scrivere se non ha letto"
print("C) sempre monca -> errore parlante:", det)

# --- D) errore secco del box: messaggio diverso da 'uscita assente' ---------
err = {"code": 500, "error": "internal error", "result": None}
fc = con([err, err, err])
ok, det = ac.close_flat()
assert not ok and "code 500" in det and "nessuna lista" in det, det
print("D) errore del box -> distinto dall'uscita assente:", det)

# --- E) eccezione (timeout/connessione): ritentata e riportata --------------
fc = con([TimeoutError("nessuna risposta a 'pi_output_get2'"), BUONA])
ok, det = ac.close_flat()
assert ok, (ok, det)
print("E) timeout al primo colpo -> ritentato e chiuso")

fc = con([TimeoutError("boom")] * 3)
ok, det = ac.close_flat()
assert not ok and "non ha risposto" in det, det
print("F) timeout su tutti i tentativi -> errore parlante:", det)

# --- G) pannello gia' chiuso: nessuna scrittura -----------------------------
chiuso = [dict(o, state=True) if o["type"] == "flat_panel" else o for o in BUONA]
fc = con([chiuso])
ok, det = ac.close_flat()
assert ok and "pi_output_set2" not in fc.calls, (ok, det, fc.calls)
print("G) gia' chiuso -> nessuna scrittura")

# --- H) snapshot: un solo tentativo, non rallenta il ciclo ------------------
fc = FakeClient([MONCA, BUONA])
idx, o = ac._flat_state(fc, tries=1)
assert (idx, o) == (None, None) and fc.calls.count("pi_output_get2") == 1, fc.calls
print("H) snapshot (tries=1) -> una sola lettura, nessuna attesa")

# --- I) open_flat e set_flat_brightness ereditano il ritentativo ------------
fc = con([MONCA, chiuso])          # parte da CHIUSO, cosi' apre davvero
ok, changed, det = ac.open_flat(fc)
assert ok and changed and det == "", (ok, changed, det)
assert fc.calls.count("pi_output_get2") == 2 and "pi_output_set2" in fc.calls, fc.calls
print("I) open_flat -> ritenta anche lui e apre")

acceso = [dict(o, state=True, value=50.0) if o["type"] == "flat_panel" else o
          for o in BUONA]                       # com'e' dopo la scrittura
fc = con([MONCA, BUONA, acceso])                # monca, buona, poi la rilettura
ok, det = ac.set_flat_brightness(50)
assert ok, (ok, det)
assert fc.calls.count("pi_output_get2") == 3, fc.calls   # 2 letture + rilettura
print("J) set_flat_brightness -> ritenta e la rilettura di verifica torna")

# --- K) set_output (fascia anticondensa): stesso difetto, stesso rimedio -----
MONCA_DEW = [o for o in BUONA if o["type"] != "dew_heater"]   # manca la fascia
CALDA = [dict(o, value=100.0) if o["type"] == "dew_heater" else o for o in BUONA]
fc = con([MONCA_DEW, BUONA, CALDA])   # monca, lettura buona, rilettura post-scrittura
ok, det = ac.set_output("dew_heater", 100)
assert ok, (ok, det)
assert fc.calls.count("pi_output_get2") == 3, fc.calls
assert "pi_output_set2" in fc.calls, fc.calls
print("K) set_output -> ritenta la lettura e scrive la fascia anticondensa")

fc = con([MONCA_DEW, MONCA_DEW, MONCA_DEW])
ok, det = ac.set_output("dew_heater", 100)
assert not ok and "'dew_heater' non letto" in det and "manca 'dew_heater'" in det, det
assert "pi_output_set2" not in fc.calls, "non deve scrivere senza aver letto"
print("L) set_output senza lettura -> non scrive e lo dice:", det)

# rilettura di verifica che non arriva: va detto che la scrittura E' partita
fc = con([BUONA, MONCA_DEW, MONCA_DEW, MONCA_DEW])
ok, det = ac.set_output("dew_heater", 100)
assert not ok and "scritto, ma rilettura" in det, det
print("M) rilettura fallita -> distinta dalla mancata scrittura:", det)

# --- A2: letture che restituirebbero un valore PLAUSIBILE ma falso -----------
class TempClient(FakeClient):
    """get_control_value scriptato: ogni voce e' la risposta grezza."""
    def __init__(self, risposte):
        super().__init__([])
        self.risposte = list(risposte)

    def call(self, method, params=None, max_wait=None):
        self.calls.append(f"{method}{params}")
        return (self.risposte.pop(0) if self.risposte else {"code": 0, "result": {}}), None


VAL = lambda v: {"code": 0, "result": {"name": "x", "value": v}}
ac._client = lambda: TempClient([VAL(-100), VAL(-10), VAL(1)])
ok, temp, target, cooler, det = ac.camera_cooling()
assert ok and temp == -10.0 and target == -10.0 and cooler is True, (ok, temp, target, cooler, det)
print("N) camera_cooling normale: temp", temp, "target", target, "cooler", cooler)

# 'value' assente con code 0: prima diventava 0.0 °C — una temperatura CREDIBILE
# che poteva far passare (o fallire) il gate termico dei flat senza avvisare
ac._client = lambda: TempClient([{"code": 0, "result": {"name": "Temperature"}}])
ok, temp, target, cooler, det = ac.camera_cooling()
assert not ok and temp is None and "senza 'value'" in det, (ok, temp, det)
print("O) Temperature senza valore -> lettura dichiarata fallita:", det)

ac._client = lambda: TempClient([VAL(-100), {"code": 0, "result": {}}])
ok, temp, target, cooler, det = ac.camera_cooling()
assert not ok and "TargetTemp senza 'value'" in det, (ok, det)
print("P) TargetTemp senza valore -> idem:", det)

print("\nTUTTI I TEST FLAT-READ-RETRY: OK")
