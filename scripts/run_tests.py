"""Run the unit suite and record the result as evidence — reviewer P1-06.

The report used to contain the literal string "61 tests, all passing". The
suite had 62. A hand-typed count is a claim that silently rots the moment
anyone adds a test, and this project's whole rule is that no number appears in
a document unless a script produced it.

So the count is MEASURED: collect, run, and write `out/test_summary.json`.
`scripts/report.py` reads that file. Nobody types a test count again.

Run:  python scripts/run_tests.py            -> out/test_summary.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "out")


def run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "pytest", *args],
                          cwd=ROOT, capture_output=True, text=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--path", default="tests/")
    ap.add_argument("--out", default=os.path.join(OUT, "test_summary.json"))
    args = ap.parse_args()

    collected = run(["--collect-only", "-q", args.path])
    m = re.search(r"(\d+)\s+tests?\s+collected", collected.stdout)
    n_collected = int(m.group(1)) if m else None

    t0 = time.perf_counter()
    res = run(["-q", args.path])
    elapsed = time.perf_counter() - t0
    tail = res.stdout.strip().splitlines()[-1] if res.stdout.strip() else ""

    passed = failed = errors = 0
    for pat, key in ((r"(\d+) passed", "passed"), (r"(\d+) failed", "failed"),
                     (r"(\d+) error", "errors")):
        mm = re.search(pat, res.stdout)
        if mm:
            if key == "passed":
                passed = int(mm.group(1))
            elif key == "failed":
                failed = int(mm.group(1))
            else:
                errors = int(mm.group(1))

    # Collected and executed must agree. If they do not, something was skipped
    # or crashed during collection, and a green "N passed" would be hiding it.
    consistent = (n_collected is None) or (n_collected == passed + failed + errors)

    summary = {
        "path": args.path,
        "collected": n_collected,
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "all_passed": bool(passed and not failed and not errors),
        "collected_matches_executed": consistent,
        "seconds": round(elapsed, 2),
        "pytest_summary_line": tail,
        "returncode": res.returncode,
    }

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=1)

    print(f"collected : {n_collected}")
    print(f"passed    : {passed}   failed: {failed}   errors: {errors}")
    print(f"elapsed   : {elapsed:.1f} s")
    if not consistent:
        print("WARNING: collected count does not match executed count")
    print(f"written   : {args.out}")
    sys.exit(0 if summary["all_passed"] and consistent else 1)


if __name__ == "__main__":
    main()
