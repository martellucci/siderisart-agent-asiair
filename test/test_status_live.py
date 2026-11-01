"""Test OFFLINE dello Status a riquadro «vivo» (richiesta utente 2026-10-20:
meno messaggi su Telegram):
- nella notte nautica UN messaggio, poi modificato al suo posto ogni
  status_live_minutes fino all'alba nautica, dove si ferma con la nota finale
- un nuovo Status ferma il precedente (nota "non più aggiornato")
- fuori dalla notte: messaggio singolo, nessuna modifica
- riga ASIAIR = Plan / Autorun / Pause; barra delle riprese; fine prevista
  in ora TX e IT, con l'avviso se cade oltre l'alba
- messaggio cancellato in chat -> si smette senza mandarne di nuovi
- riavvio del bot -> il riquadro vivo si riprende dal file
Nessun rig, nessuna rete: diagnose, telemetria e Telegram sono finti.
"""
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from pathlib import Path as _P
ROOT = _P(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import sfro_agent as A
import sfro_mqtt as M
import sfro_telegram as T

A.setup_logging("WARNING")
cfg = A.load_config(ROOT / "config.example.yaml")
TMP = Path(tempfile.mkdtemp(prefix="sfro-statuslive-"))
cfg["state_file"] = str(TMP / "state.json")
cfg["telegram_bot"]["status_live_file"] = str(TMP / "status_live.json")
assert cfg["telegram_bot"]["status_live_minutes"] == 9, "config: 9 minuti"

MSGS, EDITS = [], []
EDIT_REPLY = {"ok": True}
T.SfroBot.send = (lambda self, t, keyboard=None, thread=None:
                  (MSGS.append(t), len(MSGS))[1])
SES = {}


class FakePub:
    def __init__(self, cfg, dry=False):
        pass

    def slow_payloads(self):
        return {"session": dict(SES), "camera": {}}


M.Publisher = FakePub


def mkbot():
    b = T.SfroBot(cfg)
    b.chat_id = "42"
    b.diagnose = lambda with_kasa=True: {
        "vpn_up": True, "kasa_ok": True, "kasa_err": None, "children": [],
        "plug": "ON", "asiair_ping": True, "asiair_rpc": True, "rpc_err": None}
    b.guide_rms = lambda exp_s=None: (0.88, 0.66)
    b.api = lambda method, **p: ((EDITS.append((p["message_id"], p["text"])),
                                  dict(EDIT_REPLY))[1]
                                 if method == "editMessageText" else {"ok": True})
    b.live_every_s = 0.2
    return b


A.http_get_json = lambda *a, **k: None
A.parse_alpaca = lambda v: "OPEN"
tz = T.ZoneInfo(cfg["timezone_observatory"])


def set_night(b, secs):
    """Notte nautica che finisce fra `secs` secondi (None = fuori notte)."""
    if secs is None:
        b.night = lambda: (False, None, datetime.now(tz) + timedelta(hours=5), "x")
    else:
        end = datetime.now(tz) + timedelta(seconds=secs)
        b.night = lambda: (True, None, end, "x")


def reset():
    MSGS.clear()
    EDITS.clear()
    EDIT_REPLY.clear()
    EDIT_REPLY["ok"] = True


# S1: piano in corso, fine oltre l'alba -> riquadro vivo fino all'alba
eta = datetime.now(tz) + timedelta(hours=5)
SES.update(plan_started=True, capturing=True, plan_name="Sideris",
           target="SH2-132", filter="O", frame_done=48, frame_total=110,
           exp_s=300.0, eta=eta.isoformat(timespec="seconds"),
           capture_state="expose")
reset()
bot = mkbot()
set_night(bot, 1.0)
bot.do_status(None)
assert len(MSGS) == 1, MSGS
t0 = MSGS[0]
print("### S1 primo messaggio\n" + t0)
assert t0.startswith("📊 Stato SFRO — ultimo aggiornamento "), t0
assert "🟢 ASIAIR: Plan" in t0 and "▶️ Piano «Sideris» in corso" in t0, t0
assert "📷 Riprese: ▓▓▓▓░░░░░░ 44%" in t0, t0
assert "48/110 fatte · 62 mancanti · pose da 300s" in t0, t0
assert (f"⏱️ Fine prevista alle ore {eta:%H:%M} TX · "
        f"{eta.astimezone(A.TZ_IT):%H:%M} IT (oltre l'alba nautica") in t0, t0
assert "🔄 Si aggiorna qui ogni" in t0, t0
assert (TMP / "status_live.json").exists()
time.sleep(1.8)
assert len(MSGS) == 1, MSGS                  # nessun messaggio nuovo
assert len(EDITS) >= 3 and all(m == 1 for m, _ in EDITS), EDITS
assert "🔄" in EDITS[0][1] and EDITS[-1][1].endswith(
    "⏹️ Aggiornamenti finiti: alba nautica delle "
    f"{bot.night()[2]:%H:%M} TX"), EDITS[-1]
assert not (TMP / "status_live.json").exists()
n = len(EDITS)
time.sleep(0.5)
assert len(EDITS) == n, "dopo l'alba non si modifica piu'"

# S2: un secondo Status ferma il primo
reset()
set_night(bot, 30)
bot.do_status(None)
time.sleep(0.3)
bot.do_status(None)
assert len(MSGS) == 2, MSGS
nota = [t for m, t in EDITS if m == 1 and "Non più aggiornato" in t]
assert len(nota) == 1, EDITS
time.sleep(0.6)
after = [m for m, t in EDITS[EDITS.index((1, nota[0])) + 1:]]
assert after and set(after) == {2}, EDITS        # si aggiorna solo il nuovo
bot.live["ev"].set()

# S3: fuori dalla notte -> messaggio singolo, autorun, nessun footer
reset()
SES.update(plan_started=False, capturing=True, capture_state="expose")
set_night(bot, None)
bot2 = mkbot()
bot2.night = bot.night
bot2.do_status(None)
time.sleep(0.5)
assert len(MSGS) == 1 and not EDITS, (MSGS, EDITS)
assert "🟢 ASIAIR: Autorun" in MSGS[0] and "▶️ Autorun in corso" in MSGS[0]
assert "🔄" not in MSGS[0] and "oltre l'alba" not in MSGS[0], MSGS[0]

# S4: pausa (meteo e app)
assert T.SfroBot.asiair_mode({"plan_started": True, "capturing": False},
                             {"weather_closed": True}) == "Pause (tetto chiuso)"
assert T.SfroBot.asiair_mode({"plan_started": True, "capture_state": "pause"},
                             {}) == "Pause"
assert T.SfroBot.asiair_mode({}, {}) == "nessuna attività"

# S5: messaggio cancellato in chat -> si smette, niente messaggi nuovi
reset()
SES.update(plan_started=True, capturing=True)
bot3 = mkbot()
set_night(bot3, 30)
bot3.do_status(None)
EDIT_REPLY.update(ok=False, description="Bad Request: message to edit not found")
time.sleep(0.6)
assert len(MSGS) == 1 and len(EDITS) == 1, (MSGS, EDITS)
assert bot3.live is None and not (TMP / "status_live.json").exists()

# S6: riavvio del bot -> ripresa dal file; alba gia' passata -> chiusura
reset()
bot4 = mkbot()
set_night(bot4, 30)
bot4.do_status(None)
bot4.live["ev"].set()                         # "muore" il vecchio processo
bot5 = mkbot()
bot5.live_resume()
assert bot5.live and bot5.live["mid"] == 1
time.sleep(0.5)
assert any(m == 1 for m, _ in EDITS), EDITS
bot5.live["ev"].set()
reset()
(TMP / "status_live.json").write_text(
    '{"mid": 7, "body": "📊 vecchio", "until": "%s"}'
    % (datetime.now(tz) - timedelta(minutes=5)).isoformat())
bot6 = mkbot()
bot6.live_resume()
assert EDITS == [(7, "📊 vecchio\n⏹️ Aggiornamenti finiti: alba nautica delle "
                  f"{datetime.now(tz) - timedelta(minutes=5):%H:%M} TX")], EDITS
assert bot6.live is None and not (TMP / "status_live.json").exists()

print("OK test_status_live")
