#!/usr/bin/env python3
"""Replay a timestamped block trace against a raw block device."""

from __future__ import annotations

import argparse
import os
from pathlib import Path


PAGE_SIZE = 4096


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--device", default="/dev/nvme0n1")
    parser.add_argument("--sync-every", type=int, default=0)
    args = parser.parse_args()
    if args.sync_every < 0:
        raise ValueError("sync-every cannot be negative")

    operations = 0
    reads = 0
    writes = 0
    with open(args.device, "r+b", buffering=0) as device:
        for line_number, line in enumerate(
            args.trace.open(encoding="utf-8"), 1
        ):
            fields = line.strip().replace(",", " ").split()
            if not fields or fields[0].startswith("#"):
                continue
            if len(fields) != 4:
                raise ValueError(f"{args.trace}:{line_number}: expected four fields")
            _, operation, offset_text, length_text = fields
            offset = int(offset_text)
            length = int(length_text)
            if offset % PAGE_SIZE or length % PAGE_SIZE:
                raise ValueError(
                    f"{args.trace}:{line_number}: requests must be 4 KiB aligned"
                )
            if operation.upper() == "W":
                device.seek(offset)
                device.write(b"\0" * length)
                writes += 1
            elif operation.upper() == "R":
                device.seek(offset)
                data = device.read(length)
                if len(data) != length:
                    raise IOError(f"short read at trace line {line_number}")
                reads += 1
            else:
                raise ValueError(f"{args.trace}:{line_number}: operation must be R or W")
            operations += 1
            if args.sync_every and operations % args.sync_every == 0:
                os.fsync(device.fileno())
        os.fsync(device.fileno())
    print(f"operations={operations} reads={reads} writes={writes}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
