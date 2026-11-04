#!/usr/bin/env python3
"""Generate a reproducible workload suite and Belady summaries.

The default suite is intentionally bounded by a total trace-size budget.
Every trace uses:

    timestamp operation offset_bytes length_bytes

The manifest records the generator parameters and Belady's optimal CMT
statistics for the same read reference stream.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import json
import random
import re
from pathlib import Path

from belady import belady_stats, tvpn_references


PAGE_SIZE = 4096
REQUEST_SIZES = (4096, 8192, 16384, 65536)
DEFAULT_WORKLOADS = (
    "seq-mixed",
    "uniform-read",
    "uniform-write",
    "uniform-mixed",
    "zipf-hot20-access80-a1.2",
    "zipf-hot10-access90-a1.2",
    "zipf-hot20-access80-a0.8",
    "mixed-50-seq-50-random",
)
MANIFEST_FIELDS = (
    "workload",
    "trace_file",
    "trace_sha256",
    "operations",
    "address_range_gib",
    "request_sizes",
    "read_percent",
    "write_percent",
    "seed",
    "pattern",
    "hot_region_percent",
    "hot_access_percent",
    "zipf_alpha",
    "sequential_percent",
    "random_percent",
    "belady_capacity",
    "belady_references",
    "belady_hits",
    "belady_misses",
    "belady_hit_rate",
    "bytes_written",
)


def choose_zipf_page(
    rng: random.Random, cumulative: list[float], total: float
) -> int:
    return bisect.bisect_left(cumulative, rng.random() * total)


def workload_config(name: str) -> dict[str, float | str]:
    if name == "seq-mixed":
        return {"pattern": "sequential", "read_percent": 50.0}
    if name == "uniform-read":
        return {"pattern": "uniform", "read_percent": 100.0}
    if name == "uniform-write":
        return {"pattern": "uniform", "read_percent": 0.0}
    if name == "uniform-mixed":
        return {"pattern": "uniform", "read_percent": 50.0}
    if name == "mixed-50-seq-50-random":
        return {
            "pattern": "mixed",
            "read_percent": 50.0,
            "sequential_percent": 50.0,
            "random_percent": 50.0,
        }
    match = re.fullmatch(
        r"zipf-hot(?P<hot>\d+)-access(?P<access>\d+)-a(?P<alpha>[0-9.]+)",
        name,
    )
    if match:
        hot = match.group("hot")
        access = match.group("access")
        alpha = float(match.group("alpha"))
        return {
            "pattern": "zipf",
            "read_percent": 50.0,
            "hot_region_percent": float(hot),
            "hot_access_percent": float(access),
            "zipf_alpha": alpha,
        }
    raise ValueError(f"unknown workload: {name}")


def generate_trace(
    path: Path,
    name: str,
    operations: int,
    total_pages: int,
    seed: int,
) -> dict[str, str]:
    config = workload_config(name)
    rng = random.Random(seed)
    page = 0
    hot_pages = max(1, int(total_pages * float(config.get("hot_region_percent", 20)) / 100))
    cold_start = hot_pages
    cumulative: list[float] = []
    zipf_total = 0.0
    alpha = float(config.get("zipf_alpha", 1.2))
    for rank in range(1, hot_pages + 1):
        zipf_total += rank ** -alpha
        cumulative.append(zipf_total)

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as output:
        output.write("# timestamp operation offset_bytes length_bytes\n")
        for index in range(operations):
            pattern = str(config["pattern"])
            if pattern == "sequential":
                start = page
                page = (page + 16) % total_pages
            elif pattern == "uniform":
                start = rng.randrange(total_pages)
            elif pattern == "zipf":
                if rng.random() < float(config["hot_access_percent"]) / 100:
                    start = choose_zipf_page(rng, cumulative, zipf_total)
                else:
                    start = cold_start + rng.randrange(total_pages - cold_start)
            else:
                if rng.random() < 0.5:
                    start = page
                    page = (page + 16) % total_pages
                else:
                    start = rng.randrange(total_pages)

            size = rng.choice(REQUEST_SIZES)
            start = min(start, total_pages - size // PAGE_SIZE)
            operation = "R" if rng.random() < float(config["read_percent"]) / 100 else "W"
            output.write(f"{index * 0.000001:.6f} {operation} {start * PAGE_SIZE} {size}\n")

    references = tvpn_references(path)
    hits, misses = belady_stats(references, 8192)
    return {
        "workload": name,
        "trace_file": str(path),
        "trace_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "operations": str(operations),
        "address_range_gib": "",
        "request_sizes": "4096,8192,16384,65536",
        "read_percent": str(config["read_percent"]),
        "write_percent": str(100 - float(config["read_percent"])),
        "seed": str(seed),
        "pattern": str(config["pattern"]),
        "hot_region_percent": str(config.get("hot_region_percent", "")),
        "hot_access_percent": str(config.get("hot_access_percent", "")),
        "zipf_alpha": str(config.get("zipf_alpha", "")),
        "sequential_percent": str(config.get("sequential_percent", "")),
        "random_percent": str(config.get("random_percent", "")),
        "belady_capacity": "8192",
        "belady_references": str(len(references)),
        "belady_hits": str(hits),
        "belady_misses": str(misses),
        "belady_hit_rate": f"{hits / len(references):.9f}" if references else "0.000000000",
        "bytes_written": str(path.stat().st_size),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operations", type=int, default=10_000_000)
    parser.add_argument("--range-gib", type=float, default=25.0)
    parser.add_argument("--budget-gib", type=float, default=3.0)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).parents[1] / "traces")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--workload", action="append", dest="workloads")
    args = parser.parse_args()
    if args.operations <= 0 or args.range_gib <= 0 or args.budget_gib <= 0:
        raise ValueError("operations, range-gib, and budget-gib must be positive")

    workloads = args.workloads or list(DEFAULT_WORKLOADS)
    manifest_path = args.manifest or args.output_dir / "manifest.csv"
    total_pages = int(args.range_gib * (1024**3) // PAGE_SIZE)
    budget = int(args.budget_gib * 1024**3)
    rows: list[dict[str, str]] = []
    used = 0
    generated_paths: list[Path] = []
    for index, name in enumerate(workloads):
        path = args.output_dir / f"{name}.trace"
        row = generate_trace(path, name, args.operations, total_pages, args.seed + index)
        used += int(row["bytes_written"])
        if used > budget:
            for generated_path in generated_paths + [path]:
                generated_path.unlink(missing_ok=True)
            raise RuntimeError(
                f"trace budget exceeded after {name}: {used} bytes > {budget} bytes"
            )
        generated_paths.append(path)
        row["address_range_gib"] = str(args.range_gib)
        rows.append(row)

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=MANIFEST_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    metadata = manifest_path.with_suffix(".json")
    metadata.write_text(json.dumps({
        "operations_per_trace": args.operations,
        "address_range_gib": args.range_gib,
        "budget_gib": args.budget_gib,
        "base_seed": args.seed,
        "request_sizes": REQUEST_SIZES,
        "workloads": workloads,
        "total_bytes": used,
    }, indent=2) + "\n", encoding="utf-8")
    print(f"generated={len(rows)} total_bytes={used} manifest={manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
