#!/usr/bin/env python3
"""Run a stage of the baseline matrix defined in experiments/configs/<stage>.json.

    run_matrix.py STAGE [--jobs N] [--dry-run] [--only-ftl F] [--only-workload W]

A stage config lists FTLs, traces, precondition modes and (seed, repeat)
pairs. Every combination becomes one run_femu.py invocation with a stable
run ID (<stage>-<ftl>-<workload>-<precondition>-s<seed>-r<repeat>), so an
interrupted stage resumes: completed runs are skipped and failed ones moved
to <results-dir>/_failed/ and retried once. Runs execute in parallel up to
--jobs, and a new VM only starts while the host has enough free memory
(a preconditioned 30 GiB FEMU device keeps about 35 GB resident).
After all runs, repeats with identical configuration and seed are compared
(invariant 11) with validate_run.py --compare.
"""

from __future__ import annotations

import argparse
import itertools
import json
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "experiments" / "scripts"
CONFIGS = REPO / "experiments" / "configs"


def mem_available_gib() -> float:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) / (1 << 20)
    return 0.0


def expand(config: dict, stage: str) -> list[dict]:
    traces = []
    for pattern in config["traces"]:
        matches = sorted(REPO.glob(pattern))
        if not matches:
            sys.exit(f"no trace matches {pattern}")
        traces += matches
    runs = []
    for ftl, trace, precondition in itertools.product(config["ftls"], traces, config["preconditions"]):
        plan = config.get("seeds_by_precondition", {}).get(precondition, config["seeds"])
        for item in plan:
            seed, repeat = item["seed"], item["repeat"]
            workload = trace.name.removesuffix(".trace")
            runs.append({"stage": stage, "ftl": ftl, "trace": trace, "workload": workload,
                         "precondition": precondition, "seed": seed, "repeat": repeat,
                         "run_id": f"{stage}-{ftl}-{workload}-{precondition}-s{seed}-r{repeat}"})
    return runs


def status(results: Path, run: dict) -> str:
    manifest = results / run["run_id"] / "manifest.json"
    if not manifest.exists():
        return "todo"
    data = json.loads(manifest.read_text())
    return "done" if data.get("status") == "completed" and (results / run["run_id"] / "validation.json").exists() \
        else "failed"


def execute(run: dict, args, results: Path) -> tuple[dict, int]:
    while mem_available_gib() < args.min_free_gib:
        time.sleep(30)
    cmd = [sys.executable, str(SCRIPTS / "run_femu.py"), "--ftl", run["ftl"], "--trace", str(run["trace"]),
           "--seed", str(run["seed"]), "--precondition", run["precondition"], "--repeat", str(run["repeat"]),
           "--run-id", run["run_id"], "--results-dir", str(results)]
    if args.qemu_dir:
        cmd += ["--qemu", str(Path(args.qemu_dir) / run["ftl"] / "qemu-system-x86_64")]
    log = results / "_logs"
    log.mkdir(exist_ok=True)
    with (log / f"{run['run_id']}.log").open("w") as out:
        code = subprocess.run(cmd, stdout=out, stderr=subprocess.STDOUT).returncode
    return run, code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("stage")
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument("--min-free-gib", type=float, default=60.0,
                        help="start a VM only while MemAvailable exceeds this")
    parser.add_argument("--results-dir", type=Path, default=REPO / "experiments" / "results")
    parser.add_argument("--qemu-dir", help="directory with <ftl>/qemu-system-x86_64 (default: build-ftl/<ftl>/build)")
    parser.add_argument("--only-ftl", action="append")
    parser.add_argument("--only-workload", action="append")
    parser.add_argument("--only-precondition", action="append")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    config = json.loads((CONFIGS / f"{args.stage}.json").read_text())
    runs = expand(config, args.stage)
    runs = [r for r in runs if (not args.only_ftl or r["ftl"] in args.only_ftl)
            and (not args.only_workload or r["workload"] in args.only_workload)
            and (not args.only_precondition or r["precondition"] in args.only_precondition)]
    results = args.results_dir
    results.mkdir(parents=True, exist_ok=True)
    todo = []
    for run in runs:
        state = status(results, run)
        if state == "failed":
            failed = results / "_failed"
            failed.mkdir(exist_ok=True)
            target = failed / f"{run['run_id']}-{int(time.time())}"
            if not args.dry_run:
                shutil.move(str(results / run["run_id"]), str(target))
            state = "todo"
        if state == "todo":
            todo.append(run)
    print(f"stage {args.stage}: {len(runs)} runs, {len(runs) - len(todo)} done, {len(todo)} to run")
    if args.dry_run:
        for run in todo:
            print(f"  {run['run_id']}")
        return 0

    codes = {}
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = []
        for run in todo:
            futures.append(pool.submit(execute, run, args, results))
            time.sleep(20)  # stagger boots so memory checks see the previous VM
        for future in as_completed(futures):
            run, code = future.result()
            codes[run["run_id"]] = code
            print(f"{'ok  ' if code == 0 else 'FAIL'} {run['run_id']}", flush=True)

    groups = defaultdict(list)
    for run in runs:
        if status(results, run) == "done":
            groups[(run["ftl"], run["workload"], run["precondition"], run["seed"])].append(results / run["run_id"])
    determinism_failures = 0
    for key, dirs in groups.items():
        if len(dirs) > 1:
            code = subprocess.run([sys.executable, str(SCRIPTS / "validate_run.py"), "--compare", *map(str, dirs)],
                                  capture_output=True, text=True).returncode
            determinism_failures += code != 0
            print(f"determinism {'ok  ' if code == 0 else 'FAIL'} {key}")
    failed = [k for k, c in codes.items() if c != 0]
    print(f"stage {args.stage}: {len(codes) - len(failed)}/{len(codes)} new runs valid, "
          f"{determinism_failures} determinism failures")
    return 1 if failed or determinism_failures else 0


if __name__ == "__main__":
    sys.exit(main())
