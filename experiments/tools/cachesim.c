/*
 * cachesim: exact cache-bound computations for a reference stream.
 *
 * Input:  a raw file of little-endian uint32 cache keys (one per reference,
 *         in trace order), written by profile_traces.py.
 * Output:
 *   - <sd_out>: uint32 LRU stack distance per reference (number of distinct
 *     keys referenced since the previous reference to the same key;
 *     UINT32_MAX for a first reference). An LRU cache of capacity C hits a
 *     reference iff its stack distance < C.
 *   - stdout, for every requested capacity, one line each of:
 *       "belady <capacity> <hits>"  Belady's MIN (evict the key whose next
 *                                   use is furthest away)
 *       "lfu <capacity> <hits>"     in-cache LFU: a key's count starts at 1
 *                                   on insertion, is incremented on each hit
 *                                   and is discarded on eviction
 *       "plfu <capacity> <hits>"    perfect LFU: counts persist across
 *                                   evictions (every reference counts)
 *     LFU victims are the cached key with the smallest count; ties go to the
 *     least recently used key, so results are deterministic.
 *
 * All policies assume every miss inserts the key ("demand fill"). Real FTLs
 * may insert conditionally; see experiments/docs/METRICS.md.
 *
 * Build: cc -O2 -o cachesim cachesim.c
 * Usage: cachesim <keys.u32> <sd_out.u32> <capacity>...
 */
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define NONE UINT32_MAX

static void die(const char *msg)
{
    fprintf(stderr, "cachesim: %s: %s\n", msg, errno ? strerror(errno) : "error");
    exit(1);
}

static void *xcalloc(size_t n, size_t sz)
{
    void *p = calloc(n ? n : 1, sz);
    if (!p) {
        die("out of memory");
    }
    return p;
}

/* ---- Fenwick tree over reference positions (1-based internally) ---- */
static void fw_add(int32_t *tree, uint64_t n, uint64_t pos, int32_t delta)
{
    for (uint64_t i = pos + 1; i <= n; i += i & (~i + 1)) {
        tree[i] += delta;
    }
}

static int64_t fw_sum(const int32_t *tree, uint64_t pos) /* sum of [0, pos] */
{
    int64_t s = 0;
    for (uint64_t i = pos + 1; i > 0; i -= i & (~i + 1)) {
        s += tree[i];
    }
    return s;
}

/* ---- max-heap of (next_use, key) for Belady ---- */
typedef struct {
    uint32_t next;
    uint32_t key;
} HeapEnt;

static void heap_push(HeapEnt *h, uint64_t *len, HeapEnt e)
{
    uint64_t i = (*len)++;
    while (i > 0) {
        uint64_t p = (i - 1) / 2;
        if (h[p].next >= e.next) {
            break;
        }
        h[i] = h[p];
        i = p;
    }
    h[i] = e;
}

static HeapEnt heap_pop(HeapEnt *h, uint64_t *len)
{
    HeapEnt top = h[0];
    HeapEnt last = h[--(*len)];
    uint64_t i = 0;
    for (;;) {
        uint64_t l = 2 * i + 1, r = l + 1, m = i;
        uint32_t mv = last.next;
        if (l < *len && h[l].next > mv) {
            m = l;
            mv = h[l].next;
        }
        if (r < *len && h[r].next > mv) {
            m = r;
        }
        if (m == i) {
            break;
        }
        h[i] = h[m];
        i = m;
    }
    if (*len > 0) {
        h[i] = last;
    }
    return top;
}

/* ---- min-heap of (count, last use) for LFU, with lazy deletion ---- */
typedef struct {
    uint64_t prio; /* (count << 32) | position of the key's latest use */
    uint32_t key;
} LfuEnt;

static void lfu_push(LfuEnt *h, uint64_t *len, LfuEnt e)
{
    uint64_t i = (*len)++;
    while (i > 0) {
        uint64_t p = (i - 1) / 2;
        if (h[p].prio <= e.prio) {
            break;
        }
        h[i] = h[p];
        i = p;
    }
    h[i] = e;
}

static LfuEnt lfu_pop(LfuEnt *h, uint64_t *len)
{
    LfuEnt top = h[0];
    LfuEnt last = h[--(*len)];
    uint64_t i = 0;
    for (;;) {
        uint64_t l = 2 * i + 1, r = l + 1, m = i;
        uint64_t mv = last.prio;
        if (l < *len && h[l].prio < mv) {
            m = l;
            mv = h[l].prio;
        }
        if (r < *len && h[r].prio < mv) {
            m = r;
        }
        if (m == i) {
            break;
        }
        h[i] = h[m];
        i = m;
    }
    if (*len > 0) {
        h[i] = last;
    }
    return top;
}

/*
 * LFU with LRU tie-break. perfect=0: counts live only while cached.
 * perfect=1: counts accumulate over the whole trace.
 * A heap entry is live iff the key's latest use still equals the position
 * stored in the entry (positions are unique, so this is unambiguous).
 */
static uint64_t lfu_run(const uint32_t *keys, uint64_t n, uint64_t nkeys,
                        uint64_t cap, int perfect)
{
    const uint32_t ABSENT = NONE; /* positions are < n < NONE */
    uint32_t *freq = xcalloc(nkeys, sizeof(uint32_t));
    uint32_t *lastuse = xcalloc(nkeys, sizeof(uint32_t));
    for (uint64_t k = 0; k < nkeys; k++) {
        lastuse[k] = ABSENT;
    }
    uint64_t limit = 4 * cap + 1024; /* compact the heap beyond this size */
    uint64_t room = (limit + 2 < n + 2) ? limit + 2 : n + 2;
    LfuEnt *heap = xcalloc(room, sizeof(LfuEnt));
    uint64_t heap_len = 0, size = 0, hits = 0;

    for (uint64_t i = 0; i < n; i++) {
        uint32_t k = keys[i];
        if (perfect) {
            freq[k]++;
        }
        if (lastuse[k] != ABSENT) {
            hits++;
            if (!perfect) {
                freq[k]++;
            }
        } else {
            if (size >= cap) {
                for (;;) {
                    LfuEnt v = lfu_pop(heap, &heap_len);
                    if (lastuse[v.key] == (uint32_t)(v.prio & 0xffffffffu)) {
                        lastuse[v.key] = ABSENT;
                        if (!perfect) {
                            freq[v.key] = 0;
                        }
                        size--;
                        break;
                    }
                }
            }
            size++;
            if (!perfect) {
                freq[k] = 1;
            }
        }
        lastuse[k] = (uint32_t)i;
        lfu_push(heap, &heap_len, (LfuEnt){ ((uint64_t)freq[k] << 32) | i, k });
        if (heap_len > limit) {
            uint64_t m = 0;
            for (uint64_t t = 0; t < heap_len; t++) {
                if (lastuse[heap[t].key] == (uint32_t)(heap[t].prio & 0xffffffffu)) {
                    heap[m++] = heap[t];
                }
            }
            heap_len = 0;
            for (uint64_t t = 0; t < m; t++) {
                lfu_push(heap, &heap_len, heap[t]);
            }
        }
    }
    free(heap);
    free(lastuse);
    free(freq);
    return hits;
}

int main(int argc, char **argv)
{
    if (argc < 3) {
        fprintf(stderr, "usage: %s <keys.u32> <sd_out.u32> <capacity>...\n", argv[0]);
        return 2;
    }

    FILE *f = fopen(argv[1], "rb");
    if (!f) {
        die("open keys");
    }
    if (fseek(f, 0, SEEK_END) != 0) {
        die("seek keys");
    }
    long bytes = ftell(f);
    rewind(f);
    if (bytes < 0 || bytes % 4 != 0) {
        die("keys file size is not a multiple of 4");
    }
    uint64_t n = (uint64_t)bytes / 4;
    if (n >= NONE) {
        die("too many references");
    }
    uint32_t *keys = xcalloc(n, sizeof(uint32_t));
    if (fread(keys, sizeof(uint32_t), n, f) != n) {
        die("read keys");
    }
    fclose(f);

    uint32_t maxkey = 0;
    for (uint64_t i = 0; i < n; i++) {
        if (keys[i] > maxkey) {
            maxkey = keys[i];
        }
    }
    uint64_t nkeys = (uint64_t)maxkey + 1;

    /* LRU stack distances. */
    uint32_t *last = xcalloc(nkeys, sizeof(uint32_t));
    for (uint64_t k = 0; k < nkeys; k++) {
        last[k] = NONE;
    }
    int32_t *tree = xcalloc(n + 1, sizeof(int32_t));
    uint32_t *sd = xcalloc(n, sizeof(uint32_t));
    for (uint64_t i = 0; i < n; i++) {
        uint32_t k = keys[i];
        if (last[k] == NONE) {
            sd[i] = NONE;
        } else {
            uint64_t p = last[k];
            /* distinct keys whose latest reference lies in (p, i) */
            sd[i] = (uint32_t)(fw_sum(tree, i == 0 ? 0 : i - 1) - fw_sum(tree, p));
            fw_add(tree, n, p, -1);
        }
        fw_add(tree, n, i, +1);
        last[k] = (uint32_t)i;
    }
    free(tree);

    FILE *o = fopen(argv[2], "wb");
    if (!o) {
        die("open sd_out");
    }
    if (fwrite(sd, sizeof(uint32_t), n, o) != n) {
        die("write sd_out");
    }
    fclose(o);
    free(sd);

    /* Next-use positions (NONE = never used again). */
    uint32_t *next_use = xcalloc(n, sizeof(uint32_t));
    for (uint64_t k = 0; k < nkeys; k++) {
        last[k] = NONE;
    }
    for (uint64_t i = n; i-- > 0;) {
        next_use[i] = last[keys[i]];
        last[keys[i]] = (uint32_t)i;
    }

    /* Belady MIN per requested capacity. cached[k] holds the next-use value
     * of the live heap entry for k, or NONE-1 sentinel when k is absent. */
    const uint32_t ABSENT = NONE - 1; /* next_use never equals n-1 < NONE-1 */
    uint32_t *cached = last; /* reuse buffer */
    HeapEnt *heap = xcalloc(n + 1, sizeof(HeapEnt));
    for (int a = 3; a < argc; a++) {
        char *end = NULL;
        unsigned long long cap = strtoull(argv[a], &end, 10);
        if (!end || *end || cap == 0) {
            die("bad capacity");
        }
        for (uint64_t k = 0; k < nkeys; k++) {
            cached[k] = ABSENT;
        }
        uint64_t heap_len = 0, size = 0, hits = 0;
        for (uint64_t i = 0; i < n; i++) {
            uint32_t k = keys[i];
            if (cached[k] != ABSENT) {
                hits++;
            } else {
                if (size >= cap) {
                    for (;;) {
                        HeapEnt v = heap_pop(heap, &heap_len);
                        if (cached[v.key] == v.next) {
                            cached[v.key] = ABSENT;
                            size--;
                            break;
                        }
                    }
                }
                size++;
            }
            cached[k] = next_use[i];
            heap_push(heap, &heap_len, (HeapEnt){ next_use[i], k });
            if (heap_len >= n) {
                /* compact: drop stale entries */
                uint64_t j = 0;
                for (uint64_t t = 0; t < heap_len; t++) {
                    if (cached[heap[t].key] == heap[t].next) {
                        heap[j++] = heap[t];
                    }
                }
                uint64_t m = j;
                heap_len = 0;
                for (uint64_t t = 0; t < m; t++) {
                    heap_push(heap, &heap_len, heap[t]);
                }
            }
        }
        printf("belady %llu %llu\n", cap, (unsigned long long)hits);
        printf("lfu %llu %llu\n", cap,
               (unsigned long long)lfu_run(keys, n, nkeys, cap, 0));
        printf("plfu %llu %llu\n", cap,
               (unsigned long long)lfu_run(keys, n, nkeys, cap, 1));
    }
    free(heap);
    free(next_use);
    free(cached);
    free(keys);
    return 0;
}
