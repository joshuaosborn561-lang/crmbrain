from __future__ import annotations

import os
import sys
from dataclasses import replace

from crmbrain.config import Settings, now_utc
from crmbrain.cycle import process_exit_code, run
from crmbrain.google_auth import report_scopes

FULL_CYCLE_HOURS_UTC = {12, 22}


def _wants_dry_run(argv: list[str]) -> bool:
    if "--dry-run" in argv:
        return True
    return os.getenv("CRMBRAIN_DRY_RUN", "").strip().lower() in {"1", "true", "yes"}


def _parse_reextract_since(argv: list[str]):
    from crmbrain.config import _parse_lookback_start

    if "--reextract-since" not in argv:
        return None, argv
    idx = argv.index("--reextract-since")
    if idx + 1 >= len(argv):
        return None, argv
    stamp = _parse_lookback_start(argv[idx + 1])
    cleaned = argv[:idx] + argv[idx + 2 :]
    return stamp, cleaned


def main() -> int:
    raw = list(sys.argv[1:])
    dry_run = _wants_dry_run(raw)
    reextract_since, raw = _parse_reextract_since(raw)
    args = [a for a in raw if a != "--dry-run"]
    cmd = args[0] if args else "auto"
    settings = Settings.from_env()
    if dry_run:
        settings = replace(settings, dry_run=True)
    if reextract_since is not None:
        settings = replace(
            settings,
            reextract_since=reextract_since,
            lookback_start_at=reextract_since,
            lookback_override=True,
        )
    if cmd == "google-scopes":
        print(report_scopes(settings).as_text())
        return 0
    if cmd == "auto":
        briefs_only = now_utc().hour not in FULL_CYCLE_HOURS_UTC
        report = run(settings=settings, briefs_only=briefs_only)
    elif cmd == "cycle":
        report = run(settings=settings)
    elif cmd == "briefs":
        report = run(settings=settings, briefs_only=True)
    else:
        print("usage: python -m crmbrain [auto|cycle|briefs|google-scopes] [--dry-run] [--reextract-since YYYY-MM-DD]")
        return 2
    print(report.summary_text())
    return process_exit_code(report)


if __name__ == "__main__":
    raise SystemExit(main())
