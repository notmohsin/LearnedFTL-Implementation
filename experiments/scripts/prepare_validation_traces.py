#!/usr/bin/env python3
"""Create fixed-size prefixes of generated traces for safe validation runs."""

from __future__ import annotations

import argparse
from pathlib import Path


def prepare_trace(source: Path, destination: Path, operations: int) -> int:
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
    return copied


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--operations", type=int, default=100_000)
    parser.add_argument("--skip-short", action="store_true",
                        help="skip (and report) sources with fewer operations instead of failing")
    args = parser.parse_args()
    if args.operations <= 0:
        raise ValueError("operations must be positive")

    sources = sorted(args.source_dir.glob("*.trace"))
    if not sources:
        raise FileNotFoundError(f"no traces found in {args.source_dir}")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for source in sources:
        destination = args.output_dir / source.name
        copied = prepare_trace(source, destination, args.operations)
        if copied != args.operations:
            destination.unlink()
            if not args.skip_short:
                raise ValueError(f"{source}: expected {args.operations} operations, found {copied}")
            print(f"{source.name}: only {copied} operations, skipped")
            continue
        print(f"{source.name}: {args.operations} operations -> {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
