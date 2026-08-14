#!/usr/bin/env python3
"""
SFRO — Storico sessioni: FITS (NAS) -> SQLite -> Google Sheets.

Due log (decisi con l'utente 2026-07-03):
  1. SESSIONI  : un record per notte x soggetto x filtro x tempo esposizione
                 (n. esposizioni, integrazione totale, RMS medi, causa fine, luna...)
  2. DETTAGLIO : un record per FILE (RMS medio nella finestra dell'esposizione,
                 filtro, esposizione, temperatura, guida...)

Fonti:
  - Header FITS dei Light sul NAS (autorevole per soggetto/filtro/tempi/temp).
    DATE-OBS = inizio esposizione in UTC (verificato 2026-07-03 vs mtime).
    NB: l'header ASIAIR NON contiene HFR/star count.
  - Sink guida JSONL scritto da sfro_mqtt.py (eventi GuideStep, arcsec):
    /var/lib/sfro-agent/guide/YYYY-MM-DD.jsonl (data UTC), righe
    {"t": epoch, "ra": arcsec, "dec": arcsec, "b": 0|1}  (b=1 -> settle/dither).

Fonte di verita' = SQLite (dedup per nome file); il Google Sheet ('Diario
Astrofotografia', condiviso tra postazioni: tab 'Sessioni' con colonna Location
+ tab dettaglio per postazione, es. 'Dettaglio_SFRO') e' un MIRROR:
Dettaglio append-only, Sessioni riscritto.
SOLO Light: FLAT/DARK/BIAS esclusi (deciso dall'utente).
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import logging
log = logging.getLogger("sfro-sessionlog")

DEFAULTS = {
    "enabled": False,
    "db_file": "/var/lib/sfro-agent/sessions.db",
    "scan_dir": "/mnt/astronomia/asiair_sfro/ASIAIR/Plan/Light",
    "guide_sink_dir": "/var/lib/sfro-agent/guide",
    # sink autofocus (scritto dal publisher MQTT a ogni corsa conclusa):
    # alimenta le colonne "HFR migliore"/"N autofocus" della tab Sessioni
    "focus_sink_dir": "/var/lib/sfro-agent/focus",
    "backfill_days": 30,        # alla prima esecuzione ingerisce solo file recenti
    "sheet_id": "",
    "sa_json": "/opt/sfro-agent/gdrive_sa.json",
    "tab_sessions": "Sessioni",
    "tab_detail": "",           # vuoto = dettaglio per-frame NON su Sheets (2026-08-12)
    "tab_by_object": "Per soggetto",
    "tab_by_month": "Per mese",
    # dettaglio per-frame: un CSV per notte sul NAS (vuoto = niente CSV)
    "detail_csv_dir": "",
    "location_label": "SFRO",   # colonna 'Location' della tab Sessioni (sheet condiviso tra postazioni)
    "idle_finalize_minutes": 10,
}

SESS_HEADER = ["Location", "Notte", "Soggetto", "Filtro", "Esp (s)", "N frame", "Integrazione",
               "Inizio (loc)", "Fine (loc)", "RMS med (\")", "RMS AR (\")",
               "RMS DEC (\")", "RMS max (\")", "Gain", "Bin", "Temp med (°C)",
               "Guida (%)", "Luna (%)", "Causa fine", "Aggiornato (UTC)",
               "HFR migliore", "N autofocus"]
DET_HEADER = ["File", "Notte", "Soggetto", "Filtro", "Esp (s)", "Inizio (UTC)",
              "Inizio (loc)", "RMS med (\")", "RMS AR (\")", "RMS DEC (\")",
              "Picco (\")", "Campioni", "Copertura (%)", "Dither (%)", "Gain",
              "Offset", "Bin", "Temp (°C)", "Fuoco", "MB", "Percorso"]
DET_SELECT = """SELECT file, night_id, object, filter, exptime, date_obs_utc,
                       date_obs_local, rms_tot, rms_ra, rms_dec, rms_peak, guide_n,
                       guide_cover_pct, dither_pct, gain, offset, bin, ccd_temp,
                       focus_pos, size_mb, path
                FROM frames"""
# Riepiloghi (riscritti a ogni push: poche righe, sempre coerenti col DB)
BYOBJ_HEADER = ["Location", "Soggetto", "Filtro", "Notti", "N frame", "Integrazione",
                "Ore", "Esp (s)", "Gain", "RMS med (\")", "Prima notte", "Ultima notte"]
BYMON_HEADER = ["Location", "Mese", "Notti", "N frame", "Integrazione", "Ore",
                "Soggetti", "RMS med (\")", "HFR med", "Luna med (%)"]


def sl_cfg(cfg: dict) -> dict:
    out = dict(DEFAULTS)
    out.update(cfg.get("session_log", {}) or {})
    return out


# --------------------------------------------------------------------------- #
# Header FITS (parser minimale: solo primary header, niente astropy)
# --------------------------------------------------------------------------- #
def read_fits_header(path: str) -> dict:
    with open(path, "rb") as fh:
        raw = b""
        while True:
            block = fh.read(2880)
            raw += block
            if b"END     " in block or len(block) < 2880:
                break
    hdr = {}
    for i in range(0, len(raw), 80):
        card = raw[i:i + 80].decode("ascii", "replace")
        key = card[:8].strip()
        if key == "END":
            break
        if card[8:10] != "= ":
            continue
        val = card[10:]
        if val.lstrip().startswith("'"):          # stringa FITS 'xxx'
            s = val.lstrip()[1:]
            val = s.split("'", 1)[0].strip()
        else:
            val = val.split("/", 1)[0].strip()
        hdr[key] = val
    return hdr


def _f(hdr, key, default=None):
    try:
        return float(hdr[key])
    except (KeyError, ValueError, TypeError):
        return default


def _i(hdr, key, default=None):
    v = _f(hdr, key)
    return int(v) if v is not None else default


def night_id_of(dt_utc: datetime, tz: ZoneInfo) -> str:
    """Notte = data del crepuscolo SERALE locale (stessa convenzione dell'agente):
    ora locale meno 12h -> data."""
    return (dt_utc.astimezone(tz) - timedelta(hours=12)).date().isoformat()


def moon_illum_pct(d) -> float:
    """% di illuminazione lunare (approssimazione dalla fase astral 0..29.53)."""
    try:
        from astral import moon
        phase = moon.phase(d)                     # 0=nuova, ~14.77=piena
        return round(50.0 * (1 - math.cos(2 * math.pi * phase / 29.53)), 0)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Guida: statistiche per finestra [t0, t1] dal sink JSONL
# --------------------------------------------------------------------------- #
def guide_stats(sink_dir: str, t0: datetime, t1: datetime) -> dict:
    """RMS/picco (arcsec) dei campioni GuideStep nella finestra (UTC).
    Esclude i campioni settle/dither dall'RMS (come ASIAIR/PHD2)."""
    out = {"rms_tot": None, "rms_ra": None, "rms_dec": None, "peak": None,
           "n": 0, "cover_pct": None, "dither_pct": None}
    days = set()
    d = t0.date()
    while d <= t1.date():
        days.add(d)
        d += timedelta(days=1)
    e0, e1 = t0.timestamp(), t1.timestamp()
    good, bad_n, first, last = [], 0, None, None
    for d in sorted(days):
        p = Path(sink_dir) / f"{d.isoformat()}.jsonl"
        if not p.exists():
            continue
        try:
            with open(p) as fh:
                for line in fh:
                    try:
                        s = json.loads(line)
                    except Exception:
                        continue
                    t = s.get("t", 0)
                    if t < e0 or t > e1:
                        continue
                    first = t if first is None else min(first, t)
                    last = t if last is None else max(last, t)
                    if s.get("b"):
                        bad_n += 1
                    else:
                        good.append((float(s.get("ra", 0)), float(s.get("dec", 0))))
        except OSError:
            continue
    n_tot = len(good) + bad_n
    out["n"] = n_tot
    if n_tot == 0:
        return out
    out["dither_pct"] = round(100.0 * bad_n / n_tot, 1)
    if first is not None and last is not None and e1 > e0:
        out["cover_pct"] = round(min(100.0, 100.0 * (last - first) / (e1 - e0)), 1)
    if good:
        ra = [a for a, _ in good]
        de = [b for _, b in good]
        # RMS sulla MEDIA della finestra (dev. standard), come ASIAIR/PHD2:
        # l'RMS su zero gonfia il valore quando c'e' deriva nella finestra
        mr = sum(ra) / len(ra)
        md = sum(de) / len(de)
        rr = math.sqrt(sum((x - mr) ** 2 for x in ra) / len(ra))
        rd = math.sqrt(sum((x - md) ** 2 for x in de) / len(de))
        out["rms_ra"] = round(rr, 2)
        out["rms_dec"] = round(rd, 2)
        out["rms_tot"] = round(math.sqrt(rr * rr + rd * rd), 2)
        out["peak"] = round(max(math.hypot(a, b) for a, b in good), 2)
    return out


# --------------------------------------------------------------------------- #
# SQLite
# --------------------------------------------------------------------------- #
SCHEMA = """
CREATE TABLE IF NOT EXISTS frames (
  file TEXT PRIMARY KEY, night_id TEXT, object TEXT, filter TEXT,
  date_obs_utc TEXT, date_obs_local TEXT, exptime REAL,
  gain INTEGER, offset INTEGER, bin INTEGER, ccd_temp REAL, focus_pos INTEGER,
  rms_tot REAL, rms_ra REAL, rms_dec REAL, rms_peak REAL,
  guide_n INTEGER, guide_cover_pct REAL, dither_pct REAL,
  size_mb REAL, path TEXT, pushed INTEGER DEFAULT 0, added_utc TEXT
);
CREATE INDEX IF NOT EXISTS idx_frames_night ON frames(night_id);
CREATE TABLE IF NOT EXISTS sessions (
  night_id TEXT, object TEXT, filter TEXT, exptime REAL,
  n_frames INTEGER, total_s REAL, t_start_local TEXT, t_end_local TEXT,
  rms_tot_avg REAL, rms_ra_avg REAL, rms_dec_avg REAL, rms_max REAL,
  gain INTEGER, bin INTEGER, temp_avg REAL, guide_cover_avg REAL,
  moon_pct REAL, end_cause TEXT, updated_utc TEXT,
  hfr_best REAL, af_n INTEGER,
  PRIMARY KEY (night_id, object, filter, exptime)
);
"""


def open_db(path: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=15)
    db.executescript(SCHEMA)
    # migrazione dei DB gia' esistenti: CREATE TABLE IF NOT EXISTS non aggiunge
    # colonne. Nuove dal 2026-08-12: statistica autofocus della notte.
    have = {r[1] for r in db.execute("PRAGMA table_info(sessions)")}
    for col, tipo in (("hfr_best", "REAL"), ("af_n", "INTEGER")):
        if col not in have:
            db.execute(f"ALTER TABLE sessions ADD COLUMN {col} {tipo}")
    db.commit()
    return db


def focus_stats(sink_dir: str, night_id: str, tz: ZoneInfo) -> dict:
    """Autofocus della NOTTE dal sink scritto dal publisher MQTT:
    {'hfr_best': miglior HFR della notte, 'af_n': quante corse concluse}.
    Il sink e' un JSONL per data UTC con righe {"t": epoch, "hfr": x, "pos": n},
    una per ogni evento AutoFocus 'complete'; la notte si ricava dal timestamp
    con la stessa regola dei frame (ora locale -12h), cosi' una corsa dopo la
    mezzanotte finisce nella notte giusta."""
    out = {"hfr_best": None, "af_n": 0}
    if not sink_dir:
        return out
    base = Path(sink_dir)
    if not base.is_dir():
        return out
    try:
        d0 = datetime.fromisoformat(night_id).date()
    except ValueError:
        return out
    hfrs = []
    # la notte puo' cadere su due date UTC (e su tre con fusi lontani)
    for delta in (-1, 0, 1, 2):
        p = base / f"{(d0 + timedelta(days=delta)).isoformat()}.jsonl"
        if not p.exists():
            continue
        try:
            with open(p) as fh:
                for line in fh:
                    try:
                        s = json.loads(line)
                    except Exception:
                        continue
                    hfr = s.get("hfr")
                    if hfr is None:
                        continue
                    try:
                        t = datetime.fromtimestamp(float(s.get("t", 0)), timezone.utc)
                    except (TypeError, ValueError, OSError):
                        continue
                    if night_id_of(t, tz) != night_id:
                        continue
                    hfrs.append(float(hfr))
        except OSError:
            continue
    if hfrs:
        out["hfr_best"] = round(min(hfrs), 2)
        out["af_n"] = len(hfrs)
    return out


def ingest(cfg: dict) -> dict:
    """Scansiona scan_dir, ingerisce i Light NUOVI in frames, riaggrega sessions.
    Ritorna {'new': n, 'nights': [...]}. Idempotente (PK = nome file)."""
    c = sl_cfg(cfg)
    tz = ZoneInfo(cfg.get("timezone_observatory", "UTC"))
    db = open_db(c["db_file"])
    try:
        known = {r[0] for r in db.execute("SELECT file FROM frames")}
        cutoff = time.time() - float(c.get("backfill_days", 30)) * 86400
        root = Path(c["scan_dir"])
        new, nights = 0, set()
        if root.is_dir():
            for p in sorted(root.rglob("*.fit*")):
                name = p.name
                if name in known or name.startswith("."):
                    continue
                try:
                    st = p.stat()
                except OSError:
                    continue
                if st.st_mtime < cutoff:
                    continue
                try:
                    hdr = read_fits_header(str(p))
                except Exception as e:
                    log.warning("header illeggibile %s: %s", name, e)
                    continue
                if (hdr.get("IMAGETYP") or "").strip().lower() != "light":
                    continue
                try:
                    dt_utc = datetime.fromisoformat(hdr["DATE-OBS"]).replace(tzinfo=timezone.utc)
                except Exception:
                    log.warning("DATE-OBS mancante/invalido in %s", name)
                    continue
                exp = _f(hdr, "EXPTIME", _f(hdr, "EXPOSURE", 0)) or 0
                gs = guide_stats(c["guide_sink_dir"], dt_utc, dt_utc + timedelta(seconds=exp))
                nid = night_id_of(dt_utc, tz)
                db.execute(
                    "INSERT OR IGNORE INTO frames VALUES (?,?,?,?,?,?,?,?,?,?,?,?,"
                    "?,?,?,?,?,?,?,?,?,0,?)",
                    (name, nid, hdr.get("OBJECT", "?"), hdr.get("FILTER", "-"),
                     dt_utc.isoformat(timespec="seconds"),
                     dt_utc.astimezone(tz).isoformat(timespec="seconds"), exp,
                     _i(hdr, "GAIN"), _i(hdr, "OFFSET"), _i(hdr, "XBINNING", 1),
                     _f(hdr, "CCD-TEMP"), _i(hdr, "FOCUSPOS"),
                     gs["rms_tot"], gs["rms_ra"], gs["rms_dec"], gs["peak"],
                     gs["n"], gs["cover_pct"], gs["dither_pct"],
                     round(st.st_size / 1048576, 1), str(p),
                     datetime.now(timezone.utc).isoformat(timespec="seconds")))
                new += 1
                nights.add(nid)
        for nid in nights:
            _aggregate_night(db, nid, tz, c.get("focus_sink_dir", ""))
        db.commit()
        return {"new": new, "nights": sorted(nights)}
    finally:
        db.close()


def _aggregate_night(db: sqlite3.Connection, night_id: str, tz: ZoneInfo,
                     focus_dir: str = "") -> None:
    """(Ri)calcola le righe sessions della notte dai frames (dettaglio = verita').
    end_cause NON viene toccato qui (lo imposta finalize)."""
    rows = db.execute(
        """SELECT object, filter, exptime, COUNT(*), SUM(exptime),
                  MIN(date_obs_local), MAX(date_obs_local),
                  AVG(rms_tot), AVG(rms_ra), AVG(rms_dec), MAX(rms_peak),
                  MAX(gain), MAX(bin), AVG(ccd_temp), AVG(guide_cover_pct)
           FROM frames WHERE night_id=? GROUP BY object, filter, exptime""",
        (night_id,)).fetchall()
    try:
        moon = moon_illum_pct(datetime.fromisoformat(night_id).date())
    except Exception:
        moon = None
    now_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # statistica autofocus della notte (uguale per tutte le righe della notte)
    fs = focus_stats(focus_dir, night_id, tz)
    for (obj, filt, exp, n, tot, t0, t1, rt, rr, rd, rp, gain, b, temp, gcov) in rows:
        t_end = t1
        try:  # fine = inizio ultimo frame + esposizione
            t_end = (datetime.fromisoformat(t1) + timedelta(seconds=exp or 0)
                     ).isoformat(timespec="seconds")
        except Exception:
            pass
        db.execute(
            """INSERT INTO sessions (night_id, object, filter, exptime, n_frames,
                 total_s, t_start_local, t_end_local, rms_tot_avg, rms_ra_avg,
                 rms_dec_avg, rms_max, gain, bin, temp_avg, guide_cover_avg,
                 moon_pct, end_cause, updated_utc, hfr_best, af_n)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?,?)
               ON CONFLICT(night_id, object, filter, exptime) DO UPDATE SET
                 hfr_best=excluded.hfr_best, af_n=excluded.af_n,
                 n_frames=excluded.n_frames, total_s=excluded.total_s,
                 t_start_local=excluded.t_start_local, t_end_local=excluded.t_end_local,
                 rms_tot_avg=excluded.rms_tot_avg, rms_ra_avg=excluded.rms_ra_avg,
                 rms_dec_avg=excluded.rms_dec_avg, rms_max=excluded.rms_max,
                 gain=excluded.gain, bin=excluded.bin, temp_avg=excluded.temp_avg,
                 guide_cover_avg=excluded.guide_cover_avg, moon_pct=excluded.moon_pct,
                 updated_utc=excluded.updated_utc""",
            (night_id, obj, filt, exp, n, tot, t0, t_end,
             _r(rt), _r(rr), _r(rd), _r(rp), gain, b, _r(temp, 1), _r(gcov, 0),
             moon, now_utc, fs["hfr_best"], fs["af_n"] or None))


def _r(v, nd=2):
    return round(v, nd) if isinstance(v, (int, float)) else v


def filters_used(cfg: dict, night_id: str) -> list:
    """Filtri (lettere come nell'header FITS, es. ['H','S']) dei LIGHT
    registrati per la notte. Usato dal flusso flat per abilitare solo gli
    slot flat/dark dei filtri realmente usati in sessione."""
    c = sl_cfg(cfg)
    db = open_db(c["db_file"])
    try:
        rows = db.execute("SELECT DISTINCT filter FROM frames WHERE night_id=?",
                          (night_id,)).fetchall()
    finally:
        db.close()
    return sorted(r[0] for r in rows if r[0])


def filters_gains_used(cfg: dict, night_id: str) -> list:
    """Coppie (filtro, gain) dei LIGHT della notte, es. [('B',0),('L',100)].
    Dal 2026-07-24 il flusso flat fa i flat/dark PER GAIN: stesso filtro con
    due gain (0 e 100) -> due passate. gain NULL nel DB -> 0."""
    c = sl_cfg(cfg)
    db = open_db(c["db_file"])
    try:
        rows = db.execute("SELECT DISTINCT filter, gain FROM frames "
                          "WHERE night_id=?", (night_id,)).fetchall()
    finally:
        db.close()
    return sorted((r[0], int(r[1] or 0)) for r in rows if r[0])


def finalize(cfg: dict, night_id: str = None, cause: str = "") -> int:
    """Imposta la causa di fine sessione sulle righe della notte che non ne
    hanno gia' una. Se night_id manca, usa la notte piu' recente nel DB."""
    c = sl_cfg(cfg)
    db = open_db(c["db_file"])
    try:
        if not night_id:
            r = db.execute("SELECT MAX(night_id) FROM sessions").fetchone()
            night_id = r[0] if r else None
        if not night_id:
            return 0
        cur = db.execute(
            "UPDATE sessions SET end_cause=?, updated_utc=? "
            "WHERE night_id=? AND (end_cause IS NULL OR end_cause='')",
            (cause, datetime.now(timezone.utc).isoformat(timespec="seconds"), night_id))
        db.commit()
        return cur.rowcount
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Dettaglio per-frame: CSV per notte sul NAS
# --------------------------------------------------------------------------- #
def export_detail_csv(cfg: dict, nights=None, db: sqlite3.Connection = None) -> dict:
    """Scrive/riscrive il CSV del dettaglio per le notti indicate (default: le
    notti con frame non ancora esportati). Un file per notte, riscritto per
    intero: idempotente e sempre allineato al DB.

    Dal 2026-08-12 questo SOSTITUISCE la tab 'Dettaglio' del Google Sheet, che
    cresceva di ~1.600 righe al mese rendendo il diario illeggibile.
    """
    c = sl_cfg(cfg)
    out = {"files": 0, "rows": 0, "nights": []}
    if not c.get("detail_csv_dir"):
        return out
    base = Path(c["detail_csv_dir"])
    own, db = (db is None), (db or open_db(c["db_file"]))
    try:
        if nights is None:
            nights = [r[0] for r in db.execute(
                "SELECT DISTINCT night_id FROM frames WHERE pushed=0 ORDER BY 1")]
        if not nights:
            return out
        base.mkdir(parents=True, exist_ok=True)   # NAS non montato -> OSError
        loc = c.get("location_label", "SFRO")
        for nid in nights:
            rows = db.execute(DET_SELECT + " WHERE night_id=? ORDER BY date_obs_utc",
                              (nid,)).fetchall()
            if not rows:
                continue
            p = base / f"{loc}_{nid.replace('-', '')}.csv"
            tmp = p.with_suffix(".tmp")
            with open(tmp, "w", newline="", encoding="utf-8-sig") as fh:
                w = csv.writer(fh, delimiter=";")   # ';' = Excel italiano
                w.writerow(DET_HEADER)
                for r in rows:
                    w.writerow(["" if v is None else v for v in r])
            tmp.replace(p)
            out["files"] += 1
            out["rows"] += len(rows)
            out["nights"].append(nid)
        return out
    finally:
        if own:
            db.close()


# --------------------------------------------------------------------------- #
# Google Sheets (mirror)
# --------------------------------------------------------------------------- #
def _sheet(c):
    import gspread
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_file(
        c["sa_json"], scopes=["https://www.googleapis.com/auth/spreadsheets",
                              "https://www.googleapis.com/auth/drive"])
    return gspread.authorize(creds).open_by_key(c["sheet_id"])


def _ws(sh, title, header, cols):
    try:
        ws = sh.worksheet(title)
    except Exception:
        ws = sh.add_worksheet(title=title, rows=200, cols=cols)
    a1 = ws.get_values("A1:A1")
    if not (a1 and a1[0] and a1[0][0]):
        ws.update(values=[header], range_name="A1")
        try:
            ws.freeze(rows=1)
        except Exception:
            pass
    return ws


def push(cfg: dict) -> dict:
    """Mirror del DB sul diario:
      - dettaglio per-frame  -> CSV per notte sul NAS (non piu' su Sheets)
      - Sessioni + riepiloghi -> Google Sheet, riscritti per intero
    """
    c = sl_cfg(cfg)
    if not c.get("sheet_id"):
        return {"error": "sheet_id non configurato"}
    if not Path(c["sa_json"]).exists():
        return {"error": f"chiave service account assente: {c['sa_json']}"}
    db = open_db(c["db_file"])
    try:
        res = {}
        # --- Dettaglio: CSV per notte (NAS). Se il NAS non c'e' NON si segna
        # pushed=1: si riprova al ciclo dopo, il diario intanto sale lo stesso.
        det_rows = []
        try:
            ex = export_detail_csv(cfg, db=db)
            res["csv_files"] = ex["files"]
            res["csv_rows"] = ex["rows"]
            if ex["nights"]:
                db.execute("UPDATE frames SET pushed=1 WHERE night_id IN (%s)"
                           % ",".join("?" * len(ex["nights"])), ex["nights"])
                db.commit()
        except OSError as e:
            res["csv_error"] = str(e)
            log.warning("CSV dettaglio non scritti (%s): riprovo al prossimo giro", e)
        sh = _sheet(c)
        # --- Dettaglio su Sheets: solo se ancora configurato (legacy) ---
        if c.get("tab_detail"):
            det = _ws(sh, c["tab_detail"], DET_HEADER, len(DET_HEADER))
            det_rows = db.execute(
                DET_SELECT + " WHERE pushed=0 ORDER BY date_obs_utc").fetchall()
            if det_rows:
                det.append_rows([["" if v is None else v for v in r] for r in det_rows],
                                value_input_option="USER_ENTERED")
                db.executemany("UPDATE frames SET pushed=1 WHERE file=?",
                               [(r[0],) for r in det_rows])
                db.commit()
        rows = det_rows
        # --- Sessioni: riscrittura completa ---
        sess = db.execute(
            """SELECT night_id, object, filter, exptime, n_frames, total_s,
                      t_start_local, t_end_local, rms_tot_avg, rms_ra_avg,
                      rms_dec_avg, rms_max, gain, bin, temp_avg, guide_cover_avg,
                      moon_pct, end_cause, updated_utc, hfr_best, af_n
               FROM sessions ORDER BY night_id, object, filter, exptime""").fetchall()
        loc = c.get("location_label", "SFRO")
        ours = []
        for r in sess:
            r = [loc] + list(r)
            r[6] = _hms(r[6])                       # integrazione totale HH:MM:SS
            ours.append(r)
        # Notte, Location
        _rewrite_loc(sh, c["tab_sessions"], SESS_HEADER, ours, loc,
                     lambda r: (str(r[1]), str(r[0])))
        # --- Riepiloghi: le due tab che si leggono davvero per le statistiche ---
        by_obj, by_mon = _summaries(db, loc)
        if c.get("tab_by_object"):
            _rewrite_loc(sh, c["tab_by_object"], BYOBJ_HEADER, by_obj, loc,
                         lambda r: (str(r[1]), str(r[2]), str(r[0])))
        if c.get("tab_by_month"):
            _rewrite_loc(sh, c["tab_by_month"], BYMON_HEADER, by_mon, loc,
                         lambda r: (str(r[1]), str(r[0])))
        res.update({"detail_appended": len(rows), "sessions": len(sess),
                    "by_object": len(by_obj), "by_month": len(by_mon)})
        return res
    finally:
        db.close()


def _rewrite_loc(sh, title, header, ours, loc, sortkey) -> int:
    """Riscrive su una tab SOLO le righe di questa postazione, preservando
    quelle delle altre (il foglio e' condiviso: colonna A = Location)."""
    ws = _ws(sh, title, header, len(header))
    keep = [(r + [""] * len(header))[:len(header)]
            for r in ws.get_values()[1:] if r and r[0] and r[0] != loc]
    mine = [["" if v is None else v for v in r] for r in ours]
    ws.clear()
    ws.update(values=[header] + sorted(keep + mine, key=sortkey), range_name="A1")
    try:
        ws.freeze(rows=1)
    except Exception:
        pass
    return len(mine)


def _summaries(db: sqlite3.Connection, loc: str):
    """Righe delle tab 'Per soggetto' (soggetto x filtro) e 'Per mese'."""
    by_obj = [[loc, obj, filt, nn, n, _hms(tot), round((tot or 0) / 3600, 2),
               _r(exp, 0), gain, _r(rms), p0, p1]
              for (obj, filt, nn, n, tot, rms, exp, gain, p0, p1) in db.execute(
        """SELECT object, filter, COUNT(DISTINCT night_id), COUNT(*), SUM(exptime),
                  AVG(rms_tot), MAX(exptime), MAX(gain), MIN(night_id), MAX(night_id)
           FROM frames GROUP BY object, filter""")]
    # HFR e luna stanno sulle sessioni (una per notte x soggetto x filtro)
    extra = {m: (h, mo) for m, h, mo in db.execute(
        """SELECT substr(night_id,1,7), AVG(hfr_best), AVG(moon_pct)
           FROM sessions GROUP BY 1""")}
    by_mon = []
    for (m, nn, n, tot, rms, nobj) in db.execute(
        """SELECT substr(night_id,1,7), COUNT(DISTINCT night_id), COUNT(*),
                  SUM(exptime), AVG(rms_tot), COUNT(DISTINCT object)
           FROM frames GROUP BY 1"""):
        h, mo = extra.get(m, (None, None))
        by_mon.append([loc, m, nn, n, _hms(tot), round((tot or 0) / 3600, 2),
                       nobj, _r(rms), _r(h), _r(mo, 0)])
    return by_obj, by_mon


def _hms(seconds):
    try:
        s = int(seconds)
        return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"
    except (TypeError, ValueError):
        return seconds


def build_report(cfg: dict) -> dict:
    """Rigenera la dashboard HTML (best-effort: non deve mai far fallire il
    diario). Ritorna il dict di sfro_report.build o {'error': ...}."""
    try:
        import sfro_report
        return sfro_report.build(cfg)
    except Exception as e:
        log.warning("dashboard non rigenerata: %s", e)
        return {"error": str(e)}


def update_and_push(cfg: dict) -> dict:
    """Ingest + push in un colpo (chiamata per-ciclo dall'agente)."""
    r = ingest(cfg)
    if r.get("new"):
        log.info("sessionlog: %d nuovi frame (%s)", r["new"], ",".join(r["nights"]))
    p = push(cfg)
    if p.get("error"):
        raise RuntimeError(p["error"])
    r.update(p)
    if r.get("new"):
        r["report"] = build_report(cfg)
    return r


def migrate_detail(cfg: dict, tab: str, delete: bool = False) -> dict:
    """Una tantum (2026-08-12): riversa TUTTO il dettaglio in CSV per notte e,
    solo se il conteggio torna, elimina la vecchia tab dal Google Sheet."""
    c = sl_cfg(cfg)
    db = open_db(c["db_file"])
    try:
        nights = [r[0] for r in db.execute(
            "SELECT DISTINCT night_id FROM frames ORDER BY 1")]
        n_db = db.execute("SELECT COUNT(*) FROM frames").fetchone()[0]
        try:
            ex = export_detail_csv(cfg, nights=nights, db=db)
        except OSError as e:
            # NAS non montato: mai eliminare la tab se il dettaglio non e' salvo
            return {"error": f"CSV non scritti ({e}): il foglio NON e' stato toccato"}
        db.execute("UPDATE frames SET pushed=1")
        db.commit()
    finally:
        db.close()
    out = {"nights": len(ex["nights"]), "csv_rows": ex["rows"], "db_rows": n_db,
           "files": ex["files"]}
    if ex["rows"] != n_db:
        out["error"] = ("conteggi diversi: NON tocco il foglio "
                        f"(CSV {ex['rows']} vs DB {n_db})")
        return out
    if not delete:
        out["nota"] = "esportazione fatta; per rimuovere la tab usa --delete-tab"
        return out
    sh = _sheet(c)
    try:
        ws = sh.worksheet(tab)
    except Exception as e:
        out["tab"] = f"tab '{tab}' non trovata ({e}): niente da eliminare"
        return out
    n_sheet = len(ws.get_values())
    sh.del_worksheet(ws)
    out["tab"] = f"tab '{tab}' eliminata ({n_sheet - 1} righe)"
    return out


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="SFRO storico sessioni (FITS->SQLite->diario)")
    ap.add_argument("--config", default=str(Path(__file__).parent / "config.yaml"))
    ap.add_argument("--scan", action="store_true", help="ingest dei FITS nuovi")
    ap.add_argument("--push", action="store_true", help="mirror su Google Sheets + CSV")
    ap.add_argument("--csv", action="store_true", help="riscrive i CSV dettaglio (tutte le notti)")
    ap.add_argument("--report", action="store_true", help="rigenera la dashboard HTML")
    ap.add_argument("--migrate-detail", metavar="TAB", nargs="?", const="Dettaglio_SFRO",
                    help="una tantum: dettaglio storico -> CSV (default tab Dettaglio_SFRO)")
    ap.add_argument("--delete-tab", action="store_true",
                    help="con --migrate-detail: elimina la tab dopo l'export verificato")
    ap.add_argument("--finalize", metavar="CAUSA", help="imposta causa fine notte")
    ap.add_argument("--night", default=None, help="night_id per --finalize")
    ap.add_argument("--dump", action="store_true", help="stampa le sessioni dal DB")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    import yaml
    cfg = yaml.safe_load(Path(args.config).read_text())

    if args.scan:
        print(json.dumps(ingest(cfg), ensure_ascii=False))
    if args.csv:
        c = sl_cfg(cfg)
        db = open_db(c["db_file"])
        nights = [r[0] for r in db.execute("SELECT DISTINCT night_id FROM frames ORDER BY 1")]
        db.close()
        print(json.dumps(export_detail_csv(cfg, nights=nights), ensure_ascii=False))
    if args.migrate_detail:
        print(json.dumps(migrate_detail(cfg, args.migrate_detail, args.delete_tab),
                         ensure_ascii=False))
    if args.report:
        print(json.dumps(build_report(cfg), ensure_ascii=False))
    if args.finalize:
        n = finalize(cfg, args.night, args.finalize)
        print(f"finalize: {n} righe aggiornate")
    if args.push:
        print(json.dumps(push(cfg), ensure_ascii=False))
    if args.dump:
        c = sl_cfg(cfg)
        db = open_db(c["db_file"])
        for r in db.execute("SELECT night_id, object, filter, exptime, n_frames, "
                            "total_s, rms_tot_avg, moon_pct, end_cause FROM sessions "
                            "ORDER BY night_id, object, filter, exptime"):
            print(r)
        db.close()


if __name__ == "__main__":
    main()
