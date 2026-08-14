#!/usr/bin/env python3
"""
sfro_mqtt.py — Publisher MQTT (sola lettura) per l'osservatorio SFRO.

Servizio PERSISTENTE (systemd service, non timer): pubblica la telemetria
dell'ASIAIR su un broker MQTT (default 192.0.2.20) per Home Assistant.
  - GUIDA: ascolta in streaming gli eventi GuideStep (canale 4400) e pubblica
    sfro/guide ogni `guide_interval_seconds` (default 15s) con l'RMS su finestra
    mobile `guide_window_seconds` (default 120s).
  - EVENTI PUSH (2026-08-12): ascolta la 4700 SENZA inviare nulla — l'ASIAIR
    spinge da solo PiStatus (temp CPU, undervolt, overcurrent), Sequence
    (avanzamento piano), Exposure, SaveImage. Pubblica sfro/pi e tiene
    aggiornato sfro/session in tempo reale invece che al ritmo del poll.
  - LENTI: ogni `slow_interval_seconds` (default 600s) interroga in sola lettura
    (get_*) e pubblica sfro/{session,camera,mount,focuser,storage,power,agent};
    `power` = volt/ampere per uscita (get_power_supply).
  - DISPONIBILITA': LWT su sfro/status (online/offline, retained).
  - DISCOVERY: pubblica le config Home Assistant MQTT Discovery (le entita'
    compaiono da sole).

NON invia MAI comandi all'ASIAIR (nessun set_*/start/stop): pura telemetria.
Riusa i mattoni di sfro_agent (AsiairControl, KasaCloud, nautical_window).
"""
import argparse
import json
import math
import socket
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import paho.mqtt.client as mqtt

import sfro_agent as A

HERE = Path(__file__).resolve().parent
log = A.log


# --------------------------------------------------------------------------- #
# Specifica sensori -> Home Assistant MQTT Discovery
# (component, object_id, name, topic_suffix, json_key, unit, device_class)
# component 'binary' usa payload bool del json_key.
# --------------------------------------------------------------------------- #
SENSORS = [
    # guide (15s) — tutto in ARCSEC (come l'app ASIAIR)
    ("sensor", "guide_rms_total", "Guide RMS Totale", "guide", "rms_total", '"', None),
    ("sensor", "guide_rms_ra", "Guide RMS RA", "guide", "rms_ra", '"', None),
    ("sensor", "guide_rms_dec", "Guide RMS Dec", "guide", "rms_dec", '"', None),
    ("sensor", "guide_peak_ra", "Guide Picco RA", "guide", "peak_ra", '"', None),
    ("sensor", "guide_peak_dec", "Guide Picco Dec", "guide", "peak_dec", '"', None),
    ("binary", "guiding", "Guida attiva", "guide", "guiding", None, "running"),
    ("binary", "star_lost", "Stella persa", "guide", "star_lost", None, "problem"),
    ("binary", "dithering", "Dither in corso", "guide", "dithering", None, "running"),
    # session (10m)
    ("sensor", "target", "Soggetto", "session", "target", None, None),
    ("sensor", "filter", "Filtro attivo", "session", "filter", None, None),
    ("sensor", "seq_type", "Tipo sequenza", "session", "seq_type", None, None),
    ("sensor", "sequence", "Sequenza", "session", "sequence", None, None),
    ("sensor", "frame_done", "Frame ripresi", "session", "frame_done", None, None),
    ("sensor", "frame_total", "Frame totali", "session", "frame_total", None, None),
    ("sensor", "progress", "Avanzamento", "session", "progress_pct", "%", None),
    ("sensor", "exposure", "Esposizione", "session", "exp_s", "s", "duration"),
    ("sensor", "gain", "Gain", "session", "gain", None, None),
    ("sensor", "time_left", "Tempo rimanente", "session", "left_time_min", "min", "duration"),
    ("sensor", "eta", "Fine stimata", "session", "eta", None, "timestamp"),
    ("sensor", "exp_state", "Stato esposizione", "session", "exp_state", None, None),
    ("binary", "plan_started", "Piano in esecuzione", "session", "plan_started", None, "running"),
    ("binary", "merid_flip", "Flip al meridiano", "session", "merid_flip", None, "running"),
    # camera (10m)
    ("sensor", "cam_temp", "Temperatura sensore", "camera", "sensor_temp_c", "°C", "temperature"),
    ("sensor", "cam_target_temp", "Temperatura target", "camera", "target_temp_c", "°C", "temperature"),
    ("sensor", "cooler_pct", "Potenza raffreddamento", "camera", "cooler_pct", "%", None),
    ("binary", "cooler_on", "Raffreddamento ON", "camera", "cooler_on", None, "running"),
    # mount (10m)
    ("binary", "tracking", "Tracking", "mount", "tracking", None, "running"),
    ("sensor", "pier_side", "Lato pila", "mount", "pier_side", None, None),
    ("sensor", "ra", "AR", "mount", "ra_h", "h", None),
    ("sensor", "dec", "Dec", "mount", "dec_deg", "°", None),
    ("sensor", "alt", "Altezza", "mount", "alt_deg", "°", None),
    ("sensor", "az", "Azimut", "mount", "az_deg", "°", None),
    ("sensor", "voltage", "Tensione alimentazione", "mount", "input_voltage_v", "V", "voltage"),
    # focuser (10m)
    ("sensor", "eaf_pos", "Posizione EAF", "focuser", "position", "step", None),
    ("sensor", "eaf_temp", "Temperatura EAF", "focuser", "temp_c", "°C", "temperature"),
    ("sensor", "hfr", "HFR ultimo autofocus", "focuser", "hfr", None, None),
    # storage (10m)
    ("sensor", "disk_free", "Spazio libero", "storage", "free_gb", "GB", "data_size"),
    ("sensor", "disk_free_pct", "Spazio libero %", "storage", "free_pct", "%", None),
    # pi (eventi push PiStatus, ~ogni 10s: salute del Raspberry dell'ASIAIR)
    ("sensor", "pi_temp", "Temperatura CPU", "pi", "temp_c", "°C", "temperature"),
    ("binary", "undervolt", "Sottotensione", "pi", "undervolt", None, "problem"),
    ("binary", "overcurrent", "Sovracorrente", "pi", "overcurrent", None, "problem"),
    ("binary", "overtemp", "CPU in sovratemperatura", "pi", "overtemp", None, "problem"),
    # power (10m, get_power_supply: volt/ampere per uscita)
    ("sensor", "power_total", "Potenza totale", "power", "total_w", "W", "power"),
    ("sensor", "power_input_v", "Tensione uscite", "power", "input_v", "V", "voltage"),
    ("sensor", "power_dew_a", "Corrente fascia anticondensa", "power", "dew_heater_a", "A", "current"),
    ("sensor", "power_cam_a", "Corrente camera", "power", "camera_a", "A", "current"),
    # agent (5m, qui derivati: notte nautica + ripresa + asiair online)
    ("binary", "in_night", "Notte nautica", "agent", "in_nautical_night", None, "running"),
    ("binary", "imaging", "Ripresa in corso", "agent", "imaging_active", None, "running"),
    ("binary", "asiair_online", "ASIAIR online", "agent", "asiair_online", None, "connectivity"),
]


class Publisher:
    def __init__(self, cfg, dry=False):
        self.cfg = cfg
        self.dry = dry
        m = cfg.get("mqtt", {})
        self.host = m.get("host", "192.0.2.20")
        self.port = int(m.get("port", 1883))
        self.base = m.get("base_topic", "sfro").rstrip("/")
        self.disc_prefix = m.get("discovery_prefix", "homeassistant")
        self.discovery = bool(m.get("discovery", True))
        self.guide_interval = float(m.get("guide_interval_seconds", 15))
        self.guide_window = float(m.get("guide_window_seconds", 120))
        self.slow_interval = float(m.get("slow_interval_seconds", 600))
        self.device_name = m.get("device_name", "SFRO ASIAIR")
        # I campi RADistanceRaw/DECDistanceRaw degli eventi GuideStep sono GIA' in
        # ARCSEC (come li mostra l'app ASIAIR: dx/dy sono in pixel, le *DistanceRaw no).
        # guide_arcsec_scale = solo calibrazione fine (default 1.0).
        self.arcsec_scale = float(m.get("guide_arcsec_scale", 1.0))
        creds = A.load_kv_file(HERE / m.get("creds_file", "mqtt.txt"))
        self.user = creds.get("username", "")
        self.passwd = creds.get("password", "")
        self.acfg = cfg.get("asiair", {})
        self.host_asiair = self.acfg.get("host", "")
        self.imager_port = int(self.acfg.get("port", 4700))
        self.tz = ZoneInfo(cfg.get("timezone_observatory", "UTC"))
        self.steps = deque()          # (mono_ts, ra_raw, dec_raw)
        self.last_step_wall = 0.0
        self._stop = threading.Event()
        self.client = None
        # Sink guida su file (JSONL per data UTC): usato da sfro_sessionlog per
        # correlare l'RMS alla finestra di esposizione di ogni FITS.
        self.sink_dir = (cfg.get("session_log", {}) or {}).get(
            "guide_sink_dir", "/var/lib/sfro-agent/guide")
        self._sink_fh = None
        self._sink_day = None
        self.focus_sink_dir = (cfg.get("session_log", {}) or {}).get(
            "focus_sink_dir", "")
        # riconnessione forzata del listener guida se il socket resta muto
        # (vedi guide_listener: ASIAIR spento senza FIN/RST = zombie)
        self.guide_stale = float(m.get("guide_stale_seconds", 600))
        # Eventi push della 4700 (vedi event_listener): l'ASIAIR li manda da
        # solo, senza che gli si chieda nulla. ev_fresh = entro quanto un
        # evento e' ancora buono per pubblicare (oltre = box spento o muto).
        self.ev = {}
        self.ev_ts = 0.0
        self.ev_fresh = float(m.get("event_fresh_seconds", 120))
        self.ev_stale = float(m.get("event_stale_seconds", 300))
        # Battito sulla 4700 come fa l'app ufficiale (test_connection ogni 5s,
        # comportamento osservato in cattura): tiene viva la connessione del
        # listener, che altrimenti resterebbe muta. 0 = disattivato.
        self.hb_interval = float(m.get("heartbeat_seconds", 5))
        self.last_session = {}

    def _sink_write(self, ra, dec, bad):
        """Append di un GuideStep sul JSONL del giorno (UTC). Best-effort."""
        try:
            day = time.strftime("%Y-%m-%d", time.gmtime())
            if day != self._sink_day:
                if self._sink_fh:
                    self._sink_fh.close()
                d = Path(self.sink_dir)
                d.mkdir(parents=True, exist_ok=True)
                self._sink_fh = open(d / f"{day}.jsonl", "a", buffering=1)
                self._sink_day = day
                for old in sorted(d.glob("*.jsonl"))[:-30]:   # tieni 30 giorni
                    old.unlink(missing_ok=True)
            self._sink_fh.write(json.dumps(
                {"t": round(time.time(), 2), "ra": round(ra, 3),
                 "dec": round(dec, 3), "b": 1 if bad else 0}) + "\n")
        except Exception as e:
            log.debug("guide sink: %s", e)

    # ---------------- MQTT ----------------
    def _topic(self, suffix):
        return f"{self.base}/{suffix}"

    def connect_mqtt(self):
        cid = f"sfro-mqtt-{int(time.time())}"
        try:   # paho-mqtt 2.x richiede la versione dell'API callback
            self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                      client_id=cid, protocol=mqtt.MQTTv311)
        except (AttributeError, TypeError):   # paho-mqtt 1.x
            self.client = mqtt.Client(client_id=cid, protocol=mqtt.MQTTv311)
        if self.user:
            self.client.username_pw_set(self.user, self.passwd)
        self.client.will_set(self._topic("status"), "offline", qos=1, retain=True)
        self.client.connect(self.host, self.port, keepalive=60)
        self.client.loop_start()
        self.publish("status", "online", retain=True, raw=True)
        if self.discovery:
            self.publish_discovery()

    def publish(self, suffix, payload, retain=False, raw=False):
        topic = self._topic(suffix)
        data = payload if raw else json.dumps(payload, ensure_ascii=False)
        if self.dry:
            print(f"[PUB] {topic} {data}")
            return
        self.client.publish(topic, data, qos=0, retain=retain)

    def publish_discovery(self):
        dev = {"identifiers": ["sfro_asiair"], "name": self.device_name,
               "manufacturer": "ZWO/SFRO", "model": "ASIAIR Pro"}
        for comp, obj, name, suffix, key, unit, dc in SENSORS:
            comp_ha = "binary_sensor" if comp == "binary" else "sensor"
            cfg = {
                "name": name,
                "unique_id": f"sfro_{obj}",
                "state_topic": self._topic(suffix),
                "availability_topic": self._topic("status"),
                "device": dev,
            }
            if comp == "binary":
                cfg["value_template"] = ("{{ 'ON' if value_json.%s else 'OFF' }}" % key)
                cfg["payload_on"] = "ON"
                cfg["payload_off"] = "OFF"
            else:
                cfg["value_template"] = "{{ value_json.%s }}" % key
                if unit:
                    cfg["unit_of_measurement"] = unit
            if dc:
                cfg["device_class"] = dc
            dtopic = f"{self.disc_prefix}/{comp_ha}/sfro_{obj}/config"
            if self.dry:
                print(f"[DISC] {dtopic}")
            else:
                self.client.publish(dtopic, json.dumps(cfg, ensure_ascii=False),
                                    qos=1, retain=True)

    # ---------------- GUIDA: listener eventi ----------------
    def guide_listener(self):
        # A rig spento (tutto il giorno, dopo il teardown) la connessione
        # fallisce ogni 13s: senza backoff sono migliaia di righe di journal al
        # giorno. Stesso schema gia' in uso sull'agente di casa (2026-08-12).
        backoff = 5
        while not self._stop.is_set():
            s = None
            try:
                s = socket.create_connection((self.host_asiair, 4400), 8)
                # L'ASIAIR spento non manda FIN/RST: senza keepalive la recv
                # resta in timeout silenzioso PER SEMPRE e al riavvio del box
                # non ci si riconnette piu' (visto 2026-07-04/07: tre notti
                # senza telemetria guida). Il keepalive kernel fa fallire la
                # recv entro ~60s dal blackout del peer.
                s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 30)
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
                s.settimeout(5)
                if backoff > 5:
                    log.info("guide listener: ASIAIR di nuovo raggiungibile")
                backoff = 5
                buf = b""
                last_rx = time.monotonic()
                while not self._stop.is_set():
                    try:
                        chunk = s.recv(4096)
                    except socket.timeout:
                        # cintura+bretelle oltre al keepalive: socket muto
                        # troppo a lungo -> riconnessione (innocua se il box
                        # e' su ma la guida e' semplicemente ferma)
                        if time.monotonic() - last_rx > self.guide_stale:
                            log.info("guide listener: muto da %.0fs, riconnetto",
                                     self.guide_stale)
                            break
                        continue
                    if not chunk:
                        break
                    last_rx = time.monotonic()
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        try:
                            msg = json.loads(line.decode("utf-8", "ignore").strip())
                        except Exception:
                            continue
                        if msg.get("Event") == "GuideStep":
                            # IsSettle/IsDither: frame di assestamento dopo un dither;
                            # vanno ESCLUSI dall'RMS (come fa ASIAIR/PHD2), altrimenti
                            # lo spike del dither gonfia la media.
                            bad = bool(msg.get("IsSettle") or msg.get("IsDither"))
                            ra_a = float(msg.get("RADistanceRaw", 0) or 0)
                            dec_a = float(msg.get("DECDistanceRaw", 0) or 0)
                            self.steps.append((time.monotonic(), ra_a, dec_a, bad))
                            self.last_step_wall = time.time()
                            self._sink_write(ra_a * self.arcsec_scale,
                                             dec_a * self.arcsec_scale, bad)
            except Exception as e:
                log.log(logging_level_for(backoff), "guide listener: %s", e)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 60)
            finally:
                if s is not None:
                    try:
                        s.close()
                    except OSError:
                        pass

    def _guide_payload(self):
        cutoff = time.monotonic() - self.guide_window
        while self.steps and self.steps[0][0] < cutoff:
            self.steps.popleft()
        # RMS solo sui frame di guida REALE (escludi dither/settle, come ASIAIR)
        ra = [r for _, r, _, bad in self.steps if not bad]
        dec = [d for _, _, d, bad in self.steps if not bad]
        dithering = bool(self.steps and self.steps[-1][3])
        guiding = (time.time() - self.last_step_wall) < 30 and len(self.steps) > 0

        def rms(v):
            # RMS sulla MEDIA della finestra (dev. standard), come ASIAIR/PHD2:
            # l'RMS su zero gonfia il valore quando c'e' deriva nella finestra
            if not v:
                return 0.0
            m = sum(v) / len(v)
            return math.sqrt(sum((x - m) ** 2 for x in v) / len(v))
        sc = self.arcsec_scale
        rr, rd = rms(ra) * sc, rms(dec) * sc
        tot = math.sqrt(rr * rr + rd * rd)          # tutto in ARCSEC
        return {
            "rms_total": round(tot, 2), "rms_ra": round(rr, 2), "rms_dec": round(rd, 2),
            "peak_ra": round(max((abs(x) for x in ra), default=0) * sc, 2),
            "peak_dec": round(max((abs(x) for x in dec), default=0) * sc, 2),
            "last_ra": round(ra[-1] * sc, 2) if ra else 0,
            "last_dec": round(dec[-1] * sc, 2) if dec else 0,
            "guiding": guiding, "star_lost": (not guiding), "dithering": dithering,
            "n_samples": len(ra), "ts": datetime.now(self.tz).isoformat(timespec="seconds"),
        }

    # ---------------- EVENTI PUSH 4700 (tempo reale, zero richieste) ----------------
    def event_listener(self):
        """L'ASIAIR SPINGE eventi sulla 4700 senza che gli si chieda nulla
        (verificato live 2026-08-12 sul fw 13.41 aprendo la socket e basta):
        PiStatus (temp/undervolt/overcurrent), Sequence (avanzamento piano),
        Exposure, SaveImage, Version. Qui si legge e si aggiorna self.ev; a
        pubblicare ci pensa il loop principale.
        L'unica cosa che si INVIA e' il battito `test_connection` (sola lettura,
        nessun effetto sul box) ogni `heartbeat_seconds`, come fa l'app
        ufficiale: tiene viva la connessione invece di lasciarla muta."""
        backoff = 5          # come il listener guida: rig spento = niente rumore
        while not self._stop.is_set():
            s = None
            try:
                s = socket.create_connection((self.host_asiair, self.imager_port), 8)
                # stesso keepalive del listener guida: l'ASIAIR spento non manda
                # FIN/RST e senza questo la recv resta muta per sempre
                s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 30)
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
                s.settimeout(min(5, self.hb_interval) if self.hb_interval else 5)
                if backoff > 5:
                    log.info("event listener: ASIAIR di nuovo raggiungibile")
                backoff = 5
                buf = b""
                last_rx = last_hb = time.monotonic()
                hb_id = 0
                while not self._stop.is_set():
                    if self.hb_interval and time.monotonic() - last_hb >= self.hb_interval:
                        hb_id += 1
                        s.sendall((json.dumps(
                            {"id": hb_id, "method": "test_connection", "params": []},
                            separators=(",", ":")) + "\r\n").encode())
                        last_hb = time.monotonic()
                    try:
                        chunk = s.recv(4096)
                    except socket.timeout:
                        # PiStatus arriva di continuo: silenzio lungo = socket
                        # zombie, meglio riconnettere
                        if time.monotonic() - last_rx > self.ev_stale:
                            log.info("event listener: muto da %.0fs, riconnetto",
                                     self.ev_stale)
                            break
                        continue
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        try:
                            msg = json.loads(line.decode("utf-8", "ignore").strip())
                        except Exception:
                            continue
                        # last_rx conta solo gli EVENTI veri: le risposte al
                        # nostro battito non devono mascherare una socket zombie
                        if msg.get("Event"):
                            last_rx = time.monotonic()
                        self._apply_event(msg)
            except Exception as e:
                log.log(logging_level_for(backoff), "event listener: %s", e)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 60)
            finally:
                if s is not None:
                    try:
                        s.close()
                    except OSError:
                        pass

    def _apply_event(self, msg):
        """Aggiorna lo stato dagli eventi push. Le risposte alle richieste
        (hanno 'id') qui non arrivano: non chiediamo niente."""
        ev = msg.get("Event")
        if not ev:
            return
        self.ev_ts = time.time()
        if ev == "PiStatus":
            self.ev["pi"] = {
                "temp_c": round(float(msg.get("temp") or 0), 1),
                "undervolt": bool(msg.get("is_undervolt")),
                "overcurrent": bool(msg.get("is_over_current")),
                "overtemp": bool(msg.get("is_overtemp")),
            }
        elif ev == "Sequence":
            # stessa forma del 'progress' di get_app_state: la rilegge
            # _session_live. Si tiene solo se porta davvero i contatori: un
            # Sequence senza cur_plan non e' una notizia e non deve cancellare
            # l'avanzamento gia' noto (test_mqtt_events B2).
            p = msg.get("progress") or {}
            if (p.get("cur_plan") or {}).get("total"):
                self.ev["progress"] = p
        elif ev == "Exposure":
            self.ev["exp_state"] = msg.get("state")
        elif ev == "SaveImage" and msg.get("state") == "complete":
            self.ev["last_file"] = msg.get("filename")
        elif ev == "AutoFocus" and msg.get("state") == "complete":
            # HFR della corsa appena conclusa: last_point e' [posizione, HFR]
            # (verificato in cattura); se manca, si ripiega sul minimo della
            # curva a V. Serve SOLO a scrivere il sink del diario: nessun
            # sensore, gli eventi di autofocus non ci interessano in HA.
            res = msg.get("result") or {}
            lp = res.get("last_point")
            hfr = pos = None
            if isinstance(lp, (list, tuple)) and len(lp) >= 2:
                pos, hfr = lp[0], lp[1]
            else:
                pts = [p for p in (res.get("points") or [])
                       if isinstance(p, (list, tuple)) and len(p) >= 2]
                if pts:
                    pos, hfr = min(pts, key=lambda p: p[1])
            if hfr is not None:
                self._focus_sink_write(hfr, pos)
        elif ev == "Version":
            self.ev["firmware"] = msg.get("firmware_ver_string")

    def _focus_sink_write(self, hfr, pos):
        """Una riga JSONL per ogni autofocus CONCLUSO, nel file del giorno UTC.
        Alimenta le colonne "HFR migliore"/"N autofocus" della tab Sessioni:
        il sessionlog raggruppa per notte con la stessa regola dei frame.
        Best-effort: se non si riesce a scrivere, pazienza."""
        if not self.focus_sink_dir:
            return
        try:
            d = time.strftime("%Y-%m-%d", time.gmtime())   # come il sink guida
            p = Path(self.focus_sink_dir)
            p.mkdir(parents=True, exist_ok=True)
            riga = {"t": round(time.time(), 3), "hfr": round(float(hfr), 3)}
            if pos is not None:
                riga["pos"] = int(pos)
            with open(p / f"{d}.jsonl", "a") as fh:
                fh.write(json.dumps(riga) + "\n")
            log.info("autofocus concluso: HFR %.2f (pos %s)", float(hfr), pos)
        except Exception as e:
            log.warning("sink autofocus: %s", e)

    def _pi_payload(self):
        """Salute del Pi dagli eventi PiStatus. None se gli eventi non arrivano
        (ASIAIR spento): meglio non pubblicare che pubblicare valori vecchi."""
        pi = self.ev.get("pi")
        if not pi or (time.time() - self.ev_ts) > self.ev_fresh:
            return None
        return dict(pi, firmware=self.ev.get("firmware"),
                    ts=datetime.now(self.tz).isoformat(timespec="seconds"))

    def _session_live(self):
        """Ultimo payload 'session' del poll lento, sovrascritto con i contatori
        che arrivano dagli eventi: l'avanzamento si muove in tempo reale invece
        di aspettare il poll successivo. Senza eventi freschi resta identico a
        quello del poll (nessuna regressione)."""
        if not self.last_session or (time.time() - self.ev_ts) > self.ev_fresh:
            # box spento o muto: il topic resta quello del poll lento, come prima
            return None
        out = dict(self.last_session)
        p = self.ev.get("progress") or {}
        cur = p.get("cur_plan") or {}
        done, total = cur.get("lapse"), cur.get("total")
        if done is not None:
            out["frame_done"] = done
        if total:
            out["frame_total"] = total
        if done is not None and total:
            out["progress_pct"] = round(100 * done / total)
        name = (p.get("cur_target") or {}).get("target_name")
        if name:
            out["target"] = name
        ftype = (p.get("cur_seq") or {}).get("frame_type")
        if ftype:
            out["seq_type"] = ftype
        if self.ev.get("exp_state"):
            out["exp_state"] = self.ev["exp_state"]
        if self.ev.get("last_file"):
            out["last_file"] = self.ev["last_file"]
        return out

    @staticmethod
    def _focus_quality(aps):
        """HFR migliore dell'ULTIMO autofocus, da get_app_state (gia' scaricato:
        zero chiamate in piu'). NB verificato live 2026-08-12: `last_point` e'
        transitorio — a rig fermo e' None — mentre la curva a V (`points`, coppie
        [posizione, HFR]) RESTA fino all'autofocus successivo. Quindi si prende
        il minimo della curva, che e' il fuoco raggiunto.
        Se l'ultima corsa e' fallita o e' stata interrotta (es. code 253
        'aborted') l'HFR NON si pubblica: meglio nessun dato che un dato di una
        corsa non conclusa."""
        af = (aps.get("auto_focus") or {}).get("result") or {}
        err = af.get("error") or ""
        pts = [p for p in (af.get("points") or [])
               if isinstance(p, (list, tuple)) and len(p) >= 2]
        if err or not pts:
            return {"hfr": None, "hfr_pos": None, "af_error": err or None}
        best = min(pts, key=lambda p: p[1])
        return {"hfr": round(float(best[1]), 2), "hfr_pos": int(best[0]),
                "af_error": None}

    @staticmethod
    def _power_payload(rails, outs):
        """get_power_supply → [[V, A], …], una coppia per uscita nell'ORDINE di
        pi_output_get2 (correlazione verificata sul flat panel spento: stessa
        posizione a 0V/0A), piu' una coppia finale in piu' = ingresso."""
        p, tot = {}, 0.0
        for i, pair in enumerate(rails):
            try:
                v, a = float(pair[0]), float(pair[1])
            except (TypeError, ValueError, IndexError):
                continue
            if i < len(outs) and isinstance(outs[i], dict) and outs[i].get("type"):
                name = outs[i]["type"]
            elif i == len(outs):
                name = "input"
            else:
                name = f"rail{i}"
            p[f"{name}_v"] = round(v, 2)
            p[f"{name}_a"] = round(a, 3)
            p[f"{name}_w"] = round(v * a, 1)
            tot += v * a
        p["total_w"] = round(tot, 1)
        volts = [v for k, v in p.items() if k.endswith("_v") and v > 1]
        p["input_v"] = round(max(volts), 2) if volts else None
        return p

    # ---------------- TELEMETRIA LENTA (poll read-only) ----------------
    def slow_payloads(self):
        """Interroga l'ASIAIR (get_*) e ritorna {suffix: payload}. Sola lettura."""
        out = {}
        host = self.host_asiair
        online = A.ping(host) if host else False
        # notte nautica
        dep = self.cfg.get("nautical", {}).get("depression", 12)
        in_night, _, _, _ = A.nautical_window(datetime.now(self.tz),
                                              self.cfg["location"], self.tz, dep)
        capturing = False
        try:
            with A.AsiairClient(host, 4700, timeout=8) as c:
                def gv(name):
                    r, _ = c.call("get_control_value", [name, True], max_wait=6)
                    v = r.get("result")
                    return v.get("value") if isinstance(v, dict) else None
                aps = c.call("get_app_state", [], max_wait=6)[0].get("result", {})
                cap = aps.get("capture", {})
                prog = cap.get("progress", {})
                capturing = bool(cap.get("is_working"))
                ep = c.call("get_enabled_plan", [], max_wait=6)[0].get("result", [])
                plan = ep[0] if ep else {}
                tgt = (plan.get("targets") or [{}])[0]
                seq = (tgt.get("seqs") or [{}])[0]
                wp = c.call("get_wheel_position", [], max_wait=6)[0].get("result")
                names = c.call("get_wheel_setting", [], max_wait=6)[0].get("result", {}).get("names", [])
                filt = names[wp] if isinstance(wp, int) and wp < len(names) else None
                left = plan.get("left_time_sec")
                eta = (datetime.now(self.tz) + timedelta(seconds=left)).isoformat(timespec="seconds") if left else None
                cur = prog.get("cur_plan", {})
                done, total = cur.get("lapse"), cur.get("total")
                seq_desc = (f"{seq.get('type')} {filt or ''} {seq.get('exp')}s x{seq.get('repeat')}"
                            .replace("  ", " ").strip() if seq else None)
                out["session"] = {
                    "target": prog.get("cur_target", {}).get("target_name"),
                    "filter": filt, "seq_type": seq.get("type"), "sequence": seq_desc,
                    "frame_done": done, "frame_total": total,
                    "progress_pct": round(100 * done / total) if (done is not None and total) else None,
                    "exp_s": seq.get("exp"), "gain": seq.get("gain"), "bin": seq.get("bin"),
                    "left_time_min": round(left / 60) if left else None, "eta": eta,
                    "plan_name": plan.get("plan_name"), "plan_started": bool(plan.get("is_plan_started")),
                    "capturing": capturing,
                }
                out["camera"] = {
                    "sensor_temp_c": gv("Temperature"), "target_temp_c": gv("TargetTemp"),
                    "cooler_pct": gv("CoolPowerPerc"), "cooler_on": bool(gv("CoolerOn")),
                    "gain": gv("Gain"),
                }
                fi = c.call("get_focuser_info", [], max_wait=6)[0].get("result", {})
                out["focuser"] = {"position": fi.get("position"),
                                  "temp_c": fi.get("temperature"),
                                  **self._focus_quality(aps)}
                # flip al meridiano in corso: campo gia' presente in get_app_state
                out["session"]["merid_flip"] = bool(
                    (aps.get("merid_flip") or {}).get("is_working"))
                dv = c.call("get_disk_volume", [], max_wait=6)[0].get("result", {})
                free, tot_mb = dv.get("freeMB"), dv.get("totalMB")
                out["storage"] = {
                    "free_gb": round(free / 1024, 1) if free else None,
                    "total_gb": round(tot_mb / 1024, 1) if tot_mb else None,
                    "free_pct": round(100 * free / tot_mb) if (free and tot_mb) else None,
                }
                # Volt/ampere per uscita: dice se un'uscita STA ASSORBENDO, cosa
                # che pi_output_get2 (che riporta solo lo stato impostato) non
                # distingue — es. fascia anticondensa accesa ma scollegata.
                rails = c.call("get_power_supply", [], max_wait=6)[0].get("result") or []
                outs = c.call("pi_output_get2", [], max_wait=6)[0].get("result") or []
                out["power"] = self._power_payload(rails, outs)
        except Exception as e:
            log.info("poll imager: %s", e)
        try:
            with A.AsiairClient(host, 4400, timeout=8) as g:
                si = g.call("scope_get_info", [], max_wait=6)[0].get("result", {})
                out["mount"] = {
                    "tracking": bool(si.get("is_enable_track")), "pier_side": si.get("pier_side"),
                    "ra_h": round(si.get("RA"), 4) if si.get("RA") is not None else None,
                    "dec_deg": round(si.get("Dec"), 4) if si.get("Dec") is not None else None,
                    "alt_deg": round(si.get("Alt"), 2) if si.get("Alt") is not None else None,
                    "az_deg": round(si.get("Az"), 2) if si.get("Az") is not None else None,
                    "input_voltage_v": round(si.get("input_voltage", 0) / 1000, 1) if si.get("input_voltage") else None,
                    "guide_rate": si.get("guide_rate"),
                }
        except Exception as e:
            log.info("poll mount: %s", e)
        out["agent"] = {"in_nautical_night": in_night, "imaging_active": capturing,
                        "asiair_online": online}
        return out

    # ---------------- LOOP ----------------
    def run(self):
        self.connect_mqtt()
        if self.host_asiair:
            threading.Thread(target=self.guide_listener, daemon=True).start()
            threading.Thread(target=self.event_listener, daemon=True).start()
        next_slow = 0.0
        try:
            while not self._stop.is_set():
                now = time.monotonic()
                self.publish("guide", self._guide_payload())
                if now >= next_slow:
                    for suffix, payload in self.slow_payloads().items():
                        self.publish(suffix, payload, retain=True)
                        if suffix == "session":
                            self.last_session = payload
                    next_slow = now + self.slow_interval
                # dagli eventi push: salute del Pi e avanzamento in tempo reale
                # (a ogni giro, non al ritmo del poll lento)
                pi = self._pi_payload()
                if pi:
                    self.publish("pi", pi, retain=True)
                live = self._session_live()
                if live:
                    self.publish("session", live, retain=True)
                self._stop.wait(self.guide_interval)
        finally:
            self.publish("status", "offline", retain=True, raw=True)
            if self.client and not self.dry:
                self.client.loop_stop()
                self.client.disconnect()

    def stop(self):
        self._stop.set()
        if self._sink_fh:
            try:
                self._sink_fh.close()
            except OSError:
                pass


def logging_level_for(backoff):
    """Primo fallimento a INFO, i successivi (backoff cresciuto) a DEBUG:
    evita di riempire il journal quando l'ASIAIR resta spento per ore."""
    import logging as _l
    return _l.INFO if backoff <= 5 else _l.DEBUG


def main():
    ap = argparse.ArgumentParser(description="Publisher MQTT telemetria SFRO (read-only)")
    ap.add_argument("--config", default=str(HERE / "config.yaml"))
    ap.add_argument("--selftest", action="store_true",
                    help="stampa discovery + un payload guida finto e i payload lenti, ed esce")
    args = ap.parse_args()
    cfg = A.load_config(Path(args.config))
    A.setup_logging(cfg.get("log_level", "INFO"))
    pub = Publisher(cfg, dry=args.selftest)
    if args.selftest:
        pub.publish_discovery()
        # payload guida finto
        pub.steps.extend([(time.monotonic(), 0.5, -0.4, False), (time.monotonic(), -0.3, 0.2, False)])
        pub.last_step_wall = time.time()
        print("[PUB] sfro/guide", json.dumps(pub._guide_payload(), ensure_ascii=False))
        # eventi push finti: stessa forma catturata dal vivo sul box (fw 13.41)
        for ev in (
            {"Event": "Version", "firmware_ver_string": "13.41"},
            {"Event": "PiStatus", "temp": 41.3, "is_undervolt": False,
             "is_over_current": False, "is_overtemp": False},
            {"Event": "Exposure", "state": "downloading", "page": "plan"},
            {"Event": "SaveImage", "state": "complete", "filename": "Light_300s_0061.fit"},
            {"Event": "Sequence", "state": "frame_complete", "progress": {
                "cur_plan": {"total": 98, "lapse": 61},
                "cur_target": {"target_name": "Rotten Fish Nebula"},
                "cur_seq": {"frame_type": "light"}}},
        ):
            pub._apply_event(ev)
        print("[PUB] sfro/pi", json.dumps(pub._pi_payload(), ensure_ascii=False))
        pub.last_session = {"target": "vecchio", "frame_done": 0, "frame_total": 98,
                            "progress_pct": 0, "seq_type": "light", "exp_s": 300}
        print("[PUB] sfro/session", json.dumps(pub._session_live(), ensure_ascii=False))
        print("[PUB] sfro/power", json.dumps(pub._power_payload(
            [[12.096, 0.70355], [12.201, 0.0302], [0.021, 0.0],
             [12.2325, 0.145647], [12.2535, 1.8786]],
            [{"type": "camera"}, {"type": "other"}, {"type": "flat_panel"},
             {"type": "dew_heater"}]), ensure_ascii=False))
        print("# (slow_payloads richiede l'ASIAIR: saltato in selftest)")
        return
    if not cfg.get("mqtt", {}).get("enabled", False):
        log.warning("mqtt.enabled = false: esco.")
        return
    try:
        pub.run()
    except KeyboardInterrupt:
        pub.stop()


if __name__ == "__main__":
    main()
