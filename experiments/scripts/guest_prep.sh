#!/usr/bin/env bash
# Prepare the FEMU NVMe device inside the guest so the block layer passes the
# replayed request stream through unchanged: no merging, no readahead, no I/O
# scheduler, and no splitting of requests up to the hardware limit.
# Prints the resulting settings as JSON. Run with sudo.
set -euo pipefail

DEV="${1:-nvme0n1}"
Q="/sys/block/${DEV}/queue"
[[ -d "$Q" ]] || { echo "no such block device: $DEV" >&2; exit 1; }

# udev re-probes a block device (blkid reads at offset 0 and the GPT backup
# at the end) after any close-after-write, and multipathd/snapd probe too;
# those reads would land inside the measured workload.
for unit in multipathd.service multipathd.socket snapd.service snapd.socket \
            systemd-udevd.service systemd-udevd-control.socket systemd-udevd-kernel.socket; do
    systemctl stop "$unit" 2>/dev/null || true
done

echo 2 > "$Q/nomerges"
echo 0 > "$Q/read_ahead_kb"
echo none > "$Q/scheduler"
cat "$Q/max_hw_sectors_kb" > "$Q/max_sectors_kb"

printf '{"device":"%s","bytes":%s,"nomerges":%s,"read_ahead_kb":%s,"scheduler":"%s","max_sectors_kb":%s,"max_hw_sectors_kb":%s,"logical_block_size":%s,"guest_kernel":"%s"}\n' \
    "$DEV" "$(( $(cat /sys/block/$DEV/size) * 512 ))" "$(cat $Q/nomerges)" "$(cat $Q/read_ahead_kb)" \
    "$(sed 's/.*\[\(.*\)\].*/\1/' $Q/scheduler)" "$(cat $Q/max_sectors_kb)" \
    "$(cat $Q/max_hw_sectors_kb)" "$(cat $Q/logical_block_size)" "$(uname -r)"
