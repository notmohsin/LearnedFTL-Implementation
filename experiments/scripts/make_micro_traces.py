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
        # KI-27: the zero-initialised buffer makes LPN 0 look buffered, so the
        # whole first request (LPNs 0-15) is dropped; LPNs 16-255 fill slots
        # 0-239 and the slot-0 read hit is LPN 16.
        "leaftl": {"buffer_hits": ["==", 1], "served_lpns": ["==", 0], "mapped_read_lpns": ["==", 1],
                   "buffer_flushes": ["==", 0], "unmapped_read_lpns": ["==", reads - 1],
                   "writes_dropped": ["==", 16], "writes_buffered": ["==", 240]},
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
        # KI-27: the first request (LPNs 0-63) is dropped (LPN 0 matches the
        # zero-initialised buffer), so 2048 buffered LPNs are reached at LPN
        # 2111 (one flush); LPNs 1000-1015 were flushed, then overwritten.
        "leaftl": {"buffer_flushes": ["==", 1], "writes_dropped": ["==", 64],
                   "stale_map_entries": ["==", 16], "served_stale_page": ["==", 16],
                   "mapped_read_lpns": ["==", 16]},
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
        "leaftl": {"writes_dropped": ["==", 1]},  # the write to LPN 0 (KI-27)
    }

    # 5. Read cost after one LeaFTL flush. Write 2048 consecutive LPNs (4096..6143, 32 x 256 KiB; LPN 0 is
    #    avoided because of KI-27), then read LPNs 4096..4111 twice (two 64 KiB requests).
    #    LeaFTL: the 2048th LPN fills the buffer: one flush. quick_sort sorts 2049 slots (KI-30): the hidden
    #    slot holds 0, so the flush maps LPN 0 and 4096..6142 and leaves 6143 in the hidden slot. Each of the
    #    8 translation pages (16..23) gets one accurate segment, the first in its group, so it is stored.
    #    Read 1: LPN 4096 misses the CMT, reads translation page 16 (1 flash read), the model finds the
    #    accurate segment (model hit), the segment is cached, the data page is read (1 flash read): 2 reads.
    #    LPNs 4097..4111 hit the cached segment: 1 flash read each. Read 2: 16 hits, 1 flash read each.
    #    Total: 1 miss, 31 hits, 33 flash reads, 31 of 32 served LPNs translation-free. (This assumes the
    #    segment predicts the right physical page; a wrong prediction would add a flash read and show here.)
    #    LearnedFTL: every write to a not-yet-mapped translation page is a write-path CMT miss that inserts
    #    one entry (process_translation_page_write, no prefetch): 2048 misses, 2048 entries (capacity 8192).
    #    All 32 read LPNs hit the CMT: 1 flash read each.
    ops = [("W", 4096 + 64 * i, 64) for i in range(32)] + [("R", 4096, 16), ("R", 4096, 16)]
    traces["flush-then-read"] = ops
    expected["flush-then-read"] = {
        "both": {"read_lpns": ["==", 32], "write_lpns": ["==", 2048], "mapped_read_lpns": ["==", 32],
                 "unmapped_read_lpns": ["==", 0], "served_lpns": ["==", 32]},
        "leaftl": {"buffer_flushes": ["==", 1], "writes_buffered": ["==", 2048], "writes_dropped": ["==", 0],
                   "cmt_misses": ["==", 1], "cmt_hits": ["==", 31], "model_queries": ["==", 1],
                   "model_hits": ["==", 1], "model_misses": ["==", 0], "served_cmt_hits": ["==", 31],
                   "served_translation_free": ["==", 31], "served_reads_hist": ["==", [0, 31, 1, 0, 0]],
                   "served_flash_reads": ["==", 33], "served_tp_reads": ["==", 1]},
        "learnedftl": {"cmt_write_misses": ["==", 2048], "cmt_write_hits": ["==", 0], "cmt_hits": ["==", 32],
                       "cmt_misses": ["==", 0], "served_cmt_hits": ["==", 32],
                       "served_reads_hist": ["==", [0, 32, 0, 0, 0]], "served_flash_reads": ["==", 32]},
    }

    # 6. A LearnedFTL CMT miss, and two LPNs LeaFTL cannot find. Write 16384 consecutive LPNs (4096..20479,
    #    256 x 256 KiB), then read LPN 4096 twice (two 4 KiB requests), then LPNs 6142..6143 (one 8 KiB request).
    #    LearnedFTL: 16384 write-path misses insert 16384 entries into a CMT of 8192, so the 8192 oldest
    #    (LPNs 4096..12287, translation pages 8..23) are evicted; a node's first (dirty) eviction writes its
    #    translation page, so pages 8..23 are mapped. Read 1 of LPN 4096: CMT miss, translation page read,
    #    data read = 2 flash reads. (The eviction it causes takes a dirty entry of page 24, which was never
    #    written before: no extra read.) Read 2: CMT hit, 1 flash read. LPN 6142: miss, 2 flash reads; the
    #    miss prefetches the rest of the request inside the translation page, LPN 6143: hit, 1 flash read.
    #    Total: 2 misses, 2 hits, 6 flash reads, 2 translation-page reads.
    #    LeaFTL: 8 flushes. Flush 1 is as in trace 5: LPN 4096 is read as there (2 flash reads, then 1).
    #    LPN 6142 was mapped by flush 1, but the last LPN of a flush is never put in a segment (KI-9).
    #    LPN 6143 was held back by flush 1 (KI-30) and mapped by flush 2 as a one-LPN segment for
    #    translation page 23, whose top level then holds exactly one segment, so the new one is dropped
    #    (KI-28). Both reads miss the CMT, read translation page 23 (1 flash read each), get a model miss
    #    and return nothing although the data is mapped: 2 x invalid_but_mapped, 2 wasted flash reads.
    ops = [("W", 4096 + 64 * i, 64) for i in range(256)] + [("R", 4096, 1), ("R", 4096, 1), ("R", 6142, 2)]
    traces["evict-then-read"] = ops
    expected["evict-then-read"] = {
        "both": {"read_lpns": ["==", 4], "write_lpns": ["==", 16384]},
        "leaftl": {"buffer_flushes": ["==", 8], "writes_buffered": ["==", 16384], "writes_dropped": ["==", 0],
                   "cmt_misses": ["==", 3], "cmt_hits": ["==", 1], "model_queries": ["==", 3],
                   "model_hits": ["==", 1], "model_misses": ["==", 2], "invalid_but_mapped": ["==", 2],
                   "served_lpns": ["==", 2], "served_reads_hist": ["==", [0, 1, 1, 0, 0]],
                   "served_flash_reads": ["==", 3], "unmapped_flash_reads": ["==", 2],
                   "unmapped_read_lpns": ["==", 2], "crb_match_discarded": ["==", 0]},
        "learnedftl": {"cmt_write_misses": ["==", 16384], "cmt_write_hits": ["==", 0],
                       "cmt_misses": ["==", 2], "cmt_hits": ["==", 2], "served_lpns": ["==", 4],
                       "served_cmt_hits": ["==", 2], "served_translation_free": ["==", 2],
                       "served_reads_hist": ["==", [0, 2, 2, 0, 0]], "served_flash_reads": ["==", 6],
                       "served_tp_reads": ["==", 2], "model_hits": ["==", 0], "unmapped_read_lpns": ["==", 0]},
    }

    OUT.mkdir(parents=True, exist_ok=True)
    for name, ops in traces.items():
        (OUT / f"{name}.trace").write_text(trace_lines(ops))
    (OUT / "expected.json").write_text(json.dumps(expected, indent=2) + "\n")
    print(f"wrote {len(traces)} micro-traces to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
