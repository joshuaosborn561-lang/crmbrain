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


def _parse_sample_cards(argv: list[str]) -> tuple[int | None, str | None, list[str]]:
    if "--sample-cards" not in argv:
        return None, None, argv
    idx = argv.index("--sample-cards")
    count = 10
    consumed = 1
    if idx + 1 < len(argv) and str(argv[idx + 1]).lstrip("-").isdigit():
        count = max(1, int(argv[idx + 1]))
        consumed = 2
    out_path = None
    rest = argv[:idx] + argv[idx + consumed :]
    if "--out" in rest:
        oidx = rest.index("--out")
        if oidx + 1 < len(rest):
            out_path = rest[oidx + 1]
            rest = rest[:oidx] + rest[oidx + 2 :]
        else:
            rest = rest[:oidx] + rest[oidx + 1 :]
    return count, out_path, rest


def _run_sample_cards(settings: Settings, count: int, out_path: str | None) -> int:
    import json

    from crmbrain.nurture import sample_hubspot_nurture_cards

    if not settings.hubspot_token:
        print(
            "HubSpot token missing in this environment. On the Railway crmbrain "
            "service run:\n  python -m crmbrain --sample-cards 10"
        )
        return 2
    payload = sample_hubspot_nurture_cards(settings, count, out_path=out_path)
    print(json.dumps(payload.get("cards") or [], indent=2))
    print(
        f"\nwrote {payload.get('count', 0)} HubSpot Nurture cards to "
        f"{payload.get('out_path')}"
    )
    skipped = payload.get("skipped") or {}
    if skipped:
        print("skipped: " + ", ".join(f"{k}={v}" for k, v in sorted(skipped.items())))
    return 0


def main() -> int:
    raw = list(sys.argv[1:])
    dry_run = _wants_dry_run(raw)
    reextract_since, raw = _parse_reextract_since(raw)
    sample_n, sample_out, raw = _parse_sample_cards(raw)
    args = [a for a in raw if a != "--dry-run"]
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
    if sample_n is not None:
        return _run_sample_cards(settings, sample_n, sample_out)
    cmd = args[0] if args else "auto"
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
        print(
            "usage: python -m crmbrain [auto|cycle|briefs|google-scopes] "
            "[--dry-run] [--reextract-since YYYY-MM-DD] [--sample-cards N] [--out PATH]"
        )
        return 2
    print(report.summary_text())
    return process_exit_code(report)


if __name__ == "__main__":
    raise SystemExit(main())
