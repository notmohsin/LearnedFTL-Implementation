#!/usr/bin/env python3
"""Generate a reproducible hot/cold block-I/O trace."""

from __future__ import annotations

import argparse
import bisect
import csv
import random
from pathlib import Path


PAGE_SIZE = 4096
RESULT_COLUMNS = (
    "run_id",
    "ftl",
    "workload",
    "workload_description",
    "seed",
    "requested_operations",
    "trace_file",
    "address_range_gib",
    "read_percent",
    "write_percent",
    "hot_region_percent",
    "hot_access_percent",
    "locality_model",
    "zipf_alpha",
)


def next_run_id(rows: list[dict[str, str]]) -> str:
    ids = [int(row["run_id"]) for row in rows if row.get("run_id", "").isdigit()]
    return str(max(ids, default=0) + 1)


def append_metadata(csv_path: Path, metadata: dict[str, str]) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    if csv_path.exists():
        with csv_path.open(newline="", encoding="utf-8") as source:
            reader = csv.DictReader(source)
            rows = list(reader)
            fields = list(reader.fieldnames or [])
    else:
        rows = []
        fields = []

    for field in RESULT_COLUMNS:
        if field not in fields:
            fields.append(field)
    rows.append({field: metadata.get(field, "") for field in fields})
    temporary = csv_path.with_suffix(".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(csv_path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operations", type=int, default=100_000)
    parser.add_argument("--range-gib", type=float, default=1.0)
    parser.add_argument("--read-percent", type=float, default=50.0)
    parser.add_argument("--hot-percent", type=float, default=20.0)
    parser.add_argument("--hot-access-percent", type=float, default=80.0)
    parser.add_argument("--zipf-alpha", type=float, default=1.2)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--trace",
        type=Path,
        default=Path(__file__).parents[1] / "traces" / "zipf-80-20.trace",
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=Path(__file__).parents[1] / "results" / "leaftl_runs.csv",
    )
    args = parser.parse_args()
    if args.operations <= 0 or args.range_gib <= 0:
        raise ValueError("operations and range-gib must be positive")
    if not 0 <= args.read_percent <= 100:
        raise ValueError("read-percent must be between 0 and 100")

    total_pages = int(args.range_gib * (1024**3) // PAGE_SIZE)
    hot_pages = max(1, int(total_pages * args.hot_percent / 100))
    cold_pages = total_pages - hot_pages
    if cold_pages <= 0:
        raise ValueError("the address range must contain a cold region")

    weights = [1.0 / (rank**args.zipf_alpha) for rank in range(1, hot_pages + 1)]
    cumulative = []
    total_weight = 0.0
    for weight in weights:
        total_weight += weight
        cumulative.append(total_weight)

    rng = random.Random(args.seed)
    args.trace.parent.mkdir(parents=True, exist_ok=True)
    with args.trace.open("w", encoding="utf-8") as trace:
        trace.write("# timestamp operation offset_bytes length_bytes\n")
        for operation_number in range(args.operations):
            hot = rng.random() < args.hot_access_percent / 100
            if hot:
                draw = rng.random() * total_weight
                page = bisect.bisect_left(cumulative, draw) + 1
            else:
                page = hot_pages + rng.randrange(cold_pages)
            operation = "R" if rng.random() < args.read_percent / 100 else "W"
            timestamp = operation_number * 0.000001
            trace.write(f"{timestamp:.6f} {operation} {(page - 1) * PAGE_SIZE} {PAGE_SIZE}\n")

    with args.csv.open(newline="", encoding="utf-8") as source:
        existing_rows = list(csv.DictReader(source))
    run_id = next_run_id(existing_rows)
    append_metadata(
        args.csv,
        {
            "run_id": run_id,
            "ftl": "pending",
            "workload": "zipf-80-20",
            "workload_description": "100,000 4 KiB operations over 1 GiB; 80% Zipf hot 20%, 20% uniform cold 80%",
            "seed": str(args.seed),
            "requested_operations": str(args.operations),
            "trace_file": str(args.trace),
            "address_range_gib": str(args.range_gib),
            "read_percent": str(args.read_percent),
            "write_percent": str(100 - args.read_percent),
            "hot_region_percent": str(args.hot_percent),
            "hot_access_percent": str(args.hot_access_percent),
            "locality_model": "zipf-hot-uniform-cold",
            "zipf_alpha": str(args.zipf_alpha),
        },
    )
    print(f"run_id={run_id}")
    print(f"trace={args.trace}")
    print(f"operations={args.operations}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
