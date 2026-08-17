"""Test OFFLINE dello SCARICO DEL CICLO (2026-08-15), la modifica che rende
sostenibile l'agente ogni 3 minuti invece di 5.

Misura di partenza (journal, 5 giorni): ciclo NOTTURNO 30 s di mediana contro
2,2 s di quello diurno. Tutta la differenza era lavoro di I/O che col tempo di
reazione non c'entra nulla, e che ora ha una cadenza propria:
  SYNC   : la passata rsync periodica gira ogni `sync_interval_minutes` (15),
           non piu' a ogni ciclo. I punti FISSI (fine piano, meteo, flat,
           teardown) sincronizzano SEMPRE, la cadenza non li riguarda.
  DIARIO : l'ingest+push parte solo se il sync ha portato qualcosa; e il push
           su Sheets — che rilegge e riscrive tre tab per intero — si fa solo
           con frame nuovi o righe rimaste indietro.
  KASA   : il token del cloud TP-Link sta in cache su file (600), niente piu'
           un login completo a ogni ciclo. Se il cloud lo rifiuta, UN solo
           nuovo login e si riprova.
Nessun rig, nessuna rete: fake di AsiairControl/KASA/Telegram/sessionlog.
"""
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from pathlib import Path as _P
ROOT = _P(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import sfro_agent as A
import sfro_sessionlog as SLmod

A.setup_logging("WARNING")
cfg = A.load_config(ROOT / "config.example.yaml")
TMP = Path(tempfile.mkdtemp(prefix="sfro-cadenza-"))
cfg["state_file"] = str(TMP / "state.json")
cfg["manual_shutdown_file"] = str(TMP / "manual_shutdown.json")
cfg["manual_flat_request_file"] = str(TMP / "flat_request.json")
cfg["manual_flat_reply_file"] = str(TMP / "flat_reply.json")
cfg["flat_cancel_request_file"] = str(TMP / "flat_cancel.json")
cfg["kasa_token_file"] = str(TMP / "kasa_token.json")
TOK = Path(cfg["kasa_token_file"])
tz = ZoneInfo(cfg["timezone_observatory"])

# =============================== 1. CONFIGURAZIONE ===========================
# Il timer e la config devono raccontare la stessa storia: un deploy a meta'
# (codice nuovo, timer vecchio) e' proprio il caso che non si vede a occhio.
timer = (ROOT / "systemd" / "sfro-agent.timer").read_text()
assert "OnCalendar=*:0/3" in timer, timer
assert cfg["sync_module"].get("sync_interval_minutes") == 15, cfg["sync_module"]
assert cfg.get("kasa_token_file"), "config: kasa_token_file"
print("### 1 config: timer *:0/3, sync 15 min, cache token configurata")

# =============================== 2. TOKEN KASA ===============================
cfg["kasa"]["device_id"] = cfg["kasa"]["device_id"] or "DEVICEID0000000000000000000000000000000"
DEV_ID = cfg["kasa"]["device_id"]
(TMP / "kasa.txt").write_text("username=tizio@example.com\npassword=segreta\n")
kfiles = {"kasa": TMP / "kasa.txt", "telegram": A.Path("/no"),
          "asiair": A.Path("/no")}
SYS = {"system": {"get_sysinfo": {"children": [
    {"id": "1", "alias": "Asiair", "state": 1},
    {"id": "2", "alias": "Mount", "state": 0}]}}}


class FakeCloud:
    """Cloud TP-Link finto: accetta un solo token valido per volta."""
    def __init__(self):
        self.calls, self.valid = [], "T1"

    # istanza, non funzione: assegnata a KasaCloud._post NON viene legata
    # (niente self del chiamante), quindi la firma e' (url, payload)
    def __call__(self, url, payload):
        m = payload.get("method")
        self.calls.append(m)
        if m == "login":
            return {"token": self.valid}
        if url.split("token=")[-1] != self.valid:
            raise RuntimeError("Kasa error -20651: Token expired")
        if m == "getDeviceList":
            return {"deviceList": [{"deviceId": DEV_ID, "alias": "strip",
                                    "appServerUrl": "https://eu.tplink"}]}
        return {"responseData": json.dumps(SYS)}


cloud = FakeCloud()
A.KasaCloud._post = cloud


def kasa(label):
    cloud.calls.clear()
    r = A._kasa_connect(cfg, kfiles, cfg["http"], "uuid-test")
    print(f"### {label}\n   post:", cloud.calls, "| plug:", r[3], "| err:", r[4])
    return r


# E1: prima volta -> login vero, token salvato a permessi 600
TOK.unlink(missing_ok=True)
r = kasa("E1 primo giro (cache vuota)")
assert cloud.calls[0] == "login" and r[4] is None, cloud.calls
assert r[3] == "ON" and len(r[2]) == 2, r
assert TOK.exists() and (TOK.stat().st_mode & 0o777) == 0o600, oct(TOK.stat().st_mode)
assert json.loads(TOK.read_text())["token"] == "T1"

# E2: giro successivo -> NESSUN login, si usa la cache
r = kasa("E2 secondo giro (token in cache)")
assert "login" not in cloud.calls, cloud.calls
assert r[3] == "ON" and r[4] is None, r

# E3: token scaduto per TTL -> login rifatto
d = json.loads(TOK.read_text())
d["ts"] = time.time() - (cfg["kasa"].get("token_ttl_hours", 12) * 3600 + 60)
TOK.write_text(json.dumps(d))
r = kasa("E3 token scaduto (TTL)")
assert cloud.calls[0] == "login" and r[4] is None, cloud.calls

# E4: cache di un ALTRO utente (credenziali cambiate) -> login rifatto
TOK.write_text(json.dumps({"token": "T1", "user": "altro@example.com",
                           "ts": time.time()}))
r = kasa("E4 cache di un altro utente")
assert cloud.calls[0] == "login" and r[4] is None, cloud.calls

# E5: token in cache RIFIUTATO dal cloud -> un solo nuovo login, poi funziona
cloud.valid = "T2"          # il cloud ha invalidato T1: quello in cache non vale
r = kasa("E5 token in cache rifiutato dal cloud")
assert cloud.calls.count("login") == 1, cloud.calls
assert r[3] == "ON" and r[4] is None, r
assert json.loads(TOK.read_text())["token"] == "T2", TOK.read_text()

# E6: cloud giu' con token FRESCO -> un solo tentativo, nessun raddoppio
TOK.unlink(missing_ok=True)


class Boom:
    def __call__(self, url, payload):
        cloud.calls.append(payload.get("method"))
        raise RuntimeError("connessione rifiutata")


A.KasaCloud._post = Boom()
r = kasa("E6 cloud irraggiungibile")
assert cloud.calls == ["login"], cloud.calls
assert r[4] and r[3] == "UNKNOWN", r
assert not TOK.exists(), "nessun token da salvare se il login fallisce"
A.KasaCloud._post = cloud

# =============================== 3. PUSH DEL DIARIO ==========================
PUSHES = []
SLmod.push = lambda c: (PUSHES.append("push"), {})[1]
SLmod.build_report = lambda c: {}
ING = {"new": 0, "nights": []}
PEND = [0]
SLmod.ingest = lambda c: dict(ING)
SLmod.pending_push = lambda c: PEND[0]


def diario(label, new, pending):
    ING["new"], PEND[0] = new, pending
    PUSHES.clear()
    r = SLmod.update_and_push(cfg)
    print(f"### {label}\n   push:", PUSHES or "-", "| pushed:", r.get("pushed"))
    return r


# D1: niente di nuovo e niente arretrato -> il foglio non si tocca
r = diario("D1 nessun frame nuovo", new=0, pending=0)
assert PUSHES == [] and r["pushed"] is False, r
# D2: frame nuovi -> push
r = diario("D2 frame nuovi", new=3, pending=0)
assert PUSHES == ["push"] and r["pushed"] is True, r
# D3: nessun frame nuovo ma righe rimaste indietro (NAS giu' al giro prima)
r = diario("D3 arretrato da recuperare", new=0, pending=5)
assert PUSHES == ["push"] and r["pushed"] is True, r

# =============================== 4. CADENZA DEL SYNC =========================
state = {}
MSGS, CALLS = [], []
A.Telegram.send = lambda self, t, kb=None: MSGS.append(t)
SYNC = {"ok": True, "files": 7, "error": None}


class FakeSL:
    @staticmethod
    def ingest(cfg): return {"new": 2, "nights": ["2026-08-15"]}
    @staticmethod
    def finalize(cfg, nid, cause): return 1
    @staticmethod
    def push(cfg): return {}
    @staticmethod
    def build_report(cfg): return {}
    @staticmethod
    def update_and_push(cfg): CALLS.append("diario"); return {"new": 0}
    @staticmethod
    def filters_gains_used(cfg, nid): return [("H", 100)]


A.SL = FakeSL
KB = dict(in_night=True, roof="OPEN", asiair_ping=True, night="2026-08-15",
          snap=None, plug="ON")
A.nautical_window = lambda now, loc, tz, dep=12: (KB["in_night"], None, None,
                                                  KB["night"])
A.vpn_diagnose = lambda host, probe="": {"vpn_up": True,
                                         "asiair_up": KB["asiair_ping"], "cause": ""}
A.http_get_json = lambda u, h: {"Value": KB["roof"] == "OPEN", "ErrorNumber": 0}
A.sync_pass = lambda sc, dry: (CALLS.append("sync"), dict(SYNC))[1]
A.time.sleep = lambda s: None
A.AsiairControl.snapshot = lambda self: KB["snap"]
A.AsiairControl.start = lambda self, snap=None: (CALLS.append("plan_start"),
                                                 (True, ""))[1]
A.AsiairControl.plan_left = lambda self: (True, 0, "1/1 target completati")
A.AsiairControl.connect_all = lambda self: (True, "5 device")
A.AsiairControl.teardown = lambda self, keep_cooler=False: (
    CALLS.append(f"teardown(keep_cooler={keep_cooler})"),
    {"stop": (True, ""), "home": (True, ""), "flat": (True, "")})[1]
A._kasa_connect = lambda cfg, files, http, tu: (
    "kc", "dev", [{"alias": "Asiair", "id": "1", "state": 1}], KB["plug"], None)
A.ping = lambda host: KB["asiair_ping"]

NOW = [datetime(2026, 8, 15, 3, 0, tzinfo=tz)]
_real_dt = A.datetime


class FakeDT(_real_dt):
    @classmethod
    def now(cls, tz=None):
        return NOW[0].astimezone(tz) if tz else NOW[0]


A.datetime = FakeDT
files = {"telegram": A.Path("/no"), "kasa": A.Path("/no"), "asiair": A.Path("/no")}


def snap(**kw):
    s = {"reachable": True, "capturing": True, "plan_started": True,
         "plan_name": "Sideris", "cam_open": True, "focuser_connected": True,
         "wheel_connected": True, "mount_connected": True, "guide_connected": True,
         "has_plan": True, "flat_open": True}
    s.update(kw)
    return s


def cyc(label, minutes=None, **kw):
    """Un ciclo dell'agente. `minutes` avanza l'orologio virtuale."""
    if minutes is not None:
        NOW[0] = NOW[0] + timedelta(minutes=minutes)
    KB.update(kw)
    MSGS.clear()
    CALLS.clear()
    A.run_cycle(cfg, state, files, dry_run=False)
    last = (state.get("last_sync_ts") or "")[11:19]
    print(f"### {label} [{NOW[0]:%H:%M}]\n   calls:", CALLS or "-",
          "| last_sync:", last or "-")


def has(c):
    return c in CALLS


# C1: prima ripresa della notte -> nessun last_sync_ts, si sincronizza subito
cyc("C1 prima ripresa (nessun sync precedente)", snap=snap())
assert has("sync") and has("diario"), CALLS
assert state["last_sync_ts"].startswith("2026-08-15T03:00"), state["last_sync_ts"]

# C2/C3: i cicli dentro i 15 minuti NON sincronizzano (e' il guadagno vero:
# a 3 minuti sono 4 cicli su 5 che tornano a costare due secondi)
cyc("C2 +3 min", 3, snap=snap())
assert not has("sync") and not has("diario"), CALLS
cyc("C3 +12 min (14' dal sync)", 11, snap=snap())
assert not has("sync"), CALLS

# C4: scaduta la cadenza -> nuova passata
cyc("C4 +15 min esatti dal sync", 1, snap=snap())
assert has("sync") and has("diario"), CALLS
assert state["last_sync_ts"].startswith("2026-08-15T03:15"), state["last_sync_ts"]

# C5: passata che non porta nulla -> niente ingest, il diario non si tocca
SYNC.update(files=0)
cyc("C5 sync a vuoto (0 elementi)", 15, snap=snap())
assert has("sync") and not has("diario"), CALLS

# C6: sync in ERRORE -> avviso, ma il timestamp avanza lo stesso: a VPN giu'
# non si martella ogni ciclo
SYNC.update(files=0, error="NAS dest non montato", ok=False)
cyc("C6 sync in errore", 15, snap=snap())
assert has("sync") and any("Sync FITS in errore" in m for m in MSGS), MSGS
cyc("C7 ciclo dopo l'errore: si rispetta la cadenza", 3, snap=snap())
assert not has("sync"), CALLS
SYNC.update(files=7, error=None, ok=True)

# C8: PUNTO FISSO — chiusura meteo durante la ripresa: il sync finale parte
# comunque, cadenza o non cadenza, e riallinea il timestamp
cyc("C8 tetto chiuso dal meteo (sync fisso)", 3, roof="CLOSED", snap=snap())
assert has("sync"), CALLS
assert state["last_sync_ts"].startswith("2026-08-15T03:51"), state["last_sync_ts"]
assert state.get("weather_closed"), state

# C9: nuova notte -> lo stato si azzera e la prima ripresa sincronizza subito
NOW[0] = datetime(2026, 8, 16, 3, 0, tzinfo=tz)
cyc("C9 nuova notte", night="2026-08-16", roof="OPEN", snap=snap())
assert state.get("night_id") == "2026-08-16", state.get("night_id")
assert has("sync") and has("diario"), CALLS

print("\nTUTTI I TEST DI CADENZA OK ✅")
