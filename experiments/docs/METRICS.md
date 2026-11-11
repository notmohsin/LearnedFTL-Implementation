# Metric definitions for the LeaFTL / LearnedFTL baselines

This document defines every metric we report, what the authors' code actually counts, and how the two
FTLs differ. All rates are **fractions in [0, 1]** in machine-readable output. Known code behaviours that
affect counters are in [KNOWN_ISSUES.md](KNOWN_ISSUES.md) (referenced as KI-n).

## 1. Units of work

- **Request**: one NVMe read or write as received by the FTL (`NvmeRequest`, `slba`/`nlb` in 512 B sectors).
- **LPN**: one 4 KiB logical page. A request covers LPNs `slba/8 .. (slba+nlb-1)/8`. All read metrics are
  counted **per read LPN**, which is how both FTLs loop (`ssd_read` in `leaftl.c` and `ld-tpftl.c`).
- **Translation page (TP)**: a flash page holding a contiguous run of LPN→PPN mappings.
  `tvpn = lpn / ents_per_pg`.

## 2. How the two FTLs differ

| | LeaFTL (`hw/femu/bbssd/leaftl.c`) | LearnedFTL (`hw/femu/bbssd/ld-tpftl.c`) |
|---|---|---|
| Entries per translation page (`ents_per_pg`) | **256** (`leaftl.c:502`) | **512** (4096 / 8 B, `ld-tpftl.c:799-801`) |
| CMT capacity (`tt_cmt_size`) | 8192 (`leaftl.c:505`) | 8192 (`ld-tpftl.c:809`) |
| One CMT entry holds | one learned **segment** (slope, intercept, start LPN, length, accuracy flag) for one translation page | one **LPN→PPN** mapping; entries grouped in TP nodes |
| CMT key | `tvpn` (hash `tvpn % 24593`) | `lpn` |
| Mappings covered by a full CMT | up to 8192 TPs × 256 = 2,097,152 LPNs (8 GiB) **if** segments cover their whole TP | 8192 LPNs (32 MiB) |
| Replacement | LRU (one list) | two-level LRU: TP nodes, then clean-before-dirty entries |
| Inserted on | read miss of a mapped TP whose model lookup is *accurate* (KI-8) | read miss (prefetching the rest of the request within the TP) **and** write (KI-14) |
| Learned component | piece-wise linear segments + conflict-resolution buffer, consulted on a CMT miss after reading the TP | in-place-update linear model per GTD entry + bitmap of accurately-predicted LPNs, consulted on a CMT miss **before** reading the TP |
| Write buffer | 2048 LPNs, but reads only check slot 0 (KI-5) | none on the read path |
| Wrong prediction cost | prediction checked against the true page map; +1 flash read if wrong (KI-6, KI-7) | prediction checked against the true page map at **no** flash cost, then falls back to a TP read (KI-20) |
| Raw geometry | 8 ch × 8 LUN × 256 blk × 512 pg × 4 KiB = 32 GiB | same |

Consequence: **raw CMT hit rates are not comparable between the two FTLs.** A LeaFTL hit is a cached
*segment* covering up to 256 LPNs; a LearnedFTL hit is one cached LPN. Cross-FTL comparison uses the
headline metrics in section 4.

## 3. Counters in the authors' code (and our additions)

### LeaFTL read loop (`ssd_read`, `leaftl.c`)

For each read LPN, in order:

1. `access_cnt++`, `read_lpn_cnt++` (our addition).
2. Before the first write: `continue` with no further counting (KI-11) → bucket **pre_first_write**.
3. Write-buffer slot 0 matches → `single_read++` → bucket **buffer_hit** (KI-5).
4. CMT lookup by `tvpn`:
   - hit → `cmt_hit_cnt++`, predict PPN from the cached segment (KI-7) → bucket **cmt_hit**;
   - miss → `cmt_miss_cnt++`, `read_cnt=1`, then `process_translation_page_read`:
     - TP unmapped → return without reading or predicting (KI-3) → bucket **cmt_miss_unmapped_tp**;
     - TP mapped → one translation-page flash read, then a model query:
       `model_queries++` and exactly one of `model_hit`, `model_miss` (`all_seg_get_ppn`, `leaftl.c:1783-1808`).
       `model_correction` is always 0: a match in a conflict-resolution buffer is discarded (KI-31) and counted
       as `crb_match_discarded`
       → bucket **cmt_miss_model**.
5. `vppn == INVALID_VAL` → `access_cnt--`, `invalid_read_cnt++` (our addition) → LPN treated as invalid.
6. Otherwise the true map (`get_maptbl_ent`) decides: if the LPN is mapped, the data page is read (+1 flash read),
   and if the predicted PPN ≠ true PPN another flash read is charged first (+1). `read_cnt` ∈ {1, 2, 3} increments
   `single_read`, `double_read` or `triple_read`.

### LearnedFTL read loop (`ssd_read`, `ld-tpftl.c:2574-2660`)

For each read LPN:

1. `access_cnt++`.
2. CMT lookup by `lpn`:
   - hit and LPN mapped → `cmt_hit_cnt++` → 1 flash read (data);
   - hit but LPN unmapped → hit undone, `model_out_range++` (KI-15) → bucket **unmapped**.
3. Miss: if `bitmaps[lpn]` and the GTD entry's model is usable → `model_predict`:
   - correct → `model_hit_num++` → 1 flash read (data), no TP read;
   - not → `model_miss_num++`, continue as a CMT miss. (Our change to an authors' counter: their code increments
     `model_hit_num` whenever `bitmaps[lpn]` is set, before the prediction is tried, and has no `model_miss_num`.
     Commit `2298bfac2` moved the increment so it counts correct predictions only. No decision reads this counter.) Correctness is decided by comparing the prediction
     with the true page map, which costs no flash read in this code (KI-20).
4. CMT miss → `cmt_miss_cnt++`, read the TP (prefetch to end of request within the TP), then the data
   → 2 flash reads. If the LPN is unmapped the miss is undone (`cmt_miss_cnt--`, `access_cnt--`) → bucket **unmapped**.
5. No single/double/triple counters exist in the authors' code (KI-16); they are added as measurement-only counters.

## 4. Headline metrics (used for cross-FTL comparison)

### How read cost is measured

The authors' `read_cnt` (and so their `single_read`/`double_read`/`triple_read`) is not the number of flash reads:
LeaFTL counts a translation-page read that never happens when the page is unmapped, misses the translation-page read
on its stale-segment path, and counts write-buffer hits as single reads (KI-26). Our instrumentation therefore measures
read cost directly: in `ssd_read`, each read LPN snapshots the NAND read counters (`nand_reads_user`,
`nand_reads_translation`) when its loop iteration starts, and the difference when it ends is the number of flash
reads issued while serving it, including translation reads caused by dirty CMT evictions (`ftl_stats.h`,
`struct ftl_read_acct`). Invariant 4 checks that every read issued inside `ssd_read` is attributed this way, and
invariant 12 that the NAND counters cover every operation the authors charge energy for.

Each read LPN ends in one bucket:

- **served**: the true page map (`maptbl`) has a mapping and the data page was read from flash
  (`served_lpns`, with `served_reads_hist[k]` = served LPNs that needed k flash reads, k = 1, 2, 3, 4+);
- **buffer hit** (LeaFTL only): returned from the write buffer with no flash read;
- **unmapped**: everything else (never written, read before the first write, or written but not found by the FTL).

**Mapped read LPNs** = buffer hits + served. All headline ratios use this denominator.

| Metric | Definition |
|---|---|
| **Translation-free read ratio** | Mapped read LPNs served without any translation-page read (buffer hit, or served with no TP read: CMT hit or LearnedFTL model hit) / mapped. |
| **Single / double / triple read ratio** | Mapped read LPNs served with 1 (or 0, buffer hit) / 2 / 3 flash reads / mapped. |
| **Flash reads per mapped read** | All flash reads issued for served LPNs / mapped. |
| **... misprediction-charged** | As above, plus one read per LearnedFTL wrong model prediction, which the authors' code detects for free (KI-20). |
| **CMT hit rate (mapped)** | Served LPNs whose CMT lookup hit / served. Same definition for both FTLs, but a LeaFTL entry covers a translation page and a LearnedFTL entry one LPN. |
| **Unmapped-read share** | Unmapped read LPNs / all read LPNs. Under `fill`/`steady` this is 0 for LearnedFTL and equals LeaFTL's lost-data share. |
| **Stale-read share** | Mapped reads served from an already-invalidated page (LeaFTL write-buffer window, KI-23) / mapped. |
| **Lost-data read share** | (Unmapped − never-written) read LPNs / read LPNs of written data: data the FTL failed to return (KI-27, KI-28, KI-31; after GC also KI-24). |
| **Gap to Belady** | Belady-OPT hit rate at 8192 entries on the trace's read stream (FTL-native keying) − CMT hit rate (mapped). The Belady rate covers all read LPNs and the mapped hit rate only the served ones; lost reads are always CMT misses, so this understates the gap. In the measured runs it is about 0 or negative for LeaFTL: do not use it alone. |
| **Gap to Belady, all lookups** | The same Belady rate − CMT hit rate (native), so both cover (nearly) all read LPNs. It includes the effect of LeaFTL's lost reads and of caching only accurate segments. In the measured `warm` runs it is within 0.06 of the lost-data share, so it measures failed lookups, not replacement-policy headroom. |

Per-FTL metrics, reported but **not compared across FTLs**:

| Metric | Definition |
|---|---|
| CMT hit rate (native) | The FTL's own `cmt_hits / (cmt_hits + cmt_misses)`, including lookups of unmapped LPNs. |
| Model hit rate | LeaFTL: `model_hits / model_queries` (after a TP read). LearnedFTL: correct predictions / predictions tried (before a TP read). |
| Authors' single/double/triple | Reported for comparison with the papers; `readcnt_mismatch` counts served LPNs where they disagree with the measured reads. |

Device metrics:

| Metric | Definition |
|---|---|
| NAND reads | `nand_reads_user` (incl. mispredicted reads), `nand_reads_translation` (read, write and GC paths), `nand_reads_gc` (migrated data). |
| NAND programs | `nand_writes_user + nand_writes_translation + nand_writes_gc`; equals the authors' `wa_cnt`/`write_num` (invariant 5). LeaFTL never programs GC-migrated data (KI-22) and LearnedFTL never charges translation writes (KI-25). |
| WAF | NAND programs / host-written LPNs in the workload interval. |
| GC runs, erases | `gc_runs` (delta), `nand_erases`. `gc_runs` is not comparable across FTLs: LeaFTL counts one per reclaimed victim line; LearnedFTL counts one per call of each of its four GC functions (`gtd_do_gc`, `batch_gtd_do_gc`, `batch_line_do_gc`, `line_do_gc`), so a batch that reclaims several lines counts once, or once plus once per line. Compare GC work with `nand_erases`. |
| Energy | 3.5 / 16.7 / 132 (authors' units) per read / program / erase. |
| Latency, throughput | KVM on exclusive cores only (KI-19); blank for every TCG run. |

## 5. Our configuration vs the papers

| Parameter | LearnedFTL paper (HPCA'24, §IV) | LeaFTL paper (ASPLOS'23) | This repo (baselines) |
|---|---|---|---|
| Platform | FEMU blackbox, KVM | WiscSim simulator + real open-channel SSD | FEMU blackbox under TCG (counters); KVM for latency once available |
| Capacity | 32 GB logical + 2 GB OP | 1 TB (simulator), 2 TB (real SSD) | 32 GiB raw, 30 GiB logical (KI-18) |
| Geometry | 8 ch × 8 ways, 256 blk/chip, 512 pg/blk, 4 KB pages | 16 KB pages | as LearnedFTL paper |
| NAND latency | 40 µs read, 200 µs write, 2 ms erase | — | same as LearnedFTL paper (`leaftl.h:17-19`) |
| Entries per TP | 512 | — | LeaFTL 256, LearnedFTL 512 (native; constants untouched) |
| CMT size | ~3% of mappings for DFTL/TPFTL; LearnedFTL 1.5% + models at equal memory | — | 8192 entries for both: LearnedFTL ≈ 0.1% of 8.4M mappings; LeaFTL 8192 segments |
| LeaFTL implementation | re-implemented in FEMU; write strategy changed to match TPFTL; VPPN added | original (WiscSim) | the LearnedFTL authors' FEMU port |

Implication: the CMT here is much smaller than in the LearnedFTL paper, so absolute hit rates will be lower
than the published ones. We compare **trends** (e.g. LearnedFTL needing fewer double reads than LeaFTL on random
reads), not absolute values, and say so in every results table.

## 6. Bounds

`experiments/traces/TRACES.md` gives, per trace, hit rates on the read reference stream for four cache policies,
keyed per TP (256, 512) and per LPN, at the CMT size and across a capacity sweep (1,024 to 65,536 entries).
They are computed by `experiments/tools/cachesim.c`, which is checked against brute-force references by
`experiments/tools/test_cachesim.py`.

| Policy | Definition |
|---|---|
| LRU | Evict the least recently used key. Computed exactly for all capacities from LRU stack distances. |
| LFU (in-cache) | A key's count starts at 1 on insertion, rises by 1 on each hit and is **discarded on eviction**. The victim is the cached key with the lowest count; ties go to the least recently used. This is the version a controller could implement. |
| Perfect LFU | Same victim rule, but counts **persist across evictions** and every reference counts, including the one that inserts the key. An idealised reference. |
| Belady-OPT | Evict the key whose next use is furthest in the future. Needs the whole trace, so it is a bound, not an implementable policy. |

All four assume every miss inserts the key (demand fill). Neither FTL does exactly that (section 2), so these
are reference points rather than strict upper bounds on the native CMT hit rate. The gap between a real
FTL's measured hit rate and Belady-OPT at the same capacity and keying is the headroom a smarter policy could target,
but only if the FTL's misses are cache misses. For LeaFTL as ported they are mostly failed lookups (KI-28, KI-31).
