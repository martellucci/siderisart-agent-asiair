#!/usr/bin/env python3
"""Attempt the ASIAIR 4700 RPC auth handshake with a supplied RSA key.

Flow (firmware 7.18+, per seestar_alp):
    get_verify_str            -> {"str": "<challenge>"}
    sign(challenge)            RSA PKCS#1 v1.5 / SHA-1, base64
    verify_client {sign,data}  -> server accepts
    pi_is_verified             -> confirms the channel is unlocked

Usage:
    python3 handshake.py --host <air-ip> --key embedded_key.pem
"""

import argparse
import base64
import sys

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from air_rpc import Air


def load_key(path):
    with open(path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def sign(key, challenge):
    """RSA PKCS#1 v1.5 over SHA-1 of the challenge bytes, base64-encoded."""
    sig = key.sign(challenge.encode(), padding.PKCS1v15(), hashes.SHA1())
    return base64.b64encode(sig).decode()


def result_of(reply):
    """Pull .result out of a reply, or None if the method was dropped/errored."""
    if reply is None:
        return None
    return reply.get("result") if "result" in reply else reply


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, default=4700)
    ap.add_argument("--key", default="embedded_key.pem")
    a = ap.parse_args()

    key = load_key(a.key)
    print(f"key: {key.key_size}-bit RSA")

    air = Air(a.host, a.port)
    try:
        print("test_connection ->", result_of(air.call("test_connection", [])))

        vs = result_of(air.call("get_verify_str", []))
        print("get_verify_str  ->", vs)
        challenge = vs["str"] if isinstance(vs, dict) else vs
        if not challenge:
            print("no challenge returned; cannot continue")
            return 1

        signature = sign(key, challenge)
        print(f"signed ({len(signature)} b64 chars): {signature[:44]}...")

        # verify_client takes two positional string params: [signature, challenge].
        try:
            vc = air.call("verify_client", [signature, challenge], timeout=8)
            print("verify_client   ->", result_of(vc))
        except TimeoutError:
            print("verify_client   -> (silent — server dropped it)")

        try:
            pv = result_of(air.call("pi_is_verified", [], timeout=8))
            print("pi_is_verified  ->", pv)
        except TimeoutError:
            print("pi_is_verified  -> (silent)")

        # If verified, a previously-gated read should now answer.
        try:
            st = air.call("get_device_state", [], timeout=8)
            print("get_device_state->", result_of(st))
        except TimeoutError:
            print("get_device_state-> (still silent — not verified)")
    finally:
        air.close()


if __name__ == "__main__":
    sys.exit(main())
