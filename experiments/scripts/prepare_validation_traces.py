#!/usr/bin/env python3
"""Create fixed-size prefixes of generated traces for safe validation runs."""

from __future__ import annotations

import argparse
from pathlib import Path


def prepare_trace(source: Path, destination: Path, operations: int) -> None:
    copied = 0
    with source.open(encoding="utf-8") as input_file, destination.open(
        "w", encoding="utf-8"
    ) as output_file:
        for line in input_file:
            if line.startswith("#"):
                output_file.write(line)
                continue
            if not line.strip():
                continue
            if copied >= operations:
                break
            output_file.write(line)
            copied += 1
    if copied != operations:
        raise ValueError(f"{source}: expected {operations} operations, found {copied}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--operations", type=int, default=100_000)
    args = parser.parse_args()
    if args.operations <= 0:
        raise ValueError("operations must be positive")

    sources = sorted(args.source_dir.glob("*.trace"))
    if not sources:
        raise FileNotFoundError(f"no traces found in {args.source_dir}")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for source in sources:
        destination = args.output_dir / source.name
        prepare_trace(source, destination, args.operations)
        print(f"{source.name}: {args.operations} operations -> {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
