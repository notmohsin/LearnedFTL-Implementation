#!/usr/bin/env bash
# Build one FEMU binary per FTL from a committed revision, without touching
# the main checkout (which always stays on LeaFTL as committed).
#
#   experiments/scripts/build_ftl.sh leaftl|learnedftl [REV]   (REV default: HEAD)
#
# Creates a detached git worktree at build-ftl/<ftl>/src at REV, applies the
# README's FTL switch there (the bbssd/*.c entry in hw/femu/meson.build and the
# #include in hw/femu/bbssd/bb.c), configures and builds in build-ftl/<ftl>/build,
# and writes build-ftl/<ftl>/build_info.json with the commit, the switch diff,
# the compiler and the binary's SHA-256.
set -euo pipefail

FTL="${1:?usage: build_ftl.sh leaftl|learnedftl [REV]}"
REV="${2:-HEAD}"
case "$FTL" in
    leaftl) SRC_C=leaftl.c; SRC_H=leaftl.h ;;
    learnedftl) SRC_C=ld-tpftl.c; SRC_H=ld-tpftl.h ;;
    *) echo "unknown FTL: $FTL" >&2; exit 2 ;;
esac

REPO="$(git -C "$(dirname -- "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"
SHA="$(git -C "$REPO" rev-parse --verify "${REV}^{commit}")"
ROOT="$REPO/build-ftl/$FTL"
WT="$ROOT/src"
BUILD="$ROOT/build"
mkdir -p "$ROOT"
export PATH="$HOME/.local/bin:$PATH"   # ninja

if [[ -d "$WT" ]]; then
    if [[ "$(git -C "$WT" rev-parse HEAD)" != "$SHA" ]]; then
        echo "worktree at $(git -C "$WT" rev-parse --short HEAD), moving to ${SHA:0:12}"
        git -C "$WT" checkout -q -- hw/femu/meson.build hw/femu/bbssd/bb.c
        git -C "$WT" checkout -q --detach "$SHA"
    fi
else
    git -C "$REPO" worktree add -q --detach "$WT" "$SHA"
fi

# The worktree must hold exactly REV plus the FTL switch.
git -C "$WT" checkout -q -- hw/femu/meson.build hw/femu/bbssd/bb.c
if [[ -n "$(git -C "$WT" status --porcelain --untracked-files=no)" ]]; then
    echo "worktree $WT has unexpected changes" >&2
    git -C "$WT" status --short >&2
    exit 1
fi
sed -i -E "s#'bbssd/(leaftl|ld-tpftl|dftl|tpftl)\.c'#'bbssd/${SRC_C}'#" "$WT/hw/femu/meson.build"
sed -i -E "s#^\#include \"\./(leaftl|ld-tpftl|dftl|tpftl)\.h\"#\#include \"./${SRC_H}\"#" "$WT/hw/femu/bbssd/bb.c"
grep -q "'bbssd/${SRC_C}'" "$WT/hw/femu/meson.build" || { echo "meson switch failed" >&2; exit 1; }
grep -q "#include \"./${SRC_H}\"" "$WT/hw/femu/bbssd/bb.c" || { echo "bb.c switch failed" >&2; exit 1; }
git -C "$WT" diff > "$ROOT/switch.diff"

mkdir -p "$BUILD"
cd "$BUILD"
if [[ ! -f build.ninja ]]; then
    "$WT/configure" --enable-kvm --disable-werror --disable-docs \
        --target-list=x86_64-softmmu > configure.log 2>&1
fi
ninja -j "$(nproc)" qemu-system-x86_64 qemu-img > ninja.log 2>&1 || { tail -40 ninja.log >&2; exit 1; }

BIN="$BUILD/qemu-system-x86_64"
python3 - "$ROOT/build_info.json" <<EOF
import hashlib, json, subprocess, sys, datetime
def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""): h.update(c)
    return h.hexdigest()
info = {
    "ftl": "$FTL", "commit": "$SHA", "source_file": "hw/femu/bbssd/$SRC_C",
    "worktree": "$WT", "binary": "$BIN", "binary_sha256": sha("$BIN"),
    "switch_diff_sha256": sha("$ROOT/switch.diff"),
    "compiler": subprocess.run(["cc", "--version"], capture_output=True, text=True).stdout.splitlines()[0],
    "configure": "--enable-kvm --disable-werror --disable-docs --target-list=x86_64-softmmu",
    "built": datetime.datetime.now().isoformat(timespec="seconds"),
}
json.dump(info, open(sys.argv[1], "w"), indent=2)
print(json.dumps(info, indent=2))
EOF
