#!/usr/bin/env bash
set -euo pipefail

IMGDIR="${HOME}/images"
PRISTINE="${FEMU_PRISTINE_IMAGE:-${IMGDIR}/femu-pristine.qcow2}"
TARGET="${1:-${IMGDIR}/femu-rerun.qcow2}"

if [[ ! -f "$PRISTINE" ]]; then
    echo "Pristine image not found: $PRISTINE" >&2
    exit 1
fi

if [[ "$TARGET" == "$PRISTINE" ]]; then
    echo "Refusing to overwrite the pristine image." >&2
    exit 1
fi

cp --reflink=auto --preserve=mode,timestamps "$PRISTINE" "$TARGET"
chmod u+rw,go+r "$TARGET"
printf 'Prepared fresh FEMU image: %s\n' "$TARGET"
