"""Test OFFLINE della STIMA CONDENSA e dell'attesa di asciugatura adattiva
(2026-09-06). Nessuna rete: la stazione meteo e' un fake che risponde alle
proprieta' Alpaca observingconditions.

Copre:
  - lettura + controlli di sanita' (valori assurdi = dato assente)
  - raffreddamento radiativo stimato (cielo sereno/velato/coperto/ignoto, vento)
  - correzione sotto zero (BRINA: punto di brinamento piu' alto della rugiada)
  - accumulo di notte (margine minimo, minuti bagnati, pioggia) e cadenza
  - verdetto e minuti: SECCA 5' / INTERMEDIA 10' / NORMALE 20' / UMIDA 30',
    con tutte le condizioni di caduta sul valore fisso (NORMALE = caso CIECO)
"""
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path as _P
ROOT = _P(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import sfro_agent as A

A.setup_logging("ERROR")
cfg = A.load_config(ROOT / "config.example.yaml")
D = cfg["dew"]
FF = cfg["flat_flow"]
tz = ZoneInfo(cfg["timezone_observatory"])
T0 = datetime(2026, 9, 6, 2, 0, tzinfo=tz)

assert D.get("enabled"), "config: dew.enabled"
assert FF.get("dry_wait_dry_minutes") == 5, "config: 5 min a notte secca"
assert FF.get("dry_wait_minutes") == 20, "config: 20 min standard"
assert FF.get("dry_wait_wet_minutes") == 30, "config: 30 min a notte umida"

# --- fake della stazione ---------------------------------------------------
STATION = {}


def fake_get(url, http, session=None):
    for prop, key in A.WEATHER_PROPS.items():
        if url.endswith("/" + prop):
            v = STATION.get(key, "MISSING")
            if v == "MISSING":
                return {"ErrorNumber": 1024, "ErrorMessage": "not implemented"}
            return {"Value": v, "ErrorNumber": 0}
    return None


A.http_get_json = fake_get


def station(**kw):
    STATION.clear()
    STATION.update(kw)


# --- 1) lettura e sanita' ---------------------------------------------------
station(temp_c=20.0, dewpoint_c=10.0, humidity_pct=52.0, sky_temp_c=-5.0,
        wind_ms=0.5, rain_mm_h=0.0)
w = A.read_weather(cfg["roof"], D)
assert w and w["temp_c"] == 20.0 and w["dewpoint_c"] == 10.0, w

station(temp_c=20.0)                       # manca il punto di rugiada
assert A.read_weather(cfg["roof"], D) is None, "senza rugiada niente stima"
station(temp_c=20.0, dewpoint_c=25.0)      # rugiada SOPRA l'aria: impossibile
assert A.read_weather(cfg["roof"], D) is None, "campione assurdo va scartato"
station(temp_c=999.0, dewpoint_c=10.0)
assert A.read_weather(cfg["roof"], D) is None, "temperatura fuori scala"
station(temp_c=20.0, dewpoint_c=10.0, humidity_pct=-3.0, sky_temp_c=-200.0)
w = A.read_weather(cfg["roof"], D)
assert w["humidity_pct"] is None and w["sky_temp_c"] is None, w
print("lettura + sanita': OK")

# --- 2) raffreddamento radiativo stimato ------------------------------------
def marg(**kw):
    station(**kw)
    return A.dew_margin(A.read_weather(cfg["roof"], D), D)


m = marg(temp_c=20.0, dewpoint_c=10.0, sky_temp_c=-5.0, wind_ms=0.5)
assert m["cooling_k"] == 5 and m["margin_c"] == 5.0, m       # sereno e calmo
m = marg(temp_c=20.0, dewpoint_c=10.0, sky_temp_c=-5.0, wind_ms=4.0)
assert m["cooling_k"] == 3 and m["margin_c"] == 7.0, m       # sereno ma ventoso
m = marg(temp_c=20.0, dewpoint_c=10.0, sky_temp_c=10.0, wind_ms=0.5)
assert m["cooling_k"] == 3, m                                 # velato
m = marg(temp_c=20.0, dewpoint_c=10.0, sky_temp_c=18.0, wind_ms=0.5)
assert m["cooling_k"] == 1 and m["margin_c"] == 9.0, m       # coperto
m = marg(temp_c=20.0, dewpoint_c=10.0, wind_ms=0.5)           # cielo ignoto
assert m["cooling_k"] == 5, "cielo ignoto -> caso peggiore, mai il piu' comodo"
m = marg(temp_c=20.0, dewpoint_c=16.0, sky_temp_c=-5.0, wind_ms=0.5)
assert m["condensa"] is True and m["margin_c"] == -1.0, m
print("raffreddamento radiativo: OK")

# --- 3) BRINA: sotto zero si e' piu' prudenti -------------------------------
m = marg(temp_c=3.0, dewpoint_c=-4.0, sky_temp_c=-30.0, wind_ms=0.5)
# 3 - (-4) = 7, meno k=5 -> 2; superficie stimata 3-5 = -2 <= 0 -> BRINA, -1
assert m["frost"] is True and m["margin_c"] == 1.0, m
m = marg(temp_c=20.0, dewpoint_c=10.0, sky_temp_c=-5.0, wind_ms=0.5)
assert m["frost"] is False, "sopra zero e' condensa, non brina"
print("correzione brina: OK")

# --- 4) accumulo di notte e cadenza -----------------------------------------
st = {}
station(temp_c=20.0, dewpoint_c=10.0, sky_temp_c=-5.0, wind_ms=0.5, rain_mm_h=0.0)
A.dew_sample(st, cfg, T0)
assert st["dew_samples"] == 1 and st["dew_margin_min"] == 5.0, st
A.dew_sample(st, cfg, T0 + timedelta(minutes=2))     # troppo presto
assert st["dew_samples"] == 1, "cadenza non rispettata"
station(temp_c=12.0, dewpoint_c=11.0, sky_temp_c=-9.0, wind_ms=0.5, rain_mm_h=0.0)
A.dew_sample(st, cfg, T0 + timedelta(minutes=6))     # margine 1-5 = -4
assert st["dew_samples"] == 2 and st["dew_margin_min"] == -4.0, st
assert st["dew_wet_minutes"] == 6, st
assert st["dew_last"]["condensa"] is True
station(temp_c=12.0, dewpoint_c=11.0, sky_temp_c=-9.0, wind_ms=0.5, rain_mm_h=0.4)
A.dew_sample(st, cfg, T0 + timedelta(minutes=12))
assert st["dew_rain"] is True and st["dew_wet_minutes"] == 12, st
station(temp_c=99.0, dewpoint_c=11.0)                # campione fuori scala
prev = dict(st)
A.dew_sample(st, cfg, T0 + timedelta(minutes=18))
assert st["dew_samples"] == prev["dew_samples"], "un campione assurdo non conta"
assert st["dew_fail"] == 1, st
print("accumulo di notte: OK")


def verdict(**over):
    s = {"dew_samples": 40, "dew_margin_min": 6.0, "dew_wet_minutes": 0,
         "dew_last": {"margin_c": 6.0, "ts": T0.isoformat(), "temp_c": 20.0,
                      "dewpoint_c": 10.0}}
    s.update(over)
    return A.dew_verdict(s, cfg, T0)


# --- 5) verdetto ------------------------------------------------------------
v, mnt, why = verdict()
assert (v, mnt) == ("SECCA", 5.0), (v, mnt, why)

v, mnt, why = verdict(dew_samples=0, dew_last={})
assert (v, mnt) == ("NORMALE", 20.0) and "nessun dato" in why, (v, mnt, why)

v, mnt, why = verdict(dew_last={"margin_c": 6.0,
                                "ts": (T0 - timedelta(hours=3)).isoformat()})
assert (v, mnt) == ("NORMALE", 20.0) and "non aggiornato" in why, (v, mnt, why)

v, mnt, why = verdict(dew_samples=8)
assert (v, mnt) == ("NORMALE", 20.0) and "pochi dati" in why, (v, mnt, why)

# misurata tutta la notte e mai bagnata, ma senza i 3°C della banda SECCA:
# banda INTERMEDIA (2026-09-07), non i 20' del caso cieco
v, mnt, why = verdict(dew_margin_min=1.2)
assert (v, mnt) == ("INTERMEDIA", 10.0) and "senza condensa" in why, (v, mnt, why)

# il caso vero della notte 6->7 settembre: spread mai sotto 5, margine minimo
# 0.1°C su 75 letture. Mai bagnata -> 10', non 20'
v, mnt, why = verdict(dew_samples=75, dew_margin_min=0.1,
                      dew_last={"margin_c": 0.3, "ts": T0.isoformat(),
                                "temp_c": 24.7, "dewpoint_c": 19.4})
assert (v, mnt) == ("INTERMEDIA", 10.0) and "75 letture" in why, (v, mnt, why)

# ...ma quella notte ebbe una chiusura meteo: a tetto chiuso non si campiona,
# quindi resta il caso CIECO a 20' anche con 75 letture buone
v, mnt, why = verdict(dew_samples=75, dew_margin_min=0.1, dew_blocked=True,
                      dew_last={"margin_c": 0.3, "ts": T0.isoformat()})
assert (v, mnt) == ("NORMALE", 20.0) and "chiusura meteo" in why, (v, mnt, why)

v, mnt, why = verdict(dew_blocked=True)
assert (v, mnt) == ("NORMALE", 20.0) and "chiusura meteo" in why, (v, mnt, why)

# pochi dati: il caso cieco vince anche se il poco che si e' visto era asciutto
v, mnt, why = verdict(dew_samples=8, dew_margin_min=1.2)
assert (v, mnt) == ("NORMALE", 20.0) and "pochi dati" in why, (v, mnt, why)

v, mnt, why = verdict(dew_wet_minutes=42, dew_margin_min=-1.0)
assert (v, mnt) == ("UMIDA", 30.0) and "42 min" in why, (v, mnt, why)

v, mnt, why = verdict(dew_rain=True)
assert (v, mnt) == ("UMIDA", 30.0) and "pioggia" in why, (v, mnt, why)

v, mnt, why = verdict(dew_last={"margin_c": -0.5, "ts": T0.isoformat()})
assert (v, mnt) == ("UMIDA", 30.0) and "in atto" in why, (v, mnt, why)

# margine minimo ampio ma condensa ADESSO -> umida (conta anche il presente)
v, mnt, why = verdict(dew_last={"margin_c": 0.0, "ts": T0.isoformat()})
assert (v, mnt) == ("UMIDA", 30.0), (v, mnt, why)

# margine minimo ampio ma ORA sotto la soglia di secco -> niente banda SECCA,
# ma la notte e' stata misurata e mai bagnata: INTERMEDIA
v, mnt, why = verdict(dew_last={"margin_c": 1.5, "ts": T0.isoformat()})
assert (v, mnt) == ("INTERMEDIA", 10.0), (v, mnt, why)
print("verdetto: OK")

# --- 6) interruttori --------------------------------------------------------
cfg2 = A.load_config(ROOT / "config.example.yaml")
cfg2["dew"]["adaptive"] = False
s = {"dew_samples": 40, "dew_margin_min": 6.0, "dew_wet_minutes": 0,
     "dew_last": {"margin_c": 6.0, "ts": T0.isoformat()}}
v, mnt, why = A.dew_verdict(s, cfg2, T0)
assert (v, mnt) == ("NORMALE", 20.0) and "OSSERVAZIONE" in why and "SECCA" in why, why
cfg2["dew"]["enabled"] = False
v, mnt, why = A.dew_verdict(s, cfg2, T0)
assert (v, mnt) == ("NORMALE", 20.0) and "disattivata" in why, why
assert A.dew_sample(s, cfg2, T0) == {}, "disabilitato = nessuna lettura"
print("interruttori enabled/adaptive: OK")

# --- 7) riga Telegram -------------------------------------------------------
s = {"dew_last": {"margin_c": -1.4, "temp_c": 12.0, "dewpoint_c": 11.0,
                  "humidity_pct": 94.0, "sky_temp_c": -9.0}}
txt = A.dew_text(s, "UMIDA", 30, "42 min con condensa (min -4.0°C)")
assert "💧" in txt and "asciugatura 30 min" in txt and "rugiada 11.0" in txt, txt
print("riga Telegram:", txt.replace("\n", " | "))

print("\nTUTTI I TEST CONDENSA: OK")
