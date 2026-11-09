#!/usr/bin/env bash
# Launch one FEMU black-box VM for a baseline run (runs in the foreground;
# run_femu.py backgrounds it). Configuration comes from the environment:
#
#   QEMU       FEMU qemu-system-x86_64 binary (required)
#   RUN_DIR    per-run results directory (required); logs, GC log, reqlog go here
#   IMAGE      guest OS disk (a per-run qcow2 overlay; required)
#   SEED       cloud-init seed ISO (default ~/images/femu-seed.iso)
#   SSH_PORT   host port forwarded to guest :22 (default 8080)
#   DEVSZ_MB   logical device size exposed to the guest (default 30720 = 30 GiB;
#              the FTLs' raw geometry is fixed at 32 GiB, so the rest is de-facto OP)
#   ACCEL      auto | tcg | kvm (default auto: kvm if /dev/kvm is usable, else tcg)
#   SMP, MEM   guest vCPUs / RAM (default 4 / 4G)
#   REQLOG     1 to write FEMU's per-request log to $RUN_DIR/reqlog.csv (default 1)
#
# Latency/throughput must never be reported from ACCEL=tcg runs.
set -euo pipefail

: "${QEMU:?QEMU binary required}"
: "${RUN_DIR:?RUN_DIR required}"
: "${IMAGE:?IMAGE required}"
SEED="${SEED:-${HOME}/images/femu-seed.iso}"
SSH_PORT="${SSH_PORT:-8080}"
DEVSZ_MB="${DEVSZ_MB:-30720}"
ACCEL="${ACCEL:-auto}"
SMP="${SMP:-4}"
MEM="${MEM:-4G}"
REQLOG="${REQLOG:-1}"
# unix socket paths must stay under 108 bytes, so not inside RUN_DIR
QMP_SOCK="${QMP_SOCK:-/tmp/femu-qmp-${USER}-${SSH_PORT}.sock}"

TOOLS="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../tools" && pwd)"
SHIM="${TOOLS}/build/gclog_redirect.so"
if [[ ! -f "$SHIM" || "$TOOLS/gclog_redirect.c" -nt "$SHIM" ]]; then
    mkdir -p "${TOOLS}/build"
    cc -O2 -Wall -Wextra -shared -fPIC -o "$SHIM" "$TOOLS/gclog_redirect.c" -ldl
fi

if [[ "$ACCEL" == auto ]]; then
    if [[ -r /dev/kvm && -w /dev/kvm ]]; then ACCEL=kvm; else ACCEL=tcg; fi
fi
case "$ACCEL" in
    kvm) ACCEL_ARGS=(-enable-kvm -cpu host) ;;
    tcg) ACCEL_ARGS=(-accel tcg,thread=multi -cpu max) ;;
    *) echo "ACCEL must be auto, tcg or kvm" >&2; exit 2 ;;
esac

mkdir -p "$RUN_DIR"
cat > "$RUN_DIR/launch.env" <<EOF
QEMU=$QEMU
IMAGE=$IMAGE
SEED=$SEED
SSH_PORT=$SSH_PORT
DEVSZ_MB=$DEVSZ_MB
ACCEL=$ACCEL
SMP=$SMP
MEM=$MEM
REQLOG=$REQLOG
EOF

export FEMU_GC_LOG_DIR="$RUN_DIR/gclog"
export FEMU_STDOUT_LINEBUF=1
if [[ "$REQLOG" == 1 ]]; then
    export FEMU_REQLOG="$RUN_DIR/reqlog.csv"
else
    unset FEMU_REQLOG
fi
export LD_PRELOAD="$SHIM${LD_PRELOAD:+:$LD_PRELOAD}"

exec "$QEMU" \
    -name "FEMU-baseline" \
    "${ACCEL_ARGS[@]}" \
    -smp "$SMP" \
    -m "$MEM" \
    -device virtio-scsi-pci,id=scsi0 \
    -device scsi-hd,drive=hd0 \
    -drive "file=${IMAGE},if=none,aio=threads,cache=writeback,format=qcow2,id=hd0" \
    -drive "file=${SEED},if=virtio,format=raw,readonly=on" \
    -device "femu,devsz_mb=${DEVSZ_MB},femu_mode=1" \
    -net "user,hostfwd=tcp:127.0.0.1:${SSH_PORT}-:22" \
    -net nic,model=virtio \
    -display none \
    -monitor none \
    -serial "file:${RUN_DIR}/console.log" \
    -qmp "unix:${QMP_SOCK},server=on,wait=off"
