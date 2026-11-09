# LeaFTL / LearnedFTL baselines on FEMU

Reproducible, validated measurements of mapping-cache (CMT) behaviour for LeaFTL (`hw/femu/bbssd/leaftl.c`) and
LearnedFTL (`hw/femu/bbssd/ld-tpftl.c`). The authors' FTL logic and constants are unchanged; only measurement
instrumentation was added.

| Document | Contents |
|---|---|
| `docs/BASELINE_PLAN.md` | The plan, its status and every deviation, with the measurement that caused it |
| `docs/METRICS.md` | What each metric means, per FTL, and how read cost is measured |
| `docs/INVARIANTS.md` | The 12 invariants every run is checked against (HARD / KNOWN / REPORT) |
| `docs/KNOWN_ISSUES.md` | Authors'-code issues (KI-1..KI-27), their effect on the numbers and how each is handled |
| `traces/TRACES.md` | Profile of every trace: provenance, mix, footprint, locality, cache-policy bounds |
| `results/baseline_summary.csv` | One row per FTL x workload x trace length x precondition (with `baseline_summary_columns.csv`) |
| `results/baseline_runs.csv` | One row per run |

## Reproduce

Requirements: the guest image `~/images/femu-pristine.qcow2` (+ `.sha256`), `~/images/femu-seed.iso`, the SSH key
`~/images/femu_id_ed25519`, and `ninja` on `PATH`. No root is needed; runs use KVM if `/dev/kvm` is usable and TCG
otherwise (counters only under TCG; latency is never reported from TCG).

```bash
# traces that are generated, not committed
python3 experiments/scripts/prepare_validation_traces.py --source-dir experiments/traces \
    --output-dir experiments/matrix-traces --operations 1000000 --skip-short
mkdir -p experiments/real-traces/raw
for f in WebSearch1 WebSearch2 WebSearch3 Financial1 Financial2; do
    curl -o experiments/real-traces/raw/$f.spc.bz2 https://skulddata.cs.umass.edu/traces/storage/$f.spc.bz2
    python3 experiments/scripts/normalize_trace.py experiments/real-traces/raw/$f.spc.bz2 \
        experiments/real-traces/$(echo $f | tr A-Z a-z).trace --ops 1000000
done   # each output's SHA-256 must match its committed .json sidecar

# one FEMU binary per FTL, built from a commit in its own worktree
experiments/scripts/build_ftl.sh leaftl
experiments/scripts/build_ftl.sh learnedftl

# self-tests
python3 experiments/tools/test_cachesim.py
bash experiments/tools/test_gclog_redirect.sh
python3 experiments/scripts/test_validate_run.py

# stages (resumable; each run is validated as it finishes)
python3 experiments/scripts/run_matrix.py micro && python3 experiments/scripts/check_micro.py experiments/results/micro-*
python3 experiments/scripts/run_matrix.py validation
python3 experiments/scripts/run_matrix.py matrix
python3 experiments/scripts/run_matrix.py real
python3 experiments/scripts/aggregate.py
```

A single run: `experiments/scripts/run_femu.py --ftl leaftl --trace experiments/validation-traces/uniform-mixed.trace
--seed 1 --precondition warm`. Its directory under `results/` holds `manifest.json` (provenance and timings),
`records.jsonl` (FEMU's stats records), `reqlog.csv.gz` (every request FEMU received), `femu.log`, `console.log` and
`validation.json`.

Preconditions: `none` (empty device), `warm` (every LPN the trace touches written once first; the main condition),
`fill` (whole 30 GiB written first; GC stress).
