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
  seen in every sequential run in the old `runs.csv`.
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
- **Effect:** one LPN per 2048-LPN flush has no learned segment and cannot be predicted (the true map still
  serves it).
- **Handling:** documented.

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
- **Handling:** GC runs are counted additively at the point a victim is actually reclaimed (invariant 5).

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
- **Handling:** measured. An additive counter `model_wrong_predictions` counts the `false` returns where the bitmap
  said the LPN was predictable, and results report "flash reads per read LPN" both as-coded and with each wrong
  prediction charged one extra read (the LeaFTL convention).
