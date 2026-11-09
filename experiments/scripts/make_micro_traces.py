#!/usr/bin/env python3
"""Write the hand-checked micro-traces to experiments/traces/micro/.

Each trace is a few dozen requests whose effect on each FTL can be worked out
by hand from the authors' code (see the derivation next to each expectation
below and experiments/docs/METRICS.md). expected.json holds, per trace and
FTL, conditions on the workload record that check_micro.py tests after
run_femu.py --precondition none. Deterministic: rerunning rewrites identical files.
"""

from __future__ import annotations

import json
from pathlib import Path

PAGE = 4096
OUT = Path(__file__).resolve().parents[1] / "traces" / "micro"


def trace_lines(ops):
    lines = ["# timestamp operation offset_bytes length_bytes"]
    for i, (op, lpn, nlpn) in enumerate(ops):
        lines.append(f"{i / 1e6:.6f} {op} {lpn * PAGE} {nlpn * PAGE}")
    return "\n".join(lines) + "\n"


def main() -> int:
    traces, expected = {}, {}

    # 1. Write 256 LPNs (16 x 64 KiB), read each back with 4 KiB reads.
    #    LearnedFTL updates its page map and inserts into the CMT on every
    #    write (KI-14), so all 256 reads are CMT hits served with one flash
    #    read and no translation read.
    #    LeaFTL holds writes in a 2048-LPN buffer; nothing is flushed after 256
    #    LPNs, so the page map is still empty. Only a read matching buffer
    #    slot 0 (LPN 0, the first one written) is found (KI-5); the other 255
    #    written LPNs are not returned.
    ops = [("W", 16 * i, 16) for i in range(16)] + [("R", lpn, 1) for lpn in range(0, 256, 8)]
    traces["write-read-buffered"] = ops
    reads = 32
    expected["write-read-buffered"] = {
        "both": {"read_lpns": ["==", reads], "write_lpns": ["==", 256]},
        "learnedftl": {"mapped_read_lpns": ["==", reads], "served_cmt_hits": ["==", reads],
                       "served_translation_free": ["==", reads], "served_flash_reads": ["==", reads],
                       "unmapped_read_lpns": ["==", 0]},
        "leaftl": {"buffer_hits": ["==", 1], "served_lpns": ["==", 0], "mapped_read_lpns": ["==", 1],
                   "buffer_flushes": ["==", 0], "unmapped_read_lpns": ["==", reads - 1]},
    }

    # 2. Reads before any write, one write, then reads of never-written LPNs.
    #    LeaFTL skips reads before its first write (KI-11: pre_first_write).
    #    Neither FTL may return data for never-written LPNs.
    ops = [("R", 5000 + 4 * i, 1) for i in range(8)] + [("W", 1_000_000, 1)] + \
          [("R", 200_000 + 3 * i, 1) for i in range(24)]
    traces["read-unwritten"] = ops
    expected["read-unwritten"] = {
        "both": {"read_lpns": ["==", 32], "mapped_read_lpns": ["==", 0], "unmapped_read_lpns": ["==", 32],
                 "served_lpns": ["==", 0]},
        "leaftl": {"pre_first_write": ["==", 8]},
        "learnedftl": {"unmapped_flash_reads": ["==", 0]},
    }

    # 3. Write 4096 LPNs (two full LeaFTL buffers -> two flushes, page map and
    #    translation pages written), overwrite 16 of them, read those 16.
    #    LeaFTL invalidates the old pages at write time but only updates its
    #    page map at the next flush, so the 16 page-map entries point at
    #    invalidated pages (stale_map_entries) and reads are served from
    #    stale pages, except an LPN in buffer slot 0. LearnedFTL updates its
    #    map at write time: no stale entries or stale reads.
    ops = [("W", 64 * i, 64) for i in range(64)] + [("W", 1000 + i, 1) for i in range(16)] + \
          [("R", 1000 + i, 1) for i in range(16)]
    traces["overwrite-then-read"] = ops
    expected["overwrite-then-read"] = {
        "both": {"read_lpns": ["==", 16], "write_lpns": ["==", 4096 + 16]},
        "leaftl": {"buffer_flushes": ["==", 2], "stale_map_entries": ["==", 16],
                   "served_stale_page": [">=", 15], "mapped_read_lpns": ["==", 16]},
        "learnedftl": {"stale_map_entries": ["==", 0], "served_stale_page": ["==", 0],
                       "mapped_read_lpns": ["==", 16]},
    }

    # 4. Request sizes at the histogram bucket edges (1, 2-3, 4-7, 8-15,
    #    16-31, 32-63, 64+ LPNs); validate_run.py checks both histograms
    #    against the trace exactly.
    sizes = [1, 2, 3, 4, 7, 8, 15, 16, 31, 32, 63, 64, 65]
    ops, lpn = [], 0
    for n in sizes:
        ops.append(("W", lpn, n))
        lpn += n
    lpn = 0
    for n in sizes:
        ops.append(("R", lpn, n))
        lpn += n
    traces["size-buckets"] = ops
    expected["size-buckets"] = {
        "both": {"read_size_hist": ["==", [1, 2, 2, 2, 2, 2, 2]],
                 "write_size_hist": ["==", [1, 2, 2, 2, 2, 2, 2]]},
    }

    OUT.mkdir(parents=True, exist_ok=True)
    for name, ops in traces.items():
        (OUT / f"{name}.trace").write_text(trace_lines(ops))
    (OUT / "expected.json").write_text(json.dumps(expected, indent=2) + "\n")
    print(f"wrote {len(traces)} micro-traces to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
