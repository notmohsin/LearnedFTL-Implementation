/*
 * ftl_stats.h: shared, additive-only measurement helpers for leaftl.c and
 * ld-tpftl.c.
 *
 * Purpose: both FTLs' count_segments() print a JSON record for the
 * experiment tooling to parse. Before this header the two files picked
 * their own key names and types for the same underlying quantities (e.g.
 * leaftl printed "access_cnt"/"writes" with %d, ld-tpftl printed
 * "internal_mapping_accesses"/"nand_page_writes"). This header gives both
 * a single emitter for every key they have in common, so the key names,
 * JSON field order and C types can never drift apart again. FTL-specific
 * keys (buckets that only exist in one FTL's control flow) are appended by
 * each file's own count_segments() after the common prefix this header
 * prints, never interleaved with it.
 *
 * This header does not change any FTL decision logic. It only formats
 * counters that are incremented in leaftl.c / ld-tpftl.c themselves.
 */
#ifndef __FTL_STATS_H
#define __FTL_STATS_H

#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <inttypes.h>

/*
 * Request-size histogram, in LPNs touched by one NVMe request. Bucket i
 * covers lengths (2^i - 1, 2^(i+1) - 1], i.e. buckets are
 * [1],[2-3],[4-7],[8-15],[16-31],[32-63],[64+]. The last bucket is a
 * catch-all for anything bigger.
 */
#define FTL_STATS_SIZE_BUCKETS 7

static inline int ftl_stats_size_bucket(uint64_t nlpn)
{
    int b = 0;
    uint64_t hi = 1; /* upper bound (inclusive) of bucket b */
    while (nlpn > hi && b < FTL_STATS_SIZE_BUCKETS - 1) {
        hi = hi * 2 + 1;
        b++;
    }
    return b;
}

/* Every field here is a quantity both FTLs can compute from their own
 * control flow. See leaftl.c/ld-tpftl.c count_segments() for how each is
 * populated; see experiments/docs/METRICS.md for the exact semantics. */
struct ftl_stats_common {
    const char *ftl;
    int ents_per_tp;
    int cmt_entries;

    /* geometry, same struct ssdparams field names in both FTLs */
    int nchs;
    int luns_per_ch;
    int blks_per_pl;
    int pgs_per_blk;
    int tt_pgs;

    uint64_t read_reqs;
    uint64_t write_reqs;
    uint64_t read_lpns;   /* every read LPN the FTL loop saw, no exclusions */
    uint64_t write_lpns;  /* every write LPN the FTL loop saw */
    uint64_t read_size_hist[FTL_STATS_SIZE_BUCKETS];
    uint64_t write_size_hist[FTL_STATS_SIZE_BUCKETS];

    uint64_t buffer_flushes; /* 0 and structurally meaningless if the FTL has no write buffer */
    uint64_t gc_runs;        /* counted only when a victim was actually reclaimed, unlike gc_cnt (KI-12) */

    uint64_t mapped_read_lpns;   /* read LPNs that resolved to real data (old leaftl access_cnt) */
    uint64_t unmapped_read_lpns; /* read LPNs that did not (old leaftl invalid_read_cnt) */

    uint64_t cmt_hits;
    uint64_t cmt_misses;

    uint64_t model_queries;
    uint64_t model_hits;
    uint64_t model_misses;

    uint64_t single_reads;
    uint64_t double_reads;
    uint64_t triple_reads; /* always 0 for LearnedFTL: it never charges a 3rd flash read (KI-20) */

    uint64_t nand_page_writes; /* actual NAND program ops: leaftl wa_cnt, ld-tpftl write_num */
    int gc_cnt;                 /* authors' own counter, kept verbatim (KI-12: counts even with no victim) */

    uint64_t invariant_failures;
};

/*
 * Prints the shared prefix of the JSON record, as a syntactically complete
 * object missing its closing brace: "{...,\"invariant_failures\":N". The
 * caller appends its own FTL-specific ",\"key\":value" pairs and then
 * "}\n". This keeps the common keys in one guaranteed order across both
 * FTLs while still letting each file add buckets the other doesn't have.
 */
static inline void ftl_stats_print_common_prefix(const struct ftl_stats_common *c)
{
    uint64_t cmt_accesses = c->cmt_hits + c->cmt_misses;
    uint64_t translation_reads = c->single_reads + c->double_reads + c->triple_reads;

    printf("{\"ftl\":\"%s\",\"ents_per_tp\":%d,\"cmt_entries\":%d,"
           "\"nchs\":%d,\"luns_per_ch\":%d,\"blks_per_pl\":%d,"
           "\"pgs_per_blk\":%d,\"tt_pgs\":%d,"
           "\"read_reqs\":%" PRIu64 ",\"write_reqs\":%" PRIu64 ","
           "\"read_lpns\":%" PRIu64 ",\"write_lpns\":%" PRIu64 ","
           "\"read_size_hist\":[%" PRIu64 ",%" PRIu64 ",%" PRIu64 ",%" PRIu64
           ",%" PRIu64 ",%" PRIu64 ",%" PRIu64 "],"
           "\"write_size_hist\":[%" PRIu64 ",%" PRIu64 ",%" PRIu64 ",%" PRIu64
           ",%" PRIu64 ",%" PRIu64 ",%" PRIu64 "],"
           "\"buffer_flushes\":%" PRIu64 ",\"gc_runs\":%" PRIu64 ","
           "\"mapped_read_lpns\":%" PRIu64 ",\"unmapped_read_lpns\":%" PRIu64 ","
           "\"cmt_hits\":%" PRIu64 ",\"cmt_misses\":%" PRIu64 ",\"cmt_accesses\":%" PRIu64 ","
           "\"cmt_hit_rate\":%.9f,"
           "\"model_queries\":%" PRIu64 ",\"model_hits\":%" PRIu64 ",\"model_misses\":%" PRIu64 ","
           "\"model_hit_rate\":%.9f,"
           "\"single_reads\":%" PRIu64 ",\"double_reads\":%" PRIu64 ",\"triple_reads\":%" PRIu64 ","
           "\"translation_reads\":%" PRIu64 ","
           "\"nand_page_writes\":%" PRIu64 ",\"gc_cnt\":%d,"
           "\"invariant_failures\":%" PRIu64,
           c->ftl, c->ents_per_tp, c->cmt_entries,
           c->nchs, c->luns_per_ch, c->blks_per_pl, c->pgs_per_blk, c->tt_pgs,
           c->read_reqs, c->write_reqs, c->read_lpns, c->write_lpns,
           c->read_size_hist[0], c->read_size_hist[1], c->read_size_hist[2],
           c->read_size_hist[3], c->read_size_hist[4], c->read_size_hist[5],
           c->read_size_hist[6],
           c->write_size_hist[0], c->write_size_hist[1], c->write_size_hist[2],
           c->write_size_hist[3], c->write_size_hist[4], c->write_size_hist[5],
           c->write_size_hist[6],
           c->buffer_flushes, c->gc_runs,
           c->mapped_read_lpns, c->unmapped_read_lpns,
           c->cmt_hits, c->cmt_misses, cmt_accesses,
           cmt_accesses ? (double)c->cmt_hits / (double)cmt_accesses : 0.0,
           c->model_queries, c->model_hits, c->model_misses,
           c->model_queries ? (double)c->model_hits / (double)c->model_queries : 0.0,
           c->single_reads, c->double_reads, c->triple_reads, translation_reads,
           c->nand_page_writes, c->gc_cnt,
           c->invariant_failures);
    /* caller appends its own keys, then "}\n" */
}

/*
 * Optional per-request log, enabled by FEMU_REQLOG=/path/to/file. Read via
 * getenv() exactly once (at ftl_thread startup, see ssd_read/ssd_write
 * call sites), never on the per-request hot path: when the env var is
 * unset fp is NULL and ftl_reqlog_write/flush become a single null check.
 * One CSV line per request: "seq,op,slba,nlb".
 */
struct ftl_reqlog {
    FILE *fp;
    uint64_t seq;
};

static inline void ftl_reqlog_init(struct ftl_reqlog *rl)
{
    const char *path = getenv("FEMU_REQLOG");
    rl->seq = 0;
    rl->fp = (path && *path) ? fopen(path, "w") : NULL;
}

static inline void ftl_reqlog_write(struct ftl_reqlog *rl, char op,
                                     uint64_t slba, uint32_t nlb)
{
    if (!rl->fp) {
        return;
    }
    fprintf(rl->fp, "%" PRIu64 ",%c,%" PRIu64 ",%u\n", rl->seq++, op, slba, nlb);
}

static inline void ftl_reqlog_flush(struct ftl_reqlog *rl)
{
    if (rl->fp) {
        fflush(rl->fp);
    }
}

#endif /* __FTL_STATS_H */
