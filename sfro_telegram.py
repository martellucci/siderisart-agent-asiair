#!/usr/bin/env python3
"""
sfro_telegram.py — Bot Telegram interattivo per l'osservatorio SFRO.

Servizio PERSISTENTE (systemd sfro-telegram.service): long polling getUpdates
sul bot di telegram.txt; il comando /sfro apre un menu a bottoni con 4
operazioni su ASIAIR/KASA:
  - Status   : sistema on/off, tetto, VPN/KASA e — se il piano gira — soggetto,
               frame fatti/mancanti, RMS totale medio della sessione e
               dell'ultimo frame (dal sink guida JSONL scritto da sfro_mqtt).
  - Start    : (conferma Si'/No) KASA on -> attesa boot -> connect_all con
               priming. Cooler e anti-dew restano SPENTI (garanzia esplicita).
               Cancella il marker di shutdown manuale.
  - Go Plan  : avvia il piano SOLO con tetto APERTO e sistema acceso.
  - Shutdown : (conferma Si'/No) stop piano + OF2 chiuso (best-effort),
               pi_shutdown, attesa che il ping muoia, KASA tutta OFF.
               Scrive il marker di shutdown manuale: l'agente NON riaccende
               fino alla prossima notte (o a uno Start dal bot).
  - Rsync    : sync manuale ASIAIR->NAS (Light/Dark/Flat, stessa sync_pass
               dell'agente), con avviso a inizio e fine + conteggio file.
  - Flat/Dark: (conferma Si'/No) chiude la sessione e avvia SUBITO flat e dark
               senza aspettare l'alba (maltempo: inutile attendere la
               riapertura del tetto). Ferma piano/autorun, park, OF2 chiuso,
               reset piano, cooler ON, fascia anticondensa in asciugatura; poi
               passa la palla all'agente (asciugatura -> flat per filtro/gain di
               stanotte -> dark -> sync NAS) che a fine corsa CHIEDE se
               spegnere: la risposta torna qui (bottoni fs:yes/fs:no).
  - Stop Flat: (conferma Si'/No) interrompe l'ATTESA dei 30 minuti prima dei
               flat: niente flat ne' dark, rig lasciato com'e' e in carico
               all'utente (il promemoria di spegnere continua). Solo durante
               l'attesa: un autorun flat/dark gia' partito non si tocca.

Diagnostica errori a scala (richiesta utente): VPN giu' -> KASA irraggiungibile
-> ASIAIR irraggiungibile -> ASIAIR raggiungibile ma in errore (con dettaglio).

Solo la chat di telegram.txt e' autorizzata. Una sola operazione mutante alla
volta (Start/Go Plan/Shutdown); lo Status e' sempre disponibile.
"""

import argparse
import json
import math
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

import sfro_agent as A

HERE = Path(__file__).resolve().parent
log = A.log

MENU_KB = [
    [{"text": "\U0001F4CA Status", "callback_data": "m:status"},
     {"text": "\U0001F680 Start", "callback_data": "m:start"}],
    [{"text": "▶️ Go Plan", "callback_data": "m:goplan"},
     {"text": "\U0001F534 Shutdown", "callback_data": "m:shutdown"}],
    [{"text": "\U0001F4BE Rsync", "callback_data": "m:rsync"},
     {"text": "\U0001F312 Flat/Dark", "callback_data": "m:flatdark"}],
    [{"text": "\U0001F6D1 Stop Flat", "callback_data": "m:stopflat"}],
]

CONFIRM_TEXT = {"start": "🚀 Stai per avviare ASIAIR, confermi?",
                "shutdown": "🔴 Shutdown in avvio, confermi?",
                "flatdark": "🌒 Chiudo la sessione e avvio FLAT e DARK?\n"
                            "• un piano o un autorun in corso viene FERMATO\n"
                            "• mount in park, OF2 chiuso, cooler acceso "
                            "(attesa temperatura)\n"
                            "• asciugatura, poi flat/dark dei filtri di stanotte "
                            "e sync sul NAS\n"
                            "• alla fine ti chiedo se spegnere tutto",
                "stopflat": "🛑 Interrompo l'attesa dei flat?\n"
                            "• niente flat né dark stanotte\n"
                            "• il rig resta com'è (pannello chiuso, cooler "
                            "acceso, fascia al 100%) e torna in carico a te\n"
                            "• continua il promemoria periodico di spegnere\n"
                            "• «No» = il countdown prosegue"}

# rifiuti dello Stop Flat, per fase del flusso (2026-08-15): si annulla SOLO
# l'attesa, un autorun flat/dark gia' partito non si tocca
STOPFLAT_NO = {
    "running": "i FLAT sono già partiti: un autorun in corso non lo fermo.",
    "darks": "i DARK sono già partiti: un autorun in corso non lo fermo.",
    "ask_shutdown": "flat e dark sono già finiti: sto aspettando la tua "
                    "risposta sullo spegnimento.",
    "done": "il flusso flat è già concluso.",
    "cancelled": "l'attesa flat è già stata interrotta.",
    "error": "il flusso flat è già fermo in errore.",
}


class SfroBot:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.files = A.resolve_files(cfg)
        tgc = A.load_kv_file(self.files["telegram"])
        self.token = tgc.get("bot_token", "")
        self.chat_id = str(tgc.get("chat_id", ""))
        self.thread_id = tgc.get("thread_id", "")
        bc = cfg.get("telegram_bot", {}) or {}
        self.poll_timeout = int(bc.get("poll_timeout_seconds", 50))
        self.http_cfg = cfg.get("http", {})
        self.tz = ZoneInfo(cfg.get("timezone_observatory", "UTC"))
        self.ac = A.AsiairControl(cfg.get("asiair", {}))
        self.acfg = cfg.get("asiair", {})
        self.su = cfg.get("startup", {}) or {}
        self.ff = cfg.get("flat_flow", {}) or {}
        # marker letto dall'agente: shutdown manuale -> niente riaccensione
        self.marker = Path(cfg.get("manual_shutdown_file",
                                   "/var/lib/sfro-agent/manual_shutdown.json"))
        # scambio col ciclo dell'agente per i flat/dark MANUALI (2026-08-01):
        # richiesta scritta dal bot, risposta sullo spegnimento idem; lo
        # state.json lo scrive SOLO l'agente (qui si legge e basta)
        self.mf_req = Path(cfg.get("manual_flat_request_file",
                                   "/var/lib/sfro-agent/flat_request.json"))
        self.mf_rep = Path(cfg.get("manual_flat_reply_file",
                                   "/var/lib/sfro-agent/flat_reply.json"))
        # stop dell'ATTESA flat (2026-08-15): stessa meccanica, un file solo
        self.fc_req = Path(cfg.get("flat_cancel_request_file",
                                   "/var/lib/sfro-agent/flat_cancel.json"))
        self.state_file = Path(cfg.get("state_file", ""))
        self.offset = 0
        self.op_lock = threading.Lock()   # una sola operazione mutante alla volta
        self.op_name = None
        self.pending = {}                 # message_id -> (azione, ts) da confermare
        # terminal_uuid Kasa: riusa quello dell'agente se leggibile
        try:
            st = json.loads(Path(cfg.get("state_file", "")).read_text())
            self.tu = st.get("terminal_uuid") or str(uuid.uuid4())
        except Exception:
            self.tu = str(uuid.uuid4())

    # ---------------- API Telegram ----------------
    def api(self, method: str, **payload) -> dict:
        try:
            r = requests.post(f"https://api.telegram.org/bot{self.token}/{method}",
                              json=payload, timeout=self.poll_timeout + 10)
            d = r.json()
            if not d.get("ok"):
                log.warning("telegram %s: %s", method, str(d)[:200])
            return d
        except Exception as e:
            log.warning("telegram %s: %s", method, e)
            return {"ok": False}

    def send(self, text: str, keyboard=None, thread=None):
        p = {"chat_id": self.chat_id, "text": text}
        th = thread if thread is not None else (self.thread_id or None)
        if th:
            p["message_thread_id"] = th
        if keyboard:
            p["reply_markup"] = {"inline_keyboard": keyboard}
        d = self.api("sendMessage", **p)
        return (d.get("result") or {}).get("message_id")

    # ---------------- dispatch update ----------------
    def run(self):
        self.api("setMyCommands",
                 commands=[{"command": "sfro", "description": "Menù osservatorio SFRO"}])
        while True:
            d = self.api("getUpdates", offset=self.offset, timeout=self.poll_timeout,
                         allowed_updates=["message", "callback_query"])
            if not d.get("ok"):
                time.sleep(5)
                continue
            for up in d.get("result", []):
                self.offset = max(self.offset, up["update_id"] + 1)
                try:
                    if "message" in up:
                        self.on_message(up["message"])
                    elif "callback_query" in up:
                        self.on_callback(up["callback_query"])
                except Exception as e:
                    log.error("update %s: %s", up.get("update_id"), e)

    def on_message(self, msg: dict):
        if str((msg.get("chat") or {}).get("id")) != self.chat_id:
            return  # chat non autorizzata: ignora
        text = (msg.get("text") or "").strip()
        cmd = text.split()[0].split("@")[0].lower() if text else ""
        if cmd == "/sfro":
            self.send("🔭 Menù SFRO — scegli un'operazione:", keyboard=MENU_KB,
                      thread=msg.get("message_thread_id"))

    def on_callback(self, cb: dict):
        msg = cb.get("message") or {}
        cbid = cb.get("id")
        if str((msg.get("chat") or {}).get("id")) != self.chat_id:
            self.api("answerCallbackQuery", callback_query_id=cbid)
            return
        data = cb.get("data") or ""
        th = msg.get("message_thread_id")
        mid = msg.get("message_id")

        def ack(t=""):
            self.api("answerCallbackQuery", callback_query_id=cbid, text=t)

        if data == "m:status":
            ack("Leggo lo stato…")
            threading.Thread(target=self._safe, args=("Status", self.do_status, th),
                             daemon=True).start()
        elif data in ("m:start", "m:shutdown", "m:flatdark", "m:stopflat"):
            ack()
            self.ask_confirm(data[2:], th)
        elif data == "m:goplan":
            ack("Verifico tetto e sistema…")
            self.spawn("Go Plan", self.do_goplan, th)
        elif data == "m:rsync":
            ack("Avvio il sync…")
            self.spawn("Rsync", self.do_rsync, th)
        elif data.startswith("c:"):
            action = data[2:]
            # consuma la conferma: bottoni via dal messaggio (no doppio tap)
            self.api("editMessageReplyMarkup", chat_id=self.chat_id, message_id=mid)
            pend = self.pending.pop(mid, None)
            if action == "no":
                ack("Annullato")
                # il «No» allo Stop Flat non e' un'operazione annullata
                # qualunque: vuol dire che il countdown va avanti (2026-08-15)
                self.send("👍 Nessuna interruzione: l'attesa flat prosegue."
                          if pend and pend[0] == "stopflat"
                          else "❌ Operazione annullata.", thread=th)
                return
            if pend is None or pend[0] != action or time.time() - pend[1] > 600:
                ack()
                self.send("⌛ Conferma scaduta: rilancia /sfro.", thread=th)
                return
            ack("Confermato")
            if action == "start":
                self.spawn("Start", self.do_start, th)
            elif action == "shutdown":
                self.spawn("Shutdown", self.do_shutdown, th)
            elif action == "flatdark":
                self.spawn("Flat/Dark", self.do_flatdark, th)
            elif action == "stopflat":
                self.spawn("Stop Flat", self.do_stopflat, th)
        elif data.startswith("fs:"):
            # risposta alla domanda di fine flusso flat MANUALE: il messaggio
            # con i bottoni lo manda l'AGENTE (non e' nel dizionario pending)
            self.api("editMessageReplyMarkup", chat_id=self.chat_id, message_id=mid)
            if data == "fs:yes":
                ack("Spengo tutto")
                self.spawn("Shutdown", self.do_flat_shutdown, th)
            else:
                ack("Lascio acceso")
                self.spawn("Flat/Dark", self.do_flat_keep, th)

    def ask_confirm(self, action: str, th):
        kb = [[{"text": "✅ Sì", "callback_data": f"c:{action}"},
               {"text": "❌ No", "callback_data": "c:no"}]]
        mid = self.send(CONFIRM_TEXT[action], keyboard=kb, thread=th)
        if mid:
            self.pending[mid] = (action, time.time())

    def _safe(self, name, fn, th):
        try:
            fn(th)
        except Exception as e:
            log.error("%s: %s", name, e)
            self.send(f"⛔ {name}: errore inatteso: {e}", thread=th)

    def spawn(self, name, fn, th):
        """Esegue un'operazione MUTANTE in un worker; una sola alla volta."""
        def _run():
            if not self.op_lock.acquire(blocking=False):
                self.send(f"⏳ C'è già un'operazione in corso ({self.op_name}): "
                          "attendi che finisca e riprova.", thread=th)
                return
            self.op_name = name
            try:
                self._safe(name, fn, th)
            finally:
                self.op_name = None
                self.op_lock.release()
        threading.Thread(target=_run, daemon=True).start()

    # ---------------- diagnostica comune ----------------
    def diagnose(self, with_kasa=True) -> dict:
        """Scala diagnostica: VPN -> KASA -> ASIAIR (ping + RPC).
        kasa_ok/vpn_up None = non verificato/non verificabile."""
        d = {"vpn_up": None, "kasa_ok": None, "kasa_err": None, "children": [],
             "plug": "UNKNOWN", "kc": None, "dev": None, "managed": [],
             "asiair_ping": False, "asiair_rpc": False, "rpc_err": None}
        # VPN + ping ASIAIR in un colpo solo: vpn_diagnose riconosce anche
        # l'ICMP 'host unreachable' del router remoto attraverso il tunnel
        # (a rig spento nessun host SFRO risponde all'echo diretto)
        vd = A.vpn_diagnose(self.ac.host, self.acfg.get("vpn_probe_host", ""))
        d["vpn_up"] = vd["vpn_up"]
        d["asiair_ping"] = vd["asiair_up"]
        if with_kasa:
            kc, dev, managed, plug, err = A._kasa_connect(
                self.cfg, self.files, self.http_cfg, self.tu)
            d.update(kasa_ok=err is None, kasa_err=err, kc=kc, dev=dev,
                     managed=managed, plug=plug)
            if kc is not None and dev is not None:
                try:
                    d["children"] = kc.children(dev)
                except Exception:
                    pass
        if d["asiair_ping"]:
            code, res = self.ac._call1(self.ac.port, "test_connection")
            d["asiair_rpc"] = code == 0
            if code != 0:
                d["rpc_err"] = (f"code {code}: {res}" if code is not None
                                else f"trasporto: {res}")
        return d

    def err_lines(self, d: dict) -> list:
        """Righe di errore secondo la scala richiesta (solo i problemi)."""
        out = []
        if d["vpn_up"] is False:
            out.append("⛔ VPN NON attiva: rete remota irraggiungibile.")
        if d["kasa_ok"] is False:
            out.append(f"⛔ KASA non raggiungibile: {d['kasa_err']}")
        if not d["asiair_ping"]:
            out.append("⛔ ASIAIR non raggiungibile "
                       + ("(VPN giù)." if d["vpn_up"] is False
                          else "(spento o in avvio)."))
        elif not d["asiair_rpc"]:
            out.append(f"⛔ ASIAIR raggiungibile ma in ERRORE: {d['rpc_err']}")
        return out

    # ---------------- Status ----------------
    def do_status(self, th):
        d = self.diagnose()
        roof = A.parse_alpaca(A.http_get_json(
            A.alpaca_issafe_url(self.cfg["roof"]), self.http_cfg))
        lines = [f"📊 Stato SFRO — {datetime.now(self.tz).strftime('%H:%M')} TX",
                 {"OPEN": "🏠 Tetto: APERTO",
                  "CLOSED": "🏠 Tetto: CHIUSO"}.get(roof, "🏠 Tetto: SCONOSCIUTO"),
                 "🌐 VPN: " + {True: "OK", False: "GIÙ", None: "n/d"}[d["vpn_up"]]]
        if d["children"]:
            lines.append("🔌 KASA: " + " · ".join(
                f"{c['alias']} {'ON' if c['state'] else 'OFF'}" for c in d["children"]))
        errs = self.err_lines(d)
        if not d["asiair_rpc"]:
            on = d["plug"] == "ON" or d["asiair_ping"]
            lines.append("🟡 Sistema ACCESO ma ASIAIR non operativo." if on
                         else "💤 Sistema SPENTO.")
            lines += errs
            self.send("\n".join(lines), thread=th)
            return
        lines.append("🟢 Sistema ACCESO, ASIAIR operativo.")
        if errs:                      # es. KASA cloud giu' con rig acceso
            lines += errs
        # telemetria: riusa sfro_mqtt.slow_payloads (sola lettura, stessi campi HA)
        try:
            import sfro_mqtt as M
            data = M.Publisher(self.cfg, dry=True).slow_payloads()
        except Exception as e:
            data = {}
            lines.append(f"⚠️ Telemetria non leggibile: {e}")
        ses = data.get("session") or {}
        if ses.get("plan_started") or ses.get("capturing"):
            done, total = ses.get("frame_done"), ses.get("frame_total")
            lines.append(f"▶️ Piano «{ses.get('plan_name') or 'n/d'}» in corso")
            tgt = ses.get("target") or "n/d"
            lines.append(f"🎯 Soggetto: {tgt}"
                         + (f" · filtro {ses['filter']}" if ses.get("filter") else ""))
            if done is not None and total:
                lines.append(f"📷 Riprese: {done}/{total} fatte, {total - done} mancanti"
                             + (f" ({ses['exp_s']}s)" if ses.get("exp_s") else ""))
            if ses.get("left_time_min"):
                eta = (ses.get("eta") or "")[11:16]
                lines.append(f"⏱️ Restano ~{ses['left_time_min']} min"
                             + (f" (fine {eta})" if eta else ""))
            rms_s, rms_f = self.guide_rms(ses.get("exp_s"))

            def _f(v):
                return f'{v:.2f}"' if v is not None else "n/d"
            lines.append(f"📈 RMS totale: sessione {_f(rms_s)} · ultimo frame {_f(rms_f)}")
        else:
            lines.append("⏸️ Nessun piano in esecuzione.")
            cam = data.get("camera") or {}
            if cam.get("sensor_temp_c") is not None:
                lines.append(f"🧊 Camera {cam['sensor_temp_c']}°C, "
                             f"cooler {'ON' if cam.get('cooler_on') else 'OFF'}")
        self.send("\n".join(lines), thread=th)

    def guide_rms(self, exp_s=None, win=120.0):
        """(rms medio sessione, rms ultimo frame) in arcsec dal sink JSONL di
        sfro_mqtt; (None, None) senza dati. Sessione = dall'accensione del rig
        (state.json) o dall'inizio della notte nautica. RMS come ASIAIR/PHD2:
        deviazione standard attorno alla media per finestre da `win` secondi,
        esclusi i campioni dither/settle (b=1); 'ultimo frame' = finestra degli
        ultimi exp_s secondi di guida."""
        sink = Path((self.cfg.get("session_log", {}) or {}).get(
            "guide_sink_dir", "/var/lib/sfro-agent/guide"))
        now = time.time()
        start = None
        try:
            st = json.loads(Path(self.cfg.get("state_file", "")).read_text())
            ts = st.get("asiair_power_on_ts")
            if ts:
                t = datetime.fromisoformat(ts).timestamp()
                if now - t < 24 * 3600:
                    start = t
        except Exception:
            pass
        if start is None:
            in_n, n_start, _, _ = A.nautical_window(
                datetime.now(self.tz), self.cfg["location"], self.tz,
                self.cfg.get("nautical", {}).get("depression", 12))
            start = n_start.timestamp() if (in_n and n_start) else now - 12 * 3600
        samples = []
        for day in {time.strftime("%Y-%m-%d", time.gmtime(t)) for t in (start, now)}:
            f = sink / f"{day}.jsonl"
            if not f.exists():
                continue
            for ln in f.read_text().splitlines():
                try:
                    r = json.loads(ln)
                except Exception:
                    continue
                if r.get("t", 0) >= start and not r.get("b"):
                    samples.append((r["t"], r.get("ra", 0), r.get("dec", 0)))
        if len(samples) < 5:
            return None, None
        samples.sort()

        def _tot(chunk):
            def _rms(vals):
                m = sum(vals) / len(vals)
                return math.sqrt(sum((x - m) ** 2 for x in vals) / len(vals))
            return math.sqrt(_rms([r for _, r, _ in chunk]) ** 2
                             + _rms([dc for _, _, dc in chunk]) ** 2)

        t0 = samples[0][0]
        buckets = {}
        for s in samples:
            buckets.setdefault(int((s[0] - t0) // win), []).append(s)
        tots = [_tot(ch) for ch in buckets.values() if len(ch) >= 5]
        rms_session = sum(tots) / len(tots) if tots else None
        span = float(exp_s or win)
        tmax = samples[-1][0]
        last = [s for s in samples if s[0] >= tmax - span]
        rms_last = _tot(last) if len(last) >= 5 else None
        return rms_session, rms_last

    # ---------------- Start ----------------
    def do_start(self, th):
        self.send("🚀 Start: accendo KASA e preparo l'ASIAIR…", thread=th)
        d = self.diagnose()
        if d["vpn_up"] is False:
            # la sonda VPN puo' dare falsi negativi a rig spento (2026-07-06) e
            # la KASA e' cloud: si AVVISA e si procede; se la VPN e' davvero
            # giu' lo Start fallira' al ping post-boot con causa chiara
            self.send("⚠️ La rete remota non risponde (sonda VPN): procedo comunque "
                      "con l'accensione KASA (cloud). Se la VPN è davvero giù, "
                      "lo Start fallirà all'attesa del boot.", thread=th)
        if d["kasa_ok"] is False and not d["asiair_ping"]:
            self.send(f"⛔ Start annullato: KASA non raggiungibile ({d['kasa_err']}), "
                      "non posso accendere il rig.", thread=th)
            return
        if not d["asiair_ping"]:
            powered, failed, perr = A._kasa_power_on(d["kc"], d["dev"], d["managed"], False)
            if failed or (not powered and d["plug"] != "ON"):
                self.send(f"⛔ Start: accensione KASA fallita ({failed or perr}).",
                          thread=th)
                return
            names = ", ".join(m["alias"] for m in d["managed"]) or "prese"
            self.send(f"🔌 KASA accesa ({names}). Aspetto il boot dell'ASIAIR…",
                      thread=th)
            t0 = time.time()
            tmo = float(self.su.get("ping_timeout_seconds", 240))
            while not A.ping(self.ac.host):
                if time.time() - t0 > tmo:
                    vd = A.vpn_diagnose(self.ac.host,
                                        self.acfg.get("vpn_probe_host", ""))
                    causa = ("VPN NON attiva: rete remota irraggiungibile"
                             if vd["vpn_up"] is False else
                             "VPN su, box che non parte? Controlla il rig")
                    self.send(f"⛔ Start: l'ASIAIR non risponde al ping dopo "
                              f"{int(tmo)}s dall'accensione ({causa}).", thread=th)
                    return
                time.sleep(3)
        ok, det = self.ac.connect_all()
        if not ok:
            self.send(f"⛔ Start: connessione device incompleta: {det}. "
                      "Rig lasciato acceso: riprova o intervieni dall'app.", thread=th)
            return
        # cooler e anti-dew SPENTI (richiesta utente): il boot li lascia gia'
        # off, questo e' un enforcement esplicito coi comandi della config
        okc, detc = self.ac.cooler_off()
        cool = ("cooler e anti-dew SPENTI" if okc
                else f"cooler non verificato ({detc})")
        self.marker.unlink(missing_ok=True)   # via il blocco 'shutdown manuale'
        self.send(f"✅ Sistema PRONTO: {det}; {cool}.\n"
                  "Il piano NON è avviato: usa Go Plan quando vuoi.", thread=th)

    # ---------------- Go Plan ----------------
    def do_goplan(self, th):
        roof = A.parse_alpaca(A.http_get_json(
            A.alpaca_issafe_url(self.cfg["roof"]), self.http_cfg))
        if roof != "OPEN":
            self.send("⛔ Go Plan annullato: tetto CHIUSO, impossibile procedere."
                      if roof == "CLOSED" else
                      "⛔ Go Plan annullato: stato tetto SCONOSCIUTO "
                      "(API SafetyMonitor non risponde).", thread=th)
            return
        d = self.diagnose(with_kasa=False)
        if not d["asiair_rpc"]:
            self.send("⛔ Go Plan annullato: sistema non pronto.\n"
                      + "\n".join(self.err_lines(d)), thread=th)
            return
        snap = self.ac.snapshot()
        if snap.get("plan_started") or snap.get("capturing"):
            self.send(f"ℹ️ Piano «{snap.get('plan_name') or 'n/d'}» GIÀ in esecuzione.",
                      thread=th)
            return
        missing = self.ac.missing_devices(snap)
        if missing:
            self.send(f"⛔ Go Plan: device non connessi ({', '.join(missing)}). "
                      "Usa Start per connetterli.", thread=th)
            return
        ok, det = self.ac.start(snap)
        if ok:
            self.send(f"▶️ Tetto aperto, OF2 aperto: piano "
                      f"«{snap.get('plan_name') or 'n/d'}» AVVIATO.", thread=th)
        else:
            self.send(f"⛔ Avvio piano non riuscito: {det}", thread=th)

    # ---------------- Flat/Dark manuali ----------------
    def flat_stage(self):
        """Fase del flusso flat dallo stato dell'agente (sola lettura)."""
        try:
            return json.loads(self.state_file.read_text()).get("flat_stage")
        except Exception:
            return None

    def flat_reply(self, answer, done, ok, detail=""):
        """Risposta alla domanda di fine flusso: 'keep' (lascia acceso) o
        'shutdown'. done=False = spegnimento in corso, l'agente aspetta zitto."""
        try:
            self.mf_rep.parent.mkdir(parents=True, exist_ok=True)
            self.mf_rep.write_text(json.dumps(
                {"answer": answer, "done": bool(done), "ok": bool(ok),
                 "detail": str(detail)[:200],
                 "ts": datetime.now(self.tz).isoformat()}))
        except Exception as e:
            log.warning("risposta flat manuale: %s", e)

    def do_flatdark(self, th):
        """Bottone Flat/Dark (richiesta utente 2026-08-01): chiude la sessione
        e avvia SUBITO flat e dark senza aspettare l'alba — serve quando il
        maltempo rende inutile attendere la riapertura del tetto.
        Il bot fa la parte immediata (stop di piano/autorun, park, OF2 chiuso,
        reset piano, cooler, fascia anticondensa in asciugatura) e passa il
        resto all'agente, che riusa il flusso di sempre (asciugatura -> flat
        per filtro/gain di stanotte -> dark -> sync NAS) e alla fine CHIEDE se
        spegnere invece di spegnere da solo.
        NB: qui un AUTORUN/attivita' manuale VIENE fermato — l'opposto della
        regola dell'agente ('non si tocca mai'), ma e' proprio quello che
        chiede chi preme il bottone."""
        if not self.ff.get("enabled"):
            self.send("⛔ Flat/Dark: flusso flat DISABILITATO in config.", thread=th)
            return
        stage = self.flat_stage()
        if stage in ("drying", "running", "darks", "ask_shutdown"):
            self.send(f"ℹ️ Flusso flat GIÀ in corso (fase «{stage}»): "
                      "non lo faccio ripartire.", thread=th)
            return
        if self.mf_req.exists():
            self.send("ℹ️ Richiesta flat/dark già inviata: l'agente la prende "
                      "in carico entro pochi minuti.", thread=th)
            return
        d = self.diagnose(with_kasa=False)
        if not d["asiair_rpc"]:
            self.send("⛔ Flat/Dark annullati: sistema non pronto.\n"
                      + "\n".join(self.err_lines(d)), thread=th)
            return
        try:
            snap = self.ac.snapshot()
        except Exception as e:
            self.send(f"⛔ Flat/Dark annullati: ASIAIR non interrogabile ({e}).",
                      thread=th)
            return
        busy = ""
        if snap.get("plan_started") or snap.get("capturing"):
            busy = (f"piano «{snap.get('plan_name') or 'n/d'}»"
                    if snap.get("plan_started") else "autorun/ripresa manuale")
        roof = A.parse_alpaca(A.http_get_json(
            A.alpaca_issafe_url(self.cfg["roof"]), self.http_cfg))
        # RICHIESTA SCRITTA PRIMA delle azioni: se un ciclo dell'agente cade
        # proprio adesso deve vedere il flusso flat, non un piano da riavviare
        try:
            self.mf_req.parent.mkdir(parents=True, exist_ok=True)
            self.mf_req.write_text(json.dumps(
                {"ts": datetime.now(self.tz).isoformat(),
                 "closed_ts": datetime.now(self.tz).isoformat(),
                 "source": "telegram", "ask_shutdown": True,
                 "cause": "flat_manuale"}))
        except Exception as e:
            self.send(f"⛔ Flat/Dark annullati: non riesco a scrivere la "
                      f"richiesta per l'agente ({e}).", thread=th)
            return

        def annulla(msg):
            self.mf_req.unlink(missing_ok=True)
            self.send(f"⛔ {msg}\nFlat/Dark ANNULLATI: rig lasciato com'è, "
                      "intervieni tu.", thread=th)

        self.send("🌒 Flat/Dark: chiudo la sessione"
                  + (f" (fermo {busy})" if busy else " (niente in corso)")
                  + ("." if roof != "OPEN" else
                     ".\n⚠️ Il tetto risulta APERTO: procedo comunque, i flat "
                     "si fanno a pannello chiuso."), thread=th)
        td = self.ac.teardown(keep_cooler=True)   # stop, park, OF2 chiuso
        okf, detf = td.get("flat", (False, "n/d"))
        if not okf:
            return annulla(f"pannello OF2 NON chiuso ({detf}): senza pannello "
                           "chiuso i flat non si possono fare.")
        oks, _ = td.get("stop", (False, ""))
        okh_m, deth_m = td.get("home", (False, ""))
        self.send("🛑 Sessione chiusa: "
                  + ("stop OK · " if oks else "stop NON confermato · ")
                  + ("mount in park · " if okh_m else f"park X ({deth_m}) · ")
                  + "OF2 chiuso.", thread=th)
        okr, detr = self.ac.reset_plan()          # come a fine notte (2026-07-08)
        if not okr:
            self.send(f"⚠️ RESET del piano non riuscito ({detr}): resettalo "
                      "dall'app prima della prossima sessione.", thread=th)
        # cooler: se spento va acceso e si aspetta il target (l'attesa vera la
        # fa il gate temperatura dell'agente, prima di far partire i flat)
        okt, temp, target, cooler, dett = self.ac.camera_cooling()
        if not okt:
            self.send(f"⚠️ Temperatura camera non leggibile ({dett}): "
                      "il gate temperatura riproverà prima dei flat.", thread=th)
        elif cooler is False:
            okc, detc = self.ac.cooler_on()
            if not okc:
                return annulla(f"cooler NON riacceso ({detc}): i flat vanno "
                               "fatti alla temperatura dei light.")
            oka, deta = self.ac.ensure_anti_dew()
            self.send(f"🧊 Cooler era SPENTO: acceso (camera {temp:.1f}°C, "
                      f"target {target:.1f}°C)"
                      + (", anti-dew ON." if oka else
                         f", ⚠️ anti-dew non verificato ({deta}).")
                      + "\nI flat partiranno solo a temperatura raggiunta.",
                      thread=th)
        else:
            self.send(f"🧊 Camera {temp:.1f}°C (target {target:.1f}°C), "
                      "cooler acceso.", thread=th)
        hv = int(self.ff.get("dew_heater_dry_pct", 100))
        okh, deth = self.ac.set_output("dew_heater", hv)
        if not okh:
            self.send(f"⚠️ Fascia anticondensa NON impostata al {hv}% ({deth}): "
                      "proseguo comunque.", thread=th)
        dry = int(self.ff.get("dry_wait_minutes", 30))
        self.send(f"✅ Richiesta presa in consegna dall'agente (entro 3 min).\n"
                  f"⏳ Asciugatura {dry} min a pannello chiuso"
                  + (f", fascia anticondensa al {hv}%" if okh else "")
                  + ", poi flat e dark dei filtri di stanotte e sync sul NAS.\n"
                  "A fine processo ti chiederò se spegnere tutto.", thread=th)

    def do_stopflat(self, th):
        """Bottone Stop Flat (richiesta utente 2026-08-15): interrompe l'ATTESA
        dei 30 minuti tra la fine del piano e i flat. Niente flat né dark: il
        rig resta com'è e torna in carico all'utente (il promemoria periodico
        di spegnere continua ad arrivare).
        Qui non si comanda NULLA all'ASIAIR — si scrive solo la richiesta per
        l'agente, che la esegue al ciclo successivo (entro 3 min): funziona
        anche con la VPN giù. Vale solo in fase 'drying': un autorun flat/dark
        già partito non si tocca."""
        stage = self.flat_stage()
        if stage != "drying":
            self.send("ℹ️ Niente da interrompere: "
                      + STOPFLAT_NO.get(stage, "nessuna attesa flat in corso."),
                      thread=th)
            return
        if self.fc_req.exists():
            self.send("ℹ️ Interruzione già richiesta: l'agente la prende in "
                      "carico entro pochi minuti.", thread=th)
            return
        try:
            self.fc_req.parent.mkdir(parents=True, exist_ok=True)
            self.fc_req.write_text(json.dumps(
                {"ts": datetime.now(self.tz).isoformat(), "source": "telegram"}))
        except Exception as e:
            self.send(f"⛔ Stop Flat NON riuscito: non riesco a scrivere la "
                      f"richiesta per l'agente ({e}). L'attesa prosegue.",
                      thread=th)
            return
        self.send("🛑 Interruzione richiesta: l'agente la prende in carico "
                  "entro 3 minuti e te lo conferma.\n"
                  "Niente flat né dark: il rig resta acceso e com'è, decidi tu. "
                  "Per spegnere: /sfro → Shutdown.", thread=th)

    def wait_ask_stage(self, th, timeout=40) -> bool:
        """Aspetta che lo stato dell'agente sia davvero in 'ask_shutdown': il
        messaggio con la domanda parte PRIMA che il ciclo salvi state.json,
        quindi un tap immediato arriverebbe con lo stato ancora vecchio."""
        t0 = time.time()
        while self.flat_stage() != "ask_shutdown":
            if time.time() - t0 > timeout:
                self.send("⌛ Richiesta non più valida: il flusso flat non è "
                          "in attesa di risposta.", thread=th)
                return False
            time.sleep(2)
        return True

    def do_flat_keep(self, th):
        """'No, lascia acceso' alla domanda di fine flat/dark."""
        if not self.wait_ask_stage(th):
            return
        self.flat_reply("keep", True, True, "")
        self.send("👍 Nessuno spegnimento: rig lasciato ACCESO.", thread=th)

    def do_flat_shutdown(self, th):
        """'Sì, spegni tutto' alla domanda di fine flat/dark: stesso Shutdown
        del menù, con l'esito comunicato all'agente (che chiude il flusso)."""
        if not self.wait_ask_stage(th):
            return
        self.flat_reply("shutdown", False, False, "in corso")
        ok = False
        try:
            ok = bool(self.do_shutdown(th))
        finally:
            self.flat_reply("shutdown", True, ok,
                            "" if ok else "shutdown non completato")

    # ---------------- Rsync ----------------
    def do_rsync(self, th):
        """Sync manuale ASIAIR->NAS (stessa sync_pass dell'agente: cartelle
        Light/Dark/Flat della share 'TF Images'). Operazione lunga: avviso a
        INIZIO e FINE con il conteggio dei file sincronizzati (2026-07-09).
        Dal 2026-07-26 i file arrivano sul NAS smistati per data (aaaammgg):
        essendo la stessa sync_pass, il bottone si comporta come l'agente."""
        sc = self.cfg.get("sync_module", {}) or {}
        if not sc.get("enabled"):
            self.send("⛔ Rsync: modulo sync disabilitato in config.", thread=th)
            return
        d = self.diagnose(with_kasa=False)
        if not d["asiair_ping"]:
            self.send("⛔ Rsync annullato: ASIAIR non raggiungibile "
                      + ("(VPN giù)." if d["vpn_up"] is False
                         else "(spento o in avvio)."), thread=th)
            return
        self.send("💾 Rsync ASIAIR→NAS avviato (Light/Dark/Flat, smistati "
                  "per data) — può richiedere alcuni minuti…", thread=th)
        r = A.sync_pass(sc, dry_run=False)
        if r.get("error"):
            self.send(f"⛔ Rsync in errore: {r['error']}", thread=th)
            return
        bk = r.get("by_kind") or {}
        det = " · ".join(f"{k} {bk.get(k, 0)}" for k in ("light", "flat", "dark"))
        self.send(f"✅ Rsync completato: {r.get('files', 0)} elementi "
                  f"sincronizzati ({det}).", thread=th)

    # ---------------- Shutdown ----------------
    def do_shutdown(self, th):
        """Ritorna True solo a spegnimento COMPLETO (ASIAIR giù + KASA OFF):
        lo usa il flusso flat manuale per riferire l'esito all'agente."""
        self.send("🔴 Shutdown avviato…", thread=th)
        d = self.diagnose()
        if d["kasa_ok"] is False:
            self.send(f"⛔ Shutdown annullato: KASA non raggiungibile "
                      f"({d['kasa_err']}): non potrei togliere corrente.", thread=th)
            return False
        steps = []
        if d["asiair_rpc"]:
            try:
                snap = self.ac.snapshot()
                if snap.get("capturing") or snap.get("plan_started"):
                    self.ac.stop()
                    steps.append("piano fermato")
                okf, detf = self.ac.close_flat()
                steps.append("OF2 chiuso" if okf else f"OF2 non chiuso ({detf})")
            except Exception as e:
                steps.append(f"pre-shutdown: {e}")
            # fascia anticondensa al valore di riposo PRIMA del pi_shutdown,
            # come a fine flusso flat (5% = "spenta"; 5 e non 0: con value 0
            # il firmware forza state=false). Non bloccante.
            hv = int(self.ff.get("dew_heater_end_pct", 5))
            okh, deth = self.ac.set_output("dew_heater", hv)
            steps.append(f"fascia anticondensa {hv}%" if okh
                         else f"fascia anticondensa NON a {hv}% ({deth})")
            ok, det = self.ac.shutdown()
            if not ok:
                self.send(f"⛔ Shutdown ASIAIR fallito ({det}). KASA lasciata ACCESA.",
                          thread=th)
                return False
            steps.append("pi_shutdown inviato")
            # KASA giu' SOLO quando l'ASIAIR ha davvero smesso di rispondere:
            # ping morto, oppure ping vivo ma app spenta (A.wait_asiair_down)
            down, mode, ddet = A.wait_asiair_down(
                self.ac.host, self.ac.port,
                float(self.ff.get("shutdown_wait_seconds", 15)),
                float(self.ff.get("shutdown_ping_timeout_seconds", 120)),
                float(self.ff.get("shutdown_grace_seconds", 90)))
            if not down:
                self.send(f"⛔ {ddet}: KASA lasciata ACCESA, intervieni tu.",
                          thread=th)
                return False
            if mode == "app":
                self.send(f"⚠️ {ddet}: tolgo comunque corrente (è l'unico modo).",
                          thread=th)
            steps.append("ping morto" if mode == "ping" else "app spenta")
        elif d["asiair_ping"]:
            self.send(f"⛔ Shutdown annullato: ASIAIR raggiungibile ma in ERRORE "
                      f"({d['rpc_err']}): non posso comandare pi_shutdown. "
                      "KASA lasciata accesa.", thread=th)
            return False
        else:
            if d["vpn_up"] is False:
                # senza VPN non si puo' verificare che il box sia davvero spento:
                # togliere corrente a un Pi acceso rischia di corrompere la SD
                self.send("⛔ Shutdown annullato: VPN GIÙ, non posso verificare lo "
                          "stato dell'ASIAIR prima di togliere corrente. "
                          "Ripristina la VPN e riprova.", thread=th)
                return False
            if d["children"] and all(c["state"] == 0 for c in d["children"]):
                self.send("ℹ️ Sistema GIÀ spento (ASIAIR non risponde, KASA tutta OFF).",
                          thread=th)
                return True
            steps.append("ASIAIR già spento")
        n_off, failed, err = A._kasa_power_off_all(d["kc"], d["dev"], False)
        if failed or (n_off == 0 and err):
            self.send(f"⛔ Spegnimento KASA incompleto ({failed or err}).", thread=th)
            return False
        try:
            self.marker.parent.mkdir(parents=True, exist_ok=True)
            self.marker.write_text(json.dumps(
                {"ts": datetime.now(self.tz).isoformat(), "source": "telegram"}))
        except Exception as e:
            log.warning("marker shutdown manuale: %s", e)
        self.send("✅ Shutdown COMPLETATO: " + ", ".join(steps)
                  + f" → KASA tutta OFF ({n_off} prese).\n"
                  "L'agente non riaccenderà fino alla prossima notte "
                  "(o a uno Start dal menù).", thread=th)
        return True


def main():
    ap = argparse.ArgumentParser(description="Bot Telegram /sfro (menu ASIAIR/KASA)")
    ap.add_argument("--config", default=str(HERE / "config.yaml"))
    ap.add_argument("--selftest", action="store_true",
                    help="verifica config/credenziali ed esce (nessuna rete)")
    args = ap.parse_args()
    cfg = A.load_config(Path(args.config))
    A.setup_logging(cfg.get("log_level", "INFO"))
    if not (cfg.get("telegram_bot", {}) or {}).get("enabled", True):
        log.warning("telegram_bot.enabled = false: esco.")
        return
    bot = SfroBot(cfg)
    if args.selftest:
        print(json.dumps({"token_ok": bool(bot.token), "chat_id_ok": bool(bot.chat_id),
                          "asiair_host": bot.ac.host, "marker": str(bot.marker)}))
        return
    if not bot.token or not bot.chat_id:
        log.error("telegram.txt senza bot_token/chat_id: impossibile partire.")
        return
    log.info("Bot /sfro in ascolto (chat %s)", bot.chat_id)
    bot.run()


if __name__ == "__main__":
    main()
