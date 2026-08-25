#!/usr/bin/env python3
"""Emit one complete, block-aligned byte segment of an immutable OSM PBF.

Segments contain whole PBF records only. At nonzero starts the emitted bytes
begin with an OSMData record; callers may prepend the source OSMHeader record
for tools that expect a standalone PBF. The manifest records exact source byte
coverage so independent segment results can later be proven contiguous.
"""
from __future__ import annotations

import argparse
import json
import re
import struct
import sys
import time
from pathlib import Path

import requests

CONTENT_RANGE = re.compile(r"^bytes\s+(\d+)-(\d+)/(\d+)$", re.I)
MAX_HEADER = 64 * 1024
MAX_BLOB = 64 * 1024 * 1024


def read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    value = 0
    shift = 0
    for _ in range(10):
        if pos >= len(buf):
            raise ValueError("truncated protobuf varint")
        b = buf[pos]
        pos += 1
        value |= (b & 0x7F) << shift
        if not (b & 0x80):
            return value, pos
        shift += 7
    raise ValueError("protobuf varint too long")


def blob_header_info(buf: bytes) -> tuple[str | None, int]:
    pos = 0
    typ = None
    datasize = None
    while pos < len(buf):
        key, pos = read_varint(buf, pos)
        field, wire = key >> 3, key & 7
        if wire == 0:
            value, pos = read_varint(buf, pos)
            if field == 3:
                datasize = value
        elif wire == 1:
            pos += 8
        elif wire == 2:
            n, pos = read_varint(buf, pos)
            if n < 0 or pos + n > len(buf):
                raise ValueError("invalid protobuf length-delimited field")
            value = buf[pos:pos+n]
            pos += n
            if field == 1:
                typ = value.decode("utf-8", errors="strict")
        elif wire == 5:
            pos += 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")
        if pos > len(buf):
            raise ValueError("protobuf field exceeds BlobHeader")
    if datasize is None:
        raise ValueError("BlobHeader missing datasize")
    if not (0 <= datasize <= MAX_BLOB):
        raise ValueError(f"invalid Blob datasize {datasize}")
    return typ, datasize


class RangeReader:
    def __init__(self, url: str, start: int, max_reconnects: int = 100):
        self.url = url
        self.offset = start
        self.total: int | None = None
        self.max_reconnects = max_reconnects
        self.reconnects = 0
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "test-fuckle-proof-pbf-segment/1.0"})
        self.response = None
        self._open()

    def _open(self) -> None:
        if self.response is not None:
            try:
                self.response.close()
            except Exception:
                pass
        while True:
            try:
                r = self.session.get(
                    self.url,
                    headers={"Range": f"bytes={self.offset}-"},
                    stream=True,
                    timeout=(30, 180),
                    allow_redirects=True,
                )
                if self.offset > 0 and r.status_code != 206:
                    raise RuntimeError(f"range resume at {self.offset} returned HTTP {r.status_code}")
                if r.status_code not in (200, 206):
                    r.raise_for_status()
                if r.status_code == 206:
                    m = CONTENT_RANGE.match((r.headers.get("Content-Range") or "").strip())
                    if not m:
                        raise RuntimeError("missing/invalid Content-Range")
                    first, last, total = map(int, m.groups())
                    if first != self.offset or last < first or total <= last:
                        raise RuntimeError(f"bad Content-Range {m.group(0)!r} at {self.offset}")
                    if self.total is None:
                        self.total = total
                    elif self.total != total:
                        raise RuntimeError(f"source length changed {self.total}->{total}")
                else:
                    if self.offset != 0:
                        raise RuntimeError("HTTP 200 allowed only at source byte 0")
                    length = r.headers.get("Content-Length")
                    if not length or not length.isdigit():
                        raise RuntimeError("initial HTTP 200 has no numeric Content-Length")
                    self.total = int(length)
                self.response = r
                return
            except Exception:
                self.reconnects += 1
                if self.reconnects > self.max_reconnects:
                    raise
                time.sleep(min(2 ** min(self.reconnects - 1, 5), 30))

    def read_exact(self, n: int) -> bytes:
        out = bytearray()
        while len(out) < n:
            if self.total is not None and self.offset >= self.total:
                raise EOFError(f"source ended at {self.offset} while reading {n} bytes")
            try:
                assert self.response is not None
                part = self.response.raw.read(n - len(out))
                if not part:
                    raise requests.ConnectionError("range response ended early")
                out += part
                self.offset += len(part)
            except Exception:
                self.reconnects += 1
                if self.reconnects > self.max_reconnects:
                    raise
                time.sleep(min(2 ** min(self.reconnects - 1, 5), 30))
                self._open()
        return bytes(out)

    def close(self) -> None:
        if self.response is not None:
            self.response.close()
        self.session.close()


def read_record(rr: RangeReader) -> tuple[bytes, str | None]:
    prefix = rr.read_exact(4)
    header_len = struct.unpack(">I", prefix)[0]
    if not (1 <= header_len <= MAX_HEADER):
        raise ValueError(f"invalid PBF BlobHeader length {header_len} at {rr.offset-4}")
    header = rr.read_exact(header_len)
    typ, datasize = blob_header_info(header)
    blob = rr.read_exact(datasize)
    return prefix + header + blob, typ


def source_header(url: str) -> bytes:
    rr = RangeReader(url, 0)
    try:
        record, typ = read_record(rr)
        if typ != "OSMHeader":
            raise RuntimeError(f"first PBF record is {typ!r}, expected OSMHeader")
        return record
    finally:
        rr.close()


def atomic_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--start-offset", type=int, default=0)
    ap.add_argument("--max-bytes", type=int, default=16 * 1024 * 1024 * 1024)
    ap.add_argument("--manifest", type=Path, required=True)
    ap.add_argument("--header-out", type=Path)
    ap.add_argument("--max-reconnects", type=int, default=100)
    args = ap.parse_args()
    if args.start_offset < 0 or args.max_bytes <= 0:
        raise SystemExit("invalid start offset/max bytes")

    if args.header_out is not None:
        hdr = source_header(args.url)
        args.header_out.parent.mkdir(parents=True, exist_ok=True)
        args.header_out.write_bytes(hdr)

    rr = RangeReader(args.url, args.start_offset, args.max_reconnects)
    start = args.start_offset
    blocks = 0
    first_type = None
    last_type = None
    emitted = 0
    out = sys.stdout.buffer
    try:
        if rr.total is not None and start > rr.total:
            raise RuntimeError(f"start {start} beyond source length {rr.total}")
        if rr.total is not None and start == rr.total:
            atomic_json(args.manifest, {
                "source_url": args.url, "start_offset": start, "end_offset": start,
                "total_bytes": rr.total, "bytes_emitted": 0, "blocks_emitted": 0,
                "source_complete": True, "pbf_block_boundary_start": True,
                "pbf_block_boundary_end": True, "reconnects": rr.reconnects,
            })
            return 0

        while rr.total is None or rr.offset < rr.total:
            record, typ = read_record(rr)
            if blocks == 0:
                first_type = typ
                if start == 0 and typ != "OSMHeader":
                    raise RuntimeError(f"byte 0 record type is {typ!r}, expected OSMHeader")
                if start > 0 and typ != "OSMData":
                    raise RuntimeError(f"resume offset {start} is not an OSMData block boundary: {typ!r}")
            out.write(record)
            emitted += len(record)
            blocks += 1
            last_type = typ
            if emitted >= args.max_bytes:
                break
        out.flush()
        end = rr.offset
        total = rr.total
        if total is None:
            raise RuntimeError("source total length never established")
        manifest = {
            "source_url": args.url,
            "start_offset": start,
            "end_offset": end,
            "total_bytes": total,
            "bytes_emitted": emitted,
            "blocks_emitted": blocks,
            "first_record_type": first_type,
            "last_record_type": last_type,
            "source_complete": end == total,
            "pbf_block_boundary_start": True,
            "pbf_block_boundary_end": True,
            "reconnects": rr.reconnects,
        }
        atomic_json(args.manifest, manifest)
        print(json.dumps(manifest), file=sys.stderr, flush=True)
        return 0
    except BrokenPipeError:
        return 141
    finally:
        rr.close()


if __name__ == "__main__":
    raise SystemExit(main())
