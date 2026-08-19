#!/usr/bin/env python3
# Trigger independent Planet completeness audit via watched script path.
from __future__ import annotations
import argparse, hashlib, json, re, sys
from collections import Counter

ELIGIBLE = {"city","town","village","hamlet","isolated_dwelling","locality"}
PLACE_RE = re.compile(r"(?:^|,)place=([^,]+)(?:,|$)")

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    counts = Counter(); all_place_values = Counter(); seen = set(); duplicates = 0
    digest = hashlib.sha256(); lines = 0; tagged_place_objects = 0
    for raw in sys.stdin:
        lines += 1
        if not raw: continue
        typch = raw[0]
        if typch not in "nwr": continue
        sp = raw.find(" ")
        if sp < 2: continue
        try: oid = int(raw[1:sp])
        except ValueError: continue
        tags = ""
        for field in raw.rstrip("\n").split(" ")[1:]:
            if field.startswith("T"):
                tags = field[1:]; break
        if not tags: continue
        m = PLACE_RE.search(tags)
        if not m: continue
        place = m.group(1)
        tagged_place_objects += 1; all_place_values[place] += 1
        if place not in ELIGIBLE: continue
        typ = {"n":"node","w":"way","r":"relation"}[typch]
        key = (typch, oid)
        if key in seen: duplicates += 1
        else: seen.add(key)
        counts["total"] += 1; counts[f"place:{place}"] += 1; counts[f"osm_type:{typ}"] += 1
        digest.update(f"{typch}\t{oid}\t{place}\n".encode("utf-8"))
    out = {
        "label": args.label,
        "input_opl_lines": lines,
        "place_tagged_objects": tagged_place_objects,
        "eligible_counts": dict(counts),
        "eligible_unique_objects": len(seen),
        "duplicate_object_visits": duplicates,
        "eligible_ordered_object_digest_sha256": digest.hexdigest(),
        "all_place_value_counts": dict(all_place_values),
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, sort_keys=True); f.write("\n")
    print(json.dumps({"label": args.label,"eligible_total": counts["total"],"unique": len(seen),"duplicates": duplicates,"digest": digest.hexdigest()}))
    return 0 if duplicates == 0 else 4

if __name__ == "__main__":
    raise SystemExit(main())
