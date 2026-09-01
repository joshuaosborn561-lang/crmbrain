from __future__ import annotations

import sys

from crmbrain.config import now_utc
from crmbrain.cycle import run

FULL_CYCLE_HOURS_UTC = {12, 22}


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "auto"
    if cmd == "auto":
        briefs_only = now_utc().hour not in FULL_CYCLE_HOURS_UTC
        report = run(briefs_only=briefs_only)
    elif cmd == "cycle":
        report = run()
    elif cmd == "briefs":
        report = run(briefs_only=True)
    else:
        print("usage: python -m crmbrain [auto|cycle|briefs]")
        return 2
    print(report.summary_text())
    return 0 if not report.errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
