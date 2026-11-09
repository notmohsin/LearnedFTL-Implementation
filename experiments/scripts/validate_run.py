#!/usr/bin/env python3
"""Validate one FEMU baseline run against experiments/docs/INVARIANTS.md.

    validate_run.py RUN_DIR                  check one run, write RUN_DIR/validation.json
    validate_run.py --compare RUN_DIR...     invariant 11: identical counters across repeats

A run directory (written by run_femu.py) holds manifest.json, records.jsonl
(FEMU stats records: [-2] = baseline print after preconditioning, [-1] = the
workload interval) and reqlog.csv[.gz] (FEMU's per-request log with "#print"
markers at each stats print).

Every check has a class: HARD (must pass, otherwise the run is excluded from
baseline claims), KNOWN (measured impact of a documented authors' bug, never
gated) or REPORT (a derived value). Exit code 0 iff every HARD check passed.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from pathlib import Path

PAGE = 4096
SECTOR = 512
SIZE_BUCKETS = 7
# counters the FTLs never zero at a stats print (since-boot values)
SINCE_BOOT = ("gc_cnt", "gc_runs", "nand_page_writes")  # authors never zero these (KI-17)
# fields that legitimately differ between repeats of the same configuration
NON_DETERMINISTIC: set[str] = set()


def size_bucket(nlpn: int) -> int:
    bucket, high = 0, 1
    while nlpn > high and bucket < SIZE_BUCKETS - 1:
        high = high * 2 + 1
        bucket += 1
    return bucket


def read_trace(path: Path) -> list[tuple[str, int, int]]:
    ops = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            fields = line.strip().replace(",", " ").split()
            if not fields or fields[0].startswith("#"):
                continue
            _, op, offset, length = fields
            ops.append((op.upper(), int(offset), int(length)))
    return ops


def read_reqlog(run_dir: Path) -> list[list[tuple[str, int, int]]] | None:
    """Return the request log cut into stats-print intervals."""
    path = run_dir / "reqlog.csv"
    if path.exists():
        handle = path.open(encoding="utf-8")
    elif (run_dir / "reqlog.csv.gz").exists():
        handle = gzip.open(run_dir / "reqlog.csv.gz", "rt", encoding="utf-8")
    else:
        return None
    segments: list[list[tuple[str, int, int]]] = [[]]
    with handle:
        for line in handle:
            if line.startswith("#print"):
                segments.append([])
                continue
            _, op, slba, nlb = line.rstrip("\n").split(",")
            segments[-1].append((op, int(slba), int(nlb)))
    return segments


def reference_model(ops, precondition: str, device_bytes: int) -> dict:
    """Invariant 10: what an exact FTL must see for this trace."""
    written = None if precondition in ("fill", "steady") else set()  # None = everything written
    if precondition == "warm":  # the trace's whole footprint was written first
        written = {lpn for _, offset, length in ops
                   for lpn in range(offset // PAGE, (offset + length) // PAGE)}
    expect = {"read_reqs": 0, "write_reqs": 0, "read_lpns": 0, "write_lpns": 0,
              "read_size_hist": [0] * SIZE_BUCKETS, "write_size_hist": [0] * SIZE_BUCKETS,
              "never_written_reads": 0}
    device_lpns = device_bytes // PAGE
    for op, offset, length in ops:
        first, count = offset // PAGE, length // PAGE
        if op == "W":
            expect["write_reqs"] += 1
            expect["write_lpns"] += count
            expect["write_size_hist"][size_bucket(count)] += 1
            if written is not None:
                written.update(range(first, first + count))
        else:
            expect["read_reqs"] += 1
            expect["read_lpns"] += count
            expect["read_size_hist"][size_bucket(count)] += 1
            for lpn in range(first, first + count):
                if lpn >= device_lpns or (written is not None and lpn not in written):
                    expect["never_written_reads"] += 1
    return expect


class Report:
    def __init__(self):
        self.checks: list[dict] = []

    def add(self, inv: str, name: str, cls: str, ok: bool | None, **details):
        status = "report" if ok is None else ("pass" if ok else "fail")
        self.checks.append({"invariant": inv, "check": name, "class": cls,
                            "status": status, **details})

    def hard(self):
        return [c for c in self.checks if c["class"] == "HARD"]


def headline(rec: dict) -> dict:
    mapped = rec["mapped_read_lpns"]
    read_lpns = rec["read_lpns"]
    hist = rec["served_reads_hist"]
    ratio = lambda num, den: (num / den) if den else None
    lookups = rec["cmt_hits"] + rec["cmt_misses"]
    out = {
        "read_lpns": read_lpns, "mapped_read_lpns": mapped,
        "unmapped_read_lpns": rec["unmapped_read_lpns"],
        "unmapped_read_share": ratio(rec["unmapped_read_lpns"], read_lpns),
        "translation_free_read_ratio": ratio(rec["buffer_hits"] + rec["served_translation_free"], mapped),
        "double_read_ratio": ratio(hist[2], mapped),
        "triple_read_ratio": ratio(hist[3], mapped),
        "quad_plus_read_ratio": ratio(hist[4], mapped),
        "flash_reads_per_mapped_read": ratio(rec["served_flash_reads"], mapped),
        "tp_reads_per_mapped_read": ratio(rec["served_tp_reads"], mapped),
        "cmt_hit_rate_mapped": ratio(rec["served_cmt_hits"], rec["served_lpns"]),
        "cmt_hit_rate_native": ratio(rec["cmt_hits"], lookups),
        "model_hit_rate": ratio(rec["model_hits"], rec["model_hits"] + rec["model_misses"]
                                + rec.get("model_corrections", 0)),
        "wasted_flash_reads_on_unmapped": rec["unmapped_flash_reads"],
        "read_path_tp_writes": rec["read_path_tp_writes"],
        "nand_reads_user": rec["nand_reads_user"],
        "nand_reads_translation": rec["nand_reads_translation"],
        "nand_reads_gc": rec["nand_reads_gc"],
        "nand_writes_user": rec["nand_writes_user"],
        "nand_writes_translation": rec["nand_writes_translation"],
        "nand_writes_gc": rec["nand_writes_gc"],
        "nand_erases": rec["nand_erases"],
        "nand_programs": rec["nand_writes_user"] + rec["nand_writes_translation"] + rec["nand_writes_gc"],
        "waf": ratio(rec["nand_writes_user"] + rec["nand_writes_translation"] + rec["nand_writes_gc"],
                     rec["write_lpns"]),
        "readcnt_mismatch": rec["readcnt_mismatch"],
        "served_stale_page": rec["served_stale_page"],
        "writes_dropped": rec.get("writes_dropped", 0),
        "dropped_write_share": ratio(rec.get("writes_dropped", 0), rec["write_lpns"]),
        "stale_read_share": ratio(rec["served_stale_page"], mapped),
        "read_lpns_with_lost_data": None,
    }
    # misprediction-charged LearnedFTL cost: each wrong model prediction costs
    # one flash read on real hardware but none in the authors' code (KI-20)
    if rec["ftl"] == "learnedftl":
        out["flash_reads_per_mapped_read_mispred_charged"] = ratio(
            rec["served_flash_reads"] + rec["model_misses"], mapped)
    else:
        out["flash_reads_per_mapped_read_mispred_charged"] = out["flash_reads_per_mapped_read"]
    # a buffer hit is served with zero flash reads; counted with single reads
    out["single_read_ratio"] = ratio(rec["buffer_hits"] + hist[1], mapped)
    return out


def validate(run_dir: Path) -> dict:
    manifest = json.loads((run_dir / "manifest.json").read_text())
    records = [json.loads(line) for line in (run_dir / "records.jsonl").read_text().splitlines() if line]
    report = Report()
    ftl = manifest["ftl"]
    report.add("9", "two stats records (baseline + workload)", "HARD", len(records) >= 2,
               records=len(records))
    if len(records) < 2:
        return finish(run_dir, manifest, report, None, None)
    base, rec = records[-2], records[-1]

    workload_phase = manifest.get("phases", {}).get("workload", {}).get("result", {})
    ops = read_trace(Path(manifest["trace"]))
    device_bytes = manifest.get("phases", {}).get("guest_prep", {}).get("result", {}).get(
        "bytes", manifest["devsz_mb"] << 20)
    expect = reference_model(ops, manifest["precondition"], device_bytes)

    # ---- 1: flow conservation -------------------------------------------------
    for key in ("read_reqs", "write_reqs", "read_lpns", "write_lpns"):
        report.add("1", f"{key} received == issued", "HARD", rec[key] == expect[key],
                   femu=rec[key], trace=expect[key])
    report.add("1", "guest replay completed with no read-verify errors", "HARD",
               workload_phase.get("verify_errors") == 0 and
               workload_phase.get("reads") == expect["read_reqs"] and
               workload_phase.get("writes") == expect["write_reqs"],
               replay=workload_phase)
    segments = read_reqlog(run_dir)
    if segments is None:
        report.add("1", "request log equals trace", "HARD", False, reason="no reqlog")
    else:
        prints = len(segments) - 1
        work = segments[prints - 1] if prints >= 2 else []
        issued = [(op, offset // SECTOR, length // SECTOR) for op, offset, length in ops]
        first_diff = next((i for i, (a, b) in enumerate(zip(work, issued)) if a != b), None)
        same = len(work) == len(issued) and first_diff is None
        detail = {"logged": len(work), "issued": len(issued), "prints_in_log": prints}
        if not same:
            index = first_diff if first_diff is not None else min(len(work), len(issued))
            detail["first_difference_at"] = index
            detail["logged_there"] = work[index] if index < len(work) else None
            detail["issued_there"] = issued[index] if index < len(issued) else None
        report.add("1", "request log equals trace (op, slba, nlb, order)", "HARD", same, **detail)
        # 9: the baseline record counts exactly the requests before the first workload request
        pre = segments[prints - 2] if prints >= 2 else []
        report.add("9", "baseline record matches its request-log interval", "HARD",
                   base["read_reqs"] == sum(1 for r in pre if r[0] == "R") and
                   base["write_reqs"] == sum(1 for r in pre if r[0] == "W"),
                   baseline_reads=base["read_reqs"], logged_reads=sum(1 for r in pre if r[0] == "R"),
                   baseline_writes=base["write_reqs"], logged_writes=sum(1 for r in pre if r[0] == "W"))
    for key in ("read_size_hist", "write_size_hist"):
        report.add("10", f"{key} == trace", "HARD", rec[key] == expect[key],
                   femu=rec[key], trace=expect[key])

    # ---- 2: read partition ----------------------------------------------------
    report.add("2", "in-FEMU read-partition checks", "HARD",
               rec["inv2_violations"] == 0 and base["inv2_violations"] == 0,
               workload=rec["inv2_violations"], baseline=base["inv2_violations"])
    report.add("2", "read_lpns == mapped + unmapped", "HARD",
               rec["read_lpns"] == rec["mapped_read_lpns"] + rec["unmapped_read_lpns"])
    report.add("2", "served_reads_hist sums to served_lpns", "HARD",
               sum(rec["served_reads_hist"]) == rec["served_lpns"],
               hist=rec["served_reads_hist"], served=rec["served_lpns"])
    report.add("2", "no served LPN with zero flash reads", "HARD", rec["served_reads_hist"][0] == 0,
               zero_read_served=rec["served_reads_hist"][0])
    if ftl == "leaftl":
        report.add("2", "pre_first_write + buffer_hits + cmt_hits + cmt_misses == read_lpns", "HARD",
                   rec["pre_first_write"] + rec["buffer_hits"] + rec["cmt_hits"] + rec["cmt_misses"]
                   == rec["read_lpns"])
        report.add("2", "terminal partition == read_lpns", "HARD",
                   rec["pre_first_write"] + rec["buffer_hits"] + rec["cmt_hit_invalid_gtd"] +
                   rec["invalid_read_cnt"] + rec["true_unmapped"] + rec["served_lpns"] == rec["read_lpns"])
        report.add("2", "model_queries == hits + corrections + misses", "HARD",
                   rec["model_queries"] == rec["model_hits"] + rec["model_corrections"] + rec["model_misses"])
        report.add("2", "mapped == buffer_hits + served", "HARD",
                   rec["mapped_read_lpns"] == rec["buffer_hits"] + rec["served_lpns"])
        report.add("2", "write_lpns == buffered + absorbed + dropped", "HARD",
                   rec["writes_buffered"] + rec["writes_absorbed"] + rec["writes_dropped"] == rec["write_lpns"])
    else:
        report.add("2", "mapped == served", "HARD", rec["mapped_read_lpns"] == rec["served_lpns"])
        report.add("2", "cmt_write_hits + cmt_write_misses == write_lpns", "HARD",
                   rec["cmt_write_hits"] + rec["cmt_write_misses"] == rec["write_lpns"])

    # ---- 3: causality ---------------------------------------------------------
    report.add("3", "served CMT hits <= CMT hits", "HARD", rec["served_cmt_hits"] <= rec["cmt_hits"])
    report.add("3", "translation-free served <= served", "HARD",
               rec["served_translation_free"] <= rec["served_lpns"])
    if ftl == "leaftl":
        report.add("3", "model_queries <= cmt_misses", "HARD", rec["model_queries"] <= rec["cmt_misses"])
        report.add("3", "unmapped_tp <= cmt_misses", "HARD", rec["unmapped_tp"] <= rec["cmt_misses"])
    else:
        report.add("3", "model tried only on CMT miss: hits+misses <= read_lpns - cmt_hits", "HARD",
                   rec["model_hits"] + rec["model_misses"] <= rec["read_lpns"] - rec["cmt_hits"])
        # a CMT miss can evict a dirty TP node whose write-back reads another
        # translation page (read-modify-write), so 3 reads are possible
        report.add("3", "LearnedFTL LPNs served with 3+ flash reads (dirty-eviction write-back)", "REPORT",
                   None, three=rec["served_reads_hist"][3], four_plus=rec["served_reads_hist"][4])

    # ---- 4: read-cost attribution ---------------------------------------------
    report.add("4", "every read-path flash read attributed to one read LPN (in FEMU)", "HARD",
               rec["inv4_violations"] == 0 and base["inv4_violations"] == 0)
    report.add("4", "read-path reads <= user + translation reads", "HARD",
               rec["read_path_flash_reads"] <= rec["nand_reads_user"] + rec["nand_reads_translation"])

    # ---- 5: write / NAND accounting -------------------------------------------
    ours = rec["nand_writes_user"] + rec["nand_writes_translation"] + rec["nand_writes_gc"]
    authors = rec["nand_page_writes"] - base["nand_page_writes"]  # since-boot counter (KI-17)
    report.add("5", "our NAND write split == authors' NAND write counter (interval delta)", "HARD",
               ours == authors, ours=ours, authors=authors)
    h = headline(rec)
    report.add("5", "WAF (NAND programs / host-written LPNs)", "REPORT", None, waf=h["waf"],
               nand_programs=h["nand_programs"], write_lpns=rec["write_lpns"])

    # ---- 6, 7, 12: in-FEMU structural checks ----------------------------------
    # 3.5 and 132 are exact in binary floating point, so those sums never drift;
    # summing 16.7 does (1.4 after 3e9 writes). A tolerance just under half the
    # per-op value still detects a single miscounted operation.
    for name, per_op, tol, ops_count in (
            ("energy_read", 3.5, 0.5, rec["nand_reads_user"] + rec["nand_reads_translation"] + rec["nand_reads_gc"]),
            ("energy_write", 16.7, 8.0, rec["nand_writes_user"] + rec["nand_writes_translation"] + rec["nand_writes_gc"]),
            ("energy_erase", 132.0, 0.5, rec["nand_erases"])):
        report.add("12", f"{name} == {per_op} x NAND ops (tolerance {tol})", "HARD",
                   abs(rec[name] - per_op * ops_count) <= tol, femu=rec[name], expected=per_op * ops_count)
    report.add("6", "in-FEMU inv6_violations == 0 (baseline and workload)", "HARD",
               rec["inv6_violations"] == 0 and base["inv6_violations"] == 0,
               workload=rec["inv6_violations"], baseline=base["inv6_violations"])
    # the in-FEMU energy check uses a fixed 0.5 tolerance, too tight for the
    # write sum after ~1e9 writes; the offline checks above are authoritative
    report.add("12", "in-FEMU energy flag (fixed 0.5 tolerance)", "REPORT", None,
               workload=rec["inv12_violations"], baseline=base["inv12_violations"])
    if ftl == "learnedftl":
        report.add("7", "in-FEMU inv7_violations == 0 (baseline and workload)", "HARD",
                   rec["inv7_violations"] == 0 and base["inv7_violations"] == 0,
                   workload=rec["inv7_violations"], baseline=base["inv7_violations"])
    else:
        # LeaFTL's GC leaves its mapping state inconsistent (KI-21..KI-23): block,
        # reverse-map and stale-entry parts of the audit are measured, not gated.
        report.add("7", "line lists consistent with their counters (baseline and workload)", "HARD",
                   rec["inv7_line_bad"] == 0 and base["inv7_line_bad"] == 0)
        report.add("7", "LeaFTL mapping/space inconsistencies after GC", "KNOWN", None,
                   blocks=rec["inv7_block_mismatches"], rmap=rec["inv7_rmap_mismatches"],
                   stale_map_entries=rec["stale_map_entries"],
                   baseline_blocks=base["inv7_block_mismatches"], baseline_rmap=base["inv7_rmap_mismatches"])

    # ---- 8: translation correctness (KNOWN) -----------------------------------
    if ftl == "leaftl":
        report.add("8", "LeaFTL prediction / lookup errors", "KNOWN", None,
                   model_corrections=rec["model_corrections"], model_misses=rec["model_misses"],
                   cmt_hit_invalid_gtd=rec["cmt_hit_invalid_gtd"],
                   invalid_but_mapped=rec["invalid_but_mapped"],
                   readcnt_mismatch=rec["readcnt_mismatch"],
                   served_stale_page=rec["served_stale_page"],
                   stale_map_entries=rec["stale_map_entries"])
        report.add("8", "LeaFTL host writes silently dropped (KI-27)", "KNOWN", None,
                   writes_dropped=rec["writes_dropped"], write_lpns=rec["write_lpns"],
                   baseline_writes_dropped=base["writes_dropped"])
    else:
        report.add("8", "LearnedFTL wrong model predictions (checked for free, KI-20)", "KNOWN", None,
                   model_misses=rec["model_misses"], readcnt_mismatch=rec["readcnt_mismatch"])
        report.add("8", "LearnedFTL never serves a read from an invalidated page", "HARD",
                   rec["served_stale_page"] == 0, served_stale_page=rec["served_stale_page"])

    # ---- 9: snapshot integrity -------------------------------------------------
    for key in SINCE_BOOT:
        report.add("9", f"{key} non-decreasing across records", "HARD",
                   all(records[i][key] <= records[i + 1][key] for i in range(len(records) - 1)))

    # ---- 10: trace-derived expectations ---------------------------------------
    never = expect["never_written_reads"]
    if ftl == "learnedftl":
        report.add("10", "unmapped reads == never-written reads", "HARD",
                   rec["unmapped_read_lpns"] == never, femu=rec["unmapped_read_lpns"], trace=never)
    else:
        report.add("10", "unmapped reads >= never-written reads", "HARD",
                   rec["unmapped_read_lpns"] >= never, femu=rec["unmapped_read_lpns"], trace=never)
    lost = rec["unmapped_read_lpns"] - never
    report.add("10", "written data the FTL did not return (unmapped - never-written)", "KNOWN", None,
               lpns=lost, share_of_written_reads=(lost / (rec["read_lpns"] - never))
               if rec["read_lpns"] > never else None)
    h["never_written_reads"] = never
    h["read_lpns_with_lost_data"] = lost
    return finish(run_dir, manifest, report, h, rec)


def finish(run_dir: Path, manifest: dict, report: Report, metrics, rec) -> dict:
    hard = report.hard()
    failed = sorted({c["invariant"] for c in hard if c["status"] == "fail"}, key=int)
    hard_ids = sorted({c["invariant"] for c in hard}, key=int)
    result = {
        "run_id": manifest["run_id"], "ftl": manifest["ftl"], "workload": manifest["workload"],
        "precondition": manifest["precondition"], "seed": manifest["seed"],
        "hard_invariants_passed": f"{len(hard_ids) - len(failed)}/{len(hard_ids)}",
        "hard_checks_passed": f"{sum(c['status'] == 'pass' for c in hard)}/{len(hard)}",
        "failed_invariant_ids": failed, "valid": not failed,
        "metrics": metrics, "checks": report.checks,
    }
    (run_dir / "validation.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def compare(run_dirs: list[Path]) -> int:
    """Invariant 11: identical counters across repeats of one configuration."""
    runs = []
    for run_dir in run_dirs:
        manifest = json.loads((run_dir / "manifest.json").read_text())
        records = [json.loads(l) for l in (run_dir / "records.jsonl").read_text().splitlines() if l]
        runs.append((run_dir, manifest, records))
    keys = ("ftl", "workload", "precondition", "seed", "devsz_mb", "trace_sha256")
    config = {k: runs[0][1][k] for k in keys}
    mismatched_config = [str(d) for d, m, _ in runs if {k: m[k] for k in keys} != config]
    differences = []
    ref = runs[0][2]
    for run_dir, _, records in runs[1:]:
        if len(records) != len(ref):
            differences.append({"run": str(run_dir), "records": len(records), "expected": len(ref)})
            continue
        for index, (a, b) in enumerate(zip(ref[-2:], records[-2:])):
            for key in a:
                if key in NON_DETERMINISTIC:
                    continue
                if a[key] != b.get(key):
                    differences.append({"run": str(run_dir), "record": ["baseline", "workload"][index],
                                        "field": key, "reference": a[key], "value": b.get(key)})
    ok = not differences and not mismatched_config
    result = {"invariant": "11", "class": "HARD", "status": "pass" if ok else "fail",
              "config": config, "runs": [str(d) for d, _, _ in runs],
              "config_mismatch": mismatched_config, "differences": differences[:50],
              "difference_count": len(differences)}
    print(json.dumps(result, indent=2))
    for run_dir, _, _ in runs:
        (run_dir / "determinism.json").write_text(json.dumps(result, indent=2) + "\n")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run_dirs", type=Path, nargs="+")
    parser.add_argument("--compare", action="store_true")
    args = parser.parse_args()
    if args.compare:
        return compare(args.run_dirs)
    status = 0
    for run_dir in args.run_dirs:
        result = validate(run_dir)
        for check in result["checks"]:
            if check["status"] == "fail":
                print(f"  FAIL inv{check['invariant']} {check['check']}: "
                      f"{ {k: v for k, v in check.items() if k not in ('invariant', 'check', 'class', 'status')} }")
        print(f"{result['run_id']}: HARD invariants {result['hard_invariants_passed']} "
              f"(checks {result['hard_checks_passed']}), failed={result['failed_invariant_ids']}")
        status |= 0 if result["valid"] else 1
    return status


if __name__ == "__main__":
    sys.exit(main())
