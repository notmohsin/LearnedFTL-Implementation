# LeaFTL Baseline and LearnedFTL Evaluation Plan

## Goal

Measure whether learned mapping models improve CMT hit rate, mapping DRAM
usage, translation-page reads, double/triple reads, latency, and throughput.
All results must be reproducible.

## Phase 1 - Define exact metrics

For every FTL, record:

```text
user_read_requests
user_write_requests
translation_accesses
cmt_hits
cmt_misses
cmt_hit_rate
model_queries
model_hits
model_misses
model_hit_rate
single_translation_reads
double_translation_reads
triple_translation_reads
total_translation_reads
```

Definitions:

```text
CMT hit rate = cmt_hits / (cmt_hits + cmt_misses)
Model hit rate = model_hits / model_queries
Double-read rate = double_reads / total_translation_accesses
```

Status:

- [x] CMT hits, misses, accesses, and hit rate are instrumented for LeaFTL.
- [x] Single, double, triple, and total translation reads are instrumented.
- [x] Add model queries, model hits, model misses, corrections, and model hit
  rate.
- [x] Emit machine-readable JSON Lines statistics from the print-stat command.
- [x] Validate that counters reset and do not mix runs.

## Phase 2 - Establish baselines

Run the same workload against:

1. DFTL
2. TPFTL
3. LeaFTL
4. LearnedFTL, once instrumented

Keep identical:

- FEMU geometry.
- VM image and SSD size.
- CMT size.
- Trace and random seed.
- Build flags.
- Fresh FEMU process per run.
- Counter reset before each workload.

Status:

- [x] FEMU request, CMT, mapping, learned-segment, NAND-read, write, and GC
  paths are mapped.
- [ ] Add build-selectable DFTL configuration.
- [ ] Add build-selectable TPFTL configuration.
- [ ] Run identical traces against DFTL, TPFTL, and LeaFTL.
- [ ] Add LearnedFTL to the comparison.

## Phase 3 - Add reproducible instrumentation

Use explicit counters:

```c
uint64_t cmt_accesses;
uint64_t cmt_hits;
uint64_t cmt_misses;
uint64_t model_queries;
uint64_t model_hits;
uint64_t model_misses;
uint64_t single_reads;
uint64_t double_reads;
uint64_t triple_reads;
```

Emit one machine-readable record per experiment, including:

```text
ftl
trace
seed
cmt_accesses
cmt_hits
cmt_misses
cmt_hit_rate
model_queries
model_hits
model_misses
model_hit_rate
single_reads
double_reads
triple_reads
```

Status:

- [x] Human-readable CMT and translation-read counters exist for LeaFTL.
- [x] Add learned lookup counters at the model-search/correction boundary.
- [x] Add machine-readable output.
- [x] Validate output against hand-calculated rates.

## Phase 4 - Build deterministic trace replay

Trace format:

```text
timestamp operation offset length
0.000000 W 0 4096
0.001000 W 4096 4096
0.002000 R 0 4096
```

Replay must control operation order, LBA, request size, inter-arrival delay,
random seed, operation count, and read/write ratio.

Use direct raw-device I/O, `O_DIRECT` where supported, fixed request sizes,
explicit synchronization, and no filesystem mounted on `/dev/nvme0n1`.

Status:

- [x] Raw-device replay has been manually verified.
- [x] Validate one small deterministic workload end-to-end.
- [ ] Add deterministic trace generation.
- [ ] Add timestamp-aware replay.
- [ ] Add direct-I/O and read-data validation.
- [ ] Record trace checksums and workload properties.

## Phase 5 - Start with controlled workloads

Before real traces, run:

### Sequential

100% sequential writes and 100% sequential reads. Validate learned segment
formation.

### Random

100% random reads and 100% random writes with a fixed seed. Stress CMT misses
and model prediction.

### Zipf/skewed

80% of accesses within 20% of the address range. Measure locality benefits.

### Mixed

70% reads, 30% writes, with 70% sequential and 30% random access.

Use at least one million operations after instrumentation is validated.

Status:

- [ ] Implement sequential workload.
- [ ] Implement random workload.
- [ ] Implement skewed workload.
- [ ] Implement mixed workload.
- [ ] Validate returned data for every workload.

## Phase 6 - Use real large traces

Potential sources include MSR Cambridge, FIU, UMass, SNIA IOTTA, public
RocksDB traces, and captured `blktrace` or `bpftrace` traces.

Normalize each trace to timestamp, operation, offset, and length. Record source,
filename, checksum, duration, operation count, address range, read/write ratio,
and request-size distribution.

Status:

- [ ] Select a trace source.
- [ ] Build a trace normalizer.
- [ ] Record provenance and checksums.
- [ ] Run repeated real-trace experiments.

## Phase 7 - Reproducibility package

Target layout:

```text
experiments/
  configs/
    ssd.json
    cmt.json
  traces/
    synthetic-seq.trace
    synthetic-random.trace
  scripts/
    build.sh
    run_femu.sh
    replay_trace.py
    collect_stats.py
  results/
  MANIFEST.sha256
```

Each result must include source commits, compiler and kernel versions, FTL name,
SSD geometry, CMT size, trace checksum, random seed, workload parameters, and
the result file.

Run each workload at least three times and report mean, standard deviation,
minimum, and maximum.

Status:

- [ ] Create experiment directory structure.
- [ ] Add metadata collection.
- [ ] Add result aggregation.
- [ ] Add manifest generation.
- [ ] Run three repetitions per workload.

## Recommended first milestone

Do not begin with real traces. First implement and validate DFTL versus TPFTL
versus LeaFTL using one million 4 KiB operations with fixed seeds across
sequential, random, and skewed workloads.

The comparison table should include:

| FTL | Workload | CMT accesses | CMT hits | CMT hit rate | Single | Double | Triple |
|---|---|---:|---:|---:|---:|---:|---:|
| DFTL | Random | | | | | | |
| TPFTL | Random | | | | | | |
| LeaFTL | Random | | | | | | |

Then add learned-model metrics for LeaFTL and LearnedFTL.

## Current next step

Use the validated JSONL statistics with a deterministic workload, then add the
trace generator and replay tooling.
