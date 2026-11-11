# Known issues in the LeaFTL and LearnedFTL implementations

These are behaviours of the authors' code (HPCA'24 LearnedFTL repository, including its FEMU port of
LeaFTL) that affect measurements. **We do not modify the authors' FTL logic or constants.** Each issue
is documented, and the experiment design either works around it or measures its effect.

Line numbers refer to commit `e37f3be56` plus the uncommitted measurement instrumentation of
2026-11-08. Re-check them if the files change.

Severity:
- **Blocker**: makes a run crash or produces meaningless numbers unless handled.
- **Metric**: changes what a counter means or inflates/deflates it.
- **Fidelity**: the code deviates from the paper's described design. Results reflect the code, not the paper.

| ID | FTL | Severity | Summary |
|---|---|---|---|
| KI-1 | both | Blocker | Statistics reset command is a no-op |
| KI-2 | LeaFTL | Blocker | Hard-coded GC log path crashes on first ch0/lun0 erase if the directory is missing |
| KI-3 | LeaFTL | Metric | Reads of unmapped translation pages are counted as CMT misses |
| KI-4 | LeaFTL | Metric | `vppn` read uninitialised / stale after an unmapped miss |
| KI-5 | LeaFTL | Fidelity | Write-buffer lookup only checks slot 0 of 2048 |
| KI-6 | LeaFTL | Fidelity | CMT entries are never invalidated on overwrite, flush or GC |
| KI-7 | LeaFTL | Fidelity | CMT hit path never checks the LPN is inside the cached segment |
| KI-8 | LeaFTL | Fidelity | Only accurate segments are cached; corrected lookups never are |
| KI-9 | LeaFTL | Fidelity | Last LPN of each buffer flush is never put in a segment |
| KI-10 | LeaFTL | Fidelity | LPN 0 is used as "no update" sentinel for translation-page updates |
| KI-11 | LeaFTL | Metric | Reads before the first write skip the FTL and get 0 latency |
| KI-12 | LeaFTL | Metric | `gc_cnt` increments even when GC finds no victim |
| KI-13 | LeaFTL | Metric | Read counts printed with `%d` (32-bit) |
| KI-14 | LearnedFTL | Metric | Write path fills the CMT but write lookups are not counted |
| KI-15 | LearnedFTL | Metric | CMT hits on unmapped LPNs are re-labelled `model_out_range` |
| KI-16 | LearnedFTL | Metric | No single/double/triple read counters |
| KI-17 | both | Metric | Write/GC/erase counters (`write_cnt`, `gc_cnt`, `wa_cnt`) are never reset |
| KI-18 | both | Blocker | No over-provisioning: logical size equals raw flash |
| KI-19 | both | Metric | Latency depends on host wall-clock time |
| KI-20 | LearnedFTL | Fidelity | Model predictions are checked against the true page map for free; wrong predictions cost no flash read |

---

### KI-1: Statistics reset is a no-op (both)

- **Where:** `hw/femu/bbssd/bb.c:27-72`. `reset_stat()` has its whole body commented out. It is reached by
  FEMU flip command `cdw10=8` (`bb.c` `bb_flip`), which `experiments/scripts/femu_stat.py` can send.
- **Effect:** counters accumulate from VM boot. Guest boot, partition probing and udev reads land in the
  first printed record. This is the most likely cause of the constant `cmt_hits=131` / `model_queries=4`
  seen in every sequential run in the old hand-edited `runs.csv` (removed; in git history).
- **Note:** the stats *print* (`cdw10=9`, `count_segments()`) zeroes most counters after printing
  (`leaftl.c` end of `count_segments`, `ld-tpftl.c:2546-2551`).
- **Handling:** the run script prints once immediately before the workload and discards that record as a
  baseline snapshot, then prints after the workload (plan Phase 2 item 4, invariant 9).

### KI-2: Hard-coded GC log path (LeaFTL)

- **Where:** `leaftl.c:2583` opens `/home/lzh/femu/log/gc_frequency.txt` with no NULL check.
  `ssd_advance_status` sets `flag=true` for `ch==0 && lun==0` (`leaftl.c:777-779`) and writes to the file on
  every erase there (`leaftl.c:834-835`).
- **Effect:** if `/home/lzh/femu/log/` does not exist, `gc_fp` is NULL and the first erase on ch0/lun0
  (i.e. the first GC that reaches that LUN) crashes QEMU. Runs without GC never hit it, which is why no
  run so far has crashed (every old run shows `gc=0`).
- **Handling:** with root, create the directory (`sudo mkdir -p /home/lzh/femu/log && sudo chown "$USER" /home/lzh/femu/log`).
  Without root (our case: a shared server, no sudo), the run script preloads `experiments/tools/gclog_redirect.so`
  and sets `FEMU_GC_LOG_DIR`, which redirects `fopen`/`fopen64` of that exact path into the run's results directory.
  The authors' code is untouched, and `experiments/tools/test_gclog_redirect.sh` tests the shim. The QEMU binary
  imports `fopen64` dynamically from glibc, so the redirect applies to it.

### KI-3: Unmapped translation-page reads counted as CMT misses (LeaFTL)

- **Where:** on a CMT miss, `ssd_read` counts `cmt_miss_cnt++` (`leaftl.c` read loop, `else` branch) and calls
  `process_translation_page_read()`, which returns NULL **without querying the model or inserting** when
  the translation page has no GTD mapping (`leaftl.c:1143-1148`).
- **Effect:** reads to never-written regions (or regions whose translation page has not been flushed yet)
  are permanent misses. In the old seq-mixed runs the trace reads only addresses that were never written,
  so the measured 0.03% hit rate is an artefact. LearnedFTL excludes such reads from its hit/miss counts
  (KI-15), so the two FTLs' raw hit rates are not comparable without correction.
- **Handling:** precondition `fill` makes every read mapped. The read-LPN partition (invariant 2) reports
  unmapped reads as their own bucket, and `experiments/traces/TRACES.md` reports each trace's
  read-after-write share up front.

### KI-4: `vppn` uninitialised or stale (LeaFTL)

- **Where:** `uint64_t vppn;` is declared without an initialiser in `ssd_read` (`leaftl.c` start of `ssd_read`).
  When KI-3's early return happens, `process_translation_page_read` never assigns `*ppn`, so the following
  `if (vppn == INVALID_VAL)` tests an uninitialised value (first LPN) or the previous LPN's value.
  The inaccurate-segment branch of `segment_search_simple_segment` (`leaftl.c:1512-1534`) also leaves `*ppn`
  unset when the LPN is not found in the conflict-resolution buffer.
- **Effect:** undefined behaviour in C. In practice the stale value usually differs from `INVALID_VAL`, so the
  read falls through to the true-map check (`get_maptbl_ent`), finds the LPN unmapped, charges 0 latency and
  counts a **single read** (`read_cnt` is 1 from the miss). This inflates `single_reads` for unmapped reads.
- **In practice:** FEMU's counters are reproduced exactly (`leaftl_replica.py`) only when `vppn` is modelled as
  keeping the value the previous `ssd_read` call left in the same stack slot, across requests.
- **Handling:** precondition `fill`. Invariant 4 (read-cost accounting) cross-checks single/double/triple
  against NAND reads actually issued, which exposes reads counted as "single" with no flash read.

### KI-5: Write-buffer lookup checks only slot 0 (LeaFTL)

- **Where:** `for (int i = 0; i < 1; i++)` over `buffer_lpns` in `ssd_read` (`leaftl.c` read loop).
  The buffer holds `buffer_size = 2048` LPNs (`leaftl.c` `ssd_init_params`).
- **Effect:** reads of data still in the write buffer go to the CMT path instead of being served from DRAM.
  After a flush the buffer is sorted and not cleared, so slot 0 is effectively an arbitrary stale LPN.
- **Handling:** documented only. The read-LPN partition reports `buffer_hits` separately, so its rarity is visible.

### KI-6: CMT never invalidated (LeaFTL)

- **Where:** CMT insert/evict happen only in `process_translation_page_read` (`leaftl.c:1155-1164`). The write
  path, `buffer_flush`, `GC_flush`, `compact` and GC never touch cached segments.
- **Effect:** after an overwrite, a cached segment can predict the old PPN. The read path compares against the
  true page map (`get_maptbl_ent`) and charges one **extra flash read** when the prediction is wrong, so data
  is still correct but double/triple reads rise.
- **Handling:** measured. Invariant 8 counts mispredicted reads (prediction ≠ `maptbl`) by cause.

### KI-7: Hit path does not check segment range (LeaFTL)

- **Where:** a CMT hit calls `segment_search_simple_segment(&cmt_ent->sm, lpn, ...)` (`leaftl.c` read loop), which
  evaluates the cached segment's line for **any** LPN in the translation page, inside or outside the segment's
  `[slpa, slpa+len)` range (`leaftl.c:1503-1511`).
- **Effect:** LPNs outside the cached segment get a prediction from the wrong segment, almost always wrong, so an
  extra flash read follows (counted as double/triple). These count as CMT *hits*.
- **Handling:** measured by invariant 8 (out-of-segment category).

### KI-8: Only accurate segments cached (LeaFTL)

- **Where:** `process_translation_page_read` inserts only if `sm.len >= 0` (`leaftl.c:1154`); `sm.len` is set only on
  the accurate path of the lookup. The CMT "refresh" branch in `ssd_read` (`!is_same_segment(...)`) is effectively
  dead because the hit path copies the cached segment into `lru_en` unchanged.
- **Effect:** translation pages whose segments are approximate are never cached, so every read of them misses.
- **Handling:** documented. Visible as `cmt_misses` with `model_corrections`.

### KI-9: Last LPN of each flush never segmented (LeaFTL)

- **Where:** learning loop at the end of `buffer_flush` (`leaftl.c` around 2165-2190):
  when `i == buffer_size-1` the segment length is `i - last_pointer`, which excludes element `i`.
- **Effect:** one LPN per 2048-LPN flush has no learned segment. Unless an older segment covers it, the lookup is a
  model miss, `vppn` becomes `INVALID_VAL` and the read returns no data (an earlier version of this entry said the
  true map still serves it; it does not). Measured with `experiments/tools/leaftl_replica.py --fix last`: it moves
  the lost-data share by less than 0.001.
- **Handling:** documented; part of the lost data reported under KI-28.

### KI-10: LPN 0 used as a sentinel (LeaFTL)

- **Where:** `gtd_to_update[gtd_index] = buffer_lpns[i]` then `if (gtd_to_update[i] != 0)` (`leaftl.c:2114-2121`).
- **Effect:** a flush whose only LPN for translation page 0 is LPN 0 skips that translation-page write.
- **Handling:** documented; affects at most one translation page.

### KI-11: Reads before the first write (LeaFTL)

- **Where:** `if (!ssd->start_write) continue;` in `ssd_read` (`leaftl.c` read loop).
- **Effect:** these reads are counted in `access_cnt`/`read_lpn_cnt` but bypass the CMT and get zero latency.
  Guest boot reads land here.
- **Handling:** bucket `pre_first_write` in the read partition. With the baseline snapshot (KI-1) and preconditioning,
  workloads never run before the first write.

### KI-12: GC counted without a victim (LeaFTL)

- **Where:** `do_gc` increments `gc_cnt` before discovering there is no victim line (`leaftl.c` around 1351, 1362-1364).
- **Handling:** GC runs are counted additively at the point a victim is actually reclaimed (`gc_runs`). This holds for
  LeaFTL only; LearnedFTL's `gc_runs` counts calls of its four GC functions (see `METRICS.md`, device metrics).

### KI-13: 32-bit read counters printed with `%d` (LeaFTL)

- **Where:** `single_read`, `double_read`, `triple_read` are printed with `%d` in the JSON record of `count_segments`.
- **Effect:** wraps above 2³¹ reads (relevant for long real traces).
- **Handling:** the shared JSON emitter (plan Phase 2 item 5) prints all counters as `uint64`.

### KI-14: LearnedFTL write path fills the CMT uncounted

- **Where:** `ssd_write` calls `cmt_hit` (`ld-tpftl.c` around 2700) with the counter increment commented out, and
  inserts on a miss via `process_translation_page_write`.
- **Effect:** writes change CMT contents (and help later reads) without appearing in hit/miss counters. Read hit
  rates are correct, but the CMT reference stream is reads+writes, not reads only.
- **Handling:** counted additively as `cmt_write_hits` / `cmt_write_misses`, never mixed into read hit rate.

### KI-15: `model_out_range` re-labels unmapped CMT hits (LearnedFTL)

- **Where:** on a CMT hit to an unmapped LPN the code undoes the hit (`cmt_hit_cnt--`, `access_cnt--`) and does
  `model_out_range++` (`ld-tpftl.c:2582-2591`), although no model was involved.
- **Handling:** reported as `unmapped` in the read partition. `model_out_range` is not used as a model metric.

### KI-16: No single/double/triple counters (LearnedFTL)

- **Effect:** the paper's main metric (double reads) is not directly reported by the code.
- **Handling:** additive counters at the read sites: CMT hit and model hit = 1 flash read; CMT miss with a mapped
  translation page = 2 flash reads (`ld-tpftl.c` read loop).

### KI-17: Non-resettable counters (both)

- **Also:** LearnedFTL never zeroes `read_joule`/`write_joule`/`erase_joule` at a print (LeaFTL does), so its energy
  is since-boot as well; the instrumentation reports and checks per-interval energy for both.

- `write_cnt`, `gc_cnt`, `wa_cnt` are never zeroed. **Handling:** used only as before/after deltas (invariant 9).

### KI-18: No over-provisioning (both)

- **Where:** geometry is hard-coded (32 GiB raw: `leaftl.c:454-518`, `ld-tpftl.c:747-810`) and ignores `devsz_mb`.
  `femu-scripts/run-blackbox-tcg.sh` exposes `devsz_mb=32768`.
- **Effect:** filling the whole logical space leaves GC no free space (`do_gc` can find no victim).
- **Handling:** expose `devsz_mb=30720` (30 GiB). The unused 2 GiB of raw flash is de-facto OP, close to the
  LearnedFTL paper's 32 GiB + 2 GiB OP.

### KI-19: Wall-clock latency (both)

- **Where:** request `stime` comes from `qemu_clock_get_ns(QEMU_CLOCK_REALTIME)` (`hw/femu/nvme-io.c:66`), and internal
  operations with `stime == 0` use the current wall clock (`leaftl.c:771-772`, `ld-tpftl.c:1141`).
- **Effect:** latency and throughput vary with host load and are meaningless under TCG. FTL state (mapping, CMT,
  GC decisions) does not depend on time, so counters are deterministic.
- **Handling:** latency is reported only from KVM runs with pinned vCPUs on exclusive cores, as mean ± CI over repeats.
  This environment has no KVM access (no root on a shared server), so latency and throughput are pending; counters
  do not depend on time and are unaffected. The TCG-versus-KVM equality of counters is a cross-check to run once KVM exists.

### KI-20: Model predictions verified against an oracle (LearnedFTL)

- **Where:** `model_predict` (`ld-tpftl.c:2459-2519`) computes the predicted PPN, then reads the true mapping
  (`get_maptbl_ent`, `ld-tpftl.c:2500-2501`) and returns `true` only if they match. The code comment says this
  guards against floating-point inaccuracy even when `bitmaps[lpn] == 1`. A mismatch also increments
  `model_out_range` when the predicted page holds no LPN (`ld-tpftl.c:2509-2512`).
- **Effect:** a real SSD would read the predicted page, find the wrong LPN in its OOB area, and then read the
  translation page: a wasted flash read. Here a wrong prediction is detected for free and costs no flash read,
  so LearnedFTL's flash reads are under-counted relative to LeaFTL, which does charge +1 read for a misprediction
  (KI-6, KI-7). The bitmap is meant to make wrong predictions impossible, so the size of this effect is unknown.
- **Handling:** measured. `model_misses` (the authors' `model_miss_num`) counts exactly the `false` returns, which
  only happen when the bitmap said the LPN was predictable. Results report flash reads per mapped read both as-coded
  (`flash_reads_per_mapped_read`) and with each wrong prediction charged one extra read
  (`flash_reads_per_mapped_read_mispred_charged`, the LeaFTL convention).

## Found while running the baselines (Phase 2b and 5)

These were found by the invariants in `INVARIANTS.md` on real runs, not by reading the code first. Each was
then traced to the lines below (line numbers are for the instrumented source at commit `HEAD`; the function names
are stable). None is fixed; each is measured and reported.

### KI-21: GC updates the wrong translation pages (LeaFTL)

- **Where:** `GC_flush` (`leaftl.c:1990`) is called with the LPNs that GC migrates (`buffer_lpns`, sorted into
  `lpns[]`), but chooses the translation pages to rewrite with `ssd->buffer_lpns[i]` (`leaftl.c:2029`): the
  *host write buffer*, not the migrated LPNs. The same line in `buffer_flush` (`leaftl.c:2161`) is correct there.
- **Effect:** every GC pass rewrites the translation pages of whatever happens to be in the host write buffer
  (about 340 per victim block on `uniform-mixed`) and never rewrites the translation pages of the LPNs it moved.
  Measured on `uniform-mixed`, 100k ops after `fill`: 23.1M translation-page writes for 286k host-written LPNs,
  WAF 81.7. Together with KI-23 it leaves the mapping state inconsistent (KI-24).
- **Handling:** measured (`nand_writes_translation`, `waf`). Every LeaFTL result in which GC runs (all `fill` and
  `steady` runs) carries this caveat in its `notes`.

### KI-22: GC data migration is charged as a read of the destination page (LeaFTL)

- **Where:** `GC_flush` issues `NAND_READ` on the newly allocated destination page `wppa[i]`
  (`leaftl.c:2063-2068`) and no `NAND_WRITE`; the source page was already read by `gc_read_page` in
  `clean_one_data_block` (`leaftl.c:1323`).
- **Effect:** GC-migrated user data is never programmed in the cost model: `nand_writes_gc` is always 0 for LeaFTL,
  GC programs are missing from WAF and energy, and GC reads are counted twice (source and destination).
- **Handling:** reported as is. LeaFTL's WAF therefore *understates* GC data copies while overstating translation
  writes (KI-21); the two are reported separately (`nand_writes_translation`, `nand_reads_gc`).

### KI-23: Overwrite invalidates the old page before the map is updated (LeaFTL)

- **Where:** `ssd_write` marks the LPN's current page invalid as soon as the write arrives (`leaftl.c:3045`), but
  the page map is only updated when the 2048-LPN write buffer is flushed (`buffer_flush`). The buffer is searched
  only at slot 0 on reads (KI-5).
- **Effect:** until the flush, the page map points at an invalidated page. A read of that LPN is served from it,
  which on real hardware returns the previous version of the data (a stale read). If GC erases that block first,
  the map points at a free, later reused, page.
- **Handling:** measured by `served_stale_page` (reads served from a non-valid page) and `stale_map_entries`
  (invariant 7 audit). The micro-trace `overwrite-then-read` reproduces it with 16 LPNs. LearnedFTL updates its
  map at write time and must show 0 for both (HARD).

### KI-24: Mapping state inconsistent after GC (LeaFTL)

- **Where:** consequence of KI-21 and KI-23 (and KI-4/KI-6 on the read side); visible only once GC has run.
- **Effect:** after `fill` + 100k `uniform-mixed` ops: 8,671 blocks whose valid-page count differs from the number
  of map entries pointing into them, 14,317 valid pages whose reverse map does not resolve back, 2,534 map entries
  pointing at non-valid pages (more than the 2,048-LPN buffer can explain), and 8.3% of reads of written data not
  returned (`invalid_but_mapped`: the FTL found no mapping although the page map has one).
- **Handling:** for LeaFTL, invariant 7's block, reverse-map and stale-entry parts are KNOWN (reported with counts);
  its line-list part stays HARD. The lost reads are reported as `lost_data_read_share`. For LearnedFTL the whole of
  invariant 7 is HARD.

### KI-25: Translation-page writes not charged (LearnedFTL)

- **Where:** `translation_write_page` (`ld-tpftl.c:1368`) has its `ssd_advance_status(..., NAND_WRITE)` call
  commented out (`ld-tpftl.c:1378-1382`), while the page is still allocated, marked valid and added to the GTD.
- **Effect:** `nand_writes_translation` is always 0 for LearnedFTL: translation-page programs are missing from its
  WAF, energy and timing.
- **Handling:** reported as is; LearnedFTL's WAF understates translation writes. The count of translation pages
  written could be added as a further counter if a corrected WAF is needed.

### KI-26: The authors' `read_cnt` is not the number of flash reads (LeaFTL)

- **Where:** `ssd_read` (`leaftl.c`): `read_cnt++` happens before `process_translation_page_read`, which returns
  without reading when the translation page is unmapped (KI-3); the stale-segment path on a CMT hit reads a
  translation page without `read_cnt++`; and a write-buffer hit counts as a single read with no flash read.
- **Effect:** the authors' `single_read`/`double_read`/`triple_read` neither partition read LPNs nor count flash
  reads exactly.
- **Handling:** headline metrics use flash reads measured per read LPN (`served_reads_hist`, see `METRICS.md`);
  the authors' counters are still reported, and `readcnt_mismatch` counts served LPNs where they disagree.

### KI-27: Host writes silently dropped by the buffer duplicate check (LeaFTL)

- **Where:** `ssd_write` (`leaftl.c:3006`) declares `bool update_in_buffer = false` once per request
  (`leaftl.c:3019`) and sets it when an LPN is found in the write buffer (`leaftl.c:3055-3060`), but never resets it
  for the next LPN. The search scans all 2048 slots of `buffer_lpns`, which is zero-initialised and never cleared
  after a flush (`buffer_pointer` is reset to 0 and old LPNs stay in the slots).
- **Effect:** once one LPN of a request matches any slot (an LPN still live in the buffer, an LPN left over from an
  earlier flush, or LPN 0 against a zero slot), that LPN and every later LPN of the request are neither buffered nor
  flushed. If the LPN was mapped, its old page has already been invalidated (KI-23), so the map points at a dead page;
  if not, the write is lost and later reads are unmapped. The first request of every run touching LPN 0 is dropped
  entirely: the first 256 KiB (64 LPNs) of the `fill` precondition, and the first request of the micro-traces.
- **Handling:** measured: `writes_buffered + writes_absorbed + writes_dropped = write_lpns` (HARD), where absorbed
  means a genuine live duplicate. `writes_dropped` and `dropped_write_share` are reported (KNOWN). The micro-traces
  `write-read-buffered`, `overwrite-then-read` and `size-buckets` predict the dropped counts exactly.

### KI-28: The learned lookup fails for many written LPNs (LeaFTL)

- **Where:** two bugs in the segment store; the read then takes the `vppn == INVALID_VAL` branch of `ssd_read` and
  returns no data.
  1. `segment_insert_simple_segment` (`leaftl.c:1584-1634`): when the group's top level holds exactly one segment,
     `q = sg->segment_head->next` is NULL, the `while (q)` loop (`:1606`) never runs, and the function returns
     without linking the new segment (unless it lies entirely below the head, `:1596`). A level holds exactly one
     segment after a group's first insert and after every overlap (`level_seg_mgmt_insert`, `:1749-1757`), so this
     is the common case: 82% of all segments built in the 80 replayed runs were dropped.
  2. `compact` (`:1859`), called every 1,000,000 written LPNs of an interval (`ssd_write`, `:3053`), deletes lower
     levels that `get_valid_lpns` (`:1650`) reports as fully shadowed. `get_valid_lpns` marks the LPNs of a
     conflict-resolution buffer (CRB) as 0 instead of 1 (`:1672`) and skips the last LPN of an accurate segment
     (`:1658`), so levels that hold the only mapping for some LPNs are freed.

  A dropped segment's CRB has already been pushed onto the group's CRB list by `regression` (`:1494-1509`), and the
  CRB search scans every CRB of the group (`:1556-1571`), so scattered (inaccurate) writes stay partly findable as
  model corrections; contiguous (accurate) segments have no CRB and are lost outright.
- **Evidence:** `experiments/tools/leaftl_replica.py` re-implements the write buffer, `buffer_flush`, `regression`,
  segment insertion, `all_seg_get_ppn`, the CMT and `compact`, driven by each run's FEMU request log. On all 80
  LeaFTL runs without GC it reproduces FEMU's read-partition, model, CMT and write counters exactly (3,040
  counters, 0 mismatches; `experiments/tools/ki28_attribution.py`). Replayed with bug 1 switched off, the
  lost-data share is at most 0.0002 on every 100k-request and real-trace run, and 0.003-0.117 on the 1M-request runs
  that reach `compact`; with bugs 1 and 2 off it is at most 0.0002 everywhere, and 0 with KI-9, KI-27 and KI-30 also
  off. Fixed alone, KI-9, KI-27 and KI-30 never remove the loss: they only change which segments bug 1 drops (KI-30
  alone moves the share by up to +0.21). Per-run values: `experiments/results/ki28_attribution.csv`.
- **Effect:** under `warm`, 8-63% of reads of written data are not returned (`invalid_but_mapped`,
  `model_misses == invalid_read_cnt`).
- **Handling:** measured as `invalid_but_mapped` and `lost_data_read_share` (KNOWN, invariant 10). LeaFTL's
  translation-free ratio and flash reads per read are computed over the reads it did serve, so they must always be
  read together with its lost-data share. Not fixed (authors' code unchanged).

### KI-29: GC thrashes on a full device (LearnedFTL)

- **Where:** `should_do_gc_v3` and the batch GC functions (`ld-tpftl.c:444` onwards). Lines are partitioned among
  many write pointers; once fewer than 4 lines are free (the authors' own "what's wrong?" message, printed before
  every GC), GC runs for almost every write, and victims are nearly fully valid, so each GC copies almost a whole
  line and frees almost nothing.
- **Effect:** measured on `uniform-mixed`, 100k ops after `fill`: 46,578 GC calls (`gc_runs`, which for LearnedFTL
  counts batch calls and per-line calls together), 5.96M block erases and 1.53 billion
  GC page copies for 286k host-written LPNs (WAF 5,338), at ~15 requests/s of wall-clock time (the FTL thread is
  CPU-bound; KVM would not help). The constant model retraining during GC also makes almost every read a correct model
  prediction (translation-free ratio 0.997), which is a side effect of the thrashing, not a property of the cache.
- **Handling:** `fill` is a GC-stress condition only; LearnedFTL's `fill` rows carry this caveat and are not used for
  cache comparisons. The `warm` condition (footprint ≤ 15.3 GiB of 30 GiB) keeps LearnedFTL out of this regime;
  even there, 1M-op write-heavy traces start GC late (`uniform-mixed` 1M: WAF 1.82, 144 GC runs).

### KI-30: `buffer_flush` sorts one slot past the write buffer (LeaFTL)

- **Where:** `quick_sort(ssd->buffer_lpns, 0, sp->buffer_size)` (`leaftl.c:2143`; also `GC_flush`, `:2011`), but
  `quick_sort` (`util.c:53`) takes an inclusive upper index, so it sorts 2049 elements of a 2048-element array.
  `g_malloc0(2048 * 8)` returns a glibc chunk with 16,392 usable bytes, so element 2048 is the next chunk's
  `prev_size` field, which malloc never uses while this chunk is in use: no crash, but a hidden 2049th slot that
  starts at 0 and keeps whatever the sort leaves there.
- **Effect:** each flush keeps the 2048 smallest of 2049 values (the 2048 new LPNs and the hidden slot). The largest goes
  to the hidden slot and is neither mapped nor segmented in that flush; the previous flush's leftover (LPN 0 on the
  first flush) is mapped again instead. `leaftl_replica.py` models the slot this way and reproduces FEMU's counters
  exactly, which confirms it.
- **Handling:** documented; its effect on lost reads is measured under KI-28 (`--fix sort`).
