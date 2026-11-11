# LeaFTL / LearnedFTL baselines on FEMU

Reproducible, validated measurements of mapping-cache (CMT) behaviour for LeaFTL (`hw/femu/bbssd/leaftl.c`) and
LearnedFTL (`hw/femu/bbssd/ld-tpftl.c`). The authors' FTL logic and constants are unchanged; only measurement
instrumentation was added.

## What is where

```
experiments/
  README.md              this file
  docs/                  BASELINE_PLAN.md  the plan, its status and every deviation
                         METRICS.md        what each number means, per FTL
                         INVARIANTS.md     the 12 checks every run must pass
                         KNOWN_ISSUES.md   bugs in the authors' code (KI-1..KI-30) and how each is handled
  configs/               one file per stage: which FTLs, traces, preconditions and repeats to run
  scripts/               everything that runs, checks and summarises experiments (table below)
  tools/                 helpers: cache simulator, GC-log shim, LeaFTL replica (KI-28)
  crosscheck/simplessd/  checks our LRU/LFU numbers against the CMT of our SimpleSSD fork, ~/SSD-Simulator (run_crosscheck.py)
  traces/                TRACES.md (profile of every trace), profiles.json, manifest.json
    full/                the 9 generated synthetic traces (10M requests each, zipf-80-20 100k; Git LFS)
    validation/          the first 100k requests of each (validation stage)
    matrix/              the first 1M requests of each (matrix stage; regenerated, not committed)
    real/                UMass traces, first 1M requests, normalised (+ raw/ downloads; .json sidecars committed)
    micro/               4 tiny hand-checked traces with expected.json
  results/               baseline_summary.csv          one row per FTL x workload x length x precondition
                         baseline_summary_columns.csv  what every column means
                         baseline_runs.csv             one row per run
                         request_costs.csv             flash reads/writes per request and per page, setup vs workload
                         request_costs_columns.csv     what every request_costs column means
                         ki28_attribution.csv          LeaFTL lost-read attribution (KI-28)
                         simplessd_crosscheck.json     do our LRU/LFU hit counts match the SimpleSSD fork's? (per trace)
    micro/ validation/ matrix/ real/                   one folder per run (not committed)
    _logs/               console output of each run_matrix.py run
    _superseded/         runs replaced by re-runs, kept for the record
```

| Script | What it does |
|---|---|
| `scripts/build_ftl.sh` | Builds one FEMU binary per FTL in its own git worktree (`build-ftl/<ftl>/`) |
| `scripts/run_matrix.py` | Runs a whole stage from `configs/<stage>.json`, in parallel, resumable |
| `scripts/run_femu.py` | One run: fresh disk, boot, prepare guest, precondition, replay, stats, validate |
| `scripts/launch_femu.sh`, `guest_prep.sh`, `replay_trace.py`, `femu_stat.py` | The pieces `run_femu.py` uses |
| `scripts/validate_run.py` | Checks one run against the 12 invariants; `--compare` checks repeats are identical |
| `scripts/aggregate.py` | Builds the CSVs in `results/` from the run folders |
| `scripts/test_validate_run.py`, `check_micro.py` | Prove the checks work (mutated runs, hand-derived micro-traces) |
| `scripts/generate_workloads.py`, `generate_8020_trace.py`, `prepare_validation_traces.py`, `normalize_trace.py`, `make_micro_traces.py` | Make the traces |
| `scripts/profile_traces.py`, `belady.py` | Profile traces and compute cache bounds (`traces/TRACES.md`) |

## Reproduce

Requirements: the guest image `~/images/femu-pristine.qcow2` (+ `.sha256`), `~/images/femu-seed.iso`, the SSH key
`~/images/femu_id_ed25519`, and `ninja` on `PATH`. No root is needed; runs use KVM if `/dev/kvm` is usable and TCG
otherwise (counters only under TCG; latency is never reported from TCG).

```bash
# traces that are generated, not committed
python3 experiments/scripts/prepare_validation_traces.py --source-dir experiments/traces/full \
    --output-dir experiments/traces/matrix --operations 1000000 --skip-short
mkdir -p experiments/traces/real/raw
for f in WebSearch1 WebSearch2 WebSearch3 Financial1 Financial2; do
    curl -o experiments/traces/real/raw/$f.spc.bz2 https://skulddata.cs.umass.edu/traces/storage/$f.spc.bz2
    python3 experiments/scripts/normalize_trace.py experiments/traces/real/raw/$f.spc.bz2 \
        experiments/traces/real/$(echo $f | tr A-Z a-z).trace --ops 1000000
done   # each output's SHA-256 must match its committed .json sidecar

# one FEMU binary per FTL, built from a commit in its own worktree
experiments/scripts/build_ftl.sh leaftl
experiments/scripts/build_ftl.sh learnedftl

# self-tests
python3 experiments/tools/test_cachesim.py
bash experiments/tools/test_gclog_redirect.sh
python3 experiments/scripts/test_validate_run.py

# stages (resumable; each run is validated as it finishes; runs land in results/<stage>/)
python3 experiments/scripts/run_matrix.py micro && python3 experiments/scripts/check_micro.py experiments/results/micro/*
python3 experiments/scripts/run_matrix.py validation
python3 experiments/scripts/run_matrix.py matrix
python3 experiments/scripts/run_matrix.py real
python3 experiments/scripts/aggregate.py
python3 experiments/tools/ki28_attribution.py
```

A single run: `experiments/scripts/run_femu.py --ftl leaftl --trace experiments/traces/validation/uniform-mixed.trace
--seed 1 --precondition warm` (lands in `results/manual/`). A run folder holds `manifest.json` (provenance and
timings), `records.jsonl` (FEMU's stats records), `reqlog.csv.gz` (every request FEMU received), `femu.log`,
`console.log` and `validation.json`.

Preconditions: `none` (empty device), `warm` (every LPN the trace touches written once first; the main condition),
`fill` (whole 30 GiB written first; GC stress).

The folders were reorganised on 2026-11-10 (traces into `traces/<kind>/`, runs into `results/<stage>/`). Saved runs
record their old trace path under `moved` in `manifest.json`; their console logs (`run.log`, `launch.env`) still show
the paths used at the time.
