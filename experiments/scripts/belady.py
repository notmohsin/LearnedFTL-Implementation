#!/usr/bin/env python3
"""Simulate Belady's optimal CMT replacement policy for a workload trace.

Trace format:
    timestamp operation offset_bytes length_bytes

Reads are expanded to 4 KiB logical pages. Each page is converted to the
LeaFTL translation-page number (TVPN), which is the CMT key:

    TVPN = logical_page_number // 256

The same reference stream can be used to compare LeaFTL and LearnedFTL
replacement behavior. Writes establish device state but do not access the CMT
and are therefore excluded from the Belady reference stream.
"""

from __future__ import annotations

import argparse
import csv
import heapq
import os
import tempfile
from array import array
from pathlib import Path
from typing import Iterable


PAGE_SIZE = 4096
ENTRIES_PER_TRANSLATION_PAGE = 256
RESULT_COLUMNS = (
    "belady_capacity",
    "belady_references",
    "belady_hits",
    "belady_misses",
    "belady_hit_rate",
)


def parse_trace(path: Path) -> Iterable[tuple[str, str, int, int]]:
    with path.open(encoding="utf-8") as trace:
        for line_number, line in enumerate(trace, 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            fields = stripped.replace(",", " ").split()
            if len(fields) != 4:
                raise ValueError(
                    f"{path}:{line_number}: expected "
                    "timestamp operation offset length"
                )
            timestamp, operation = fields[0], fields[1].upper()
            try:
                offset = int(fields[2])
                length = int(fields[3])
            except ValueError as exc:
                raise ValueError(
                    f"{path}:{line_number}: offset and length must be integers"
                ) from exc
            if operation not in {"R", "W"}:
                raise ValueError(
                    f"{path}:{line_number}: operation must be R or W"
                )
            if offset < 0 or length <= 0:
                raise ValueError(
                    f"{path}:{line_number}: offset must be non-negative and "
                    "length must be positive"
                )
            yield timestamp, operation, offset, length


def tvpn_references(path: Path) -> list[int]:
    references: list[int] = []
    for _, operation, offset, length in parse_trace(path):
        if operation != "R":
            continue
        first_page = offset // PAGE_SIZE
        last_page = (offset + length - 1) // PAGE_SIZE
        references.extend(
            page // ENTRIES_PER_TRANSLATION_PAGE
            for page in range(first_page, last_page + 1)
        )
    return references


def belady_stats(references: list[int], capacity: int) -> tuple[int, int]:
    if capacity <= 0:
        raise ValueError("capacity must be positive")

    reference_count = len(references)
    infinity = reference_count
    next_use = array("q", [infinity]) * reference_count
    last_seen: dict[int, int] = {}
    for position in range(reference_count - 1, -1, -1):
        reference = references[position]
        next_use[position] = last_seen.get(reference, infinity)
        last_seen[reference] = position

    cache: dict[int, int] = {}
    eviction_heap: list[tuple[int, int]] = []
    hits = 0
    for position, reference in enumerate(references):
        if reference in cache:
            hits += 1
        elif len(cache) >= capacity:
            while True:
                neg_next, victim = heapq.heappop(eviction_heap)
                if cache.get(victim) == -neg_next:
                    del cache[victim]
                    break
        cache[reference] = next_use[position]
        heapq.heappush(eviction_heap, (-next_use[position], reference))

    return hits, reference_count - hits


def update_csv(
    csv_path: Path,
    run_id: str | None,
    result: dict[str, str],
) -> None:
    with csv_path.open(newline="", encoding="utf-8") as csv_file:
        reader = csv.DictReader(csv_file)
        rows = list(reader)
        existing_fields = reader.fieldnames or []

    if not rows:
        raise ValueError(f"{csv_path} does not contain any data rows")
    if run_id is None:
        if len(rows) != 1:
            raise ValueError("--run-id is required when the CSV has multiple rows")
        target = rows[0]
    else:
        matching = [row for row in rows if row.get("run_id") == run_id]
        if len(matching) != 1:
            raise ValueError(f"expected exactly one CSV row with run_id={run_id}")
        target = matching[0]

    fields = list(existing_fields)
    for column in RESULT_COLUMNS:
        if column not in fields:
            fields.append(column)
    target.update(result)

    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{csv_path.name}.", suffix=".tmp", dir=csv_path.parent
    )
    os.close(fd)
    temporary_path = Path(temporary_name)
    try:
        with temporary_path.open("w", newline="", encoding="utf-8") as output:
            writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        temporary_path.replace(csv_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument(
        "--csv",
        type=Path,
        default=Path(__file__).parents[1] / "results" / "leaftl_runs.csv",
    )
    parser.add_argument("--run-id")
    parser.add_argument("--capacity", type=int, default=8192)
    parser.add_argument(
        "--no-csv-update",
        action="store_true",
        help="print results without modifying the results CSV",
    )
    args = parser.parse_args()

    references = tvpn_references(args.trace)
    hits, misses = belady_stats(references, args.capacity)
    result = {
        "belady_capacity": str(args.capacity),
        "belady_references": str(len(references)),
        "belady_hits": str(hits),
        "belady_misses": str(misses),
        "belady_hit_rate": f"{hits / len(references):.9f}" if references else "0.000000000",
    }

    if not args.no_csv_update:
        update_csv(args.csv, args.run_id, result)

    print(",".join(f"{key}={value}" for key, value in result.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
