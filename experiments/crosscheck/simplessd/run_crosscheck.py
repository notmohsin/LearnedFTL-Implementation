#!/usr/bin/env python3
"""Cross-check our LRU / LFU cache numbers against the SimpleSSD simulator.

    run_crosscheck.py [--jobs N] [--capacity 8192 ...] [--simplessd ~/SSD-Simulator]

What is checked: experiments/traces/profiles.json holds, for every trace, the exact number of hits an LRU and an
(in-cache) LFU cache of C entries get on the trace's read stream, keyed per 4 KiB page, every miss inserting
(computed by experiments/tools/cachesim.c). This script replays the same read stream through the CMT of
~/SSD-Simulator and compares the hit counts. Stock SimpleSSD has no CMT: the one in ~/SSD-Simulator
(simplessd/ftl/page_mapping.cc) was added in that fork by the same author as cachesim.c. It is a second, separately
written implementation in a different code base, not a third-party one.

The comparison count: each trace with reads gives two comparisons (LRU, LFU). A trace with no reads has no LRU/LFU
numbers and gives one comparison of the zero read counts. With 20 traces with reads and 2 without, that is
40 + 2 = 42.

SimpleSSD is configured so that its CMT sees exactly that stream (sim.cfg.template / ssd.cfg.template, made from
the configs in ~/SSD-Simulator/config; ~/SSD-Simulator itself is not modified by this script):
  - only the trace's read lines are replayed (the regex matches 'R' lines; other lines are skipped), one at a time
    (QueueDepth 1, synchronous timing);
  - one CMT entry = one 4 KiB page: PageSize 4096, no superpage, multi-plane operation off;
  - no data cache in front of the FTL (read cache, read prefetch and write cache off), no window fill;
  - the first 52% of the logical space (31 GiB, more than the 30 GiB any trace addresses) is written during
    warm-up, so every page a trace reads is mapped and every miss inserts. Warm-up statistics are discarded.
    The warm-up leaves its last pages in the CMT; those pages are never read by a trace, so under both LRU and LFU
    (ties broken by recency) they are evicted before any trace page and the hit counts equal an empty start;
  - no writes are replayed, so no GC can disturb the CMT.

Output: experiments/results/simplessd_crosscheck.json (match per trace and policy) and one folder per run under
experiments/results/crosscheck/simplessd/ (configs, console output, sim_stats.log).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXP = HERE.parents[1]
STAGES = ["validation", "matrix", "real"]          # the trace folders of the runs we did (micro not profiled)
POLICIES = {"lru": 0, "lfu": 1}
ENTRY_BYTES = 8                                     # SimpleSSD bytes per CMT entry with one 4 KiB page per entry


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def parse_stats(path: Path) -> dict:
    stats = {}
    for line in path.read_text().splitlines():
        parts = line.split()
        if len(parts) >= 2:
            try:
                stats[parts[0]] = float(parts[1])
            except ValueError:
                pass
    return stats


def run_one(binary: Path, trace: Path, policy: str, capacity: int, out_root: Path) -> dict:
    out = out_root / f"{trace.parent.name}-{trace.stem}-{policy}-c{capacity}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "sim.cfg").write_text((HERE / "sim.cfg.template").read_text().replace("@TRACE@", str(trace)))
    (out / "ssd.cfg").write_text((HERE / "ssd.cfg.template").read_text()
                                 .replace("@POLICY@", str(POLICIES[policy]))
                                 .replace("@CMTBYTES@", str(capacity * ENTRY_BYTES)))
    with (out / "console.log").open("w") as log:
        code = subprocess.run(["nice", "-n", "10", str(binary), "sim.cfg", "ssd.cfg", "."], cwd=out,
                              stdout=log, stderr=subprocess.STDOUT).returncode
    stats = parse_stats(out / "sim_stats.log") if (out / "sim_stats.log").exists() else {}
    return {"exit_code": code, "dir": str(out.relative_to(EXP)),
            "read_requests": stats.get("read.request_count"),
            "read_bytes": stats.get("read.bytes"),
            "hits": stats.get("ftl.page_mapping.cmt.hits"),
            "misses": stats.get("ftl.page_mapping.cmt.misses"),
            "capacity": stats.get("ftl.page_mapping.cmt.capacity")}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--capacity", type=int, action="append", help="CMT entries (default 8192; repeatable)")
    ap.add_argument("--simplessd", type=Path, default=Path.home() / "SSD-Simulator")
    ap.add_argument("--out", type=Path, default=EXP / "results" / "simplessd_crosscheck.json")
    args = ap.parse_args()
    capacities = args.capacity or [8192]
    binary = args.simplessd / "build" / "simplessd-standalone"
    profiles = json.loads((EXP / "traces" / "profiles.json").read_text())
    traces = [t for stage in STAGES for t in sorted((EXP / "traces" / stage).glob("*.trace"))]
    out_root = EXP / "results" / "crosscheck" / "simplessd"

    jobs = [(t, p, c) for t in traces for p in POLICIES for c in capacities]
    with ThreadPoolExecutor(args.jobs) as pool:
        results = list(pool.map(lambda j: run_one(binary, *j, out_root), jobs))
    by_job = dict(zip(jobs, results))

    rows, comparisons, matching = [], 0, 0
    for trace in traces:
        key = f"traces/{trace.parent.name}/{trace.stem}"
        prof = profiles[key]
        bounds = prof["cache_bounds_read_stream"].get("lpn")
        ours_pages = bounds["references"] if bounds else 0
        file_sha = sha256(trace)
        row = {"trace": key,
               "trace_sha256": {"file": file_sha, "profiled": prof["provenance"]["sha256"],
                                "match": file_sha == prof["provenance"]["sha256"]},
               "read_requests": {"ours": prof["mix"]["read_reqs"]},
               "read_pages": {"ours": ours_pages}}
        first = by_job[(trace, "lru", capacities[0])]
        row["simplessd_exit_code"] = first["exit_code"]
        row["read_requests"]["simplessd"] = int(first["read_requests"] or 0)
        row["read_pages"]["simplessd"] = int((first["read_bytes"] or 0) // 4096)
        for part in ("read_requests", "read_pages"):
            row[part]["match"] = row[part]["ours"] == row[part]["simplessd"]
        if not bounds:
            row["note"] = "trace has no reads, so it has no LRU/LFU numbers; only the zero read counts are compared"
            comparisons += 1
            matching += (row["read_requests"]["match"] and row["read_pages"]["match"]
                         and row["trace_sha256"]["match"] and first["exit_code"] == 0)
            rows.append(row)
            continue
        for policy in POLICIES:
            row[policy] = {}
            for cap in capacities:
                ours = next(c for c in prof["cache_bounds_read_stream"]["lpn"]["curve"] if c["capacity"] == cap)
                sim = by_job[(trace, policy, cap)]
                hits = int(sim["hits"]) if sim["hits"] is not None else None
                lookups = hits + int(sim["misses"]) if hits is not None and sim["misses"] is not None else None
                ok = (sim["exit_code"] == 0 and sim["capacity"] == cap and hits == ours[f"{policy}_hits"]
                      and lookups == ours_pages and row["trace_sha256"]["match"])
                comparisons += 1
                matching += ok
                row[policy][str(cap)] = {
                    "ours_hits": ours[f"{policy}_hits"], "simplessd_hits": hits,
                    "ours_hit_rate": ours[f"{policy}_hit_rate"],
                    "simplessd_hit_rate": round(hits / lookups, 6) if lookups else None,
                    "simplessd_lookups": lookups,
                    "difference_hits": hits - ours[f"{policy}_hits"] if hits is not None else None,
                    "match": ok, "simplessd_exit_code": sim["exit_code"], "simplessd_run": sim["dir"]}
        rows.append(row)

    git = lambda *a: subprocess.run(["git", "-C", str(args.simplessd), *a], capture_output=True, text=True).stdout
    commit = git("rev-parse", "HEAD").strip()
    uncommitted = [l for l in git("status", "--short").splitlines() if l]
    built = binary.stat().st_mtime
    newer = sorted(str(f.relative_to(args.simplessd)) for d in ("simplessd", "igl", "sim", "util", "lib")
                   if (args.simplessd / d).is_dir() for f in (args.simplessd / d).rglob("*")
                   if f.suffix in (".cc", ".hh", ".c", ".h", ".cpp") and f.stat().st_mtime > built)
    report = {
        "what": "LRU and LFU hit counts of an N-entry cache on each trace's read stream, keyed per 4 KiB page: ours "
                "(experiments/traces/profiles.json, computed by experiments/tools/cachesim.c) vs the CMT of the ~/SSD-Simulator "
                "fork of SimpleSSD (added in that fork by the same author; stock SimpleSSD has none). A trace with no "
                "reads gives one comparison of the zero read counts instead of two hit-count comparisons.",
        "match_rule": "exact: same hit count, SimpleSSD looked up every read page (hits + misses = read pages), "
                      "CMT capacity as requested, run exited with code 0, and the trace file's SHA-256 equals the "
                      "one in profiles.json (so both sides read the same file)",
        "summary": {"comparisons": comparisons, "matching": matching, "all_match": comparisons == matching,
                    "hit_count_comparisons": sum(len(r[p]) for r in rows for p in POLICIES if p in r),
                    "hit_count_comparisons_with_zero_hits": sum(1 for r in rows for p in POLICIES if p in r
                                                                for v in r[p].values() if v["ours_hits"] == 0),
                    "zero_read_checks": sum(1 for r in rows if "note" in r),
                    "traces": len(traces), "policies": list(POLICIES), "capacities": capacities},
        "simplessd": {"repo": str(args.simplessd), "commit": commit, "binary": str(binary),
                      "binary_sha256": sha256(binary),
                      "uncommitted_changes": uncommitted,
                      "source_files_changed_since_build": newer,
                      "config": "experiments/crosscheck/simplessd/{sim,ssd}.cfg.template"},
        "traces": rows,
    }
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(f"{matching}/{comparisons} comparisons match; wrote {args.out}")
    return 0 if comparisons == matching else 1


if __name__ == "__main__":
    sys.exit(main())
