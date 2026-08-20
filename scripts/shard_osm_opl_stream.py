#!/usr/bin/env python3
"""Fan an OPL stream into exactly N persistent scanner subprocesses.

Each OPL line is one OSM object version. Round-robin distribution therefore
preserves every version exactly once while avoiding ambiguous GNU parallel
block/process lifetime semantics. Each shard scanner owns one output directory
for the full lifetime of the stream.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shards", type=int, default=4)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--source-label", required=True)
    ap.add_argument("--history", action="store_true")
    args = ap.parse_args()
    if args.shards < 1:
        raise SystemExit("--shards must be >=1")
    args.out.mkdir(parents=True, exist_ok=True)

    scanner = Path(__file__).resolve().parent / "scan_osm_all_name_opl.py"
    procs = []
    try:
        for i in range(args.shards):
            shard = args.out / str(i)
            shard.mkdir(parents=True, exist_ok=True)
            cmd = [
                sys.executable, str(scanner), "--out", str(shard),
                "--source-label", args.source_label,
            ]
            if args.history:
                cmd.append("--history")
            procs.append(subprocess.Popen(cmd, stdin=subprocess.PIPE))

        lines = 0
        for line in sys.stdin.buffer:
            p = procs[lines % len(procs)]
            assert p.stdin is not None
            p.stdin.write(line)
            lines += 1
            if lines % 5_000_000 == 0:
                print(f"OPL_SHARD_PROGRESS lines={lines}", file=sys.stderr, flush=True)
        for p in procs:
            if p.stdin:
                p.stdin.close()
        codes = [p.wait() for p in procs]
        bad = [(i, c) for i, c in enumerate(codes) if c != 0]
        if bad:
            print(f"shard scanner failures: {bad}", file=sys.stderr)
            return 8
        print(f"OPL_SHARD_COMPLETE lines={lines} shards={len(procs)}", file=sys.stderr)
        return 0
    except Exception:
        for p in procs:
            try:
                if p.stdin:
                    p.stdin.close()
            except Exception:
                pass
            if p.poll() is None:
                p.terminate()
        raise


if __name__ == "__main__":
    raise SystemExit(main())
