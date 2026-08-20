#!/usr/bin/env python3
"""Stream a large immutable HTTP object with exact byte-range reconnection.

Unlike a plain `curl --retry` in a pipe, reconnecting never replays bytes that
have already been emitted. Every resumed response must begin at the exact next
byte, and completion is accepted only after the advertised object length has
been emitted exactly once.
"""
from __future__ import annotations

import argparse
import re
import sys
import time

import requests

CONTENT_RANGE = re.compile(r"^bytes\s+(\d+)-(\d+)/(\d+)$", re.I)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--chunk-size", type=int, default=4 * 1024 * 1024)
    ap.add_argument("--max-reconnects", type=int, default=100)
    ap.add_argument("--connect-timeout", type=int, default=30)
    ap.add_argument("--read-timeout", type=int, default=180)
    args = ap.parse_args()

    offset = 0
    total = None
    reconnects = 0
    out = sys.stdout.buffer
    headers = {"User-Agent": "test-fuckle-proof-range-stream/1.0"}

    while total is None or offset < total:
        req_headers = dict(headers)
        req_headers["Range"] = f"bytes={offset}-"
        try:
            with requests.get(
                args.url,
                headers=req_headers,
                stream=True,
                timeout=(args.connect_timeout, args.read_timeout),
                allow_redirects=True,
            ) as r:
                if r.status_code == 416 and total is not None and offset == total:
                    break
                if offset > 0 and r.status_code != 206:
                    raise RuntimeError(
                        f"resume request at byte {offset} did not return HTTP 206; got {r.status_code}"
                    )
                if r.status_code not in (200, 206):
                    r.raise_for_status()

                if r.status_code == 206:
                    cr = r.headers.get("Content-Range", "")
                    m = CONTENT_RANGE.match(cr.strip())
                    if not m:
                        raise RuntimeError(f"missing/invalid Content-Range: {cr!r}")
                    start, end, advertised_total = map(int, m.groups())
                    if start != offset:
                        raise RuntimeError(
                            f"server resumed at byte {start}, expected exact byte {offset}"
                        )
                    if end < start or advertised_total <= end:
                        raise RuntimeError(f"invalid Content-Range bounds: {cr!r}")
                    if total is None:
                        total = advertised_total
                    elif total != advertised_total:
                        raise RuntimeError(
                            f"source length changed during scan: {total} -> {advertised_total}"
                        )
                else:
                    if offset != 0:
                        raise RuntimeError("HTTP 200 is only acceptable for the initial byte-0 request")
                    length = r.headers.get("Content-Length")
                    if not length or not length.isdigit():
                        raise RuntimeError("initial HTTP 200 response has no numeric Content-Length")
                    total = int(length)

                before = offset
                for chunk in r.iter_content(chunk_size=args.chunk_size):
                    if not chunk:
                        continue
                    if total is not None and offset + len(chunk) > total:
                        raise RuntimeError("server emitted bytes beyond advertised source length")
                    out.write(chunk)
                    offset += len(chunk)
                out.flush()

                if total is not None and offset < total:
                    if offset == before:
                        raise RuntimeError("connection ended without making byte progress")
                    raise requests.ConnectionError(
                        f"connection ended early at {offset}/{total}; reconnecting by Range"
                    )

        except BrokenPipeError:
            # Downstream failed/closed. The shell pipeline's pipefail will expose
            # the downstream failure; avoid noisy traceback while exiting nonzero.
            return 141
        except Exception as e:
            reconnects += 1
            if reconnects > args.max_reconnects:
                print(
                    f"RANGE_STREAM_FATAL offset={offset} total={total} reconnects={reconnects}: "
                    f"{type(e).__name__}: {e}",
                    file=sys.stderr,
                    flush=True,
                )
                return 2
            delay = min(2 ** min(reconnects - 1, 5), 30)
            print(
                f"RANGE_STREAM_RECONNECT offset={offset} total={total} attempt={reconnects} "
                f"sleep={delay}: {type(e).__name__}: {e}",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(delay)

    if total is None or offset != total:
        print(f"RANGE_STREAM_INCOMPLETE offset={offset} total={total}", file=sys.stderr)
        return 3
    print(
        f"RANGE_STREAM_COMPLETE bytes={offset} reconnects={reconnects}",
        file=sys.stderr,
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
