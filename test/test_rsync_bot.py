"""Test OFFLINE del bottone Rsync del bot (richiesta utente 2026-10-13: "la
procedura deve essere sempre la medesima" del sync dopo flat/dark):
- UNA passata da sync_module.long_sync_minutes, avanzamento ogni 9'
- tempo esaurito -> messaggio "NON completato", NESSUNA seconda passata e
  NESSUNA domanda a bottoni
- testi di avvio/fine/tempo esaurito IDENTICI a quelli del flusso flat
- messaggio «vivo»: avvio inviato, avanzamenti e stato finale MODIFICATI al
  suo posto (editMessageText), fine annunciata con un messaggio nuovo
- le vecchie domande "continuo con altri 30'?" rimaste in chat non fanno
  ripartire nulla
Nessun rig, nessuna rete: sync_pass, diagnose e Telegram sono finti.
"""
import sys
import tempfile
from pathlib import Path
from pathlib import Path as _P
ROOT = _P(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import sfro_agent as A
import sfro_telegram as T

A.setup_logging("WARNING")
cfg = A.load_config(ROOT / "config.example.yaml")
TMP = Path(tempfile.mkdtemp(prefix="sfro-rsyncbot-"))
cfg["state_file"] = str(TMP / "state.json")
sc = cfg["sync_module"]
assert sc["long_sync_minutes"] == 100, "config: long_sync_minutes 100"
assert "long_first_minutes" not in sc and "long_more_minutes" not in sc, \
    "config: le passate 60'+30' non esistono piu'"

MSGS, SYNC_KW, EDITS = [], [], []
SYNC_Q = []
T.SfroBot.send = (lambda self, t, keyboard=None, thread=None:
                  (MSGS.append((t, keyboard)), len(MSGS))[1])
A.sync_busy = lambda sc: None    # mai il lock di produzione nei test


def _sync_stub(sc, dry_run, cfg=None, **kw):
    SYNC_KW.append(kw)
    return SYNC_Q.pop(0)


A.sync_pass = _sync_stub
bot = T.SfroBot(cfg)
bot.chat_id = "42"
bot.diagnose = lambda with_kasa=False: {"asiair_ping": True, "vpn_up": True}
bot.api = lambda method, **p: (EDITS.append((p.get("message_id"), p.get("text")))
                               if method == "editMessageText" else None,
                               {"ok": True})[1]
bot.spawn = lambda name, fn, th: MSGS.append((f"[spawn {name}]", None))
COSA = "Rsync ASIAIR→NAS (tutte le date)"


def run(label):
    MSGS.clear()
    SYNC_KW.clear()
    EDITS.clear()
    bot.do_rsync(None)
    print(f"### {label}")
    for t, kb in MSGS:
        print("   TG>", t.replace("\n", " | ")[:140], "[bottoni]" if kb else "")
    for mid, t in EDITS:
        print(f"   ED#{mid}>", t.replace("\n", " | ")[:140])


# R1: copia completa in una passata
SYNC_Q[:] = [{"ok": True, "copied": 180, "bytes": 8_800_000_000,
              "total_files": 180, "total_bytes": 8_800_000_000,
              "elapsed_s": 106 * 60,
              "by_kind": {"light": 0, "flat": 90, "dark": 90}, "error": None}]
run("R1 completo")
assert len(SYNC_KW) == 1 and SYNC_KW[0]["cap_minutes"] == 100, SYNC_KW
assert SYNC_KW[0]["full"] is True and callable(SYNC_KW[0]["progress"]), SYNC_KW
assert MSGS[0][0] == ("💾 Rsync ASIAIR→NAS (tutte le date) — in corso\n"
                      "conto i file da copiare…\n"
                      "al massimo 100 minuti · aggiornamento qui ogni 9'"), MSGS[0]
assert len(MSGS) == 2, MSGS                    # avvio + fine, nient'altro
assert EDITS[-1][0] == 1 and EDITS[-1][1].startswith(
    "💾 Rsync ASIAIR→NAS (tutte le date) — completato ✅\n▓▓▓▓▓▓▓▓▓▓ 100%"), EDITS
assert MSGS[-1][0] == ("✅ Rsync ASIAIR→NAS (tutte le date) completato: 180 file · "
                       "8,8 GB in 1 h 46' (1,4 MB/s) — light 0 · flat 90 · "
                       "dark 90."), MSGS[-1]
# avanzamento: MODIFICA del messaggio di avvio, nessun messaggio nuovo
live = SYNC_KW[0]["progress"].__self__
live.closed = False                            # come durante la copia
n = len(MSGS)
SYNC_KW[0]["progress"]({"copied": 86, "bytes": 4_100_000_000,
                        "total_files": 180, "total_bytes": 8_800_000_000}, 3120)
assert len(MSGS) == n and EDITS[-1][0] == 1, (MSGS, EDITS)
assert "▓▓▓▓▓░░░░░ 47%\n86/180 file · 4,1/8,8 GB\ndalle " in EDITS[-1][1], EDITS[-1]

# R2: tempo esaurito -> UNA passata, messaggio, niente seconda passata ne' domanda
SYNC_Q[:] = [{"ok": False, "copied": 150, "bytes": 7_300_000_000, "cap": True,
              "total_files": 180, "total_bytes": 8_800_000_000,
              "elapsed_s": 6000,
              "error": "tempo massimo del sync esaurito (100 min)"}]
SYNC_Q_R2 = dict(SYNC_Q[0])
run("R2 tempo esaurito")
assert len(SYNC_KW) == 1 and not SYNC_Q, SYNC_KW
assert not any(kb for _, kb in MSGS), "nessuna domanda a bottoni"
cap_txt = A.sync_cap_text(COSA, sc, SYNC_Q_R2)
assert MSGS[-1][0] == cap_txt, MSGS[-1]
assert "NON completato in 100 minuti (150/180 file · 7,3/8,8 GB" in cap_txt
assert EDITS[-1][1].startswith(f"💾 {COSA} — NON completato\n▓▓▓▓▓▓▓▓░░ 83%"), EDITS
assert "non riparto da solo, rilancia da /sfro → Rsync" in cap_txt
# stesso testo del flusso flat, cambia solo il soggetto
assert (A.sync_cap_text("Sync NAS flat/dark", sc, SYNC_Q_R2)
        .replace("Sync NAS flat/dark", COSA) == cap_txt)

# R3: errore vero
SYNC_Q[:] = [{"ok": False, "copied": 0, "error": "NAS dest non montato"}]
run("R3 errore")
assert MSGS[-1][0] == "⛔ Rsync in errore: NAS dest non montato", MSGS[-1]
assert EDITS[-1][1].startswith(f"💾 {COSA} — in errore"), EDITS

# R4: le vecchie domande "altri 30'?" rimaste in chat non fanno ripartire nulla
for data in ("rs:more", "rs:stop", "ss:more", "ss:stop"):
    MSGS.clear()
    bot.on_callback({"id": "1", "data": data,
                     "message": {"chat": {"id": 42}, "message_id": 7}})
    assert not any(t.startswith("[spawn") for t, _ in MSGS), (data, MSGS)
    assert any("non esiste più" in t for t, _ in MSGS), (data, MSGS)
print("vecchie domande 'continuo?': nessuna ripartenza: OK")

print("\nTUTTI I TEST RSYNC BOT: OK")
