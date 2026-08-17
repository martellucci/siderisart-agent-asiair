#!/usr/bin/env python3
"""
sfro_agent.py
-------------
Agente SFRO per server Linux. Esecuzione via systemd timer ogni 3 minuti.

Stato del tetto = API ASCOM Alpaca UFFICIALE di SFRO (non bortle, non scraping):
  GET {alpaca_base}/api/v1/safetymonitor/{device_number}/issafe
  -> {"Value": true|false, "ErrorNumber": 0, ...}
     Value true  = safe   = tetto APERTO
     Value false = unsafe = tetto CHIUSO
  HTTPS pubblico: nessuna VPN per leggere il tetto.

Cosa fa:
  - Dopo il tramonto in Texas, legge issafe del building (device_number).
  - Su APERTURA (CHIUSO->APERTO): avvisa Telegram e ACCENDE la presa Kasa (cloud).
  - Su CHIUSURA durante la sessione: avvisa Telegram.
  - A FINE sessione (alba in Texas): avvisa Telegram.
  - Spegnimento Kasa MANUALE; promemoria Telegram ogni 10 min finche' accesa.
  - (Opzionale) Sync incrementale immagini ASIAIR->NAS (CIFS+rsync) mentre la Kasa
    e' accesa. La VPN NON e' gestita qui (sempre attiva sul router DR7).

Credenziali da file di testo nella stessa cartella:
  kasa.txt (username/password), telegram.txt (bot_token/chat_id/thread_id),
  asiair.txt (host/port, per il client di avvio ASIAIR).
Credenziali SMB per il mount: file CIFS separato (credentials=...).

Dipendenze: requests, astral, PyYAML.
"""

import argparse
import json
import logging
import os
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml
from astral import LocationInfo
from astral.sun import dawn, dusk

# Client di controllo ASIAIR (stesso pacchetto). Se assente, l'avvio piano e'
# semplicemente disattivato (l'agente continua a fare tetto/Kasa/Telegram/sync).
try:
    from asiair_client import AsiairClient
except Exception:  # pragma: no cover
    AsiairClient = None

# Storico sessioni (FITS -> SQLite -> Google Sheets). Opzionale: se il modulo o
# le sue dipendenze mancano, l'agente continua senza log storico.
try:
    import sfro_sessionlog as SL
except Exception:  # pragma: no cover
    SL = None

HERE = Path(__file__).resolve().parent
log = logging.getLogger("sfro-agent")

# Fasi del flusso flat durante le quali (e dopo le quali, fino alla notte
# successiva) la logica normale del ciclo NON deve girare: riaccenderebbe la
# KASA appena spenta o riavvierebbe il piano. 'ask_shutdown' = flusso manuale
# in attesa della risposta sullo spegnimento (2026-08-01).
# 'cancelled' (stop dell'attesa dal menu, 2026-08-15) e 'error' NON sono qui di
# proposito: il flusso e' finito e il promemoria "spegni tu" deve tornare a
# uscire; restano pero' valori PIENI, cosi' i rami che all'alba rifarebbero
# teardown+flat (pretendono "nessuna fase flat") non ripartono da soli.
FLAT_STAGES_BUSY = ("drying", "running", "darks", "ask_shutdown", "done")


# --------------------------------------------------------------------------- #
# Config e credenziali
# --------------------------------------------------------------------------- #
def setup_logging(level: str) -> None:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    log.addHandler(h)
    log.setLevel(getattr(logging, level.upper(), logging.INFO))


def load_config(path: Path) -> dict:
    if not path.exists():
        print(f"Config non trovato: {path}", file=sys.stderr)
        sys.exit(2)
    with open(path) as f:
        return yaml.safe_load(f) or {}


def load_kv_file(path: Path) -> dict:
    out = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def resolve_files(cfg: dict) -> dict:
    files = cfg.get("files", {})
    return {
        "kasa": HERE / files.get("kasa", "kasa.txt"),
        "telegram": HERE / files.get("telegram", "telegram.txt"),
        "asiair": HERE / files.get("asiair", "asiair.txt"),
    }


# --------------------------------------------------------------------------- #
# Stato persistente
# --------------------------------------------------------------------------- #
def load_state(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception as e:
            log.warning("Stato illeggibile (%s), riparto da vuoto", e)
    return {}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(path)


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def http_get_json(url: str, http_cfg: dict):
    last = None
    for i in range(http_cfg.get("retries", 2) + 1):
        try:
            r = requests.get(url, timeout=http_cfg.get("timeout_seconds", 15))
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            if i < http_cfg.get("retries", 2):
                time.sleep(http_cfg.get("retry_backoff_seconds", 3))
    log.warning("GET fallita %s: %s", url, last)
    return None


# --------------------------------------------------------------------------- #
# Stato tetto via ASCOM Alpaca
# --------------------------------------------------------------------------- #
def alpaca_issafe_url(roof_cfg: dict) -> str:
    base = roof_cfg["alpaca_base"].rstrip("/")
    n = roof_cfg["device_number"]
    return f"{base}/api/v1/safetymonitor/{n}/issafe"


def parse_alpaca(data) -> str:
    """Ritorna 'OPEN' (safe), 'CLOSED' (unsafe) o 'UNKNOWN' (errore/irraggiungibile)."""
    if not isinstance(data, dict):
        return "UNKNOWN"
    if data.get("ErrorNumber", 0) not in (0, None):
        return "UNKNOWN"
    v = data.get("Value")
    if v is True:
        return "OPEN"
    if v is False:
        return "CLOSED"
    return "UNKNOWN"


# --------------------------------------------------------------------------- #
# Astronomia (coordinate dal config)
# --------------------------------------------------------------------------- #
def nautical_window(now: datetime, loc_cfg: dict, tz: ZoneInfo, depression: float = 12.0):
    """Finestra di NOTTE NAUTICA (sole sotto i `depression`°, default 12).
    Ritorna (in_window, start, end, night_id):
      - in_window: True se `now` è dentro la notte nautica in corso;
      - start/end: estremi della notte in corso (se in_window) o della PROSSIMA;
      - night_id: identificatore della notte (data del crepuscolo serale) per
        distinguere una notte dall'altra nello stato.
    La notte va dal crepuscolo nautico SERALE (dusk) all'alba nautica del mattino
    DOPO (dawn). In località/stagioni senza crepuscolo nautico, astral solleva:
    in quel caso si considera 'sempre notte' nella finestra buio."""
    obs = LocationInfo(latitude=loc_cfg["latitude"], longitude=loc_cfg["longitude"]).observer

    def _dawn(d):
        return dawn(obs, date=d, tzinfo=tz, depression=depression)

    def _dusk(d):
        return dusk(obs, date=d, tzinfo=tz, depression=depression)

    today = now.date()
    try:
        dawn_today = _dawn(today)
        dusk_today = _dusk(today)
    except ValueError:
        # niente crepuscolo nautico (notti polari ecc.): non vincoliamo
        return True, None, None, today.isoformat()

    if now < dawn_today:
        # siamo nella notte iniziata IERI sera
        start = _dusk(today - timedelta(days=1))
        end = dawn_today
        return True, start, end, start.date().isoformat()
    if now >= dusk_today:
        # notte che inizia STASERA
        start = dusk_today
        end = _dawn(today + timedelta(days=1))
        return True, start, end, start.date().isoformat()
    # giorno: prossima notte = stasera
    return False, dusk_today, _dawn(today + timedelta(days=1)), dusk_today.date().isoformat()


# --------------------------------------------------------------------------- #
# Logica pura (testabile): notifiche + intenzione accensione
# --------------------------------------------------------------------------- #
class Telegram:
    def __init__(self, conf: dict, enabled: bool, timeout: int = 15):
        self.token = conf.get("bot_token", "")
        self.chat_id = conf.get("chat_id", "")
        self.thread_id = conf.get("thread_id", "")
        self.enabled = enabled and bool(self.token) and bool(self.chat_id)
        self.timeout = timeout

    def send(self, text: str, keyboard=None) -> None:
        """Messaggio in chat. Con `keyboard` (lista di righe di bottoni inline)
        il messaggio porta i bottoni: le callback le riceve il bot persistente
        sfro_telegram.py (l'agente e' un oneshot e non fa polling)."""
        if not self.enabled:
            log.info("[telegram disabilitato] %s", text)
            return
        payload = {"chat_id": self.chat_id, "text": text}
        if self.thread_id:
            payload["message_thread_id"] = self.thread_id
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json=payload, timeout=self.timeout)
            if r.status_code != 200:
                log.warning("Telegram HTTP %s: %s", r.status_code, r.text[:200])
        except Exception as e:
            log.warning("Invio Telegram fallito: %s", e)


# --------------------------------------------------------------------------- #
# Cloud TP-Link Kasa (lettura stato + accensione; mai spegnimento)
# --------------------------------------------------------------------------- #
class KasaCloud:
    def __init__(self, cfg: dict, creds: dict, http_cfg: dict, terminal_uuid: str,
                 token_file: str = ""):
        self.base = cfg.get("cloud_base", "https://wap.tplinkcloud.com").rstrip("/")
        self.username = creds.get("username", "")
        self.password = creds.get("password", "")
        self.timeout = http_cfg.get("timeout_seconds", 15)
        self.tu = terminal_uuid
        self.token = None
        # CACHE DEL TOKEN (2026-08-15): fino a ieri ogni ciclo faceva un login
        # completo al cloud TP-Link, e con l'agente passato a 3 minuti sarebbero
        # ~480 autenticazioni al giorno — il tipo di traffico che i cloud
        # consumer iniziano a limitare. Il token e' materiale di AUTENTICAZIONE:
        # sta in un file dedicato a permessi 600, non nello stato, e NON va nei
        # backup (si rigenera da solo da kasa.txt). Se il cloud lo rifiuta,
        # _kasa_connect rifa' il login una volta sola.
        self.token_file = Path(token_file) if token_file else None
        self.token_ttl = float(cfg.get("token_ttl_hours", 12)) * 3600
        self.token_cached = False    # il token in uso viene dalla cache?

    def _token_read(self):
        """Token dalla cache, o None se assente/scaduto/di un altro utente."""
        if not self.token_file:
            return None
        try:
            d = json.loads(self.token_file.read_text())
            if d.get("user") != self.username:
                return None          # credenziali cambiate: cache non valida
            if time.time() - float(d.get("ts", 0)) >= self.token_ttl:
                return None
            return d.get("token") or None
        except Exception:
            return None              # cache illeggibile: si rifa' il login

    def _token_write(self, token):
        """Scrittura atomica a permessi 600 (mai un file di token leggibile a
        tutti, nemmeno per l'istante fra creazione e chmod)."""
        if not self.token_file:
            return
        tmp = self.token_file.with_suffix(".tmp")
        try:
            self.token_file.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                json.dump({"token": token, "user": self.username,
                           "ts": time.time()}, fh)
            os.replace(tmp, self.token_file)
        except Exception as e:
            log.debug("cache token Kasa non scritta: %s", e)   # best-effort

    def token_forget(self):
        """Butta il token in cache (rifiutato dal cloud): il login successivo
        ne chiede uno nuovo."""
        self.token, self.token_cached = None, False
        if self.token_file:
            try:
                self.token_file.unlink(missing_ok=True)
            except Exception as e:
                log.debug("cache token Kasa non rimossa: %s", e)

    def _post(self, url, payload):
        r = requests.post(url, json=payload, timeout=self.timeout,
                          headers={"Content-Type": "application/json"})
        r.raise_for_status()
        d = r.json()
        if d.get("error_code", 0) != 0:
            raise RuntimeError(f"Kasa error {d.get('error_code')}: {d.get('msg')}")
        return d["result"]

    def login(self, force: bool = False):
        """Token dalla cache se ancora buono, altrimenti login vero.
        `force` salta la cache (la usa il retry dopo un token rifiutato)."""
        if not self.username or not self.password:
            raise RuntimeError("Credenziali Kasa mancanti (kasa.txt)")
        if not force:
            tok = self._token_read()
            if tok:
                self.token, self.token_cached = tok, True
                return
        res = self._post(self.base + "/", {"method": "login", "params": {
            "appType": "Kasa_Android", "cloudUserName": self.username,
            "cloudPassword": self.password, "terminalUUID": self.tu}})
        self.token, self.token_cached = res["token"], False
        self._token_write(self.token)

    def list_devices(self):
        return self._post(f"{self.base}/?token={self.token}",
                          {"method": "getDeviceList"}).get("deviceList", [])

    def resolve(self, device_id, alias):
        for d in self.list_devices():
            if device_id and d.get("deviceId") == device_id:
                return d
            if alias and d.get("alias") == alias:
                return d
        raise RuntimeError(f"Presa non trovata (id='{device_id}', alias='{alias}')")

    def _pass(self, dev, req):
        url = f"{(dev.get('appServerUrl') or self.base)}/?token={self.token}"
        res = self._post(url, {"method": "passthrough", "params": {
            "deviceId": dev["deviceId"], "requestData": json.dumps(req)}})
        return json.loads(res["responseData"])

    def get_sysinfo(self, dev):
        return self._pass(dev, {"system": {"get_sysinfo": {}}})["system"]["get_sysinfo"]

    def children(self, dev):
        """Prese di una power strip (es. KP303): [{id, alias, state}].
        Lista VUOTA per le prese a relè singolo."""
        try:
            si = self.get_sysinfo(dev)
        except Exception:
            return []
        return [{"id": c.get("id"), "alias": c.get("alias"),
                 "state": int(c.get("state", 0))} for c in si.get("children", [])]

    def relay_state(self, dev):
        """Stato del relè per prese SINGOLE (None se è una strip o errore)."""
        try:
            return int(self.get_sysinfo(dev)["relay_state"])
        except Exception:
            return None

    def set_outlet(self, dev, child_id, on: bool):
        """Accende/spegne una presa. child_id=None per relè singolo;
        per le strip si usa il context con il child_id."""
        req = {"system": {"set_relay_state": {"state": 1 if on else 0}}}
        if child_id:
            req = {"context": {"child_ids": [child_id]}, **req}
        self._pass(dev, req)

    def power_on(self, dev, child_id=None):  # retro-compatibile
        self.set_outlet(dev, child_id, True)


# --------------------------------------------------------------------------- #
# Controllo PIANO ASIAIR (canale 4700). Avvio = start_exposure, stop = stop_exposure.
# Esegue il piano/sequenza ATTIVO dell'ASIAIR (col mount fa anche il goto).
# --------------------------------------------------------------------------- #
class AsiairControl:
    # Canale di ciascun device: 4700 (imager) o 4400 (guider).
    # camera/EAF/EFW = open_* su 4700; mount/guida = set_connected su 4400.
    DEVICE_CHANNEL = {"camera": 4700, "focuser": 4700, "wheel": 4700,
                      "mount": 4400, "guide": 4400}

    def __init__(self, cfg: dict):
        self.enabled = bool(cfg.get("enabled", False))
        self.host = cfg.get("host", "")
        self.port = int(cfg.get("port", 4700))            # imager/plan
        self.guider_port = int(cfg.get("guider_port", 4400))  # mount/guider
        self.timeout = float(cfg.get("timeout_seconds", 8))
        self.auto_start = bool(cfg.get("auto_start", True))
        self.auto_stop = bool(cfg.get("auto_stop", True))
        # I 5 device che DEVONO risultare connessi (li connette l'UTENTE dall'app:
        # l'agente NON li connette, li VERIFICA soltanto — vedi snapshot()).
        self.required_devices = cfg.get("required_devices",
                                        ["camera", "focuser", "wheel", "mount", "guide"])
        # TEARDOWN: metodi mount-home e cooler-off. NON sono ancora stati catturati
        # dall'app (vanno sniffati a ASIAIR libero) -> configurabili e, se assenti,
        # l'agente NON inventa il comando: salta il passo e lo segnala. Forma:
        #   mount_home:  {port: 4400, method: "<nome>", params: [...]}
        #   cooler_off:  {port: 4700, method: "set_control_value", params: ["CoolerOn", 0]}
        self.mount_home_cmd = cfg.get("mount_home") or {}
        self.cooler_off_cmd = cfg.get("cooler_off") or {}
        # shutdown ASIAIR (pi_shutdown, catturato dall'app 2026-07-03)
        self.shutdown_cmd = cfg.get("shutdown") or {}
        # pagina dell'autorun flat: start_exposure agisce sul CONTESTO corrente
        # (l'avvio piano fa set_page["plan"]); nome da verificare al primo uso
        self.flat_page = cfg.get("flat_page", "autorun")
        self.mount_list_index = int(cfg.get("mount_list_index", 1))
        # Flat panel OF2 = uscita di potenza 'flat_panel' (pi_output_set2/get2).
        # state:true=ON=CHIUSO, state:false=OFF=APERTO. Va APERTO come ULTIMO passo
        # prima dell'avvio, poi si attende il tempo del motore.
        fp = cfg.get("flat_panel", {})
        self.flat_enabled = bool(fp.get("enabled", False))
        self.flat_open_wait = float(fp.get("open_wait_seconds", 7))
        self.flat_brightness = fp.get("brightness", 5)
        # Letture di pi_output_get2 (pannello flat E fascia anticondensa): quanti
        # tentativi e con che pausa. Vedi _output_state — il 2026-08-12 UNA sola
        # risposta storta ha annullato l'intera sessione di flat.
        self.out_read_tries = int(cfg.get("output_read_tries",
                                          fp.get("read_tries", 3)))
        self.out_read_wait = float(cfg.get("output_read_wait_seconds",
                                           fp.get("read_wait_seconds", 3)))
        self.out_read_detail = ""       # motivo dell'ultima lettura fallita
        # pre-avvio piano (2026-07-19): fascia anticondensa (output 4) a
        # questo valore
        self.start_dew_heater_pct = int(cfg.get("start_dew_heater_pct", 100))
        # gate posizione: avvia solo se il mount è alle coord attese (Texas)
        self.expected_lat = cfg.get("expected_lat")
        self.expected_lon = cfg.get("expected_lon")
        self.pos_tol = float(cfg.get("position_tolerance_deg", 0.2))
        # default False: il gate VERIFICA la posizione reale (non la forza).
        self.set_location = bool(cfg.get("set_location", False))

    @property
    def available(self) -> bool:
        return bool(self.enabled and AsiairClient and self.host)

    def _client(self):
        return AsiairClient(self.host, self.port, timeout=self.timeout)

    def snapshot(self) -> dict:
        """Stato ASIAIR (fonte di verità: il protocollo, l'app non si auto-refresha).
        4700: reachable, capturing, plan_started, has_plan, plan_name, cam_open,
        focuser_connected, wheel_connected, flat_open. 4400: mount/guide + posizione.
        I 5 *_connected/cam_open servono a VERIFICARE che l'utente abbia collegato i
        device dall'app (l'agente non li connette)."""
        info = {"reachable": False, "capturing": None, "has_plan": False,
                "plan_name": None, "cam_open": None,
                "focuser_connected": None, "wheel_connected": None}
        if not self.available:
            return info
        try:
            with self._client() as c:
                r, _ = c.call("get_app_state", [], max_wait=self.timeout)
                st = r.get("result", {})
                cap = st.get("capture", {}) if isinstance(st, dict) else {}
                info["reachable"] = True
                info["capturing"] = bool(cap.get("is_working"))
                info["capture_state"] = cap.get("state")  # es. first_delay/expose
                cs, _ = c.call("get_camera_state", [], max_wait=self.timeout)
                cstate = cs.get("result", {})
                info["cam_open"] = (isinstance(cstate, dict)
                                    and cstate.get("state") not in ("close", None))
                # EAF/EFW: get_*_info/state danno 'state'!=close se connessi (da
                # scollegati: code 0 con state 'close' per il focuser).
                fr, _ = c.call("get_focuser_info", [], max_wait=self.timeout)
                info["focuser_connected"] = (fr.get("code") == 0 and (fr.get("result") or {})
                                             .get("state") not in ("close", None))
                wr, _ = c.call("get_wheel_state", [], max_wait=self.timeout)
                info["wheel_connected"] = (wr.get("code") == 0 and (wr.get("result") or {})
                                           .get("state") not in ("close", None))
                # snapshot = lettura di routine a ogni ciclo: un solo tentativo,
                # niente attese (i ritentativi servono a chi deve AGIRE sul pannello)
                _, fo = self._flat_state(c, tries=1)  # flat panel: aperto = state false
                info["flat_open"] = (not bool(fo.get("state"))) if fo else None
                rp, _ = c.call("list_plan", [], max_wait=self.timeout)
                plans = rp.get("result") or []
                info["has_plan"] = len(plans) > 0
                if plans:
                    info["plan_name"] = plans[0].get("plan_name")
                # get_enabled_plan: flag autorevole 'is_plan_started' (avvio reale,
                # affidabile col FirstDelay dove capture.is_working è ingannevole).
                ep, _ = c.call("get_enabled_plan", [], max_wait=self.timeout)
                eplans = ep.get("result") or []
                info["plan_started"] = any(p.get("is_plan_started") for p in eplans)
        except Exception as e:
            log.info("ASIAIR non raggiungibile/illeggibile: %s", e)
        # stato mount + posizione sul canale guider (4400)
        try:
            with AsiairClient(self.host, self.guider_port, timeout=self.timeout) as g:
                gc, _ = g.call("get_connected", [True], max_wait=self.timeout)
                gres = gc.get("result") or {}
                info["mount_connected"] = bool(gres.get("mount"))
                info["guide_connected"] = bool(gres.get("camera"))
                si, _ = g.call("scope_get_info", [], max_wait=self.timeout)
                res = si.get("result") or {}
                info["lat"], info["lon"] = res.get("Lat"), res.get("Lon")
        except Exception:
            info["mount_connected"] = None
        return info

    def all_connected(self, snap: dict) -> bool:
        """True se TUTTI i device richiesti risultano connessi nello snapshot.
        Li collega l'UTENTE dall'app: l'agente VERIFICA soltanto, non connette."""
        return not self.missing_devices(snap)

    _DEV_KEY = {"camera": "cam_open", "focuser": "focuser_connected",
                "wheel": "wheel_connected", "mount": "mount_connected",
                "guide": "guide_connected"}
    _DEV_LABEL = {"camera": "camera", "focuser": "EAF", "wheel": "EFW",
                  "mount": "mount", "guide": "guida"}

    def missing_devices(self, snap: dict) -> list:
        """Etichette dei device richiesti NON connessi (per i messaggi di attesa)."""
        return [self._DEV_LABEL[d] for d in self.required_devices
                if d in self._DEV_KEY and not snap.get(self._DEV_KEY[d])]

    def _output_state(self, c, type_name: str = "flat_panel",
                      tries: int = None, wait_s: float = None):
        """(idx, entry) dell'uscita `type_name` da pi_output_get2 (canale 4700),
        oppure (None, None). Per il flat panel entry.state: true=ON=CHIUSO,
        false=OFF=APERTO.

        RITENTA (2026-08-12): quel giorno UNA sola risposta priva dell'uscita ha
        mandato in errore il teardown all'ultimo passo — piano fermato, cooler
        spento e mount gia' in home — annullando l'intera sessione di flat e
        lasciando il pannello APERTO e il rig acceso. Una lettura storta non
        deve costare la notte. Vale per TUTTE le uscite: la stessa lettura
        governa anche la fascia anticondensa (set_output).
        Il motivo dell'ultimo tentativo fallito resta in self.out_read_detail:
        serve a NON confondere "l'uscita non c'e'" con "la chiamata e' fallita",
        che prima davano lo stesso messaggio."""
        tries = self.out_read_tries if tries is None else int(tries)
        wait_s = self.out_read_wait if wait_s is None else float(wait_s)
        for n in range(1, max(1, tries) + 1):
            try:
                r, _ = c.call("pi_output_get2", [], max_wait=self.timeout)
            except Exception as e:
                self.out_read_detail = f"pi_output_get2 non ha risposto ({e})"
            else:
                outs = r.get("result")
                if isinstance(outs, list):
                    for i, o in enumerate(outs):
                        if isinstance(o, dict) and o.get("type") == type_name:
                            if n > 1:
                                log.info("uscita '%s': letta al tentativo %d/%d",
                                         type_name, n, tries)
                            return i, o
                    tipi = ", ".join(str(o.get("type")) for o in outs
                                     if isinstance(o, dict)) or "nessuna"
                    self.out_read_detail = (f"pi_output_get2 ha risposto con "
                                            f"{len(outs)} uscite ({tipi}): "
                                            f"manca '{type_name}'")
                else:
                    self.out_read_detail = (
                        f"pi_output_get2 code {r.get('code')}"
                        + (f" {r.get('error')}" if r.get("error") else "")
                        + " (nessuna lista di uscite)")
            if n < tries:
                log.warning("uscita '%s': %s — ritento (%d/%d)",
                            type_name, self.out_read_detail, n, tries)
                time.sleep(wait_s)
        log.error("uscita '%s': lettura fallita dopo %d tentativi: %s",
                  type_name, tries, self.out_read_detail)
        return None, None

    def _flat_state(self, c, tries: int = None, wait_s: float = None):
        """Scorciatoia storica per l'uscita del pannello flat."""
        return self._output_state(c, "flat_panel", tries, wait_s)

    def open_flat(self, c):
        """Apre il flat panel (state=false). Ritorna (ok, changed, detail).
        'changed' True se ha mosso il motore (allora va atteso flat_open_wait)."""
        idx, o = self._flat_state(c)
        if o is None:
            return False, False, f"flat_panel non letto: {self.out_read_detail}"
        if not bool(o.get("state")):
            return True, False, ""  # già aperto (OFF)
        params = {f"port{idx}": {"is_pwm": bool(o.get("is_pwm", True)),
                                 "value": o.get("value", self.flat_brightness),
                                 "state": False, "type": "flat_panel"}}
        r, _ = c.call("pi_output_set2", params, max_wait=self.timeout)
        if r.get("code") != 0:
            return False, False, f"pi_output_set2 code {r.get('code')}: {r.get('error')}"
        return True, True, ""

    def check_position(self):
        """GATE POSIZIONE (canale 4400). VERIFICA che il mount sia alle coord
        attese (Texas): legge scope_get_info e confronta lat/lon con la tolleranza.
        NON imposta nulla, a meno di set_location:true (sconsigliato: renderebbe
        la verifica banale e sovrascriverebbe la posizione reale).
        Ritorna (ok, lat, lon, detail). Senza coord attese, non vincola (ok=True)."""
        if self.expected_lat is None or self.expected_lon is None:
            return True, None, None, ""
        elat, elon = float(self.expected_lat), float(self.expected_lon)
        with AsiairClient(self.host, self.guider_port, timeout=self.timeout) as g:
            if self.set_location:  # opzionale, default OFF
                g.call("scope_set_location", [elat, elon], max_wait=self.timeout)
            r, _ = g.call("scope_get_info", [], max_wait=self.timeout)
            res = r.get("result") or {}
            lat, lon = res.get("Lat"), res.get("Lon")
            if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
                return False, lat, lon, "posizione mount non leggibile (scope_get_info)"
            if abs(lat - elat) > self.pos_tol or abs(lon - elon) > self.pos_tol:
                return (False, lat, lon,
                        f"posizione mount FUORI tolleranza: lat={lat:.4f} lon={lon:.4f} "
                        f"(atteso {elat:.4f},{elon:.4f}, tol {self.pos_tol}°)")
            return True, lat, lon, ""

    def start(self, snap: dict = None):
        """Avvia il piano ATTIVO. NON connette i device (li collega l'utente da app):
        VERIFICA che siano tutti connessi (dallo snapshot), poi verifica la posizione
        (Texas), accende l'anti-dew della camera e porta la fascia anticondensa
        al 100% (2026-07-19, non bloccanti), apre OF2 e fa set_page(plan)+
        start_exposure["light"]. Parte SOLO se TUTTI i device richiesti sono
        connessi. Ritorna (ok, detail): con ok=True il detail e' l'esito del
        pre-avvio anti-dew/fascia anticondensa, da accodare al messaggio
        Telegram."""
        if snap is None:
            snap = self.snapshot()
        missing = self.missing_devices(snap)
        if missing:
            return False, f"device non connessi: {', '.join(missing)}"
        # GATE POSIZIONE: non avviare se il mount non è alle coord attese (Texas)
        okp, lat, lon, det = self.check_position()
        if not okp:
            return False, det
        # PRE-AVVIO (2026-07-19; dal 2026-08-16 include il COOLER e sta in
        # prepare(), condiviso con l'inizializzazione al T-lead). Errori NON
        # bloccanti, riportati nel detail che il chiamante accoda al messaggio.
        prep_txt = self.prepare()
        # avvio del piano sul canale imager (4700)
        with self._client() as c:
            # ULTIMO passo: apri il flat panel OF2 e attendi il motore prima di avviare
            if self.flat_enabled:
                ok, changed, det = self.open_flat(c)
                if not ok:
                    return False, f"flat panel: {det}"
                if changed:
                    time.sleep(self.flat_open_wait)        # tempo del motore (~7s)
                    _, o2 = self._flat_state(c)
                    if o2 is None or bool(o2.get("state")):
                        return False, "flat panel ancora CHIUSO dopo l'apertura"
            c.call("set_page", ["plan"], max_wait=self.timeout)
            # AVVIO PIANO: l'app chiama start_exposure CON ["light"] (NON params vuoti):
            # avvia l'autorun del piano attivo (le light). Con [] non parte l'autorun.
            r, _ = c.call("start_exposure", ["light"], max_wait=self.timeout)
            if r.get("code") != 0:
                return False, f"start_exposure code {r.get('code')}: {r.get('error')}"
            # VERIFICA reale via get_enabled_plan -> is_plan_started:true (il flag che
            # usa l'app; affidabile anche col FirstDelay, dove is_working è ingannevole).
            # start_exposure torna code 0 anche se il piano è concluso/vuoto e NON parte.
            started = False
            for _ in range(6):
                time.sleep(1)
                a, _ = c.call("get_enabled_plan", [], max_wait=self.timeout)
                if any(p.get("is_plan_started") for p in (a.get("result") or [])):
                    started = True
                    break
            if not started:
                return False, ("start_exposure OK ma il piano NON risulta avviato "
                               "(is_plan_started=false): piano concluso/senza light "
                               "attivi → controlla/RESETTA il piano sull'ASIAIR")
        # ok: il detail riporta l'esito del pre-avvio (anti-dew + fascia
        # anticondensa)
        return True, prep_txt

    def stop(self) -> bool:
        with self._client() as c:
            r, _ = c.call("stop_exposure", [], max_wait=self.timeout)
            return r.get("code") == 0

    def _plan_dirty(self, c):
        """Progresso residuo del PIANO (get_plan, canale 4700): lista con una
        riga per ogni target ABILITATO che ha progresso da azzerare (lapsed>0
        o left_time<total_time); [] = piano già azzerato; None = get_plan non
        leggibile (code!=0 o nessun piano)."""
        r, _ = c.call("get_plan", [], max_wait=self.timeout)
        plans = r.get("result") or []
        if r.get("code") != 0 or not plans:
            return None
        dirty = []
        for t in plans[0].get("targets", []):
            if not t.get("enable"):
                continue
            lapsed = sum(s.get("lapsed", 0) for s in t.get("seqs", []))
            tot, left = t.get("total_time_sec"), t.get("left_time_sec")
            if lapsed > 0 or (tot or 0) > (left or 0):
                dirty.append(f"{t.get('target_name')}: {lapsed} pose fatte, "
                             f"restano {left}/{tot}s")
        return dirty

    def plan_left(self) -> tuple:
        """Lavoro RESIDUO del PIANO (get_plan, canale 4700): (ok, left_sec, detail).
        left_sec = somma di `left_time_sec` sui target ABILITATI; 0 = piano
        ESAURITO, cioe' tutte le pose previste sono state fatte.
        Serve a distinguere un piano CONCLUSO da uno interrotto per errore:
        fino al 2026-08-15 il piano fermo a tetto aperto faceva scattare in
        entrambi i casi l'avviso «FERMATO in modo non previsto» (segnalato
        dall'utente: pose esaurite, avviso di errore alle 12:45).
        ok=False se get_plan non e' leggibile o non ci sono target abilitati:
        in quel caso il chiamante non puo' concludere nulla."""
        if not self.available:
            return False, None, "ASIAIR non configurato"
        try:
            with self._client() as c:
                r, _ = c.call("get_plan", [], max_wait=self.timeout)
                plans = r.get("result") or []
                if r.get("code") != 0 or not plans:
                    return False, None, (f"get_plan code {r.get('code')}"
                                         + (" (nessun piano)" if not plans else ""))
                targets = [t for t in plans[0].get("targets", [])
                           if t.get("enable")]
                if not targets:
                    return False, None, "nessun target abilitato nel piano"
                left = sum(max(0, int(t.get("left_time_sec") or 0))
                           for t in targets)
                fatti = [t for t in targets
                         if int(t.get("left_time_sec") or 0) <= 0]
                return True, left, f"{len(fatti)}/{len(targets)} target completati"
        except Exception as e:
            return False, None, str(e)

    def reset_plan(self) -> tuple:
        """RESET del progresso del PIANO a fine notte (richiesta utente
        2026-07-08): un piano interrotto (stop all'alba, fermo impianto) NON
        riparte con start_exposure finché non viene resettato — visto live la
        notte 07/07-08/07: tetto aperto ma piano fermo fino al reset manuale
        dall'app (prima posa 23:09 TX invece di ~21:45).
        Il comando di reset dell'app non è mai stato catturato: si prova
        reset_sequence_progress sulla pagina 'plan' (i comandi agiscono sulla
        pagina corrente, come start_exposure) e poi nomi alternativi — code
        103 = metodo inesistente, innocuo (oracolo discovery). L'esito è
        VERIFICATO rileggendo get_plan: target abilitati con lapsed 0 e
        left==total. Ritorna (ok, detail)."""
        if not self.available:
            return False, "ASIAIR non configurato"
        candidates = ("reset_sequence_progress", "reset_plan_progress",
                      "reset_plan", "clear_plan_progress")
        try:
            with self._client() as c:
                before = self._plan_dirty(c)
                if before is None:
                    return False, "get_plan non leggibile"
                if not before:
                    return True, "già azzerato"
                c.call("set_page", ["plan"], max_wait=self.timeout)
                tried = []
                for name in candidates:
                    r, _ = c.call(name, [], max_wait=self.timeout)
                    code = r.get("code")
                    tried.append(f"{name}={code}")
                    if code != 0:
                        continue
                    # il ricalcolo di left_time è rapido ma non istantaneo
                    for _ in range(5):
                        time.sleep(1)
                        if self._plan_dirty(c) == []:
                            return True, (f"azzerato con {name} "
                                          f"({'; '.join(before)})")
                return False, ("progresso NON azzerato (" + "; ".join(before)
                               + ") — tentativi: " + ", ".join(tried))
        except Exception as e:
            return False, str(e)

    def close_flat(self) -> tuple:
        """Chiude il flat panel OF2 (state=true=ON=CHIUSO). Ritorna (ok, detail)."""
        try:
            with self._client() as c:
                idx, o = self._flat_state(c)
                if o is None:
                    return False, f"flat_panel non letto: {self.out_read_detail}"
                if bool(o.get("state")):
                    return True, ""  # già chiuso
                params = {f"port{idx}": {"is_pwm": bool(o.get("is_pwm", True)),
                                         "value": o.get("value", self.flat_brightness),
                                         "state": True, "type": "flat_panel"}}
                r, _ = c.call("pi_output_set2", params, max_wait=self.timeout)
                if r.get("code") != 0:
                    return False, f"pi_output_set2 code {r.get('code')}"
                return True, ""
        except Exception as e:
            return False, str(e)

    def set_flat_brightness(self, value) -> tuple:
        """Imposta la luminosita' del pannello LASCIANDOLO CHIUSO (state=true).
        MAI value 0: il firmware forza state=false e il pannello si APRE
        (verificato live 2026-07-03). Ritorna (ok, detail)."""
        try:
            v = max(1, int(value))
            with self._client() as c:
                idx, o = self._flat_state(c)
                if o is None:
                    return False, f"flat_panel non letto: {self.out_read_detail}"
                params = {f"port{idx}": {"is_pwm": True, "value": v,
                                         "state": True, "type": "flat_panel"}}
                r, _ = c.call("pi_output_set2", params, max_wait=self.timeout)
                if r.get("code") != 0:
                    return False, f"pi_output_set2 code {r.get('code')}"
                _, o2 = self._flat_state(c)
                if not o2 or not bool(o2.get("state")) or int(o2.get("value") or 0) != v:
                    return False, f"rilettura incoerente: {o2}"
                return True, ""
        except Exception as e:
            return False, str(e)

    def set_output(self, type_name: str, value) -> tuple:
        """Imposta un'uscita di potenza dell'ASIAIR per `type` (pi_output_get2/
        set2, canale 4700), es. 'dew_heater' (output 4, la fascia
        anticondensa). value minimo 1 con state=true: value 0 e' VIETATO (il
        firmware forza state=false e SPEGNE l'uscita — sul flat panel lo
        APRIREBBE, visto live 2026-07-03). Verifica con rilettura. Ritorna
        (ok, detail).
        Lettura e rilettura passano da _output_state, quindi RITENTANO: qui
        c'era lo stesso difetto che il 2026-08-12 annullo' i flat, ma sulla
        fascia anticondensa (dew_heater)."""
        try:
            v = max(1, int(value))
            with self._client() as c:
                idx, o = self._output_state(c, type_name)
                if o is None:
                    return False, f"'{type_name}' non letto: {self.out_read_detail}"
                params = {f"port{idx}": {"is_pwm": bool(o.get("is_pwm", True)),
                                         "value": v, "state": True,
                                         "type": type_name}}
                r, _ = c.call("pi_output_set2", params, max_wait=self.timeout)
                if r.get("code") != 0:
                    return False, f"pi_output_set2 code {r.get('code')}"
                _, o2 = self._output_state(c, type_name)
                if o2 is None:
                    return False, (f"scritto, ma rilettura di '{type_name}' non "
                                   f"riuscita: {self.out_read_detail}")
                if not bool(o2.get("state")) or int(o2.get("value") or 0) != v:
                    return False, f"rilettura incoerente: {o2}"
                return True, ""
        except Exception as e:
            return False, str(e)

    def ensure_anti_dew(self) -> tuple:
        """Accende l'Anti-Dew Heater della camera PRINCIPALE se risulta spento
        (set_control_value ["AntiDewHeater", 1], canale 4700 — lo stesso comando
        che cooler_off usa con 0; lettura: get_control_value, verificata live
        2026-07-19). Richiesta utente 2026-07-19: trovato spento, l'umidita'
        del mattino ha rovinato ~10 pose. Ritorna (ok, detail)."""
        try:
            with self._client() as c:
                r, _ = c.call("get_control_value", ["AntiDewHeater"],
                              max_wait=self.timeout)
                if r.get("code") != 0:
                    return False, f"get_control_value code {r.get('code')}"
                if int((r.get("result") or {}).get("value") or 0):
                    return True, "gia' acceso"
                r, _ = c.call("set_control_value", ["AntiDewHeater", 1],
                              max_wait=self.timeout)
                if r.get("code") != 0:
                    return False, f"set_control_value code {r.get('code')}"
                r, _ = c.call("get_control_value", ["AntiDewHeater"],
                              max_wait=self.timeout)
                if not int((r.get("result") or {}).get("value") or 0):
                    return False, "era SPENTO e la riaccensione non risulta (rilettura 0)"
                return True, "era SPENTO: acceso ora"
        except Exception as e:
            return False, str(e)

    def camera_cooling(self) -> tuple:
        """Stato termico della camera principale (4700, verificato live
        2026-07-24): (ok, temp_C, target_C, cooler_on, detail).
        `Temperature` e' in DECIMI di grado (384 = 38.4°C); `TargetTemp` e'
        il target impostato dall'app (type text, es. 0.0); `CoolerOn` 0/1."""
        try:
            with self._client() as c:
                r, _ = c.call("get_control_value", ["Temperature"],
                              max_wait=self.timeout)
                if r.get("code") != 0:
                    return False, None, None, None, f"Temperature code {r.get('code')}"
                # A2 (2026-08-12): un 'value' mancante NON deve diventare 0.0 —
                # zero gradi e' una temperatura PLAUSIBILE e passerebbe (o
                # farebbe fallire) il gate termico dei flat senza che nessuno se
                # ne accorga. Meglio dichiarare la lettura non riuscita.
                tv = (r.get("result") or {}).get("value")
                if tv is None:
                    return False, None, None, None, "Temperature senza 'value'"
                temp = float(tv) / 10.0
                r, _ = c.call("get_control_value", ["TargetTemp"],
                              max_wait=self.timeout)
                if r.get("code") != 0:
                    return False, None, None, None, f"TargetTemp code {r.get('code')}"
                gv = (r.get("result") or {}).get("value")
                if gv is None:
                    return False, None, None, None, "TargetTemp senza 'value'"
                target = float(gv)
                r, _ = c.call("get_control_value", ["CoolerOn"],
                              max_wait=self.timeout)
                cooler = (bool(int((r.get("result") or {}).get("value") or 0))
                          if r.get("code") == 0 else None)
                return True, temp, target, cooler, ""
        except Exception as e:
            return False, None, None, None, str(e)

    def cooler_on(self) -> tuple:
        """Accende il raffreddamento della camera (set_control_value
        ["CoolerOn", 1], speculare a cooler_off) con rilettura di verifica.
        Il target resta quello gia' impostato (TargetTemp). Ritorna (ok, detail)."""
        try:
            with self._client() as c:
                r, _ = c.call("set_control_value", ["CoolerOn", 1],
                              max_wait=self.timeout)
                if r.get("code") != 0:
                    return False, f"set_control_value CoolerOn code {r.get('code')}"
                r, _ = c.call("get_control_value", ["CoolerOn"],
                              max_wait=self.timeout)
                if not int((r.get("result") or {}).get("value") or 0):
                    return False, "CoolerOn non risulta acceso alla rilettura"
                return True, ""
        except Exception as e:
            return False, str(e)

    def prepare(self) -> str:
        """PRE-AVVIO del rig: anti-dew della camera, fascia anticondensa al
        valore di partenza e COOLER acceso. Ritorna il testo da mostrare.

        Sta in un metodo solo perche' dal 2026-08-16 serve in DUE punti: al
        T-lead (inizializzazione, quando il piano non parte ancora) e dentro
        start() (avvio del piano). Chiamarlo due volte non fa danno — i tre
        passi rileggono e non ripetono cio' che e' gia' a posto — ed e' proprio
        la ridondanza che vogliamo: se il T-lead salta (tetto aperto tardi,
        KASA irraggiungibile), il piano non deve partire con la camera calda.

        Gli errori NON bloccano: meglio riprendere con la fascia storta che
        perdere la notte. Finiscono nel testo, che il chiamante mette su
        Telegram."""
        out = []
        ok, det = self.ensure_anti_dew()
        out.append(f"anti-dew camera ON ({det})" if ok else
                   f"⚠️ anti-dew camera NON verificato ({det}): accendilo dall'app")
        ok, det = self.set_output("dew_heater", self.start_dew_heater_pct)
        out.append(f"fascia anticondensa al {self.start_dew_heater_pct}%" if ok else
                   f"⚠️ fascia anticondensa NON impostata ({det})")
        ok, det = self.cooler_on()
        if ok:
            okc, temp, target, _, _ = self.camera_cooling()
            out.append(f"cooler ON ({temp}°C → target {target}°C)" if okc
                       else "cooler ON")
        else:
            out.append(f"⚠️ cooler NON acceso ({det})")
        return " · ".join(out)

    def set_camera_gain(self, gain) -> tuple:
        """Imposta il GAIN della camera principale (set_control_value ["Gain",N],
        canale 4700 — verificato live 2026-07-24 con readback 0<->100). Gli slot
        autorun con gain -10000 ("default") usano il gain corrente della camera:
        impostarlo qui governa flat E dark flat. Ritorna (ok, detail)."""
        try:
            g = int(gain)
            with self._client() as c:
                r, _ = c.call("set_control_value", ["Gain", g], max_wait=self.timeout)
                if r.get("code") != 0:
                    return False, f"set_control_value Gain code {r.get('code')}"
                r, _ = c.call("get_control_value", ["Gain"], max_wait=self.timeout)
                v = (r.get("result") or {}).get("value")
                if r.get("code") != 0 or int(v if v is not None else -1) != g:
                    return False, f"rilettura gain incoerente: {v}"
                return True, ""
        except Exception as e:
            return False, str(e)

    def read_flat_exps(self, filter_idxs) -> dict:
        """Tempi di posa CALCOLATI dall'auto-exp dei flat: dopo la passata,
        ASIAIR scrive il tempo nel campo `exp` dello slot (verificato live
        2026-07-24: 1.8 -> 1.62 e il valore persiste). Ritorna {idx_filtro: exp}
        per gli slot FLAT dei filtri indicati, o None se non leggibili."""
        code, seq = self._call1(self.port, "get_target_sequences")
        if code != 0:
            return None
        out = {}
        for s in (seq or {}).get("slots") or []:
            if s.get("type") == "flat" and s.get("filter") in filter_idxs:
                out[s["filter"]] = float(s.get("exp") or 0)
        return out if len(out) == len(set(filter_idxs)) else None

    def start_flats(self) -> tuple:
        """Avvia l'autorun FLAT gia' programmato dall'utente (cattura app 2026-07-03):
        reset_sequence_progress [] + start_exposure ["light"] (stesso comando dei
        light, NON esiste un param "flat"). Verifica POSITIVA di partenza: la
        sequenza flat deve CONSUMARSI (left_time_sec sotto il valore post-reset).
        Ritorna (ok, detail)."""
        try:
            with self._client() as c:
                # PAGINA AUTORUN prima dello start: start_exposure agisce sul
                # contesto corrente e dopo una notte l'agente ha lasciato "plan".
                # Se la pagina non cambia NON si avvia niente: meglio fermi che
                # far ripartire il PIANO delle light col pannello chiuso.
                pg, _ = c.call("set_page", [self.flat_page], max_wait=self.timeout)
                if pg.get("code") != 0:
                    return False, (f"set_page '{self.flat_page}' code {pg.get('code')}: "
                                   "nome pagina autorun da verificare")
                r, _ = c.call("reset_sequence_progress", [], max_wait=self.timeout)
                if r.get("code") != 0:
                    return False, f"reset_sequence_progress code {r.get('code')}"
                q, _ = c.call("get_target_sequences", [], max_wait=self.timeout)
                # A2 (2026-08-12): distinguere "la sequenza e' vuota" da "non ho
                # potuto leggerla". Prima erano lo stesso messaggio e mandavano a
                # cercare un guasto nell'app che non c'era.
                qres = q.get("result")
                if q.get("code") != 0 or not isinstance(qres, dict):
                    return False, (f"get_target_sequences non leggibile "
                                   f"(code {q.get('code')} {q.get('error') or ''}): "
                                   "sequenza autorun NON verificata")
                left0 = int(qres.get("left_time_sec") or 0)
                if left0 <= 0:
                    return False, ("sequenza autorun VUOTA (left_time_sec=0): "
                                   "flat non programmati/abilitati sull'ASIAIR")
                r, _ = c.call("start_exposure", ["light"], max_wait=self.timeout)
                if r.get("code") != 0:
                    return False, f"start_exposure code {r.get('code')}: {r.get('error')}"
                # VERIFICA POSITIVA: solo l'autorun consuma la sequenza FLAT, quindi
                # left_time_sec DEVE scendere sotto il valore post-reset. is_working
                # da solo non basta (da pagina sbagliata start_exposure scatta UNA
                # posa di preview, visto live 2026-07-03) e get_enabled_plan e'
                # INUTILIZZABILE come rete anti-piano: is_plan_started resta true
                # anche a piano fermo e dopo un reboot (verificato 2026-07-04,
                # falso allarme all'alba con autorun abortito per niente).
                working_seen, left = False, left0
                for _ in range(10):
                    time.sleep(3)
                    a, _ = c.call("get_app_state", [], max_wait=self.timeout)
                    cap = (a.get("result") or {}).get("capture") or {}
                    q, _ = c.call("get_target_sequences", [], max_wait=self.timeout)
                    left = int((q.get("result") or {}).get("left_time_sec") or 0)
                    working_seen = working_seen or bool(cap.get("is_working"))
                    # con l'auto-exp (2026-07-24) il tempo ricalcolato puo'
                    # SUPERARE il preset e left salire SOPRA left0 (visto live:
                    # G a gain 0, 362->661s): qualunque CAMBIO di left rispetto
                    # a left0 e' prova che l'autorun sta lavorando (solo lui
                    # tocca la sequenza; da pagina sbagliata left resta uguale).
                    # In piu': un frame completato vale comunque come prova.
                    done = int(((cap.get("frame_summary") or {})
                                .get("complete_num")) or 0)
                    if (cap.get("is_working") and cap.get("exposure_mode") == "autosave"
                            and (left != left0 or done >= 1)):
                        return True, ""
                # in 30s la sequenza flat non e' avanzata: qualunque cosa sia
                # partita NON sono i flat -> fermala e lascia decidere all'utente
                c.call("stop_exposure", [], max_wait=self.timeout)
                return False, (f"la sequenza flat non avanza (left {left}s su {left0}s, "
                               f"is_working visto={working_seen}): cattura fermata")
        except Exception as e:
            return False, str(e)

    def flats_status(self) -> tuple:
        """Stato dell'autorun flat: (ok, working, left_time_sec, prog, detail).
        working = capture.is_working; left_time_sec da get_target_sequences
        (0 a fine sequenza; >0 se interrotta a meta'); prog = 'fatte/totali'
        da frame_summary (None se non disponibile) — per i messaggi di
        avanzamento (richiesta utente 2026-07-24: mai silente sui flat)."""
        try:
            with self._client() as c:
                a, _ = c.call("get_app_state", [], max_wait=self.timeout)
                cap = (a.get("result") or {}).get("capture") or {}
                fs = cap.get("frame_summary") or {}
                prog = (f"{fs.get('complete_num')}/{fs.get('total')}"
                        if fs.get("total") else None)
                q, _ = c.call("get_target_sequences", [], max_wait=self.timeout)
                left = int((q.get("result") or {}).get("left_time_sec") or 0)
                return True, bool(cap.get("is_working")), left, prog, ""
        except Exception as e:
            return False, False, 0, None, str(e)

    def _call1(self, port, method, params=None, timeout=None):
        """Comando one-shot su connessione fresca (robusto ai broken-pipe).
        Ritorna (code, result|error); code None = errore di trasporto."""
        tmo = timeout or self.timeout
        try:
            with AsiairClient(self.host, port, timeout=tmo) as c:
                r, _ = c.call(method, params or [], max_wait=tmo)
                return r.get("code"), (r.get("result") if "result" in r else r.get("error"))
        except Exception as e:
            return None, str(e)

    def pi_clock_sync(self, tol_seconds: float = 120.0) -> tuple:
        """OROLOGIO DEL PI (incidente 2026-08-17): l'ASIAIR non ha RTC e, senza
        internet ne' app al boot, il clock di sistema resta al default del
        firmware (2019-02-14) -> il driver mount nasce con l'ora sballata e
        OGNI goto viene rifiutato ("Mount slews failed" x3162 la notte del
        17/8). Correggere l'ora DOPO non basta (lo stato resta avvelenato,
        serve il reboot): va sistemata PRIMA di connettere i device. Legge
        pi_get_time; se fuori tolleranza imposta l'ora del server con
        pi_set_time (come fa l'app a ogni connect) e RILEGGE per conferma.
        Ritorna (ok, detail); ok=False = orologio sballato e non corretto."""
        def _skew(r):
            # scarto |ora Pi - ora server| in secondi, nella tz del Pi
            if not isinstance(r, dict):
                return None, None
            tzname = r.get("time_zone") or "Europe/Rome"
            try:
                tz = ZoneInfo(tzname)
            except Exception:
                tz, tzname = ZoneInfo("Europe/Rome"), "Europe/Rome"
            try:
                pi_dt = datetime(int(r["year"]), int(r["mon"]), int(r["day"]),
                                 int(r["hour"]), int(r["min"]), int(r["sec"]),
                                 tzinfo=tz)
            except Exception:
                return None, tzname
            return abs((datetime.now(tz) - pi_dt).total_seconds()), tzname

        code, res = self._call1(self.port, "pi_get_time")
        if code != 0:
            # metodo assente/illeggibile: non blocco (non e' la prova che l'ora
            # sia sbagliata), ma lo dico nel dettaglio
            return True, f"orologio Pi non leggibile (pi_get_time code {code})"
        skew, tzname = _skew(res)
        if skew is not None and skew <= tol_seconds:
            return True, f"orologio Pi ok (scarto {skew:.0f}s)"
        was = ("{}-{:02d}-{:02d} {:02d}:{:02d}".format(
                   res.get("year"), res.get("mon") or 0, res.get("day") or 0,
                   res.get("hour") or 0, res.get("min") or 0)
               if isinstance(res, dict) else "?")
        # sballato (o campi illeggibili): imposto l'ora del server nella
        # STESSA tz che il Pi dichiara (payload = stessa forma di pi_get_time)
        nowl = datetime.now(ZoneInfo(tzname))
        payload = {"year": nowl.year, "mon": nowl.month, "day": nowl.day,
                   "hour": nowl.hour, "min": nowl.minute, "sec": nowl.second,
                   "time_zone": tzname}
        scode, _ = self._call1(self.port, "pi_set_time", [payload])
        if scode != 0:
            # forma alternativa: oggetto nudo (come pi_output_set2)
            scode, _ = self._call1(self.port, "pi_set_time", payload)
        code2, res2 = self._call1(self.port, "pi_get_time")
        skew2, _ = _skew(res2) if code2 == 0 else (None, None)
        if skew2 is not None and skew2 <= tol_seconds:
            return True, f"orologio Pi CORRETTO (leggeva {was})"
        return False, (f"orologio Pi SBALLATO e non correggibile: leggeva {was}, "
                       f"pi_set_time code {scode}, scarto dopo il set "
                       + ("n/d" if skew2 is None else f"{skew2:.0f}s"))

    def connect_all(self) -> tuple:
        """Connette TUTTI i device a freddo (ricetta VALIDATA live 2026-07-04:
        cold boot 5/5 in ~60s, camere in 2-5s come dall'app). INDISPENSABILE il
        PRIMING `get_connected_cameras` sul canale PRIMA del connect della sua
        camera (senza: open_camera 204 "out of limit" / set_connected 207 "fail
        to operate" A OLTRANZA — era il problema storico). Guida per ULTIMA.
        Il mount PERDE L'OROLOGIO senza corrente -> scope_set_time dopo il
        connect (come fa l'app). Ritorna (ok, detail con i tempi)."""
        t0 = time.time()
        deadline = t0 + 360          # tetto globale: non bloccare il ciclo oltre
        times = {}

        def _budget(b):
            return max(5.0, min(b, deadline - time.time()))

        def _wait(name, check, issue, budget, reissue=True):
            t = time.time()
            if check():
                times[name] = 0.0
                return True
            issue()
            while time.time() - t < _budget(budget):
                time.sleep(2)
                if check():
                    times[name] = time.time() - t
                    return True
                if reissue and time.time() - t > 6:
                    issue()
            return False

        def _cams(port):
            # PRIMING: popola la lista camere del canale (a ridosso del boot
            # puo' essere ancora vuota: ritenta qualche secondo)
            t = time.time()
            while time.time() - t < _budget(60):
                code, res = self._call1(port, "get_connected_cameras")
                if code == 0 and isinstance(res, list) and res:
                    return res
                time.sleep(3)
            return []

        def _cid(cams, needle, default):
            for c in cams:
                if needle in (c.get("name") or "").lower():
                    return c.get("id")
            return default

        try:
            # 0) servizi su (dopo il ping i canali impiegano ancora qualche s)
            t = time.time()
            for port in (self.port, self.guider_port):
                while True:
                    if self._call1(port, "test_connection")[0] == 0:
                        break
                    if time.time() - t > _budget(120):
                        return False, f"servizio porta {port} non pronto"
                    time.sleep(3)
            # 0.5) OROLOGIO DEL PI prima di TUTTO (2026-08-17): col clock al
            # default il connect del mount nasce avvelenato e i goto vengono
            # rifiutati per l'intera notte (nemmeno correggere l'ora dopo lo
            # sana: serve il reboot). Senza ora buona NON si connette nulla.
            ok_t, clock_det = self.pi_clock_sync()
            if not ok_t:
                return False, clock_det
            # 1) camera principale (4700, con priming)
            cams = _cams(self.port)
            if not cams:
                return False, "lista camere (4700) vuota: USB non enumerato"
            id_main = _cid(cams, "2600", 0)
            if not _wait("cam",
                         lambda: self._call1(self.port, "get_camera_info")[0] == 0,
                         lambda: self._call1(self.port, "open_camera", [id_main]),
                         90):
                return False, "camera principale non connessa"

            # 2) EAF + EFW (da scollegati: code 0 ma state 'close')
            def _st_ok(method):
                code, res = self._call1(self.port, method)
                st = (res or {}).get("state") if isinstance(res, dict) else None
                return code == 0 and st and st != "close"

            if not _wait("eaf", lambda: _st_ok("get_focuser_info"),
                         lambda: self._call1(self.port, "open_focuser", [0]), 60):
                return False, "EAF non connesso"
            if not _wait("efw", lambda: _st_ok("get_wheel_state"),
                         lambda: self._call1(self.port, "open_wheel", [0]), 60):
                return False, "EFW non connesso"

            # 3) mount (4400): select SOLO da scollegato (312 se gia' connesso)
            def _mount_ok():
                code, res = self._call1(self.guider_port, "get_connected", [True])
                return bool((res or {}).get("mount")) if isinstance(res, dict) else False

            def _mount_issue():
                if not _mount_ok():
                    self._call1(self.guider_port, "select_mount_list_index",
                                [self.mount_list_index])
                self._call1(self.guider_port, "set_connected",
                            [{"mount": True, "async": True}])

            if not _wait("mount", _mount_ok, _mount_issue, 90):
                return False, "mount non connesso"
            # senza corrente il mount perde l'orologio (utc 2000-1-1); la
            # location invece persiste. Formato scoperto 2026-07-04. Dal
            # 2026-08-17 l'esito e' VERIFICATO con scope_get_time (prima era
            # fire-and-forget: un fallimento silenzioso = notte di goto
            # rifiutati). Tolleranza LARGA (2h): deve acchiappare il reset a
            # 2000-1-1, non fare il pignolo su offset/estate.
            def _mount_clock_ok():
                c2, r2 = self._call1(self.guider_port, "scope_get_time")
                if c2 != 0 or not isinstance(r2, list) or not r2:
                    return False
                try:  # es. ["2026-8-17T5:51:47", "1"] — elemento 0 in UTC
                    dt = datetime.strptime(str(r2[0]), "%Y-%m-%dT%H:%M:%S")
                except Exception:
                    return False
                dt = dt.replace(tzinfo=ZoneInfo("UTC"))
                return abs((datetime.now(ZoneInfo("UTC")) - dt)
                           .total_seconds()) <= 7200
            ok_mc = False
            for _ in range(3):
                nowu = datetime.now(ZoneInfo("UTC"))
                self._call1(self.guider_port, "scope_set_time",
                            [nowu.strftime("%Y-%m-%dT%H:%M:%S"), "-0"])
                if _mount_clock_ok():
                    ok_mc = True
                    break
                time.sleep(2)
            if not ok_mc:
                return False, ("orologio del MOUNT non impostato "
                               "(scope_set_time senza effetto)")

            # 3b) LOCATION AL CONNECT — CAUSA VERA della notte 16-17/8
            # (diagnosticata live 2026-08-17). Senza `scope_set_location` dopo
            # il connect, il route planner dell'ASIAIR non calcola il percorso:
            # OGNI goto muore in 41ms con Event ScopeGoto state:fail
            # error:"internal error" code:300 e `route:[]`, e l'app mostra
            # "Mount slews failed". La location PERSISTE nel mount (scope_get_info
            # la rilegge giusta) ma non basta: va (ri)scritta nella sessione.
            # L'app la manda a ogni connect, l'agente non l'aveva mai mandata —
            # per questo funzionava solo quando l'utente apriva l'app.
            # Verificato live: prima 3 goto falliti a 41ms, subito dopo la set
            # il mount ha slewato su NGC 7635 (Alt 55.8°).
            # ORDINE VOLUTO: prima si LEGGE, poi si scrive. Un mount fuori
            # tolleranza (rig spostato) NON va "corretto" scrivendoci sopra:
            # sarebbe la fine del gate posizione, che da qui in poi troverebbe
            # sempre le coord giuste perche' gliele abbiamo messe noi.
            elat = elon = None
            if self.expected_lat is not None and self.expected_lon is not None:
                elat, elon = float(self.expected_lat), float(self.expected_lon)
                _, res_i = self._call1(self.guider_port, "scope_get_info")
                lat = (res_i or {}).get("Lat") if isinstance(res_i, dict) else None
                lon = (res_i or {}).get("Lon") if isinstance(res_i, dict) else None
                if (isinstance(lat, (int, float)) and isinstance(lon, (int, float))
                        and (abs(lat - elat) > self.pos_tol
                             or abs(lon - elon) > self.pos_tol)):
                    return False, (f"posizione mount FUORI tolleranza: "
                                   f"lat={lat:.4f} lon={lon:.4f} (atteso "
                                   f"{elat:.4f},{elon:.4f}, tol {self.pos_tol}°)")
            else:
                # senza coord attese in config riscrivo quelle che il mount
                # gia' dichiara: serve comunque a "svegliare" il planner
                _, res_i = self._call1(self.guider_port, "scope_get_info")
                if isinstance(res_i, dict):
                    elat, elon = res_i.get("Lat"), res_i.get("Lon")
            if not isinstance(elat, (int, float)) or not isinstance(elon, (int, float)):
                return False, "posizione mount non leggibile (scope_get_info)"
            lc, _ = self._call1(self.guider_port, "scope_set_location", [elat, elon])
            if lc != 0:
                return False, (f"scope_set_location fallita (code {lc}): "
                               "senza location i goto verrebbero RIFIUTATI")

            # 4) camera guida (4400, con priming del SUO canale, per ULTIMA)
            cams_g = _cams(self.guider_port)
            id_g = _cid(cams_g, "120", 1)

            def _guide_ok():
                code, res = self._call1(self.guider_port, "get_connected", [True])
                return bool((res or {}).get("camera")) if isinstance(res, dict) else False

            def _guide_issue():
                if not _guide_ok():
                    self._call1(self.guider_port, "set_camera_idx", [id_g])
                self._call1(self.guider_port, "set_connected", [{"camera": True}])

            if not _wait("guida", _guide_ok, _guide_issue, 120):
                return False, "camera guida non connessa"

            det = ", ".join(f"{k} {v:.0f}s" for k, v in times.items())
            return True, (f"5/5 device in {time.time()-t0:.0f}s ({det}); "
                          f"{clock_det}; ora+location al mount OK")
        except Exception as e:
            return False, str(e)

    # chiavi complete di uno slot autorun: set_sequence VUOLE l'oggetto intero
    # (cattura app 2026-07-05); campi parziali non sono previsti
    SEQ_SLOT_KEYS = ("filter", "suffix", "repeat", "id", "enable", "autoexp",
                     "gain", "exp", "bin", "type", "capture_index")

    # Tempi di posa: l'ASIAIR RILEGGE un valore leggermente diverso da quello
    # scritto (chiesto 8.19 -> riletto 8.190001, live 2026-07-27), artefatto di
    # rappresentazione in virgola mobile del firmware. Confrontarli con `!=`
    # faceva fallire la verifica e fermava il flusso PRIMA dei dark, con i flat
    # gia' fatti. Tolleranza 2 ms: mille volte l'artefatto osservato (1e-6) e
    # cinque volte sotto il passo di 0.01 s con cui l'agente scrive i dark dal
    # 2026-08-15 -> un tempo davvero sbagliato viene comunque rilevato.
    EXP_TOL = 0.002

    @staticmethod
    def same_exp(a, b, tol=EXP_TOL) -> bool:
        """Due tempi di posa sono lo stesso valore a meno dell'artefatto float."""
        return abs(float(a or 0) - float(b or 0)) <= tol

    @staticmethod
    def round_exp(v) -> float:
        """Tempo di posa arrotondato al CENTESIMO di secondo (richiesta utente
        2026-08-15, prima era 1 decimale): il dark flat deve avere lo STESSO
        tempo del flat fino al centesimo, altrimenti PixInsight fatica ad
        accoppiarli (flat 5.74 con dark 5.70 = coppia non riconosciuta).
        Mai sotto 0.01 s, cosi' un flat brevissimo non diventa una posa nulla."""
        return max(round(float(v), 2), 0.01)

    def get_wheel_names(self):
        """Nomi dei filtri EFW in ordine di posizione (get_wheel_setting.names,
        es. ['L','R','G','B','S','H','O']); lo slot autorun li indica col campo
        `filter` 0-based. None se non leggibili."""
        code, res = self._call1(self.port, "get_wheel_setting")
        return (res or {}).get("names") if code == 0 else None

    def configure_autorun_slots(self, kind: str, filter_idxs, max_exp=None,
                                exp_by_filter=None) -> tuple:
        """Abilita SOLO gli slot autorun di tipo `kind` ('flat'/'dark') dei
        filtri indicati (indici EFW 0-based); disabilita tutti gli altri.
        `max_exp` esclude i dark 'library' a lunga posa (es. 300/600s, valutato
        sull'exp PRE-modifica). `exp_by_filter` {idx: sec} scrive il tempo di
        posa negli slot abilitati (dark flat = tempo del flat corrispondente,
        richiesta utente 2026-07-24). NB: uno slot con progresso NON e'
        editabile -> reset_sequence_progress PRIMA degli edit (code 224, visto
        live 2026-07-05). Verifica con rilettura. Ritorna (ok, n_frame, detail)."""
        try:
            code, seq = self._call1(self.port, "get_target_sequences")
            if code != 0:
                return False, 0, f"get_target_sequences code {code}"
            slots = (seq or {}).get("slots") or []
            r, _ = self._call1(self.port, "reset_sequence_progress")
            if r != 0:
                return False, 0, f"reset_sequence_progress code {r}"
            for s in slots:
                want = (s.get("type") == kind and s.get("filter") in filter_idxs
                        and (max_exp is None or float(s.get("exp") or 0) <= max_exp))
                want_exp = (exp_by_filter or {}).get(s.get("filter")) if want else None
                if (bool(s.get("enable")) != want
                        or (want_exp is not None
                            and not self.same_exp(s.get("exp"), want_exp))):
                    params = {k: s.get(k) for k in self.SEQ_SLOT_KEYS}
                    params["enable"] = want
                    if want_exp is not None:
                        params["exp"] = want_exp
                    c2, err = self._call1(self.port, "set_sequence", [params])
                    if c2 != 0:
                        return False, 0, f"set_sequence id{s.get('id')} code {c2}: {err}"
            code, seq2 = self._call1(self.port, "get_target_sequences")
            en = [x for x in (seq2 or {}).get("slots") or [] if x.get("enable")]
            bad = [x.get("id") for x in en
                   if x.get("type") != kind or x.get("filter") not in filter_idxs
                   or (exp_by_filter and x.get("filter") in exp_by_filter
                       and not self.same_exp(x.get("exp"),
                                             exp_by_filter[x.get("filter")]))]
            if bad or not en:
                # anche gli ATTESI nel messaggio: senza non si distingue uno slot
                # sbagliato da un tempo che non torna (diagnosi 2026-07-27)
                return False, 0, (f"verifica fallita: abilitati "
                                  f"{[(x.get('id'), x.get('type'), x.get('filter'), x.get('exp')) for x in en]}"
                                  f", attesi {kind} filtri {sorted(filter_idxs)}"
                                  + (f" con exp {dict(sorted(exp_by_filter.items()))}"
                                     if exp_by_filter else ""))
            frames = sum(int(x.get("repeat") or 0) for x in en)
            left = int((seq2 or {}).get("left_time_sec") or 0)
            if left <= 0:
                return False, 0, "left_time_sec=0 dopo la configurazione"
            # un filtro senza slot utilizzabile (assente, o solo un dark
            # 'library' escluso da max_exp) non e' un errore ma va DETTO: il
            # dettaglio finisce nel messaggio Telegram, altrimenti la notte
            # resta senza quei frame e ce ne si accorge in PixInsight
            miss = sorted(set(filter_idxs) - {x.get("filter") for x in en})
            return True, frames, (f"{len(en)} slot {kind}, {frames} frame, "
                                  f"~{max(1, left // 60)} min"
                                  + (f" ⚠️ nessuno slot per i filtri {miss}"
                                     if miss else ""))
        except Exception as e:
            return False, 0, str(e)

    def _run_cmds(self, spec, default_port: int) -> tuple:
        """Esegue UNO o PIÙ comandi configurati. `spec` = dict singolo {port,method,
        params} oppure LISTA di dict (eseguiti in ordine). Se vuoto/method assente,
        NON inventa nulla: ritorna (False, 'non configurato'). Ritorna (ok, detail):
        ok=True solo se TUTTI i comandi vanno a buon fine."""
        cmds = spec if isinstance(spec, list) else ([spec] if spec else [])
        cmds = [c for c in cmds if c and c.get("method")]
        if not cmds:
            return False, "non configurato (da catturare dall'app)"
        fails = []
        for cmd in cmds:
            port = int(cmd.get("port", default_port))
            params = cmd.get("params", [])
            try:
                with AsiairClient(self.host, port, timeout=self.timeout) as c:
                    r, _ = c.call(cmd["method"], params, max_wait=self.timeout)
                    if r.get("code") not in (0, None):
                        fails.append(f"{cmd['method']} code {r.get('code')}")
            except Exception as e:
                fails.append(f"{cmd['method']}: {e}")
        return (not fails), ("; ".join(fails) if fails else "")

    def cooler_off(self) -> tuple:
        """Spegne il raffreddamento della camera principale (cooler off diretto:
        l'ASIAIR non ha un warm-up tipo NINA). Spegne anche l'AntiDewHeater.
        Comandi da config (cooler_off: lista)."""
        return self._run_cmds(self.cooler_off_cmd, self.port)

    def mount_home(self) -> tuple:
        """Manda il mount (AM5N) in HOME/park. Comando da config (mount_home),
        sul canale guider (4400)."""
        return self._run_cmds(self.mount_home_cmd, self.guider_port)

    def shutdown(self) -> tuple:
        """Spegne l'ASIAIR (pi_shutdown, da config `shutdown`). Dopo il result 0
        il box smette di rispondere in pochi secondi."""
        return self._run_cmds(self.shutdown_cmd, self.port)

    def teardown(self, keep_cooler: bool = False) -> dict:
        """Chiusura ordinata: stop piano, cooler off, mount in home, chiudi OF2.
        Con keep_cooler=True SALTA il cooler (flusso flat a seguire: i flat vanno
        fatti alla stessa temperatura dei light). Esegue tutti i passi (non si
        ferma al primo errore) e ritorna l'esito di ciascuno:
        {'stop':(ok,det), ['cooler':(ok,det),] 'home':(ok,det), 'flat':(ok,det)}."""
        out = {}
        try:
            out["stop"] = (self.stop(), "")
        except Exception as e:
            out["stop"] = (False, str(e))
        if not keep_cooler:
            out["cooler"] = self.cooler_off()
        out["home"] = self.mount_home()
        out["flat"] = self.close_flat()
        return out


# --------------------------------------------------------------------------- #
# Sync ASIAIR -> NAS (CIFS + rsync), SENZA VPN (VPN sempre su sul router)
# --------------------------------------------------------------------------- #
def _run(cmd, timeout=None):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def ping(host: str) -> bool:
    return _run(["ping", "-c1", "-w3", host]).returncode == 0


def app_alive(host: str, port: int, timeout: float = 3) -> bool:
    """True se l'app ASIAIR accetta ancora connessioni su host:port. Il ping
    risponde il kernel del Pi, questo risponde il SOFTWARE: e' la differenza
    che serve dopo pi_shutdown."""
    try:
        with socket.create_connection((host, int(port)), timeout):
            return True
    except OSError:
        return False


def wait_asiair_down(host: str, port: int, wait_s: float = 15,
                     ping_timeout_s: float = 120, grace_s: float = 90) -> tuple:
    """Attende che l'ASIAIR sia DAVVERO spento dopo pi_shutdown, prima di
    togliere corrente con la KASA. Il ping da solo non basta: capita che il Pi
    continui a rispondere al ping mentre l'app e' gia' giu' e il box e' morto
    a tutti gli effetti (utente 2026-08-05: "l'unico sistema e' lo spegnimento
    dell'alimentazione"). Con la sola prova del ping il flusso abortiva e la
    KASA restava accesa tutto il giorno.
    Esiti (down, mode, detail):
    - ('ping') il box smette di rispondere al ping: caso normale, via subito;
    - ('app') il ping resiste ma la porta dell'app non accetta piu' (verificato
      due volte a distanza di 5s, per non cadere su un rifiuto momentaneo):
      si aspetta `grace_s` perche' il sistema finisca di scrivere sulla SD,
      poi si puo' togliere corrente;
    - (False) rispondono ANCORA sia ping sia app: spegnimento non riuscito,
      la corrente NON si tocca."""
    time.sleep(wait_s)
    deadline = time.time() + ping_timeout_s
    app_misses = 0
    while True:
        if not ping(host):
            return True, "ping", "l'ASIAIR non risponde più al ping"
        app_misses = 0 if app_alive(host, port) else app_misses + 1
        if app_misses >= 2:
            log.warning("ASIAIR %s: ping vivo ma app (porta %s) spenta; "
                        "attendo %ss e tolgo corrente", host, port, int(grace_s))
            time.sleep(grace_s)
            return True, "app", (f"il Pi risponde ancora al ping ma l'app "
                                 f"(porta {port}) è spenta")
        if time.time() >= deadline:
            return False, "", (f"l'ASIAIR risponde ANCORA al ping E all'app "
                               f"(porta {port}) dopo lo shutdown")
        time.sleep(5)


def vpn_diagnose(host: str, probe: str = "") -> dict:
    """Verifica raggiungibilità dell'ASIAIR via VPN e distingue la causa.
    - asiair_up: il box ASIAIR risponde al ping.
    - vpn_up: True se la VPN risulta su anche a box spento/in boot.
    - cause: testo della causa se non raggiungibile (per l'avviso Telegram).
    NB 2026-07-06: a rig SPENTO nessun host SFRO risponde all'echo diretto
    (nemmeno la sonda o il gateway), ma il router remoto risponde 'Destination
    Host Unreachable' ATTRAVERSO il tunnel quando si pinga l'ASIAIR:
    quell'errore ICMP e' la prova che la VPN e' attiva (visto live: falso
    'VPN giu'' con VPN su e KASA off)."""
    if not host:
        return {"asiair_up": False, "vpn_up": None, "cause": "host ASIAIR non configurato"}
    out = _run(["ping", "-c2", "-w4", host])
    if out.returncode == 0:
        return {"asiair_up": True, "vpn_up": True, "cause": ""}
    # Il router REMOTO risponde 'Destination Host Unreachable' ATTRAVERSO il
    # tunnel quando il box e' spento: quell'ICMP prova che la VPN e' attiva.
    # Si accetta solo se arriva da un indirizzo della STESSA rete dell'ASIAIR
    # (primi due ottetti uguali), altrimenti a rispondere e' il router di casa.
    net = ".".join(host.split(".")[:2])
    if any(ln.startswith(f"From {net}.") for ln in (out.stdout or "").splitlines()):
        return {"asiair_up": False, "vpn_up": True,
                "cause": "ASIAIR spento o in avvio (VPN attiva: risponde il router remoto)"}
    if probe:
        if ping(probe):
            return {"asiair_up": False, "vpn_up": True,
                    "cause": "ASIAIR spento o in avvio (VPN attiva)"}
        return {"asiair_up": False, "vpn_up": False,
                "cause": "VPN NON attiva (rete remota irraggiungibile)"}
    return {"asiair_up": False, "vpn_up": None,
            "cause": f"ASIAIR {host} irraggiungibile (VPN giù o box spento)"}


def is_mounted(path: str) -> bool:
    return os.path.ismount(path)


# Smistamento per data sul NAS (2026-07-26). L'ASIAIR ammucchia tutti i frame
# di tutte le notti in un'unica cartella per tipo (e per soggetto nei light) e
# l'opzione non e' configurabile nell'app: la separazione delle sessioni, che
# serve in PixInsight, la fa l'agente in DESTINAZIONE.
#   Autorun/Flat/<aaaammgg>/  ·  Autorun/Dark/<aaaammgg>/
#   Plan/Light/<Soggetto>/<aaaammgg>/            (i light dell'agente sono un
#                                                 PIANO, quindi sotto Plan/)
# La data e' quella nel NOME FILE (orologio ASIAIR = ora di Roma) e coincide con
# il night_id: a -7h l'intera notte dell'osservatorio cade nello stesso giorno
# di calendario romano (light ~04:50-12:50, flat/dark ~13:20), quindi una
# sessione non si spezza mai su due cartelle. La sorgente ASIAIR non si tocca.
FNAME_DATE = re.compile(r"_(\d{8})-\d{6}_")
DATED_ROOTS = (("ASIAIR/Autorun/Flat", "flat"), ("ASIAIR/Autorun/Dark", "dark"))
LIGHT_ROOT = "ASIAIR/Plan/Light"
# pattern rsync equivalenti a FNAME_DATE: DEVONO restare allineati alla regex,
# altrimenti un nome anomalo finirebbe sia in radice sia in una cartella data
DATED_GLOB = "*_" + "[0-9]" * 8 + "-" + "[0-9]" * 6 + "_*"


def date_glob(d: str) -> str:
    return f"*_{d}-" + "[0-9]" * 6 + "_*"


def dated_dirs(mp: str) -> list:
    """[(percorso relativo, kind)] delle cartelle i cui file vanno smistati per
    data: Flat, Dark e una voce per ogni soggetto sotto Plan/Light."""
    out = []
    for rel, kind in DATED_ROOTS:
        if (Path(mp) / rel).is_dir():
            out.append((rel, kind))
    lroot = Path(mp) / LIGHT_ROOT
    if lroot.is_dir():
        try:
            targets = sorted(p.name for p in lroot.iterdir() if p.is_dir())
        except OSError as e:
            log.warning("elenco soggetti illeggibile (%s): %s", lroot, e)
            targets = []
        out += [(f"{LIGHT_ROOT}/{t}", "light") for t in targets]
    return out


def split_by_date(src: Path) -> tuple:
    """(date presenti nei nomi file, c'e' almeno un file senza data)."""
    dates, undated = set(), False
    try:
        names = os.listdir(src)
    except OSError as e:
        log.warning("cartella illeggibile (%s): %s", src, e)
        return dates, undated
    for n in names:
        if n.startswith("."):
            continue
        m = FNAME_DATE.search(n)
        if m:
            dates.add(m.group(1))
        elif (Path(src) / n).is_file():
            undated = True
    return dates, undated


def sync_pass(sc: dict, dry_run: bool) -> dict:
    res = {"ok": False, "files": 0, "error": None}
    nas = sc["nas_dest"]
    if not is_mounted(nas):
        res["error"] = f"NAS dest non montato: {nas}"
        log.error(res["error"])
        return res
    ip = sc["asiair_ip"]
    if not ping(ip):
        res["error"] = f"ASIAIR non raggiungibile: {ip}"
        log.info(res["error"])
        return res
    mp = sc["mount_point"]
    share = f"//{ip}/{sc['smb_share_name']}"
    Path(mp).mkdir(parents=True, exist_ok=True)
    try:
        if not is_mounted(mp):
            m = _run(["mount", "-t", "cifs", "-o",
                      f"credentials={sc['credentials_file']}", share, mp], timeout=30)
            if m.returncode != 0:
                res["error"] = f"mount CIFS fallito: {m.stderr.strip()}"
                log.error(res["error"])
                return res
        base = ["rsync", "-rah"]
        for ex in sc.get("rsync_exclude", ["*.jpg"]):
            base += ["--exclude", ex]
        tail = list(sc.get("rsync_extra", []))
        if dry_run:
            tail.append("--dry-run")
        cap_min = sc.get("per_run_cap_minutes", 30)
        deadline = time.monotonic() + cap_min * 60
        dest_root = Path(nas) / Path(mp).name   # /mnt/astronomia/asiair_sfro
        # conteggio elementi trasferiti + dettaglio per tipo (per il bottone
        # Rsync del bot): righe -i con 2° carattere 'f' = file veri (le
        # directory non contano nel dettaglio)
        res["by_kind"] = {"light": 0, "flat": 0, "dark": 0}

        def one_pass(filters, src, dst, kind=None):
            """Una passata rsync src/ -> dst/. Ritorna il messaggio d'errore
            (None se ok) e aggiorna i conteggi."""
            left = deadline - time.monotonic()
            if left < 10:
                return (f"tempo massimo del sync esaurito ({cap_min} min): "
                        f"il resto va alla passata successiva")
            # la destinazione puo' essere profonda 2 livelli nuovi (soggetto
            # nuovo + data): rsync ne crea solo uno, quindi la si prepara qui.
            # Anche in dry-run: sono cartelle vuote, ma il conteggio previsto
            # resta fedele a quello della passata vera.
            Path(dst).mkdir(parents=True, exist_ok=True)
            out = _run(base + filters + tail + ["-i", f"{src}/", f"{dst}/"],
                       timeout=left)
            for ln in out.stdout.splitlines():
                if not ln or ln[0] not in "<>ch.":
                    continue
                res["files"] += 1
                if kind and len(ln) > 1 and ln[1] == "f":
                    res["by_kind"][kind] += 1
            # rc 24 = "file vanished": sorgenti spariti DURANTE la copia
            # (l'ASIAIR rinomina i file a fine scrittura) — tutto il resto e'
            # copiato e i mancanti li prende la passata dopo. E' un avviso, NON
            # un errore: trattarlo come fatale ha annullato lo shutdown del
            # 2026-07-24.
            if out.returncode == 24:
                log.warning("rsync rc=24 (file vanished durante la copia): "
                            "tollerato, ripresi al prossimo sync")
            if out.returncode not in (0, 24):
                return f"rsync rc={out.returncode}: {out.stderr.strip()[:200]}"
            return None

        # 1) passata generale: tutto il resto della share (Preview, Live,
        #    Video, log, Plan) invariato. Le tre radici smistate per data sono
        #    escluse qui e gestite sotto.
        skip = []
        for rel in ("ASIAIR/Autorun/Flat", "ASIAIR/Autorun/Dark", LIGHT_ROOT):
            skip += ["--exclude", f"/{rel}/"]   # pota: niente scansione CIFS
        err = one_pass(skip, mp, str(dest_root))
        # 2) passate per (cartella, data): stesso file -> sempre lo stesso
        #    percorso, quindi l'incrementale di rsync continua a funzionare.
        for rel, kind in ([] if err else dated_dirs(mp)):
            src = Path(mp) / rel
            dates, undated = split_by_date(src)
            for d in sorted(dates):
                err = one_pass(["--include", date_glob(d), "--exclude", "*"],
                               str(src), str(dest_root / rel / d), kind)
                if err:
                    break
            # file dal nome non riconoscibile: restano nella radice come prima,
            # non si perde mai niente
            if not err and undated:
                err = one_pass(["--exclude", DATED_GLOB],
                               str(src), str(dest_root / rel), kind)
            if err:
                break
        res["ok"] = err is None
        if err:
            res["error"] = err
            log.error(err)
    except subprocess.TimeoutExpired as e:
        # tipico di VPN caduta a meta' trasferimento: il comando (mount o
        # rsync) si e' bloccato oltre il cap. Errore ordinario, NON eccezione:
        # deve arrivare a Telegram/fail() come gli altri, non crashare il ciclo.
        res["error"] = (f"comando oltre il tempo massimo ({e.cmd[0]}, "
                        f"{int(e.timeout)}s): VPN caduta durante il sync?")
        log.error(res["error"])
    finally:
        # umount di una share CIFS morta (VPN giu') puo' bloccarsi: timeout
        # breve + fallback lazy; MAI sollevare da qui.
        try:
            if is_mounted(mp):
                _run(["umount", mp], timeout=20)
        except subprocess.TimeoutExpired:
            pass
        try:
            if is_mounted(mp):
                _run(["umount", "-l", mp], timeout=10)
                log.warning("umount CIFS lazy dopo blocco: %s", mp)
        except subprocess.TimeoutExpired:
            log.error("umount CIFS bloccato anche in lazy: %s", mp)
    return res


# --------------------------------------------------------------------------- #
# Ciclo
# --------------------------------------------------------------------------- #
def _kasa_connect(cfg, files, http_cfg, tu):
    """Login Kasa + risoluzione presa/strip. Ritorna (kc, dev, managed, plug, err).
    managed = [{alias,id,state}]; plug 'ON' se almeno una gestita e' accesa."""
    plug, err, dev, kc, managed = "UNKNOWN", None, None, None, []
    try:
        kc = KasaCloud(cfg["kasa"], load_kv_file(files["kasa"]), http_cfg, tu,
                       cfg.get("kasa_token_file", ""))
    except Exception as e:      # credenziali illeggibili: come prima, kc None
        log.error("Kasa: %s", e)
        return None, None, [], "UNKNOWN", str(e)
    # Due tentativi: il primo puo' usare il token in cache, e se il cloud lo
    # rifiuta (scaduto/invalidato) si rifa' il login VERO una volta sola. Piu'
    # di uno no: un cloud giu' deve fallire subito, non raddoppiare l'attesa.
    for attempt in (1, 2):
        plug, err, dev, managed = "UNKNOWN", None, None, []
        try:
            kc.login(force=(attempt == 2))
            dev = kc.resolve(cfg["kasa"].get("device_id", ""),
                             cfg["kasa"].get("plug_alias", ""))
            kids = kc.children(dev)
            if kids:  # power strip (KP303)
                wanted = cfg["kasa"].get("outlets") or [k["alias"] for k in kids]
                by_alias = {k["alias"]: k for k in kids}
                for a in wanted:
                    if a in by_alias:
                        managed.append(by_alias[a])
                    else:
                        log.warning("Presa '%s' non presente sulla strip", a)
            else:  # rele' singolo
                managed.append({"alias": dev.get("alias"), "id": None,
                                "state": kc.relay_state(dev)})
            known = [m["state"] for m in managed if m["state"] is not None]
            if known:
                plug = "ON" if any(s == 1 for s in known) else "OFF"
        except Exception as e:
            err = str(e)
            # il token in cache non vale piu': buttalo e riprova col login vero.
            # Qualunque altro errore (cloud giu', rete) si comporta come prima.
            if attempt == 1 and kc.token_cached:
                log.info("Kasa: token in cache rifiutato (%s), rifaccio il login", e)
                kc.token_forget()
                continue
            log.error("Kasa: %s", e)
        break
    return kc, dev, managed, plug, err


def _kasa_power_on(kc, dev, managed, dry_run):
    """Accende le prese gestite spente. Ritorna (powered, failed, err)."""
    to_on = [m for m in managed if m["state"] == 0]
    if not to_on:
        return False, [], None
    if dry_run:
        log.info("[dry-run] avrei acceso: %s", [m["alias"] for m in to_on])
        return False, [], None
    powered, failed, err = False, [], None
    for m in to_on:
        try:
            kc.set_outlet(dev, m["id"], True)
            m["state"] = 1
            powered = True
        except Exception as e:
            err = str(e)
            failed.append(m["alias"])
            log.error("Accensione presa '%s': %s", m["alias"], e)
    return powered, failed, err


def _teardown_text(prefix, td):
    """Riga riassuntiva dell'esito teardown (solo i passi eseguiti: con
    keep_cooler il passo 'cooler' e' assente)."""
    order = [("stop piano", "stop"), ("cooler off", "cooler"),
             ("mount home", "home"), ("OF2 chiuso", "flat")]
    parts = []
    for name, key in order:
        if key not in td:
            continue
        ok, det = td.get(key, (False, "n/d"))
        parts.append(f"{name} {'OK' if ok else 'X (' + (det or '') + ')'}")
    return prefix + "\n" + " · ".join(parts)


def _kasa_power_off_all(kc, dev, dry_run):
    """Spegne TUTTE le prese della strip (o il rele' singolo) — fine flusso flat.
    Ritorna (n_off, failed, err)."""
    if kc is None or dev is None:
        return 0, [], "Kasa non disponibile"
    if dry_run:
        log.info("[dry-run] avrei spento TUTTE le prese KASA")
        return 0, [], None
    try:
        kids = kc.children(dev)
    except Exception as e:
        return 0, [], str(e)
    targets = kids or [{"id": None, "alias": dev.get("alias")}]
    n, failed, err = 0, [], None
    for k in targets:
        try:
            kc.set_outlet(dev, k.get("id"), False)
            n += 1
        except Exception as e:
            err = str(e)
            failed.append(k.get("alias"))
            log.error("Spegnimento presa '%s': %s", k.get("alias"), e)
    return n, failed, err


def run_cycle(cfg: dict, state: dict, files: dict, dry_run: bool) -> dict:
    tz = ZoneInfo(cfg["timezone_observatory"])
    now = datetime.now(tz)
    http_cfg = cfg.get("http", {})

    tu = cfg["kasa"].get("terminal_uuid") or state.get("terminal_uuid") or str(uuid.uuid4())
    state["terminal_uuid"] = tu

    tg = Telegram(load_kv_file(files["telegram"]),
                  cfg.get("telegram", {}).get("enabled", True))

    def notify(text, keyboard=None):
        if dry_run:
            log.info("[dry-run TG] %s", text)
        else:
            tg.send(text, keyboard)

    alerts = state.setdefault("alerts", {})
    alert_cd = cfg.get("alert_cooldown_minutes", 30) * 60

    def alert(key, text):
        last = alerts.get(key)
        last_dt = datetime.fromisoformat(last) if last else None
        if last_dt is None or (now - last_dt).total_seconds() >= alert_cd:
            notify(text)
            alerts[key] = now.isoformat()

    def clear_alert(key):
        alerts.pop(key, None)

    status_int = cfg.get("status_interval_minutes", 10) * 60

    def status_due(key):
        last = state.get(key)
        last_dt = datetime.fromisoformat(last) if last else None
        return last_dt is None or (now - last_dt).total_seconds() >= status_int

    def status_sent(key):
        state[key] = now.isoformat()

    depression = cfg.get("nautical", {}).get("depression", 12)
    in_night, n_start, n_end, night_id = nautical_window(now, cfg["location"], tz, depression)
    roof = parse_alpaca(http_get_json(alpaca_issafe_url(cfg["roof"]), http_cfg))

    ac = AsiairControl(cfg.get("asiair", {}))
    acfg = cfg.get("asiair", {})
    sc = cfg.get("sync_module", {})
    ff = cfg.get("flat_flow", {}) or {}

    # Marker di SHUTDOWN MANUALE (bot Telegram /sfro): se lo shutdown e' avvenuto
    # DOPO l'inizio della notte corrente, l'agente NON riaccende il rig per il
    # resto della notte (lo Start manuale dal bot cancella il marker). Uno
    # shutdown diurno (dopo i flat) non blocca la notte successiva.
    mo_file = Path(cfg.get("manual_shutdown_file",
                           "/var/lib/sfro-agent/manual_shutdown.json"))

    def manual_shutdown_active() -> bool:
        try:
            ts = datetime.fromisoformat(json.loads(mo_file.read_text())["ts"])
        except Exception:
            return False
        if n_start is None:
            return False
        # riferimento = inizio notte MENO il lead dell'avvio anticipato: copre
        # anche uno shutdown manuale dato nella finestra T-5 pre-crepuscolo
        lead = float((cfg.get("startup", {}) or {}).get("lead_minutes", 5))
        return ts >= n_start - timedelta(minutes=lead)

    # File di scambio col bot per i FLAT/DARK MANUALI (bottone Flat/Dark del
    # menu /sfro, richiesta utente 2026-08-01): il bot scrive la RICHIESTA (ha
    # gia' fermato piano/autorun, parcheggiato, chiuso OF2, acceso il cooler e
    # messo la fascia anticondensa in asciugatura), l'agente prende in carico
    # il flusso; a fine flusso l'agente CHIEDE lo spegnimento e il bot scrive
    # la RISPOSTA.
    # Due file distinti e un solo scrittore per file: nessuna corsa su state.json.
    mf_req = Path(cfg.get("manual_flat_request_file",
                          "/var/lib/sfro-agent/flat_request.json"))
    mf_rep = Path(cfg.get("manual_flat_reply_file",
                          "/var/lib/sfro-agent/flat_reply.json"))
    # STOP dell'ATTESA flat dal menu (richiesta utente 2026-08-15): durante i
    # 30' di asciugatura si puo' annullare il flusso e riprendersi il rig.
    fc_req = Path(cfg.get("flat_cancel_request_file",
                          "/var/lib/sfro-agent/flat_cancel.json"))

    # --- Kasa: lettura stato ---
    kc, dev, managed, plug, kasa_err = _kasa_connect(cfg, files, http_cfg, tu)
    if kasa_err and plug == "UNKNOWN":
        alert("kasa", f"⚠️ Kasa non raggiungibile: {kasa_err}")
    else:
        clear_alert("kasa")

    powered = False
    snap = None

    # ---------------- CADENZA del sync incrementale (2026-08-15) ----------------
    # Il costo di una passata rsync e' quasi tutto SCANSIONE dell'albero CIFS
    # attraverso la VPN, ed e' costante: non dipende da quanti file nuovi ci
    # sono. Farla a OGNI ciclo (com'era fino a ieri) con l'agente passato a 3
    # minuti avrebbe voluto dire scandire piu' spesso per spostare ogni volta
    # meno roba: il ciclo notturno misurato durava 30 s contro i 2 s di quello
    # diurno, e quasi tutto era questo piu' il push su Sheets.
    # Ora la passata PERIODICA ha una cadenza sua. I punti FISSI — fine piano
    # (final_sync), flat, teardown, bottone Rsync del bot — NON passano di qui:
    # sincronizzano sempre, la cadenza non li riguarda.
    sync_int = float(sc.get("sync_interval_minutes", 15)) * 60

    def sync_mark():
        """Segna 'appena sincronizzato': lo chiamano anche le passate fisse,
        cosi' la periodica non riparte subito dopo una passata completa."""
        state["last_sync_ts"] = now.isoformat()

    def sync_due() -> bool:
        last = state.get("last_sync_ts")
        try:
            last_dt = datetime.fromisoformat(last) if last else None
        except (TypeError, ValueError):
            last_dt = None          # stato vecchio o corrotto: sincronizza
        return last_dt is None or (now - last_dt).total_seconds() >= sync_int

    def sync_periodic():
        """Passata incrementale a cadenza. Ritorna il risultato del sync, o
        None se non era il momento (o se il sync e' disabilitato).
        Il timestamp si aggiorna PRIMA della passata e anche quando fallisce: a
        VPN giu' non si martella ogni ciclo, si riprova alla cadenza dopo —
        l'avviso di errore parte comunque."""
        if not sc.get("enabled") or not sync_due():
            return None
        sync_mark()
        r = sync_pass(sc, dry_run)
        if r.get("error"):
            alert("sync", f"⚠️ Sync FITS in errore: {r['error']}")
        else:
            clear_alert("sync")
        return r

    def final_sync():
        """Sync FITS finale + riepilogo. Solo se sync abilitato. Avvisa
        all'INIZIO e alla FINE (richiesta utente: l'rsync via VPN puo'
        durare minuti e deve essere visibile)."""
        if not sc.get("enabled"):
            return
        notify("💾 Sync FITS finale verso il NAS avviato (può richiedere alcuni minuti)…")
        sync_mark()
        r = sync_pass(sc, dry_run)
        if r.get("error"):
            notify(f"⚠️ Sync FITS finale in errore: {r['error']}")
        else:
            notify(f"💾 Sync FITS finale completato ({r.get('files', 0)} elementi). "
                   "Tutti salvati.")

    # ------------------ storico sessioni (SQLite + Google Sheets) ------------------
    slc = cfg.get("session_log", {}) or {}
    sl_on = bool(SL and slc.get("enabled"))

    def session_update():
        """Ingest FITS nuovi + mirror su Sheets (per-ciclo, best-effort)."""
        if not sl_on:
            return
        if dry_run:
            log.info("[dry-run] session log: ingest+push")
            return
        try:
            SL.update_and_push(cfg)
            clear_alert("sessionlog")
        except Exception as e:
            alert("sessionlog", f"⚠️ Storico sessioni (Drive): {e}")

    def session_finalize(cause):
        """Chiusura del log di notte: ingest finale, causa fine, push. Una volta."""
        if not sl_on or state.get("session_finalized"):
            return
        if dry_run:
            log.info("[dry-run] session log: finalize (%s)", cause)
            return
        state["session_finalized"] = True
        try:
            r = SL.ingest(cfg)
            n = SL.finalize(cfg, state.get("night_id"), cause)
            if r.get("new") or n:
                p = SL.push(cfg)
                if p.get("error"):
                    raise RuntimeError(p["error"])
                # dashboard statistiche rigenerata a fine notte (best-effort)
                rep = SL.build_report(cfg)
                url = (cfg.get("report", {}) or {}).get("url", "")
                txt = f"📒 Diario aggiornato su Drive (fine: {cause})."
                if p.get("csv_error"):
                    txt += f"\n⚠️ Dettaglio CSV non salvato sul NAS: {p['csv_error']}"
                if url and not rep.get("error"):
                    txt += f"\n📊 Statistiche della notte: {url}"
                notify(txt)
            clear_alert("sessionlog")
        except Exception as e:
            alert("sessionlog", f"⚠️ Storico sessioni (chiusura {cause}): {e}")

    def do_teardown(prefix, cause):
        # Per alba/fine-piano (NON chiusura meteo) segue il flusso flat: cooler
        # MANTENUTO cosi' i flat escono alla stessa temperatura dei light.
        ff_next = bool(ff.get("enabled")) and cause in ("alba_nautica", "piano_fermo_10m")
        td = ac.teardown(keep_cooler=ff_next)
        txt = _teardown_text(prefix, td)
        # RESET del piano a fine notte, subito dopo il teardown e PRIMA del
        # timer flat (richiesta utente 2026-07-08): un piano interrotto non
        # riparte la sera dopo senza reset. Solo fine sessione naturale, NON
        # chiusura meteo (lì il piano deve poter riprendere da dov'era).
        if cause in ("alba_nautica", "piano_fermo_10m"):
            okr, detr = ac.reset_plan()
            if okr:
                txt += f"\n🔁 Piano resettato per stasera ({detr})."
            else:
                txt += (f"\n⚠️ RESET del piano NON riuscito ({detr}): "
                        "resettalo dall'app prima di stasera.")
                log.error("Reset piano fallito: %s", detr)
        if ff_next:
            txt += ("\n🧊 Cooler MANTENUTO per i flat — asciugatura pannello "
                    f"{int(ff.get('dry_wait_minutes', 25))} min, poi autorun flat.")
            # fascia anticondensa (uscita dew_heater) al massimo PRIMA del
            # timer di asciugatura (richiesta utente 2026-07-09); errore non
            # bloccante
            hv = int(ff.get("dew_heater_dry_pct", 100))
            okh, deth = ac.set_output("dew_heater", hv)
            if okh:
                txt += f"\n🔥 Fascia anticondensa al {hv}% per l'asciugatura."
            else:
                txt += f"\n⚠️ Fascia anticondensa NON impostata al {hv}% ({deth})."
        notify(txt)
        final_sync()
        session_finalize(cause)
        state["teardown_done"] = True
        state["imaging_active"] = False
        if ff_next:
            ok_flat, det_flat = td.get("flat", (False, "n/d"))
            if ok_flat:
                state["flat_stage"] = "drying"
                state["flat_closed_ts"] = now.isoformat()
                # countdown flats (richiesta utente 2026-07-07): primo avviso
                # subito dopo lo storico, poi uno per ciclo in flat_flow_cycle.
                # I minuti residui sono calcolati sull'orologio REALE: il sync
                # finale sopra puo' aver gia' consumato parte dell'attesa.
                rem_s = (float(ff.get("dry_wait_minutes", 25)) * 60
                         - (datetime.now(tz) - now).total_seconds())
                rem_m = max(1, int((rem_s + 59) // 60))
                notify(f"⏳ Attesa Flats -{rem_m} minuti")
                state["flat_wait_notified"] = rem_m
            else:
                state["flat_stage"] = "error"
                state["flat_error"] = f"{now.isoformat()} pannello non chiuso ({det_flat})"
                log.error("Flusso flat: pannello non chiuso (%s)", det_flat)
                notify(f"⛔ Flat ANNULLATI: pannello non chiuso ({det_flat}). "
                       "Rig lasciato acceso: intervieni tu.")

    def weather_hold():
        """Chiusura meteo con INTENTO di ripresa (richiesta utente 2026-07-24):
        ferma il piano, parcheggia il mount e chiude OF2 (attivita' di fermo/home),
        ma MANTIENE il cooler+anti-dew (ripresa rapida e alla stessa temperatura)
        e NON resetta il piano ne' finalizza la sessione (deve poter riprendere
        dal punto interrotto). Imposta 'weather_closed': il recupero avviene alla
        riapertura del tetto (weather_recovery)."""
        td = ac.teardown(keep_cooler=True)
        notify(_teardown_text(
            "⛈️ Tetto CHIUSO (meteo) durante la ripresa. Piano in PAUSA, "
            "cooler mantenuto: riprendo dal punto interrotto alla riapertura.", td))
        final_sync()   # metti al sicuro i FITS gia' ripresi
        state["imaging_active"] = False
        state["teardown_done"] = True     # blocca l'avvio generico: riparte solo il recupero
        state["weather_closed"] = True
        for k in ("weather_manual", "weather_restart_done",
                  "weather_reopen_notified", "weather_idle_ts",
                  "weather_resume_announced"):
            state.pop(k, None)

    def weather_recovery(snap, autorun_on):
        """Tetto RIAPERTO dopo una chiusura meteo, piano non ancora in corso
        (richiesta utente 2026-07-24). Setup FERMO -> riavvio automatico del piano
        dal punto interrotto (una sola volta). Attivita' manuale in corso (autorun)
        -> non tocco nulla, avviso della riapertura. Riavvio non riuscito o
        attivita' manuale poi terminata -> promemoria ogni status_int per l'avvio
        manuale. Ritorna il cycle result (il chiamante fa 'return')."""
        res = lambda: _cycle_result(now, in_night, night_id, roof, plug,
                                    powered, snap, kasa_err)
        if autorun_on:
            # attivita' manuale in corso: NON toccare, avvisa una volta
            state["weather_manual"] = True
            if not state.get("weather_reopen_notified"):
                state["weather_reopen_notified"] = True
                notify("☀️ Tetto RIAPERTO, ma c'è un autorun in corso: non tocco "
                       "nulla. Avvia tu la ripresa a mano quando vuoi.")
            return res()
        # setup FERMO (nessun piano/autorun in corso)
        if not state.get("weather_manual") and not state.get("weather_restart_done"):
            # riavvio AUTOMATICO (una sola volta) dal punto interrotto
            if not ac.all_connected(snap):
                miss = ", ".join(ac.missing_devices(snap)) or "—"
                alert("weather_restart", f"☀️ Tetto RIAPERTO ma device non tutti "
                      f"connessi (mancano: {miss}). Collegali dall'app: riprendo appena pronti.")
                return res()   # ritenta al prossimo ciclo (nessun tentativo ancora fatto)
            state["weather_restart_done"] = True
            notify("☀️ Tetto RIAPERTO, setup fermo: riavvio il piano dal punto interrotto…")
            if dry_run:
                log.info("[dry-run] avrei ripreso il piano «%s»", snap.get("plan_name"))
                return res()
            try:
                ok, detail = ac.start(snap)
            except Exception as e:
                ok, detail = False, f"eccezione: {e}"
            if ok:
                state["imaging_active"] = True
                state["plan_name"] = snap.get("plan_name")
                state.pop("weather_closed", None)
                state.pop("teardown_done", None)
                clear_alert("weather_restart")
                notify(f"▶️ Piano «{snap.get('plan_name')}» RIPRESO dopo la chiusura "
                       "meteo." + (f"\n🔥 {detail}" if detail else ""))
            else:
                alert("weather_restart", f"⚠️ Ripresa del piano non riuscita: {detail}. "
                      "Avvialo a mano; ti avviso ogni 10 min finché il setup resta fermo.")
            return res()
        # riavvio gia' tentato/declinato o attivita' manuale terminata:
        # promemoria per l'avvio MANUALE ogni status_int (fino all'alba)
        if status_due("weather_idle_ts"):
            status_sent("weather_idle_ts")
            notify("☀️ Tetto aperto e setup FERMO: puoi avviare la ripresa a mano "
                   "(la ripresa automatica non è ripartita).")
        return res()

    # ------------------ flusso flat post-sessione (macchina a stati) ------------------
    def flat_flow_cycle():
        """Un passo per ciclo. Fasi: drying (25m) -> running (autorun flat) ->
        sync -> cooler off -> shutdown ASIAIR -> KASA tutta OFF -> done.
        Nel flusso MANUALE (bottone Flat/Dark del bot, 2026-08-01) al posto
        dello shutdown automatico c'e' la fase 'ask_shutdown': si CHIEDE se
        spegnere e la risposta arriva dal bot col file di reply.
        Errori: STOP + tutto acceso (scelta utente 2026-07-03): stage 'error'."""
        stage = state.get("flat_stage")

        def fail(msg):
            # dettaglio anche in state+log: Telegram non basta per la diagnosi
            # a freddo (visto 2026-07-04: causa ricostruibile solo dal messaggio)
            state["flat_stage"] = "error"
            state["flat_error"] = f"{now.isoformat()} {msg}"
            log.error("Flusso flat: %s", msg)
            notify(f"⛔ {msg}\nFlusso flat INTERROTTO: rig lasciato acceso, intervieni tu.")

        def _session_filter_triples():
            """Triple (indice EFW, lettera, gain) dei LIGHT di stanotte (dal DB
            sessioni; i flat/dark ne sono esclusi per costruzione). Stesso
            filtro ripreso con gain diversi (es. 0 e 100) -> piu' triple:
            flat e dark vanno fatti PER OGNI combinazione (2026-07-24)."""
            try:
                pairs = SL.filters_gains_used(cfg, state.get("night_id"))
            except Exception as e:
                return None, f"DB sessioni: {e}"
            if not pairs:
                return None, "nessun light registrato stanotte"
            names = ac.get_wheel_names()
            if not names:
                return None, "nomi filtri EFW non leggibili"
            missing = sorted({l for l, _ in pairs if l not in names})
            if missing:
                return None, f"filtri {missing} non presenti nella EFW {names}"
            return ([(names.index(l), l, g) for l, g in pairs],
                    "+".join(sorted({l for l, _ in pairs})))

        def _start_flat_group():
            """Configura e avvia l'autorun flat del PRIMO gruppo in
            state['flat_groups'] = [gain, pct, idxs, lbl]: stesso GAIN camera
            (gli slot sono a gain 'default' -10000 -> vale quello della camera)
            e stessa luminosita' pannello. Tempo di posa AUTO: lo calcola
            l'ASIAIR e lo scrive nel campo exp dello slot (verificato live
            2026-07-24). Errori -> fail()."""
            gain, pct, idxs, lbl = state["flat_groups"][0]
            ok_g, det_g = ac.set_camera_gain(gain)
            if not ok_g:
                return fail(f"gain camera non impostato a {gain} ({det_g}).")
            ok_g, _nfr, det_cfg = ac.configure_autorun_slots("flat", set(idxs))
            if not ok_g:
                return fail(f"slot flat {lbl} non configurati: {det_cfg}.")
            ok_g, det_b = ac.set_flat_brightness(pct)
            if not ok_g:
                return fail(f"pannello non impostato al {pct}% ({det_b}).")
            ok_g, det_s = ac.start_flats()
            if not ok_g:
                ac.set_flat_brightness(int(ff.get("idle_brightness", 5)))  # luce giu'
                return fail(f"autorun flat {lbl} NON avviato: {det_s}.")
            state["flat_stage"] = "running"
            state["flat_started_ts"] = now.isoformat()
            notify(f"💡 Pannello al {pct}% per {lbl} (gain {gain}): "
                   f"autorun flat avviato ({det_cfg}).")

        def _start_dark_group():
            """Avvia i dark flat del PRIMO gruppo in state['dark_groups'] =
            [gain, {idx: exp}, lbl]: gain camera al valore del gruppo (slot a
            gain default) e per ogni filtro il TEMPO DEL FLAT corrispondente
            scritto nello slot dark (richiesta utente 2026-07-24). Errori ->
            fail()."""
            gain, exp_map, lbl = state["dark_groups"][0]
            ok_g, det_g = ac.set_camera_gain(gain)
            if not ok_g:
                return fail(f"gain camera non impostato a {gain} ({det_g}).")
            # tempo del flat arrotondato al CENTESIMO (richiesta utente
            # 2026-08-15): il dark flat deve avere lo stesso tempo del flat fino
            # al centesimo, se no PixInsight non li accoppia (flat 5.74 con dark
            # 5.70 non riconosciuto come coppia)
            exps = {int(i): ac.round_exp(e) for i, e in exp_map.items()}
            # max_exp sul preset PRE-modifica: eventuali dark 'library' a lunga
            # posa ricreati ad hoc dall'utente non vanno MAI toccati
            max_e = float(ff.get("dark_flat_max_exp_seconds", 30))
            ok_g, _nfr, det_cfg = ac.configure_autorun_slots(
                "dark", set(exps), max_e, exps)
            if not ok_g:
                return fail(f"slot dark flat {lbl} non configurati: {det_cfg}.")
            ok_g, det_s = ac.start_flats()
            if not ok_g:
                return fail(f"autorun dark flat {lbl} NON avviato: {det_s}.")
            state["flat_stage"] = "darks"
            state["flat_started_ts"] = now.isoformat()
            lm = state.get("flat_letters") or {}
            ttxt = " · ".join(f"{lm.get(str(i), '?')} {e:g}s"
                              for i, e in sorted(exps.items()))
            notify(f"🌑 Dark flat {lbl} (gain {gain}) avviati ({det_cfg}). "
                   f"Pose: {ttxt}.")

        def ask_shutdown(again=False):
            """Domanda a bottoni di fine flusso MANUALE: spengo o lascio acceso?
            I bottoni li serve il bot (callback fs:yes / fs:no)."""
            notify(("🌓 Flat e dark completati e sincronizzati sul NAS.\n"
                    if not again else
                    "❓ Aspetto ancora la risposta (flat e dark sono finiti).\n")
                   + "Spengo l'ASIAIR e tolgo corrente alle prese?",
                   keyboard=[[{"text": "✅ Sì, spegni tutto",
                               "callback_data": "fs:yes"},
                              {"text": "❌ No, lascia acceso",
                               "callback_data": "fs:no"}]])

        if stage == "ask_shutdown":
            # in attesa della risposta dal bot (file di reply). Nessuna risposta
            # -> ridomanda ogni status_int; oltre il timeout decide da sola la
            # via prudente (rig ACCESO, come per ogni errore del flusso).
            try:
                rep = json.loads(mf_rep.read_text())
            except Exception:
                rep = None
            if rep is None:
                asked = datetime.fromisoformat(state.get("flat_ask_ts", now.isoformat()))
                tmo = float(ff.get("manual_ask_timeout_minutes", 60))
                if (now - asked).total_seconds() > tmo * 60:
                    state["flat_stage"] = "done"
                    notify(f"⌛ Nessuna risposta entro {int(tmo)} min: NON spengo "
                           "nulla, rig lasciato ACCESO. Usa /sfro → Shutdown "
                           "quando vuoi.")
                elif status_due("flat_ask_reminder_ts"):
                    status_sent("flat_ask_reminder_ts")
                    ask_shutdown(again=True)
                return
            if not rep.get("done"):
                return          # il bot sta gia' eseguendo: aspetta in silenzio
            mf_rep.unlink(missing_ok=True)
            if rep.get("answer") == "keep":
                state["flat_stage"] = "done"
                notify("👍 Flat e dark conclusi: rig lasciato ACCESO come "
                       "richiesto. L'agente non riavvia nulla stanotte.")
            elif rep.get("ok"):
                state["flat_stage"] = "done"
                notify("🔌 Sessione conclusa: ASIAIR spento e prese OFF "
                       "(comando dal menù). Tutto a riposo. 🌅")
            else:
                fail(f"spegnimento richiesto dal menù NON riuscito "
                     f"({rep.get('detail') or 'vedi messaggi del bot'}).")
            return

        if stage == "drying":
            closed = datetime.fromisoformat(state["flat_closed_ts"])
            left_s = (float(ff.get("dry_wait_minutes", 25)) * 60
                      - (now - closed).total_seconds())
            if left_s > 0:
                # countdown "Attesa Flats -N minuti" ad ogni ciclo (5m); dedupe
                # sul valore per non ripetere lo stesso -N su cicli ravvicinati
                rem_m = max(1, int((left_s + 59) // 60))
                if rem_m != state.get("flat_wait_notified"):
                    notify(f"⏳ Attesa Flats -{rem_m} minuti")
                    state["flat_wait_notified"] = rem_m
                return
            if dry_run:
                log.info("[dry-run] flat flow: pannello %s%% + avvio autorun",
                         ff.get("brightness", 85))
                return
            # GATE TEMPERATURA (richiesta utente 2026-07-24): flat/dark alla
            # STESSA temperatura dei light (target letto dalla camera, e' quello
            # impostato dall'app). Normalmente il cooler e' rimasto acceso dal
            # teardown; se e' spento -> cooler+anti-dew ON; se la camera e'
            # sopra target+tolleranza -> ASPETTA (ricontrollo ogni ciclo, con
            # timeout: oltre -> STOP col rig acceso, policy errori del flusso).
            okt, temp, target, cooler, dett = ac.camera_cooling()
            if not okt:
                return fail(f"temperatura camera non leggibile ({dett}).")
            if cooler is False:
                okc, detc = ac.cooler_on()
                if not okc:
                    return fail(f"cooler non riacceso ({detc}).")
                oka, deta = ac.ensure_anti_dew()
                notify(f"🧊 Cooler era SPENTO: raffreddamento riacceso (camera "
                       f"{temp:.1f}°C, target {target:.1f}°C)"
                       + (", anti-dew ON." if oka else
                          f", ⚠️ anti-dew non verificato ({deta})."))
            tol = float(ff.get("camera_temp_tolerance", 2.0))
            if temp > target + tol:
                started = state.get("flat_cool_ts")
                max_m = float(ff.get("camera_cool_max_minutes", 30))
                if started and (now - datetime.fromisoformat(started)
                                ).total_seconds() > max_m * 60:
                    return fail(f"camera NON a temperatura dopo {int(max_m)} min "
                                f"({temp:.1f}°C, target {target:.1f}°C): "
                                "controlla il cooler.")
                if not started:
                    state["flat_cool_ts"] = now.isoformat()
                notify(f"🧊 Camera a {temp:.1f}°C (target {target:.1f}°C): "
                       "flat in attesa del raffreddamento.")
                return
            state.pop("flat_cool_ts", None)
            if ff.get("session_slots", False):
                # PRIMA PASSATA: solo gli slot FLAT delle combinazioni
                # (filtro, gain) usate stanotte, RAGGRUPPATI per (gain camera,
                # luminosita' pannello) — dal 2026-07-24 LRGB 50% / SHO 75% e
                # gain uguale a quello dei light (il pannello non e' lineare
                # nella notte: tempo AUTO + gain reale). Un autorun per gruppo.
                # I dark flat verranno in ultima passata a pannello spento
                # (il gap flat->dark e' ~1-2s e un taglio luce al volo
                # sporcherebbe il primo dark). Errori -> STOP.
                triples, det_u = _session_filter_triples()
                if triples is None:
                    return fail(f"filtri di sessione non determinabili ({det_u}).")
                bmap = ff.get("brightness_by_filter") or {}
                bmap0 = ff.get("brightness_by_filter_gain0") or {}
                groups = {}
                for idx, letter, gain in triples:
                    pct = int(bmap.get(letter, ff.get("brightness", 85)))
                    # GAIN 0: pannello piu' luminoso per gli RGB — al 50% il
                    # tempo AUTO tocca il tetto ASIAIR (~15s) e il calcolo puo'
                    # FALLIRE (2026-07-26: flat B mai partito). MAI 0%.
                    if gain == 0 and letter in bmap0:
                        pct = int(bmap0[letter])
                    g = groups.setdefault((gain, pct), ([], []))
                    g[0].append(idx)
                    g[1].append(letter)
                state["flat_filters"] = sorted({i for i, _, _ in triples})
                state["flat_letters"] = {str(i): l for i, l, _ in triples}
                state["flat_times"] = {}
                state["flat_groups"] = [
                    [gain, pct, sorted(ix), "+".join(ls)]
                    for (gain, pct), (ix, ls) in sorted(groups.items(),
                                                        reverse=True)]
                return _start_flat_group()
            # senza session_slots: un solo autorun con la luminosita' di default
            b = int(ff.get("brightness", 85))
            ok, det = ac.set_flat_brightness(b)
            if not ok:
                return fail(f"pannello non impostato al {b}% ({det}).")
            ok, det = ac.start_flats()
            if not ok:
                ac.set_flat_brightness(int(ff.get("idle_brightness", 5)))  # luce giu'
                return fail(f"autorun flat NON avviato: {det}.")
            state["flat_stage"] = "running"
            state["flat_started_ts"] = now.isoformat()
            notify(f"💡 Pannello al {b}%, autorun flat avviato.")
            return

        if stage in ("running", "darks"):
            label = "flat" if stage == "running" else "dark flat"
            if dry_run:
                log.info("[dry-run] flat flow: monitoraggio autorun %s", label)
                return
            started = datetime.fromisoformat(state["flat_started_ts"])
            run_s = (now - started).total_seconds()
            max_s = float(ff.get("flat_max_minutes", 90)) * 60
            ok, working, left, prog, det = ac.flats_status()
            if not ok:
                if run_s > max_s:
                    fail(f"ASIAIR illeggibile durante i {label} oltre il tempo "
                         f"massimo ({det}).")
                return  # transitorio: riprova al prossimo ciclo
            if working:
                if run_s > max_s:
                    fail(f"{label} oltre il tempo massimo ({int(max_s / 60)} min): "
                         "controlla l'autorun sull'ASIAIR.")
                    return
                # avanzamento ad ogni ciclo (richiesta utente 2026-07-24: mai
                # silente durante flat/dark): gruppo corrente, posa, residuo
                cur = ""
                if stage == "running" and state.get("flat_groups"):
                    g = state["flat_groups"][0]
                    cur = f" {g[3]} (gain {g[0]})"
                elif stage == "darks" and state.get("dark_groups"):
                    g = state["dark_groups"][0]
                    cur = f" {g[2]} (gain {g[0]})"
                rem_m = max(1, int((left + 59) // 60))
                notify(f"⏳ {label.capitalize()}{cur} in corso"
                       + (f": posa {prog}" if prog else "")
                       + f", ~{rem_m} min residui.")
                return
            # cattura ferma: completata o interrotta a meta'?
            if left > 60:
                return fail(f"autorun {label} FERMO con ~{int(left / 60)} min residui "
                            "(interrotto/errore).")
            if stage == "running" and state.get("flat_groups"):
                grp = state["flat_groups"].pop(0)   # [gain, pct, idxs, lbl]
                # tempi AUTO calcolati da ASIAIR: li scrive nel campo exp dello
                # slot (verificato live 2026-07-24); servono ai dark flat
                exps = ac.read_flat_exps(set(grp[2]))
                if exps is None:
                    return fail(f"tempi auto dei flat {grp[3]} non leggibili "
                                "dagli slot: dark flat non configurabili.")
                ft = state.setdefault("flat_times", {})
                for i, e in exps.items():
                    ft[f"{i}:{grp[0]}"] = e
                lm = state.get("flat_letters") or {}
                ttxt = " · ".join(f"{lm.get(str(i), '?')} {e:g}s"
                                  for i, e in sorted(exps.items()))
                notify(f"✅ Flat {grp[3]} (gain {grp[0]}) completati "
                       f"(~{int(run_s / 60)} min). Tempi auto: {ttxt}.")
                if state["flat_groups"]:
                    # prossimo gruppo (altro gain e/o luminosita' pannello)
                    return _start_flat_group()
            elif stage == "darks" and state.get("dark_groups"):
                grp = state["dark_groups"].pop(0)   # [gain, {idx: exp}, lbl]
                notify(f"✅ Dark flat {grp[2]} (gain {grp[0]}) completati "
                       f"(~{int(run_s / 60)} min).")
                if state["dark_groups"]:
                    return _start_dark_group()
            else:
                notify(f"✅ {label.capitalize()} completati (~{int(run_s / 60)} min).")
            if stage == "running":
                ac.set_flat_brightness(int(ff.get("idle_brightness", 5)))  # chiuso e spento
                if ff.get("session_slots", False) and ff.get("dark_flats", False):
                    # SECONDA PASSATA: dark flat a pannello ormai spento, un
                    # autorun PER GAIN (slot a gain default -> gain camera) col
                    # tempo del flat corrispondente scritto in ogni slot
                    # (richiesta utente 2026-07-24). Errori -> STOP.
                    ft = state.get("flat_times") or {}
                    if not ft:
                        return fail("tempi dei flat non in stato: "
                                    "dark flat non configurabili.")
                    by_gain = {}
                    for key, e in ft.items():
                        i_s, g_s = key.split(":")
                        by_gain.setdefault(int(g_s), {})[i_s] = float(e)
                    lm = state.get("flat_letters") or {}
                    state["dark_groups"] = [
                        [g, m, "+".join(sorted(lm.get(i, "?") for i in m))]
                        for g, m in sorted(by_gain.items(), reverse=True)]
                    return _start_dark_group()
            # sync finale: i flat vanno sul NAS (restano FUORI dal log Sheets).
            # Avviso a INIZIO e FINE (richiesta utente: rsync via VPN lento)
            if sc.get("enabled"):
                notify("💾 Sync NAS dei flat avviato (può richiedere alcuni minuti)…")
                sync_mark()   # passata fissa: la periodica non riparte subito
                r = sync_pass(sc, dry_run)
                if r.get("error"):
                    return fail(f"sync NAS finale in errore: {r['error']}. "
                                "Spegnimento ANNULLATO.")
                notify(f"💾 Sync NAS completato ({r.get('files', 0)} elementi, "
                       "flat inclusi).")
            # FLAT/DARK MANUALI dal bot (2026-08-01): lo spegnimento NON e'
            # automatico, si CHIEDE. Lo esegue poi il bot (do_shutdown, che
            # riporta anche la fascia anticondensa al valore di riposo) e la
            # risposta torna qui col file di reply.
            if state.get("flat_ask_shutdown"):
                state["flat_stage"] = "ask_shutdown"
                state["flat_ask_ts"] = now.isoformat()
                state.pop("flat_ask_reminder_ts", None)
                mf_rep.unlink(missing_ok=True)   # eventuale risposta vecchia
                ask_shutdown()
                return
            # fascia anticondensa al valore di riposo PRIMA dello shutdown
            # finale (richiesta utente 2026-07-09; dal 2026-07-30 il riposo e'
            # 5% = "spenta", non piu' 50%). 5 e non 0 perche' con value 0 il
            # firmware forza state=false. Errore non bloccante
            hv = int(ff.get("dew_heater_end_pct", 5))
            okh, deth = ac.set_output("dew_heater", hv)
            if okh:
                notify(f"🔥 Fascia anticondensa riportata al {hv}%"
                       + (" (spenta)." if hv <= 5 else "."))
            else:
                notify(f"⚠️ Fascia anticondensa NON riportata al {hv}% ({deth}); "
                       "proseguo con lo shutdown.")
            # NIENTE cooler off qui (scelta utente 2026-07-03 dopo il test live):
            # cooler e antidew li gestisce lo shutdown dell'ASIAIR stesso.
            ok, det = ac.shutdown()
            if not ok:
                return fail(f"shutdown ASIAIR fallito ({det}). KASA lasciata accesa.")
            # KASA giu' SOLO quando l'ASIAIR ha davvero smesso di rispondere:
            # ping morto, oppure ping vivo ma app spenta (vedi wait_asiair_down)
            down, mode, ddet = wait_asiair_down(
                ac.host, ac.port,
                float(ff.get("shutdown_wait_seconds", 15)),
                float(ff.get("shutdown_ping_timeout_seconds", 120)),
                float(ff.get("shutdown_grace_seconds", 90)))
            if not down:
                return fail(f"{ddet}: KASA lasciata accesa.")
            if mode == "app":
                notify(f"⚠️ {ddet}: tolgo comunque corrente (è l'unico modo).")
            n_off, k_failed, k_err = _kasa_power_off_all(kc, dev, dry_run)
            if k_failed or (n_off == 0 and k_err):
                return fail(f"spegnimento KASA incompleto ({k_failed or k_err}).")
            state["flat_stage"] = "done"
            notify(f"🔌 ASIAIR spento e KASA tutta OFF ({n_off} prese). "
                   "Sessione conclusa, tutto a riposo. 🌅")
            return

    # ------------- INIZIALIZZAZIONE DEL RIG (T-lead dalla notte nautica) -------------
    # Dal 2026-07-04 l'agente porta su il rig DA ZERO (specifica utente): a T-lead
    # dal crepuscolo nautico accende KASA, aspetta il boot e connette i device
    # (connect_all: PRIMING get_connected_cameras + ora al mount).
    # RIVISTO 2026-08-16 su specifica dell'utente: il T-lead passa a 10 minuti e
    # si FERMA all'inizializzazione — connessione + anti-dew + fascia + COOLER.
    # Il piano NON parte qui: parte al crepuscolo nautico, dal ramo di notte, che
    # trova i device gia' connessi. Cosi' i 10 minuti servono a quello per cui
    # esistono, portare la camera in temperatura prima della prima posa.
    # DEVE stare PRIMA del blocco flat: a T-10 flat_stage puo' essere ancora
    # 'done' dal mattino e farebbe early-return.
    su = cfg.get("startup", {}) or {}

    def startup_rig():
        nonlocal plug
        dusk_txt = n_start.astimezone(tz).strftime("%H:%M")
        if plug != "ON":
            if kc is None or not managed:
                alert("startup", "⛔ Avvio rig: KASA non raggiungibile, non posso "
                                 "accendere. Ritento al prossimo ciclo.")
                return
            ok_p, failed, perr = _kasa_power_on(kc, dev, managed, dry_run)
            if failed or not ok_p:
                alert("startup", f"⛔ Avvio rig: accensione KASA fallita "
                                 f"({failed or perr}). Ritento al prossimo ciclo.")
                return
            plug = "ON"
        notify(f"🌆 Notte nautica alle {dusk_txt}: KASA accesa, aspetto il boot "
               "dell'ASIAIR e connetto i device…")
        t0 = time.time()
        while not ping(ac.host):
            if time.time() - t0 > float(su.get("ping_timeout_seconds", 240)):
                alert("startup", "⛔ Avvio rig: l'ASIAIR non risponde al ping dopo "
                                 "l'accensione. Ritento al prossimo ciclo.")
                return
            time.sleep(3)
        ok_c, det_c = ac.connect_all()
        if not ok_c:
            alert("startup", f"⛔ Avvio rig: connessione device incompleta: {det_c}. "
                             "Ritento al prossimo ciclo.")
            return
        state["startup_night_id"] = night_id     # rig su e connesso: non ripetere
        state["asiair_power_on_ts"] = now.isoformat()
        clear_alert("startup")
        snap2 = ac.snapshot()
        if snap2 and (snap2.get("plan_started") or snap2.get("capturing")):
            notify(f"🔗 Rig avviato e connesso ({det_c}). Piano gia' in corso.")
            return
        # SOLO INIZIALIZZAZIONE (specifica utente 2026-08-16): qui il piano NON
        # parte. Il T-lead serve a dare corrente, connettere i device e portare
        # la camera in temperatura; l'avvio e' compito del ramo di notte, al
        # crepuscolo nautico, che trova i device gia' connessi e chiama start().
        # Prima del 2026-08-16 il T-5 faceva anche partire il piano: significava
        # riprendere con la camera ancora calda e con il cielo ancora chiaro.
        prep_txt = ac.prepare()
        notify(f"🔗 Rig acceso e connesso ({det_c}).\n🔥 {prep_txt}\n"
               f"⏳ Piano in attesa: parte alle {dusk_txt}, all'inizio della "
               "notte nautica.")

    # SOLO A TETTO APERTO (correzione utente 2026-07-04): se il tetto e' chiuso
    # resta tutto spento; il T-5 apre solo la FINESTRA di monitoraggio, che poi
    # prosegue per tutta la notte con la logica in-night (apertura tardiva ->
    # KASA on al ramo tetto-aperto + connect_all di fallback + avvio piano).
    if (su.get("enabled") and not in_night and n_start is not None
            and roof == "OPEN"
            and state.get("startup_night_id") != night_id
            and not manual_shutdown_active()
            and 0 <= (n_start - now).total_seconds() <= float(su.get("lead_minutes", 10)) * 60):
        if dry_run:
            log.info("[dry-run] init rig T-%s: KASA + connect_all + cooler/fascia "
                     "(piano NON avviato qui)", su.get("lead_minutes", 10))
        else:
            startup_rig()
        return _cycle_result(now, in_night, night_id, roof, plug, powered,
                             snap, kasa_err)

    # ------------- RICHIESTA MANUALE di flat/dark dal bot (2026-08-01) -------------
    # Va PRIMA del gate flat e di tutta la logica di notte: nello stesso ciclo la
    # sessione entra nel flusso flat e il resto (che a tetto chiuso/aperto
    # potrebbe riavviare il piano appena fermato dal bot) non gira piu'.
    def manual_flat_request():
        """Prende in carico la richiesta del bot: light sul NAS e nel diario
        (servono a scegliere i filtri!), poi stato = 'drying' e da qui in avanti
        e' il flusso flat di sempre, con la variante 'chiedi prima di spegnere'."""
        try:
            req = json.loads(mf_req.read_text())
        except Exception as e:
            req = {}
            log.warning("Richiesta flat manuale illeggibile (%s): uso i default", e)
        mf_req.unlink(missing_ok=True)
        mf_rep.unlink(missing_ok=True)
        if not ff.get("enabled"):
            notify("⛔ Flat/Dark manuali: flusso flat DISABILITATO in config, "
                   "non faccio nulla.")
            return
        notify("🌓 Flat/Dark MANUALI presi in carico (richiesta dal menù): "
               "chiudo la sessione di stanotte e avvio l'asciugatura.")
        # i light vanno sul NAS e nel diario PRIMA di leggere i filtri usati:
        # _session_filter_triples li cerca nel DB, che si popola dai FITS del NAS
        final_sync()
        session_finalize(req.get("cause") or "flat_manuale")
        # dentro la notte la sessione da chiudere e' QUESTA: allinea night_id
        # (serve a scegliere i filtri e impedisce che il reset di inizio notte,
        # piu' avanti nel ciclo, azzeri il flusso appena impostato)
        if in_night:
            state["night_id"] = night_id
        state["flat_stage"] = "drying"
        state["flat_closed_ts"] = req.get("closed_ts") or now.isoformat()
        state["flat_manual"] = True
        state["flat_ask_shutdown"] = bool(req.get("ask_shutdown", True))
        state["imaging_active"] = False
        state["teardown_done"] = True
        state.pop("flat_wait_notified", None)
        state.pop("flat_cool_ts", None)
        # la pausa meteo non ha piu' senso: la notte e' chiusa per scelta
        for k in ("weather_closed", "weather_manual", "weather_restart_done",
                  "weather_reopen_notified", "weather_idle_ts",
                  "weather_resume_announced"):
            state.pop(k, None)

    if mf_req.exists():
        if dry_run:
            log.info("[dry-run] richiesta flat manuale presente: la lascio")
        else:
            manual_flat_request()

    # ---------- STOP dell'ATTESA flat dal menu (richiesta utente 2026-08-15) ----------
    # Va QUI, prima del blocco flat: nel ciclo in cui arriva l'annullo i flat non
    # devono partire nemmeno se i 30' di asciugatura sono appena scaduti.
    # Dopo manual_flat_request: se arrivassero entrambe le richieste vince
    # l'ultima intenzione, cioe' lo stop.
    def flat_cancel_request():
        """Annulla il flusso flat durante l'ATTESA: nessun comando all'ASIAIR,
        il rig resta esattamente com'e' (pannello chiuso, cooler acceso, fascia
        al 100%, mount in park) e torna in carico all'utente.
        Vale SOLO in fase 'drying' (attesa asciugatura e attesa raffreddamento
        camera): un autorun flat/dark gia' partito non si tocca. La richiesta
        non applicabile viene comunque consumata (file vecchio rimasto li')."""
        fc_req.unlink(missing_ok=True)
        stage = state.get("flat_stage")
        if stage != "drying":
            notify("ℹ️ Richiesta di STOP flat ignorata: "
                   + (f"il flusso è in fase «{stage}»." if stage
                      else "nessuna attesa flat in corso."))
            return
        # 'cancelled' e NON None: non e' una fase attiva (il promemoria periodico
        # di spegnimento riparte, come chiesto) ma e' un valore PIENO, quindi i
        # rami che all'alba rifanno teardown+flat — che pretendono "nessuna fase
        # flat" — restano spenti. Il piano non riparte: lo blocca teardown_done.
        state["flat_stage"] = "cancelled"
        state["flat_cancel_ts"] = now.isoformat()
        for k in ("flat_wait_notified", "flat_cool_ts", "flat_groups",
                  "dark_groups", "flat_ask_ts", "flat_ask_reminder_ts"):
            state.pop(k, None)
        # il promemoria "spegnili tu" riparte, ma non nello stesso ciclo: il
        # messaggio qui sotto lo dice gia'. Il primo arriva un intervallo dopo.
        state["last_kasa_reminder_ts"] = now.isoformat()
        notify("🛑 Attesa flat INTERROTTA su tua richiesta: niente flat né dark.\n"
               "Il rig resta com'è (pannello chiuso, cooler acceso, fascia "
               "anticondensa al 100%, mount in park): da qui in poi decidi tu.\n"
               "Quando hai finito spegni con /sfro → Shutdown.")

    if fc_req.exists():
        if dry_run:
            log.info("[dry-run] richiesta di stop flat presente: la lascio")
        else:
            flat_cancel_request()

    # Il flusso flat ha PRECEDENZA su tutto il resto del ciclo: finche' e' attivo
    # (o concluso, fino a nuova notte) la logica normale non deve girare, altrimenti
    # vedrebbe "ASIAIR spento + tetto aperto" e riaccenderebbe la KASA appena spenta.
    if state.get("flat_stage") in FLAT_STAGES_BUSY:
        if in_night and state.get("night_id") != night_id:
            pass  # NUOVA notte: prosegui, il reset qui sotto azzera anche il flusso
        else:
            if ff.get("enabled"):
                if state.get("flat_stage") != "done":
                    flat_flow_cycle()
            else:
                state["flat_stage"] = None  # disabilitato in config: dimentica
            if state.get("flat_stage") in FLAT_STAGES_BUSY:
                return _cycle_result(now, in_night, night_id, roof, plug, powered,
                                     snap, kasa_err)

    # ============================ FUORI NOTTE NAUTICA ============================
    if not in_night:
        # SICUREZZA ALBA (richiesta utente 2026-07-24): un PIANO puo' essere in
        # corso senza che l'agente lo tracci (avviato a mano a ridosso dell'alba).
        # Sonda una volta l'ASIAIR: piano avviato -> va SEMPRE fermato (teardown
        # + flat). Un AUTORUN (capturing senza piano) invece NON si tocca.
        day_snap = None
        if (state.get("night_id") and not state.get("teardown_done")
                and not state.get("flat_stage") and not state.get("imaging_active")
                and not state.get("weather_closed")
                and not state.get("dawn_plan_checked")
                and plug == "ON" and not manual_shutdown_active()
                and ac.available and ping(ac.host)):
            day_snap = ac.snapshot()
            if day_snap and day_snap.get("reachable"):
                state["dawn_plan_checked"] = True   # una sonda sola per notte
        # fine notte con ripresa ancora attiva -> teardown dovuto (una volta)
        if state.get("imaging_active") and not state.get("teardown_done"):
            do_teardown("🌅 Fine della notte nautica.", "alba_nautica")
        elif day_snap and day_snap.get("plan_started"):
            do_teardown("🌅 Fine della notte nautica: piano ANCORA IN CORSO "
                        "(avviato a mano?) — fermato per sicurezza.", "alba_nautica")
        elif state.get("weather_closed") and not state.get("flat_stage"):
            # chiusura meteo mai riaperta entro l'alba (2026-07-24): la notte ha
            # comunque avuto light -> flat come di consueto (il cooler e' rimasto
            # acceso durante la pausa, quindi si e' alla stessa temperatura).
            do_teardown("🌅 Fine della notte nautica (era in pausa meteo).",
                        "alba_nautica")
        elif (ff.get("enabled") and state.get("night_id") and state.get("plan_stopped_ts")
              and not state.get("teardown_done") and not state.get("flat_stage")):
            # piano fermatosi poco prima dell'alba (sotto i 10m del trigger
            # fine-piano): teardown mai fatto -> falla ora e fai partire i flat
            do_teardown("🌅 Fine della notte nautica (piano gia' fermo).", "alba_nautica")
        elif state.get("night_id") and not state.get("session_finalized"):
            # piano gia' fermo all'alba: chiudi comunque il log della notte
            session_finalize("alba_nautica")
        # il recupero/promemoria meteo vale SOLO entro la notte nautica: azzera
        for k in ("weather_closed", "weather_manual", "weather_restart_done",
                  "weather_reopen_notified", "weather_idle_ts",
                  "weather_resume_announced"):
            state.pop(k, None)
        # promemoria Kasa accesa (per i flat) ogni status_int — MAI durante o
        # dopo il flusso flat automatico (la spegne lui)
        if plug == "ON" and state.get("flat_stage") not in FLAT_STAGES_BUSY:
            # dalle 22:00 italiane al crepuscolo nautico l'accensione e'
            # propedeutica alla prossima sessione: nessun promemoria
            quiet = False
            if n_start:
                dusk_it = n_start.astimezone(ZoneInfo("Europe/Rome"))
                thr = dusk_it.replace(hour=22, minute=0, second=0, microsecond=0)
                if thr > dusk_it:
                    thr -= timedelta(days=1)
                quiet = now >= thr
            if not quiet and status_due("last_kasa_reminder_ts"):
                names = ", ".join(m["alias"] for m in managed if m["state"] == 1) or "attive"
                notify(f"🔌 KASA + ASIAIR ancora ACCESI ({names}) per i flat. "
                       "L'agente non li spegne: spegnili tu quando hai finito.")
                status_sent("last_kasa_reminder_ts")
        else:
            state["last_kasa_reminder_ts"] = None
        return _cycle_result(now, in_night, night_id, roof, plug, powered, snap, kasa_err)

    # ============================== NOTTE NAUTICA ===============================
    # reset dei flag a inizio di una NUOVA notte
    if state.get("night_id") != night_id:
        state["night_id"] = night_id
        for k in ("nautical_announced", "imaging_active", "teardown_done",
                  "error_notified", "asiair_power_on_ts", "last_wait_ts",
                  "last_kasa_reminder_ts", "plan_name",
                  "plan_stopped_ts", "plan_completed", "session_finalized",
                  "plan_stall_left", "plan_stall_ts",
                  "flat_stage", "flat_closed_ts", "flat_started_ts", "flat_error",
                  "flat_filters", "flat_groups", "flat_wait_notified",
                  "flat_letters", "flat_times", "dark_groups", "flat_cool_ts",
                  "flat_manual", "flat_ask_shutdown", "flat_ask_ts",
                  "flat_ask_reminder_ts", "flat_cancel_ts",
                  "weather_closed", "weather_manual", "weather_restart_done",
                  "weather_reopen_notified", "weather_idle_ts",
                  "weather_resume_announced", "dawn_plan_checked",
                  # nuova notte = prima passata subito, senza aspettare la
                  # cadenza a partire dal sync dell'alba precedente
                  "last_sync_ts"):
            state.pop(k, None)

    # annuncio d'inizio notte (una volta), con lo stato del tetto
    if not state.get("nautical_announced"):
        state["nautical_announced"] = True
        if roof == "OPEN":
            notify("🌙 Notte nautica iniziata — tetto APERTO.")
        elif roof == "CLOSED":
            notify("🌙 Notte nautica iniziata — tetto CHIUSO. In attesa di apertura.")
        else:
            notify("🌙 Notte nautica iniziata — stato tetto SCONOSCIUTO.")

    if roof == "UNKNOWN":
        alert("roof_unknown", "⚠️ Stato tetto SCONOSCIUTO (API SafetyMonitor non risponde).")
        return _cycle_result(now, in_night, night_id, roof, plug, powered, snap, kasa_err)
    clear_alert("roof_unknown")

    if roof == "OPEN":
        reach_ping = ping(ac.host) if ac.host else False
        snap = ac.snapshot() if (reach_ping and ac.available) else None
        reachable = bool(snap and snap.get("reachable"))

        if not reachable:
            # ASIAIR irraggiungibile (spento o VPN giù). L'accensione KASA NON dipende
            # dalla VPN: la presa si comanda via cloud TP-Link, e alimentare il rig è il
            # primo passo per farlo salire (a SFRO la VPN può salire solo dopo). Quindi:
            # plug OFF -> accendi SEMPRE (se auto_power_on). La diagnosi VPN serve solo
            # per l'AVVISO quando la presa è GIÀ accesa e l'ASIAIR non risponde.
            if manual_shutdown_active():
                # Shutdown MANUALE dal bot stanotte: niente riaccensione; chiudi
                # la contabilita' della sessione senza teardown (box gia' spento,
                # altrimenti all'alba partirebbe un teardown su un rig morto).
                if state.get("imaging_active"):
                    state["imaging_active"] = False
                    state["teardown_done"] = True
                session_finalize("shutdown_manuale")
            elif cfg["kasa"].get("auto_power_on", True) and plug == "OFF":
                powered, failed, perr = _kasa_power_on(kc, dev, managed, dry_run)
                if powered:
                    plug = "ON"
                    state["asiair_power_on_ts"] = now.isoformat()
                    tail = ("Connetto i device appena l'ASIAIR risponde."
                            if su.get("enabled") else "Collega i device dall'app.")
                    notify(f"🔌 ASIAIR spento/irraggiungibile → KASA accesa (Asiair+Mount). {tail}")
                if failed:
                    alert("kasa_power", f"⚠️ Accensione Kasa fallita: {failed} ({perr})")
            elif plug == "ON":
                # presa già accesa ma ASIAIR non risponde: in boot (VPN su) o VPN/rete giù
                vpn = vpn_diagnose(ac.host, acfg.get("vpn_probe_host", ""))
                if vpn.get("vpn_up"):
                    if status_due("last_wait_ts"):
                        notify("⏳ ASIAIR acceso, in avvio… in attesa che risponda.")
                        status_sent("last_wait_ts")
                else:
                    alert("asiair_unreach",
                          f"⚠️ ASIAIR acceso ma non raggiungibile: {vpn.get('cause')}.")
            else:
                # plug UNKNOWN (lettura Kasa fallita) -> l'avviso Kasa è già stato dato
                alert("asiair_unreach", "⚠️ ASIAIR non raggiungibile e stato Kasa ignoto.")
        else:
            clear_alert("asiair_unreach")
            # distinzione PIANO vs AUTORUN (2026-07-24): plan_started = piano vero;
            # capturing senza piano = autorun (flat/dark) o posa singola, avviati a
            # mano. Il piano si interrompe/riprende/ferma all'alba; l'autorun NO.
            plan_on = bool(snap.get("plan_started"))
            capturing = bool(snap.get("capturing"))
            autorun_on = capturing and not plan_on
            busy = plan_on or capturing

            # RECUPERO DOPO CHIUSURA METEO (richiesta utente 2026-07-24): il tetto
            # e' riaperto. Il piano e' "gia' ripartito" SOLO se sta anche
            # scattando: is_plan_started resta true dopo il nostro stop (bug
            # 2026-07-25, riapertura ignorata in silenzio), quindi da solo non
            # prova nulla. Ripartito davvero -> lo riadotto e AVVISO (mai
            # silenzioso); altrimenti weather_recovery decide riavvio/promemoria.
            if state.get("weather_closed"):
                if plan_on and capturing:
                    for k in ("weather_closed", "weather_manual",
                              "weather_restart_done", "weather_reopen_notified",
                              "weather_idle_ts", "weather_resume_announced"):
                        state.pop(k, None)
                    state.pop("teardown_done", None)
                    notify(f"☀️ Tetto RIAPERTO e piano «{snap.get('plan_name')}» "
                           "già in ripresa: lo riadotto e continuo a seguirlo.")
                else:
                    return weather_recovery(snap, autorun_on)

            if busy:
                state.pop("plan_stopped_ts", None)
            elif state.get("plan_stopped_ts") and not state.get("session_finalized"):
                # piano fermo a tetto aperto da idle_finalize_minutes -> chiusura log
                stopped = datetime.fromisoformat(state["plan_stopped_ts"])
                idle_s = float(slc.get("idle_finalize_minutes", 10)) * 60
                if (now - stopped).total_seconds() >= idle_s:
                    if ff.get("enabled") and not state.get("teardown_done"):
                        # fine piano anticipata (scelta utente): teardown con
                        # cooler tenuto + flusso flat gia' in notte. Il testo
                        # dice COME e' finito il piano (2026-08-15): esaurito
                        # (fine normale) o fermo per altro motivo.
                        do_teardown(
                            "🏁 Piano COMPLETATO: chiusura sessione."
                            if state.get("plan_completed") else
                            "🏁 Piano fermo da 10 min: chiusura sessione.",
                            "piano_fermo_10m")
                    else:
                        session_finalize("piano_fermo_10m")
            if plan_on:
                # --- RIPRESA IN CORSO (piano) ---
                state["imaging_active"] = True
                state["plan_name"] = snap.get("plan_name")
                state.pop("error_notified", None)
                # il piano scatta di nuovo: qualunque "completato" precedente
                # non vale piu' (ripresa dopo pausa meteo o riavvio manuale)
                state.pop("plan_completed", None)
                # piano in corso: l'alba DEVE poter fare teardown+flat anche se un
                # teardown meteo aveva alzato il flag (sicurezza, richiesta 2026-07-24)
                state.pop("teardown_done", None)
                clear_alert("plan_error")
                # WATCHDOG STALLO (2026-08-17): la notte del 17/8 il piano
                # risultava "avviato" (is_plan_started E is_working true)
                # mentre il mount rifiutava 3162 goto di fila: zero frame per
                # 2h15m e nessun avviso. Il progresso VERO e' il residuo del
                # piano (get_plan, lo stesso del 🏁): se non scende per
                # plan_stall_minutes -> allarme, ripetuto col cooldown degli
                # alert. SOLO avviso: la diagnosi resta all'utente, l'agente
                # non tocca mai una ripresa in corso. first_delay escluso
                # (attesa programmata: residuo fermo per costruzione). NB il
                # residuo cala a fine posa: la soglia deve stare sopra
                # posa massima + flip/autofocus (300s + ~10' -> default 20').
                wd_m = float(acfg.get("plan_stall_minutes", 20))
                if wd_m > 0 and snap.get("capture_state") != "first_delay":
                    okp, left, _ = ac.plan_left()
                    if okp:
                        if state.get("plan_stall_left") != left:
                            state["plan_stall_left"] = left
                            state["plan_stall_ts"] = now.isoformat()
                            clear_alert("plan_stall")
                        elif state.get("plan_stall_ts"):
                            stall_m = (now - datetime.fromisoformat(
                                state["plan_stall_ts"])).total_seconds() / 60
                            if stall_m >= wd_m:
                                alert("plan_stall",
                                      f"🛑 Piano «{snap.get('plan_name')}» avviato ma "
                                      f"SENZA PROGRESSI da ~{int(stall_m)} min: "
                                      "residuo invariato (goto rifiutato? guida "
                                      "persa? plate solve?). Controlla l'ASIAIR "
                                      "dall'app.")
                r = sync_periodic()   # incrementale a cadenza, silenzioso
                # l'ingest legge i FITS DAL NAS: se non e' passato niente di
                # nuovo non c'e' nulla da ingerire e il giro sarebbe a vuoto.
                # Col sync disabilitato i frame arrivano per altre vie: allora
                # si ingerisce a ogni ciclo, come si e' sempre fatto.
                if not sc.get("enabled") or (r and r.get("files")):
                    session_update()   # ingest dei FITS sincronizzati + push
            elif autorun_on:
                # autorun MANUALE (flat/dark/posa) in corso: non e' il piano, non lo
                # traccio come imaging e non avvio nulla; sincronizzo e basta.
                sync_periodic()
            elif state.get("imaging_active") and not state.get("error_notified"):
                # Eravamo in ripresa e ora il piano e' fermo a tetto aperto:
                # ESAURITO o INTERROTTO? Lo dice il residuo del piano
                # (get_plan): left_time_sec a zero su tutti i target abilitati
                # = tutte le pose previste sono state fatte, ed e' una fine
                # NORMALE, non un errore (correzione richiesta dall'utente il
                # 2026-08-15: a pose esaurite arrivava l'avviso di errore).
                # error_notified resta il flag "gia' annunciato" per entrambi i
                # casi: senza, il messaggio si ripeterebbe ad ogni ciclo.
                okp, left, detp = ac.plan_left()
                idle_m = int(float(slc.get("idle_finalize_minutes", 10)))
                coda = (" Chiusura sessione"
                        + (" e flat" if ff.get("enabled") else "")
                        + f" tra ~{idle_m} minuti.")
                if okp and left <= 0:
                    state["plan_completed"] = True
                    notify(f"🏁 Piano «{state.get('plan_name') or snap.get('plan_name')}» "
                           f"COMPLETATO: tutte le pose previste sono state "
                           f"fatte ({detp})." + coda)
                elif okp:
                    notify(f"⚠️ Il piano si è FERMATO con ~{max(1, left // 60)} min "
                           f"di pose ancora da fare ({detp}). Controlla "
                           "l'ASIAIR/guida." + coda)
                else:
                    notify("⚠️ Il piano si è FERMATO in modo non previsto e il "
                           f"residuo non è leggibile ({detp}). Controlla "
                           "l'ASIAIR/guida." + coda)
                state["error_notified"] = True
                state["imaging_active"] = False
                state["plan_stopped_ts"] = now.isoformat()
            elif ac.all_connected(snap) and not state.get("teardown_done"):
                # tutti i device collegati (da app) -> AVVIO (mai dopo il
                # teardown della notte: niente riavvii post-chiusura)
                if dry_run:
                    log.info("[dry-run] avrei avviato il piano «%s»", snap.get("plan_name"))
                else:
                    try:
                        ok, detail = ac.start(snap)
                    except Exception as e:
                        ok, detail = False, f"eccezione: {e}"
                    if ok:
                        state["imaging_active"] = True
                        state["plan_name"] = snap.get("plan_name")
                        clear_alert("plan_start")
                        notify(f"▶️ Device OK, OF2 aperto, piano «{snap.get('plan_name')}» avviato."
                               + (f"\n🔥 {detail}" if detail else ""))
                    else:
                        alert("plan_start", f"⚠️ Avvio piano non riuscito: {detail}")
            elif not state.get("teardown_done"):
                # device mancanti: con startup.enabled li connette l'AGENTE
                # (connect_all, stessa ricetta dell'avvio T-5) — fallback se
                # l'avvio anticipato e' saltato. Con startup DISABILITATO
                # (modalita' 2026-06-30, RIPRISTINATA 2026-07-09 su richiesta
                # utente dopo il guasto mount): li connette l'UTENTE dall'app,
                # l'agente verifica e avvisa soltanto, poi avvia il piano al
                # ciclo in cui risultano tutti connessi (ramo qui sopra).
                if not su.get("enabled"):
                    if status_due("last_wait_ts"):
                        miss = ", ".join(ac.missing_devices(snap)) or "—"
                        notify(f"⏳ ASIAIR acceso, device non tutti connessi "
                               f"(mancano: {miss}). Collegali dall'app: avvio "
                               "il piano appena pronti.")
                        status_sent("last_wait_ts")
                elif dry_run:
                    log.info("[dry-run] avrei connesso i device (connect_all)")
                else:
                    ok_c, det_c = ac.connect_all()
                    if ok_c:
                        clear_alert("connect")
                        notify(f"🔗 Device connessi dall'agente ({det_c}). "
                               "Avvio del piano al prossimo ciclo.")
                    elif status_due("last_wait_ts"):
                        miss = ", ".join(ac.missing_devices(snap)) or "—"
                        notify(f"⏳ ASIAIR acceso ma connessione device incompleta "
                               f"({det_c}). Mancanti: {miss}. Ritento ogni ciclo.")
                        status_sent("last_wait_ts")

    elif roof == "CLOSED":
        # chiusura meteo durante la ripresa -> PAUSA con intento di ripresa
        # (richiesta utente 2026-07-24): niente reset/finalize, cooler mantenuto,
        # weather_closed per il recupero alla riapertura. Se il piano era gia'
        # ripreso dopo una pausa precedente (imaging_active + teardown_done pulito)
        # ri-pausa; se siamo gia' in pausa (imaging_active False) non fa nulla.
        if state.get("imaging_active") and not state.get("teardown_done"):
            weather_hold()
        elif state.get("weather_closed"):
            # SORVEGLIANZA in pausa meteo (2026-07-25): se durante la pausa
            # compare una cattura (riavvio MANUALE dell'utente, o mai esclusa
            # una ripresa autonoma dell'ASIAIR) NON si tocca nulla — attivita'
            # manuale sacra — ma si AVVISA: tetto chiuso + mount in posa va
            # segnalato subito (alert con cooldown, non spam).
            reach2 = ping(ac.host) if ac.host else False
            snap2 = ac.snapshot() if (reach2 and ac.available) else None
            if snap2 and snap2.get("reachable") and snap2.get("capturing"):
                alert("weather_closed_capture",
                      "⚠️ Tetto ancora CHIUSO (pausa meteo) ma c'è una CATTURA "
                      "in corso sull'ASIAIR. Non tocco nulla: verifica tu "
                      "(mount in posa a tetto chiuso).")
            else:
                clear_alert("weather_closed_capture")

    # promemoria Kasa accesa anche dentro la notte, dopo il teardown (per i flat
    # MANUALI: mai quando il flusso flat automatico e' in corso o concluso)
    if (plug == "ON" and not state.get("imaging_active") and state.get("teardown_done")
            and not state.get("weather_closed")
            and state.get("flat_stage") not in FLAT_STAGES_BUSY):
        if status_due("last_kasa_reminder_ts"):
            names = ", ".join(m["alias"] for m in managed if m["state"] == 1) or "attive"
            notify(f"🔌 KASA + ASIAIR ancora ACCESI ({names}) per i flat. "
                   "L'agente non li spegne: spegnili tu quando hai finito.")
            status_sent("last_kasa_reminder_ts")

    return _cycle_result(now, in_night, night_id, roof, plug, powered, snap, kasa_err)


def _cycle_result(now, in_night, night_id, roof, plug, powered, snap, kasa_err):
    return {
        "ts": now.isoformat(), "in_nautical_night": in_night, "night_id": night_id,
        "roof": roof, "plug": plug, "powered_now": powered,
        "kasa_error": kasa_err, "asiair": snap,
    }


def main():
    ap = argparse.ArgumentParser(description="Agente SFRO (tetto Alpaca + Kasa + Telegram + sync)")
    ap.add_argument("--config", default=str(HERE / "config.yaml"))
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="un ciclo (default)")
    mode.add_argument("--discover", action="store_true", help="elenca le prese Kasa ed esce")
    mode.add_argument("--status", action="store_true", help="stampa stato/decisione ed esce")
    ap.add_argument("--dry-run", action="store_true", help="non comanda presa, rsync in dry-run")
    args = ap.parse_args()

    cfg = load_config(Path(args.config))
    setup_logging(cfg.get("log_level", "INFO"))
    files = resolve_files(cfg)
    state_path = Path(cfg.get("state_file", str(HERE / "state.json")))
    state = load_state(state_path)

    if args.discover:
        tu = state.get("terminal_uuid") or str(uuid.uuid4())
        state["terminal_uuid"] = tu
        kc = KasaCloud(cfg["kasa"], load_kv_file(files["kasa"]), cfg.get("http", {}), tu)
        kc.login()
        for d in kc.list_devices():
            print(f"alias={d.get('alias')!r} deviceId={d.get('deviceId')} "
                  f"model={d.get('deviceModel')} "
                  f"online={'si' if d.get('status') == 1 else 'no'} "
                  f"server={d.get('appServerUrl')}")
            if d.get("status") == 1:
                for k in kc.children(d):  # prese di una power strip
                    st = "ON" if k["state"] == 1 else "OFF"
                    print(f"    presa alias={k['alias']!r} state={st} child_id={k['id']}")
        save_state(state_path, state)
        return

    try:
        pub = run_cycle(cfg, state, files, dry_run=args.dry_run or args.status)
    finally:
        # anche su eccezione: le transizioni gia' fatte (e annunciate) non
        # vanno perse, o il ciclo dopo ripeterebbe azioni/messaggi
        save_state(state_path, state)
    if args.status:
        print(json.dumps(pub, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
