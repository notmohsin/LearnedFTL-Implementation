#!/usr/bin/env python3
"""Replay a block trace against a raw device with O_DIRECT, QD1, in strict trace order.

Runs inside the FEMU guest. No page cache, readahead or merging can act on the
request stream (guest_prep.sh also disables merging and readahead), so every
trace line reaches FEMU as exactly one NVMe command.

Modes:
  replay   replay --trace (lines: "timestamp op offset_bytes length_bytes")
  fill     sequential writes covering the whole device (--block bytes each)
  random   --bytes of random, block-aligned overwrites (--block bytes each, --seed)
  warm     write every LPN the --trace touches once, in ascending LPN order, as
           runs of consecutive LPNs split at --block bytes (the trace's footprint)

Every written 4 KiB page carries a stamp (magic, lpn, version) so --verify can
check that reads return the most recent write made by this process.
Prints one JSON summary line on stdout.
"""

from __future__ import annotations

import argparse
import json
import mmap
import os
import random
import struct
import sys
import time
from pathlib import Path

PAGE = 4096
MAGIC = 0x46454D55  # "FEMU"
STAMP = struct.Struct("<IIQ")  # magic, version, lpn


def parse_trace(path: Path):
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            fields = line.strip().replace(",", " ").split()
            if not fields or fields[0].startswith("#"):
                continue
            if len(fields) != 4:
                raise ValueError(f"{path}:{number}: expected four fields")
            _, op, offset, length = fields
            op = op.upper()
            offset, length = int(offset), int(length)
            if op not in ("R", "W"):
                raise ValueError(f"{path}:{number}: op must be R or W")
            if offset % PAGE or length % PAGE or length <= 0:
                raise ValueError(f"{path}:{number}: request must be 4 KiB aligned")
            yield op, offset, length


class Device:
    def __init__(self, path: str, max_len: int, seed: int, verify: bool):
        self.fd = os.open(path, os.O_RDWR | os.O_DIRECT)
        self.size = os.lseek(self.fd, 0, os.SEEK_END)
        self.buf = mmap.mmap(-1, max_len)  # page aligned, as O_DIRECT needs
        rng = random.Random(seed)
        self.buf.write(bytes(rng.getrandbits(8) | 1 for _ in range(max_len)))  # non-zero
        self.view = memoryview(self.buf)
        self.verify = verify
        self.versions: dict[int, int] = {}
        self.counts = {"reads": 0, "writes": 0, "read_lpns": 0, "write_lpns": 0,
                       "verify_checked": 0, "verify_errors": 0}

    def write(self, offset: int, length: int) -> None:
        if offset + length > self.size:
            raise ValueError(f"write past end of device at {offset}")
        first = offset // PAGE
        for i in range(length // PAGE):
            lpn = first + i
            version = self.versions.get(lpn, 0) + 1
            self.versions[lpn] = version
            STAMP.pack_into(self.buf, i * PAGE, MAGIC, version, lpn)
        done = os.pwrite(self.fd, self.view[:length], offset)
        if done != length:
            raise IOError(f"short write at {offset}: {done}/{length}")
        self.counts["writes"] += 1
        self.counts["write_lpns"] += length // PAGE

    def read(self, offset: int, length: int) -> None:
        if offset + length > self.size:
            raise ValueError(f"read past end of device at {offset}")
        done = os.preadv(self.fd, [self.view[:length]], offset)
        if done != length:
            raise IOError(f"short read at {offset}: {done}/{length}")
        self.counts["reads"] += 1
        self.counts["read_lpns"] += length // PAGE
        if self.verify:
            first = offset // PAGE
            for i in range(length // PAGE):
                expected = self.versions.get(first + i)
                if expected is None:
                    continue  # never written by this process: content undefined
                magic, version, lpn = STAMP.unpack_from(self.buf, i * PAGE)
                self.counts["verify_checked"] += 1
                if (magic, version, lpn) != (MAGIC, expected, first + i):
                    self.counts["verify_errors"] += 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=("replay", "fill", "random", "warm"), default="replay")
    parser.add_argument("--trace", type=Path)
    parser.add_argument("--device", default="/dev/nvme0n1")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--block", type=int, default=1 << 20,
                        help="request size for fill/random modes (bytes)")
    parser.add_argument("--bytes", type=int, default=0,
                        help="total bytes for random mode")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()

    if args.block % PAGE:
        raise ValueError("--block must be a multiple of 4 KiB")
    max_len = args.block
    ops = None
    if args.mode in ("replay", "warm"):
        if not args.trace:
            parser.error("--trace is required for replay and warm")
        ops = list(parse_trace(args.trace))
        if args.mode == "replay":
            max_len = max(length for _, _, length in ops)
        else:
            lpns = sorted({lpn for _, offset, length in ops
                           for lpn in range(offset // PAGE, (offset + length) // PAGE)})
            per_req = args.block // PAGE
            ops = []
            i = 0
            while i < len(lpns):
                j = i
                while j + 1 < len(lpns) and lpns[j + 1] == lpns[j] + 1 and j + 1 - i < per_req:
                    j += 1
                ops.append(("W", lpns[i] * PAGE, (j - i + 1) * PAGE))
                i = j + 1

    dev = Device(args.device, max_len, args.seed, args.verify)
    start = time.monotonic()
    if args.mode in ("replay", "warm"):
        for op, offset, length in ops:
            if op == "W":
                dev.write(offset, length)
            else:
                dev.read(offset, length)
    elif args.mode == "fill":
        usable = dev.size - dev.size % args.block
        for offset in range(0, usable, args.block):
            dev.write(offset, args.block)
    else:
        rng = random.Random(args.seed)
        slots = dev.size // args.block
        for _ in range(args.bytes // args.block):
            dev.write(rng.randrange(slots) * args.block, args.block)
    elapsed = time.monotonic() - start
    os.close(dev.fd)

    summary = {"mode": args.mode, "trace": str(args.trace) if args.trace else None,
               "seed": args.seed, "device_bytes": dev.size, "elapsed_s": round(elapsed, 3),
               **dev.counts}
    print(json.dumps(summary, sort_keys=True))
    return 1 if dev.counts["verify_errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
