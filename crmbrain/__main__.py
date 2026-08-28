from __future__ import annotations

import sys

from crmbrain.cycle import run


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "cycle"
    if cmd != "cycle":
        print("usage: python -m crmbrain cycle")
        return 2
    report = run()
    print(report.summary_text())
    return 0 if not report.errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
