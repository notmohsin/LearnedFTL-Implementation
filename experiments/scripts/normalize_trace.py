#!/usr/bin/env python3
"""Normalise an SPC-format block trace (UMass Financial / WebSearch) for FEMU replay.

    normalize_trace.py SOURCE.spc[.bz2] OUTPUT.trace [--ops N] [--chunk-bytes B]
                       [--device-bytes B] [--source-url URL]

SPC records are "ASU,LBA,size_bytes,op,timestamp" with LBA in 512-byte sectors
and one address space per ASU (application storage unit). The output is the
repo's trace format ("timestamp op offset_bytes length_bytes"):

1. The first --ops records are kept (all if 0).
2. Each request is widened to the 4 KiB pages it touches (offset rounded down,
   end rounded up): the FTLs map whole 4 KiB pages.
3. The distinct (ASU, chunk) pairs touched, with chunk = --chunk-bytes
   (default 2 MiB = one LearnedFTL translation page = two LeaFTL ones), are
   packed densely in (ASU, address) order into the device. Within a chunk
   offsets are unchanged, and consecutive touched chunks of an ASU stay
   adjacent, so translation-page grouping and sequential runs are preserved;
   only untouched address space is removed.

A sidecar OUTPUT.json records the source, its SHA-256, the parameters and how
much was changed. Fails if the packed footprint does not fit the device.
"""

from __future__ import annotations

import argparse
import bz2
import hashlib
import json
import sys
from pathlib import Path

PAGE = 4096


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def records(path: Path, limit: int):
    opener = bz2.open if path.suffix == ".bz2" else open
    count = 0
    with opener(path, "rt", encoding="ascii", errors="replace") as handle:
        for line in handle:
            fields = line.strip().split(",")
            if len(fields) < 5:
                continue
            try:
                asu, lba, size, op, ts = int(fields[0]), int(fields[1]), int(fields[2]), fields[3].strip().upper(), float(fields[4])
            except ValueError:
                continue
            if op not in ("R", "W") or size <= 0:
                continue
            yield asu, lba * 512, size, op, ts
            count += 1
            if limit and count >= limit:
                return


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--ops", type=int, default=1_000_000)
    parser.add_argument("--chunk-bytes", type=int, default=2 << 20)
    parser.add_argument("--device-bytes", type=int, default=30 << 30)
    parser.add_argument("--source-url", default="")
    args = parser.parse_args()
    chunk = args.chunk_bytes
    if chunk % PAGE:
        sys.exit("--chunk-bytes must be a multiple of 4 KiB")

    reqs = []
    keys = set()
    widened = 0
    for asu, offset, size, op, ts in records(args.source, args.ops):
        start = offset // PAGE * PAGE
        end = -(-(offset + size) // PAGE) * PAGE
        widened += (start != offset) or (end - start != size)
        reqs.append((ts, op, asu, start, end - start))
        for c in range(start // chunk, (end - 1) // chunk + 1):
            keys.add((asu, c))
    slots = {key: i for i, key in enumerate(sorted(keys))}
    footprint = len(slots) * chunk
    if footprint > args.device_bytes:
        sys.exit(f"packed footprint {footprint / 2**30:.2f} GiB exceeds the device")

    t0 = reqs[0][0] if reqs else 0.0
    reads = 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w") as out:
        out.write("# timestamp operation offset_bytes length_bytes\n")
        for ts, op, asu, start, length in reqs:
            new = slots[(asu, start // chunk)] * chunk + start % chunk
            out.write(f"{ts - t0:.6f} {op} {new} {length}\n")
            reads += op == "R"
    meta = {
        "source": str(args.source.name), "source_url": args.source_url, "source_sha256": sha256(args.source),
        "format": "SPC (ASU,LBA[512 B],size,op,timestamp)", "records_kept": len(reqs), "ops_limit": args.ops,
        "read_percent": round(100 * reads / len(reqs), 3) if reqs else None,
        "requests_widened_to_4k": widened, "chunk_bytes": chunk, "asus": len({a for a, _ in keys}),
        "chunks_touched": len(slots), "packed_footprint_gib": round(footprint / 2**30, 3),
        "normalisation": "4 KiB alignment by widening; (ASU, chunk) pairs packed densely in (ASU, address) order; "
                         "offsets within a chunk unchanged",
        "output_sha256": sha256(args.output), "generator": "experiments/scripts/normalize_trace.py",
    }
    args.output.with_suffix(".json").write_text(json.dumps(meta, indent=2) + "\n")
    print(json.dumps(meta))
    return 0


if __name__ == "__main__":
    sys.exit(main())
