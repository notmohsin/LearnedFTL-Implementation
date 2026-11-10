#!/usr/bin/env python3
"""Offline replica of LeaFTL's mapping logic, driven by a run's FEMU request log.

    leaftl_replica.py RUN_DIR [--fix insert,sort,last,drop] [--quiet]

Purpose: isolate the cause of KI-28 (reads of written LPNs that the learned lookup cannot resolve) without
changing the authors' code. The replica re-implements, line for line, the parts of hw/femu/bbssd/leaftl.c that
decide whether a read LPN can be resolved:

  ssd_write             buffer insertion, the carried-over `update_in_buffer` flag and the 2048-slot scan (KI-27),
                        compact() every 1,000,000 written LPNs of an interval
  buffer_flush          quick_sort(buffer_lpns, 0, buffer_size) with an inclusive upper bound, i.e. 2049 slots;
                        maptbl/GTD updates (LPN-0 sentinel, KI-10); segmentation (threshold 10, TRAIN_LEN 256,
                        group boundary, last LPN excluded: KI-9)
  regression            accuracy flag (consecutive LPNs) and the conflict-resolution buffer (CRB) of an
                        inaccurate segment
  segment_insert_simple_segment / level_seg_mgmt_insert   sorted insert into the top level, overlap -> new level
  all_seg_get_ppn       level-by-level search; CRB search over every CRB of the group
  process_translation_page_read / cmt_hit   tvpn-keyed LRU CMT of 8192 entries; only accurate lookups inserted
  compact / get_valid_lpns / delete_seg

PPN values are not modelled (they decide only whether a served read needs an extra flash read), so the replica
predicts the counters that classify read LPNs: model_queries/hits/corrections/misses, cmt_hits/misses,
unmapped_tp, invalid_read_cnt, invalid_but_mapped, buffer_hits, pre_first_write, writes_*, buffer_flushes.
Runs with GC are refused (GC_flush and do_gc are not modelled).

The slot past the end of buffer_lpns: g_malloc0(2048 * 8) returns a glibc chunk whose usable size is 16,392
bytes, so element 2048 is the next chunk's prev_size field, which malloc never touches while this chunk is in
use. It starts at 0 (calloc) and afterwards holds whatever the sort left there. The replica models it as a
private 2049th slot; an exact match with FEMU's counters is the evidence that this model is right.

--fix switches off individual bugs (counterfactuals, to attribute the misses):
  insert  a second segment is inserted when the top level holds exactly one segment
  sort    sort the 2048 buffer slots only
  last    the last LPN of a flush is included in its segment
  drop    `update_in_buffer` reset per LPN and only live buffer slots searched
  compact get_valid_lpns marks CRB LPNs valid (the authors' code writes 0) and includes the last LPN of an
          accurate segment, so compact() deletes only levels that are fully shadowed
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from collections import Counter, OrderedDict
from pathlib import Path

TT_PGS = 8388608
ENTS_PER_TP = 256          # spp->ents_per_pg and sp->mapping_per_page
BUFFER = 2048
THRESHOLD = 10
TRAIN_LEN = 256
CMT_ENTRIES = 8192
SECS_PER_PG = 8
INVALID = "INVALID"        # stands for INVALID_VAL


class Seg:
    __slots__ = ("slpa", "len", "accurate", "crb")

    def __init__(self, lpns: list[int], accurate: bool):
        group_start = (lpns[0] // ENTS_PER_TP) * ENTS_PER_TP
        self.slpa = lpns[0] - group_start
        self.len = lpns[-1] - lpns[0]
        self.accurate = accurate
        self.crb = None

    @property
    def lb(self):
        return self.slpa

    @property
    def ub(self):
        return self.slpa + self.len


class Replica:
    def __init__(self, fix: set[str]):
        self.fix = fix
        self.buf = [0] * (BUFFER + 1)          # slot BUFFER is the one quick_sort reaches past the end
        self.ptr = 0
        self.start_write = False
        self.mapped = bytearray(TT_PGS)        # maptbl entry ever set (no GC in modelled runs)
        self.gtd = bytearray(TT_PGS // ENTS_PER_TP)
        self.levels: dict[int, list[list[Seg]]] = {}     # group -> levels, top first
        self.crbs: dict[int, list[Seg]] = {}             # group -> inaccurate segments with a CRB, head first
        self.crb_count: dict[int, Counter] = {}          # group -> LPN multiplicity over its CRBs
        self.cmt: OrderedDict[int, None] = OrderedDict()  # MRU at the end
        self.vppn = "garbage"                  # ssd_read's uninitialised local; see read()
        self.lost_segments = 0
        self.total_segments = 0
        self.reset()

    def reset(self):
        self.c = Counter()
        self.write_cnt = 0

    # ---- write path ---------------------------------------------------------------------------------
    def write(self, start: int, end: int):
        self.start_write = True
        c = self.c
        c["write_reqs"] += 1
        update_in_buffer = False
        for lpn in range(start, end + 1):
            self.write_cnt += 1
            c["write_lpns"] += 1
            if self.write_cnt % 1000000 == 0:
                self.compact()
            if "drop" in self.fix:
                update_in_buffer = lpn in self.buf[:self.ptr]
            elif not update_in_buffer:
                update_in_buffer = lpn in self.buf[:BUFFER]
            if update_in_buffer:
                c["writes_absorbed" if lpn in self.buf[:self.ptr] else "writes_dropped"] += 1
                continue
            c["writes_buffered"] += 1
            if self.ptr == BUFFER - 1:
                self.buf[self.ptr] = lpn
                self.flush()
                self.ptr = 0
            else:
                self.buf[self.ptr] = lpn
                self.ptr += 1

    def flush(self):
        self.c["buffer_flushes"] += 1
        if "sort" in self.fix:
            self.buf[:BUFFER] = sorted(self.buf[:BUFFER])
        else:
            self.buf[:] = sorted(self.buf)
        lpns = self.buf[:BUFFER]
        gtd_to_update: dict[int, int] = {}
        for lpn in lpns:
            self.mapped[lpn] = 1
            gtd_to_update[lpn // ENTS_PER_TP] = lpn
        for tvpn, value in gtd_to_update.items():
            if value != 0:                     # KI-10 sentinel
                self.gtd[tvpn] = 1
        last = 0
        for i in range(1, BUFFER):
            cut = (lpns[i] - lpns[i - 1] > THRESHOLD or i - last >= TRAIN_LEN
                   or lpns[i] // ENTS_PER_TP != lpns[i - 1] // ENTS_PER_TP)
            if i == BUFFER - 1 and "last" in self.fix:
                if cut:                        # close the open segment, then the last LPN on its own
                    self.insert(lpns[last:i])
                    self.insert(lpns[i:])
                else:
                    self.insert(lpns[last:])
            elif cut or i == BUFFER - 1:
                self.insert(lpns[last:i])      # authors': element i (the last LPN) is left out (KI-9)
                last = i

    def insert(self, lpns: list[int]):
        self.total_segments += 1
        group = lpns[0] // ENTS_PER_TP
        accurate = all(b - a == 1 for a, b in zip(lpns, lpns[1:]))
        seg = Seg(lpns, accurate)
        if not accurate:                       # regression() creates the CRB before the insert
            seg.crb = list(lpns)
            self.crbs.setdefault(group, []).insert(0, seg)
            self.crb_count.setdefault(group, Counter()).update(lpns)
        levels = self.levels.setdefault(group, [])
        if not levels:
            levels.append([seg])
            return
        top = levels[0]
        start, end = seg.lb, seg.ub
        if end < top[0].lb:
            top.insert(0, seg)
            return
        if len(top) == 1:
            if "insert" in self.fix:
                if start > top[0].ub:
                    top.append(seg)
                else:
                    levels.insert(0, [seg])
            else:
                self.lost_segments += 1        # while (q) never runs: segment silently dropped
            return
        p, q = 0, 1
        while q < len(top):
            if end < top[q].lb and start > top[p].ub:
                top.insert(q, seg)
                return
            if start > top[q].ub:
                p, q = p + 1, q + 1
                if q == len(top):
                    top.append(seg)
                    return
            else:
                levels.insert(0, [seg])        # overlap: new top level holding only this segment
                return

    def compact(self):
        for group, levels in self.levels.items():
            if not levels:
                continue
            get = self.valid_lpns(group, levels[0])
            keep = [levels[0]]
            for level in levels[1:]:
                second = self.valid_lpns(group, level)
                delete = True
                for k in range(ENTS_PER_TP):
                    if second[k] == 1 and get[k] == 0:
                        get[k] = 1
                        delete = False
                if delete:
                    for seg in level:
                        if seg.crb is not None:
                            self.crbs[group].remove(seg)
                            self.crb_count[group].subtract(seg.crb)
                else:
                    keep.append(level)
            self.levels[group] = keep

    def valid_lpns(self, group: int, level: list[Seg]) -> list[int]:
        out = [0] * ENTS_PER_TP
        base = group * ENTS_PER_TP
        fixed = "compact" in self.fix
        for seg in level:
            if seg.accurate:
                for i in range(seg.slpa, seg.slpa + seg.len + fixed):   # authors': excludes the last LPN
                    out[i] = 1
            elif self.crbs.get(group):
                for lpn in seg.crb:
                    out[lpn - base] = 1 if fixed else 0                  # authors' code writes 0 here
        return out

    # ---- read path ----------------------------------------------------------------------------------
    def lookup(self, lpn: int):
        """all_seg_get_ppn: returns ('hit'|'correction'|'miss', cacheable)."""
        group = lpn // ENTS_PER_TP
        off = lpn - group * ENTS_PER_TP
        for level in self.levels.get(group, []):
            for seg in level:
                if seg.lb <= off <= seg.ub:
                    if seg.accurate:
                        return "hit", True
                    if self.crb_count.get(group, {}).get(lpn, 0) > 0:
                        return "correction", False
                    break                       # flag 0: next level
        return "miss", False

    def read(self, start: int, end: int):
        c = self.c
        c["read_reqs"] += 1
        # KI-4: `vppn` is uninitialised and only assigned on some paths. Each ssd_read call reuses the same stack
        # slot, so in practice it holds the value the previous call left there; modelled as persisting.
        vppn = self.vppn
        for lpn in range(start, end + 1):
            c["read_lpns"] += 1
            if not self.start_write:
                c["pre_first_write"] += 1
                continue
            if self.buf[0] == lpn:
                c["buffer_hits"] += 1
                continue
            tvpn = lpn // ENTS_PER_TP
            if tvpn in self.cmt:
                self.cmt.move_to_end(tvpn)
                c["cmt_hits"] += 1
                vppn = "value"
            else:
                c["cmt_misses"] += 1
                if not self.gtd[tvpn]:
                    c["unmapped_tp"] += 1      # vppn keeps its previous value
                else:
                    c["model_queries"] += 1
                    kind, cacheable = self.lookup(lpn)
                    c[{"hit": "model_hits", "correction": "model_corrections", "miss": "model_misses"}[kind]] += 1
                    vppn = INVALID if kind == "miss" else "value"
                    if cacheable:
                        if len(self.cmt) == CMT_ENTRIES:
                            self.cmt.popitem(last=False)
                        self.cmt[tvpn] = None
            if vppn == INVALID:
                c["invalid_read_cnt"] += 1
                if self.mapped[lpn]:
                    c["invalid_but_mapped"] += 1
            elif self.mapped[lpn]:
                c["served_lpns"] += 1
            else:
                c["true_unmapped"] += 1
        self.vppn = vppn


COMPARED = ["read_lpns", "write_lpns", "pre_first_write", "buffer_hits", "cmt_hits", "cmt_misses", "unmapped_tp",
            "model_queries", "model_hits", "model_corrections", "model_misses", "invalid_read_cnt",
            "invalid_but_mapped", "served_lpns", "true_unmapped", "writes_buffered", "writes_absorbed",
            "writes_dropped", "buffer_flushes"]


def replay(run: Path, fix: set[str]):
    records = [json.loads(l) for l in (run / "records.jsonl").read_text().splitlines() if l]
    if any(r.get("gc_runs", 0) for r in records):
        raise SystemExit(f"{run.name}: GC ran; GC is not modelled")
    rep = Replica(fix)
    intervals = []
    with gzip.open(run / "reqlog.csv.gz", "rt") as handle:
        for line in handle:
            if line.startswith("#"):
                intervals.append(rep.c)
                rep.reset()
                continue
            _, op, slba, nlb = line.rstrip("\n").split(",")
            slba, nlb = int(slba), int(nlb)
            start, end = slba // SECS_PER_PG, (slba + nlb - 1) // SECS_PER_PG
            (rep.read if op == "R" else rep.write)(start, end)
    return rep, intervals, records


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run", type=Path)
    ap.add_argument("--fix", default="")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    fix = set(filter(None, args.fix.split(",")))
    rep, intervals, records = replay(args.run, fix)
    mismatches = 0
    out = {"run": args.run.name, "fix": sorted(fix), "segments": rep.total_segments,
           "lost_segments": rep.lost_segments, "intervals": []}
    for i, (pred, rec) in enumerate(zip(intervals, records)):
        rows = {k: {"replica": pred.get(k, 0), "femu": rec.get(k)} for k in COMPARED}
        if not fix:
            bad = [k for k, v in rows.items() if v["femu"] is not None and v["replica"] != v["femu"]]
            mismatches += len(bad)
        out["intervals"].append({k: v["replica"] for k, v in rows.items()} | {"femu": {k: v["femu"] for k, v in rows.items()}})
        if not args.json:
            print(f"interval {i}:")
            for k, v in rows.items():
                flag = "" if fix or v["femu"] is None or v["replica"] == v["femu"] else "   <-- MISMATCH"
                print(f"  {k:20s} replica={v['replica']:>10}  femu={v['femu']!s:>10}{flag}")
    if args.json:
        print(json.dumps(out))
    else:
        print(f"segments built {rep.total_segments}, dropped by the single-segment insert {rep.lost_segments}")
        if not fix:
            print(f"{mismatches} mismatching counter(s)")
    return 1 if mismatches else 0


if __name__ == "__main__":
    sys.exit(main())
