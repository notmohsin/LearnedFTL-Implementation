# Invariant catalogue for the LeaFTL / LearnedFTL baselines

Invariants are the evidence that a run's counters mean what `METRICS.md` says. Each has an ID, a statement, where
it is checked, and a class:

- **HARD**: must hold in every run. A run with a failed HARD check is kept in the raw data and in
  `baseline_runs.csv`, but is excluded from any baseline claim.
- **KNOWN**: expected to be non-zero because of a documented authors' bug (`KNOWN_ISSUES.md`). The value is
  reported as that bug's measured impact, never zeroed or hidden.
- **REPORT**: a derived value (for example WAF), recorded with the checks.

They are checked at two levels:

- **In FEMU**, additively, at every stats print (`count_segments` in `leaftl.c` / `ld-tpftl.c`). Each family has its
  own `inv<N>_violations` counter in the JSON record and prints the offending values on failure, so a violation is
  diagnosable from `femu.log` alone.
- **Offline**, by `experiments/scripts/validate_run.py`, on the run's records, FEMU's request log and the trace.
  Invariants 1, 3, 9, 10 and 11 can only be checked offline because they compare FEMU's output with the trace,
  with other records, or with other runs.

`run_femu.py` runs the validator after every run and writes `validation.json`; `run_matrix.py` runs the
determinism comparison (invariant 11) across repeats.

## How the design changed when it met real runs

The first draft of this catalogue was written from the code. Running it changed four things, all recorded here
rather than silently adjusted:

1. **No separate shadow page map.** Both FTLs resolve every read through `get_maptbl_ent()` (`ssd->maptbl`), the
   page-level table they program flash from, so translation correctness is measured against it directly
   (invariant 8). Whether `maptbl` itself is ever written wrongly is checked structurally (invariant 7), not by
   re-implementing the FTL.
2. **The authors' `read_cnt` cannot be used for read cost.** It counts a translation-page read that never happens
   when the page is unmapped, misses the translation-page read on LeaFTL's stale-segment path, and counts write-buffer
   hits as single reads (KI-26). Read cost is therefore measured per read LPN: `ssd_read` snapshots the NAND read
   counters when each LPN's loop iteration starts and attributes the difference to that LPN (`ftl_stats.h`).
3. **Since-boot counters.** `wa_cnt`/`write_num`, `gc_cnt` and `gc_runs` are never zeroed by a stats print (KI-17), so
   they are compared as interval deltas.
4. **LeaFTL's GC breaks its own mapping state** (KI-21 to KI-24). The block, reverse-map and stale-entry parts of
   invariant 7 are KNOWN for LeaFTL and HARD for LearnedFTL.

The victim-line lists also differ: LeaFTL tracks victims only in a priority queue with a self-reported count,
while LearnedFTL additionally keeps a walkable `victim_list` (`ld-tpftl.h:324`).

## Catalogue

### 1. Flow conservation (HARD, offline)

FEMU received exactly what the trace issued.

- `read_reqs`, `write_reqs`, `read_lpns`, `write_lpns` of the workload record equal the counts recomputed from the
  trace file.
- FEMU's request log (`FEMU_REQLOG`, one `seq,op,slba,nlb` line per request, `#print` markers at stats prints),
  cut at the last two markers, equals the trace line by line: same operation, start sector and length, same order.
- The guest replay finished with the trace's request counts and no read-verification errors.

This proves no merging, splitting, readahead, page cache or background I/O reached the device. The first run without
`guest_prep.sh` stopping udev failed it: udev re-probed the device after the replay closed it (264 extra 4 KiB reads
at offset 0 and at the GPT backup location).

### 2. Read partition (HARD, in FEMU and offline)

Every read LPN leaves `ssd_read` through exactly one exit.

- LeaFTL: `read_lpns = pre_first_write + buffer_hits + cmt_hit_invalid_gtd + invalid_read_cnt + true_unmapped +
  served_lpns`; also `read_lpns = pre_first_write + buffer_hits + cmt_hits + cmt_misses`,
  `cmt_misses = unmapped_tp + model_queries`, `model_queries = model_hits + model_corrections + model_misses`.
- LearnedFTL: `read_lpns = access_cnt + unmapped_read_lpns`, `served_lpns = access_cnt`,
  `single + double + triple = access_cnt`, and on the write path `cmt_write_hits + cmt_write_misses = write_lpns`.
- Both (offline): `read_lpns = mapped + unmapped`, the served-reads histogram sums to `served_lpns`, and no served
  LPN was served with zero flash reads.

### 3. Causality (HARD, offline)

- Served CMT hits ≤ CMT hits; translation-free served LPNs ≤ served LPNs.
- LeaFTL: `model_queries ≤ cmt_misses`, `unmapped_tp ≤ cmt_misses` (a model is consulted only after a miss).
- LearnedFTL: model predictions ≤ `read_lpns − cmt_hits` (tried only on a CMT miss), and no served LPN needs a
  third flash read.

### 4. Read-cost attribution (HARD, in FEMU)

Every flash read issued inside `ssd_read` (measured as a per-request delta) is attributed to exactly one read LPN:
`served_flash_reads + unmapped_flash_reads = read_path_flash_reads`. The early exits (pre-first-write, buffer hit,
stale segment with unmapped GTD) are given no reads, so a read issued on one of those paths fails this check.
Offline, read-path reads must not exceed all user + translation reads.

### 5. Write / NAND accounting (HARD, offline)

Our NAND program counters, split by purpose (`nand_writes_user/translation/gc`), equal the authors' own NAND write
counter (`wa_cnt` for LeaFTL, `write_num` for LearnedFTL) over the interval. WAF is derived from our counters
(REPORT). Uncharged cases in the authors' code are documented, not patched: LeaFTL never programs GC-migrated data
(KI-22), LearnedFTL never charges translation-page writes (KI-25).

### 6. Cache occupancy (HARD, in FEMU)

At every stats print, a read-only walk of the CMT agrees with the authors' own `used_cmt_entry_cnt`, which stays
within `[0, tt_entries]`. LeaFTL: length of `cm.cmt_entry_list`. LearnedFTL: sum of `cmt_entry_cnt` over
`cm.TPnode_list`.

### 7. Mapping / space consistency (HARD for LearnedFTL; partly KNOWN for LeaFTL, in FEMU)

A read-only audit at every stats print (an O(`tt_pgs`) walk, twice per run):

- **Line lists** (HARD for both): walked free/full (and LearnedFTL victim) lists match their counters, and their sum
  does not exceed the number of lines (the gap is the open write-pointer lines).
- **Blocks**: each block's `vpc` equals its pages in state `PG_VALID` and the number of map entries pointing into it.
  Data blocks are owned by `maptbl`; translation blocks (LeaFTL `TRANS` lines, LearnedFTL `GTD` lines) by the GTD.
- **Reverse map**: every valid page's `rmap` entry resolves back to that page through `maptbl` or the GTD.
- **Stale entries**: map entries pointing at a non-valid page. Allowed only for LPNs whose newer version is still in
  the write buffer: at most `buffer_size` (2048) for LeaFTL, 0 for LearnedFTL.

LeaFTL passes all four on runs without GC. After GC it fails the block, reverse-map and stale-entry parts (KI-24),
which are therefore KNOWN for LeaFTL and reported as counts (`inv7_block_mismatches`, `inv7_rmap_mismatches`,
`stale_map_entries`).

### 8. Translation correctness (KNOWN for LeaFTL; partly HARD for LearnedFTL)

Measured against `maptbl`, the ground truth both FTLs program from:

- LeaFTL (KNOWN): `model_corrections`, `model_misses`, `cmt_hit_invalid_gtd`, `invalid_but_mapped` (written data the
  FTL could not resolve), `served_stale_page` (reads served from an invalidated page, KI-23), `readcnt_mismatch`.
- LearnedFTL: `model_misses` (wrong predictions, caught at no flash cost, KI-20) is KNOWN; `served_stale_page == 0`
  is HARD, because LearnedFTL updates its map at write time.

### 9. Snapshot integrity (HARD, offline)

Each run has a baseline record (after preconditioning) and a workload record; since-boot counters (`gc_cnt`,
`gc_runs`, `nand_page_writes`) never decrease between them; and the baseline record's request counts equal the
request log before the first marker, which shows the print zeroed the per-interval counters.

### 10. Trace-derived expectations (HARD, offline)

A reference model replays the trace, tracking which LPNs have been written (everything is written after `fill` or
`steady`), and predicts the request-size histograms exactly and the number of never-written read LPNs. LearnedFTL's
unmapped reads must equal it; LeaFTL's must be at least it (LeaFTL cannot return data that was never written). The
excess for LeaFTL is reported as KNOWN lost data (`lost_data_read_share`).

### 11. Determinism (HARD, offline, across runs)

Repeats with the same FTL, trace, seed, precondition and device size produce identical baseline and workload
records, field for field. Under TCG this includes every counter; there is no latency field to exclude.

### 12. Energy accounting (HARD, in FEMU)

`read_joule`, `write_joule`, `erase_joule` are accumulated inside `ssd_advance_status` from operation counts only
(`+= 3.5` per read, `16.7` per write, `132` per erase; `leaftl.c:811,835,863`, `ld-tpftl.c:1183,1207,1234`), so this
holds under TCG. They must equal our NAND read/write/erase counters times those constants within 0.5: a single
miscounted operation moves a total by at least 3.5, while `long double` accumulation of 16.7 drifts by about 1e-4 over
10^7 additions (observed: 5e-5 after 2.3e7). This independently confirms that the per-purpose counters of
invariants 4 and 5 cover every NAND operation.

## Self-tests

- `experiments/scripts/check_micro.py` with the micro-traces in `experiments/traces/micro/` (hand-derived expected
  counters, including traces that deliberately trigger KNOWN cases: buffered reads, reads of unwritten LPNs,
  overwrite-then-read).
- `experiments/scripts/test_validate_run.py` mutates a real run's records, request log and replay summary and
  checks that the validator reports the expected invariant as failed.

## Status

| ID | Statement | Class | Where |
|----|-----------|-------|-------|
| 1 | Flow conservation | HARD | offline |
| 2 | Read partition | HARD | FEMU + offline |
| 3 | Causality | HARD | offline |
| 4 | Read-cost attribution | HARD | FEMU + offline |
| 5 | Write/NAND accounting (WAF reported) | HARD | offline |
| 6 | Cache occupancy | HARD | FEMU |
| 7 | Mapping/space consistency | HARD (LearnedFTL), line lists HARD + rest KNOWN (LeaFTL) | FEMU |
| 8 | Translation correctness | KNOWN (LearnedFTL stale reads HARD) | FEMU |
| 9 | Snapshot integrity | HARD | offline |
| 10 | Trace-derived expectations | HARD (+ KNOWN lost data) | offline |
| 11 | Determinism | HARD | offline, across repeats |
| 12 | Energy accounting | HARD | FEMU |
