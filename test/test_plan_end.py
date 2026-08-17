"""Test OFFLINE della FINE PIANO (correzione 2026-08-15).

Fino al 2026-08-15 il piano fermo a tetto aperto faceva scattare SEMPRE
l'avviso «Il piano si e' FERMATO in modo non previsto (errore)», anche quando
aveva semplicemente esaurito le pose (segnalato dall'utente: messaggio di
errore alle 12:45 a piano concluso).

Qui si verifica `plan_left()`, la lettura su cui si basa la distinzione:
  - tutti i target abilitati con left_time_sec 0  -> left 0  = piano ESAURITO
  - residuo su almeno un target                   -> left >0 = piano INTERROTTO
  - target disabilitati                            -> non contano
  - get_plan illeggibile / senza piano / senza target abilitati -> ok False
"""
import sys
from pathlib import Path as _P
ROOT = _P(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import sfro_agent as A

cfg = A.load_config(ROOT / "config.example.yaml")
ac = A.AsiairControl(cfg["asiair"])


class FakeClient:
    """Risponde a get_plan con la risposta scriptata."""
    def __init__(self, risposta):
        self.risposta = risposta
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def call(self, method, params, max_wait=None):
        self.calls.append((method, params))
        return self.risposta, None


def with_plan(risposta):
    fc = FakeClient(risposta)
    ac._client = lambda: fc
    return fc


def target(nome, left, total=3600, enable=True):
    return {"target_name": nome, "enable": enable, "left_time_sec": left,
            "total_time_sec": total, "seqs": [{"lapsed": 10}]}


def plan(*targets):
    return {"code": 0, "result": [{"plan_name": "Notte", "targets": list(targets)}]}


# --- piano ESAURITO: nessun residuo su nessun target abilitato -------------
fc = with_plan(plan(target("M31", 0), target("NGC7000", 0)))
ok, left, det = ac.plan_left()
assert ok and left == 0, (ok, left, det)
assert det == "2/2 target completati", det
assert [m for m, _ in fc.calls] == ["get_plan"], fc.calls

# un solo target, esaurito
with_plan(plan(target("M31", 0)))
ok, left, det = ac.plan_left()
assert ok and left == 0 and det == "1/1 target completati", (ok, left, det)

# --- piano INTERROTTO: residuo da fare -------------------------------------
with_plan(plan(target("M31", 0), target("NGC7000", 1800)))
ok, left, det = ac.plan_left()
assert ok and left == 1800, (ok, left, det)
assert det == "1/2 target completati", det

# residuo su tutti: nessun target completato
with_plan(plan(target("M31", 600), target("NGC7000", 1200)))
ok, left, det = ac.plan_left()
assert ok and left == 1800 and det == "0/2 target completati", (ok, left, det)

# --- i target DISABILITATI non contano -------------------------------------
# un target spento con 2 ore di residuo non deve far sembrare interrotto un
# piano che ha finito tutto quello che doveva fare
with_plan(plan(target("M31", 0), target("Spento", 7200, enable=False)))
ok, left, det = ac.plan_left()
assert ok and left == 0 and det == "1/1 target completati", (ok, left, det)

# --- letture NON conclusive: ok False --------------------------------------
with_plan({"code": 0, "result": []})
ok, left, det = ac.plan_left()
assert not ok and left is None and "nessun piano" in det, (ok, left, det)

with_plan({"code": 103, "result": []})
ok, left, det = ac.plan_left()
assert not ok and "103" in det, (ok, left, det)

# piano presente ma con tutti i target disabilitati: non si conclude nulla
with_plan(plan(target("Spento", 0, enable=False)))
ok, left, det = ac.plan_left()
assert not ok and "nessun target abilitato" in det, (ok, left, det)


# eccezione di trasporto (ASIAIR che cade a meta' lettura)
class Boom:
    def __enter__(self):
        raise OSError("connessione persa")

    def __exit__(self, *a):
        pass


ac._client = lambda: Boom()
ok, left, det = ac.plan_left()
assert not ok and "connessione persa" in det, (ok, left, det)

# --- valori negativi/None trattati come zero -------------------------------
# un firmware che restituisce left_time_sec None (o negativo a fine posa) non
# deve far risultare il piano "da fare" ne' abbassare il totale
with_plan(plan(target("M31", None), target("NGC7000", -5)))
ok, left, det = ac.plan_left()
assert ok and left == 0 and det == "2/2 target completati", (ok, left, det)

print("OK test_plan_end")
