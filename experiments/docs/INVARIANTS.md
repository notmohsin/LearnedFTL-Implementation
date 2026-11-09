# Invariant catalogue for the LeaFTL / LearnedFTL baselines

This is the evidence that a run's counters mean what `experiments/docs/METRICS.md` says they mean.
Each invariant below has an ID, a statement, where it is checked, and a class. **HARD** invariants
must be 0 (or satisfied) in every record of every run; a run with a non-zero HARD invariant is kept
in the raw data but excluded from any baseline claim. **KNOWN** invariants are expected to be non-zero
because of a documented authors' bug (`KNOWN_ISSUES.md`); their value is reported as that bug's
*measured* impact, never silently zeroed or excluded.

Checked at two levels:

- **In FEMU**, additively, at every stats print (`count_segments` in `leaftl.c` / `ld-tpftl.c`). Each
  gets its own `inv_<ID>_violations` counter in the JSON, and the first violation of a run is printed
  with the actual offending values (not just a count), so a failure is diagnosable from the log alone.
- **Offline**, by `experiments/scripts/validate_run.py`, on the JSONL stream, the optional `FEMU_REQLOG`
  file, and the issued trace. Several invariants (1, 3, 9, 10, 11) are *only* checkable offline — they
  compare FEMU's output against the trace or against other records in the same run, which no amount of
  in-FEMU bookkeeping alone can do.

Every invariant gets at least one micro-trace (10–50 ops) in `experiments/traces/micro/` that exercises
it, including a micro-trace that deliberately drives a KNOWN case (e.g. overwrite then read inside a
cached segment) to confirm the check actually fires rather than passing vacuously.

## Two corrections to the original plan, found while scoping this phase

Before writing the catalogue, the relevant data structures were checked directly rather than assumed
from the plan's first draft. Two things changed as a result:

1. **No separate "shadow page map" is needed for invariant 8.** Both FTLs already resolve every read
   through `get_maptbl_ent()` — `ssd->maptbl[lpn]`, a page-level table that is the actual ground truth
   both FTLs write flash from (`leaftl.c:98-106,578-580`; `ld-tpftl.c:156-164,880-882`). LeaFTL's
   `ssd_read` already compares its cached/segment prediction against `trueppa = get_maptbl_ent(...)`
   (`leaftl.c:2576`) before deciding single/double/triple; LearnedFTL's `model_predict` does the same
   (`ld-tpftl.c:2521-2525`) for free, per KI-20. So translation correctness is **already measured** by
   the existing model_hit/model_correction/model_miss counters (LeaFTL) and model_hit_num/model_miss_num
   (LearnedFTL), plus the new `cmt_hit_invalid_gtd` counter (Phase 2) for LeaFTL's uncounted path. No new
   32 MB shadow structure is being added — see invariant 8 below.
   - This does **not** check whether `maptbl` itself was ever written incorrectly (e.g. a GC migration
     writing the wrong LPN's entry). Auditing that would mean independently tracking flash placement,
     which starts to look like re-implementing the FTL rather than instrumenting it, so it is **out of
     scope** and noted as a limitation, not attempted here.
2. **The two FTLs differ on victim-line tracking** — both declare a `victim_line_list` that is commented
   out (`leaftl.h:222`, `ld-tpftl.h:322`) and use a priority queue (`victim_line_pq`) plus a self-reported
   `victim_line_cnt` for victim selection. But LearnedFTL *also* has a second, real, actively-used
   `QTAILQ_HEAD(victim_list, line) victim_list` (`ld-tpftl.h:324`, inserted/removed throughout
   `ld-tpftl.c`) that LeaFTL does not have. So invariant 7's line-count check differs per FTL: LeaFTL
   checks `free_line_cnt + full_line_cnt ≤ total_lines` (victims only self-reported, not independently
   walkable); LearnedFTL checks `free_line_cnt + full_line_cnt + victim_line_cnt ≤ total_lines` with
   `victim_line_cnt` cross-checked against an actual walk of `victim_list`. Either way the gap is at
   most the currently-open write-pointer line(s) or a line mid-GC.

## Catalogue

### 1. Flow conservation — HARD, offline only

Every `read_reqs`/`write_reqs`/`read_lpns`/`write_lpns` in the JSONL for a run equals what the trace
actually issued (computed by replaying the trace file, not by trusting the replay script). With
`FEMU_REQLOG` set, the per-request log (`seq,op,slba,nlb`) equals the trace file line-for-line, in order.
This is the proof that the guest's I/O path (merging, splitting, readahead, caching) didn't change the
request stream FEMU received — see KI-2/KI-19 in `KNOWN_ISSUES.md` for why this can't be assumed.

- **Checked:** `validate_run.py`, comparing JSONL + reqlog against the trace file.
- Needs **no new FEMU code** — `read_reqs`/`write_reqs`/`read_lpns`/`write_lpns` and `FEMU_REQLOG` already
  exist from Phase 2.

### 2. Read partition — HARD, in FEMU (already implemented in Phase 2)

Every read LPN lands in exactly one bucket of its FTL's own control flow:

- LeaFTL: `read_lpn_cnt = pre_first_write + buffer_hits + cmt_hit_cnt + cmt_miss_cnt`;
  `cmt_miss_cnt = unmapped_tp + model_queries`; `model_queries = model_hit + model_correction + model_miss`.
  (`leaftl.c` `count_segments`, the four `INVARIANT FAILED` checks added in Phase 2.)
- LearnedFTL: `read_lpns = access_cnt + unmapped_read_lpns`; `single_read + double_read + triple_read = access_cnt`;
  `cmt_write_hits + cmt_write_misses = write_lpns`.
  (`ld-tpftl.c` `count_segments`, the three checks added in Phase 2.)

- **Checked:** in FEMU (done), re-verified offline from the JSONL as a cheap cross-check.
- **Action this phase:** rename the generic `invariant_failures` bump sites to per-ID
  `inv_2_violations` (etc.) so a failure is attributable to a specific invariant rather than lumped
  together. Currently all of Phase 2's checks share one `invariant_failures` counter.

### 3. Causality — HARD, offline (checkable from existing JSON fields, no new FEMU code)

- `model_queries ≤ cmt_misses` (a model/segment is only consulted after a CMT miss) — both FTLs.
- `unmapped_tp ≤ cmt_misses` (LeaFTL).
- LearnedFTL's model is only tried when `bitmaps[lpn]` is set (`ld-tpftl.c:2693-2699`); there's no
  separate counter for "misses with bitmap set" yet, so this is checked as
  `model_use_num ≤ cmt_misses` for now, with a note that it's a looser bound until a
  `bitmap_set_on_miss` counter is added (optional, not blocking this phase).

- **Checked:** `validate_run.py`, reading the already-emitted JSON fields. No FEMU changes needed.

### 4. Read-cost accounting — HARD, in FEMU (extends Phase 2) + offline cross-check

- `single_reads + double_reads + triple_reads` = mapped reads served from flash (already checked per
  invariant 2's last clause).
- **New this phase:** an independent NAND-read counter, split by *purpose* (user data / translation
  page / GC), incremented at each call site that invokes `ssd_advance_status(..., NAND_READ)` — not
  inside `ssd_advance_status` itself (its `nand_cmd.type` is only `USER_IO`/`GC_IO`, which doesn't
  distinguish translation-page reads from data reads; call sites already know which kind they are,
  e.g. `leaftl.c:2171` is a translation-page read, `leaftl.c:2593/2604` are data reads). This is purely
  additive counting beside existing calls, not a change to `ssd_advance_status` or `nand_cmd`.
- Cross-check: `single_reads + 2·double_reads + 3·triple_reads` = `nand_reads_user + nand_reads_translation`
  (GC reads are excluded from the user-facing read-cost formula; they're reported separately).

- **Checked:** in FEMU (new counters) + `validate_run.py` (the cross-check arithmetic).

### 5. Write / NAND accounting — HARD, in FEMU (new) + offline

- NAND page programs = user data pages flushed + translation-page writes + GC-migrated pages, counted
  the same way as invariant 4 (additive counters beside existing `ssd_advance_status(..., NAND_WRITE)`
  call sites, classified by purpose at the call site).
- WAF = NAND programs / user-written LPNs, **derived** by `validate_run.py` from these counters, not
  taken from the authors' `wa_cnt`/`write_num` (KI-9/KI-17: these are since-boot deltas of unclear
  scope, kept and reported, but not treated as ground truth for WAF).

- **Checked:** in FEMU (new counters) + `validate_run.py` (WAF derivation).

### 6. Cache occupancy — HARD, in FEMU (read-only walk, new)

Both FTLs already track `used_cmt_entry_cnt`/`free_cmt_entry_cnt` against `tt_entries` in
`struct cmt_mgmt` (`leaftl.h:259-268`, `ld-tpftl.h:135-147`). The invariant is a read-only
cross-check, at every stats print, that this self-reported count agrees with an independent walk:

- `0 ≤ used_cmt_entry_cnt ≤ tt_entries` (capacity never exceeded).
- LeaFTL: walking `cm.cmt_entry_list` (a flat `QTAILQ`) counts exactly `used_cmt_entry_cnt` entries.
- LearnedFTL: walking `cm.TPnode_list` and summing each node's `cmt_entry_cnt` counts exactly
  `used_cmt_entry_cnt` entries (its CMT is two-level: TP nodes, then entries per node).
- The walk is read-only (no `QTAILQ_REMOVE`/insert), run only at stats-print time (twice per run), so
  it adds no per-request overhead.

- **Checked:** in FEMU, `count_segments`, new code.

### 7. Mapping / space consistency — HARD, in FEMU (end-of-run audit, new)

Corrected per the note above (victim-line tracking differs by FTL):

- LeaFTL: `free_line_cnt + full_line_cnt ≤ total_lines` (victims only self-reported via `victim_line_cnt`,
  not independently walkable, so they're not added to the left side).
- LearnedFTL: `free_line_cnt + full_line_cnt + victim_line_cnt ≤ total_lines`, with `victim_line_cnt`
  itself cross-checked against a walk of the real `victim_list`.
- Either way, the gap is explained by at most the currently-open write-pointer line(s) (one per FTL;
  LearnedFTL may have more than one active write pointer — check `struct write_pointer` usage before
  assuming exactly one).
- Per-block valid-page count (`line.vpc`, aggregated per block) equals the number of `maptbl` entries
  that currently resolve into that block — an O(`tt_pgs`) walk of `maptbl`, done only at stats-print
  time (not hot path).
- Every valid page's reverse map entry (`ssd->rmap[pgidx]`, `leaftl.c:169-181` / equivalent in
  `ld-tpftl.c`) points to an LPN whose `maptbl` entry resolves back to that exact page.

- **Checked:** in FEMU, `count_segments`, new code. This is the most expensive check (O(tt_pgs)), but
  it only runs twice per run.

### 8. Translation correctness — KNOWN for LeaFTL, KNOWN for LearnedFTL (not HARD — see correction above)

No new shadow structure. This invariant is **already measured** by existing per-read comparisons
against `maptbl` (the real ground truth both FTLs flush to), reported as:

- LeaFTL: `model_correction` rate (segment was cached but wrong) and `model_miss` rate, plus the new
  `cmt_hit_invalid_gtd` (Phase 2) for the uncounted correction-with-invalid-GTD path.
- LearnedFTL: `model_miss_num` rate (model consulted but wrong, per KI-20 — checked against `maptbl` at
  no flash-read cost in the authors' code).

Both are **KNOWN**, not HARD: they are the measured impact of the documented prediction-staleness bugs
(stale CMT after overwrite, no in-segment check, uninitialised `vppn` for LeaFTL), not something a
HARD gate should ever expect to be zero. Reported in every result table as a rate, never excluded.

- **Checked:** in FEMU (the counters already exist or were just added in Phase 2); reported, not gated,
  by `validate_run.py`.
- **Explicit limitation:** this does not catch `maptbl` itself being written incorrectly (e.g. by a GC
  migration bug) — see the correction note above. Not attempted this phase.

### 9. Snapshot integrity — HARD, offline (checkable from the JSONL alone, no new FEMU code)

- Resettable counters are non-decreasing *within* an interval and reset to 0 immediately after a print
  (true by construction since Phase 2's print-then-zero code, but worth asserting from outside).
- Non-resettable counters (`write_cnt`/`wa_cnt`/`gc_cnt`, `write_num`, `gc_runs`) are only ever used as
  before/after deltas between two consecutive JSONL records of the same run — `validate_run.py` must
  never read them as absolute values.

- **Checked:** `validate_run.py`, by diffing consecutive records of a run's JSONL stream.

### 10. Trace-derived expectations — HARD, offline (the reference-model check)

`validate_run.py` replays the trace's address stream through a small reference model (tracking which
LPNs have been written since the last precondition) and predicts, independent of FEMU: exact
`read_lpns`/`write_lpns`, the count of never-written read LPNs (which must equal LearnedFTL's
`unmapped_read_lpns` exactly, and be ≤ LeaFTL's `invalid_read_cnt` + `unmapped_tp`-derived count, since
LeaFTL's unmapped accounting is coarser — see KI-3), and the request-size histogram.

- **Checked:** `validate_run.py` only. Depends on `experiments/traces/profiles.json` (Phase 1b) for the
  per-trace expected values where those overlap (footprint, never-written reads).

### 11. Determinism — HARD, offline, cross-run

Three runs of the same FTL/trace/seed/precondition/accel give byte-identical counters in every JSONL
field except latency (which doesn't exist yet under TCG) and wall-clock fields. Any variance is a bug
to investigate before any baseline is trusted, not averaged away.

- **Checked:** `validate_run.py`, comparing 3 runs' JSONL records field-by-field.

### 12. Energy accounting — HARD, in FEMU (new)

`read_joule`/`write_joule`/`erase_joule` (`struct statistics`) are accumulated inside
`ssd_advance_status` at a single chokepoint — `leaftl.c:811,835,863`, identical constants in
`ld-tpftl.c:1183,1207,1234` — as `+= 3.5` per `NAND_READ`, `+= 16.7` per `NAND_WRITE`, `+= 132` per
`NAND_ERASE`. This is a pure function of operation counts, not wall-clock time, so unlike latency it is
fully valid to check under TCG.

- `read_joule ≈ (nand_reads_user + nand_reads_translation + nand_reads_gc) × 3.5`
- `write_joule ≈ (nand_writes_user + nand_writes_translation + nand_writes_gc) × 16.7`
- `erase_joule ≈ nand_erases × 132` — a new counter, since erase has no user/translation/GC split (it's
  always GC-triggered structurally; one call site in `leaftl.c`, two in `ld-tpftl.c`, both GC functions).
- `≈` means compared with an absolute tolerance (`1e-6`), not exact equality: `16.7` isn't exactly
  representable in binary floating point, so summing it millions of times can differ from a single
  `count × 16.7` by a few ULPs. `1e-6` is far below the ~3.5+ joule gap a single real miscounted
  operation would cause, so it can't mask a genuine mismatch.

This is also a strong cross-check of invariants 4 and 5's own new counters: they were added beside the
same `ssd_advance_status` calls that increment the joule counters, by separate code, so a mismatch here
means one of those call sites was missed or miscounted.

- **Checked:** in FEMU, `count_segments`, new code (`inv12_violations`).

## Status

| ID | Statement | Class | FEMU code | Offline code |
|----|-----------|-------|-----------|---------------|
| 1 | Flow conservation | HARD | n/a (uses Phase 2 counters) | to do |
| 2 | Read partition | HARD | done (Phase 2 + Phase 2b buffer_hits fix) | to do (cross-check) |
| 3 | Causality | HARD | n/a (uses existing fields) | to do |
| 4 | Read-cost accounting | HARD | done (Phase 2b, incl. buffer_hits fix) | to do |
| 5 | Write/NAND accounting + WAF | HARD | done (Phase 2b, counters only; no C cross-check) | to do (WAF) |
| 6 | Cache occupancy | HARD | done (Phase 2b, read-only walk) | n/a |
| 7 | Mapping/space consistency | HARD | done (Phase 2b, end-of-run audit) | n/a |
| 8 | Translation correctness | KNOWN | done (Phase 2 + existing counters) | to do (report, not gate) |
| 9 | Snapshot integrity | HARD | n/a | to do |
| 10 | Trace-derived expectations | HARD | n/a | to do (reference model) |
| 11 | Determinism | HARD | n/a | to do (3-run diff) |
| 12 | Energy accounting | HARD | to do (new `nand_erases` counter + check) | n/a |

Per-invariant micro-traces (10–50 ops, `experiments/traces/micro/`) and the `inv_<ID>_violations`
renaming for invariant 2 are also to do.
