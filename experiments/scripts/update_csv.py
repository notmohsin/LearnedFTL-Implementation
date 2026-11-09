#!/usr/bin/env python3
"""Update a results row from one FTL JSONL statistics record."""

from __future__ import annotations

import argparse
import csv
import json
import os
import tempfile
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", type=Path, required=True)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--source-commit", default="")
    parser.add_argument("--compiler", default="")
    parser.add_argument("--host-kernel", default="")
    parser.add_argument("--guest-kernel", default="")
    parser.add_argument("--ssd-size", default="")
    parser.add_argument("--cmt-entries", default="")
    parser.add_argument("--notes", default="")
    args = parser.parse_args()

    record = json.loads(args.json.read_text(encoding="utf-8"))
    with args.csv.open(newline="", encoding="utf-8") as source:
        reader = csv.DictReader(source)
        rows = list(reader)
        fields = list(reader.fieldnames or [])
    matches = [row for row in rows if row.get("run_id") == args.run_id]
    if len(matches) != 1:
        raise ValueError(f"expected exactly one row with run_id={args.run_id}")

    mapping = {
        "ftl": "ftl",
        "internal_mapping_accesses": "internal_mapping_accesses",
        "access_cnt": "internal_mapping_accesses",
        "read_lpn_cnt": "read_lpn_count",
        "invalid_read_cnt": "invalid_read_count",
        "invariant_failures": "invariant_failures",
        "nand_page_writes": "nand_page_writes",
        "writes": "nand_page_writes",
        "cmt_accesses": "cmt_accesses",
        "cmt_hits": "cmt_hits",
        "cmt_misses": "cmt_misses",
        "cmt_hit_rate": "cmt_hit_rate",
        "model_queries": "model_queries",
        "model_hits": "model_hits",
        "model_corrections": "model_corrections",
        "model_misses": "model_misses",
        "model_hit_rate": "model_hit_rate",
        "single_reads": "single_reads",
        "double_reads": "double_reads",
        "triple_reads": "triple_reads",
        "translation_reads": "translation_reads",
        "gc": "gc",
    }
    for source, target in mapping.items():
        if source in record:
            value = record[source]
            if source == "cmt_hit_rate":
                value = f"{float(value) * 100:.9f}"
            matches[0][target] = str(value)

    row = matches[0]
    single = int(record.get("single_reads", 0))
    double = int(record.get("double_reads", 0))
    triple = int(record.get("triple_reads", 0))
    translation_reads = int(record.get("translation_reads", 0))
    row["double_read_rate"] = (
        f"{double / translation_reads:.9f}" if translation_reads else "0.000000000"
    )
    row["triple_read_rate"] = (
        f"{triple / translation_reads:.9f}" if translation_reads else "0.000000000"
    )
    optional_metadata = {
        "source_commit": args.source_commit,
        "compiler": args.compiler,
        "host_kernel": args.host_kernel,
        "guest_kernel": args.guest_kernel,
        "ssd_size": args.ssd_size,
        "cmt_entries": args.cmt_entries,
        "notes": args.notes,
    }
    for field, value in optional_metadata.items():
        if value:
            row[field] = value

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
    print(f"updated run_id={args.run_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
