#!/usr/bin/env python3
"""Check cachesim.c against brute-force reference implementations.

Policies checked: LRU (via stack distances), Belady-OPT, in-cache LFU and
perfect LFU, on random streams. Small capacities on long streams force the
LFU heap-compaction path.

Usage:  python3 experiments/tools/test_cachesim.py
"""

from __future__ import annotations

import collections
import importlib.util
import random
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

TOOLS = Path(__file__).resolve().parent
BELADY = TOOLS.parent / "scripts" / "belady.py"
CAPS = [1, 2, 3, 7, 32, 100, 1000]


def load_belady():
    spec = importlib.util.spec_from_file_location("belady", BELADY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ref_lru(keys, cap):
    cache, hits = collections.OrderedDict(), 0
    for key in keys:
        if key in cache:
            hits += 1
            cache.move_to_end(key)
        else:
            cache[key] = 1
            if len(cache) > cap:
                cache.popitem(last=False)
    return hits


def ref_lfu(keys, cap):
    """In-cache LFU: count starts at 1 on insert, +1 per hit, dropped on eviction.
    Victim: smallest count, ties -> least recently used."""
    cache, hits = {}, 0
    for i, key in enumerate(keys):
        if key in cache:
            hits += 1
            cache[key][0] += 1
            cache[key][1] = i
        else:
            if len(cache) >= cap:
                victim = min(cache, key=lambda k: (cache[k][0], cache[k][1]))
                del cache[victim]
            cache[key] = [1, i]
    return hits


def ref_plfu(keys, cap):
    """Perfect LFU: counts accumulate over the whole trace and survive eviction."""
    count, cache, hits = collections.Counter(), {}, 0
    for i, key in enumerate(keys):
        count[key] += 1
        if key in cache:
            hits += 1
            cache[key] = i
        else:
            if len(cache) >= cap:
                victim = min(cache, key=lambda k: (count[k], cache[k]))
                del cache[victim]
            cache[key] = i
    return hits


def main() -> int:
    belady = load_belady()
    with tempfile.TemporaryDirectory(prefix="test_cachesim.") as tmp:
        tmp = Path(tmp)
        binary = tmp / "cachesim"
        subprocess.run(["cc", "-O2", "-Wall", "-Wextra", "-o", str(binary), str(TOOLS / "cachesim.c")], check=True)
        streams = 0
        for trial in range(60):
            rng = random.Random(trial)
            n = rng.randint(1, 4000)
            nkeys = rng.randint(1, 400)
            if trial % 3 == 0:
                keys = [rng.randrange(nkeys) for _ in range(n)]          # uniform
            elif trial % 3 == 1:
                keys = [int(rng.paretovariate(1.1)) % nkeys for _ in range(n)]  # skewed
            else:                                                         # scan + hot set
                keys = [(i % nkeys) if rng.random() < 0.6 else rng.randrange(min(nkeys, 5)) for i in range(n)]
            np.array(keys, dtype="<u4").tofile(tmp / "k.u32")
            out = subprocess.run([str(binary), str(tmp / "k.u32"), str(tmp / "sd.u32"), *map(str, CAPS)],
                                 check=True, capture_output=True, text=True).stdout
            got = collections.defaultdict(dict)
            for line in out.splitlines():
                policy, cap, hits = line.split()
                got[policy][int(cap)] = int(hits)
            sd = np.fromfile(tmp / "sd.u32", dtype="<u4")
            for cap in CAPS:
                expect = {
                    "lru": ref_lru(keys, cap),
                    "belady": belady.belady_stats(keys, cap)[0],
                    "lfu": ref_lfu(keys, cap),
                    "plfu": ref_plfu(keys, cap),
                }
                assert int((sd < cap).sum()) == expect["lru"], ("lru", trial, cap)
                for policy in ("belady", "lfu", "plfu"):
                    assert got[policy][cap] == expect[policy], (policy, trial, cap, got[policy][cap], expect[policy])
            streams += 1
    print(f"cachesim OK: LRU, Belady, LFU and perfect LFU match references on {streams} random streams x {len(CAPS)} capacities")
    return 0


if __name__ == "__main__":
    sys.exit(main())
