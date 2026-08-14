"""Test OFFLINE della verifica degli slot dark: tempo di posa arrotondato a 1
decimale e confronto TOLLERANTE (guasto 2026-07-27: chiesto 8.19, riletto
8.190001 dall'ASIAIR -> 'verifica fallita' e flusso fermo prima dei dark, con i
flat gia' fatti). Qui si stubba solo il trasporto (_call1): configure_autorun_slots
gira per davvero su un finto set di slot che RIPRODUCE l'artefatto float."""
import sys
from pathlib import Path as _P
ROOT = _P(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import sfro_agent as A

A.setup_logging("WARNING")

# artefatto osservato: il firmware rilegge 8.19 come 8.190001, 1.93 come 1.93
# (la deriva diventa visibile solo per certi valori, e' rappresentazione float)
ARTEFATTO = {8.19: 8.190001, 8.2: 8.200001, 2.5: 2.5}


def build(slots):
    """AsiairControl finto: get_target_sequences/set_sequence su `slots`."""
    ac = A.AsiairControl.__new__(A.AsiairControl)
    ac.port = 4700
    calls = []

    def _call1(port, method, params=None):
        if method == "get_target_sequences":
            return 0, {"slots": [dict(s) for s in slots],
                       "left_time_sec": 600}
        if method == "reset_sequence_progress":
            return 0, None
        if method == "set_sequence":
            p = params[0]
            calls.append((p["id"], p["enable"], p.get("exp")))
            for s in slots:
                if s["id"] == p["id"]:
                    s.update(p)
                    if p.get("exp") is not None:   # scrittura -> deriva float
                        s["exp"] = ARTEFATTO.get(p["exp"], p["exp"])
            return 0, None
        raise AssertionError(method)

    ac._call1 = _call1
    return ac, calls


def dk(sid, filt, exp):
    return {"id": sid, "type": "dark", "filter": filt, "exp": exp,
            "enable": False, "repeat": 30, "suffix": "", "autoexp": False,
            "gain": -10000, "bin": 1, "capture_index": 0}


def slots():
    # come sul rig: i due dark flat (con il tempo scritto la notte scorsa), un
    # dark 'library' da 300 s dell'utente e lo slot flat da cui esce il tempo AUTO
    return [dk(8, 5, 8.19), dk(9, 0, 1.0), dk(10, 5, 300.0),
            {"id": 3, "type": "flat", "filter": 5, "exp": 8.19, "enable": True,
             "repeat": 30, "suffix": "", "autoexp": True, "gain": -10000,
             "bin": 1, "capture_index": 0}]


# T1: il caso REALE del 27/7 — 8.19 riletto 8.190001. Prima falliva.
ac, calls = build(slots())
ok, n, det = ac.configure_autorun_slots("dark", {5, 0}, 30.0, {5: 8.19, 0: 1.93})
assert ok, f"T1 il guasto del 27/7 si ripete: {det}"
assert n == 60, (n, det)
print("T1 exp 8.19 -> riletto 8.190001:", det)

# T2: arrotondamento a 1 decimale (quello che fa ora _start_dark_group)
assert A.AsiairControl.round_exp(8.190001) == 8.2
assert A.AsiairControl.round_exp(1.93) == 1.9
assert A.AsiairControl.round_exp(0.04) == 0.1, "mai una posa nulla"
assert A.AsiairControl.round_exp(15.0) == 15.0
ac, calls = build(slots())
ok, n, det = ac.configure_autorun_slots("dark", {5, 0}, 30.0, {5: 8.2, 0: 1.9})
assert ok, det
assert (8, True, 8.2) in calls and (9, True, 1.9) in calls, calls
print("T2 exp arrotondate scritte negli slot:", calls)

# T3: uno slot con la posa DAVVERO sbagliata deve ancora fallire (la tolleranza
# e' 5 ms, non un colabrodo): lo slot 9 si abilita ma resta a 1.0 s invece di 1.9
ac, _ = build(slots())
ac_set = ac._call1


def exp_ignorata(port, method, params=None):
    if method == "set_sequence" and params[0]["id"] == 9:
        p = dict(params[0])
        p["exp"] = 1.0            # il firmware ignora il tempo scritto
        return ac_set(port, method, [p])
    return ac_set(port, method, params)


ac._call1 = exp_ignorata
ok, _n, det = ac.configure_autorun_slots("dark", {5, 0}, 30.0, {5: 8.2, 0: 1.9})
assert not ok and "verifica fallita" in det, det
assert "attesi dark filtri [0, 5]" in det, det   # messaggio diagnostico
print("T3 posa sbagliata rilevata:", det)

# T4: la tolleranza copre l'artefatto ma non un decimo di secondo
assert A.AsiairControl.same_exp(8.19, 8.190001)
assert A.AsiairControl.same_exp(300.0, 300.0001)
assert not A.AsiairControl.same_exp(8.2, 8.19), "0.01 s NON e' artefatto"
assert not A.AsiairControl.same_exp(1.9, 2.0)
assert A.AsiairControl.same_exp(None, 0)

# T5: il dark 'library' da 300 s (slot 10, stesso filtro) non viene MAI toccato,
# e nemmeno lo slot flat: si configurano solo gli 8 e 9
ac, calls = build(slots())
ok, _n, det = ac.configure_autorun_slots("dark", {5, 0}, 30.0, {5: 8.2, 0: 1.9})
assert ok, det
assert not any(c[0] == 10 for c in calls), f"dark library toccato: {calls}"
assert (3, False, 8.19) in calls, f"lo slot flat va DISABILITATO: {calls}"
print("T5 dark library non toccato, flat disabilitato:", det)

# T6: filtro senza slot dark utilizzabile -> avviso nel dettaglio (finisce su
# Telegram): prima passava in silenzio e la notte restava senza quei dark
ac, calls = build([dk(9, 0, 1.0)])
ok, _n, det = ac.configure_autorun_slots("dark", {5, 0}, 30.0, {5: 8.2, 0: 1.9})
assert ok, det
assert "nessuno slot" in det and "[5]" in det, det
print("T6 filtro senza slot:", det)

print("\nTUTTI I TEST DARK-EXP-TOL: OK")
