#!/usr/bin/env python3
"""Test offline della destinazione SSD con ripiego sul NAS (2026-10-31).
rsync VERO su alberi finti: niente VPN, niente CIFS, niente dischi veri.
Il montaggio dei tre punti (share ASIAIR, NAS, SSD) e' simulato.

  A  SSD ok: i FITS vanno sull'SSD (mirror + vista), il NAS non si tocca
  B  SSD staccato: ripiego sul NAS, avviso UNA volta, registro del ripiego
  C  SSD di ritorno: rientro verificato, file e hardlink tolti dal NAS, lo
     storico sul NAS (fuori registro) intatto, avviso di ritorno
  D  SSD in sola lettura: ripiego, ma cio' che e' gia' sull'SSD non si
     riscarica (--compare-dest)
  E  errori ext4 registrati / SSD pieno: ripiego col motivo giusto
  F  spazio sotto la soglia d'avviso: avviso una volta, poi silenzio
  G  dry-run: nessun avviso, nessun registro, nessun rientro
  H  diario: scan_dir a lista, un frame in due radici conta una volta
"""
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from pathlib import Path as _P
ROOT = _P(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import sfro_agent as A
import sfro_sessionlog as SL

TMP = Path(tempfile.mkdtemp(prefix="syncssd_"))
SRC = TMP / "asiair_sfro"       # finta share ASIAIR (mount_point)
NAS = TMP / "nas"               # finto nas_dest
SSD = TMP / "ssd"               # finto ssd_dest
VAR = TMP / "var"
for d in (NAS, SSD, VAR):
    d.mkdir(parents=True)
SC = {"asiair_ip": "1.2.3.4", "smb_share_name": "TF Images",
      "mount_point": str(SRC), "credentials_file": "/dev/null",
      "nas_dest": str(NAS), "ssd_dest": str(SSD),
      "ssd_min_free_gb": 0, "ssd_warn_free_gb": 0,
      "dest_state_file": str(VAR / "sync_dest.json"),
      "fallback_ledger": str(VAR / "nas_ripiego.json"),
      "rsync_exclude": ["*.jpg"], "rsync_extra": [], "per_run_cap_minutes": 30,
      "recent_days": 7, "lock_file": str(VAR / "sync.lock")}
CFG = {"timezone_observatory": "America/Chicago", "sync_module": SC,
       "sessioni_giornaliere": {"enabled": True, "mode": "hardlink",
                                "dest": "/qualunque/sessioni_giornaliere",
                                "nights_back": 2,
                                "folder_timezone": "Europe/Rome"}}

D = datetime.now(timezone.utc).strftime("%Y%m%d")
SOGG = "NGC 7000"


def light(n):
    return (f"Light_{SOGG}_300.0s_Bin1_2600MM_H_gain100_{D}-0{n}1500_"
            f"12deg_0.0C_000{n}.fit")


FLAT = f"Flat_1.2s_Bin1_2600MM_H_gain100_{D}-140000_12deg_0.1C_0001.fit"


def put(rel, content):
    p = SRC / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)


put(f"ASIAIR/Plan/Light/{SOGG}/{light(1)}", b"L1" * 500)
put(f"ASIAIR/Plan/Light/{SOGG}/{light(2)}", b"L2" * 500)
put(f"ASIAIR/Autorun/Flat/{FLAT}", b"FF" * 300)

# --- stub: montaggi simulati, niente ping, niente mount/umount ------------- #
MOUNTED = {str(SRC): True, str(NAS): True, str(SSD): True}
A.is_mounted = lambda p: MOUNTED.get(str(p), False)
A.ping = lambda h: True
_real_run = A._run


def _run(cmd, timeout=None):
    if cmd[0] in ("mount", "umount"):
        class P:
            returncode, stdout, stderr = 0, "", ""
        return P()
    return _real_run(cmd, timeout=timeout)


A._run = _run
MOUNT_OPTS = {"opts": {"rw"}, "dev": "sdz1"}
A._mount_entry = lambda p: (f"/dev/{MOUNT_OPTS['dev']}", MOUNT_OPTS["opts"])
A.EXT4_SYS = str(TMP / "sysfs")
(TMP / "sysfs" / "sdz1").mkdir(parents=True)
(TMP / "sysfs" / "sdz1" / "errors_count").write_text("0\n")


def mirror(root):
    return Path(root) / "asiair_sfro" / "ASIAIR"


def lp(root, name):
    return mirror(root) / "Plan/Light" / SOGG / D / name


def vista(root, kind, name):
    return Path(root) / "sessioni_giornaliere" / SOGG / D / kind / name


def run(**kw):
    r = A.sync_pass(SC, dry_run=kw.get("dry_run", False), cfg=CFG)
    assert r["ok"], r
    return r


# A — SSD ok ------------------------------------------------------------------
r = run()
assert r["dest"] == "ssd" and r["dest_why"] is None, r
assert lp(SSD, light(1)).is_file() and lp(SSD, light(2)).is_file()
assert (mirror(SSD) / "Autorun/Flat" / D / FLAT).is_file()
assert not mirror(NAS).exists(), "il NAS non va toccato con l'SSD sano"
assert vista(SSD, "light", light(1)).is_file(), "vista non creata sull'SSD"
assert vista(SSD, "flat", FLAT).stat().st_ino == \
    (mirror(SSD) / "Autorun/Flat" / D / FLAT).stat().st_ino, "non e' un hardlink"
assert r["dest_notice"] == "", r["dest_notice"]
assert "sull'SSD" in A.sync_done_text("Rsync", r)
print("A  SSD ok -> mirror e vista sull'SSD, NAS intatto: OK")

# B — SSD staccato --------------------------------------------------------------
put(f"ASIAIR/Plan/Light/{SOGG}/{light(3)}", b"L3" * 500)
MOUNTED[str(SSD)] = False
r = run()
assert r["dest"] == "nas" and "non montato" in r["dest_why"], r
assert "ripiego" in r["dest_notice"] and "non montato" in r["dest_notice"], r
assert lp(NAS, light(3)).is_file(), "file nuovo non salvato sul NAS"
led = json.loads((VAR / "nas_ripiego.json").read_text())
assert any(x.endswith(light(3)) for x in led), led
assert all(x.startswith("asiair_sfro/ASIAIR/") for x in led), led
assert vista(NAS, "light", light(3)).is_file(), "vista non creata sul NAS"
assert "sul NAS (ripiego)" in A.sync_done_text("Rsync", r)
r = run()
assert r["dest"] == "nas" and r["dest_notice"] == "", \
    f"avviso ripetuto: {r['dest_notice']!r}"
print("B  SSD staccato -> ripiego sul NAS, avviso una volta, registro: OK")

# C — SSD di ritorno: rientro ---------------------------------------------------
STORICO = mirror(NAS) / "Plan/Light" / SOGG / "20260901" / "vecchio.fit"
STORICO.parent.mkdir(parents=True)
STORICO.write_bytes(b"storico")             # non nel registro: non si tocca
MOUNTED[str(SSD)] = True
r = run()
assert r["dest"] == "ssd", r
assert "di nuovo disponibile" in r["dest_notice"], r["dest_notice"]
assert "Riportati sull'SSD" in r["dest_notice"], r["dest_notice"]
assert lp(SSD, light(3)).read_bytes() == b"L3" * 500, "contenuto non riportato"
assert not lp(NAS, light(3)).exists(), "file riportato ancora sul NAS"
assert not vista(NAS, "light", light(3)).exists(), "hardlink rimasto sul NAS"
assert vista(SSD, "light", light(3)).is_file(), "vista non rifatta sull'SSD"
assert STORICO.read_bytes() == b"storico", "lo storico sul NAS e' stato toccato"
assert not (VAR / "nas_ripiego.json").exists(), "registro non svuotato"
assert r["rientro"]["errors"] == 0 and r["rientro"]["moved"] >= 1, r["rientro"]
print("C  ritorno dell'SSD -> rientro verificato, NAS ripulito, storico intatto: OK")

# D — sola lettura: ripiego con --compare-dest ------------------------------------
put(f"ASIAIR/Plan/Light/{SOGG}/{light(4)}", b"L4" * 500)
MOUNT_OPTS["opts"] = {"ro"}
r = run()
assert r["dest"] == "nas" and "sola lettura" in r["dest_why"], r
assert lp(NAS, light(4)).is_file(), "file nuovo non salvato sul NAS"
for n in (1, 2, 3):
    assert not lp(NAS, light(n)).exists(), f"light {n} riscaricato sul NAS"
MOUNT_OPTS["opts"] = {"rw"}
r = run()                                   # rientro del solo light 4
assert r["dest"] == "ssd" and lp(SSD, light(4)).is_file(), r
assert not lp(NAS, light(4)).exists()
print("D  SSD in sola lettura -> ripiego senza riscaricare cio' che c'e': OK")

# E — errori ext4 / SSD pieno -------------------------------------------------------
(TMP / "sysfs" / "sdz1" / "errors_count").write_text("3\n")
why, _ = A.ssd_problem(SC)
assert why and "3 errori" in why, why
(TMP / "sysfs" / "sdz1" / "errors_count").write_text("0\n")
SC["ssd_min_free_gb"] = 1e9
why, free = A.ssd_problem(SC)
assert why and "quasi pieno" in why and free is not None, why
SC["ssd_min_free_gb"] = 0
assert A.ssd_problem(SC)[0] is None
print("E  errori ext4 / SSD pieno -> motivo del ripiego corretto: OK")

# F — avviso di spazio, una volta sola -------------------------------------------
SC["ssd_warn_free_gb"] = 1e9
r = run()
assert "restano" in r["dest_notice"], r["dest_notice"]
r = run()
assert r["dest_notice"] == "", f"avviso spazio ripetuto: {r['dest_notice']!r}"
SC["ssd_warn_free_gb"] = 0
print("F  spazio sotto soglia -> avviso una volta: OK")

# G — dry-run: niente effetti collaterali ----------------------------------------
stato = (VAR / "sync_dest.json").read_text()
MOUNTED[str(SSD)] = False
r = A.sync_pass(SC, dry_run=True, cfg=CFG)
assert r["dest"] == "nas" and not r["dest_notice"], r
assert (VAR / "sync_dest.json").read_text() == stato, "stato cambiato in dry-run"
assert not (VAR / "nas_ripiego.json").exists(), "registro scritto in dry-run"
MOUNTED[str(SSD)] = True
print("G  dry-run -> nessun avviso, stato e registro invariati: OK")

# H — diario: due radici, un frame una volta ------------------------------------------
def fits(path, name):
    cards = [f"{'SIMPLE':8}= {'T':>20}", f"{'IMAGETYP':8}= 'Light   '",
             f"{'DATE-OBS':8}= '{D[:4]}-{D[4:6]}-{D[6:]}T02:15:00'",
             f"{'EXPTIME':8}= {300.0:>20}", f"{'OBJECT':8}= '{SOGG}'",
             f"{'FILTER':8}= 'H       '", "END"]
    raw = "".join(c.ljust(80) for c in cards).ljust(2880).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)


R1, R2 = TMP / "diario_ssd", TMP / "diario_nas"
fits(R1 / "a.fit", "a")
fits(R2 / "a.fit", "a")                   # stesso nome nell'altra radice
fits(R2 / "b.fit", "b")
cfg_sl = dict(CFG, session_log={"enabled": True, "db_file": str(VAR / "s.db"),
                                "scan_dir": [str(R1), str(R2)],
                                "guide_sink_dir": str(VAR / "g"),
                                "focus_sink_dir": "", "backfill_days": 30})
r = SL.ingest(cfg_sl)
assert r["new"] == 2, r
assert SL.ingest(cfg_sl)["new"] == 0
print("H  diario su due radici -> ogni frame una volta: OK")

shutil.rmtree(TMP)
print("TUTTO OK")
