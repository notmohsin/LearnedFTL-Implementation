#!/usr/bin/env python3
"""Create a comparison row by cloning workload metadata from an existing run."""

from __future__ import annotations

import argparse
import csv
import os
import tempfile
from pathlib import Path


FTL_FIELDS = (
    "ftl_accesses", "writes", "cmt_accesses", "cmt_hits", "cmt_misses",
    "cmt_hit_rate", "model_queries", "model_hits", "model_corrections",
    "model_misses", "model_hit_rate", "single_reads", "double_reads",
    "triple_reads", "translation_reads", "double_read_rate",
    "triple_read_rate", "gc",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--source-run-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--ftl", required=True)
    args = parser.parse_args()

    with args.csv.open(newline="", encoding="utf-8") as source:
        reader = csv.DictReader(source)
        rows = list(reader)
        fields = list(reader.fieldnames or [])
    source_rows = [row for row in rows if row.get("run_id") == args.source_run_id]
    if len(source_rows) != 1:
        raise ValueError(f"expected exactly one source row with run_id={args.source_run_id}")
    if any(row.get("run_id") == args.run_id for row in rows):
        raise ValueError(f"run_id={args.run_id} already exists")

    row = dict(source_rows[0])
    row["run_id"] = args.run_id
    row["ftl"] = args.ftl
    for field in FTL_FIELDS:
        row[field] = ""
    rows.append(row)

    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{args.csv.name}.", suffix=".tmp", dir=args.csv.parent
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        with temporary.open("w", newline="", encoding="utf-8") as output:
            writer = csv.DictWriter(output, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        temporary.replace(args.csv)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"created run_id={args.run_id} ftl={args.ftl}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
