#!/usr/bin/env bash
# Test the GC-log redirect shim against a probe that opens the same hard-coded
# path the way leaftl.c does (fopen with mode "w", then fprintf).
#
# Usage: bash experiments/tools/test_gclog_redirect.sh
set -euo pipefail

TOOLS="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

cc -O2 -Wall -Wextra -shared -fPIC -o "$WORK/gclog_redirect.so" "$TOOLS/gclog_redirect.c" -ldl

cat > "$WORK/probe.c" <<'EOF'
#define _FILE_OFFSET_BITS 64   /* makes glibc call fopen64, as the QEMU binary does */
#include <stdio.h>
int main(int argc, char **argv)
{
    FILE *f = fopen(argv[1], "w");
    if (!f) {
        puts("OPEN_FAILED");
        return 3;
    }
    fprintf(f, "%ld\n", 12345L);
    fclose(f);
    puts("OPEN_OK");
    return 0;
}
EOF
cc -O2 -Wall -o "$WORK/probe" "$WORK/probe.c"
nm -D "$WORK/probe" | grep -qw fopen64 || { echo "probe does not import fopen64" >&2; exit 1; }

GC=/home/lzh/femu/log/gc_frequency.txt
fail=0

# 1. Without the shim the hard-coded path is unwritable (unless it already exists).
if [[ ! -d /home/lzh/femu/log ]]; then
    out="$("$WORK/probe" "$GC" || true)"
    [[ "$out" == "OPEN_FAILED" ]] && echo "ok   without shim: open fails as expected" \
        || { echo "FAIL without shim: got '$out'"; fail=1; }
fi

# 2. With the shim the file lands in FEMU_GC_LOG_DIR.
out="$(FEMU_GC_LOG_DIR="$WORK/gc" LD_PRELOAD="$WORK/gclog_redirect.so" "$WORK/probe" "$GC")"
if [[ "$out" == "OPEN_OK" && "$(cat "$WORK/gc/gc_frequency.txt")" == "12345" ]]; then
    echo "ok   with shim: redirected to \$FEMU_GC_LOG_DIR"
else
    echo "FAIL with shim: got '$out'"; fail=1
fi

# 3. Unrelated paths are untouched.
FEMU_GC_LOG_DIR="$WORK/gc" LD_PRELOAD="$WORK/gclog_redirect.so" "$WORK/probe" "$WORK/other.txt" >/dev/null
if [[ "$(cat "$WORK/other.txt")" == "12345" && ! -e "$WORK/gc/other.txt" ]]; then
    echo "ok   other paths pass through unchanged"
else
    echo "FAIL pass-through"; fail=1
fi

# 4. Without FEMU_GC_LOG_DIR the shim does nothing (open still fails).
if [[ ! -d /home/lzh/femu/log ]]; then
    out="$(LD_PRELOAD="$WORK/gclog_redirect.so" "$WORK/probe" "$GC" || true)"
    [[ "$out" == "OPEN_FAILED" ]] && echo "ok   no env var: shim is inert" \
        || { echo "FAIL inert case: got '$out'"; fail=1; }
fi

exit $fail
