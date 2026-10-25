"""Fail closed if mutmut cannot run or its results are missing/incomplete."""
from __future__ import annotations

import os
import subprocess
from collections import Counter


def check_results(output: str, max_survivors: int) -> Counter:
    counts: Counter = Counter()
    names = set()
    for line in output.splitlines():
        if not line.strip():
            continue
        name, separator, status = line.strip().rpartition(": ")
        if not separator or not name or name in names or status not in {"killed", "survived"}:
            raise ValueError(f"incomplete or invalid mutation result: {line}")
        names.add(name)
        counts[status] += 1
    if not counts["killed"]:
        raise ValueError("no killed mutants: missing results or tests did not exercise the mutations")
    if counts["survived"] > max_survivors:
        raise ValueError(f"{counts['survived']} survivors exceed the allowed {max_survivors}")
    return counts


def main() -> int:
    try:
        subprocess.run(["mutmut", "run"], check=True)
        result = subprocess.run(["mutmut", "results", "--all", "true"],
                                capture_output=True, text=True, check=True)
        counts = check_results(result.stdout, int(os.environ.get("MAX_SURVIVORS", "11")))
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"mutation check failed: {exc}")
        return 1
    print(f"Mutation results: {dict(counts)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
