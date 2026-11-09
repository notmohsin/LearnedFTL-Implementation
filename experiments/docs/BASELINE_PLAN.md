# Plan: Run and validate FTL baselines on FEMU (LeaFTL / LearnedFTL)

## Context

The goal is reproducible, defensible baseline measurements of mapping-cache (CMT) behaviour for
**LeaFTL** (`hw/femu/bbssd/leaftl.c`) and **LearnedFTL** (`hw/femu/bbssd/ld-tpftl.c`). These are the
reference point for later ML-based CMT work.
DFTL and TPFTL are **out of scope**. Their files stay in the repo untouched and unbuilt. The LearnedFTL
paper's comparisons against TPFTL are noted in `experiments/docs/METRICS.md` but not reproduced.
LeaFTL (ASPLOS'23) and LearnedFTL (HPCA'24) are in `~/Implementations/papers/`.

**Scope, as decided by the user:**

- **Do not change the authors' FTL code.** That covers decision logic and constants: entries per translation page,
  CMT size and geometry. Bugs are **documented only**.
- **Allowed changes:**
  - Additive, side-effect-free **measurement instrumentation** in the FTL files (counters, prints, request logging). This continues the user's own uncommitted work.
  - Experiment scripts and run configuration outside the FTL code.
- **FEMU only.** No separate harness, no ML hooks, no new oracles. All effort goes into running and validating the baselines.
- **Translation-page size (256 vs 512) is an experiment parameter, but with constants untouched:**
  - It is covered as each FTL's native value: LeaFTL = 256, LearnedFTL = 512.
  - A same-FTL 256-vs-512 sweep would need a constant change, so it is deferred and recorded as a limitation.

**Why the current numbers can't be used yet** (findings from the code exploration):

- **Counters are never reset before a workload.** `reset_stat` in `hw/femu/bbssd/bb.c:27-72` is
  commented out. Counters zero only *after* a print (`leaftl.c:2333-2350`), so boot-time guest I/O
  leaks in. This is the likely source of the constant `cmt_hits=131`.
- **The guest changes the request stream.** Replay has no `O_DIRECT` (`experiments/scripts/replay_trace.py:26`), so the page cache,
  readahead and merging all act on it. That is why `cmt_accesses` (506k) > reads issued (293k).
- **LeaFTL counts unmapped reads as CMT misses.** Reads of never-flushed translation pages count as misses (`leaftl.c:1143-1147`, `:2437`),
  and seq-mixed reads never hit written data. So the 0.03% hit rate is an artefact of the workload design.
  LearnedFTL excludes these reads (`ld-tpftl.c:2589,2634`).
- **LeaFTL crashes on the first erase on channel 0 / LUN 0 if a hard-coded directory is missing.** It opens `/home/lzh/femu/log/gc_frequency.txt` (`leaftl.c:2583`) and
  writes to it with no NULL check (`:835`), but only for ch0/lun0 erases (`:777-779`).
- **There is no over-provisioning.** `devsz_mb=32768` equals raw flash, and every run so far shows `gc=0`, so none reached steady state.
- **The tooling has hygiene problems:**
  - Line 1 of `experiments/scripts/prepare_validation_traces.py` is corrupted.
  - `runs.csv` mixes percentages and fractions.
  - `belady.py` assumes every miss inserts, which LeaFTL doesn't do.

## Phase 0: Hygiene and environment prerequisites

1. **Fix the corrupted script.** Line 1 of `experiments/scripts/prepare_validation_traces.py` must become the shebang. This is a script, not FTL code.
2. **Clean up results.** Add `experiments/results/*/` to `.gitignore`, delete the failed `leaftl-rerun-20261108-161846/`,
   and track `reset_femu_image.sh`.
3. **Environment constraint: no root.** This is a shared university server (about 19 sessions across 7 users) and the user has no `sudo`, so:
   - **KVM is unavailable.** `/dev/kvm` is `root:kvm` with an ACL that only adds `gdm`, the `kvm` group is empty, and only an administrator can change that.
     All runs use TCG. This is fine for every **counter-based** result (hit rates, read partition, invariants, WAF, GC counts), because FTL state does not
     depend on time and the replay is single-threaded QD1. It is **not** fine for latency or throughput, which are deferred (see Phase 3 and Phase 6).
     Request KVM access from the server administrators (text in the Status section) or run the latency phase on a machine with KVM.
   - **The hard-coded GC log path cannot be created** (`/home/lzh` needs root). Instead of changing the authors' code, an `LD_PRELOAD` shim,
     `experiments/tools/gclog_redirect.c`, redirects it to `$FEMU_GC_LOG_DIR` (the per-run results directory). It is tested by
     `experiments/tools/test_gclog_redirect.sh`. `run_femu.sh` always sets it.

## Phase 1: Document the authors' code (no code changes)

- **`experiments/docs/KNOWN_ISSUES.md`.** One entry per issue, each with file:line, what happens, how it affects the metrics,
  and how the experiment design works around it:
  - LeaFTL: uninitialised `vppn` (`leaftl.c:2371`).
  - LeaFTL: the write-buffer check only looks at slot 0 (`:2391`).
  - LeaFTL: the CMT is never invalidated on overwrite.
  - LeaFTL: the hit path doesn't check whether the LPN falls inside the cached segment.
  - LeaFTL: corrected lookups are never cached.
  - LeaFTL: the last LPN of each flush is never segmented (`:2176`).
  - LeaFTL: LPN 0 is used as a sentinel (`:2115`).
  - LeaFTL: `gc_cnt` is incremented when no victim is found (`:1351`).
  - LeaFTL: reads before the first write get 0 latency (`:2387`).
  - LeaFTL: the hard-coded gc log path.
  - Empty `reset_stat` in `bb.c`.
  - LearnedFTL: the write path calls `cmt_hit` and inserts into the CMT, but those lookups aren't counted (`ld-tpftl.c:2700-2702`).
  - LearnedFTL: it has no single/double/triple read counters.
- **`experiments/docs/METRICS.md`.** Exact definitions per FTL: what one CMT entry is (LeaFTL: one segment per
  translation page, 8192 entries; LearnedFTL: one entry per LPN, 8192, in two-level LRU with a model
  tried only after a miss), where each counter increments
  (file:line), and which reads are excluded (unmapped, pre-first-write, write-buffer hits).
  Cross-FTL headline metrics:
  - **Translation-free read ratio:** mapped read LPNs served without a flash read of a translation page, over all mapped read LPNs.
  - **Double-read ratio** and **triple-read ratio.**

  Raw CMT hit rate is reported per FTL but is not directly comparable, because the entries differ.
  The doc also records the paper configurations and how ours differ, e.g. the CMT at about 3% of mappings in the LearnedFTL paper.

## Phase 1b: Trace profile master document (`experiments/traces/TRACES.md`)

A single reference document that profiles every trace in `experiments/traces/` and `experiments/validation-traces/`
(and real traces once they're added).

- It is **generated, never hand-edited**, by a new `experiments/scripts/profile_traces.py`. The script streams each trace once,
  is deterministic, and writes both `TRACES.md` and a machine-readable `traces/profiles.json`.
- It reuses the parsing and Belady code in `experiments/scripts/belady.py` and the existing `traces/manifest.csv` / `manifest.json`.
- A header section explains the trace format, each metric's definition, and how to regenerate the document.

**Per-trace profile:**

1. **Provenance:**
   - generator script and parameters, seed (from the manifest)
   - SHA-256 (verified against the manifest; a mismatch is flagged), file size
   - op count
   - for validation prefixes: parent trace and prefix length
   - for real traces: source URL and normalisation applied
2. **Mix:**
   - read/write ratio by requests and by bytes
   - request-size distribution (4/8/16/64 KiB counts, plus a histogram for real traces)
3. **Footprint:**
   - address range used
   - unique LPNs, split into read and written
   - unique translation pages at **both 256 and 512** entries per page (LeaFTL vs LearnedFTL keying)
   - footprint in GiB vs the 30 GiB logical device
4. **Locality:**
   - sequentiality: the share of requests that start where the previous request ended
   - skew: the share of accesses that go to the hottest 1%, 10% and 20% of LPNs
   - LPN reuse-distance distribution (percentiles)
   - working-set size over windows
5. **Mapping-relevant behaviour:**
   - **read-after-write share:** the fraction of read LPNs whose address was written earlier in the trace,
     reported separately for `none` vs `fill` preconditioning. This shows up front which traces hit the unmapped-read artefact.
   - reads of never-written LPNs
   - overwrite ratio
6. **Bounds:**
   - LRU, LFU (in-cache), perfect LFU and Belady-OPT hit rates at 8192 entries, keyed per translation page (256 and 512) and per LPN,
     computed by `experiments/tools/cachesim.c` and verified by `experiments/tools/test_cachesim.py`
   - curves across cache sizes (1,024 to 65,536), as tables in `TRACES.md` and values in `profiles.json`
7. **Expected FTL counters.** The trace-derived expected values that invariant 10 (Phase 2b) checks runs against:
   `read_lpns`, `write_lpns` and the never-written reads.

A summary table at the top lists one row per trace with the key numbers, so you can choose workloads at a glance.

## Phase 2: Make the request-flow instrumentation robust, then commit

This builds on the user's uncommitted counters in `leaftl.c`/`leaftl.h`/`ld-tpftl.c`. The purpose is to
**validate the flow of read and write requests** into each FTL. Every addition is counting or printing only,
so no FTL decision changes.

1. **Request-flow counters at FTL entry**, the same set in both FTLs:
   - `read_reqs`, `write_reqs`
   - `read_lpns`, `write_lpns`
   - a request-size histogram (in LPNs, power-of-two buckets)
   - `buffer_flushes` and `gc_runs` deltas
2. **Exhaustive read-LPN partition with invariants**, emitted in the JSON. Each FTL gets its own buckets
   matched to its code paths. For LeaFTL:
   - `read_lpns = pre_first_write + buffer_hits + cmt_hits + cmt_misses`
   - `cmt_misses = unmapped_tp + model_queries`
   - `model_queries = model_hits + model_corrections + model_misses`
   - `single + double + triple` reads over the mapped buckets

   LearnedFTL gets the equivalent partition over its own paths:
   - `read_lpns = unmapped + cmt_hits + model_hits + model_misses_then_cmt_miss`
   - additive single/double/triple counters at its flash-read sites

   Any mismatch increments `invariant_failures` and prints the counters involved.
   This replaces the current two ad-hoc checks in `count_segments`.
3. **Optional per-request log**, off by default and enabled by an env var read once at init
   (e.g. `FEMU_REQLOG=/path`). It writes `seq, op, slba, nlb` to a buffered file, flushed on the stats print.
   This lets the validator diff what FEMU received against what the trace issued.
4. **Snapshot/delta stats instead of reset.** `reset_stat` stays untouched, because a print already zeroes the
   counters. The run script prints once before the workload (discarded as the baseline) and once after.
   Counters the authors' code never zeroes (`write_cnt`, `gc_cnt`, `wa_cnt`) are reported as before/after deltas by the parser.
5. **One JSON schema for both FTLs** via a shared emit helper in a new header
   `hw/femu/bbssd/ftl_stats.h`:
   - the same keys everywhere (fix LeaFTL still emitting `access_cnt` / `writes`)
   - `uint64` and `%llu` throughout, since LeaFTL prints read counts with `%d`
   - all rates as fractions
   - `ftl`, `ents_per_tp`, `cmt_entries`, and the geometry included in every record
6. **LearnedFTL write-path lookups** are counted additively under separate `cmt_write_hits` / `cmt_write_misses` keys,
   so they never mix with read hit rates.
7. **Commit:** a "measurement instrumentation" commit, kept separate from any script commits.

## Phase 2b: Invariant-based validation framework (the core of validation)

Invariants are the main evidence that measurements are correct. They are written down in
`experiments/docs/INVARIANTS.md` (ID, statement, where it's checked, class) and checked at two levels:

- **In FEMU.** Additive instrumentation evaluates them at every stats print. It emits `inv_<ID>_violations` counters plus the first
  few offending values, logged once.
- **Offline.** `experiments/scripts/validate_run.py` evaluates them on the JSONL, the request log and the trace.

**Classes:**

- **HARD:** must be 0, otherwise the run is invalid and excluded from aggregation.
- **KNOWN:** expected to be non-zero because of a documented authors' bug. The value is reported as that bug's measured impact, never "fixed".

**Catalogue** (the same IDs for both FTLs where they apply):

1. **Flow conservation (HARD).** Requests and LPNs issued by the trace equal those received by the FTL, per op type (`read_reqs`,
   `write_reqs`, `read_lpns`, `write_lpns`), and the request log matches the trace in order.
2. **Read partition (HARD).** Every read LPN lands in exactly one bucket, as in Phase 2 item 2 (LeaFTL:
   `pre_first_write + buffer_hits + cmt_hits + cmt_misses`; LearnedFTL: its own buckets).
   Also `cmt_hits + cmt_misses = cmt_lookups`, and `model_hits + model_corrections + model_misses = model_queries`.
3. **Causality (HARD):**
   - `model_queries ≤ cmt_misses` (a model is only consulted after a miss)
   - `unmapped ≤ cmt_misses` for LeaFTL
   - for LearnedFTL, `model_queries ≤ #misses with bitmap set`
4. **Read-cost accounting (HARD):**
   - `single + double + triple = mapped reads served from flash`
   - `single + 2·double + 3·triple = NAND reads charged to user reads`
   - this is cross-checked against an additive NAND-read counter at `ssd_advance_status`, split into user, translation and GC reads
5. **Write/NAND accounting (HARD).** NAND page programs = user data pages flushed + translation-page writes + GC-migrated pages.
   The write amplification factor (WAF) is derived from these, not taken from the authors' `wa_cnt`.
6. **Cache occupancy (HARD).**
   - `0 ≤ occupancy ≤ capacity`
   - `inserts − evictions = occupancy` (LeaFTL: 8192 segment entries; LearnedFTL: 8192 LPN entries across TP nodes)
   - the LRU list length equals the hash-table population, checked by a read-only walk at print time
7. **Mapping/space consistency (HARD).** An end-of-run audit, read-only, at the stats print:
   - free + victim + full lines = total lines
   - the per-block valid-page count equals the number of mappings pointing into that block
   - every valid page's reverse map (OOB `lpn`) points back to a mapping that resolves to it
8. **Translation correctness (KNOWN for LeaFTL, HARD for LearnedFTL unless shown otherwise).** An additive **shadow page map**
   (`lpn → ppn`, 8M × 4 B = 32 MB) is updated at every point where the authors' code commits a mapping
   (buffer flush, GC migration). On every read, the PPN the FTL resolves is compared with the shadow map.
   - Mismatches are counted as `translation_errors`, split into stale-CMT, out-of-segment and uninitialised-`vppn`.
   - This turns the documented LeaFTL bugs (stale CMT after overwrite, no in-segment check, uninitialised `vppn`)
     into **measured** error rates instead of claims.
9. **Snapshot integrity (HARD).**
   - Counters are non-decreasing within a phase.
   - The baseline print is followed by zeroed resettable counters.
   - Non-resettable counters (`write_cnt`, `gc_cnt`, `wa_cnt`) are only ever used as before/after deltas.
10. **Trace-derived expectations (HARD, offline).** `validate_run.py` replays the trace's address stream in a tiny
    reference model, tracking which LPNs have been written since preconditioning. From that it predicts:
    - exact `read_lpns` and `write_lpns`
    - never-written read LPNs, which must equal LearnedFTL's unmapped count and be ≤ LeaFTL's unmapped count
    - the request-size histogram
11. **Determinism (HARD, cross-run).** Identical configuration and seed give identical counters across 3 runs. Only latency may vary.

Each invariant gets at least one **micro-trace** of 10–50 ops that exercises it. Micro-traces that deliberately trigger the KNOWN cases
(for example, overwrite then read inside a cached segment) confirm the checks catch real violations rather than passing vacuously.

## Phase 3: Build one binary per FTL without touching committed source

- **`experiments/scripts/build_ftl.sh <ftl>`.** It creates a git worktree per FTL under `build-ftl/<ftl>/` and switches
  the FTL using the README's documented method: the `bbssd/*.c` entry in `hw/femu/meson.build` plus the `#include` in `bb.c`.
  It builds there and records the commit SHA, the applied switch diff, the compiler and the binary SHA-256.
  `main` always stays LeaFTL as committed.
- **Run script with selectable accelerator** (`femu-scripts/run-blackbox.sh`-style wrapper, new file). It takes `QEMU=`, `devsz_mb=` and
  `ACCEL=tcg|kvm` (default: `kvm` if `/dev/kvm` is openable, else `tcg`), preloads `gclog_redirect.so` and sets `FEMU_GC_LOG_DIR`.
  With KVM it adds `-enable-kvm -cpu host` and pinned vCPUs (`femu-scripts/pin.sh`). Every result records `accel`.
  **No latency or throughput is ever reported from TCG runs**, and on this shared server even KVM latency needs exclusive cores.
- **Over-provisioning without code changes.** Both FTLs have identical 32 GiB raw geometry and ignore `devsz_mb`, so exposing
  30 GiB leaves about 7% unused raw space as de-facto OP, close to the paper's 32+2 GiB. Both get the same `devsz_mb`.

## Phase 4: Deterministic guest-side run pipeline

- **Replay.** Rewrite `experiments/scripts/replay_trace.py` to use `O_DIRECT` with page-aligned buffers (mmap), QD1, strict trace order,
  and optional timestamp pacing. Writes use a seeded, non-zero pattern. Reads optionally verify their data.
  fio (`--direct=1`) is used only for preconditioning.
- **Guest setup before every run** (`experiments/scripts/guest_prep.sh`):
  - `nomerges=2`
  - `read_ahead_kb=0`
  - no filesystem
  - `scheduler=none`
  - `max_sectors_kb` large enough that requests aren't split
- **`experiments/scripts/run_femu.sh <ftl> <trace> <seed> <precondition>`.** One run is:
  1. Fresh image via `reset_femu_image.sh`.
  2. Boot the FTL binary.
  3. Wait for SSH (port 8080, key in `~/images`).
  4. `guest_prep`.
  5. Precondition (see below).
  6. Stats print (baseline snapshot).
  7. Workload.
  8. Stats print.
  9. Shut down.
  10. Parse the JSONL from the QEMU log into `results/<run_id>/` along with the manifest.
- **Preconditioning modes:**
  - `none`: matches current behaviour, kept for comparability.
  - `fill`: sequential write of the full logical space, so all reads are mapped. This sidesteps LeaFTL's unmapped-read artefact.
  - `steady`: fill, then random overwrite until GC is active and the write amplification factor (WAF) is stable.

## Phase 5: Validation gates (must pass before any baseline is reported)

These gates apply the Phase 2b invariant catalogue, evaluated by `validate_run.py` on every run.

1. **Request-flow fidelity** (invariants 1 and 10). With `FEMU_REQLOG` on, the per-request log equals the issued trace exactly
   (op, offset, length, order) on all 9 validation traces. This proves there is no merging, splitting, readahead or cache interference.
2. **All HARD invariants are 0** (invariants 2–7, 9 and 10) in every record, for both FTLs and every precondition mode.
   The KNOWN invariant (8, LeaFTL translation errors) is reported with its value in every result table.
3. **Invariant self-test:** the micro-traces that deliberately trigger each invariant's violation path detect it.
4. **Hand-checked micro-traces** of 10–50 ops, e.g.:
   - write one translation page, then read it back
   - read unwritten space
   - overwrite, then read

   Expected counters are worked out by hand from `experiments/docs/METRICS.md` and checked per FTL. These also confirm the documented
   bugs behave as described.
5. **Repeatability** (invariant 11). Three runs of the same configuration give identical counters. Latency is reported as mean ± CI.
   Any counter variance is a failure to investigate.
6. **Oracle sanity.** `experiments/scripts/belady.py` stays as a LeaFTL-keyed upper bound only (its optimistic insertion is documented).
   The measured LeaFTL hit rate must be ≤ the Belady value on the same read stream.

## Phase 6: Baseline matrix and results

- **Configs.** `experiments/configs/*.json` define FTL, `devsz_mb`, trace, seed and precondition mode.
  `experiments/scripts/run_matrix.py` iterates {LeaFTL, LearnedFTL} × workloads × preconditions {fill, steady} × 3 repeats.
- **Workloads, in two stages:**
  1. The existing synthetic suite (`traces/manifest.csv`) at 100k and then 1M ops.
  2. Real traces: Systor'17, UMass WebSearch, MSR Cambridge and FIU. A new `normalize_trace.py` remaps
     them into the shared logical footprint and records provenance and SHA-256.
- **Results.** One JSONL record per run, stamped with:
  - the commit, worktree switch diff and binary SHA
  - compiler, host and guest kernels, and the image SHA
  - the trace SHA and the config

  `aggregate.py` produces mean, sd, 95% CI, min and max, with all rates as fractions. It replaces the hand-edited `runs.csv`.
  `clone_run.py` and `update_csv.py` are retired, or kept only to migrate the old rows.
- **Mentor-facing summary CSV: `experiments/results/baseline_summary.csv`.** `aggregate.py` generates it from validated raw JSONL only,
  and it is regenerated on every aggregation, never hand-edited.
  It has **one row per (FTL × workload × precondition)**, aggregated over repeats. Column groups, left to right:
  1. **Identity:** `ftl`, `workload`, `workload_description`, `precondition`, `repeats`, `seeds`.
  2. **Configuration:**
     - `ents_per_tp` (256/512), `cmt_entries`, `cmt_entry_type` (segment / lpn)
     - `raw_capacity_gib`, `logical_capacity_gib`, `op_percent`
     - NAND read/write/erase latencies, `accel` (kvm/tcg)
  3. **Trace profile** (from `traces/profiles.json`, Phase 1b):
     - op count, read %, mean request size
     - footprint GiB, unique TPs (256/512)
     - sequentiality, top-20% skew
     - read-after-write share
     - Belady and LRU bounds
  4. **Mapping metrics** (each as `_mean`, `_sd`, `_ci95`):
     - translation-free read ratio
     - CMT hit rate (native definition)
     - model query / hit / correction / miss rates
     - single / double / triple read ratios
     - unmapped-read share
     - gap to the Belady bound
  5. **Device metrics:**
     - NAND reads (user / translation / GC)
     - NAND programs, WAF, GC count
     - P50 / P99 / P99.9 latency and throughput. These are blank for TCG runs, never estimated.
  6. **Validation:**
     - `hard_invariants_passed` (e.g. 10/10) and `failed_invariant_ids`
     - `known_translation_errors` (rate and count, LeaFTL bug impact)
     - `deterministic_across_repeats` (yes/no)
     - `request_flow_match` (yes/no)
  7. **Comparison with the paper:** `paper_reference` (e.g. LearnedFTL Fig. 14) and `paper_trend_consistent` (yes/no/n.a.).
  8. **Provenance:**
     - `repo_commit`, `binary_sha256`, `trace_sha256`, `image_sha256`
     - `compiler`, `host_kernel`, `guest_kernel`, `run_dates`
  9. **`notes`:** plain-language caveats for that row, e.g. "reads target unwritten space under `none` precondition".

  Two companion files:
  - **`experiments/results/baseline_summary_columns.csv`:** a data dictionary with `column`, `unit`, `definition`, and `source` (counter and file:line, or trace profile).
    It lets a mentor read the summary without other docs. All rates are fractions (0–1), and units are explicit.
  - **`experiments/results/baseline_runs.csv`:** the same columns per individual run, unaggregated, for drill-down.

  Rows with failed HARD invariants are **kept, with `hard_invariants_passed` showing the failure**, but are excluded from any headline claim.
- **Outputs:**
  - A translation-free / double-read ratio table per FTL per workload.
  - Hit rates per FTL using their native definitions.
  - Write amplification factor (WAF) and GC counts.
  - P50/P99/P99.9 latency and throughput (KVM on exclusive cores only; blank, and listed as pending, until that exists).
  - A table comparing our configuration with each paper's, plus the limitations: native-only translation-page sizes, the documented bugs, and no TPFTL/DFTL comparison.
  - A qualitative check against LearnedFTL's FIO random-read trends vs LeaFTL (fewer double reads, lower P99),
    with any mismatch explained.

## Milestones

- **M0:** Phases 0, 1 and 1b. Prerequisites done, issues and metrics documented, `TRACES.md` generated for all current traces.
- **M1:** Phase 2 instrumentation and the Phase 2b invariant checks (in FEMU and in `validate_run.py`) committed. LeaFTL and LearnedFTL both build via worktrees (Phase 3).
- **M2:** Phase 4 pipeline. All Phase 5 gates pass for both FTLs on the validation traces.
- **M3:** Phase 6 synthetic matrix at 1M ops, steady state, KVM. First baseline table.
- **M4:** Real traces added. Baselines frozen and git-tagged.

## Verification (end to end)

1. Run `build_ftl.sh leaftl` and `build_ftl.sh learnedftl`. Both binaries build, and `git status` on `main` is clean.
2. `run_femu.sh leaftl validation-traces/uniform-mixed.trace 45 fill` with `FEMU_REQLOG` set, then run `validate_run.py`.
   Every HARD invariant passes, and the report lists the KNOWN translation-error count.
   Repeat for `learnedftl`.
3. Run micro-traces for both FTLs. The counters equal the hand-computed expected values.
4. Repeat step 2 three times. The counters are identical.
5. `profile_traces.py` regenerates `TRACES.md` byte-identically on a second run. Every SHA-256 matches the manifest,
   and the expected counters for the validation traces equal the `read_lpns` / `write_lpns` measured in step 2.
6. Run `run_matrix.py --dry-run`, then a real run, then `aggregate.py`.
   `baseline_summary.csv`, `baseline_runs.csv` and `baseline_summary_columns.csv` regenerate byte-identically from raw JSONL alone.
   Every column in the summary has a dictionary entry, and a test in `aggregate.py` fails if one is missing.

## Status (updated 2026-11-09)

Living copy of this plan: `LearnedFTL/experiments/docs/BASELINE_PLAN.md`. Keep the two in sync, and update the status there.

**Done (nothing committed yet):**

- [x] Phase 0: shebang of `prepare_validation_traces.py` fixed, failed run directory deleted, `.gitignore` extended
- [x] Phase 0: GC-log path handled without root by the tested `LD_PRELOAD` shim `experiments/tools/gclog_redirect.c`
- [ ] Phase 0: KVM access. Blocked on the administrators because the user has no sudo and `/dev/kvm` is `root:kvm` with only `gdm` in its ACL. Not needed for counter-based baselines. Email sent 2026-11-09 to the professor who manages `nano`, requesting `usermod -aG kvm mohsin` or `setfacl -m u:mohsin:rw /dev/kvm`; also explained the move from SimpleSSD (pure software simulator, no virtualization needed) to FEMU (boots a real guest VM, needs KVM for timing-accurate latency/throughput only). Awaiting reply.
  - If it stays blocked: a 16 GB/sudo laptop (Omarchy/Arch) was evaluated as a fallback for the latency phase only — RAM is the binding constraint (device backend is a `devsz_mb`-sized allocation; `fill`/`steady` preconditioning writes the whole 30 GiB logical space, which won't fit in 16 GB, so that machine would be limited to the small validation traces or a scaled-down device size, with the comparability caveat that implies). Counter-based baselines stay on the server under TCG regardless.
- [x] Phase 1: `experiments/docs/KNOWN_ISSUES.md` (20 issues) and `experiments/docs/METRICS.md`
- [x] Phase 1b: `experiments/scripts/profile_traces.py`, `experiments/tools/cachesim.c` (LRU, Belady-OPT, in-cache LFU, perfect LFU), `experiments/tools/test_cachesim.py`, generated `experiments/traces/TRACES.md` and `profiles.json`
- [x] Phase 2, items 1–6 (not yet committed — item 7, the commit, is pending a decision): request-flow counters, the exhaustive read-LPN partition with HARD invariant checks, the optional `FEMU_REQLOG` per-request log, and the shared JSON schema are implemented in `leaftl.c`/`leaftl.h`, `ld-tpftl.c`/`ld-tpftl.h`, and a new `hw/femu/bbssd/ftl_stats.h`. Both FTLs rebuild cleanly (LeaFTL verified in the main build; LearnedFTL verified by temporarily swapping the `bb.c`/`meson.build` FTL switch, building, then reverting it — confirmed clean via `git diff`). Not yet run against a real trace, so the invariants are structurally reviewed but not runtime-exercised; that's Phase 5.
- [ ] Phase 2b onwards: not started

**Findings so far that shape the work:**

- LearnedFTL checks model predictions against the true page map for free (KI-20), so its double-read advantage may be overstated. Results report flash reads both as-coded and with a charge per wrong prediction.
- In the 100k-op validation traces, `seq-mixed` reads previously written data 0.00% of the time and `uniform-mixed` 2.21%. The old 0.03% LeaFTL hit rate was an artefact of reading unmapped space, so baselines need `fill` preconditioning.
- **New (found while reviewing the Phase 2 instrumentation): LeaFTL has an uncounted read path.** In `ssd_read`'s CMT-hit branch (`leaftl.c:~2535`), when the cached segment disagrees with the live one and the correction's backing GTD entry is itself unmapped/invalid, the authors' code does a bare `continue`. That LPN is counted in `cmt_hit_cnt` but charged no flash read and no `invalid_read_cnt` — without a dedicated counter, the read-cost invariant (`single+double+triple == cmt_hits+cmt_misses-invalid`) would fail spuriously whenever this is hit. Added `cmt_hit_invalid_gtd` to track it and folded it into that invariant. Worth a `KNOWN_ISSUES.md` entry alongside the related "corrected lookups are never cached" issue already documented there.
