#!/usr/bin/env python3
"""Raw JSON-RPC console for the ASIAIR firmware channel on TCP 4700.

This is the undocumented channel the ASIAIR phone app itself uses. Alpaca
(see alpaca.py) covers the camera properly; this one is how you reach the
things Alpaca doesn't expose — mount goto, plate solving, autorun, guiding.

Framing: newline-terminated JSON, request/response plus unsolicited events.
    -> {"id":1,"method":"test_connection","params":[]}\\n
    <- {"id":1,"jsonrpc":"2.0","Timestamp":"...","result":...}\\n
    <- {"Event":"...","Timestamp":"..."}            (async, unprompted)

Auth (firmware 7.18+): most methods are dropped in silence until the connection
completes an RSA challenge handshake. Pass --key (or Air(host, key=...)) to run
it: get_verify_str returns a challenge, we sign it RSA PKCS#1 v1.5 / SHA-1 and
call verify_client([signature, challenge]); pi_is_verified then reads True and
the gated methods answer. Signing needs the `cryptography` package (imported
lazily, so the unauthenticated paths stay stdlib-only).

Usage:
    python3 air_rpc.py --host <air-ip> probe                 # API surface
    python3 air_rpc.py --host <air-ip> --key embedded_key.pem probe
    python3 air_rpc.py --host <air-ip> --key embedded_key.pem call get_device_state
    python3 air_rpc.py --host <air-ip> --key embedded_key.pem console
    python3 air_rpc.py --host <air-ip> listen                # watch events
"""

import argparse
import base64
import json
import os
import queue
import socket
import sys
import threading
import time

PORT = 4700

# Method names lifted from seestar_alp — Seestar and ASIAIR share ZWO's RPC
# framework, so this is a high-quality wordlist rather than guesswork. The
# device answers what it implements and stays *silent* on what it doesn't,
# so probing is "send everything, see who replies".
#
# Read-only only. Anything that moves the mount, exposes, or reboots lives in
# WRITE_METHODS below and is never sent by `probe`.
PROBE_METHODS = [
    "test_connection", "noop",
    "get_app_setting", "get_setting", "get_test_setting",
    "get_device_state", "get_event_state", "get_view_state",
    "get_camera_info", "get_camera_state", "get_camera_exp_and_bin",
    "get_control_value", "get_controls",
    "get_disk_volume", "get_image_save_path", "get_img_name_field",
    "get_albums", "is_stacked", "get_stack_info", "get_stack_setting",
    "get_sequence_setting", "get_sensor_calibration",
    "get_solve_result", "get_last_solve_result", "get_annotate_result",
    "get_focuser_position", "get_wheel_position", "get_wheel_setting",
    "get_wheel_state", "get_stacked_img", "get_server_log",
    "get_user_location", "get_verify_str", "pi_is_verified",
    "pi_get_ap", "pi_get_time", "pi_station_state",
    "scope_get_equ_coord", "scope_get_horiz_coord", "scope_get_ra_dec",
    "scope_get_track_state",
    "iscope_get_app_state", "scan_iscope",
]

# NOT probed — these change state. Call them deliberately via `call`/`console`.
WRITE_METHODS = [
    "scope_goto", "scope_sync", "scope_park", "scope_speed_move",
    "scope_move_to_horizon", "scope_set_track_state",
    "start_exposure", "stop_exposure", "start_solve", "start_polar_align",
    "stop_polar_align", "start_auto_focuse", "stop_auto_focuse",
    "start_create_dark", "start_scan_planet", "move_focuser",
    "begin_streaming", "stop_streaming",
    "iscope_start_view", "iscope_stop_view", "iscope_start_stack",
    "set_setting", "set_app_setting", "set_control_value", "set_stack_setting",
    "set_stack_type", "set_sequence_setting", "set_user_location",
    "set_img_name_field", "set_sensor_calibration", "set_wheel_position",
    "verify_client", "play_sound",
    "pi_output_set2", "pi_set_time", "pi_reboot", "pi_shutdown",
]


def sign_challenge(key_path, challenge):
    """Sign a challenge string RSA PKCS#1 v1.5 / SHA-1, base64-encoded.

    `cryptography` is imported here rather than at module top so the rest of
    the module keeps working (unauthenticated) without it installed.
    """
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    with open(key_path, "rb") as f:
        key = serialization.load_pem_private_key(f.read(), password=None)
    sig = key.sign(challenge.encode(), padding.PKCS1v15(), hashes.SHA1())
    return base64.b64encode(sig).decode()


class Air:
    def __init__(self, host, port=PORT, timeout=10, key=None):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(None)
        self._id = 0
        self._buf = b""
        self._replies = {}
        self._events = queue.Queue()
        self._lock = threading.Lock()
        self._alive = True
        self.verified = False
        self._rx = threading.Thread(target=self._reader, daemon=True)
        self._rx.start()
        if key:
            self.verify(key)

    def verify(self, key_path, timeout=8):
        """Complete the RSA challenge handshake; unlocks the gated methods.

        Returns True when pi_is_verified confirms. Raises on a missing/rejected
        challenge. Verification is per-connection — it lives on this socket.
        """
        vs = self.call("get_verify_str", [], timeout=timeout)
        challenge = (vs.get("result") or {}).get("str") if isinstance(vs, dict) else None
        if not challenge:
            raise RuntimeError(f"no challenge from get_verify_str: {vs}")
        signature = sign_challenge(key_path, challenge)
        # verify_client takes two positional strings: [signature, challenge].
        self.call("verify_client", [signature, challenge], timeout=timeout)
        pv = self.call("pi_is_verified", [], timeout=timeout)
        self.verified = bool(isinstance(pv, dict) and pv.get("result"))
        return self.verified

    def _reader(self):
        while self._alive:
            try:
                chunk = self.sock.recv(65536)
            except OSError:
                break
            if not chunk:
                break
            self._buf += chunk
            while b"\n" in self._buf:
                line, self._buf = self._buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line.decode("utf-8", "replace"))
                except ValueError:
                    self._events.put({"_raw": line[:500].decode("utf-8", "replace")})
                    continue
                if isinstance(msg, dict) and "id" in msg:
                    with self._lock:
                        ev = self._replies.get(msg["id"])
                        if ev:
                            ev[1] = msg
                            ev[0].set()
                            continue
                self._events.put(msg)
        self._alive = False

    def send(self, method, params=None):
        """Fire a request without waiting. Returns (rid, slot) for later collection."""
        with self._lock:
            self._id += 1
            rid = self._id
            slot = [threading.Event(), None]
            self._replies[rid] = slot
        req = {"id": rid, "method": method}
        if params is not None:
            req["params"] = params
        self.sock.sendall((json.dumps(req) + "\r\n").encode())
        return rid, slot

    def call(self, method, params=None, timeout=15):
        rid, slot = self.send(method, params)
        if not slot[0].wait(timeout):
            with self._lock:
                self._replies.pop(rid, None)
            raise TimeoutError(f"no reply to {method!r} within {timeout}s")
        with self._lock:
            self._replies.pop(rid, None)
        return slot[1]

    def drain_events(self):
        out = []
        while True:
            try:
                out.append(self._events.get_nowait())
            except queue.Empty:
                return out

    def close(self):
        self._alive = False
        try:
            self.sock.close()
        except OSError:
            pass


def show(msg):
    print(json.dumps(msg, indent=2, ensure_ascii=False))


def cmd_probe(air, wait=10.0):
    """Pipelined: fire every candidate, then collect. Silence == unimplemented."""
    print(f"probing {len(PROBE_METHODS)} read-only methods "
          f"(pipelined, {wait:.0f}s collection window)\n")
    slots = []
    for m in PROBE_METHODS:
        slots.append((m, air.send(m, [])))
        time.sleep(0.02)

    time.sleep(wait)

    live, silent = [], []
    for m, (rid, slot) in slots:
        if not slot[0].is_set():
            silent.append(m)
            continue
        r = slot[1]
        if "result" in r:
            live.append(m)
            print(f"  {m:<26} OK    {json.dumps(r['result'], ensure_ascii=False)[:170]}")
        else:
            print(f"  {m:<26} err   {json.dumps(r, ensure_ascii=False)[:120]}")

    print(f"\n{len(live)} implemented | {len(silent)} silent")
    if silent:
        print("silent: " + ", ".join(silent))
    ev = air.drain_events()
    if ev:
        kinds = {}
        for e in ev:
            kinds.setdefault(e.get("Event", "?"), []).append(e)
        print(f"\n{len(ev)} async events, by type:")
        for k, v in kinds.items():
            print(f"  {k:<16} x{len(v):<4} {json.dumps(v[-1], ensure_ascii=False)[:150]}")


def cmd_listen(air, seconds):
    print(f"listening for events for {seconds}s (Ctrl-C to stop)\n")
    end = time.time() + seconds
    try:
        while time.time() < end:
            for e in air.drain_events():
                print(time.strftime("%H:%M:%S"), json.dumps(e, ensure_ascii=False))
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass


def cmd_console(air):
    print("ASIAIR RPC console.  <method> [json-params]   |   .events   .quit\n")
    while True:
        try:
            line = input("air> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            continue
        if line in (".quit", ".q", "exit"):
            return
        if line == ".events":
            for e in air.drain_events():
                print("  ", json.dumps(e, ensure_ascii=False))
            continue
        method, _, rest = line.partition(" ")
        params = []
        if rest.strip():
            try:
                params = json.loads(rest)
            except ValueError:
                print("  params must be JSON, e.g.  set_setting [{\"exp_ms\":1000}]")
                continue
        try:
            show(air.call(method, params))
        except TimeoutError as e:
            print("  ", e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("ASIAIR_HOST"),
                    required="ASIAIR_HOST" not in os.environ,
                    help="Air IP address (or set the ASIAIR_HOST env var)")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--key", help="RSA key PEM; runs the auth handshake to "
                                  "unlock gated methods (mount, solve, autorun)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("probe")
    sub.add_parser("console")
    l = sub.add_parser("listen"); l.add_argument("--seconds", type=int, default=60)
    c = sub.add_parser("call")
    c.add_argument("method"); c.add_argument("params", nargs="?", default="[]")

    a = ap.parse_args()
    try:
        air = Air(a.host, a.port)
    except OSError as e:
        print(f"cannot connect to {a.host}:{a.port} — {e}", file=sys.stderr)
        return 1

    if a.key:
        try:
            ok = air.verify(a.key)
        except Exception as e:  # missing key, no cryptography, rejected challenge
            print(f"auth failed: {e}", file=sys.stderr)
            air.close()
            return 1
        print(f"auth: verified={ok}", file=sys.stderr)
        if not ok:
            air.close()
            return 1

    try:
        if a.cmd == "probe":
            cmd_probe(air)
        elif a.cmd == "console":
            cmd_console(air)
        elif a.cmd == "listen":
            cmd_listen(air, a.seconds)
        elif a.cmd == "call":
            show(air.call(a.method, json.loads(a.params)))
    finally:
        air.close()


if __name__ == "__main__":
    sys.exit(main())
