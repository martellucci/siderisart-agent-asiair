#!/usr/bin/env python3
"""
SFRO — Dashboard statistiche: SQLite -> pagina HTML autonoma.

Divisione dei compiti (decisa con l'utente 2026-08-12):
  - SQLite            = fonte di verita' (sfro_sessionlog.py)
  - Google Sheets     = diario LEGGIBILE (Sessioni + riepiloghi, poche righe)
  - CSV per notte     = dettaglio per-frame, sul NAS
  - QUESTA pagina     = dove si GUARDANO le statistiche

Il file prodotto e' autonomo (niente CDN, niente JS esterno): un solo .html
servito da Apache in /var/www/html/sfro/statistiche (http://192.0.2.10/sfro/
statistiche), raggiungibile in VPN. In /var/www/html/sfro resta un redirect.

Uso:  python3 sfro_report.py --config config.yaml [--out /tmp/x.html]
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import math
import sqlite3
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger("sfro-report")

DEFAULTS = {
    "enabled": True,
    "out_dir": "/var/www/html/sfro/statistiche",
    "url": "",                  # URL mostrato su Telegram (vuoto = non mostrato)
    "detail_nights": 10,        # notti con la tabella per-frame in fondo
    "chart_nights": 60,         # notti mostrate nei grafici temporali
    "rms_bad": 1.0,             # RMS (") oltre il quale il frame e' "da rivedere"
    "rms_peak_gate": 25.0,      # picco (") oltre il quale il frame NON racconta la
                                # guida ma un movimento della montatura: escluso
                                # dagli aggregati. 0 = nessuna esclusione.
    "targets": {},              # ore obiettivo per soggetto, es. {"NGC 7000": 60}
    "title": "SFRO — Diario osservativo",
    "logo": "logo.png",         # marchio in testata; relativo alla dir dello script.
                                # Incorporato nella pagina (resta un file solo).
    "brand": "Sideris Art · Fine Art Astrophotography",
}

# Colori per filtro: L/R/G/B e la banda stretta SHO.
FILT_COL = {"L": "#cdd6e4", "R": "#e0503a", "G": "#4bbf73", "B": "#4a8ff0",
            "H": "#d94f7d", "S": "#f2b134", "O": "#35b6d4"}
FILT_ORDER = ["L", "R", "G", "B", "H", "S", "O"]
COL_FALLBACK = "#8a93a3"


def rp_cfg(cfg: dict) -> dict:
    out = dict(DEFAULTS)
    out.update(cfg.get("report", {}) or {})
    return out


def _col(f):
    return FILT_COL.get((f or "").strip().upper()[:1], COL_FALLBACK)


def _fkey(f):
    u = (f or "-").strip().upper()[:1]
    return (FILT_ORDER.index(u) if u in FILT_ORDER else 99, u)


def hm(seconds) -> str:
    """Secondi -> '12h 34m' (o '34m' sotto l'ora)."""
    try:
        s = int(seconds or 0)
    except (TypeError, ValueError):
        return "-"
    h, m = s // 3600, s % 3600 // 60
    return f"{h}h {m:02d}m" if h else f"{m}m"


def _n(v, nd=2, dash="-"):
    return dash if v is None else f"{float(v):.{nd}f}"


def esc(s) -> str:
    return html.escape("" if s is None else str(s))


# --------------------------------------------------------------------------- #
# Ore di buio nautico per notte (per la resa: ore riprese / ore disponibili)
# --------------------------------------------------------------------------- #
def dark_hours_fn(cfg: dict):
    """Ritorna f(night_id) -> ore di buio nautico, o None se astral non c'e'."""
    try:
        from astral import LocationInfo
        from astral.sun import dawn, dusk
    except Exception:
        return lambda nid: None
    loc = cfg.get("location", {}) or {}
    tz = ZoneInfo(cfg.get("timezone_observatory", "UTC"))
    depr = float((cfg.get("nautical", {}) or {}).get("depression", 12))
    obs = LocationInfo(loc.get("name", "SFRO"), "", str(tz),
                       float(loc.get("latitude", 0)), float(loc.get("longitude", 0)))
    cache = {}

    def f(nid):
        if nid in cache:
            return cache[nid]
        try:
            d = datetime.fromisoformat(nid).date()
            t0 = dusk(obs.observer, date=d, tzinfo=tz, depression=depr)
            t1 = dawn(obs.observer, date=d + timedelta(days=1), tzinfo=tz, depression=depr)
            v = round((t1 - t0).total_seconds() / 3600.0, 2)
        except Exception:      # notti bianche o data storta
            v = None
        cache[nid] = v
        return v
    return f


# --------------------------------------------------------------------------- #
# RMS: aggregati robusti (deciso con l'utente 2026-08-13)
# --------------------------------------------------------------------------- #
# Due accorgimenti, per lo stesso motivo: pochi frame patologici falsavano tutte
# le medie (con 18 frame su 2449 la media globale passava da 0,75" a 1,05" e
# NGC 7635 risultava a 2,44" invece di 0,70").
#
#   1. GATE SUL PICCO. Se durante la posa il picco supera 'rms_peak_gate' la
#      stella di guida era fuori campo: non e' guida scarsa, e' la montatura che
#      si e' mossa (dither che l'ASIAIR smette di dichiarare a meta' evento,
#      slew, parcheggio a fine piano). Il frame esce dagli aggregati e viene
#      contato a parte. Nei dati le due popolazioni sono separate da un vuoto
#      netto: nessun frame fra 20" e 30" di picco.
#   2. MEDIANA al posto della media. Immune per costruzione a code del genere,
#      senza dipendere dalla bonta' della soglia.
#
# I frame esclusi NON spariscono: restano nell'istogramma, nel conteggio
# "scatti da rivedere", nel dettaglio per frame e in un elenco dedicato.
def _med(vals):
    v = [x for x in vals if x is not None]
    return round(statistics.median(v), 2) if v else None


def rms_index(db, gate: float) -> dict:
    """Aggregati dell'RMS per notte / soggetto / soggetto+filtro / mese.

    Ritorna anche l'elenco dei frame scartati dal gate, cosi' la pagina puo'
    dichiarare cosa ha tolto invece di limarlo in silenzio.
    """
    idx = {"night": {}, "objf": {}, "obj": {}, "month": {},
           "excl": [], "n_valid": 0, "gate": gate}
    by = {"night": {}, "objf": {}, "obj": {}, "month": {}}
    peak = {}
    for nid, loc, obj, filt, rt, rp in db.execute(
            """SELECT night_id, date_obs_local, object, filter, rms_tot, rms_peak
               FROM frames WHERE rms_tot IS NOT NULL"""):
        if gate and (rp or 0) > gate:
            idx["excl"].append((nid, loc, obj, filt, rt, rp))
            continue
        idx["n_valid"] += 1
        by["night"].setdefault(nid, []).append(rt)
        by["obj"].setdefault(obj, []).append(rt)
        by["objf"].setdefault((obj, filt), []).append(rt)
        by["month"].setdefault((nid or "")[:7], []).append(rt)
        if rp is not None:
            peak[nid] = rp if nid not in peak else max(peak[nid], rp)
    for k in by:
        idx[k] = {kk: _med(vv) for kk, vv in by[k].items()}
    idx["peak"] = peak
    idx["excl"].sort(key=lambda x: -(x[5] or 0))
    idx["n_excl"] = len(idx["excl"])
    # quante notti/soggetti sono stati toccati: serve alla nota in pagina
    idx["excl_nights"] = sorted({x[0] for x in idx["excl"]})
    return idx


# --------------------------------------------------------------------------- #
# Estrazione dati
# --------------------------------------------------------------------------- #
def collect(cfg: dict) -> dict:
    import sfro_sessionlog as SL
    c = SL.sl_cfg(cfg)
    r = rp_cfg(cfg)
    db = sqlite3.connect(c["db_file"], timeout=15)
    try:
        d = {}
        d["tot"] = db.execute(
            "SELECT COUNT(*), SUM(exptime), COUNT(DISTINCT night_id), "
            "COUNT(DISTINCT object), MIN(night_id), MAX(night_id), SUM(size_mb) "
            "FROM frames").fetchone()
        # --- per notte ---
        rows = db.execute(
            """SELECT night_id, COUNT(*), SUM(exptime), AVG(rms_tot), MAX(rms_peak),
                      AVG(ccd_temp), MIN(date_obs_local), MAX(date_obs_local),
                      COUNT(DISTINCT object)
               FROM frames GROUP BY night_id ORDER BY night_id""").fetchall()
        sess = {}
        for nid, obj, filt, cause, moon, hfr, afn in db.execute(
                """SELECT night_id, object, filter, end_cause, moon_pct, hfr_best, af_n
                   FROM sessions"""):
            s = sess.setdefault(nid, {"obj": set(), "filt": set(), "cause": None,
                                      "moon": None, "hfr": None, "af": 0})
            s["obj"].add(obj)
            s["filt"].add(filt)
            s["cause"] = s["cause"] or cause
            s["moon"] = s["moon"] if moon is None else moon
            if hfr is not None:
                s["hfr"] = hfr if s["hfr"] is None else min(s["hfr"], hfr)
            s["af"] = max(s["af"], afn or 0)
        # RMS: mediana sui frame validi (vedi rms_index) invece di AVG/MAX grezzi
        rx = rms_index(db, float(r.get("rms_peak_gate", 25.0) or 0))
        d["rms_idx"] = rx
        dark = dark_hours_fn(cfg)
        nights = []
        for (nid, n, tot, rms, peak, temp, t0, t1, nobj) in rows:
            s = sess.get(nid, {})
            dh = dark(nid)
            rms = rx["night"].get(nid)
            peak = rx["peak"].get(nid)
            nights.append({
                "id": nid, "n": n, "tot": tot or 0, "rms": rms, "peak": peak,
                "excl": sum(1 for x in rx["excl"] if x[0] == nid),
                "temp": temp, "t0": (t0 or "")[11:16], "t1": (t1 or "")[11:16],
                "obj": sorted(s.get("obj") or []) or ["?"],
                "filt": sorted(s.get("filt") or [], key=_fkey),
                "cause": s.get("cause") or "", "moon": s.get("moon"),
                "hfr": s.get("hfr"), "af": s.get("af") or 0, "dark_h": dh,
                "eff": (round(100.0 * (tot or 0) / 3600.0 / dh, 0)
                        if dh else None),
            })
        # ore per notte SPEZZATE per filtro (barre impilate del grafico notti)
        per_nf = {}
        for nid, filt, tot in db.execute(
                "SELECT night_id, filter, SUM(exptime) FROM frames "
                "GROUP BY night_id, filter"):
            per_nf.setdefault(nid, {})[filt or "-"] = tot or 0
        for n in nights:
            n["per_filt"] = per_nf.get(n["id"], {})
        d["nights"] = nights
        # --- per soggetto x filtro (l'RMS arriva dall'indice, non da AVG) ---
        d["proj"] = [
            (obj, filt, n, tot, nn, rx["objf"].get((obj, filt)), exp, gain, n0, n1)
            for (obj, filt, n, tot, nn, _avg, exp, gain, n0, n1) in db.execute(
                """SELECT object, filter, COUNT(*), SUM(exptime),
                          COUNT(DISTINCT night_id), AVG(rms_tot), MAX(exptime),
                          MAX(gain), MIN(night_id), MAX(night_id)
                   FROM frames GROUP BY object, filter""")]
        # --- per mese ---
        d["months"] = [
            (m, nn, fr, tot, rx["month"].get(m), no)
            for (m, nn, fr, tot, _avg, no) in db.execute(
                """SELECT substr(night_id,1,7) AS m, COUNT(DISTINCT night_id),
                          COUNT(*), SUM(exptime), AVG(rms_tot),
                          COUNT(DISTINCT object)
                   FROM frames GROUP BY m ORDER BY m""")]
        # --- istogramma RMS ---
        d["rms_all"] = [x[0] for x in db.execute(
            "SELECT rms_tot FROM frames WHERE rms_tot IS NOT NULL")]
        bad = float(r.get("rms_bad", 1.0))
        d["bad_n"] = sum(1 for x in d["rms_all"] if x > bad)
        d["bad_thr"] = bad
        # --- dettaglio ultime N notti ---
        last = [x["id"] for x in nights][-int(r.get("detail_nights", 10)):]
        d["detail"] = {}
        for nid in last:
            d["detail"][nid] = db.execute(
                """SELECT date_obs_local, object, filter, exptime, rms_tot, rms_ra,
                          rms_dec, rms_peak, guide_cover_pct, dither_pct, ccd_temp,
                          focus_pos, gain, file
                   FROM frames WHERE night_id=? ORDER BY date_obs_utc""",
                (nid,)).fetchall()
        d["csv_dir"] = c.get("detail_csv_dir", "")
        d["loc"] = c.get("location_label", "SFRO")
        return d
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Grafici SVG (generati a mano: la pagina non carica nulla dall'esterno)
# --------------------------------------------------------------------------- #
def svg_nights(nights, limit) -> str:
    """Ore riprese per notte; dietro, in grigio, le ore di buio disponibili."""
    ns = nights[-limit:]
    if not ns:
        return "<p class='vuoto'>Nessuna notte registrata.</p>"
    W, H, PL, PB, PT = 1000, 250, 44, 54, 10
    maxv = max([n["tot"] / 3600.0 for n in ns] +
               [n["dark_h"] or 0 for n in ns] + [1])
    maxv = math.ceil(maxv)
    bw = (W - PL - 10) / len(ns)
    plot = H - PB - PT
    # una etichetta ogni 'step' notti: sotto le ~22 stanno tutte
    tick = _ticks(len(ns), max(1, math.ceil(len(ns) / 22)))

    def y(v):
        return PT + plot - (v / maxv) * plot

    # NIENTE preserveAspectRatio='none': deformava anche il testo delle date
    p = [f"<svg viewBox='0 0 {W} {H}' class='chart'>"]
    for g in range(0, maxv + 1, max(1, maxv // 5)):
        p.append(f"<line x1='{PL}' y1='{y(g):.1f}' x2='{W-10}' y2='{y(g):.1f}' "
                 f"class='grid'/><text x='{PL-8}' y='{y(g)+4:.1f}' "
                 f"class='ax ar'>{g}h</text>")
    for i, n in enumerate(ns):
        x = PL + i * bw
        w = max(1.5, bw * 0.72)
        if n["dark_h"]:
            p.append(f"<rect x='{x:.1f}' y='{y(n['dark_h']):.1f}' width='{w:.1f}' "
                     f"height='{(y(0)-y(n['dark_h'])):.1f}' class='dark'/>")
        # barra impilata per filtro: si legge a colpo d'occhio se la notte era
        # LRGB o banda stretta
        base = 0.0
        tip = (f"{esc(n['id'])} — {hm(n['tot'])}"
               + (f" su {n['dark_h']}h di buio ({n['eff']:.0f}%)" if n["eff"] else "")
               + f"\n{esc(', '.join(n['obj']))}")
        for filt in sorted(n["per_filt"], key=_fkey):
            v = n["per_filt"][filt] / 3600.0
            p.append(f"<rect x='{x:.1f}' y='{y(base+v):.1f}' width='{w:.1f}' "
                     f"height='{max(1,(y(base)-y(base+v))):.1f}' fill='{_col(filt)}' "
                     f"opacity='.9'><title>{tip}\n{esc(filt)}: {hm(n['per_filt'][filt])}"
                     f"</title></rect>")
            base += v
        # etichette: giorno/mese inclinate di 45°, ancorate alla fine (l'angolo
        # in basso a destra cade sotto la barra), diradate se le notti sono tante
        if i in tick:
            cx, cy = x + w / 2, H - PB + 15
            p.append(f"<text x='{cx:.1f}' y='{cy}' class='ax ar' "
                     f"transform='rotate(-45 {cx:.1f} {cy})'>{esc(_dm(n['id']))}</text>")
    # anno/mese di riferimento sotto l'asse, che le etichette non lo portano
    p.append(f"<text x='{PL}' y='{H-4}' class='ax'>"
             f"{esc(_mese(ns[0]['id']))} → {esc(_mese(ns[-1]['id']))}</text>")
    p.append("</svg>")
    return "".join(p)


MESI = ["gen", "feb", "mar", "apr", "mag", "giu",
        "lug", "ago", "set", "ott", "nov", "dic"]


def _ticks(n: int, step: int) -> set:
    """Indici da etichettare: uno ogni 'step' piu' l'ultimo, che pero' sostituisce
    il precedente se gli finirebbe addosso."""
    idx = list(range(0, n, step))
    if idx and idx[-1] != n - 1:
        if (n - 1) - idx[-1] < step * 0.7:
            idx[-1] = n - 1
        else:
            idx.append(n - 1)
    return set(idx)


def _dm(nid) -> str:
    """'2026-08-11' -> '11/08' (la notte e' quella della SERA)."""
    try:
        y, m, d = nid.split("-")
        return f"{d}/{m}"
    except (AttributeError, ValueError):
        return str(nid)


def _mese(nid) -> str:
    """'2026-08-11' -> 'ago 2026'."""
    try:
        y, m, _ = nid.split("-")
        return f"{MESI[int(m)-1]} {y}"
    except (AttributeError, ValueError, IndexError):
        return str(nid)


def svg_projects(proj, targets) -> str:
    """Barre orizzontali impilate per soggetto, spezzate per filtro.
    Ogni segmento porta scatti e ore (richiesta utente 2026-08-12)."""
    agg = {}
    for (obj, filt, n, tot, nn, rms, exp, gain, n0, n1) in proj:
        per = agg.setdefault(obj, {}).setdefault(filt or "-", [0, 0])
        per[0] += tot or 0
        per[1] += n or 0
    if not agg:
        return "<p class='vuoto'>Nessun soggetto.</p>"

    def tot_s(per):
        return sum(v[0] for v in per.values())

    order = sorted(agg.items(), key=lambda kv: -tot_s(kv[1]))
    W, RH, PL = 1000, 40, 210
    H = len(order) * RH + 26
    maxv = max(tot_s(v) for _, v in order)
    maxv = max(maxv, *[float(targets.get(o, 0)) * 3600 for o, _ in order]) if targets else maxv
    sc = (W - PL - 130) / (maxv or 1)
    p = [f"<svg viewBox='0 0 {W} {H}' class='chart'>"]
    for i, (obj, per) in enumerate(order):
        yy = i * RH + 8
        tot, nfr = tot_s(per), sum(v[1] for v in per.values())
        p.append(f"<text x='{PL-10}' y='{yy+17}' class='lbl ar'>{esc(obj[:28])}</text>")
        x = PL
        for filt in sorted(per, key=_fkey):
            sec, n = per[filt]
            w = sec * sc
            p.append(f"<rect x='{x:.1f}' y='{yy}' width='{max(0.6,w):.1f}' height='22' "
                     f"fill='{_col(filt)}' opacity='.92'><title>{esc(obj)} {esc(filt)}: "
                     f"{n} scatti · {hm(sec)}</title></rect>")
            if w > 70:      # ci sta tutto: filtro, scatti e ore
                p.append(f"<text x='{x+w/2:.1f}' y='{yy+15}' class='inb'>"
                         f"{esc(filt)} · {n} · {hm(sec)}</text>")
            elif w > 26:
                p.append(f"<text x='{x+w/2:.1f}' y='{yy+15}' class='inb'>{esc(filt)}</text>")
            x += w
        p.append(f"<text x='{x+8:.1f}' y='{yy+16}' class='lbl'>{nfr} scatti · {hm(tot)}</text>")
        tg = float(targets.get(obj, 0)) * 3600 if targets else 0
        if tg:
            tx = PL + tg * sc
            p.append(f"<line x1='{tx:.1f}' y1='{yy-3}' x2='{tx:.1f}' y2='{yy+25}' "
                     f"class='target'><title>obiettivo {targets[obj]}h</title></line>")
    p.append("</svg>")
    return "".join(p)


def svg_series(nights, key, limit, color, unit="", lo=None, hi=None) -> str:
    """Spezzata di un indicatore per notte (RMS, HFR, temperatura...)."""
    ns = [n for n in nights[-limit:] if n.get(key) is not None]
    if len(ns) < 2:
        return "<p class='vuoto'>Dati insufficienti.</p>"
    W, H, PL, PB, PT = 1000, 190, 44, 34, 12
    vals = [float(n[key]) for n in ns]
    # Una sola notte disastrosa (guida impazzita) schiaccia tutto il grafico:
    # si taglia la scala poco sopra il 90° percentile e i valori fuori scala
    # si segnano a fondo scala in rosso, col valore vero nel tooltip.
    srt = sorted(vals)
    cap = max(srt[int(0.9 * (len(srt) - 1))] * 1.6, (hi or 0) * 1.8, srt[0] * 1.2)
    out = [v for v in vals if v > cap]
    vmax = max([min(v, cap) for v in vals] + ([hi] if hi else []))
    vmin = min(vals + ([lo] if lo else []))
    span = (vmax - vmin) or 1
    vmax, vmin = vmax + span * .12, max(0, vmin - span * .12)
    span = (vmax - vmin) or 1
    plot, iw = H - PB - PT, W - PL - 14
    dx = iw / (len(ns) - 1)
    tick = _ticks(len(ns), max(1, math.ceil(len(ns) / 12)))

    def y(v):
        return PT + plot - (min(float(v), vmax) - vmin) / span * plot
    p = [f"<svg viewBox='0 0 {W} {H}' class='chart'>"]
    for g in range(5):
        v = vmin + span * g / 4
        p.append(f"<line x1='{PL}' y1='{y(v):.1f}' x2='{W-14}' y2='{y(v):.1f}' "
                 f"class='grid'/><text x='{PL-8}' y='{y(v)+4:.1f}' class='ax ar'>"
                 f"{v:.2f}</text>")
    if hi:
        p.append(f"<line x1='{PL}' y1='{y(hi):.1f}' x2='{W-14}' y2='{y(hi):.1f}' "
                 f"class='soglia'/>")
    pts = " ".join(f"{PL+i*dx:.1f},{y(n[key]):.1f}" for i, n in enumerate(ns))
    p.append(f"<polyline points='{pts}' fill='none' stroke='{color}' stroke-width='2'/>")
    for i, n in enumerate(ns):
        v = float(n[key])
        fuori = v > vmax
        p.append(f"<circle cx='{PL+i*dx:.1f}' cy='{y(v):.1f}' r='{4.2 if fuori else 3.2}' "
                 f"fill='{'#e0503a' if fuori else color}'><title>{esc(n['id'])}: "
                 f"{v:.2f}{unit}{' (fuori scala)' if fuori else ''}</title></circle>")
        if i in tick:
            p.append(f"<text x='{PL+i*dx:.1f}' y='{H-PB+18}' class='ax am'>"
                     f"{esc(_dm(n['id']))}</text>")
    if out:
        p.append(f"<text x='{W-16}' y='{PT+12}' class='ax ar'>"
                 f"{len(out)} notte/i fuori scala (max {max(out):.1f}{unit})</text>")
    p.append("</svg>")
    return "".join(p)


def svg_hist(vals, thr) -> str:
    """Istogramma dell'RMS dei singoli frame: la coda a destra e' lo scarto."""
    vals = [v for v in vals if v is not None]
    if len(vals) < 10:
        return "<p class='vuoto'>Dati di guida insufficienti.</p>"
    top = max(min(max(vals), thr * 3), thr * 1.5)
    nb = 30
    step = top / nb
    bins = [0] * (nb + 1)
    for v in vals:
        bins[min(nb, int(v / step))] += 1
    W, H, PL, PB, PT = 1000, 190, 44, 34, 12
    plot, iw = H - PB - PT, W - PL - 14
    bw = iw / len(bins)
    mx = max(bins) or 1
    p = [f"<svg viewBox='0 0 {W} {H}' class='chart'>"]
    for i, b in enumerate(bins):
        h = plot * b / mx
        x = PL + i * bw
        col = "#e0503a" if i * step >= thr else "#4bbf73"
        p.append(f"<rect x='{x:.1f}' y='{PT+plot-h:.1f}' width='{bw*.86:.1f}' "
                 f"height='{max(0.5,h):.1f}' fill='{col}' opacity='.85'>"
                 f"<title>{i*step:.2f}–{(i+1)*step:.2f}\": {b} frame</title></rect>")
        if i % 5 == 0:
            p.append(f"<text x='{x:.1f}' y='{H-PB+18}' class='ax am'>{i*step:.1f}</text>")
    tx = PL + (thr / step) * bw
    p.append(f"<line x1='{tx:.1f}' y1='{PT}' x2='{tx:.1f}' y2='{PT+plot}' class='soglia'/>"
             f"<text x='{tx+6:.1f}' y='{PT+12}' class='ax'>soglia {thr}\"</text></svg>")
    return "".join(p)


# --------------------------------------------------------------------------- #
# Pagina
# --------------------------------------------------------------------------- #
CSS = """
/* Palette Sideris Art, la stessa del Deep Sky Planner: blu notte + oro.
   I font del planner (EB Garamond, DM Mono) arrivano da Google: qui la pagina
   deve restare autonoma e raggiungibile in VPN, quindi si dichiarano lo stesso
   ma con ripieghi locali di famiglia compatibile (Georgia, monospace).
   Gli accenti dato (blu/verde/ambra/rosso) sono la versione schiarita di quelli
   del planner: li' vivono su tabella chiara, qui su blu notte. */
:root{
--bg:#071535;--pan:#0a1c3e;--pan2:#0e2348;--pan3:#12294f;
--tx:#e8d9b0;--mut:rgba(245,230,184,.65);--mut2:rgba(245,230,184,.38);
--bd:rgba(245,230,184,.15);--bd2:rgba(245,230,184,.28);
--gold:#f5e6b8;--gold-dark:#c9a84c;
--acc:#6f9ee8;--ok:#4bbf73;--warn:#f2b134;--bad:#e0503a;
--sans:'EB Garamond',Georgia,'Times New Roman',serif;
--mono:'DM Mono',ui-monospace,'SF Mono',Menlo,Consolas,monospace}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);
font:16px/1.55 var(--sans);-webkit-font-smoothing:antialiased}
/* largo quanto lo schermo (le tabelle hanno 14 colonne: a 1180px si tagliavano) */
.wrap{max-width:min(1760px,97vw);margin:0 auto;padding:0 18px 60px}
/* ── testata: logo + marchio, come il planner ── */
.mast{display:flex;align-items:center;gap:16px;flex-wrap:wrap;
padding:20px 0 16px;border-bottom:1px solid var(--bd);margin-bottom:24px}
.mast img{width:62px;height:62px;flex-shrink:0;object-fit:contain;border-radius:8px}
.brand{font-family:var(--mono);font-size:9px;letter-spacing:2.5px;color:var(--mut2);
text-transform:uppercase;margin-bottom:3px}
h1{font-size:clamp(19px,3.4vw,26px);font-weight:500;color:var(--gold);
letter-spacing:.3px;line-height:1.1;margin:0}
.sub{font-family:var(--mono);font-size:10px;letter-spacing:1.4px;color:var(--mut2);
margin-top:4px;text-transform:uppercase}
h2{font-size:19px;font-weight:500;color:var(--gold);margin:36px 0 12px;
padding-bottom:8px;border-bottom:1px solid var(--bd);letter-spacing:.2px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(158px,1fr));gap:12px}
.kpi{background:var(--pan);border:1px solid var(--bd);border-radius:10px;padding:14px 16px}
.kpi .v{font-family:var(--mono);font-size:26px;font-weight:500;color:var(--gold);
letter-spacing:-.5px;line-height:1.2}
.kpi .k{font-family:var(--mono);color:var(--mut2);font-size:9px;
text-transform:uppercase;letter-spacing:1.6px;margin-bottom:5px}
.kpi .n{color:var(--mut);font-size:13px;margin-top:3px}
.card{background:var(--pan);border:1px solid var(--bd);border-radius:10px;
padding:14px;overflow-x:auto}
.due{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:820px){.due{grid-template-columns:1fr}}
.chart{width:100%;height:auto;display:block;min-width:560px}
.grid{stroke:rgba(245,230,184,.10);stroke-width:1}
.dark{fill:var(--pan3)}
.soglia{stroke:var(--bad);stroke-width:1.4;stroke-dasharray:5 4}
.target{stroke:var(--gold-dark);stroke-width:2}
.ax{fill:var(--mut2);font-size:11px;font-family:var(--mono)}
.am{text-anchor:middle}.ar{text-anchor:end}
.lbl{fill:var(--tx);font-size:12.5px}
.inb{fill:#071535;font-size:11px;font-weight:700;text-anchor:middle;
font-family:var(--mono)}
table{border-collapse:collapse;width:100%;font-size:14px;min-width:560px}
th,td{padding:8px 10px;text-align:right;border-bottom:1px solid var(--bd);
white-space:nowrap}
/* i numeri in mono: incolonnati si leggono, e richiamano i valori del planner */
td{font-family:var(--mono);font-size:13px}
td.l{font-family:var(--sans);font-size:14.5px}
th{position:sticky;top:0;background:var(--pan2);color:var(--mut2);font-weight:400;
font-family:var(--mono);font-size:9px;text-transform:uppercase;letter-spacing:1.3px;
cursor:pointer;border-bottom:1px solid var(--bd2)}
th:first-child,td:first-child,.l{text-align:left}
tbody tr:hover{background:var(--pan2)}
.tag{display:inline-block;padding:2px 8px;border-radius:20px;font-size:10px;
font-family:var(--mono);letter-spacing:.4px;border:1px solid var(--bd);color:var(--mut)}
.f{display:inline-block;width:19px;text-align:center;border-radius:4px;
font-size:11px;font-weight:700;color:#071535;margin-right:2px;font-family:var(--mono)}
.bar{height:6px;border-radius:3px;background:var(--pan3);overflow:hidden;min-width:52px}
.bar>i{display:block;height:100%}
.vuoto{color:var(--mut2);font-size:13px;margin:8px 2px}
details{margin:9px 0}
summary{cursor:pointer;color:var(--gold-dark);font-size:14px;padding:5px 0}
summary:hover{color:var(--gold)}
.scroll{max-height:460px;overflow:auto}
footer{color:var(--mut2);font-size:12px;margin-top:36px;border-top:1px solid var(--bd);
padding-top:14px}
.leg{color:var(--mut);font-size:13px;margin:6px 2px 0}
"""

JS = """
document.querySelectorAll('table.sort th').forEach(function(th,i){
  th.onclick=function(){
    var tb=th.closest('table').tBodies[0],
        rs=[].slice.call(tb.rows),
        d=th.dataset.d=(th.dataset.d==='1'?'':'1');
    rs.sort(function(a,b){
      var x=a.cells[i].dataset.v!==undefined?a.cells[i].dataset.v:a.cells[i].textContent,
          y=b.cells[i].dataset.v!==undefined?b.cells[i].dataset.v:b.cells[i].textContent,
          nx=parseFloat(x),ny=parseFloat(y),r;
      r=(!isNaN(nx)&&!isNaN(ny))?nx-ny:(''+x).localeCompare(''+y);
      return d?-r:r;});
    rs.forEach(function(r){tb.appendChild(r);});};});
"""


def _fbadge(filt):
    return (f"<span class='f' style='background:{_col(filt)}' "
            f"title='filtro {esc(filt)}'>{esc((filt or '-')[:1])}</span>")


def _logo_uri(path: str) -> str:
    """Il logo va incorporato: la pagina deve restare un file solo, servito in
    VPN senza dipendenze. Se manca, la testata semplicemente non lo mostra."""
    if not path:
        return ""
    p = Path(path)
    if not p.is_absolute():
        p = Path(__file__).parent / p
    try:
        import base64
        kind = "svg+xml" if p.suffix.lower() == ".svg" else p.suffix.lower().lstrip(".")
        return (f"data:image/{kind or 'png'};base64,"
                + base64.b64encode(p.read_bytes()).decode("ascii"))
    except OSError:
        log.warning("logo non leggibile: %s", p)
        return ""


def _excl_mark(n) -> str:
    """Asterisco accanto alle notti che hanno frame fuori dalla mediana."""
    if not n:
        return ""
    return (f"<span class='ax' title='{n} frame esclus{'o' if n == 1 else 'i'} "
            f"dalla mediana'>&nbsp;*</span>")


def _excl_box(rx: dict) -> str:
    """Elenco dei frame tenuti fuori dalle mediane: l'esclusione va dichiarata,
    altrimenti e' solo un numero che migliora da solo."""
    if not rx.get("gate"):
        return ("<div class='leg' style='margin-top:10px'>Nessuna esclusione "
                "attiva: le mediane usano tutti i frame.</div>")
    n = rx["n_excl"]
    testa = (f"<b>{n}</b> frame esclusi dalle mediane su {n + rx['n_valid']} "
             f"(picco di guida oltre {rx['gate']:.0f}\")")
    if not n:
        return f"<div class='leg' style='margin-top:10px'>{testa}.</div>"
    p = [f"<details style='margin-top:10px'><summary>{testa}</summary>",
         "<div class='leg'>Picchi di questa entità non sono guida scarsa: la "
         "stella era fuori campo perché la montatura si stava muovendo — dither "
         "che l'ASIAIR smette di dichiarare a metà evento, slew, o parcheggio a "
         "fine piano. Restano nell'istogramma, nel conteggio “scatti da "
         "rivedere” e nel dettaglio per frame.</div>",
         "<div class='card scroll'><table class='sort'><thead><tr><th>Notte</th>"
         "<th>Ora</th><th class='l'>Soggetto</th><th class='l'>Filtro</th>"
         "<th>RMS</th><th>Picco</th></tr></thead><tbody>"]
    for (nid, loc, obj, filt, rt, rp) in rx["excl"]:
        p.append(f"<tr><td class='l'>{esc(nid)}</td><td>{esc((loc or '')[11:16])}</td>"
                 f"<td class='l'>{esc(obj)}</td><td class='l'>{_fbadge(filt)}</td>"
                 f"<td style='color:var(--bad)'>{_n(rt)}</td><td>{_n(rp)}</td></tr>")
    p.append("</tbody></table></div></details>")
    return "".join(p)


def render(cfg: dict, d: dict) -> str:
    r = rp_cfg(cfg)
    tz_srv = ZoneInfo(cfg.get("timezone", "Europe/Rome")) if cfg.get("timezone") \
        else ZoneInfo("Europe/Rome")
    nfr, tot_s, nnight, nobj, n0, n1, mb = d["tot"]
    nights = d["nights"]
    targets = {str(k): float(v) for k, v in (r.get("targets") or {}).items()}
    last30 = [n for n in nights if n["id"] >= (datetime.now().date() - timedelta(days=30)).isoformat()]
    effs = [n["eff"] for n in nights if n["eff"] is not None]
    o = []
    a = o.append

    a(f"<!doctype html><html lang='it'><head><meta charset='utf-8'>"
      f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
      f"<meta name='theme-color' content='#071535'>"
      f"<title>{esc(r.get('title'))}</title><style>{CSS}</style></head><body><div class='wrap'>")
    logo = _logo_uri(r.get("logo", ""))
    a("<div class='mast'>"
      + (f"<img src='{logo}' alt='Sideris Art'>" if logo else "")
      + f"<div><div class='brand'>{esc(r.get('brand',''))}</div>"
      f"<h1>{esc(r.get('title'))}</h1>"
      f"<div class='sub'>Postazione {esc(d['loc'])} · dal {esc(n0)} al {esc(n1)} · "
      f"aggiornato {datetime.now(tz_srv).strftime('%d/%m/%Y %H:%M')}</div></div></div>")

    # ---------------- KPI ----------------
    a("<div class='kpis'>")
    a(f"<div class='kpi'><div class='k'>Integrazione</div><div class='v'>"
      f"{(tot_s or 0)/3600:.0f}h</div><div class='n'>{hm(tot_s)} in totale</div></div>")
    a(f"<div class='kpi'><div class='k'>Notti utili</div><div class='v'>{nnight}</div>"
      f"<div class='n'>{nobj} soggetti ripresi</div></div>")
    a(f"<div class='kpi'><div class='k'>Ultimi 30 giorni</div><div class='v'>"
      f"{sum(n['tot'] for n in last30)/3600:.0f}h</div><div class='n'>"
      f"in {len(last30)} notti</div></div>")
    a(f"<div class='kpi'><div class='k'>Scatti</div><div class='v'>{nfr}</div>"
      f"<div class='n'>{(mb or 0)/1024:.0f} GB di light</div></div>")
    if effs:
        a(f"<div class='kpi'><div class='k'>Resa media</div><div class='v'>"
          f"{sum(effs)/len(effs):.0f}%</div><div class='n'>del buio nautico "
          f"disponibile</div></div>")
    if d["rms_all"]:
        pc = 100.0 * d["bad_n"] / len(d["rms_all"])
        a(f"<div class='kpi'><div class='k'>Scatti da rivedere</div><div class='v'>"
          f"{pc:.0f}%</div><div class='n'>{d['bad_n']} con RMS &gt; "
          f"{d['bad_thr']}\"</div></div>")
    a("</div>")

    # ---------------- progetti ----------------
    a("<h2>Progetti — ore per soggetto e filtro</h2><div class='card'>")
    a(svg_projects(d["proj"], targets))
    a("</div>")
    prj = {}
    for (obj, filt, n, tot, nn, rms, exp, gain, p0, p1) in d["proj"]:
        p = prj.setdefault(obj, {"n": 0, "tot": 0, "nights": set(), "filt": {},
                                 "rms": [], "p0": p1, "p1": p1})
        p["n"] += n
        p["tot"] += tot or 0
        f = p["filt"].setdefault(filt or "-", [0, 0])    # [secondi, scatti]
        f[0] += tot or 0
        f[1] += n or 0
        if rms is not None:
            p["rms"].append(rms)
        p["p0"] = min(p["p0"], p0)
        p["p1"] = max(p["p1"], p1)
    a("<div class='card'><table class='sort'><thead><tr><th>Soggetto</th>"
      "<th class='l'>Filtri — scatti e ore</th><th>Notti</th><th>Scatti</th>"
      "<th>Integrazione</th>"
      "<th title='mediana dei frame validi'>RMS med.</th>"
      "<th>Obiettivo</th><th class='l'>Prima</th>"
      "<th class='l'>Ultima</th></tr></thead><tbody>")
    for obj, p in sorted(prj.items(), key=lambda kv: -kv[1]["tot"]):
        badges = "".join(
            _fbadge(f) + f"<span class='ax'> {v[1]} · {hm(v[0])}</span>&nbsp; "
            for f, v in sorted(p["filt"].items(), key=lambda kv: _fkey(kv[0])))
        tg = targets.get(obj)
        if tg:
            pcv = min(100, 100 * p["tot"] / 3600 / tg)
            col = "var(--ok)" if pcv >= 100 else "var(--acc)"
            goal = (f"<div class='bar'><i style='width:{pcv:.0f}%;background:{col}'></i>"
                    f"</div><span class='ax'>{p['tot']/3600:.0f}/{tg:.0f}h</span>")
        else:
            goal = "<span class='ax'>—</span>"
        # mediana calcolata sui frame del soggetto, non media delle mediane per
        # filtro: quest'ultima peserebbe allo stesso modo un filtro da 10 pose
        # e uno da 400
        rms = d["rms_idx"]["obj"].get(obj)
        a(f"<tr><td class='l'>{esc(obj)}</td><td class='l'>{badges}</td>"
          f"<td>{len([n for n in nights if obj in n['obj']])}</td><td>{p['n']}</td>"
          f"<td data-v='{p['tot']}'>{hm(p['tot'])}</td><td>{_n(rms)}</td>"
          f"<td data-v='{p['tot']}'>{goal}</td><td class='l'>{esc(p['p0'])}</td>"
          f"<td class='l'>{esc(p['p1'])}</td></tr>")
    a("</tbody></table></div>")

    # ---------------- notti ----------------
    a("<h2>Rendimento delle notti</h2><div class='card'>")
    a(svg_nights(nights, int(r.get("chart_nights", 60))))
    a("<div class='leg'>Barra colorata = ore effettivamente riprese; barra grigia "
      "dietro = ore di buio nautico disponibili quella notte.</div></div>")

    # ---------------- qualita' ----------------
    a("<h2>Qualità — guida e fuoco</h2><div class='due'>")
    a("<div class='card'><div class='leg'>RMS <b>mediano</b> di guida per notte (\")</div>"
      + svg_series(nights, "rms", int(r.get("chart_nights", 60)), "#6f9ee8", "\"",
                   hi=d["bad_thr"]) + "</div>")
    a("<div class='card'><div class='leg'>HFR migliore per notte</div>"
      + svg_series(nights, "hfr", int(r.get("chart_nights", 60)), "#f2b134") + "</div>")
    a("</div><div class='card' style='margin-top:14px'>"
      "<div class='leg'>Distribuzione dell'RMS dei singoli frame: la coda rossa è "
      "il materiale candidato allo scarto. Qui i frame ci sono <b>tutti</b>, "
      "compresi quelli esclusi dalle mediane.</div>"
      + svg_hist(d["rms_all"], d["bad_thr"]) + "</div>")
    a(_excl_box(d["rms_idx"]))

    # ---------------- tabella notti ----------------
    a("<h2>Diario delle notti</h2><div class='card scroll'><table class='sort'><thead><tr>"
      "<th>Notte</th><th class='l'>Soggetti</th><th class='l'>Filtri</th>"
      "<th>Da–a</th><th>Scatti</th><th>Integrazione</th><th>Resa</th>"
      "<th title='mediana dei frame validi'>RMS med.</th>"
      "<th title='picco massimo fra i frame validi'>Picco</th>"
      "<th>HFR</th><th>AF</th><th>Temp</th><th>Luna</th>"
      "<th class='l'>Fine</th></tr></thead><tbody>")
    for n in reversed(nights):
        eff = (f"<div class='bar'><i style='width:{min(100,n['eff']):.0f}%;"
               f"background:{'var(--ok)' if n['eff']>=70 else 'var(--warn)' if n['eff']>=40 else 'var(--bad)'}'>"
               f"</i></div><span class='ax'>{n['eff']:.0f}%</span>") if n["eff"] is not None else "—"
        a(f"<tr><td class='l'>{esc(n['id'])}</td>"
          f"<td class='l'>{esc(', '.join(n['obj'])[:40])}</td>"
          f"<td class='l'>{''.join(_fbadge(f) for f in n['filt'])}</td>"
          f"<td>{esc(n['t0'])}–{esc(n['t1'])}</td><td>{n['n']}</td>"
          f"<td data-v='{n['tot']}'>{hm(n['tot'])}</td>"
          f"<td data-v='{n['eff'] or 0}'>{eff}</td>"
          f"<td>{_n(n['rms'])}{_excl_mark(n['excl'])}</td>"
          f"<td>{_n(n['peak'])}</td><td>{_n(n['hfr'])}</td><td>{n['af'] or '—'}</td>"
          f"<td>{_n(n['temp'],1)}</td><td>{_n(n['moon'],0)}</td>"
          f"<td class='l'><span class='tag'>{esc(n['cause'] or '—')}</span></td></tr>")
    a("</tbody></table></div>")

    # ---------------- per mese ----------------
    a("<h2>Riepilogo mensile</h2><div class='card'><table class='sort'><thead><tr>"
      "<th>Mese</th><th>Notti</th><th>Scatti</th><th>Integrazione</th>"
      "<th>Media/notte</th><th title='mediana dei frame validi'>RMS med.</th>"
      "<th>Soggetti</th></tr></thead><tbody>")
    for (m, nn, fr, tot, rms, no) in reversed(d["months"]):
        a(f"<tr><td class='l'>{esc(m)}</td><td>{nn}</td><td>{fr}</td>"
          f"<td data-v='{tot or 0}'>{hm(tot)}</td>"
          f"<td data-v='{(tot or 0)/(nn or 1)}'>{hm((tot or 0)/(nn or 1))}</td>"
          f"<td>{_n(rms)}</td><td>{no}</td></tr>")
    a("</tbody></table></div>")

    # ---------------- dettaglio per-frame ----------------
    a(f"<h2>Dettaglio per frame — ultime {len(d['detail'])} notti</h2>")
    if d["csv_dir"]:
        a(f"<div class='leg'>Lo storico completo, notte per notte, è su NAS in "
          f"<code>{esc(d['csv_dir'])}</code> (un CSV per notte).</div>")
    for nid in reversed(list(d["detail"])):
        rows = d["detail"][nid]
        a(f"<details><summary>{esc(nid)} — {len(rows)} frame</summary>"
          "<div class='card scroll'><table class='sort'><thead><tr><th>Ora</th>"
          "<th class='l'>Soggetto</th><th class='l'>Filtro</th><th>Esp</th>"
          "<th>RMS</th><th>AR</th><th>DEC</th><th>Picco</th><th>Cop.</th>"
          "<th>Dither</th><th>Temp</th><th>Fuoco</th><th>Gain</th>"
          "<th class='l'>File</th></tr></thead><tbody>")
        for (t, obj, filt, exp, rt, rr, rd, rp, cov, dit, temp, foc, gain, f) in rows:
            hot = " style='color:var(--bad)'" if (rt or 0) > d["bad_thr"] else ""
            a(f"<tr><td>{esc((t or '')[11:19])}</td><td class='l'>{esc(obj)}</td>"
              f"<td class='l'>{_fbadge(filt)}</td><td>{_n(exp,0)}s</td>"
              f"<td{hot}>{_n(rt)}</td><td>{_n(rr)}</td><td>{_n(rd)}</td>"
              f"<td>{_n(rp)}</td><td>{_n(cov,0)}%</td><td>{_n(dit,0)}%</td>"
              f"<td>{_n(temp,1)}</td><td>{foc if foc is not None else '—'}</td>"
              f"<td>{gain if gain is not None else '—'}</td>"
              f"<td class='l ax'>{esc(f)}</td></tr>")
        a("</tbody></table></div></details>")

    a(f"<footer>SFRO — generato da sfro_report.py · fonte: SQLite "
      f"({esc(cfg.get('session_log',{}).get('db_file',''))}) · "
      f"solo LIGHT (flat/dark/bias esclusi dal diario).</footer>")
    a(f"</div><script>{JS}</script></body></html>")
    return "".join(o)


def build(cfg: dict, out: str = None) -> dict:
    """Genera la pagina. Ritorna {'file':..., 'nights':n, 'frames':n}."""
    r = rp_cfg(cfg)
    if not r.get("enabled") and not out:
        return {"skipped": True}
    d = collect(cfg)
    htm = render(cfg, d)
    path = Path(out) if out else Path(r["out_dir"]) / "index.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")          # scrittura atomica: Apache non
    tmp.write_text(htm, encoding="utf-8")   # serve mai una pagina a meta'
    tmp.replace(path)
    return {"file": str(path), "nights": len(d["nights"]), "frames": d["tot"][0],
            "kb": round(len(htm) / 1024)}


def main():
    ap = argparse.ArgumentParser(description="SFRO dashboard statistiche (SQLite -> HTML)")
    ap.add_argument("--config", default=str(Path(__file__).parent / "config.yaml"))
    ap.add_argument("--out", default=None, help="file di destinazione (default: report.out_dir)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    import yaml
    cfg = yaml.safe_load(Path(args.config).read_text())
    print(json.dumps(build(cfg, args.out), ensure_ascii=False))


if __name__ == "__main__":
    main()
