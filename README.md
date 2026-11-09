# LearnedFTL / LeaFTL baselines on FEMU

This repository measures how learned address-mapping models change mapping-cache (CMT) behaviour in flash SSDs. It compares **LeaFTL** (ASPLOS '23) and **LearnedFTL** (HPCA '24) on the [FEMU](https://github.com/vtess/FEMU) SSD emulator. It is a fork of the LearnedFTL authors' artifact, [astlxmu/LearnedFTL](https://github.com/astlxmu/LearnedFTL), with these additions:

- **Measurement instrumentation** in both FTLs. It adds request-flow counters, an exhaustive per-LPN read partition with built-in invariant checks, one shared JSON stats schema, and an optional per-request log.
- **An experiment toolkit**: deterministic synthetic traces, trace profiling with exact cache bounds (LRU, LFU, Belady-OPT), and replay and stats scripts.
- **Written analysis** of the authors' code: exact metric definitions, and 20 documented behaviours that affect measurements.
- **A run path for machines without root or KVM**: QEMU's software emulator (TCG), a cloud-init guest image, and a preload shim for a hard-coded log path.

> **Ground rule:** the authors' FTL decision logic and constants are unchanged. Every change in `hw/femu/bbssd/` only counts or prints. Bugs in the authors' code are documented and measured, never fixed. See [KNOWN_ISSUES.md](experiments/docs/KNOWN_ISSUES.md).

## Status (2026-11-09)

| | |
|---|---|
| ✅ Done | Metric definitions and known-issues analysis. Trace suite plus generated profiles and cache bounds. Instrumentation in LeaFTL and LearnedFTL (committed in `8f16a9016`; both FTLs build). |
| ⏳ Next | Running the invariant checks against real runs. Offline validator (`validate_run.py`). One build per FTL (`build_ftl.sh`). Deterministic `O_DIRECT` replay. `fill` / `steady` preconditioning. |
| ⛔ Blocked | Latency and throughput. They need KVM, and this host has no KVM access. Counter-based results don't depend on time and are unaffected. |

**There are no baseline numbers yet.** The rows in `experiments/results/runs.csv` come from early smoke tests and are known to be invalid (see below). The full roadmap and milestones are in [BASELINE_PLAN.md](experiments/docs/BASELINE_PLAN.md).

## Three things to know before trusting any number

1. **Raw CMT hit rates can't be compared across the two FTLs.** A LeaFTL CMT entry is a learned segment that covers up to 256 LPNs. A LearnedFTL entry is a single LPN→PPN mapping. Cross-FTL comparison uses the *translation-free read ratio* and the *double-read ratio* instead. See [METRICS.md](experiments/docs/METRICS.md).
2. **Reads of unwritten space distort LeaFTL's hit rate.** LeaFTL counts those reads as CMT misses, but LearnedFTL excludes them. The old 0.03% LeaFTL hit rate on `seq-mixed` came from this: that trace never reads data it wrote. Baselines therefore need `fill` preconditioning (KI-3).
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
  docs/
    METRICS.md            what every counter means, per FTL, with file:line references
    KNOWN_ISSUES.md       20 behaviours of the authors' code that affect measurements (KI-1..KI-20)
    BASELINE_PLAN.md      validation plan, invariant catalogue, milestones, status
  traces/                 9 synthetic workloads (Git LFS): 8 × 10M ops, plus zipf-80-20 at 100k
    TRACES.md             generated profile of every trace (do not edit by hand)
    profiles.json         the same data, machine-readable
    manifest.json         generator parameters and seeds
  validation-traces/      100k-op prefixes of each trace, for quick runs (plain git)
  scripts/                trace generation, profiling, replay, stats trigger (see below)
  tools/
    cachesim.c            exact LRU / LFU / perfect-LFU / Belady-OPT hit rates
    gclog_redirect.c      LD_PRELOAD shim for LeaFTL's hard-coded GC log path (KI-2)
    test_*.{py,sh}        tests for both tools
  results/runs.csv        early smoke-test rows (invalid; kept for history)
femu-scripts/run-blackbox-tcg.sh   boots FEMU under TCG with a cloud-init guest; needs no root
hw/femu/bbssd/ftl_stats.h          shared JSON schema and per-request log
```

## Quick start (TCG, no root)

These steps work on a shared Linux server without sudo or `/dev/kvm`. TCG is much slower than KVM. Counters are still exact, but latency numbers from TCG are meaningless (KI-19).

**Host requirements:** about 8 cores and 40 GB of RAM (the emulated SSD's backing store lives in RAM). You also need the usual QEMU build dependencies, `git-lfs`, `xorriso`, Python 3 with `numpy` and `pandas` (only for trace profiling), and a C compiler.

### 1. Clone, including the LFS traces

```bash
git clone https://github.com/notmohsin/LearnedFTL-Implementation.git LearnedFTL
cd LearnedFTL
git lfs pull          # fetches experiments/traces/*.trace (~2 GB)
```

You can skip `git lfs pull` if you only need `experiments/validation-traces/`.

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

After the first boot has finished, shut the guest down and save a pristine copy: `cp u20s.qcow2 femu-pristine.qcow2`. Before each run, `experiments/scripts/reset_femu_image.sh [target]` copies the pristine image to a fresh disk. Point `FEMU_OS_IMAGE` at that disk so every run starts from the same state.

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
  experiments/validation-traces/uniform-mixed.trace ubuntu@localhost:
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

`replay_trace.py` still issues buffered I/O, so the guest kernel can merge, split or read ahead on requests. Until the `O_DIRECT` replay lands, compare `read_lpns` / `write_lpns` in the JSON against the trace's expected counters in [TRACES.md](experiments/traces/TRACES.md).

## Switching FTL

Two lines select the FTL that gets built:

1. **`hw/femu/meson.build`**: replace `'bbssd/leaftl.c'` with the FTL's source file, e.g. `'bbssd/ld-tpftl.c'`.
2. **`hw/femu/bbssd/bb.c`**: change `#include "./leaftl.h"` to the matching header, e.g. `"./ld-tpftl.h"`.

Then rebuild with `make -j"$(nproc)"` in `build-femu/` (or run `./femu-compile.sh` for a clean build). `main` stays on LeaFTL. Planned: `build_ftl.sh` will build each FTL in its own git worktree so the committed source never changes.

## Traces and experiment tooling

Every trace uses the format `timestamp operation offset_bytes length_bytes` (one request per line, `R` or `W`; lines starting with `#` are comments). The synthetic suite has one sequential mixed trace, three uniform-random traces (read, write, mixed), four Zipf hot/cold traces and a 50/50 sequential/random mix. All are generated with seed 42.

| Script | Purpose |
|---|---|
| `scripts/generate_workloads.py` | Regenerate the synthetic suite and its manifest (`--operations`, `--seed`, `--workload`) |
| `scripts/generate_8020_trace.py` | Generate the `zipf-80-20` hot/cold trace |
| `scripts/prepare_validation_traces.py` | Cut fixed-length prefixes into `validation-traces/` (`--operations`, default 100k) |
| `scripts/profile_traces.py` | Regenerate `TRACES.md` and `profiles.json` byte-identically (`--only <stem>` for one trace) |
| `scripts/belady.py` | Belady-OPT CMT bound keyed by LeaFTL translation page (assumes every miss inserts) |
| `scripts/replay_trace.py` | Replay a trace on a raw block device inside the guest |
| `scripts/femu_stat.py` | Send FEMU's stats command from the guest (`9` = print) |
| `scripts/reset_femu_image.sh` | Copy the pristine guest image to a fresh run image |
| `scripts/update_csv.py`, `clone_run.py` | Legacy: hand-maintained `runs.csv`. To be replaced by generated summaries |

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
| [BASELINE_PLAN.md](experiments/docs/BASELINE_PLAN.md) | Invariant catalogue, validation gates, the run pipeline and milestones |
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
