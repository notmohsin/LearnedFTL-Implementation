#!/usr/bin/env python3
"""Self-test for validate_run.py: each mutation must be caught by the right invariant.

    test_validate_run.py [FIXTURE_DIR]

FIXTURE_DIR (default experiments/scripts/tests/fixture) is a copy of a real run
(manifest.json, records.jsonl, reqlog.csv.gz, the trace). The unmodified copy
must pass every HARD check; each mutation below must make exactly the named
invariant fail. This shows the checks fire on real violations rather than
passing vacuously.
"""

from __future__ import annotations

import copy
import gzip
import json
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import validate_run  # noqa: E402


def load(fixture: Path):
    manifest = json.loads((fixture / "manifest.json").read_text())
    records = [json.loads(l) for l in (fixture / "records.jsonl").read_text().splitlines() if l]
    with gzip.open(fixture / "reqlog.csv.gz", "rt") as handle:
        reqlog = handle.read().splitlines()
    return manifest, records, reqlog


def materialise(base: Path, manifest, records, reqlog, trace: Path) -> Path:
    run = Path(tempfile.mkdtemp(dir=base))
    manifest = dict(manifest, trace=str(trace))
    (run / "manifest.json").write_text(json.dumps(manifest))
    (run / "records.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))
    with gzip.open(run / "reqlog.csv.gz", "wt") as handle:
        handle.write("\n".join(reqlog) + "\n")
    return run


def mutations(manifest, records, reqlog):
    """(name, expected failing invariant, mutated manifest, records, reqlog)."""
    def rec_edit(index, **changes):
        out = copy.deepcopy(records)
        out[index].update(changes)
        return out

    w = records[-1]
    workload_lines = [i for i, line in enumerate(reqlog) if not line.startswith("#")]
    last_marker = max(i for i, line in enumerate(reqlog) if line.startswith("#"))
    last_req = max(i for i in workload_lines if i < last_marker)
    swapped = list(reqlog)
    a, b = last_req - 1, last_req
    swapped[a], swapped[b] = swapped[b], swapped[a]
    extra = list(reqlog)
    extra.insert(last_marker, "999999,R,0,8")
    phases = copy.deepcopy(manifest)
    phases["phases"]["workload"]["result"]["verify_errors"] = 1
    yield "read_reqs off by one", "1", manifest, rec_edit(-1, read_reqs=w["read_reqs"] + 1), reqlog
    yield "request log reordered", "1", manifest, records, swapped
    yield "extra request in log", "1", manifest, records, extra
    yield "read-verify error", "1", phases, records, reqlog
    yield "partition broken", "2", manifest, rec_edit(-1, served_lpns=w["served_lpns"] + 1,
                                                         mapped_read_lpns=w["mapped_read_lpns"] + 1), reqlog
    yield "served hit > hits", "3", manifest, rec_edit(-1, served_cmt_hits=w["cmt_hits"] + 1), reqlog
    yield "in-FEMU inv4", "4", manifest, rec_edit(-1, inv4_violations=1), reqlog
    yield "write split mismatch", "5", manifest, rec_edit(-1, nand_writes_user=w["nand_writes_user"] + 1), reqlog
    yield "in-FEMU inv6", "6", manifest, rec_edit(-1, inv6_violations=1), reqlog
    key = "inv7_violations" if manifest["ftl"] == "learnedftl" else "inv7_line_bad"
    yield "in-FEMU inv7", "7", manifest, rec_edit(-1, **{key: 1}), reqlog
    yield "since-boot counter decreased", "9", manifest, rec_edit(-2, gc_cnt=w["gc_cnt"] + 5), reqlog
    yield "size histogram off", "10", manifest, rec_edit(-1, read_size_hist=[w["read_size_hist"][0] + 1]
                                                         + w["read_size_hist"][1:]), reqlog
    yield "energy off by one write", "12", manifest, rec_edit(-1, energy_write=w["energy_write"] + 16.7), reqlog


def main() -> int:
    fixture = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "tests" / "fixture"
    manifest, records, reqlog = load(fixture)
    trace = next(fixture.glob("*.trace"))
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        clean = validate_run.validate(materialise(base, manifest, records, reqlog, trace))
        ok = clean["valid"]
        print(f"{'ok  ' if ok else 'FAIL'} unmodified fixture passes ({clean['hard_checks_passed']})")
        failures += not ok
        for name, inv, m, r, q in mutations(manifest, records, reqlog):
            result = validate_run.validate(materialise(base, m, r, q, trace))
            ok = inv in result["failed_invariant_ids"]
            print(f"{'ok  ' if ok else 'FAIL'} {name}: expected inv{inv} to fail, "
                  f"failed={result['failed_invariant_ids']}")
            failures += not ok
        determinism = []
        for change in (False, True):
            r = copy.deepcopy(records)
            if change:
                r[-1]["cmt_hits"] += 1
            determinism.append(materialise(base, manifest, r, reqlog, trace))
        ok = validate_run.compare(determinism[:1] + determinism[:1]) == 0 and \
            validate_run.compare(determinism) == 1
        print(f"{'ok  ' if ok else 'FAIL'} invariant 11 passes identical runs and catches a differing counter")
        failures += not ok
    print(f"{failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
