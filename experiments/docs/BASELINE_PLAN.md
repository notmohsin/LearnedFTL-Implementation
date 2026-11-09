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

## Status (updated 2026-11-10)

Living copy of this plan: `LearnedFTL/experiments/docs/BASELINE_PLAN.md`. Keep the two in sync, and update the status there.

### Implemented

| Phase | Deliverable | Where |
|---|---|---|
| 0 | GC-log shim (also makes FEMU's stdout line-buffered); KVM pending (request sent to the server's professor) | `experiments/tools/gclog_redirect.c` |
| 1, 1b | KNOWN_ISSUES (KI-1..KI-27), METRICS, TRACES.md for 31 traces | `experiments/docs/`, `experiments/traces/` |
| 2 | Request-flow counters, read partition, `FEMU_REQLOG`, shared JSON schema | `hw/femu/bbssd/{leaftl,ld-tpftl}.c`, `ftl_stats.h` |
| 2b | Invariants 1-12 (in FEMU and offline), per-LPN flash-read accounting, micro-traces, validator self-test | `INVARIANTS.md`, `validate_run.py`, `test_validate_run.py`, `traces/micro/` |
| 3 | One binary per FTL from a commit (git worktrees), KVM/TCG launcher, 30 GiB device (~7% OP) | `build_ftl.sh`, `launch_femu.sh` |
| 4 | O_DIRECT QD1 replay with read verification, guest prep, one-command runs | `replay_trace.py`, `guest_prep.sh`, `run_femu.py` |
| 5, 6 | Resumable parallel stages, mentor CSVs with data dictionary | `run_matrix.py`, `experiments/configs/*.json`, `aggregate.py` |
| M4 | Real traces: UMass WebSearch1-3 and Financial1-2 (first 1M requests, normalised) | `normalize_trace.py`, `experiments/real-traces/*.json` |

### Deviations from the plan, with the measurement behind each

1. **Preconditioning.** `steady` (fill + random overwrite until WAF is stable) is dropped and a `warm` mode added.
   After a full 30 GiB fill, LearnedFTL's GC runs and retrains its models on almost every write once fewer than 4
   lines are free (its own "what's wrong?" message): 8 random 64 KiB writes/s, so a 30 GiB overwrite would take ~17 h
   per run, and a 100k-op `fill` run takes ~1.7 h. The FTL thread is the bottleneck (100% of a core, guest idle), so
   KVM would not help. Stages now use: `none` (empty device, shows the unmapped-read artefact), `warm` (the trace's
   footprint written once, so every read targets written data; the main condition), and `fill` (whole device,
   GC-stress) on the 100k validation traces only, with one run per trace for LearnedFTL.
2. **Read cost is measured, not taken from the authors' `read_cnt`** (KI-26), and headline metrics are defined on
   mapped read LPNs (METRICS.md section 4).
3. **No shadow page map**: `maptbl` is already the ground truth both FTLs program from (INVARIANTS.md).
4. **Repeats.** Under TCG every counter is deterministic and the seed only changes the written data pattern, so the
   validation stage runs three identical repeats per configuration (invariant 11) and the 1M matrix runs once per
   configuration, with three repeats for two workloads. Means have zero variance; CIs are reported as 0.
5. **zipf-80-20** is only 100k requests long in `traces/`, so it is in the validation stage only.
6. **Real traces.** MSR Cambridge, FIU and Systor'17 need a SNIA IOTTA licence login and are not included; the
   UMass WebSearch traces used by the LearnedFTL paper are.
7. **LeaFTL invariant 7** (blocks, reverse map, stale entries) is KNOWN, not HARD, because LeaFTL's GC breaks its own
   mapping state (KI-21..KI-24); it stays HARD for LearnedFTL.
8. **Memory.** FEMU pins its whole DRAM backend (~32 GB per VM) when allowed to; runs set `MLOCK=0` so memory is
   allocated as written. Use `MLOCK=1` for KVM latency runs.

### Results (2026-11-10)

All runs use TCG on commit `5f2e66c` (both FTL binaries), the 30 GiB device, and the scripts at the commit that
produced `experiments/results/baseline_summary.csv`.

- **186 runs, 186 valid** (every HARD invariant passes): 8 micro, 144 validation (100k ops: none / warm / fill),
  24 matrix (1M ops, warm), 10 real (UMass, 1M requests, warm).
- **Request flow**: FEMU received exactly the trace, request by request, in every run (invariant 1).
- **Determinism**: all 49 groups with repeats produced identical counters (invariant 11); 39 configurations are single runs.
- **Micro-traces**: all 8 match the hand-derived expectations; the validator self-test catches all 13 mutations.

Main condition (`warm`, 1M requests, LeaFTL / LearnedFTL):

| Workload | Translation-free | Double reads | Flash reads per mapped read | LeaFTL lost-data share | LeaFTL gap to Belady |
|---|---|---|---|---|---|
| financial1 | 0.801 / 0.829 | 0.385 / 0.162 | 1.74 / 1.18 | 0.083 | 0.182 |
| financial2 | 0.500 / 0.849 | 0.393 / 0.136 | 2.30 / 1.17 | 0.301 | 0.363 |
| mixed-50-seq-50-random | 0.503 / 0.768 | 0.511 / 0.186 | 2.37 / 1.28 | 0.553 | 0.411 |
| seq-mixed | 0.955 / 0.845 | 0.829 / 0.150 | 1.89 / 1.16 | 0.303 | 0.005 |
| uniform-mixed | 0.300 / 0.702 | 0.352 / 0.214 | 2.59 / 1.38 | 0.628 | 0.619 |
| uniform-read | 0.364 / 0.701 | 0.417 / 0.299 | 2.52 / 1.30 | 0.583 | 0.536 |
| websearch1 | 0.742 / 0.714 | 0.584 / 0.286 | 2.02 / 1.29 | 0.235 | 0.249 |
| websearch2 | 0.738 / 0.710 | 0.590 / 0.290 | 2.04 / 1.29 | 0.236 | 0.253 |
| websearch3 | 0.742 / 0.720 | 0.582 / 0.280 | 2.02 / 1.28 | 0.222 | 0.249 |
| zipf-hot10-access90-a1.2 | 0.226 / 0.954 | 0.215 / 0.033 | 2.76 / 1.06 | 0.078 | 0.028 |
| zipf-hot20-access80-a0.8 | 0.560 / 0.798 | 0.526 / 0.149 | 2.35 / 1.26 | 0.227 | 0.269 |
| zipf-hot20-access80-a1.2 | 0.240 / 0.922 | 0.230 / 0.057 | 2.74 / 1.10 | 0.153 | 0.037 |

How to read it:

- LearnedFTL needs fewer double reads than LeaFTL in 19 of 20 `warm` comparisons (the exception is `zipf-80-20` at
  100k, where LeaFTL's reads are mostly triple reads instead), consistent with the LearnedFTL paper's trend. Its
  flash reads per read are 1.06-1.38 against LeaFTL's 1.74-2.76.
- LeaFTL's numbers cover only the reads it served: it fails to return 8-63% of reads of written data (KI-27, KI-28).
  Any comparison must quote this alongside.
- LeaFTL's gap to Belady-OPT is 0.6 on uniform traces, ~0.25 on WebSearch, and ~0.03 on the skewed zipf traces:
  this is the headroom a better replacement policy could target for a TP-keyed cache.
- `fill` rows are GC stress only: LeaFTL's translation writes explode (WAF 20-91, KI-21) and LearnedFTL thrashes
  (WAF up to 5,338, KI-29). Neither is a cache comparison.
- LearnedFTL's advantage is flattered by two accounting gaps (KI-20 free misprediction checks, KI-25 uncharged
  translation writes); `flash_reads_per_mapped_read_mispred_charged` and the NAND write split show their size.

### Findings so far

- **LeaFTL (the LearnedFTL authors' FEMU port) has correctness bugs that dominate its numbers once GC runs**: GC
  rewrites the translation pages of the host write buffer instead of the migrated LPNs (KI-21, WAF ~82 on
  `uniform-mixed` after `fill`), never programs migrated data (KI-22), serves reads from invalidated pages in the
  write-buffer window (KI-23), loses ~5% of reads of written data after GC (KI-24), and silently drops host writes
  through its buffer duplicate check (KI-27; the first request touching LPN 0 is always lost).
- LearnedFTL detects wrong model predictions for free (KI-20) and never charges translation-page writes (KI-25):
  both flatter it relative to LeaFTL and are reported separately.
- In the 100k-op validation traces, `seq-mixed` reads previously written data 0.00% of the time and `uniform-mixed`
  2.21% under `none`; the old 0.03% LeaFTL hit rate was this artefact.

### Pending

- [ ] Latency and throughput (needs KVM; the launcher already selects it when `/dev/kvm` is usable).
- [ ] A same-FTL 256-vs-512 entries-per-TP sweep (needs a constant change; out of scope by decision).
