#!/usr/bin/env python3
"""Trigger FEMU's LeaFTL statistics command from inside the guest."""

from __future__ import annotations

import argparse
import ctypes
import fcntl
from pathlib import Path


NVME_IOCTL_ADMIN_CMD = 0xC0484E41
FEMU_ADMIN_OPCODE = 0xEF


class NvmeAdminCommand(ctypes.Structure):
    _fields_ = [
        ("opcode", ctypes.c_uint8),
        ("flags", ctypes.c_uint8),
        ("rsvd1", ctypes.c_uint16),
        ("nsid", ctypes.c_uint32),
        ("cdw2", ctypes.c_uint32),
        ("cdw3", ctypes.c_uint32),
        ("metadata", ctypes.c_uint64),
        ("addr", ctypes.c_uint64),
        ("metadata_len", ctypes.c_uint32),
        ("data_len", ctypes.c_uint32),
        ("cdw10", ctypes.c_uint32),
        ("cdw11", ctypes.c_uint32),
        ("cdw12", ctypes.c_uint32),
        ("cdw13", ctypes.c_uint32),
        ("cdw14", ctypes.c_uint32),
        ("cdw15", ctypes.c_uint32),
        ("timeout_ms", ctypes.c_uint32),
        ("result", ctypes.c_uint32),
    ]


def send(command: int) -> None:
    request = NvmeAdminCommand(opcode=FEMU_ADMIN_OPCODE, cdw10=command)
    payload = bytearray(ctypes.string_at(ctypes.byref(request), ctypes.sizeof(request)))
    controller = next(
        (path for path in (Path("/dev/nvme0"), Path("/dev/nvme")) if path.exists()),
        None,
    )
    if controller is None:
        raise FileNotFoundError("neither /dev/nvme0 nor /dev/nvme exists")
    with controller.open("rb", buffering=0) as device:
        fcntl.ioctl(device.fileno(), NVME_IOCTL_ADMIN_CMD, payload, True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", type=int, choices=(8, 9), help="8=reset, 9=print")
    args = parser.parse_args()
    send(args.command)
    print("completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
