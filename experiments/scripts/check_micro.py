#!/usr/bin/env python3
"""Check micro-trace runs against the hand-derived expectations.

    check_micro.py RUN_DIR...

Reads experiments/traces/micro/expected.json and compares each run's workload
record (records.jsonl[-1]) with the conditions for its trace and FTL.
Exit code 0 iff every condition holds. Writes RUN_DIR/micro_check.json.
"""

from __future__ import annotations

import json
import operator
import sys
from pathlib import Path

EXPECTED = Path(__file__).resolve().parents[1] / "traces" / "micro" / "expected.json"
OPS = {"==": operator.eq, ">=": operator.ge, "<=": operator.le}


def main() -> int:
    expected = json.loads(EXPECTED.read_text())
    status = 0
    for arg in sys.argv[1:]:
        run_dir = Path(arg)
        manifest = json.loads((run_dir / "manifest.json").read_text())
        record = json.loads((run_dir / "records.jsonl").read_text().splitlines()[-1])
        conditions = expected.get(manifest["workload"])
        if conditions is None:
            print(f"{run_dir.name}: no expectations for {manifest['workload']}")
            status = 1
            continue
        results = []
        for scope in ("both", manifest["ftl"]):
            for field, (op, value) in conditions.get(scope, {}).items():
                actual = record.get(field)
                ok = actual is not None and OPS[op](actual, value)
                results.append({"field": field, "op": op, "expected": value, "actual": actual, "ok": ok})
        passed = all(r["ok"] for r in results)
        (run_dir / "micro_check.json").write_text(json.dumps(
            {"workload": manifest["workload"], "ftl": manifest["ftl"], "passed": passed,
             "conditions": results}, indent=2) + "\n")
        print(f"{run_dir.name}: {'PASS' if passed else 'FAIL'} "
              f"({sum(r['ok'] for r in results)}/{len(results)})")
        for r in results:
            if not r["ok"]:
                print(f"    {r['field']}: expected {r['op']} {r['expected']}, got {r['actual']}")
        status |= 0 if passed else 1
    return status


if __name__ == "__main__":
    sys.exit(main())
