#!/usr/bin/env python3
"""
asiair_client.py
----------------
Client minimale per il protocollo di controllo ASIAIR, con due scopi:
  1) testare la connettivita' e SCOPRIRE i comandi reali (in sicurezza);
  2) inviare un comando confermato (es. avvio autorun) solo con conferma esplicita.

VERIFICATO (reverse engineering pubblico):
  - Controllo su TCP porta 4400; messaggi JSON "a riga" terminati da \\r\\n in forma
    {"id": <int>, "method": "<nome>", "params": [...]}.
  - Il canale di controllo 4400 NON richiede autenticazione (basta essere in rete).
    Le credenziali ASIAIR esistenti sono quelle SMB della share immagini.
  - Le risposte sono JSON a riga; arrivano anche EVENTI asincroni (senza il nostro id).

NON VERIFICATO (NON inventato qui):
  - I NOMI dei metodi (avvio autorun/plan ecc.) e il formato del campo esito.
    Vanno SCOPERTI sul firmware reale: vedi ASIAIR_TEST.md (cattura traffico app).

HANDSHAKE DELLA 4700 (2026-08-16, punto 0.4 di MIGRAZIONE_V3.md):
  Dal firmware ~43.97 — quello che l'app v3 impone — il canale 4700 e' CHIUSO:
  finche' il client non si autentica il box risponde SOLO a `test_connection` e
  `get_verify_str` e tutto il resto lo ignora IN SILENZIO, senza errore. Qui
  l'autenticazione sta in connect(), cioe' nell'unico punto da cui passano tutti
  i comandi di tutti i flussi (agente, bot, flat/dark, teardown, shutdown): si
  scrive una volta e ogni chiamante la eredita.
  Sul nostro 13.41 e' INERTE: `get_verify_str` risponde 103 e non si firma nulla.
  Vedi ASIAIR_FINDINGS.md, "AGGIORNAMENTI 2026-08-12".

Config: file asiair.txt nella stessa cartella (host/port). CLI sovrascrive.
Dipendenze: stdlib; `cryptography` serve SOLO a firmare, cioe' solo su firmware
v3 — se manca, sul 13.41 non se ne accorge nessuno.
"""

import argparse
import base64
import json
import os
import socket
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

# Chiave privata dell'app ASIAIR, estratta dall'APK v3 (MIGRAZIONE_V3.md 0.2).
# Non e' un segreto NOSTRO (sta dentro l'app di chiunque), ma resta a 600 e fuori
# dall'archivio codice dei backup come ogni file di chiave.
KEY_FILE = os.path.join(HERE, "asiair_key.pem")

# Esito dell'handshake gia' scoperto, per (host, porta), valido per il processo.
# Serve perche' l'agente apre una connessione FRESCA per ogni comando (_call1):
# senza memoria pagherebbe una sonda in piu' a ogni comando, sulla VPN, anche
# sul firmware vecchio dove non serve a niente. Si memorizza solo "legacy": su
# v3 lo sblocco e' per-connessione e la firma va rifatta ogni volta.
_VERIFY_MODE: dict = {}


def _sign_challenge(challenge: str, key_file: str = KEY_FILE) -> str:
    """Firma la sfida come fa l'app: RSA PKCS#1 v1.5 su SHA-1, in base64.
    Import locale: sul firmware vecchio non si arriva mai qui e `cryptography`
    non deve essere un requisito per far girare l'agente."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    with open(key_file, "rb") as fh:
        key = serialization.load_pem_private_key(fh.read(), password=None)
    return base64.b64encode(
        key.sign(challenge.encode(), padding.PKCS1v15(), hashes.SHA1())).decode()


def load_conf(path: str) -> dict:
    """Legge asiair.txt (key=value). Chiavi: host, port, smb_username, smb_password."""
    conf = {}
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                conf[k.strip()] = v.strip()
    return conf


class AsiairClient:
    def __init__(self, ip: str, port: int = 4400, timeout: float = 8.0,
                 key_file: str = KEY_FILE):
        self.ip, self.port, self.timeout = ip, port, timeout
        self.sock: socket.socket | None = None
        self._buf = b""
        self._id = 0
        self.key_file = key_file
        self.verified = False        # True solo se ABBIAMO davvero firmato

    def connect(self):
        self.sock = socket.create_connection((self.ip, self.port), self.timeout)
        self.sock.settimeout(self.timeout)
        self._verify()

    def _verify(self):
        """Autentica il canale se il firmware lo richiede. Inerte sul 13.41.

        La sonda e' `get_verify_str` e NON `pi_is_verified` (che il piano 0.4
        ipotizzava): prima dell'autenticazione la v3 risponde solo a
        test_connection e get_verify_str, quindi pi_is_verified degenererebbe
        proprio nel timeout che vogliamo evitare. get_verify_str invece risponde
        SEMPRE — con la sfida sul firmware nuovo, con 103 sul nostro (verificato
        live il 2026-08-12). E' la stessa regola "solo il 103 autorizza il
        fallback legacy" del flusso v3 di terzi visto sul forum SFRO.

        Un guasto della SONDA (box muto o lento) NON e' un errore: si prosegue
        come si e' sempre fatto e non si memorizza nulla, cosi' il tentativo si
        ripete alla prossima connessione. Se invece la sfida ARRIVA — e allora
        il box e' certamente v3 e senza firma ignorerebbe ogni comando — un
        fallimento diventa un errore ESPLICITO: mai un timeout muto."""
        if _VERIFY_MODE.get((self.ip, self.port)) == "legacy":
            return
        budget = min(self.timeout, 5.0)
        try:
            r, _ = self.call("get_verify_str", [], max_wait=budget)
        except (TimeoutError, ConnectionError, OSError, ValueError):
            return
        res = r.get("result")
        challenge = res.get("str") if isinstance(res, dict) else (
            res if isinstance(res, str) else None)
        if not challenge:                    # code 103: firmware senza handshake
            _VERIFY_MODE[(self.ip, self.port)] = "legacy"
            return
        _VERIFY_MODE[(self.ip, self.port)] = "v3"
        try:
            firma = _sign_challenge(challenge, self.key_file)
        except Exception as e:               # chiave assente/illeggibile, o niente cryptography
            raise ConnectionError(
                f"canale {self.port} non autenticabile: {e}") from e
        try:
            self.call("verify_client", [firma, challenge], max_wait=budget)
        except TimeoutError:
            pass          # la v3 non sempre risponde: la prova vera e' la riverifica
        try:
            pv, _ = self.call("pi_is_verified", [], max_wait=budget)
        except TimeoutError as e:
            raise ConnectionError(
                f"canale {self.port} NON autenticato: il box resta muto") from e
        if pv.get("result") not in (True, 1):
            raise ConnectionError(
                f"canale {self.port} NON autenticato: firma rifiutata")
        self.verified = True

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()

    def _send(self, obj: dict):
        assert self.sock is not None
        self.sock.sendall((json.dumps(obj, separators=(",", ":")) + "\r\n").encode())

    def _read_line(self) -> dict:
        assert self.sock is not None
        while b"\n" not in self._buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("connessione chiusa dall'ASIAIR")
            self._buf += chunk
        raw, self._buf = self._buf.split(b"\n", 1)
        return json.loads(raw.decode("utf-8").strip())

    def call(self, method: str, params=None, max_wait: float | None = None):
        """Invia {id,method,params}; ritorna (risposta_con_stesso_id, [eventi])."""
        if params is None:
            params = []
        self._id += 1
        my_id = self._id
        self._send({"id": my_id, "method": method, "params": params})
        events = []
        deadline = time.time() + (max_wait or self.timeout)
        while True:
            if time.time() > deadline:
                raise TimeoutError(f"nessuna risposta a '{method}' (id={my_id})")
            msg = self._read_line()
            if isinstance(msg, dict) and msg.get("id") == my_id:
                return msg, events
            events.append(msg)


def _p(obj):
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def main() -> int:
    ap = argparse.ArgumentParser(description="Client/probe protocollo ASIAIR (TCP 4400)")
    ap.add_argument("--conf", default=os.path.join(HERE, "asiair.txt"),
                    help="file di config (default: asiair.txt nella cartella dello script)")
    ap.add_argument("--ip", help="override host ASIAIR")
    ap.add_argument("--port", type=int, help="override porta (default 4400)")
    ap.add_argument("--timeout", type=float, default=8.0)

    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--probe", action="store_true",
                   help="connettivita': invia un solo metodo di prova (read-only)")
    g.add_argument("--method", help="invio generico di un metodo da te scoperto")
    g.add_argument("--start", action="store_true",
                   help="ATTUAZIONE: invia il metodo di avvio (richiede --start-method e --yes-move-mount)")
    ap.add_argument("--params", default="[]", help="parametri JSON (lista o oggetto)")
    ap.add_argument("--probe-method", default="test_connection",
                    help="metodo per --probe (NON verificato; un eventuale errore e' innocuo)")
    ap.add_argument("--start-method", default="",
                    help="nome del metodo di avvio, SCOPERTO e confermato sul tuo firmware")
    ap.add_argument("--yes-move-mount", action="store_true",
                    help="conferma esplicita: so che l'avvio puo' MUOVERE IL MOUNT")
    args = ap.parse_args()

    conf = load_conf(args.conf)
    ip = args.ip or conf.get("host")
    port = args.port or int(conf.get("port", 4400))
    if not ip:
        print(f"ERRORE: nessun host. Mettilo in {args.conf} (host=...) o usa --ip.",
              file=sys.stderr)
        return 2

    try:
        params = json.loads(args.params)
    except json.JSONDecodeError as e:
        print(f"--params non e' JSON valido: {e}", file=sys.stderr)
        return 2

    if args.start:
        if not args.start_method:
            print("ERRORE: --start richiede --start-method <NOME_CONFERMATO>.\n"
                  "Scopri prima il nome reale (vedi ASIAIR_TEST.md), poi passalo qui.",
                  file=sys.stderr)
            return 2
        if not args.yes_move_mount:
            print("STOP: l'avvio di un autorun puo' MUOVERE IL MOUNT.\n"
                  "Assicurati che il telescopio possa ruotare in sicurezza, poi\n"
                  "ripeti aggiungendo --yes-move-mount.", file=sys.stderr)
            return 2
        method = args.start_method
    elif args.probe:
        method = args.probe_method
    else:
        method = args.method

    print(f"# ASIAIR {ip}:{port}  metodo='{method}'  params={params}")
    try:
        with AsiairClient(ip, port, args.timeout) as c:
            resp, events = c.call(method, params)
            print("# risposta:")
            _p(resp)
            if events:
                print(f"# eventi asincroni ({len(events)}):")
                for ev in events:
                    _p(ev)
    except (OSError, TimeoutError, ConnectionError) as e:
        print(f"ERRORE comunicazione ASIAIR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
