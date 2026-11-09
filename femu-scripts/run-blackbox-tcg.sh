#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
IMGDIR="${HOME}/images"
OSIMGF="${FEMU_OS_IMAGE:-${IMGDIR}/u20s.qcow2}"
SEEDIMG="${IMGDIR}/femu-seed.iso"
QEMU="${SCRIPT_DIR}/../build-femu/qemu-system-x86_64"
LOG="${SCRIPT_DIR}/../build-femu/log"
QMP="${SCRIPT_DIR}/../build-femu/qmp-sock"

if [[ ! -x "$QEMU" ]]; then
    echo "FEMU binary not found: $QEMU" >&2
    echo "Build FEMU before running this script." >&2
    exit 1
fi

if [[ ! -e "$OSIMGF" ]]; then
    echo "VM disk image could not be found: $OSIMGF" >&2
    echo "Create a user-owned Ubuntu image and rerun this script." >&2
    exit 1
fi

if [[ ! -e "$SEEDIMG" ]]; then
    echo "Cloud-init seed image could not be found: $SEEDIMG" >&2
    exit 1
fi

exec "$QEMU" \
    -name "FEMU-BBSSD-VM" \
    -accel tcg,thread=multi \
    -cpu max \
    -smp 4 \
    -m 4G \
    -device virtio-scsi-pci,id=scsi0 \
    -device scsi-hd,drive=hd0 \
    -drive "file=${OSIMGF},if=none,aio=threads,cache=writeback,format=qcow2,id=hd0" \
    -drive "file=${SEEDIMG},if=virtio,format=raw,readonly=on" \
    -device femu,devsz_mb=32768,femu_mode=1 \
    -net user,hostfwd=tcp::8080-:22 \
    -net nic,model=virtio \
    -nographic \
    -qmp "unix:${QMP},server=on,wait=off" \
    2>&1 | tee "${LOG}"
