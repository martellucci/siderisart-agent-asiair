"""Test OFFLINE del conteggio per-cartella (by_kind) di sync_pass (bottone Rsync).
Dal 2026-07-26 la classificazione NON viene piu' dal percorso ma dal gruppo
(cartella, data) che ha generato la passata: qui si stubba rsync e si verifica
che ogni file finisca nel tipo giusto e che le directory non entrino nel
dettaglio. La verifica end-to-end con rsync vero e' in test_sync_layout.py."""
import shutil
import sys
import tempfile
from pathlib import Path
from pathlib import Path as _P
ROOT = _P(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import sfro_agent as A

TMP = tempfile.mkdtemp(prefix="bykind_")   # sync_pass crea le cartelle dest

# ogni passata rsync "trasferisce" 1 directory + 2 file
FAKE_OUT = "\n".join([
    "cd+++++++++ ./",                      # directory: fuori dal dettaglio
    ">f+++++++++ 1000 a.fit",
    ">f+++++++++ 2000 b.fit",
    "",
])


class FakeProc:
    returncode = 0
    stdout = FAKE_OUT
    stderr = ""


CALLS = []
A.is_mounted = lambda p: True
A.ping = lambda h: True
A._run = lambda cmd, timeout=None: (CALLS.append(cmd), FakeProc())[1]


# rsync gira in streaming dal 2026-10-08 (conteggio dal vivo per l'avanzamento):
# lo stub riusa A._run (anche quando un test lo sostituisce) e ne passa le righe
def _fake_stream(cmd, timeout, on_line):
    out = A._run(cmd, timeout=timeout)
    for ln in out.stdout.splitlines():
        on_line(ln)
    return out


A._run_stream = _fake_stream
# un flat, un dark e due soggetti: 4 gruppi, ognuno con una sola data
A.dated_dirs = lambda mp: [("ASIAIR/Autorun/Flat", "flat"),
                           ("ASIAIR/Autorun/Dark", "dark"),
                           ("ASIAIR/Plan/Light/NGC 7000", "light"),
                           ("ASIAIR/Plan/Light/M 42", "light")]
A.split_by_date = lambda src: ({"20260726"}, False)

sc = {"nas_dest": TMP, "asiair_ip": "1.2.3.4",
      "mount_point": TMP + "/asiair_sfro",
      "smb_share_name": "TF Images", "credentials_file": "/c",
      "rsync_exclude": [], "rsync_extra": [], "per_run_cap_minutes": 30,
      "lock_file": TMP + "/sync.lock"}       # mai il lock di produzione
r = A.sync_pass(sc, dry_run=False)

assert r["ok"] and r["error"] is None, r
# 5 passate (1 generale + 4 gruppi) x 3 elementi
assert r["files"] == 15, r
# i file dei 4 gruppi (2 ciascuno); la passata generale non ha tipo
assert r["by_kind"] == {"light": 4, "flat": 2, "dark": 2}, r
# file COMPLETATI e byte: 2 file x 5 passate (le directory non contano)
assert (r["copied"], r["bytes"]) == (10, 15000), r

rs = [c for c in CALLS if c[0] == "rsync"]
assert len(rs) == 5, rs
# la passata generale esclude le tre radici smistate per data
gen = rs[0]
for rel in ("/ASIAIR/Autorun/Flat/", "/ASIAIR/Autorun/Dark/", "/ASIAIR/Plan/Light/"):
    assert rel in gen, (rel, gen)
# le passate di gruppo filtrano per data e finiscono nella cartella della data
assert "*_20260726-[0-9][0-9][0-9][0-9][0-9][0-9]_*" in rs[1], rs[1]
assert rs[1][-1].endswith("/asiair_sfro/ASIAIR/Autorun/Flat/20260726/"), rs[1]
assert rs[4][-1].endswith("/asiair_sfro/ASIAIR/Plan/Light/M 42/20260726/"), rs[4]

print("by_kind:", r["by_kind"], "files:", r["files"], "passate:", len(rs))

# TETTO vs VPN (2026-09-15): rsync ucciso al tetto. ASIAIR ancora raggiungibile
# = copia lunga (cap, si riprende); ASIAIR muto = VPN caduta (errore vero)
import subprocess                                               # noqa: E402


def _run_timeout(cmd, timeout=None):
    CALLS.append(cmd)
    if cmd[0] == "rsync":
        raise subprocess.TimeoutExpired(cmd, timeout or 0)
    return FakeProc()


A._run = _run_timeout
r = A.sync_pass(sc, dry_run=False)
assert not r["ok"] and r.get("cap"), r
assert "tempo massimo del sync esaurito" in r["error"], r
assert "VPN" not in r["error"], r

PINGS = iter([True, False])          # su all'inizio, giu' a tetto scaduto
A.ping = lambda h: next(PINGS)
r = A.sync_pass(sc, dry_run=False)
assert not r["ok"] and not r.get("cap"), r
assert "VPN caduta" in r["error"], r
print("tetto con ASIAIR su -> cap; con ASIAIR giu' -> VPN: OK")

# AGGANCIO della vista per soggetto x notte (2026-09-17): passando cfg,
# sync_pass aggiorna sessioni_giornaliere in coda alla passata. Gli altri test
# stubbano sync_pass e quindi non coprirebbero questo punto.
A._run = lambda cmd, timeout=None: (CALLS.append(cmd), FakeProc())[1]
A.ping = lambda h: True
# Data dei file = OGGI (UTC), non una data fissa: la vista ricontrolla solo le
# ultime nights_back notti rispetto a oggi, e con la data fissa del 17/9 questo
# test e' tornato rosso da solo qualche giorno dopo (scoperto il 2026-10-08).
from datetime import datetime, timedelta, timezone              # noqa: E402
D = datetime.now(timezone.utc).strftime("%Y%m%d")
FLAT = f"Flat_1.2s_Bin1_2600MM_H_gain100_{D}-140000_12deg_0.1C_0001.fit"
src = Path(TMP) / "asiair_sfro" / "ASIAIR"
(src / f"Plan/Light/NGC 7000/{D}").mkdir(parents=True, exist_ok=True)
(src / f"Autorun/Flat/{D}").mkdir(parents=True, exist_ok=True)
(src / f"Plan/Light/NGC 7000/{D}" /
 f"Light_NGC 7000_300.0s_Bin1_2600MM_H_gain100_{D}-021500_1deg_0.0C_0001.fit"
 ).write_bytes(b"fit")
(src / f"Autorun/Flat/{D}" / FLAT).write_bytes(b"fit")
cfg = {"timezone_observatory": "America/Chicago",
       "sync_module": dict(sc, mount_point=TMP + "/asiair_sfro"),
       "sessioni_giornaliere": {"enabled": True, "mode": "hardlink",
                                "dest": TMP + "/sessioni_giornaliere",
                                "folder_timezone": "Europe/Rome"}}
r = A.sync_pass(sc, dry_run=False, cfg=cfg)
assert r["ok"], r
assert r.get("sess"), "sync_pass non ha aggiornato la vista per soggetto"
assert (r["sess"]["light"], r["sess"]["flat"]) == (1, 1), r["sess"]
# cartella = giorno italiano dell'istante: 02:15 e 14:00 UTC cadono nello
# stesso giorno anche a Roma (04:15 e 16:00 d'estate, 03:15 e 15:00 d'inverno)
link = Path(TMP) / "sessioni_giornaliere" / "NGC 7000" / D / "flat" / FLAT
assert link.exists(), link
# in dry-run la vista NON si tocca (nessun hardlink a fronte di una simulazione)
r = A.sync_pass(sc, dry_run=True, cfg=cfg)
assert not r.get("sess"), r
print("aggancio vista per soggetto dentro sync_pass: OK")

# FINESTRA DI DATE (2026-10-08): le passate automatiche saltano le date piu'
# vecchie di recent_days (la notte del 7-8/10 dieci passate hanno riscaricato
# flat di settimane prima, dopo una pulizia del NAS); il bottone Rsync del bot
# (full=True) e recent_days=0 le copiano tutte, come prima.
recente = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
vecchia = (datetime.now() - timedelta(days=30)).strftime("%Y%m%d")
A.split_by_date = lambda src: ({recente, vecchia}, False)
sc_win = dict(sc, recent_days=7)


def passate_per_data():
    return [c for c in CALLS if c[0] == "rsync" and "--include" in c]


CALLS.clear()
r = A.sync_pass(sc_win, dry_run=False)
assert r["ok"], r
pd = passate_per_data()
assert len(pd) == 4, pd                     # 4 cartelle x la sola data recente
assert all(A.date_glob(recente) in c for c in pd), pd
assert not any(A.date_glob(vecchia) in c for c in pd), pd
assert r["skipped_dates"] == 4, r
print("finestra 7 giorni: data di 30 giorni fa saltata in tutte e 4 le cartelle")

CALLS.clear()
r = A.sync_pass(sc_win, dry_run=False, full=True)
assert r["ok"] and r["skipped_dates"] == 0, r
assert len(passate_per_data()) == 8, passate_per_data()
print("bottone Rsync (full=True): tutte le date")

CALLS.clear()
r = A.sync_pass(dict(sc, recent_days=0), dry_run=False)
assert r["skipped_dates"] == 0 and len(passate_per_data()) == 8, r
print("recent_days=0: nessuna finestra, comportamento storico")

# TETTO SU MISURA + AVANZAMENTO (2026-10-08): cap_minutes sostituisce
# per_run_cap_minutes (bottone Rsync e sync dopo flat/dark: 60' poi 30') e
# progress viene chiamata a cadenza coi file completati finora.
import time                                                     # noqa: E402
A.split_by_date = lambda src: ({recente}, False)
SEEN = {}


def _slow_stream(cmd, timeout, on_line):
    SEEN["timeout"] = max(SEEN.get("timeout", 0), timeout)
    for ln in FAKE_OUT.splitlines():
        on_line(ln)
        time.sleep(0.05)
    return FakeProc()


A._run_stream = _slow_stream
TICKS = []
r = A.sync_pass(dict(sc, progress_minutes=0.004), dry_run=False, cap_minutes=60,
                progress=lambda rr, sec: TICKS.append((rr["copied"], sec)))
assert r["ok"] and SEEN["timeout"] > 30 * 60, SEEN      # tetto 60', non 30'
assert TICKS and all(t[0] <= r["copied"] for t in TICKS), TICKS
assert [t[0] for t in TICKS] == sorted(t[0] for t in TICKS), TICKS
n_ticks = len(TICKS)
time.sleep(0.5)
assert len(TICKS) == n_ticks, "avanzamento dopo la fine della passata"
print(f"tetto 60' e {n_ticks} avanzamenti progressivi: OK")
# in dry-run nessun avanzamento
TICKS.clear()
A.sync_pass(dict(sc, progress_minutes=0.004), dry_run=True,
            progress=lambda rr, sec: TICKS.append(1))
assert not TICKS, TICKS
txt = A.sync_progress_text(47, 2_400_000_000, 1200, "passata 1")
assert txt == ("⏳ Rsync in corso — passata 1: 47 file copiati finora · "
               "questa passata 2,4 GB in 20' (2,0 MB/s)."), txt
print(txt)

# BLOCCO DEL SYNC (2026-10-08): con un altro sync in corso (lock tenuto, qui
# da un secondo open del file: flock e' per descrittore) sync_pass non monta
# e non lancia rsync, e dice chi sta copiando.
import fcntl                                                     # noqa: E402
CALLS.clear()
A._run_stream = _fake_stream
other = open(sc["lock_file"], "a+")
fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
other.seek(0)
other.truncate()
other.write("4242 sfro_telegram.py 2026-10-08T21:00:00\n")
other.flush()
r = A.sync_pass(sc, dry_run=False)
assert r.get("busy") == "Rsync dal menù Telegram" and not r["ok"], r
assert "già un sync in corso" in r["error"] and not CALLS, (r, CALLS)
assert A.sync_busy(sc) == "Rsync dal menù Telegram"
other.close()                                  # finito l'altro: si riparte
assert A.sync_busy(sc) is None
r = A.sync_pass(sc, dry_run=False)
assert r["ok"] and not r.get("busy"), r
assert "sfro" in open(sc["lock_file"]).read() or "test_" in open(sc["lock_file"]).read()
print("lock del sync: secondo sync rifiutato con il nome di chi copia, poi libero")

shutil.rmtree(TMP)
print("TEST RSYNC BY_KIND: OK")
