#!/usr/bin/env python3
"""Run one FEMU baseline: one FTL x one trace x one seed x one precondition mode.

Steps (BASELINE_PLAN.md Phase 4):
  1. fresh guest disk: a qcow2 overlay on the read-only pristine image
  2. boot the FTL's FEMU binary (launch_femu.sh: accel, GC-log shim, devsz, reqlog)
  3. wait for SSH, copy the guest-side scripts and the trace
  4. guest_prep.sh (no merging/readahead/scheduler, udev and multipathd stopped)
  5. precondition: none | fill (sequential write of the whole device) |
     steady (fill, then random overwrites)
  6. stats print -> baseline record (also zeroes the resettable counters)
  7. workload: replay_trace.py (O_DIRECT, QD1, trace order, read verification)
  8. stats print -> workload record
  9. shut the guest down
 10. write records.jsonl and manifest.json, then run validate_run.py

Everything for the run lands in <results-dir>/<run_id>/.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "experiments" / "scripts"
IMAGES = Path.home() / "images"
GUEST_FILES = ("replay_trace.py", "guest_prep.sh", "femu_stat.py")
STEADY_BLOCK = 64 * 1024


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Guest:
    def __init__(self, port: int, key: Path, log):
        self.port = port
        self.key = key
        self.log = log
        self.opts = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                     "-o", "LogLevel=ERROR", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                     "-i", str(key)]

    def ssh(self, command: str, timeout: float | None = None, check: bool = True) -> str:
        self.log(f"guest$ {command}")
        result = subprocess.run(["ssh", "-p", str(self.port), *self.opts, "ubuntu@127.0.0.1", command],
                                capture_output=True, text=True, timeout=timeout)
        if check and result.returncode != 0:
            raise RuntimeError(f"guest command failed ({result.returncode}): {command}\n"
                               f"{result.stdout}\n{result.stderr}")
        return result.stdout

    def scp(self, sources: list[Path], dest: str) -> None:
        subprocess.run(["scp", "-P", str(self.port), *self.opts, *map(str, sources),
                        f"ubuntu@127.0.0.1:{dest}"], check=True, capture_output=True, timeout=600)

    def wait_ready(self, qemu: subprocess.Popen, timeout: float) -> float:
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            if qemu.poll() is not None:
                raise RuntimeError(f"QEMU exited with {qemu.returncode} during boot")
            try:
                self.ssh("true", timeout=20)
                return time.monotonic() - start
            except (RuntimeError, subprocess.TimeoutExpired):
                time.sleep(4)
        raise TimeoutError("guest SSH did not come up")


def json_records(log: Path) -> list[dict]:
    records = []
    if log.exists():
        for line in log.read_text(errors="replace").splitlines():
            if line.startswith('{"ftl"'):
                records.append(json.loads(line))
    return records


def stats_print(guest: Guest, log: Path, timeout: float = 600) -> dict:
    before = len(json_records(log))
    guest.ssh("sudo python3 /tmp/femu/femu_stat.py 9", timeout=timeout)
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        records = json_records(log)
        if len(records) > before:
            return records[-1]
        time.sleep(1)
    raise TimeoutError("FEMU did not print a stats record")


def build_info(ftl: str, qemu: Path) -> dict:
    info_path = REPO / "build-ftl" / ftl / "build_info.json"
    info = json.loads(info_path.read_text()) if info_path.exists() else {}
    if info.get("binary") and Path(info["binary"]).resolve() != qemu.resolve():
        info = {}
    info["binary"] = str(qemu)
    info["binary_sha256"] = sha256(qemu)
    return info


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ftl", required=True, choices=("leaftl", "learnedftl"))
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--precondition", choices=("none", "fill", "steady"), required=True)
    parser.add_argument("--qemu", type=Path, help="default: build-ftl/<ftl>/build/qemu-system-x86_64")
    parser.add_argument("--results-dir", type=Path, default=REPO / "experiments" / "results")
    parser.add_argument("--run-id")
    parser.add_argument("--repeat", type=int, default=1, help="repeat index, recorded in the manifest")
    parser.add_argument("--devsz-mb", type=int, default=30720)
    parser.add_argument("--accel", choices=("auto", "tcg", "kvm"), default="auto")
    parser.add_argument("--steady-bytes", type=int, default=None,
                        help="random overwrite volume for steady (default: 1x device)")
    parser.add_argument("--no-reqlog", action="store_true")
    parser.add_argument("--boot-timeout", type=float, default=900)
    parser.add_argument("--keep-image", action="store_true")
    args = parser.parse_args()

    trace = args.trace.resolve()
    qemu = (args.qemu or REPO / "build-ftl" / args.ftl / "build" / "qemu-system-x86_64").resolve()
    if not qemu.exists():
        sys.exit(f"FEMU binary not found: {qemu} (run experiments/scripts/build_ftl.sh {args.ftl})")
    pristine = IMAGES / "femu-pristine.qcow2"
    key = IMAGES / "femu_id_ed25519"
    workload = trace.name.removesuffix(".trace")
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_id = args.run_id or f"{args.ftl}-{workload}-{args.precondition}-s{args.seed}-r{args.repeat}-{stamp}"
    run_dir = (args.results_dir / run_id).resolve()
    if run_dir.exists():
        sys.exit(f"run directory exists: {run_dir}")
    run_dir.mkdir(parents=True)

    progress = (run_dir / "run.log").open("a", buffering=1)

    def log(message: str) -> None:
        line = f"{dt.datetime.now().isoformat(timespec='seconds')} {message}"
        progress.write(line + "\n")
        print(f"[{run_id}] {message}", flush=True)

    manifest: dict = {
        "run_id": run_id, "ftl": args.ftl, "workload": workload, "trace": str(trace),
        "trace_sha256": sha256(trace), "seed": args.seed, "precondition": args.precondition,
        "repeat": args.repeat, "devsz_mb": args.devsz_mb, "started": dt.datetime.now().isoformat(),
        "host": platform.node(), "host_kernel": platform.release(),
        "image": str(pristine),
        "image_sha256": (IMAGES / "femu-pristine.qcow2.sha256").read_text().split()[0]
        if (IMAGES / "femu-pristine.qcow2.sha256").exists() else sha256(pristine),
        "build": build_info(args.ftl, qemu), "phases": {}, "status": "running",
    }
    write_manifest = lambda: (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    write_manifest()

    overlay = run_dir / "os.qcow2"
    subprocess.run([str(qemu.parent / "qemu-img"), "create", "-q", "-f", "qcow2", "-b", str(pristine),
                    "-F", "qcow2", str(overlay)], check=True)
    port = free_port()
    env = dict(os.environ, QEMU=str(qemu), RUN_DIR=str(run_dir), IMAGE=str(overlay),
               SSH_PORT=str(port), DEVSZ_MB=str(args.devsz_mb), ACCEL=args.accel,
               REQLOG="0" if args.no_reqlog else "1")
    femu_log = run_dir / "femu.log"
    log(f"booting {qemu} on port {port}")
    with femu_log.open("w") as out:
        qemu_proc = subprocess.Popen([str(SCRIPTS / "launch_femu.sh")], env=env, stdout=out,
                                     stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                     start_new_session=True)
    launch_env = dict(line.split("=", 1) for line in
                      (run_dir / "launch.env").read_text().split("\n") if "=" in line) \
        if (run_dir / "launch.env").exists() else {}

    guest = Guest(port, key, log)
    phases = manifest["phases"]

    def timed(name: str, func):
        start = time.monotonic()
        result = func()
        phases[name] = {"seconds": round(time.monotonic() - start, 1)}
        if isinstance(result, dict):
            phases[name]["result"] = result
        write_manifest()
        log(f"{name} done in {phases[name]['seconds']} s")
        return result

    try:
        timed("boot", lambda: {"ssh_ready_s": round(guest.wait_ready(qemu_proc, args.boot_timeout), 1)})
        launch_env = dict(line.split("=", 1) for line in
                          (run_dir / "launch.env").read_text().split("\n") if "=" in line)
        manifest["accel"] = launch_env.get("ACCEL")
        guest.ssh("mkdir -p /tmp/femu")
        guest.scp([SCRIPTS / name for name in GUEST_FILES] + [trace], "/tmp/femu/")
        prep = timed("guest_prep", lambda: json.loads(guest.ssh("sudo bash /tmp/femu/guest_prep.sh")))
        manifest["guest_kernel"] = prep.get("guest_kernel")

        replay = "sudo python3 /tmp/femu/replay_trace.py"
        if args.precondition in ("fill", "steady"):
            timed("precondition_fill", lambda: json.loads(
                guest.ssh(f"{replay} --mode fill --block {1 << 20}")))
        if args.precondition == "steady":
            volume = args.steady_bytes if args.steady_bytes is not None else prep["bytes"]
            timed("precondition_random", lambda: json.loads(guest.ssh(
                f"{replay} --mode random --block {STEADY_BLOCK} --bytes {volume} --seed {args.seed}")))

        timed("baseline_print", lambda: {"invariant_failures": stats_print(guest, femu_log)["invariant_failures"]})
        timed("workload", lambda: json.loads(guest.ssh(
            f"{replay} --mode replay --trace /tmp/femu/{trace.name} --seed {args.seed} --verify",
            check=False) or "{}"))
        timed("final_print", lambda: {"invariant_failures": stats_print(guest, femu_log)["invariant_failures"]})

        log("shutting down")
        guest.ssh("sudo systemctl poweroff", timeout=30, check=False)
        try:
            qemu_proc.wait(timeout=180)
        except subprocess.TimeoutExpired:
            log("guest did not power off; killing QEMU")
            os.killpg(qemu_proc.pid, signal.SIGTERM)
            qemu_proc.wait(timeout=30)
        manifest["status"] = "completed"
    except Exception as error:  # record the failure, then clean up
        manifest["status"] = "failed"
        manifest["error"] = f"{type(error).__name__}: {error}"
        log(f"FAILED: {manifest['error']}")
        if qemu_proc.poll() is None:
            os.killpg(qemu_proc.pid, signal.SIGTERM)
            try:
                qemu_proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(qemu_proc.pid, signal.SIGKILL)
    finally:
        manifest["finished"] = dt.datetime.now().isoformat()
        manifest["qemu_exit_code"] = qemu_proc.poll()
        records = json_records(femu_log)
        with (run_dir / "records.jsonl").open("w") as out:
            for record in records:
                out.write(json.dumps(record) + "\n")
        manifest["records"] = len(records)
        write_manifest()
        if not args.keep_image:
            overlay.unlink(missing_ok=True)

    if manifest["status"] == "completed":
        result = subprocess.run([sys.executable, str(SCRIPTS / "validate_run.py"), str(run_dir)],
                                capture_output=True, text=True)
        log(result.stdout.strip().splitlines()[-1] if result.stdout.strip() else result.stderr.strip())
        manifest["validation_exit_code"] = result.returncode
        write_manifest()
    reqlog = run_dir / "reqlog.csv"
    if reqlog.exists():
        with reqlog.open("rb") as src, gzip.open(str(reqlog) + ".gz", "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst)
        reqlog.unlink()
    log(f"status={manifest['status']} dir={run_dir}")
    return 0 if manifest["status"] == "completed" and manifest.get("validation_exit_code") == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
