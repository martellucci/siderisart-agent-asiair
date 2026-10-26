#!/usr/bin/env python3
"""
SFRO — Sessioni giornaliere: vista per SOGGETTO x NOTTE dei FITS gia' sul NAS.

Layout prodotto (richiesta utente 2026-09-17):

    <dest>/<soggetto>/<aaaammgg notte>/light/   i light di quel soggetto
                                       /flat/   flat dei SUOI filtri+gain
                                                (e del SUO angolo, 2026-10-24)
                                       /dark/   dark dei SUOI filtri+gain

I flat e i dark della notte sono UNICI ma servono a piu' soggetti: compaiono
sotto ognuno dei soggetti che quel filtro+gain l'ha usato quella notte.
Di default con HARDLINK (mode: hardlink), quindi il file e' sempre lo stesso e
non occupa spazio due volte; il NAS non accetta symlink (CIFS, verificato
2026-09-17), quindi l'unica alternativa e' la copia vera (mode: copy).

Questa e' una SECONDA VISTA: l'albero originale asiair_sfro/ASIAIR/... non si
tocca mai.  E' la destinazione incrementale di rsync (spostare i file la'
dentro farebbe riscaricare tutto via VPN) ed e' anche lo scan_dir del diario.

DATA ITALIANA, non quella dell'osservatorio (richiesta utente 2026-09-18): la
cartella porta il giorno che era in ITALIA quando il frame e' stato ripreso.
La sessione che a questo sito comincia la sera del 17/9 in Italia e' gia' il 18/9,
e la cartella si chiama 20260918.

Non serve lo scarto di 12h della convenzione del diario: una notte texana
INTERA — dal tramonto ai flat del mattino dopo — cade dentro un solo giorno
italiano, perche' in Italia sono all'incirca le 03:00-16:00.  Il cambio di data
italiano cade alle 17:00-18:00 di questo sito, cioe' nel pomeriggio, a notte
finita e prima di quella dopo.  I nomi ASIAIR portano invece la data UTC, che
non coincide con nessuna delle due: si converte sempre dall'istante.

Filtro e gain si leggono dal NOME FILE: per i flat e i dark e' l'unica fonte
possibile, perche' nel database del diario ci sono solo i light.

ROTATORE CAA (richiesta utente 2026-10-24): i flat si fanno all'angolo di
ripresa di ogni soggetto, quindi un soggetto riceve solo i flat del SUO angolo
(`_12deg_` nel nome, angolo in cielo). Il confronto e' a meno di 180°: al
meridian flip l'angolo in cielo salta di 180° ma il rotatore non si muove, e
12° e 192° sono la stessa posizione. Tolleranza angle_tolerance_deg. I dark
non ruotano: restano abbinati per filtro+gain. Nomi senza angolo -> nessun
filtro sull'angolo (come prima).
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger("sfro-sessioni")

DEFAULTS = {
    "enabled": False,
    "dest": "/mnt/astronomia/sessioni_giornaliere",
    "mode": "hardlink",        # hardlink (niente spazio) | copy (file doppi)
    "nights_back": 2,          # notti ricontrollate a ogni passata
    # fuso che DA' IL NOME alla cartella-data: quello di casa, non quello
    # dell'osservatorio (2026-09-18). Non c'entra col night_id del diario.
    "folder_timezone": "Europe/Rome",
    "angle_tolerance_deg": 2,
}

LIGHT_REL = "Plan/Light"
CALIB_RELS = (("Autorun/Flat", "flat"), ("Autorun/Dark", "dark"))
SUBDIRS = {"light": "light", "flat": "flat", "dark": "dark"}

# Nome ASIAIR: Light_<sogg>_300.0s_Bin1_2600MM_H_gain100_20260917-021500_...fit
# Il gruppo centrale e' <camera> oppure <camera>_<filtro>: con una camera a
# colori il token filtro non c'e' e i frame si accoppiano solo tra loro.
FNAME = re.compile(r"_Bin\d+_(.+?)_gain(\d+)_(\d{8})-(\d{6})_")
DEG = re.compile(r"_(-?\d+(?:\.\d+)?)deg_")


def sg_cfg(cfg: dict) -> dict:
    out = dict(DEFAULTS)
    out.update(cfg.get("sessioni_giornaliere", {}) or {})
    return out


def src_root(cfg: dict) -> Path:
    """Radice ASIAIR sul NAS, ricavata dalla config del sync (una sola fonte di
    verita'): <nas_dest>/<nome del mount point>/ASIAIR."""
    sc = cfg.get("sync_module", {}) or {}
    return (Path(sc.get("nas_dest", "/mnt/astronomia"))
            / Path(sc.get("mount_point", "/mnt/asiair_sfro")).name / "ASIAIR")


def parse_name(name: str) -> dict | None:
    """(filtro, gain, istante UTC, angolo) dal nome file, None se non
    riconoscibile. angolo = None se il nome non lo porta."""
    m = FNAME.search(name)
    if not m:
        return None
    mid = m.group(1).split("_")
    try:
        dt = datetime.strptime(m.group(3) + m.group(4), "%Y%m%d%H%M%S")
    except ValueError:
        return None
    a = DEG.search(name)
    return {"filter": mid[1] if len(mid) > 1 else None,
            "gain": int(m.group(2)),
            "utc": dt.replace(tzinfo=timezone.utc),
            "angle": float(a.group(1)) if a else None}


def same_angle(a, b, tol: float) -> bool:
    """Stesso angolo del rotatore a meno di 180° (meridian flip). Un angolo
    mancante non esclude nulla."""
    if a is None or b is None:
        return True
    d = (a - b) % 180
    return min(d, 180 - d) <= tol


def night_of(dt_utc: datetime, tz: ZoneInfo) -> str:
    """Cartella-data (aaaammgg) = giorno ITALIANO dell'istante di ripresa.
    NON e' il night_id del diario (quello resta sul fuso dell'osservatorio):
    qui conta il giorno di chi guarda l'archivio da casa."""
    return dt_utc.astimezone(tz).strftime("%Y%m%d")


def utc_dirs(nights) -> set:
    """Cartelle-data (UTC) da aprire per quelle date: N-1, N e N+1.
    E' un SOVRAINSIEME volutamente largo — costa un paio di listati, per lo
    piu' di cartelle inesistenti — perche' a decidere davvero e' il confronto
    per singolo file in scan(), non questo elenco."""
    out = set()
    for n in nights:
        d = datetime.strptime(n, "%Y%m%d")
        for k in (-1, 0, 1):
            out.add((d + timedelta(days=k)).strftime("%Y%m%d"))
    return out


def scan(root: Path, nights: set, tz: ZoneInfo) -> tuple:
    """(light, calib) delle notti richieste.
    light: [(path, soggetto, notte, filtro, gain, angolo)]
    calib: [(path, tipo, notte, filtro, gain, angolo)]"""
    lights, calib, skipped = [], [], 0
    dirs = utc_dirs(nights)

    def frames(d: Path):
        nonlocal skipped
        try:
            names = sorted(os.listdir(d))
        except OSError as e:
            log.warning("cartella illeggibile (%s): %s", d, e)
            return
        for n in names:
            if n.startswith("."):
                continue
            p = d / n
            if not p.is_file():
                continue
            info = parse_name(n)
            if not info:
                skipped += 1
                log.warning("nome non riconoscibile, saltato: %s", p)
                continue
            yield p, info

    lroot = root / LIGHT_REL
    if lroot.is_dir():
        try:
            subjects = sorted(p for p in lroot.iterdir() if p.is_dir())
        except OSError as e:
            log.warning("elenco soggetti illeggibile (%s): %s", lroot, e)
            subjects = []
        for sub in subjects:
            for d in dirs:
                if not (sub / d).is_dir():
                    continue
                for p, i in frames(sub / d):
                    nid = night_of(i["utc"], tz)
                    if nid in nights:
                        lights.append((p, sub.name, nid, i["filter"],
                                       i["gain"], i["angle"]))

    for rel, kind in CALIB_RELS:
        for d in dirs:
            cd = root / rel / d
            if not cd.is_dir():
                continue
            for p, i in frames(cd):
                nid = night_of(i["utc"], tz)
                if nid in nights:
                    calib.append((p, kind, nid, i["filter"], i["gain"],
                                  i["angle"]))
    return lights, calib, skipped


def _link(src: Path, dst: Path, mode: str, res: dict) -> None:
    """Collega (o copia) src in dst. Gia' presente = niente da fare: la
    passata e' incrementale e gira dopo OGNI sync."""
    if dst.exists():
        return
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if mode == "copy":
            shutil.copy2(src, dst)
        else:
            os.link(src, dst)
    except OSError as e:
        # Mai fatale: un file che non si collega non deve fermare il flusso
        # flat ne' lo spegnimento del rig.
        res["errors"] += 1
        if res["errors"] <= 3:
            log.warning("collegamento fallito (%s -> %s): %s", src, dst, e)
        return
    res[res["_kind"]] += 1


def aggiorna(cfg: dict, nights=None, dry_run: bool = False) -> dict:
    """Aggiorna la vista per le notti indicate (default: le ultime
    nights_back, oggi compreso). Non cancella e non sposta MAI nulla."""
    res = {"light": 0, "flat": 0, "dark": 0, "errors": 0, "skipped": 0,
           "nights": [], "soggetti": 0}
    c = sg_cfg(cfg)
    if not c.get("enabled"):
        return res
    tz = ZoneInfo(c.get("folder_timezone", "Europe/Rome"))
    if nights:
        nights = {str(n) for n in nights}
    else:
        oggi = datetime.now(tz).date()
        nights = {(oggi - timedelta(days=k)).strftime("%Y%m%d")
                  for k in range(int(c.get("nights_back", 2)))}
    res["nights"] = sorted(nights)
    root, dest = src_root(cfg), Path(c["dest"])
    if not root.is_dir():
        log.warning("radice FITS assente (%s): vista non aggiornata", root)
        return res
    lights, calib, res["skipped"] = scan(root, nights, tz)

    # filtri+gain usati da OGNI soggetto in OGNI notte: le calibrazioni
    # seguono il singolo soggetto, non la notte intera (scelta utente
    # 2026-09-17: un soggetto ripreso solo in H non si porta i flat L)
    # e gli ANGOLI di ognuno (rotatore, 2026-10-24): {(filtro, gain): {angoli}}
    usati = {}
    for _p, sogg, nid, filt, gain, ang in lights:
        usati.setdefault((sogg, nid), {}).setdefault((filt, gain), set()).add(ang)
    tol = float(c.get("angle_tolerance_deg", 2))

    def serve(kind, ang, angoli):
        return kind != "flat" or any(same_angle(ang, a, tol) for a in angoli)
    res["soggetti"] = len(usati)
    if dry_run:
        for (sogg, nid), pairs in sorted(usati.items()):
            n_l = sum(1 for x in lights if x[1] == sogg and x[2] == nid)
            det = " ".join(f"{f or '-'}/g{g}" for f, g in sorted(
                pairs, key=lambda x: (x[0] or "", x[1])))
            n_c = sum(1 for _p, k, n, f, g, a in calib
                      if n == nid and (f, g) in pairs
                      and serve(k, a, pairs[(f, g)]))
            print(f"{sogg} / {nid}: {n_l} light, {n_c} calib  [{det}]")
        return res

    for p, sogg, nid, _f, _g, _a in lights:
        res["_kind"] = "light"
        _link(p, dest / sogg / nid / SUBDIRS["light"] / p.name, c["mode"], res)
    for p, kind, nid, filt, gain, ang in calib:
        for (sogg, n), pairs in usati.items():
            if (n == nid and (filt, gain) in pairs
                    and serve(kind, ang, pairs[(filt, gain)])):
                res["_kind"] = kind
                _link(p, dest / sogg / nid / SUBDIRS[kind] / p.name,
                      c["mode"], res)
    res.pop("_kind", None)
    return res


def riepilogo(res: dict) -> str:
    """Riga per Telegram, vuota se non c'e' stato niente di nuovo."""
    n = res.get("light", 0) + res.get("flat", 0) + res.get("dark", 0)
    if not n:
        return ""
    s = (f"🗂 Sessioni giornaliere: {res['light']} light, "
         f"{res['flat'] + res['dark']} calib collegati")
    if res.get("errors"):
        s += f" ({res['errors']} non riusciti)"
    return s + "."


def main() -> int:
    import yaml
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="/opt/sfro-agent/config.yaml")
    ap.add_argument("--notte", action="append", metavar="AAAAMMGG",
                    help="notte da (ri)collegare; ripetibile")
    ap.add_argument("--dry-run", action="store_true",
                    help="elenca soggetti e conteggi senza toccare il NAS")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    with open(a.config) as fh:
        cfg = yaml.safe_load(fh)
    cfg.setdefault("sessioni_giornaliere", {})["enabled"] = True
    r = aggiorna(cfg, a.notte, a.dry_run)
    print(f"notti: {' '.join(r['nights'])} · soggetti: {r['soggetti']} · "
          f"light {r['light']} · flat {r['flat']} · dark {r['dark']} · "
          f"saltati {r['skipped']} · errori {r['errors']}")
    return 1 if r["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
