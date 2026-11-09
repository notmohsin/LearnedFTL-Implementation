#!/usr/bin/env python3
"""Aggregate validated baseline runs into the mentor-facing CSVs.

    aggregate.py [--results-dir experiments/results]

Reads every <results-dir>/<run_id>/ written by run_femu.py (manifest.json,
records.jsonl, validation.json, optional determinism.json) and writes, in
<results-dir>/:

  baseline_runs.csv             one row per run
  baseline_summary.csv          one row per (FTL x workload x trace length x precondition),
                                mean / sd / 95% CI over repeats
  baseline_summary_columns.csv  data dictionary: column, unit, definition, source

Everything is regenerated from the raw run directories; nothing is hand-edited.
Runs with failed HARD invariants are kept (with the failure shown) but flagged
in `notes`. Latency and throughput are blank for TCG runs, never estimated.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PROFILES = REPO / "experiments" / "traces" / "profiles.json"
GIB = 1 << 30

FTL_CONFIG = {
    "leaftl": {"cmt_entry_type": "learned segment per translation page (256 LPNs)",
               "profile_keying": "tp256", "paper_reference": "LeaFTL ASPLOS'23; LearnedFTL HPCA'24 Fig. 14 (LeaFTL bars)"},
    "learnedftl": {"cmt_entry_type": "one LPN->PPN mapping (TP-grouped, 512 LPNs per TP)",
                   "profile_keying": "lpn", "paper_reference": "LearnedFTL HPCA'24 Figs. 13-14"},
}
NAND_US = {"read": 40, "write": 200, "erase": 2000}  # leaftl.h:17-19, ld-tpftl.h, LearnedFTL paper
RAW_GIB = 32

MAPPING_METRICS = [
    ("translation_free_read_ratio", "fraction",
     "Mapped read LPNs served without any translation-page flash read (buffer hit, CMT hit, model hit) / mapped read LPNs."),
    ("cmt_hit_rate_mapped", "fraction",
     "Served (mapped, read-from-flash) LPNs whose CMT lookup hit / served LPNs. Comparable definition for both FTLs, but a LeaFTL entry covers a whole translation page and a LearnedFTL entry one LPN."),
    ("cmt_hit_rate_native", "fraction",
     "The FTL's own cmt_hits / (cmt_hits + cmt_misses), including lookups for unmapped LPNs. Not comparable across FTLs."),
    ("model_hit_rate", "fraction",
     "LeaFTL: model hits / model queries (after a TP read). LearnedFTL: correct predictions / predictions tried (before a TP read)."),
    ("single_read_ratio", "fraction", "Mapped read LPNs served with exactly 1 flash read (or 0, buffer hit) / mapped read LPNs."),
    ("double_read_ratio", "fraction", "Mapped read LPNs served with exactly 2 flash reads / mapped read LPNs."),
    ("triple_read_ratio", "fraction", "Mapped read LPNs served with exactly 3 flash reads / mapped read LPNs (LeaFTL only)."),
    ("quad_plus_read_ratio", "fraction", "Mapped read LPNs served with 4 or more flash reads / mapped read LPNs."),
    ("flash_reads_per_mapped_read", "flash reads / LPN",
     "All flash reads issued while serving mapped read LPNs (TP, mispredicted and data reads, incl. eviction write-back reads) / mapped read LPNs."),
    ("flash_reads_per_mapped_read_mispred_charged", "flash reads / LPN",
     "As flash_reads_per_mapped_read, but each LearnedFTL wrong model prediction is charged one flash read, as LeaFTL's are (KI-20)."),
    ("tp_reads_per_mapped_read", "flash reads / LPN", "Translation-page reads while serving mapped read LPNs / mapped read LPNs."),
    ("unmapped_read_share", "fraction", "Read LPNs the FTL did not resolve to written data / all read LPNs."),
    ("stale_read_share", "fraction",
     "Mapped read LPNs served from a page that had already been invalidated (stale data; LeaFTL write-buffer window) / mapped read LPNs."),
    ("lost_data_read_share", "fraction",
     "Read LPNs whose data had been written but which the FTL did not return / read LPNs of written data (KNOWN, LeaFTL KI-5)."),
    ("gap_to_belady", "fraction",
     "LeaFTL only: Belady-OPT hit rate at 8192 entries on the trace's read stream keyed per 256-LPN translation page, minus cmt_hit_rate_mapped. Headroom for a better replacement policy. Blank for LearnedFTL, whose CMT also fills on writes and prefetches, so demand-fill bounds do not bound it."),
]
DEVICE_METRICS = [
    ("nand_reads_user", "pages", "User-data flash reads in the workload interval, incl. mispredicted reads."),
    ("nand_reads_translation", "pages", "Translation-page flash reads in the workload interval (read path, write path and GC)."),
    ("nand_reads_gc", "pages", "Flash reads of user data migrated by GC."),
    ("nand_programs", "pages", "NAND programs: user data + translation pages + GC-migrated data (see KNOWN_ISSUES for uncharged cases)."),
    ("waf", "ratio", "nand_programs / host-written LPNs in the workload interval."),
    ("gc_runs", "count", "GC invocations that reclaimed a victim during the workload interval (delta of the since-boot counter)."),
    ("nand_erases", "blocks", "Block erases in the workload interval."),
    ("energy_mj", "mJ", "read/write/erase energy from the authors' per-op constants (3.5/16.7/132 uJ), workload interval."),
    ("latency_p50_us", "us", "Per-request latency P50. Blank: TCG runs, latency needs KVM on exclusive cores (KI-19)."),
    ("latency_p99_us", "us", "Per-request latency P99. Blank under TCG."),
    ("latency_p999_us", "us", "Per-request latency P99.9. Blank under TCG."),
    ("throughput_mib_s", "MiB/s", "Workload throughput. Blank under TCG."),
]


def ci95(values: list[float]) -> float | None:
    n = len(values)
    if n < 2:
        return None
    t = {2: 12.706, 3: 4.303, 4: 3.182, 5: 2.776, 6: 2.571, 7: 2.447, 8: 2.365, 9: 2.306, 10: 2.262}.get(n, 1.96)
    return t * statistics.stdev(values) / math.sqrt(n)


def fmt(value):
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.6g}"
    if isinstance(value, (list, dict)):
        return json.dumps(value, separators=(",", ":"))
    return str(value)


def load_runs(results_dir: Path) -> list[dict]:
    runs = []
    for run_dir in sorted(p for p in results_dir.iterdir() if p.is_dir() and not p.name.startswith("_")):
        if not (run_dir / "validation.json").exists():
            continue
        manifest = json.loads((run_dir / "manifest.json").read_text())
        validation = json.loads((run_dir / "validation.json").read_text())
        records = [json.loads(l) for l in (run_dir / "records.jsonl").read_text().splitlines() if l]
        determinism = json.loads((run_dir / "determinism.json").read_text()) \
            if (run_dir / "determinism.json").exists() else None
        runs.append({"dir": run_dir, "manifest": manifest, "validation": validation,
                     "records": records, "determinism": determinism})
    return runs


def trace_profile(profiles: dict, trace: str) -> dict | None:
    rel = Path(trace).resolve().relative_to(REPO / "experiments")
    return profiles.get(f"{rel.parent}/{rel.stem}")


def belady_8192(profile: dict | None, keying: str):
    if not profile:
        return None
    for point in profile["cache_bounds_read_stream"][keying]["curve"]:
        if point["capacity"] == 8192:
            return point
    return None


def run_row(run: dict, profiles: dict) -> dict:
    m, v, recs = run["manifest"], run["validation"], run["records"]
    rec, base = recs[-1], recs[-2]
    metrics = v["metrics"] or {}
    ftl = m["ftl"]
    prof = trace_profile(profiles, m["trace"])
    bel = belady_8192(prof, FTL_CONFIG[ftl]["profile_keying"])
    written_reads = (rec["read_lpns"] - metrics.get("never_written_reads", 0)) if metrics else None
    row = {
        "run_id": m["run_id"], "ftl": ftl, "workload": m["workload"],
        "trace_ops": prof["operations"] if prof else None, "precondition": m["precondition"],
        "seed": m["seed"], "repeat": m.get("repeat", 1), "accel": m.get("accel"),
        "ents_per_tp": rec["ents_per_tp"], "cmt_entries": rec["cmt_entries"],
        "logical_capacity_gib": m["devsz_mb"] / 1024,
        "valid": v["valid"], "hard_invariants_passed": v["hard_invariants_passed"],
        "failed_invariant_ids": ";".join(v["failed_invariant_ids"]),
    }
    for name, _, _ in MAPPING_METRICS:
        row[name] = metrics.get(name)
    row["lost_data_read_share"] = (metrics.get("read_lpns_with_lost_data") / written_reads) \
        if metrics and written_reads else None
    # only LeaFTL's CMT is a demand-filled cache of one entry per translation page;
    # LearnedFTL also fills on writes and prefetches, so no demand-fill bound applies
    row["gap_to_belady"] = (bel["belady_hit_rate"] - metrics["cmt_hit_rate_mapped"]) \
        if ftl == "leaftl" and bel and metrics.get("cmt_hit_rate_mapped") is not None else None
    for name in ("nand_reads_user", "nand_reads_translation", "nand_reads_gc", "nand_programs", "waf", "nand_erases"):
        row[name] = metrics.get(name)
    row["gc_runs"] = rec["gc_runs"] - base["gc_runs"]
    row["energy_mj"] = (3.5 * (rec["nand_reads_user"] + rec["nand_reads_translation"] + rec["nand_reads_gc"])
                        + 16.7 * metrics.get("nand_programs", 0) + 132 * rec["nand_erases"]) / 1000
    for name in ("latency_p50_us", "latency_p99_us", "latency_p999_us", "throughput_mib_s"):
        row[name] = None  # TCG: never reported
    row["read_lpns"], row["mapped_read_lpns"] = rec["read_lpns"], rec["mapped_read_lpns"]
    row["readcnt_mismatch"], row["model_mispredictions"] = rec["readcnt_mismatch"], rec["model_misses"]
    flow = [c for c in v["checks"] if c["check"].startswith("request log equals trace")]
    row["request_flow_match"] = "yes" if flow and flow[0]["status"] == "pass" else "no"
    row["deterministic_across_repeats"] = (
        "yes" if run["determinism"]["status"] == "pass" else "no") if run["determinism"] else "n.a."
    build = m.get("build", {})
    row.update({"repo_commit": build.get("commit"), "binary_sha256": build.get("binary_sha256"),
                "trace_sha256": m["trace_sha256"], "image_sha256": m.get("image_sha256"),
                "compiler": build.get("compiler"), "host_kernel": m.get("host_kernel"),
                "guest_kernel": m.get("guest_kernel"), "run_date": m["started"][:10],
                "trace": str(Path(m["trace"]).resolve().relative_to(REPO))})
    row["_profile"] = prof
    row["_belady"] = bel
    return row


def summarize(rows: list[dict]) -> list[dict]:
    groups = defaultdict(list)
    for row in rows:
        groups[(row["ftl"], row["workload"], row["trace_ops"], row["precondition"])].append(row)
    out = []
    for (ftl, workload, ops, precondition), members in sorted(groups.items(), key=lambda kv: (
            kv[0][1], kv[0][2] or 0, kv[0][3], kv[0][0])):
        first = members[0]
        prof, bel = first["_profile"], first["_belady"]
        valid = [r for r in members if r["valid"]]
        use = valid or members
        s = {
            "ftl": ftl, "workload": workload,
            "workload_description": describe(prof),
            "trace_ops": ops, "precondition": precondition,
            "repeats": len(members), "seeds": ";".join(sorted({str(r["seed"]) for r in members})),
            "ents_per_tp": first["ents_per_tp"], "cmt_entries": first["cmt_entries"],
            "cmt_entry_type": FTL_CONFIG[ftl]["cmt_entry_type"],
            "raw_capacity_gib": RAW_GIB, "logical_capacity_gib": first["logical_capacity_gib"],
            "op_percent": round(100 * (RAW_GIB - first["logical_capacity_gib"]) / first["logical_capacity_gib"], 2),
            "nand_read_us": NAND_US["read"], "nand_write_us": NAND_US["write"], "nand_erase_us": NAND_US["erase"],
            "accel": ";".join(sorted({str(r["accel"]) for r in members})),
        }
        if prof:
            s.update({
                "trace_read_percent": prof["mix"]["read_percent_by_requests"],
                "trace_mean_request_kib": prof["mix"]["mean_request_kib"],
                "trace_footprint_gib": prof["footprint"]["footprint_gib"],
                "trace_unique_tp256": prof["footprint"]["unique_tp256"],
                "trace_unique_tp512": prof["footprint"]["unique_tp512"],
                "trace_sequential_request_share": prof["locality"]["sequential_request_share"],
                "trace_top20pct_lpn_access_share": prof["locality"]["top20pct_lpn_access_share"],
                "trace_read_after_write_share": prof["mapping"][
                    "read_after_write_share_precondition_" + ("none" if precondition == "none" else "fill")],
                "bound_belady_hit_rate_8192": bel["belady_hit_rate"] if bel else None,
                "bound_lru_hit_rate_8192": bel["lru_hit_rate"] if bel else None,
            })
        for name, _, _ in MAPPING_METRICS:
            values = [r[name] for r in use if r[name] is not None]
            s[f"{name}_mean"] = statistics.fmean(values) if values else None
            s[f"{name}_sd"] = statistics.stdev(values) if len(values) > 1 else (0.0 if values else None)
            s[f"{name}_ci95"] = ci95(values)
        for name, _, _ in DEVICE_METRICS:
            values = [r[name] for r in use if r[name] is not None]
            s[name] = statistics.fmean(values) if values else None
        s["runs_valid"] = f"{len(valid)}/{len(members)}"
        s["hard_invariants_passed"] = min((r["hard_invariants_passed"] for r in members),
                                          key=lambda x: int(x.split("/")[0]))
        s["failed_invariant_ids"] = ";".join(sorted({i for r in members for i in r["failed_invariant_ids"].split(";") if i},
                                                    key=int))
        s["known_stale_read_share"] = s["stale_read_share_mean"]
        s["known_lost_data_read_share"] = s["lost_data_read_share_mean"]
        s["known_readcnt_mismatch"] = statistics.fmean([r["readcnt_mismatch"] for r in use])
        s["known_model_mispredictions"] = statistics.fmean([r["model_mispredictions"] for r in use])
        determinism = {r["deterministic_across_repeats"] for r in members}
        s["deterministic_across_repeats"] = "yes" if determinism == {"yes"} else (
            "no" if "no" in determinism else "n.a.")
        s["request_flow_match"] = "yes" if all(r["request_flow_match"] == "yes" for r in members) else "no"
        s["paper_reference"] = FTL_CONFIG[ftl]["paper_reference"]
        s["paper_trend_consistent"] = None  # filled in by trend_check()
        s["repo_commit"] = ";".join(sorted({str(r["repo_commit"]) for r in members}))
        s["binary_sha256"] = ";".join(sorted({str(r["binary_sha256"]) for r in members}))
        s["trace_sha256"] = first["trace_sha256"]
        s["image_sha256"] = first["image_sha256"]
        s["compiler"] = first["compiler"]
        s["host_kernel"] = first["host_kernel"]
        s["guest_kernel"] = first["guest_kernel"]
        s["run_dates"] = ";".join(sorted({r["run_date"] for r in members}))
        s["notes"] = notes(s, members)
        out.append(s)
    trend_check(out)
    return out


def describe(prof: dict | None) -> str:
    if not prof:
        return ""
    prov = prof["provenance"]
    parts = []
    if prov.get("pattern"):
        parts.append(prov["pattern"])
    parts.append(f"{prof['mix']['read_percent_by_requests']:.0f}% reads")
    parts.append(f"{prof['footprint']['footprint_gib']:.2f} GiB footprint")
    return ", ".join(parts)


def notes(s: dict, members: list[dict]) -> str:
    out = ["TCG run: counters only, latency/throughput not measured"]
    if s["precondition"] == "none":
        out.append(f"no preconditioning: {100 * (1 - (s.get('trace_read_after_write_share') or 0)):.1f}% "
                   "of read LPNs target never-written space")
    if s["runs_valid"].split("/")[0] != s["runs_valid"].split("/")[1]:
        out.append(f"HARD invariant failure in some runs (ids {s['failed_invariant_ids']}): excluded from claims")
    if s["ftl"] == "leaftl" and (s.get("stale_read_share_mean") or 0) > 0:
        out.append("LeaFTL serves some reads from invalidated pages (write-buffer window, KNOWN)")
    if s["deterministic_across_repeats"] == "n.a.":
        out.append("single run, determinism shown on the 100k validation stage")
    return "; ".join(out)


def trend_check(summary: list[dict]) -> None:
    """LearnedFTL HPCA'24 claim: fewer double reads than LeaFTL on the same workload."""
    by_key = {(s["workload"], s["trace_ops"], s["precondition"], s["ftl"]): s for s in summary}
    for s in summary:
        other = by_key.get((s["workload"], s["trace_ops"], s["precondition"],
                            "leaftl" if s["ftl"] == "learnedftl" else "learnedftl"))
        if not other or s["double_read_ratio_mean"] is None or other["double_read_ratio_mean"] is None:
            s["paper_trend_consistent"] = "n.a."
            continue
        learned, lea = (s, other) if s["ftl"] == "learnedftl" else (other, s)
        s["paper_trend_consistent"] = "yes" if learned["double_read_ratio_mean"] <= lea["double_read_ratio_mean"] else "no"


def dictionary() -> list[dict]:
    d = []
    add = lambda col, unit, definition, source: d.append(
        {"column": col, "unit": unit, "definition": definition, "source": source})
    add("ftl", "", "FTL under test: leaftl (hw/femu/bbssd/leaftl.c) or learnedftl (hw/femu/bbssd/ld-tpftl.c).", "manifest.json")
    add("workload", "", "Trace name (experiments/traces/TRACES.md).", "manifest.json")
    add("workload_description", "", "Generator pattern, read share and footprint of the trace.", "traces/profiles.json")
    add("trace_ops", "requests", "Requests in the replayed trace (100k validation stage, 1M matrix stage).", "traces/profiles.json")
    add("precondition", "", "none: empty device; fill: whole 30 GiB written sequentially first; steady: fill + 30 GiB of random 64 KiB overwrites.", "run_femu.py")
    add("repeats", "count", "Runs aggregated in this row.", "results/<run>/")
    add("seeds", "", "Seeds used (write pattern and steady-state overwrite order).", "manifest.json")
    add("ents_per_tp", "LPNs", "Mappings per translation page (native constant, not changed).", "FEMU record (leaftl.c:502 / ld-tpftl.c:801)")
    add("cmt_entries", "entries", "CMT capacity (native constant, not changed).", "FEMU record (leaftl.c:505 / ld-tpftl.c:809)")
    add("cmt_entry_type", "", "What one CMT entry holds.", "METRICS.md section 2")
    add("raw_capacity_gib", "GiB", "Raw flash: 8 ch x 8 LUN x 256 blocks x 512 pages x 4 KiB.", "FTL geometry")
    add("logical_capacity_gib", "GiB", "Device size exposed to the guest (devsz_mb).", "launch_femu.sh")
    add("op_percent", "%", "Over-provisioning: (raw - logical) / logical.", "derived")
    add("nand_read_us", "us", "NAND page read latency constant.", "leaftl.h:17 / ld-tpftl.h")
    add("nand_write_us", "us", "NAND page program latency constant.", "leaftl.h:18 / ld-tpftl.h")
    add("nand_erase_us", "us", "NAND block erase latency constant.", "leaftl.h:19 / ld-tpftl.h")
    add("accel", "", "QEMU accelerator: tcg (software) or kvm. Latency is only valid under kvm.", "launch.env")
    add("trace_read_percent", "%", "Read requests / all requests.", "traces/profiles.json mix")
    add("trace_mean_request_kib", "KiB", "Mean request size.", "traces/profiles.json mix")
    add("trace_footprint_gib", "GiB", "Distinct LPNs touched x 4 KiB.", "traces/profiles.json footprint")
    add("trace_unique_tp256", "TPs", "Distinct translation pages touched at 256 LPNs per TP (LeaFTL keying).", "traces/profiles.json footprint")
    add("trace_unique_tp512", "TPs", "Distinct translation pages touched at 512 LPNs per TP (LearnedFTL keying).", "traces/profiles.json footprint")
    add("trace_sequential_request_share", "fraction", "Requests starting where the previous one ended.", "traces/profiles.json locality")
    add("trace_top20pct_lpn_access_share", "fraction", "Accesses to the hottest 20% of LPNs.", "traces/profiles.json locality")
    add("trace_read_after_write_share", "fraction", "Read LPNs whose address was written earlier (under this precondition).", "traces/profiles.json mapping")
    add("bound_belady_hit_rate_8192", "fraction", "Belady-OPT hit rate at 8192 entries on the read stream with demand fill, keyed like the FTL's CMT entries (tp256 for LeaFTL, lpn for LearnedFTL). A reference, not a bound, for LearnedFTL (write fill and prefetch).", "experiments/tools/cachesim.c via profiles.json")
    add("bound_lru_hit_rate_8192", "fraction", "LRU hit rate at 8192 entries, same keying.", "experiments/tools/cachesim.c via profiles.json")
    for name, unit, definition in MAPPING_METRICS:
        add(f"{name}_mean", unit, definition + " Mean over valid repeats.", "validation.json metrics (FEMU record)")
        add(f"{name}_sd", unit, f"Sample standard deviation of {name} over repeats.", "derived")
        add(f"{name}_ci95", unit, f"Half-width of the 95% t-interval of {name} (blank for one run).", "derived")
    for name, unit, definition in DEVICE_METRICS:
        add(name, unit, definition + " Mean over valid repeats.", "validation.json metrics (FEMU record)")
    add("runs_valid", "count", "Runs with every HARD invariant passing / runs.", "validation.json")
    add("hard_invariants_passed", "count", "Lowest passed/total HARD invariant count among the runs (INVARIANTS.md).", "validation.json")
    add("failed_invariant_ids", "", "HARD invariant IDs that failed in any run.", "validation.json")
    add("known_stale_read_share", "fraction", "Same as stale_read_share_mean: measured impact of LeaFTL's write-buffer window (KNOWN).", "INVARIANTS.md 8")
    add("known_lost_data_read_share", "fraction", "Same as lost_data_read_share_mean (KNOWN).", "INVARIANTS.md 10")
    add("known_readcnt_mismatch", "LPNs", "Served LPNs where the authors' read_cnt differs from the flash reads actually issued (mean per run).", "FEMU record")
    add("known_model_mispredictions", "count", "Wrong model predictions per run (LearnedFTL: caught at no flash cost, KI-20).", "FEMU record")
    add("deterministic_across_repeats", "", "yes if repeats produced identical counters (invariant 11), n.a. for a single run.", "determinism.json")
    add("request_flow_match", "", "yes if FEMU's request log equalled the trace in every run (invariant 1).", "validation.json")
    add("paper_reference", "", "Paper result this row relates to.", "papers/")
    add("paper_trend_consistent", "", "yes if LearnedFTL's double-read ratio <= LeaFTL's for this workload/precondition, as claimed in LearnedFTL HPCA'24.", "derived")
    add("repo_commit", "", "Git commit the FEMU binary was built from.", "build-ftl/<ftl>/build_info.json")
    add("binary_sha256", "", "SHA-256 of the FEMU binary.", "build_info.json")
    add("trace_sha256", "", "SHA-256 of the replayed trace.", "manifest.json")
    add("image_sha256", "", "SHA-256 of the pristine guest image (a fresh overlay per run).", "~/images/femu-pristine.qcow2.sha256")
    add("compiler", "", "Compiler used to build FEMU.", "build_info.json")
    add("host_kernel", "", "Host kernel release.", "manifest.json")
    add("guest_kernel", "", "Guest kernel release.", "guest_prep.sh")
    add("run_dates", "", "Dates the runs started.", "manifest.json")
    add("notes", "", "Plain-language caveats for the row.", "aggregate.py")
    return d


RUN_COLUMNS_EXTRA = ["run_id", "seed", "repeat", "valid", "read_lpns", "mapped_read_lpns",
                     "readcnt_mismatch", "model_mispredictions", "trace", "run_date"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--results-dir", type=Path, default=REPO / "experiments" / "results")
    args = parser.parse_args()
    profiles = json.loads(PROFILES.read_text())
    runs = load_runs(args.results_dir)
    if not runs:
        sys.exit(f"no validated runs in {args.results_dir}")
    rows = [run_row(run, profiles) for run in runs]
    summary = summarize(rows)

    columns = dictionary()
    names = [c["column"] for c in columns]
    missing = [k for k in summary[0] if k not in names]
    unused = [n for n in names if n not in summary[0]]
    if missing or unused:  # every summary column must be documented, and vice versa
        sys.exit(f"data dictionary out of sync: undocumented={missing} unused={unused}")

    with (args.results_dir / "baseline_summary.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=names)
        writer.writeheader()
        for s in summary:
            writer.writerow({k: fmt(s.get(k)) for k in names})
    with (args.results_dir / "baseline_summary_columns.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["column", "unit", "definition", "source"])
        writer.writeheader()
        writer.writerows(columns)
        for extra, unit, definition in (
                ("run_id", "", "Run directory name in experiments/results/ (baseline_runs.csv only)."),
                ("seed", "", "Seed of this run (baseline_runs.csv only)."),
                ("repeat", "", "Repeat index (baseline_runs.csv only)."),
                ("valid", "", "True if every HARD invariant passed (baseline_runs.csv only)."),
                ("read_lpns", "LPNs", "Read LPNs in the workload interval (baseline_runs.csv only)."),
                ("mapped_read_lpns", "LPNs", "Read LPNs resolved to written data (baseline_runs.csv only)."),
                ("readcnt_mismatch", "LPNs", "See known_readcnt_mismatch (baseline_runs.csv only)."),
                ("model_mispredictions", "count", "See known_model_mispredictions (baseline_runs.csv only)."),
                ("trace", "", "Replayed trace path (baseline_runs.csv only)."),
                ("run_date", "", "Date the run started (baseline_runs.csv only)."),
                ("gc_runs", "count", "(per run in baseline_runs.csv)")):
            if extra not in names:
                writer.writerow({"column": extra, "unit": unit, "definition": definition, "source": "manifest/records"})
    run_cols = ["run_id", "ftl", "workload", "trace_ops", "precondition", "seed", "repeat", "accel",
                "ents_per_tp", "cmt_entries", "logical_capacity_gib", "valid", "hard_invariants_passed",
                "failed_invariant_ids"] + [n for n, _, _ in MAPPING_METRICS] + \
               [n for n, _, _ in DEVICE_METRICS] + ["read_lpns", "mapped_read_lpns", "readcnt_mismatch",
                                                    "model_mispredictions", "request_flow_match",
                                                    "deterministic_across_repeats", "repo_commit",
                                                    "binary_sha256", "trace_sha256", "image_sha256", "compiler",
                                                    "host_kernel", "guest_kernel", "run_date", "trace"]
    with (args.results_dir / "baseline_runs.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=run_cols)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: fmt(row.get(k)) for k in run_cols})
    print(f"{len(rows)} runs -> {len(summary)} summary rows in {args.results_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
