/*
 * gclog_redirect: LD_PRELOAD shim that redirects LeaFTL's hard-coded GC log
 * path to a directory we can write, without modifying the authors' code.
 *
 * leaftl.c opens "/home/lzh/femu/log/gc_frequency.txt" with no NULL check
 * (KI-2 in experiments/docs/KNOWN_ISSUES.md). On a machine where that path
 * cannot be created (no root), QEMU crashes on the first erase on
 * channel 0 / LUN 0.
 *
 * Any fopen()/fopen64() whose path starts with FROM_PREFIX is redirected to
 * $FEMU_GC_LOG_DIR/<rest of path> (directory created if needed). Every other
 * path is passed through untouched.
 *
 * Build: cc -O2 -shared -fPIC -o gclog_redirect.so gclog_redirect.c -ldl
 * Use:   FEMU_GC_LOG_DIR=/path/to/run/dir \
 *        LD_PRELOAD=$PWD/gclog_redirect.so ./qemu-system-x86_64 ...
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

#define FROM_PREFIX "/home/lzh/femu/log/"

typedef FILE *(*fopen_fn)(const char *, const char *);

static int redirect(const char *path, char *out, size_t out_len)
{
    const char *dir = getenv("FEMU_GC_LOG_DIR");
    if (!dir || !*dir || strncmp(path, FROM_PREFIX, strlen(FROM_PREFIX)) != 0) {
        return 0;
    }
    if (snprintf(out, out_len, "%s/%s", dir, path + strlen(FROM_PREFIX)) >= (int)out_len) {
        return 0;
    }
    mkdir(dir, 0777); /* one level is enough; the run script creates parents */
    return 1;
}

static FILE *call_next(const char *name, const char *path, const char *mode)
{
    fopen_fn next = (fopen_fn)dlsym(RTLD_NEXT, name);
    if (!next) {
        errno = ENOSYS;
        return NULL;
    }
    char redirected[PATH_MAX];
    if (redirect(path, redirected, sizeof(redirected))) {
        return next(redirected, mode);
    }
    return next(path, mode);
}

FILE *fopen(const char *path, const char *mode)
{
    return call_next("fopen", path, mode);
}

FILE *fopen64(const char *path, const char *mode)
{
    return call_next("fopen64", path, mode);
}
