#!/usr/bin/env python3
"""Profile every workload trace and generate experiments/traces/TRACES.md.

The output is generated, never hand-edited. Re-running on unchanged traces
produces byte-identical TRACES.md and profiles.json.

Usage (from the repository root):
    python3 experiments/scripts/profile_traces.py
    python3 experiments/scripts/profile_traces.py --only zipf-80-20

Trace format (one request per line, '#' lines are comments):
    timestamp operation offset_bytes length_bytes
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import itertools
import json
import math
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

PAGE_SIZE = 4096
LOGICAL_DEVICE_GIB = 30.0  # devsz_mb used by the baseline runs (plan, Phase 3)
CMT_ENTRIES = 8192  # tt_cmt_size in leaftl.c and ld-tpftl.c
CAPACITIES = (1024, 2048, 4096, 8192, 16384, 32768, 65536)
WORKING_SET_WINDOW = 100_000  # page references per window
SKEW_FRACTIONS = (0.01, 0.10, 0.20)
SD_PERCENTILES = (50, 90, 99)
SIZE_BUCKETS_LPN = (1, 2, 4, 8, 16, 32, 64)  # power-of-two buckets, upper bound inclusive

# Keyings of the read reference stream used for cache bounds.
KEYINGS = {
    "tp256": ("translation page, 256 LPNs/page (LeaFTL CMT key)", 256),
    "tp512": ("translation page, 512 LPNs/page", 512),
    "lpn": ("logical page (LearnedFTL CMT entry granularity)", 1),
}

# Provenance for traces not listed in traces/manifest.csv.
EXTRA_PROVENANCE = {
    "zipf-80-20": {
        "generator": "experiments/scripts/generate_8020_trace.py",
        "source": "parameters from experiments/results/runs.csv run_id 2 (not in manifest)",
        "operations": "100000",
        "address_range_gib": "1.0",
        "read_percent": "50.0",
        "write_percent": "50.0",
        "seed": "42",
        "pattern": "zipf hot / uniform cold",
        "hot_region_percent": "20.0",
        "hot_access_percent": "80.0",
        "zipf_alpha": "1.2",
    },
}

EXPERIMENTS_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = EXPERIMENTS_DIR.parent


# --------------------------------------------------------------------------
# helpers


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def r(value: float, digits: int = 6) -> float:
    """Round for stable, readable output."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    return round(float(value), digits)


def build_cachesim() -> Path:
    source = EXPERIMENTS_DIR / "tools" / "cachesim.c"
    binary = EXPERIMENTS_DIR / "tools" / "build" / "cachesim"
    if not binary.exists() or binary.stat().st_mtime < source.stat().st_mtime:
        binary.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["cc", "-O2", "-Wall", "-o", str(binary), str(source)], check=True)
    return binary


def lfs_oids() -> dict[str, str]:
    """Map repo-relative path -> Git LFS sha256 oid (the content hash)."""
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO_DIR), "lfs", "ls-files", "-l"],
            check=True, capture_output=True, text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return {}
    oids = {}
    for line in out.splitlines():
        parts = line.split(maxsplit=2)
        if len(parts) == 3:
            oids[parts[2]] = parts[0]
    return oids


def load_manifest() -> dict[str, dict[str, str]]:
    path = EXPERIMENTS_DIR / "traces" / "manifest.csv"
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as handle:
        return {row["workload"]: row for row in csv.DictReader(handle)}


def read_trace(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(
        path, sep=" ", comment="#", header=None,
        names=["ts", "op", "offset", "length"],
        dtype={"ts": "float64", "op": "str", "offset": "int64", "length": "int64"},
        engine="c",
    )
    bad = ~frame["op"].isin(["R", "W"])
    if bad.any():
        raise ValueError(f"{path}: unexpected operation codes {sorted(frame.loc[bad, 'op'].unique())}")
    if (frame["offset"] < 0).any() or (frame["length"] <= 0).any():
        raise ValueError(f"{path}: negative offset or non-positive length")
    return frame


def validation_prefix_check(child: Path, parent: Path) -> dict:
    """Byte-compare a validation trace with the leading lines of its parent."""
    with child.open("rb") as handle:
        child_lines = handle.read().splitlines(keepends=True)
    with parent.open("rb") as handle:
        parent_head = list(itertools.islice(handle, len(child_lines)))
    ops = sum(1 for line in child_lines if line.strip() and not line.startswith(b"#"))
    return {"parent": str(parent.relative_to(REPO_DIR)), "prefix_operations": ops,
            "is_exact_prefix": child_lines == parent_head}


# --------------------------------------------------------------------------
# profiling


def cache_bounds(cachesim: Path, keys: np.ndarray, workdir: Path) -> dict:
    keys_file = workdir / "keys.u32"
    sd_file = workdir / "sd.u32"
    keys.astype("<u4").tofile(keys_file)
    out = subprocess.run(
        [str(cachesim), str(keys_file), str(sd_file), *map(str, CAPACITIES)],
        check=True, capture_output=True, text=True,
    ).stdout
    simulated: dict[str, dict[int, int]] = {"belady": {}, "lfu": {}, "plfu": {}}
    for line in out.splitlines():
        policy, capacity, hits = line.split()
        simulated[policy][int(capacity)] = int(hits)
    sd = np.fromfile(sd_file, dtype="<u4")
    n = len(keys)
    cold = sd == np.iinfo(np.uint32).max
    warm = sd[~cold]
    curve = []
    for capacity in CAPACITIES:
        lru_hits = int((sd < capacity).sum())
        point = {"capacity": capacity, "lru_hits": lru_hits, "lru_hit_rate": r(lru_hits / n)}
        for policy in ("lfu", "plfu", "belady"):
            point[f"{policy}_hits"] = simulated[policy][capacity]
            point[f"{policy}_hit_rate"] = r(simulated[policy][capacity] / n)
        curve.append(point)
    return {
        "references": n,
        "distinct_keys": int(cold.sum()),
        "compulsory_miss_rate": r(cold.sum() / n),
        "stack_distance_percentiles": (
            {f"p{p}": int(np.percentile(warm, p, method="lower")) for p in SD_PERCENTILES}
            if warm.size else {}
        ),
        "curve": curve,
    }


def profile(path: Path, cachesim: Path, workdir: Path) -> dict:
    frame = read_trace(path)
    is_read = (frame["op"] == "R").to_numpy()
    offset = frame["offset"].to_numpy()
    length = frame["length"].to_numpy()
    ts = frame["ts"].to_numpy()
    n_req = len(frame)

    aligned = bool(((offset % PAGE_SIZE) == 0).all() and ((length % PAGE_SIZE) == 0).all())
    first_lpn = offset // PAGE_SIZE
    n_lpn = (offset + length - 1) // PAGE_SIZE - first_lpn + 1

    # Expand requests to per-LPN references (trace order).
    total_refs = int(n_lpn.sum())
    lpn = np.repeat(first_lpn, n_lpn) + (
        np.arange(total_refs, dtype=np.int64) - np.repeat(np.cumsum(n_lpn) - n_lpn, n_lpn)
    )
    ref_is_read = np.repeat(is_read, n_lpn)
    positions = np.arange(total_refs, dtype=np.int64)
    max_lpn = int(lpn.max()) if total_refs else 0

    read_lpns = lpn[ref_is_read]
    write_lpns = lpn[~ref_is_read]
    read_pos = positions[ref_is_read]
    write_pos = positions[~ref_is_read]

    # ---- mix
    reads, writes = int(is_read.sum()), int((~is_read).sum())
    read_bytes, write_bytes = int(length[is_read].sum()), int(length[~is_read].sum())
    sizes = {}
    for op_name, mask in (("R", is_read), ("W", ~is_read)):
        values, counts = np.unique(length[mask], return_counts=True)
        sizes[op_name] = {f"{int(v) // 1024}KiB": int(c) for v, c in zip(values, counts)}
    size_hist = {}
    lower = 0
    for upper in SIZE_BUCKETS_LPN:
        size_hist[f"{lower + 1}-{upper}" if upper > lower + 1 else str(upper)] = int(
            ((n_lpn > lower) & (n_lpn <= upper)).sum())
        lower = upper
    size_hist[f">{lower}"] = int((n_lpn > lower).sum())

    # ---- footprint
    uniq_all = np.unique(lpn)
    uniq_read = np.unique(read_lpns)
    uniq_write = np.unique(write_lpns)
    footprint = {
        "min_offset_bytes": int(offset.min()) if n_req else 0,
        "max_end_bytes": int((offset + length).max()) if n_req else 0,
        "unique_lpns": int(uniq_all.size),
        "unique_read_lpns": int(uniq_read.size),
        "unique_written_lpns": int(uniq_write.size),
        "unique_tp256": int(np.unique(uniq_all // 256).size),
        "unique_tp512": int(np.unique(uniq_all // 512).size),
        "footprint_gib": r(uniq_all.size * PAGE_SIZE / 2**30, 4),
        "fits_logical_device": bool((offset + length).max() <= LOGICAL_DEVICE_GIB * 2**30) if n_req else True,
    }

    # ---- locality
    sequential = (offset[1:] == offset[:-1] + length[:-1]) if n_req > 1 else np.array([], bool)
    _, access_counts = np.unique(lpn, return_counts=True)
    access_counts = np.sort(access_counts)[::-1]
    cumulative = np.cumsum(access_counts)
    skew = {}
    for fraction in SKEW_FRACTIONS:
        k = max(1, math.ceil(fraction * access_counts.size))
        skew[f"top{int(fraction * 100)}pct_lpn_access_share"] = r(cumulative[k - 1] / total_refs)
    window_id = positions // WORKING_SET_WINDOW
    window_keys = np.unique(window_id * (max_lpn + 1) + lpn)
    _, per_window = np.unique(window_keys // (max_lpn + 1), return_counts=True)
    locality = {
        "sequential_request_share": r(sequential.mean()) if sequential.size else None,
        **skew,
        "working_set_window_refs": WORKING_SET_WINDOW,
        "working_set_lpns_mean": r(per_window.mean(), 1),
        "working_set_lpns_max": int(per_window.max()),
        "working_set_gib_max": r(per_window.max() * PAGE_SIZE / 2**30, 4),
    }

    # ---- mapping-relevant behaviour (read-after-write)
    first_write = np.full(max_lpn + 1, np.iinfo(np.int64).max, dtype=np.int64)
    if write_lpns.size:
        written, first_index = np.unique(write_lpns, return_index=True)
        first_write[written] = write_pos[first_index]
    read_after_write = first_write[read_lpns] < read_pos
    raw_count = int(read_after_write.sum())
    never_written_reads = int(read_lpns.size - raw_count)
    overwrites = int(write_lpns.size - uniq_write.size)
    mapping = {
        "read_after_write_share_precondition_none": r(raw_count / read_lpns.size) if read_lpns.size else None,
        "read_after_write_share_precondition_fill": 1.0 if read_lpns.size and footprint["fits_logical_device"] else None,
        "never_written_read_lpns_precondition_none": never_written_reads,
        "overwrite_share_of_written_lpns": r(overwrites / write_lpns.size) if write_lpns.size else None,
    }

    # ---- expected FTL counters (invariant 10)
    expected = {
        "read_reqs": reads,
        "write_reqs": writes,
        "read_lpns": int(read_lpns.size),
        "write_lpns": int(write_lpns.size),
        "never_written_read_lpns_precondition_none": never_written_reads,
        "never_written_read_lpns_precondition_fill": 0 if footprint["fits_logical_device"] else None,
        "request_size_histogram_lpns": size_hist,
    }

    # ---- cache bounds over the read reference stream
    bounds = {}
    if read_lpns.size:
        for name, (_, divisor) in KEYINGS.items():
            bounds[name] = cache_bounds(cachesim, read_lpns // divisor, workdir)

    return {
        "operations": n_req,
        "duration_seconds": r(ts.max() - ts.min(), 6) if n_req else 0.0,
        "page_aligned": aligned,
        "mix": {
            "read_reqs": reads,
            "write_reqs": writes,
            "read_percent_by_requests": r(100 * reads / n_req, 3) if n_req else None,
            "read_percent_by_bytes": r(100 * read_bytes / (read_bytes + write_bytes), 3) if n_req else None,
            "mean_request_kib": r(length.mean() / 1024, 3) if n_req else None,
            "request_sizes": sizes,
        },
        "footprint": footprint,
        "locality": locality,
        "mapping": mapping,
        "expected_ftl_counters": expected,
        "cache_bounds_read_stream": bounds,
    }


# --------------------------------------------------------------------------
# markdown rendering


def pct(value) -> str:
    return "n/a" if value is None else f"{100 * value:.2f}%"


def num(value) -> str:
    return "n/a" if value is None else f"{value:,}"


def render(profiles: dict[str, dict]) -> str:
    lines = [
        "# Trace profiles",
        "",
        "<!-- GENERATED by experiments/scripts/profile_traces.py - do not edit by hand. -->",
        "",
        "This is the reference profile of every workload trace used for the LeaFTL / LearnedFTL",
        "baselines. Regenerate it after adding or changing any trace:",
        "",
        "```bash",
        "python3 experiments/scripts/profile_traces.py",
        "```",
        "",
        "Machine-readable values for every number below are in `profiles.json` next to this file.",
        "",
        "## Trace format",
        "",
        "One request per line: `timestamp operation offset_bytes length_bytes`, where `operation` is",
        "`R` or `W`. Lines starting with `#` are comments. Requests are expanded to 4 KiB logical",
        "pages (LPNs) for every per-page statistic.",
        "",
        "## Metric definitions",
        "",
        "| Metric | Definition |",
        "|---|---|",
        "| SHA-256 check | File hash compared with `manifest.csv` and the Git LFS object id where available. |",
        "| Read % (req / bytes) | Share of requests, and of bytes, that are reads. |",
        "| Footprint | Distinct LPNs touched × 4 KiB. `Fits device` compares the highest byte address with the "
        f"{LOGICAL_DEVICE_GIB:g} GiB logical device used in the baseline runs. |",
        "| Unique TPs (256 / 512) | Distinct translation pages touched with 256 LPNs per page (LeaFTL) and 512 (LearnedFTL). |",
        "| Sequential share | Share of requests that start exactly where the previous request ended. |",
        "| Top-k% share | Share of all page references that go to the hottest k% of distinct LPNs. |",
        f"| Working set | Distinct LPNs per window of {WORKING_SET_WINDOW:,} consecutive page references. |",
        "| Read-after-write (RAW) share | Share of read LPNs whose address was written earlier in the trace. "
        "With precondition `none`, reads with no earlier write target unmapped space: LeaFTL counts them "
        "as CMT misses (docs/KNOWN_ISSUES.md). With precondition `fill` every read is mapped. |",
        "| Overwrite share | Share of written LPNs that overwrite an LPN written earlier in the trace. |",
        "| Stack distance | Exact LRU stack distance on the read stream: distinct keys referenced since the previous "
        "reference to the same key. An LRU cache of capacity C hits iff stack distance < C. |",
        "| LRU / LFU / Perfect-LFU / Belady hit rate | Hit rate over the read stream of: an LRU cache; an "
        "in-cache LFU cache (a key's count starts at 1 on insertion, rises on each hit and is discarded on "
        "eviction; the victim is the lowest count, ties to the least recently used); a perfect-LFU cache (counts "
        "persist across evictions, every reference counts; same victim rule); and Belady's optimal (MIN) cache. "
        "All assume every miss inserts. These are reference points on the read stream only. Real FTL CMTs "
        "differ: LeaFTL caches one learned segment per translation page and only inserts accurate segments of "
        "mapped pages; LearnedFTL also fills the CMT on writes and prefetches within a translation page. |",
        "| Expected FTL counters | Values the FTL must report for this trace (Phase 2b invariant 10): requests and "
        "LPNs per operation, never-written reads, and the request-size histogram in LPNs. |",
        "",
        "Keyings used for cache bounds:",
        "",
    ]
    for name, (description, _) in KEYINGS.items():
        lines.append(f"- `{name}`: {description}")
    lines += [
        "",
        "## Summary",
        "",
        "| Trace | Ops | Read % (req) | Mean KiB | Footprint GiB | Seq. share | Top-20% share | RAW share (none) "
        f"| LRU tp256 @{CMT_ENTRIES} | LFU tp256 @{CMT_ENTRIES} | Belady tp256 @{CMT_ENTRIES} "
        f"| LRU lpn @{CMT_ENTRIES} | LFU lpn @{CMT_ENTRIES} | Belady lpn @{CMT_ENTRIES} | SHA OK |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]

    def at_cmt(p, keying, field):
        bounds = p["cache_bounds_read_stream"].get(keying)
        if not bounds:
            return None
        return next(c[field] for c in bounds["curve"] if c["capacity"] == CMT_ENTRIES)

    for name, p in profiles.items():
        lines.append(
            f"| [{name}](#{anchor(name)}) | {num(p['operations'])} | {p['mix']['read_percent_by_requests']:.1f} "
            f"| {p['mix']['mean_request_kib']:.1f} | {p['footprint']['footprint_gib']:.3f} "
            f"| {pct(p['locality']['sequential_request_share'])} | {pct(p['locality']['top20pct_lpn_access_share'])} "
            f"| {pct(p['mapping']['read_after_write_share_precondition_none'])} "
            f"| {pct(at_cmt(p, 'tp256', 'lru_hit_rate'))} | {pct(at_cmt(p, 'tp256', 'lfu_hit_rate'))} "
            f"| {pct(at_cmt(p, 'tp256', 'belady_hit_rate'))} "
            f"| {pct(at_cmt(p, 'lpn', 'lru_hit_rate'))} | {pct(at_cmt(p, 'lpn', 'lfu_hit_rate'))} "
            f"| {pct(at_cmt(p, 'lpn', 'belady_hit_rate'))} "
            f"| {sha_status(p['provenance'])} |"
        )
    lines.append("")

    for name, p in profiles.items():
        lines += render_one(name, p)
    return "\n".join(lines).rstrip() + "\n"


def anchor(name: str) -> str:
    return name.lower().replace("/", "").replace(".", "").replace(" ", "-")


def sha_status(prov: dict) -> str:
    checks = [v for k, v in prov.items() if k.startswith("sha256_matches_")]
    if not checks:
        return "no reference"
    return "yes" if all(checks) else "**MISMATCH**"


def render_one(name: str, p: dict) -> list[str]:
    prov, mix, fp, loc, mp, ex = (p["provenance"], p["mix"], p["footprint"], p["locality"],
                                  p["mapping"], p["expected_ftl_counters"])
    out = [f"## {name}", "", "**Provenance**", ""]
    for key in sorted(prov):
        value = prov[key]
        out.append(f"- {key}: `{value}`" if value not in ("", None) else f"- {key}: n/a")
    out += [
        "",
        "**Mix and footprint**",
        "",
        "| | |",
        "|---|---|",
        f"| Operations | {num(p['operations'])} ({num(mix['read_reqs'])} R / {num(mix['write_reqs'])} W) |",
        f"| Read % by requests / bytes | {mix['read_percent_by_requests']:.2f}% / {mix['read_percent_by_bytes']:.2f}% |",
        f"| Mean request size | {mix['mean_request_kib']:.2f} KiB |",
        f"| Request sizes (R) | {', '.join(f'{k}: {num(v)}' for k, v in mix['request_sizes']['R'].items()) or 'none'} |",
        f"| Request sizes (W) | {', '.join(f'{k}: {num(v)}' for k, v in mix['request_sizes']['W'].items()) or 'none'} |",
        f"| Page aligned | {'yes' if p['page_aligned'] else '**no**'} |",
        f"| Address range | {num(fp['min_offset_bytes'])} – {num(fp['max_end_bytes'])} bytes |",
        f"| Footprint | {fp['footprint_gib']:.3f} GiB ({num(fp['unique_lpns'])} LPNs: "
        f"{num(fp['unique_read_lpns'])} read, {num(fp['unique_written_lpns'])} written) |",
        f"| Unique translation pages | {num(fp['unique_tp256'])} @256, {num(fp['unique_tp512'])} @512 |",
        f"| Fits {LOGICAL_DEVICE_GIB:g} GiB device | {'yes' if fp['fits_logical_device'] else '**no**'} |",
        "",
        "**Locality**",
        "",
        "| | |",
        "|---|---|",
        f"| Sequential request share | {pct(loc['sequential_request_share'])} |",
        f"| Access share of hottest 1% / 10% / 20% LPNs | {pct(loc['top1pct_lpn_access_share'])} / "
        f"{pct(loc['top10pct_lpn_access_share'])} / {pct(loc['top20pct_lpn_access_share'])} |",
        f"| Working set per {num(loc['working_set_window_refs'])} refs (mean / max) | "
        f"{loc['working_set_lpns_mean']:,.1f} / {num(loc['working_set_lpns_max'])} LPNs (max {loc['working_set_gib_max']:.3f} GiB) |",
        "",
        "**Mapping-relevant behaviour**",
        "",
        "| | |",
        "|---|---|",
        f"| Read-after-write share (precondition none / fill) | {pct(mp['read_after_write_share_precondition_none'])} / "
        f"{pct(mp['read_after_write_share_precondition_fill'])} |",
        f"| Never-written read LPNs (precondition none) | {num(mp['never_written_read_lpns_precondition_none'])} |",
        f"| Overwrite share of written LPNs | {pct(mp['overwrite_share_of_written_lpns'])} |",
        "",
        "**Expected FTL counters** (invariant 10)",
        "",
        "| Counter | Expected |",
        "|---|---:|",
    ]
    for key in ("read_reqs", "write_reqs", "read_lpns", "write_lpns",
                "never_written_read_lpns_precondition_none", "never_written_read_lpns_precondition_fill"):
        out.append(f"| `{key}` | {num(ex[key])} |")
    out.append("| `request_size_histogram_lpns` | " + ", ".join(
        f"{k}: {num(v)}" for k, v in ex["request_size_histogram_lpns"].items() if v) + " |")
    bounds = p["cache_bounds_read_stream"]
    if bounds:
        out += ["", "**Cache bounds on the read stream** (hit rates; every miss inserts)", ""]
        for k in bounds:
            out += [f"`{k}` - {KEYINGS[k][0]}", "",
                    "| Capacity | LRU | LFU | Perfect LFU | Belady-OPT |", "|---:|---:|---:|---:|---:|"]
            for i, capacity in enumerate(CAPACITIES):
                c = bounds[k]["curve"][i]
                out.append(f"| {capacity:,}" + (" (CMT)" if capacity == CMT_ENTRIES else "") +
                           f" | {pct(c['lru_hit_rate'])} | {pct(c['lfu_hit_rate'])} | {pct(c['plfu_hit_rate'])} "
                           f"| {pct(c['belady_hit_rate'])} |")
            out.append("")
        out += ["| Keying | References | Distinct keys | Compulsory miss rate | Stack distance p50 / p90 / p99 |",
                "|---|---:|---:|---:|---:|"]
        for k, b in bounds.items():
            sdp = b["stack_distance_percentiles"]
            out.append(f"| `{k}` | {num(b['references'])} | {num(b['distinct_keys'])} | {pct(b['compulsory_miss_rate'])} "
                       f"| {' / '.join(num(sdp.get(f'p{q}')) for q in SD_PERCENTILES) if sdp else 'n/a'} |")
    else:
        out += ["", "No reads in this trace, so there are no cache bounds."]
    out.append("")
    return out


# --------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", action="append", help="profile only traces with this stem (repeatable)")
    parser.add_argument("--group", action="append",
                        help="profile only this trace directory: traces, validation-traces, "
                             "matrix-traces (repeatable)")
    parser.add_argument("--output-dir", type=Path, default=EXPERIMENTS_DIR / "traces")
    args = parser.parse_args()

    cachesim = build_cachesim()
    manifest = load_manifest()
    oids = lfs_oids()
    profiles_path = args.output_dir / "profiles.json"
    previous = json.loads(profiles_path.read_text()) \
        if profiles_path.exists() and (args.only or args.group) else {}

    sources = [("traces", p) for p in sorted((EXPERIMENTS_DIR / "traces").glob("*.trace"))]
    sources += [("validation-traces", p) for p in sorted((EXPERIMENTS_DIR / "validation-traces").glob("*.trace"))]
    sources += [("matrix-traces", p) for p in sorted((EXPERIMENTS_DIR / "matrix-traces").glob("*.trace"))]

    profiles: dict[str, dict] = {}
    with tempfile.TemporaryDirectory(prefix="profile_traces.") as tmp:
        workdir = Path(tmp)
        for group, path in sources:
            key = f"{group}/{path.stem}"
            if (args.only and path.stem not in args.only) or (args.group and group not in args.group):
                if key in previous:
                    profiles[key] = previous[key]
                continue
            print(f"profiling {key} ...", file=sys.stderr, flush=True)
            rel = str(path.relative_to(REPO_DIR))
            digest = sha256_file(path)
            prov: dict = {"file": rel, "size_bytes": path.stat().st_size, "sha256": digest}
            if group == "traces":
                row = manifest.get(path.stem)
                if row:
                    prov["generator"] = "experiments/scripts/generate_workloads.py"
                    for field in ("pattern", "operations", "address_range_gib", "request_sizes", "read_percent",
                                  "write_percent", "seed", "hot_region_percent", "hot_access_percent",
                                  "zipf_alpha", "sequential_percent"):
                        prov[field] = row.get(field, "")
                    prov["sha256_matches_manifest"] = row["trace_sha256"] == digest
                else:
                    prov.update(EXTRA_PROVENANCE.get(path.stem, {"source": "unknown - add provenance"}))
                if rel in oids:
                    prov["sha256_matches_git_lfs_oid"] = oids[rel] == digest
            else:
                parent = EXPERIMENTS_DIR / "traces" / path.name
                prov["generator"] = "experiments/scripts/prepare_validation_traces.py"
                if parent.exists():
                    check = validation_prefix_check(path, parent)
                    prov.update(check)
                    prov["sha256_matches_parent_prefix"] = check["is_exact_prefix"]
            result = profile(path, cachesim, workdir)
            result["provenance"] = prov
            profiles[key] = result

    order = [f"{g}/{p.stem}" for g, p in sources]
    profiles = {k: profiles[k] for k in order if k in profiles}
    args.output_dir.mkdir(parents=True, exist_ok=True)
    profiles_path.write_text(json.dumps(profiles, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (args.output_dir / "TRACES.md").write_text(render(profiles), encoding="utf-8")

    # Cross-check against the Belady values recorded by generate_workloads.py.
    failures = 0
    for key, p in profiles.items():
        row = manifest.get(key.split("/", 1)[1]) if key.startswith("traces/") else None
        bounds = p["cache_bounds_read_stream"].get("tp256")
        if row and bounds and row.get("belady_hits"):
            ours = next(c["belady_hits"] for c in bounds["curve"] if c["capacity"] == int(row["belady_capacity"]))
            if ours != int(row["belady_hits"]):
                failures += 1
                print(f"MISMATCH {key}: belady_hits {ours} != manifest {row['belady_hits']}", file=sys.stderr)
        if not all(v for k, v in p["provenance"].items() if k.startswith("sha256_matches_")):
            failures += 1
            print(f"MISMATCH {key}: sha256/provenance check failed", file=sys.stderr)
    print(f"wrote {args.output_dir / 'TRACES.md'} and {profiles_path} ({len(profiles)} traces, {failures} check failures)",
          file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
