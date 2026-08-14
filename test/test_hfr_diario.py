"""Test OFFLINE della statistica autofocus nel diario (2026-08-12):
il publisher scrive una riga di sink a ogni AutoFocus concluso, il sessionlog
la trasforma nelle colonne "HFR migliore"/"N autofocus" della tab Sessioni.
Nessun ASIAIR, nessun Google Sheet, nessun broker: DB e sink in una sandbox.
"""
import json
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from pathlib import Path as _P
ROOT = _P(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import sfro_agent as A
import sfro_mqtt as M
import sfro_sessionlog as SL

TMP = Path(tempfile.mkdtemp(prefix="hfr-test-"))
SINK = TMP / "focus"
TZ = ZoneInfo("America/Chicago")

cfg = A.load_config(ROOT / "config.example.yaml")

# --- A) il publisher scrive il sink SOLO sugli autofocus conclusi -----------
pub = M.Publisher(cfg, dry=True)
pub.focus_sink_dir = str(SINK)

pub._apply_event({"Event": "AutoFocus", "state": "working"})
pub._apply_event({"Event": "AutoFocus", "state": "complete",
                  "result": {"last_point": [13996, 2.83]}})
pub._apply_event({"Event": "AutoFocus", "state": "failed",
                  "result": {"last_point": [14000, 9.9]}})
# senza last_point si ripiega sul minimo della curva a V
pub._apply_event({"Event": "AutoFocus", "state": "complete",
                  "result": {"points": [[14056, 6.1], [13990, 3.11], [13970, 4.4]]}})
pub._apply_event({"Event": "AutoFocus", "state": "complete", "result": {}})  # nulla da scrivere

righe = []
for f in sorted(SINK.glob("*.jsonl")):
    righe += [json.loads(l) for l in open(f) if l.strip()]
assert len(righe) == 2, righe
assert righe[0]["hfr"] == 2.83 and righe[0]["pos"] == 13996, righe[0]
assert righe[1]["hfr"] == 3.11 and righe[1]["pos"] == 13990, righe[1]
print("A) sink: 2 righe dalle sole corse concluse ->", righe)

# --- B) le corse si raggruppano per NOTTE, non per data ---------------------
shutil.rmtree(SINK, ignore_errors=True)
SINK.mkdir(parents=True)
notte = "2026-08-11"


def scrivi(quando_locale, hfr):
    t = quando_locale.timestamp()
    giorno = datetime.fromtimestamp(t, timezone.utc).date().isoformat()
    with open(SINK / f"{giorno}.jsonl", "a") as fh:
        fh.write(json.dumps({"t": t, "hfr": hfr, "pos": 14000}) + "\n")


# stessa notte: sera dell'11 e piccole ore del 12 (scavallano la mezzanotte)
scrivi(datetime(2026, 8, 11, 21, 30, tzinfo=TZ), 3.40)
scrivi(datetime(2026, 8, 12, 0, 20, tzinfo=TZ), 2.83)      # il migliore
scrivi(datetime(2026, 8, 12, 3, 10, tzinfo=TZ), 3.05)
# notte SUCCESSIVA: non deve entrare nel conto
scrivi(datetime(2026, 8, 12, 22, 15, tzinfo=TZ), 1.99)

fs = SL.focus_stats(str(SINK), notte, TZ)
assert fs == {"hfr_best": 2.83, "af_n": 3}, fs
print("B) notte 11/8 (scavalca la mezzanotte):", fs)
assert SL.focus_stats(str(SINK), "2026-08-12", TZ) == {"hfr_best": 1.99, "af_n": 1}
print("   notte 12/8 tenuta separata: ok")

# --- C) casi vuoti: nessun sink, cartella assente, notte senza autofocus ----
assert SL.focus_stats("", notte, TZ) == {"hfr_best": None, "af_n": 0}
assert SL.focus_stats(str(TMP / "inesistente"), notte, TZ) == {"hfr_best": None, "af_n": 0}
assert SL.focus_stats(str(SINK), "2026-07-01", TZ) == {"hfr_best": None, "af_n": 0}
print("C) sink assente o notte senza autofocus -> nessun dato, nessun errore")

# --- D) le colonne arrivano in fondo alla riga di Sessioni ------------------
assert SL.SESS_HEADER[-2:] == ["HFR migliore", "N autofocus"], SL.SESS_HEADER
assert len(SL.SESS_HEADER) == 22, len(SL.SESS_HEADER)
print("D) SESS_HEADER a 22 colonne, le due nuove in coda")

# --- E) DB: migrazione di uno vecchio e aggregazione della notte ------------
db_path = TMP / "sessions.db"
import sqlite3
vecchio = sqlite3.connect(db_path)
vecchio.executescript("""
CREATE TABLE frames (
  file TEXT PRIMARY KEY, night_id TEXT, object TEXT, filter TEXT,
  date_obs_utc TEXT, date_obs_local TEXT, exptime REAL,
  gain INTEGER, offset INTEGER, bin INTEGER, ccd_temp REAL, focus_pos INTEGER,
  rms_tot REAL, rms_ra REAL, rms_dec REAL, rms_peak REAL,
  guide_n INTEGER, guide_cover_pct REAL, dither_pct REAL,
  size_mb REAL, path TEXT, pushed INTEGER DEFAULT 0, added_utc TEXT);
CREATE TABLE sessions (
  night_id TEXT, object TEXT, filter TEXT, exptime REAL,
  n_frames INTEGER, total_s REAL, t_start_local TEXT, t_end_local TEXT,
  rms_tot_avg REAL, rms_ra_avg REAL, rms_dec_avg REAL, rms_max REAL,
  gain INTEGER, bin INTEGER, temp_avg REAL, guide_cover_avg REAL,
  moon_pct REAL, end_cause TEXT, updated_utc TEXT,
  PRIMARY KEY (night_id, object, filter, exptime));
""")
vecchio.execute("INSERT INTO frames (file, night_id, object, filter, exptime, "
                "date_obs_local, gain, bin) VALUES "
                "('a.fit', ?, 'NGC 6823', 'L', 300, '2026-08-11T22:00:00-05:00', 100, 1)",
                (notte,))
vecchio.commit()
vecchio.close()

db = SL.open_db(str(db_path))            # deve migrare senza perdere nulla
cols = {r[1] for r in db.execute("PRAGMA table_info(sessions)")}
assert {"hfr_best", "af_n"} <= cols, cols
print("E) DB vecchio migrato: colonne", sorted(cols - {'night_id'})[:3], "…")

SL._aggregate_night(db, notte, TZ, str(SINK))
r = db.execute("SELECT object, n_frames, hfr_best, af_n FROM sessions "
               "WHERE night_id=?", (notte,)).fetchone()
assert r == ("NGC 6823", 1, 2.83, 3), r
print("F) riga di Sessioni:", r)

# rieseguire non deve duplicare ne' cambiare i valori
SL._aggregate_night(db, notte, TZ, str(SINK))
assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
assert db.execute("SELECT hfr_best, af_n FROM sessions").fetchone() == (2.83, 3)
print("G) idempotente: una sola riga, stessi valori")
db.close()

shutil.rmtree(TMP, ignore_errors=True)
print("\nTUTTI I TEST HFR-DIARIO: OK")
