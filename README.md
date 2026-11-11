# LearnedFTL / LeaFTL baselines on FEMU

This repository measures how learned address-mapping models change mapping-cache (CMT) behaviour in flash SSDs. It compares **LeaFTL** (ASPLOS '23) and **LearnedFTL** (HPCA '24) on the [FEMU](https://github.com/vtess/FEMU) SSD emulator. It is a fork of the LearnedFTL authors' artifact, [astlxmu/LearnedFTL](https://github.com/astlxmu/LearnedFTL), with these additions:

- **Measurement instrumentation** in both FTLs. It adds request-flow counters, an exhaustive per-LPN read partition with built-in invariant checks, one shared JSON stats schema, and an optional per-request log.
- **An experiment toolkit**: deterministic synthetic traces, trace profiling with exact cache bounds (LRU, LFU, Belady-OPT), and replay and stats scripts.
- **Written analysis** of the authors' code: exact metric definitions, and 31 documented behaviours that affect measurements.
- **Validated baselines**: a one-command run pipeline, a validator with 12 invariants, and generated result CSVs. Start at [experiments/README.md](experiments/README.md).
- **A run path for machines without root or KVM**: QEMU's software emulator (TCG), a cloud-init guest image, and a preload shim for a hard-coded log path.

> **Ground rule:** the authors' FTL decision logic and constants are unchanged. Every change in `hw/femu/bbssd/` only counts or prints. Bugs in the authors' code are documented and measured, never fixed. See [KNOWN_ISSUES.md](experiments/docs/KNOWN_ISSUES.md). Two declared exceptions, neither of which changes an FTL decision: LearnedFTL's `model_hit_num` now counts correct predictions only ([METRICS.md](experiments/docs/METRICS.md) section 3), and FEMU continues when it cannot pin its memory (`hw/femu/backend/dram.c`).

## Status (2026-11-11)

| | |
|---|---|
| ✅ Done | Metric definitions and 31 known issues. Trace suite with profiles and cache bounds. Instrumentation in both FTLs. One build per FTL, deterministic `O_DIRECT` replay, a validator with 12 invariants, and generated CSVs. |
| ✅ Measured | 190 runs, all valid: 12 micro, 144 validation (100k requests), 24 matrix (1M requests), 10 real (UMass). Counters only (TCG). |
| ⛔ Blocked | Latency and throughput. They need KVM, and this host has no KVM access. Counter-based results don't depend on time and are unaffected. |

Headline results (`warm` precondition, 1M requests; details in [BASELINE_PLAN.md](experiments/docs/BASELINE_PLAN.md)):

- LearnedFTL needs 1.06 to 1.38 flash reads per read. LeaFTL needs 1.49 to 2.75, counted over the reads it served.
- LeaFTL, as ported by the LearnedFTL authors, does not return 10% to 88% of the reads of written data (KI-27, KI-28, KI-31). Always quote this next to any LeaFTL number.
- LeaFTL's distance to the Belady-OPT cache bound comes from these failed lookups, not from cache misses. The runs do not show headroom for a better cache replacement policy on this LeaFTL port.

The numbers are in `experiments/results/baseline_summary.csv`; `baseline_summary_columns.csv` explains every column.

## Three things to know before trusting any number

1. **Raw CMT hit rates can't be compared across the two FTLs.** A LeaFTL CMT entry is a learned segment that covers up to 256 LPNs. A LearnedFTL entry is a single LPN→PPN mapping. Cross-FTL comparison uses the *translation-free read ratio* and the *double-read ratio* instead. See [METRICS.md](experiments/docs/METRICS.md).
2. **Reads of unwritten space distort LeaFTL's hit rate.** LeaFTL counts those reads as CMT misses, but LearnedFTL excludes them. The old 0.03% LeaFTL hit rate on `seq-mixed` came from this: that trace never reads data it wrote. Baselines therefore precondition the device: the main condition, `warm`, writes every page the trace touches once before the trace starts (KI-3).
3. **LearnedFTL checks model predictions against the true page map at no flash cost.** Its double-read advantage may therefore be overstated. Results report flash reads per LPN both as coded and with an extra read charged for each wrong prediction (KI-20).

## Repository layout

The FTLs live in `hw/femu/bbssd/`. Only one is compiled at a time.

| FTL | Source | Built by default | In scope |
|---|---|---|---|
| LeaFTL (authors' FEMU port) | `leaftl.c` / `leaftl.h` | ✅ | ✅ |
| LearnedFTL | `ld-tpftl.c` / `ld-tpftl.h` | | ✅ |
| DFTL, TPFTL | `dftl.c`, `tpftl.c` | | Not reproduced |
| Shared stats emitter (this fork) | `ftl_stats.h` | — | — |

What this fork adds on top of upstream:

```text
experiments/
  README.md               map of the folders and scripts, and how to reproduce every result
  docs/
    BASELINE_PLAN.md      the plan, its status, every deviation, and the headline results
    METRICS.md            what every counter means, per FTL
    INVARIANTS.md         the 12 checks every run must pass
    KNOWN_ISSUES.md       31 behaviours of the authors' code that affect measurements (KI-1..KI-31)
  configs/                one file per stage: which FTLs, traces, preconditions and repeats to run
  traces/
    TRACES.md             generated profile of every trace (do not edit by hand)
    profiles.json         the same data, machine-readable
    manifest.csv/.json    generator parameters, seeds and hashes
    full/                 9 synthetic workloads (Git LFS): 8 × 10M ops, plus zipf-80-20 at 100k
    validation/           100k-op prefixes of each trace (plain git)
    matrix/, real/        1M-op prefixes and the UMass traces (regenerated, not committed)
    micro/                6 tiny hand-checked traces with expected.json
  scripts/                build, run, validate, aggregate, trace generation and profiling
  tools/
    cachesim.c            exact LRU / LFU / perfect-LFU / Belady-OPT hit rates
    gclog_redirect.c      LD_PRELOAD shim for LeaFTL's hard-coded GC log path (KI-2)
    leaftl_replica.py     offline replica of LeaFTL's mapping logic (KI-28)
  crosscheck/simplessd/   compares our LRU/LFU numbers with the cache in our SimpleSSD fork
  results/                generated CSVs (baseline_summary.csv, baseline_runs.csv, request_costs.csv, ...)
femu-scripts/run-blackbox-tcg.sh   boots FEMU under TCG with a cloud-init guest; needs no root
hw/femu/bbssd/ftl_stats.h          shared JSON schema and per-request log
```

## Quick start (TCG, no root)

The steps below boot FEMU by hand and replay one trace, to show how the pieces fit. The validated baselines are not made this way: they use `experiments/scripts/run_matrix.py`, which does all of this in one command per stage and checks every run. See [experiments/README.md](experiments/README.md).

These steps work on a shared Linux server without sudo or `/dev/kvm`. TCG is much slower than KVM. Counters are still exact, but latency numbers from TCG are meaningless (KI-19).

**Host requirements:** about 8 cores and 40 GB of RAM (the emulated SSD's backing store lives in RAM). You also need the usual QEMU build dependencies, `git-lfs`, `xorriso`, Python 3 with `numpy` and `pandas` (only for trace profiling), and a C compiler.

### 1. Clone, including the LFS traces

```bash
git clone https://github.com/notmohsin/LearnedFTL-Implementation.git LearnedFTL
cd LearnedFTL
git lfs pull          # fetches experiments/traces/full/*.trace (~2 GB)
```

You can skip `git lfs pull` if you only need `experiments/traces/validation/`.

### 2. Build FEMU

```bash
mkdir -p build-femu && cd build-femu
cp ../femu-scripts/femu-copy-scripts.sh . && ./femu-copy-scripts.sh .
# sudo ./pkgdep.sh      # Debian/Ubuntu only; skip if the QEMU build deps are already installed
./femu-compile.sh       # produces build-femu/qemu-system-x86_64
cd ..
```

The default build is **LeaFTL**. To build LearnedFTL instead, see [Switching FTL](#switching-ftl).

### 3. Prepare the guest image

The run script expects these files in `~/images/`:

| File | What it is |
|---|---|
| `u20s.qcow2` | Guest disk. Any Ubuntu cloud image works; we use 22.04 (`jammy-server-cloudimg-amd64.img`) behind a symlink with this name. |
| `femu-seed.iso` | cloud-init seed. It creates user `ubuntu` with your SSH key and sends console output to the serial port. |
| `femu_id_ed25519` | SSH key for the guest. |

```bash
mkdir -p ~/images/seed && cd ~/images
wget https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img
qemu-img resize jammy-server-cloudimg-amd64.img 20G   # optional; any qemu-img binary works
ln -sf jammy-server-cloudimg-amd64.img u20s.qcow2
ssh-keygen -t ed25519 -N '' -f femu_id_ed25519

cat > seed/meta-data <<'EOF'
instance-id: femu-vm
local-hostname: femu-vm
EOF

cat > seed/network-config <<'EOF'
version: 2
ethernets:
  all:
    match: {name: "en*"}
    dhcp4: true
EOF

cat > seed/user-data <<EOF
#cloud-config
hostname: femu-vm
users:
  - default
  - name: ubuntu
    groups: [adm, sudo]
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    ssh_authorized_keys:
      - $(cat femu_id_ed25519.pub)
ssh_pwauth: false
write_files:
  - path: /etc/default/grub.d/99-femu-serial.cfg
    content: |
      GRUB_CMDLINE_LINUX="console=ttyS0,115200 console=tty0"
      GRUB_TERMINAL=serial
      GRUB_SERIAL_COMMAND="serial --unit=0 --speed=115200 --word=8 --parity=no --stop=1"
runcmd:
  - update-grub
EOF

xorriso -as mkisofs -o femu-seed.iso -V cidata -J -r seed/
```

After the first boot has finished, shut the guest down and save a pristine copy: `cp u20s.qcow2 femu-pristine.qcow2`. Also record its hash: `sha256sum femu-pristine.qcow2 > femu-pristine.qcow2.sha256`. The baseline pipeline (`experiments/scripts/run_femu.py`) never writes to the pristine image: every run boots from a fresh qcow2 overlay on top of it. For the manual steps below, point `FEMU_OS_IMAGE` at a copy of the pristine image.

### 4. Boot FEMU

```bash
./femu-scripts/run-blackbox-tcg.sh          # console output is also written to build-femu/log
```

The emulated SSD appears in the guest as `/dev/nvme0n1`. Its geometry is hard-coded in both FTLs: 8 channels × 8 LUNs, 32 GiB raw, 40 µs / 200 µs / 2 ms read / program / erase.

To get past the first garbage-collection erase under LeaFTL, start QEMU with the GC-log shim. Without it, LeaFTL crashes there unless `/home/lzh/femu/log/` exists (KI-2):

```bash
cc -O2 -shared -fPIC -o experiments/tools/gclog_redirect.so experiments/tools/gclog_redirect.c -ldl
FEMU_GC_LOG_DIR=$PWD/experiments/results/my-run \
LD_PRELOAD=$PWD/experiments/tools/gclog_redirect.so \
  ./femu-scripts/run-blackbox-tcg.sh
```

Set `FEMU_REQLOG=/path/to/reqlog.csv` the same way to log every request the FTL receives as `seq,op,slba,nlb`.

### 5. Replay a trace and collect stats

From the host, copy the scripts and a trace into the guest:

```bash
scp -P 8080 -i ~/images/femu_id_ed25519 \
  experiments/scripts/{replay_trace.py,femu_stat.py} \
  experiments/traces/validation/uniform-mixed.trace ubuntu@localhost:
ssh -p 8080 -i ~/images/femu_id_ed25519 ubuntu@localhost
```

Inside the guest:

```bash
sudo python3 femu_stat.py 9     # baseline snapshot: print and zero the counters; discard this record
sudo python3 replay_trace.py --trace uniform-mixed.trace
sudo python3 femu_stat.py 9     # the record for this workload
```

On the host, each stats print shows up in `build-femu/log` as one JSON line:

```bash
grep '{"ftl"' build-femu/log
```

> The stats-reset command (`femu_stat.py 8`) is a no-op in the authors' code (KI-1). Use the print-twice snapshot pattern above. Counters that never reset (`write_cnt`, `gc_cnt`, `wa_cnt`) are only meaningful as before/after differences.

`replay_trace.py` uses `O_DIRECT`, one request at a time. The guest block layer can still merge or split requests unless you also run `sudo bash guest_prep.sh` first (copy it from `experiments/scripts/`). Compare `read_lpns` / `write_lpns` in the JSON against the trace's expected counters in [TRACES.md](experiments/traces/TRACES.md).

## Switching FTL

Two lines select the FTL that gets built:

1. **`hw/femu/meson.build`**: replace `'bbssd/leaftl.c'` with the FTL's source file, e.g. `'bbssd/ld-tpftl.c'`.
2. **`hw/femu/bbssd/bb.c`**: change `#include "./leaftl.h"` to the matching header, e.g. `"./ld-tpftl.h"`.

Then rebuild with `make -j"$(nproc)"` in `build-femu/` (or run `./femu-compile.sh` for a clean build). `main` stays on LeaFTL. For the baselines, `experiments/scripts/build_ftl.sh leaftl|learnedftl` does this switch in a separate git worktree (`build-ftl/<ftl>/`) and builds there, so the committed source never changes.

## Traces and experiment tooling

Every trace uses the format `timestamp operation offset_bytes length_bytes` (one request per line, `R` or `W`; lines starting with `#` are comments). The synthetic suite has one sequential mixed trace, three uniform-random traces (read, write, mixed), four Zipf hot/cold traces and a 50/50 sequential/random mix. All are generated with seed 42.

| Script | Purpose |
|---|---|
| `scripts/generate_workloads.py` | Regenerate the synthetic suite and its manifest (`--operations`, `--seed`, `--workload`) |
| `scripts/generate_8020_trace.py` | Generate the `zipf-80-20` hot/cold trace |
| `scripts/prepare_validation_traces.py` | Cut fixed-length prefixes of the full traces (`--operations`, default 100k) |
| `scripts/profile_traces.py` | Regenerate `TRACES.md` and `profiles.json` byte-identically (`--only <stem>` for one trace) |
| `scripts/belady.py` | Belady-OPT CMT bound keyed by LeaFTL translation page (assumes every miss inserts) |
| `scripts/replay_trace.py` | Replay a trace on a raw block device inside the guest |
| `scripts/femu_stat.py` | Send FEMU's stats command from the guest (`9` = print) |
| `scripts/build_ftl.sh`, `run_matrix.py`, `run_femu.py`, `validate_run.py`, `aggregate.py` | The baseline pipeline: build, run a stage, check each run, build the CSVs (see [experiments/README.md](experiments/README.md)) |

Run the tool tests from the repository root:

```bash
python3 experiments/tools/test_cachesim.py
bash experiments/tools/test_gclog_redirect.sh
```

## Running with KVM (upstream method)

If you have root and `/dev/kvm`, the authors' `build-femu/run-blackbox.sh` boots FEMU under KVM with `-enable-kvm -cpu host`. KVM is the only way to get meaningful latency and throughput. You also need vCPUs pinned to cores no other process uses (`femu-scripts/pin.sh`). To build a guest image by hand, follow the [FEMU README](https://github.com/vtess/FEMU#installation): install Ubuntu Server into a qcow2 image and route GRUB output to `ttyS0`.

## Documentation

| Document | Read it for |
|---|---|
| [METRICS.md](experiments/docs/METRICS.md) | Exactly what each counter counts in each FTL, the headline cross-FTL metrics, and how this setup differs from the papers' |
| [KNOWN_ISSUES.md](experiments/docs/KNOWN_ISSUES.md) | Behaviours of the authors' code that affect results, each with file:line and how the experiments handle it |
| [experiments/README.md](experiments/README.md) | Where everything is, and how to reproduce every result |
| [BASELINE_PLAN.md](experiments/docs/BASELINE_PLAN.md) | The plan, what was built, every deviation, and the headline results |
| [INVARIANTS.md](experiments/docs/INVARIANTS.md) | The 12 checks every run must pass |
| [TRACES.md](experiments/traces/TRACES.md) | Per-trace mix, footprint, locality, read-after-write share and cache bounds |

## Citation

If you use this code, please cite the original papers:

```bibtex
@inproceedings{wang2024learnedftl,
  title     = {{LearnedFTL}: A Learning-based Page-level {FTL} for Reducing Double Reads in Flash-based {SSDs}},
  author    = {Wang, Shengzhe and Lin, Zihang and Wu, Suzhen and Jiang, Hong and Zhang, Jie and Mao, Bo},
  booktitle = {2024 IEEE International Symposium on High-Performance Computer Architecture (HPCA)},
  year      = {2024}
}

@inproceedings{sun2023leaftl,
  title     = {{LeaFTL}: A Learning-Based Flash Translation Layer for Solid-State Drives},
  author    = {Sun, Jinghan and Li, Shaobo and Sun, Yunxin and Sun, Chao and Vucinic, Dejan and Huang, Jian},
  booktitle = {Proceedings of the 28th ACM International Conference on Architectural Support for Programming Languages and Operating Systems (ASPLOS)},
  year      = {2023}
}

@inproceedings{li2018femu,
  title     = {The {CASE} of {FEMU}: Cheap, Accurate, Scalable and Extensible Flash Emulator},
  author    = {Li, Huaicheng and Hao, Mingzhe and Tong, Michael Hao and Sundararaman, Swaminathan and Bj{\o}rling, Matias and Gunawi, Haryadi S.},
  booktitle = {16th USENIX Conference on File and Storage Technologies (FAST)},
  year      = {2018}
}
```

## License

FEMU is built on QEMU, and this repository inherits QEMU's licensing (mainly GPL-2.0). See [LICENSE](LICENSE) and [COPYING](COPYING). The FTL implementations are by the LearnedFTL authors (Xiamen University). The measurement instrumentation and `experiments/` were added in this fork.
