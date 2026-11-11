#!/usr/bin/env python3
"""KI-28 attribution: replay every LeaFTL run without GC through leaftl_replica.py.

    ki28_attribution.py [--jobs N] [--out experiments/results/ki28_attribution.csv]

For each run it (1) checks that the replica reproduces FEMU's counters exactly in every interval, and (2) replays
the run again with each bug switched off (insert, sort, last, drop, compact, crb, insert+compact,
insert+compact+crb, and all six) and reports the workload
interval's lost-data share: reads of written LPNs that the learned lookup could not resolve
(invalid_but_mapped) over reads of written LPNs.
"""

from __future__ import annotations

import argparse
import csv
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import leaftl_replica as lr  # noqa: E402

ROOT = HERE.parents[1]
VARIANTS = [("authors", set()), ("fix_insert", {"insert"}), ("fix_sort", {"sort"}), ("fix_last", {"last"}),
            ("fix_drop", {"drop"}), ("fix_compact", {"compact"}), ("fix_crb", {"crb"}),
            ("fix_insert_compact", {"insert", "compact"}), ("fix_insert_compact_crb", {"insert", "compact", "crb"}),
            ("fix_all", {"insert", "sort", "last", "drop", "compact", "crb"})]


def lost_share(c) -> float:
    mapped = c.get("served_lpns", 0) + c.get("invalid_but_mapped", 0)
    return c.get("invalid_but_mapped", 0) / mapped if mapped else 0.0


def one(run: Path):
    row = {"run": run.name}
    for name, fix in VARIANTS:
        rep, intervals, records = lr.replay(run, fix)
        work = intervals[-1]
        if not fix:
            bad = [k for p, r in zip(intervals, records) for k in lr.COMPARED
                   if r.get(k) is not None and p.get(k, 0) != r[k]]
            row["intervals"] = len(intervals)
            row["counters_compared"] = sum(1 for r in records for k in lr.COMPARED if r.get(k) is not None)
            row["mismatches"] = len(bad)
            row["segments_built"] = rep.total_segments
            row["segments_dropped_by_insert"] = rep.lost_segments
            row["femu_lost_share"] = round(lost_share(records[-1]), 4)
        row[f"lost_share_{name}"] = round(lost_share(work), 4)
    return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--out", type=Path, default=ROOT / "experiments/results/ki28_attribution.csv")
    args = ap.parse_args()
    import json
    runs = []
    stages = [s for s in (ROOT / "experiments/results").iterdir() if s.is_dir() and not s.name.startswith("_")]
    for d in sorted((d for s in stages for d in s.glob("*-leaftl-*") if d.is_dir()), key=lambda p: p.name):
        if not (d / "records.jsonl").exists() or not (d / "reqlog.csv.gz").exists():
            continue
        records = [json.loads(l) for l in (d / "records.jsonl").read_text().splitlines() if l]
        if any(r.get("gc_runs", 0) for r in records):
            continue
        runs.append(d)
    with ProcessPoolExecutor(args.jobs) as pool:
        rows = list(pool.map(one, runs))
    fields = list(rows[0].keys())
    with args.out.open("w", newline="") as handle:
        w = csv.DictWriter(handle, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    bad = sum(r["mismatches"] for r in rows)
    print(f"{len(rows)} runs, {sum(r['counters_compared'] for r in rows)} counters compared, {bad} mismatches")
    print(f"wrote {args.out}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
