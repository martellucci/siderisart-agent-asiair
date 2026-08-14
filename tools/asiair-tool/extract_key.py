#!/usr/bin/env python3
"""Extract the ASIAIR RSA interop key from the ZWO app package.

The key the 4700 handshake needs (see README / air_rpc.py --key) is embedded as
a plain PEM block inside the app's native library `libopenssllib.so`, which is
packed inside the ASIAIR APK/XAPK. This walks the package — recursing through
nested apk/zip containers — pulls every PEM private-key block it finds,
validates each as an RSA key, and writes it out.

For interoperability with a device you own, using an app you are licensed to use
(the DMCA §1201(f) exemption the README describes). It reads the package you
point it at; it does not download anything.

    python3 extract_key.py ASIAIR_3.0.0_APKPure.xapk          # -> embedded_key.pem
    python3 extract_key.py app.apk -o mykey.pem
    python3 extract_key.py /path/to/libopenssllib.so          # a bare .so works too
    python3 extract_key.py app.xapk --index 1                 # if more than one key

Needs the `cryptography` package to validate/fingerprint keys (same optional dep
air_rpc.py uses); without it, extraction still works but skips validation.
"""

import argparse
import io
import re
import sys
import zipfile
from pathlib import Path

# PEM private-key block: PKCS#8 ("PRIVATE KEY"), PKCS#1 ("RSA PRIVATE KEY"),
# "EC PRIVATE KEY", or "ENCRYPTED PRIVATE KEY". Non-greedy body, DOTALL.
PEM_RE = re.compile(
    rb"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----.*?"
    rb"-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----",
    re.S,
)
ZIP_EXTS = (".xapk", ".apk", ".apks", ".zip", ".jar")


def walk(name, data):
    """Yield (display_name, bytes) for every leaf file, recursing into zips."""
    if name.lower().endswith(ZIP_EXTS) or data[:2] == b"PK":
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            yield name, data
            return
        for info in zf.infolist():
            if info.is_dir():
                continue
            try:
                sub = zf.read(info)
            except Exception:
                continue
            yield from walk(f"{name}!{info.filename}", sub)
    else:
        yield name, data


def source_iter(path):
    p = Path(path)
    if p.is_dir():
        for f in sorted(p.rglob("*")):
            if f.is_file():
                yield from walk(str(f), f.read_bytes())
    else:
        yield from walk(str(p), p.read_bytes())


def describe(pem):
    """Return (label, fingerprint) for a PEM key, or (None, None) if invalid."""
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        key = serialization.load_pem_private_key(pem, password=None)
    except ImportError:
        return "unvalidated (install `cryptography`)", None
    except Exception:
        return None, None
    kind = f"RSA {key.key_size}-bit" if isinstance(key, rsa.RSAPrivateKey) \
        else type(key).__name__
    pub = key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo)
    h = hashes.Hash(hashes.SHA256()); h.update(pub)
    return kind, h.finalize().hex()[:16]


def main():
    ap = argparse.ArgumentParser(description="Extract the ASIAIR RSA interop key from an APK/XAPK.")
    ap.add_argument("package", help="path to .xapk / .apk / .so / directory")
    ap.add_argument("-o", "--out", default="embedded_key.pem",
                    help="output PEM path (default: embedded_key.pem)")
    ap.add_argument("--index", type=int, help="which key to write if several are found (0-based)")
    ap.add_argument("--stdout", action="store_true", help="print the key instead of writing a file")
    a = ap.parse_args()

    if not Path(a.package).exists():
        print(f"no such path: {a.package}", file=sys.stderr)
        return 2

    # Collect distinct PEM blocks, remembering where each first appeared.
    found = {}  # pem-bytes -> source name
    for name, data in source_iter(a.package):
        for m in PEM_RE.finditer(data):
            found.setdefault(m.group(), name)

    if not found:
        print("no PEM private-key block found in the package.", file=sys.stderr)
        return 1

    keys = []  # (pem, source, label, fingerprint)
    for pem, src in found.items():
        label, fp = describe(pem)
        if label is None:
            continue  # not a parseable private key — skip
        keys.append((pem, src, label, fp))

    if not keys:
        print("found PEM blocks, but none parsed as a private key.", file=sys.stderr)
        return 1

    # Informational output goes to stderr so --stdout yields a clean key.
    print(f"found {len(keys)} private key(s):", file=sys.stderr)
    for i, (_, src, label, fp) in enumerate(keys):
        print(f"  [{i}] {label:<14} fp={fp}  in {src}", file=sys.stderr)

    if len(keys) > 1 and a.index is None:
        print("\nmore than one key — re-run with --index N to choose.", file=sys.stderr)
        return 3
    pem, src, label, fp = keys[a.index or 0]

    if a.stdout:
        sys.stdout.buffer.write(pem if pem.endswith(b"\n") else pem + b"\n")
        return 0

    out = Path(a.out)
    if out.exists():
        old = out.read_bytes()
        if PEM_RE.search(old) and PEM_RE.search(old).group() == pem:
            print(f"\n{out} already contains this exact key — nothing to do.")
            return 0
        print(f"\nnote: overwriting existing {out}", file=sys.stderr)
    out.write_bytes(pem if pem.endswith(b"\n") else pem + b"\n")
    try:
        out.chmod(0o600)
    except OSError:
        pass
    print(f"\nwrote {label} key ({fp}) to {out}")
    print("This is a private key — keep it local; it is git-ignored in this repo.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
