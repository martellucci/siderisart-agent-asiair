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

Config: file asiair.txt nella stessa cartella (host/port). CLI sovrascrive.
Dipendenze: solo stdlib.
"""

import argparse
import json
import os
import socket
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


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
    def __init__(self, ip: str, port: int = 4400, timeout: float = 8.0):
        self.ip, self.port, self.timeout = ip, port, timeout
        self.sock: socket.socket | None = None
        self._buf = b""
        self._id = 0

    def connect(self):
        self.sock = socket.create_connection((self.ip, self.port), self.timeout)
        self.sock.settimeout(self.timeout)

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
