"""Test OFFLINE della vista per soggetto x notte (sfro_sessioni, 2026-09-17).

Verifica su un finto albero NAS:
  - layout <soggetto>/<data>/light|flat|dark/
  - la cartella-data porta il giorno ITALIANO dell'istante di ripresa
    (2026-09-18), non quello dell'osservatorio: la sessione che a questo sito
    parte la sera del 17/9 sta in 20260918, flat del mattino dopo compresi
  - i flat/dark vanno sotto OGNI soggetto che ha usato quel filtro+gain, e
    SOLO sotto quelli (un soggetto ripreso in H non si prende i flat L)
  - hardlink = stesso inode, nessuno spazio in piu'
  - passata incrementale: la seconda volta non collega piu' niente
  - nome file non riconoscibile: saltato e contato, mai perso
"""
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from pathlib import Path as _P
ROOT = _P(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import sfro_sessioni as S

TMP = Path(tempfile.mkdtemp(prefix="sessioni_"))
SRC = TMP / "asiair_sfro" / "ASIAIR"
DEST = TMP / "sessioni_giornaliere"
CFG = {"timezone_observatory": "America/Chicago",
       "sync_module": {"nas_dest": str(TMP), "mount_point": "/x/asiair_sfro"},
       "sessioni_giornaliere": {"enabled": True, "dest": str(DEST),
                                "mode": "hardlink", "nights_back": 2,
                                "folder_timezone": "Europe/Rome"}}

S1, S2 = "LDN 1295", "M 42"
N = "20260918"          # la sessione della sera del 17/9 a questo sito


# --- la regola in un colpo solo: stesso istante, due fusi, due nomi ---------
ist = datetime(2026, 9, 18, 1, 30, tzinfo=timezone.utc)   # 17/9 20:30 a questo sito
assert S.night_of(ist, ZoneInfo("Europe/Rome")) == "20260918", "data italiana"
assert S.night_of(ist, ZoneInfo("America/Chicago")) == "20260917", "data USA"


def mk(rel: str, name: str):
    p = SRC / rel / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"fit")
    return p


# --- sessione della sera del 17/9 a questo sito = 20260918 in Italia -----------
# 20260918-013000 UTC = 17/9 20:30 questo sito = 18/9 03:30 in Italia
mk(f"Plan/Light/{S1}/20260918", "Light_LDN 1295_300.0s_Bin1_2600MM_H_gain100_20260918-013000_1deg_0.0C_0001.fit")
mk(f"Plan/Light/{S1}/20260918", "Light_LDN 1295_300.0s_Bin1_2600MM_H_gain100_20260918-020000_1deg_0.0C_0002.fit")
# 20260917-234500 UTC = 17/9 18:45 questo sito = 18/9 01:45 in Italia: stessa
# sessione ma cartella UTC del giorno PRIMA (copre utc_dirs verso N-1)
mk(f"Plan/Light/{S2}/20260917", "Light_M 42_60.0s_Bin1_2600MM_L_gain0_20260917-234500_1deg_0.0C_0001.fit")
# 20260917-020000 UTC = 17/9 04:00 in Italia: sessione PRECEDENTE, fuori.
# Sta nella stessa cartella UTC di quello sopra: a decidere e' il singolo file
mk(f"Plan/Light/{S2}/20260917", "Light_M 42_60.0s_Bin1_2600MM_L_gain0_20260917-020000_1deg_0.0C_0009.fit")
# calibrazioni del mattino dopo: 20260918-140000 UTC = 18/9 09:00 questo sito =
# 18/9 16:00 in Italia -> stessa cartella dei light della sera
mk("Autorun/Flat/20260918", "Flat_1.2s_Bin1_2600MM_H_gain100_20260918-140000_12deg_0.1C_0001.fit")
mk("Autorun/Flat/20260918", "Flat_3.4s_Bin1_2600MM_L_gain0_20260918-140100_12deg_0.1C_0001.fit")
mk("Autorun/Flat/20260918", "Flat_2.0s_Bin1_2600MM_S_gain75_20260918-140200_12deg_0.1C_0001.fit")
mk("Autorun/Dark/20260918", "Dark_1.2s_Bin1_2600MM_H_gain100_20260918-141000_12deg_0.1C_0001.fit")
mk("Autorun/Dark/20260918", "Dark_3.4s_Bin1_2600MM_L_gain0_20260918-141100_12deg_0.1C_0001.fit")
# nome fuori standard: deve essere saltato con avviso, non collegato
mk("Autorun/Flat/20260918", "Flat_senza_data_ne_gain.fit")

r = S.aggiorna(CFG, [N])
print("risultato:", {k: v for k, v in r.items() if k != "nights"})
assert r["errors"] == 0, r
assert r["skipped"] == 1, r          # il nome fuori standard
assert r["soggetti"] == 2, r
assert r["light"] == 3, r            # 2 di S1 + 1 di S2 (il 4° e' altra sessione)
assert r["flat"] == 2 and r["dark"] == 2, r   # uno per soggetto, S/g75 escluso

ck = lambda p: sorted(x.name for x in (DEST / p).iterdir()) if (DEST / p).is_dir() else []

assert len(ck(f"{S1}/{N}/light")) == 2, ck(f"{S1}/{N}/light")
assert len(ck(f"{S2}/{N}/light")) == 1, ck(f"{S2}/{N}/light")
# la cartella col giorno dell'OSSERVATORIO non deve esistere
assert not (DEST / S1 / "20260917").exists(), "data USA invece di quella italiana"
# ogni soggetto ha SOLO le calibrazioni del proprio filtro+gain
assert ck(f"{S1}/{N}/flat") == ["Flat_1.2s_Bin1_2600MM_H_gain100_20260918-140000_12deg_0.1C_0001.fit"], ck(f"{S1}/{N}/flat")
assert ck(f"{S2}/{N}/flat") == ["Flat_3.4s_Bin1_2600MM_L_gain0_20260918-140100_12deg_0.1C_0001.fit"], ck(f"{S2}/{N}/flat")
assert ck(f"{S1}/{N}/dark")[0].startswith("Dark_1.2s"), ck(f"{S1}/{N}/dark")
assert ck(f"{S2}/{N}/dark")[0].startswith("Dark_3.4s"), ck(f"{S2}/{N}/dark")
# il filtro S non e' stato usato da nessuno: non deve comparire da nessuna parte
assert not list(DEST.rglob("*_S_gain75_*")), list(DEST.rglob("*_S_gain75_*"))
# la sessione precedente non e' stata toccata
assert not (DEST / S2 / "20260917").exists()

# hardlink: stesso inode dell'originale, niente spazio in piu'
src = SRC / "Autorun/Flat/20260918/Flat_1.2s_Bin1_2600MM_H_gain100_20260918-140000_12deg_0.1C_0001.fit"
dst = DEST / S1 / N / "flat" / src.name
assert src.stat().st_ino == dst.stat().st_ino, "hardlink non condiviso"
print("hardlink: inode condiviso", src.stat().st_ino)

# incrementale: la seconda passata non collega niente di nuovo
r2 = S.aggiorna(CFG, [N])
assert (r2["light"], r2["flat"], r2["dark"], r2["errors"]) == (0, 0, 0, 0), r2
assert S.riepilogo(r2) == "", S.riepilogo(r2)
assert "3 light" in S.riepilogo(r), S.riepilogo(r)

# un flat condiviso da DUE soggetti con lo stesso filtro+gain
mk(f"Plan/Light/{'SH2-132'}/20260918", "Light_SH2-132_300.0s_Bin1_2600MM_H_gain100_20260918-023000_1deg_0.0C_0001.fit")
r3 = S.aggiorna(CFG, [N])
assert r3["light"] == 1 and r3["flat"] == 1 and r3["dark"] == 1, r3
assert (DEST / "SH2-132" / N / "flat" / src.name).stat().st_ino == src.stat().st_ino
print("stesso flat sotto due soggetti, un solo file su disco")

# modulo disabilitato: non tocca niente
off = dict(CFG, sessioni_giornaliere=dict(CFG["sessioni_giornaliere"], enabled=False))
assert S.aggiorna(off, [N])["light"] == 0

shutil.rmtree(TMP)
print("TEST SESSIONI GIORNALIERE: OK")
