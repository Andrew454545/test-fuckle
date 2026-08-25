#!/usr/bin/env python3
"""Incremental MD5 using OpenSSL with a small serializable state file.

This exists solely to verify a large immutable source across multiple bounded
streaming jobs without rereading bytes. State is valid only when bytes_hashed
matches the next source byte offset exactly.
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import json
import sys
from pathlib import Path


class MD5_CTX(ctypes.Structure):
    _fields_ = [
        ("A", ctypes.c_uint32), ("B", ctypes.c_uint32),
        ("C", ctypes.c_uint32), ("D", ctypes.c_uint32),
        ("Nl", ctypes.c_uint32), ("Nh", ctypes.c_uint32),
        ("data", ctypes.c_uint32 * 16), ("num", ctypes.c_uint32),
    ]


def libcrypto():
    name = ctypes.util.find_library("crypto")
    if not name:
        raise RuntimeError("libcrypto not found")
    lib = ctypes.CDLL(name)
    lib.MD5_Init.argtypes = [ctypes.POINTER(MD5_CTX)]
    lib.MD5_Init.restype = ctypes.c_int
    lib.MD5_Update.argtypes = [ctypes.POINTER(MD5_CTX), ctypes.c_void_p, ctypes.c_size_t]
    lib.MD5_Update.restype = ctypes.c_int
    lib.MD5_Final.argtypes = [ctypes.POINTER(ctypes.c_ubyte), ctypes.POINTER(MD5_CTX)]
    lib.MD5_Final.restype = ctypes.c_int
    return lib


def new_ctx(lib) -> MD5_CTX:
    ctx = MD5_CTX()
    if lib.MD5_Init(ctypes.byref(ctx)) != 1:
        raise RuntimeError("MD5_Init failed")
    return ctx


def load_state(path: Path, lib, expected_offset: int) -> tuple[MD5_CTX, int]:
    if not path.exists():
        if expected_offset != 0:
            raise RuntimeError(f"missing MD5 state at nonzero offset {expected_offset}")
        return new_ctx(lib), 0
    d = json.loads(path.read_text(encoding="utf-8"))
    if d.get("format") != "openssl-md5-ctx-v1":
        raise RuntimeError(f"unsupported MD5 state format: {d.get('format')!r}")
    n = int(d["bytes_hashed"])
    if n != expected_offset:
        raise RuntimeError(f"MD5 state offset {n} != segment start {expected_offset}")
    ctx = MD5_CTX()
    for k in ("A", "B", "C", "D", "Nl", "Nh", "num"):
        setattr(ctx, k, int(d[k]))
    vals = d["data"]
    if len(vals) != 16:
        raise RuntimeError("invalid MD5 state data length")
    for i, v in enumerate(vals):
        ctx.data[i] = int(v)
    return ctx, n


def digest_hex(lib, ctx: MD5_CTX) -> str:
    clone = MD5_CTX()
    ctypes.memmove(ctypes.byref(clone), ctypes.byref(ctx), ctypes.sizeof(ctx))
    out = (ctypes.c_ubyte * 16)()
    if lib.MD5_Final(out, ctypes.byref(clone)) != 1:
        raise RuntimeError("MD5_Final failed")
    return bytes(out).hex()


def save_state(path: Path, ctx: MD5_CTX, bytes_hashed: int, digest: str) -> None:
    d = {
        "format": "openssl-md5-ctx-v1",
        "bytes_hashed": bytes_hashed,
        "A": int(ctx.A), "B": int(ctx.B), "C": int(ctx.C), "D": int(ctx.D),
        "Nl": int(ctx.Nl), "Nh": int(ctx.Nh), "num": int(ctx.num),
        "data": [int(x) for x in ctx.data],
        "digest_if_finalized_here": digest,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", type=Path, required=True)
    ap.add_argument("--start-offset", type=int, required=True)
    ap.add_argument("--chunk-size", type=int, default=8 * 1024 * 1024)
    args = ap.parse_args()
    if args.start_offset < 0 or args.chunk_size <= 0:
        raise SystemExit("invalid offset/chunk size")

    lib = libcrypto()
    ctx, count = load_state(args.state, lib, args.start_offset)
    inp = sys.stdin.buffer
    while True:
        chunk = inp.read(args.chunk_size)
        if not chunk:
            break
        buf = ctypes.create_string_buffer(chunk, len(chunk))
        if lib.MD5_Update(ctypes.byref(ctx), buf, len(chunk)) != 1:
            raise RuntimeError("MD5_Update failed")
        count += len(chunk)
    dig = digest_hex(lib, ctx)
    save_state(args.state, ctx, count, dig)
    print(json.dumps({"bytes_hashed": count, "digest_if_finalized_here": dig}), file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
