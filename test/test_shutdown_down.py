"""Test OFFLINE di wait_asiair_down (2026-08-05): dopo pi_shutdown la KASA si
stacca quando il ping muore OPPURE quando il ping resta vivo ma l'app (4700) e'
spenta — caso reale riferito dall'utente, in cui l'unico rimedio e' togliere
corrente. Prima si guardava solo il ping e il flusso abortiva lasciando il rig
acceso tutto il giorno (5/8/2026).
ping/app_alive e l'orologio sono finti: nessuna rete, nessuna attesa vera (le
sleep fanno avanzare un orologio virtuale, cosi' il test dura millisecondi).
"""
import sys
from pathlib import Path as _P
ROOT = _P(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import sfro_agent as A

SLEPT = []


class FakeTime:
    """time.sleep/time.time virtuali: dormire fa avanzare l'orologio."""
    def __init__(self):
        self.now = 1000.0

    def sleep(self, s):
        SLEPT.append(s)
        self.now += s

    def time(self):
        return self.now


def scenario(pings, apps):
    """pings/apps: liste di esiti consumati a ogni giro (l'ultimo si ripete)."""
    SLEPT.clear()
    A.time = FakeTime()
    A.ping = lambda h: pings.pop(0) if len(pings) > 1 else pings[0]
    A.app_alive = lambda h, p, timeout=3: apps.pop(0) if len(apps) > 1 else apps[0]
    return A.wait_asiair_down("1.2.3.4", 4700, wait_s=15,
                              ping_timeout_s=120, grace_s=90)


_ping, _app, _time = A.ping, A.app_alive, A.time
try:
    # A) caso normale: il box muore, il ping smette di rispondere
    down, mode, det = scenario([False], [True])
    assert (down, mode) == (True, "ping"), (down, mode, det)
    assert SLEPT == [15], SLEPT              # solo l'attesa fissa, niente grazia
    print("A) ping morto -> corrente via subito:", det)

    # B) il Pi pinga ancora ma l'app e' spenta: due verifiche, grazia, poi via
    down, mode, det = scenario([True], [False])
    assert (down, mode) == (True, "app"), (down, mode, det)
    assert SLEPT == [15, 5, 90], SLEPT       # 2 giri (no falso positivo) + grazia
    assert "4700" in det and "ping" in det, det
    print("B) ping vivo + app spenta -> corrente via dopo la grazia:", det)

    # C) rispondono ancora TUTTI E DUE: spegnimento non riuscito, KASA accesa
    down, mode, det = scenario([True], [True])
    assert (down, mode) == (False, ""), (down, mode, det)
    assert "ANCORA" in det, det
    assert sum(SLEPT[1:]) >= 120, SLEPT      # ha davvero atteso il timeout
    print("C) ping e app vivi -> KASA NON toccata:", det)

    # D) rifiuto momentaneo dell'app (un solo giro) e poi ping morto: NON deve
    #    contare come "app spenta" (altrimenti si toglie corrente a box vivo)
    down, mode, det = scenario([True, True, False], [False, True, True])
    assert (down, mode) == (True, "ping"), (down, mode, det)
    assert 90 not in SLEPT, SLEPT            # nessuna grazia: non e' il caso 'app'
    print("D) rifiuto singolo dell'app -> ignorato, si aspetta il ping:", det)
finally:
    A.ping, A.app_alive, A.time = _ping, _app, _time

print("\nTUTTI I TEST SHUTDOWN-DOWN: OK")
