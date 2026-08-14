"""Test OFFLINE del conteggio per-cartella (by_kind) di sync_pass (bottone Rsync).
Dal 2026-07-26 la classificazione NON viene piu' dal percorso ma dal gruppo
(cartella, data) che ha generato la passata: qui si stubba rsync e si verifica
che ogni file finisca nel tipo giusto e che le directory non entrino nel
dettaglio. La verifica end-to-end con rsync vero e' in test_sync_layout.py."""
import shutil
import sys
import tempfile
from pathlib import Path as _P
ROOT = _P(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import sfro_agent as A

TMP = tempfile.mkdtemp(prefix="bykind_")   # sync_pass crea le cartelle dest

# ogni passata rsync "trasferisce" 1 directory + 2 file
FAKE_OUT = "\n".join([
    "cd+++++++++ ./",                      # directory: fuori dal dettaglio
    ">f+++++++++ a.fit",
    ">f+++++++++ b.fit",
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
# un flat, un dark e due soggetti: 4 gruppi, ognuno con una sola data
A.dated_dirs = lambda mp: [("ASIAIR/Autorun/Flat", "flat"),
                           ("ASIAIR/Autorun/Dark", "dark"),
                           ("ASIAIR/Plan/Light/NGC 7000", "light"),
                           ("ASIAIR/Plan/Light/M 42", "light")]
A.split_by_date = lambda src: ({"20260726"}, False)

sc = {"nas_dest": TMP, "asiair_ip": "1.2.3.4",
      "mount_point": TMP + "/asiair_sfro",
      "smb_share_name": "TF Images", "credentials_file": "/c",
      "rsync_exclude": [], "rsync_extra": [], "per_run_cap_minutes": 30}
r = A.sync_pass(sc, dry_run=False)

assert r["ok"] and r["error"] is None, r
# 5 passate (1 generale + 4 gruppi) x 3 elementi
assert r["files"] == 15, r
# i file dei 4 gruppi (2 ciascuno); la passata generale non ha tipo
assert r["by_kind"] == {"light": 4, "flat": 2, "dark": 2}, r

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

shutil.rmtree(TMP)
print("by_kind:", r["by_kind"], "files:", r["files"], "passate:", len(rs))
print("TEST RSYNC BY_KIND: OK")
