#!/usr/bin/env python3
"""Test offline dello smistamento per data del sync ASIAIR->NAS.
Usa rsync VERO su un albero finto (nessuna VPN, nessun CIFS)."""
import os
import shutil
import sys
import tempfile
from pathlib import Path

from pathlib import Path as _P
ROOT = _P(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import sfro_agent as A

TMP = Path(tempfile.mkdtemp(prefix="synclayout_"))
SRC = TMP / "asiair_sfro"      # finta share ASIAIR (mount_point)
NAS = TMP / "nas"              # finto nas_dest
SC = {"asiair_ip": "1.2.3.4", "smb_share_name": "TF Images",
      "mount_point": str(SRC), "credentials_file": "/dev/null",
      "nas_dest": str(NAS), "rsync_exclude": ["*.jpg"], "rsync_extra": [],
      "per_run_cap_minutes": 30}
DEST = NAS / "asiair_sfro" / "ASIAIR"
TARGET = "Dark Scorpion Nebula"   # con spazi: caso reale


def touch(rel, content="x"):
    p = SRC / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


def build():
    for d in ("ASIAIR/Autorun/Flat", "ASIAIR/Autorun/Dark",
              f"ASIAIR/Plan/Light/{TARGET}", "ASIAIR/Preview/M 42", "ASIAIR/log"):
        (SRC / d).mkdir(parents=True, exist_ok=True)
    touch("ASIAIR/Autorun/Flat/Flat_15.0s_Bin1_2600MM_R_gain0_20260725-133801_15deg_0.0C_0001.fit")
    touch("ASIAIR/Autorun/Flat/Flat_15.0s_Bin1_2600MM_R_gain0_20260725-133830_15deg_0.0C_0002.fit")
    touch("ASIAIR/Autorun/Flat/Flat_7.0s_Bin1_2600MM_B_gain0_20260726-132543_15deg_0.0C_0001.fit")
    touch("ASIAIR/Autorun/Flat/Flat_7.0s_Bin1_2600MM_B_gain0_20260726-132550_15deg_0.0C_0002.jpg")
    touch("ASIAIR/Autorun/Dark/Dark_15.0s_Bin1_2600MM_R_gain0_20260725-134500_15deg_0.0C_0001.fit")
    touch(f"ASIAIR/Plan/Light/{TARGET}/Light_{TARGET}_120.0s_Bin1_2600MM_B_gain0_20260725-093935_12deg_0.2C_0001.fit")
    touch(f"ASIAIR/Plan/Light/{TARGET}/Light_{TARGET}_120.0s_Bin1_2600MM_B_gain0_20260725-094136_12deg_0.0C_0002.fit")
    touch(f"ASIAIR/Plan/Light/{TARGET}/Light_{TARGET}_300.0s_Bin1_2600MM_R_gain100_20260726-050000_12deg_0.0C_0001.fit")
    touch("ASIAIR/Preview/M 42/anteprima.fit")
    touch("ASIAIR/log/asiair.log")
    touch("mdb_9.log")


# --- stub: niente ping, niente mount/umount CIFS -------------------------- #
A.ping = lambda host: True
_seen = {"mp": 0}


def fake_is_mounted(path):
    if path == str(SRC):          # 1a chiamata: "gia' montato" (salta mount)
        _seen["mp"] += 1          # dalla 2a: "smontato" (salta umount)
        return _seen["mp"] == 1
    return True                   # nas_dest


A.is_mounted = fake_is_mounted


def run():
    _seen["mp"] = 0
    return A.sync_pass(SC, dry_run=False)


def ck(cond, msg):
    print(("  OK   " if cond else "  FAIL ") + msg)
    if not cond:
        raise SystemExit(1)


def files(rel):
    p = DEST / rel
    return sorted(x.name for x in p.iterdir() if x.is_file()) if p.is_dir() else []


build()

print("N1 primo sync: struttura per data")
r = run()
ck(r["ok"], f"ok=True (error={r['error']})")
ck(len(files("Autorun/Flat/20260725")) == 2, "Flat/20260725: 2 file")
ck(len(files("Autorun/Flat/20260726")) == 1, "Flat/20260726: 1 file (il .jpg escluso)")
ck(files("Autorun/Flat") == [], "nessun file sciolto nella radice Flat")
ck(len(files("Autorun/Dark/20260725")) == 1, "Dark/20260725: 1 file")
ck(len(files(f"Plan/Light/{TARGET}/20260725")) == 2, "Light/<soggetto>/20260725: 2 file")
ck(len(files(f"Plan/Light/{TARGET}/20260726")) == 1, "Light/<soggetto>/20260726: 1 file")
ck(files(f"Plan/Light/{TARGET}") == [], "nessun light sciolto nella radice soggetto")

print("N2 resto della share invariato")
ck(files("Preview/M 42") == ["anteprima.fit"], "Preview copiata come prima")
ck(files("log") == ["asiair.log"], "log copiati come prima")
ck((NAS / "asiair_sfro" / "mdb_9.log").exists(), "file di radice share copiato")

print("N3 conteggi per il bot Telegram")
ck(r["by_kind"] == {"light": 3, "flat": 3, "dark": 1},
   f"by_kind={r['by_kind']} (light 3, flat 3, dark 1)")
ck(r["files"] >= 7, f"files={r['files']}")

print("N4 secondo sync: incrementale, zero trasferimenti")
r2 = run()
ck(r2["ok"], "ok=True")
ck(r2["by_kind"] == {"light": 0, "flat": 0, "dark": 0},
   f"by_kind={r2['by_kind']} = tutti zero")

print("N5 notte nuova: solo i file nuovi")
touch(f"ASIAIR/Plan/Light/{TARGET}/Light_{TARGET}_300.0s_Bin1_2600MM_G_gain100_20260727-051000_12deg_0.0C_0001.fit")
touch("ASIAIR/Autorun/Flat/Flat_5.0s_Bin1_2600MM_G_gain0_20260727-133000_15deg_0.0C_0001.fit")
r3 = run()
ck(r3["by_kind"] == {"light": 1, "flat": 1, "dark": 0},
   f"by_kind={r3['by_kind']} (1 light + 1 flat nuovi)")
ck(len(files(f"Plan/Light/{TARGET}/20260727")) == 1, "nuova cartella Light/20260727")
ck(len(files("Autorun/Flat/20260727")) == 1, "nuova cartella Flat/20260727")

print("N6 soggetto nuovo (2 livelli da creare)")
NEW = "NGC 6960"
touch(f"ASIAIR/Plan/Light/{NEW}/Light_{NEW}_180.0s_Bin1_2600MM_H_gain100_20260727-052000_12deg_0.0C_0001.fit")
r4 = run()
ck(r4["ok"], f"ok=True (error={r4['error']})")
ck(len(files(f"Plan/Light/{NEW}/20260727")) == 1, "Light/NGC 6960/20260727 creata")

print("N7 file senza data nel nome: resta nella radice, non si perde")
touch("ASIAIR/Autorun/Flat/note_utente.txt")
r5 = run()
ck(r5["ok"], f"ok=True (error={r5['error']})")
ck("note_utente.txt" in files("Autorun/Flat"), "file senza data nella radice Flat")
ck(len(files("Autorun/Flat/20260725")) == 2, "le cartelle per data restano intatte")

print("N8 dry-run: non trasferisce")
touch("ASIAIR/Autorun/Dark/Dark_5.0s_Bin1_2600MM_G_gain0_20260727-134000_15deg_0.0C_0001.fit")
_seen["mp"] = 0
rd = A.sync_pass(SC, dry_run=True)
ck(rd["ok"], "ok=True")
ck(rd["by_kind"]["dark"] == 1, f"prevede 1 dark ({rd['by_kind']})")
ck(files("Autorun/Dark/20260727") == [], "in dry-run nessun file scritto")

print("N9 NAS non montato: errore ordinario, nessuna eccezione")
A.is_mounted = lambda p: False
rn = A.sync_pass(SC, dry_run=False)
ck(not rn["ok"] and "non montato" in rn["error"], f"error={rn['error']}")
A.is_mounted = fake_is_mounted

shutil.rmtree(TMP)
print("\nTUTTI I TEST SYNC-LAYOUT: OK")
