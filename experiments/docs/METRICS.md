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
       `model_queries++` and exactly one of `model_hit`, `model_correction`, `model_miss` (`leaftl.c:1744-1762`)
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
   - not → `model_miss_num++`, continue as a CMT miss. Correctness is decided by comparing the prediction
     with the true page map, which costs no flash read in this code (KI-20).
4. CMT miss → `cmt_miss_cnt++`, read the TP (prefetch to end of request within the TP), then the data
   → 2 flash reads. If the LPN is unmapped the miss is undone (`cmt_miss_cnt--`, `access_cnt--`) → bucket **unmapped**.
5. No single/double/triple counters exist in the authors' code (KI-16); they are added as measurement-only counters.

## 4. Headline metrics (used for cross-FTL comparison)

All are computed over **mapped read LPNs**: read LPNs that reach the FTL after the first write and map to written
data. Unmapped, pre-first-write and invalid reads are reported separately and excluded.

| Metric | Definition |
|---|---|
| **Translation-free read ratio** | Share of mapped read LPNs served with no translation-page flash read (write-buffer hit, CMT hit, or LearnedFTL model hit). |
| **Double-read ratio** | Share of mapped read LPNs that needed exactly 2 flash reads. |
| **Triple-read ratio** | Share needing 3 flash reads (LeaFTL only: TP read + wrong prediction + data). |
| **Flash reads per read LPN** | `(single + 2·double + 3·triple) / mapped read LPNs`. |
| **Misprediction rate** | Share of predictions (LeaFTL: CMT segments and models; LearnedFTL: models) that differ from the true page map. |
| **Flash reads per read LPN, misprediction-charged** | As above, but each LearnedFTL wrong prediction is charged one extra flash read, matching how LeaFTL is charged (KI-20). Reported alongside the as-coded value. |
| **Unmapped-read share** | Unmapped read LPNs / all read LPNs. Should be 0 under precondition `fill`. |

Per-FTL metrics, reported but **not compared across FTLs**:

| Metric | Definition |
|---|---|
| CMT hit rate (native) | `cmt_hits / (cmt_hits + cmt_misses)` over that FTL's own counters, with unmapped reads excluded for both. |
| Model hit rate | LeaFTL: `model_hits / model_queries` (queries happen after a TP read). LearnedFTL: `model_hits / (model_hits + model_misses)` (tried before a TP read). |

Device metrics:

| Metric | Definition |
|---|---|
| NAND reads | Split into user data, translation-page, GC. |
| NAND programs | User data + translation-page writes + GC-migrated pages. |
| WAF | NAND programs / user-written LPNs (derived, not the authors' `wa_cnt`). |
| GC runs | GC invocations that reclaimed a victim line (KI-12). |
| Latency, throughput | Per request, KVM runs on exclusive cores only (KI-19); pending until KVM access exists. P50 / P99 / P99.9. |

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
FTL's measured hit rate and Belady-OPT at the same capacity and keying is the headroom a smarter policy could target.
