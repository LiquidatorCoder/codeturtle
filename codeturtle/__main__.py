"""Command line entry point.

    python3 -m codeturtle action                                 # inside GitHub Actions
    python3 -m codeturtle review --repo owner/name --pr 12 \\
        [--tier fast|normal|full] [--providers codex,grok,claude] [--dry-run]
    python3 -m codeturtle doctor [--providers codex,grok,claude] # is every model CLI ready?
    python3 -m codeturtle stats --repo owner/name [--prs 50]     # how often findings get addressed
    python3 -m codeturtle fleet [--mode sync|pr|check]          # keep every repo's workflow current
    python3 -m codeturtle release [--dry-run]                    # tag __version__, move the major tag
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys

from . import TIERS
from .config import DEFAULTS
from .events import BadRequest, Request, from_event
from .health import doctor
from .review import Reviewer, log


def providers_arg(value: str | None) -> list[str] | None:
    names = [p.strip().lower() for p in (value or "").split(",") if p.strip()]
    return names or None


def action() -> int:
    # A cancelled job gets SIGTERM: exit through `finally` so the progress comment is removed.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    name = os.environ["GITHUB_EVENT_NAME"]
    with open(os.environ["GITHUB_EVENT_PATH"]) as fh:
        event = json.load(fh)
    try:
        req, skip = from_event(
            name, event, os.environ["GITHUB_REPOSITORY"],
            tier_input=os.environ.get("CODETURTLE_TIER", ""),
            pr_input=os.environ.get("CODETURTLE_PR", ""),
            min_tier_input=os.environ.get("CODETURTLE_MIN_TIER", ""),
            request_id_input=os.environ.get("CODETURTLE_REQUEST_ID", ""),
            fresh_input=os.environ.get("CODETURTLE_FRESH", ""),
        )
    except BadRequest as e:
        log(f"Bad request: {e}")
        return 1
    if req is None:
        log(f"Skipping: {skip}")
        return 0
    if req.override and req.override not in TIERS:
        log(f"Unknown tier {req.override!r}; expected one of {', '.join(TIERS)}")
        return 1
    dry_run = os.environ.get("CODETURTLE_DRY_RUN", "").lower() == "true"
    return Reviewer(req, providers_arg(os.environ.get("CODETURTLE_PROVIDERS")), dry_run).run()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="codeturtle")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("action", help="run inside a GitHub Actions job")
    review = sub.add_parser("review", help="review one PR from the command line")
    review.add_argument("--repo", required=True)
    review.add_argument("--pr", type=int, required=True)
    review.add_argument("--tier", choices=TIERS)
    review.add_argument("--min-tier", choices=TIERS, help="lowest tier allowed (raises the rule floor)")
    review.add_argument("--providers", help="comma-separated order, e.g. grok,claude")
    review.add_argument("--dry-run", action="store_true", help="write the review to out/ instead of posting")
    review.add_argument("--fresh", action="store_true", help="review the whole PR, not just what changed since the last review")
    check = sub.add_parser("doctor", help="check that every model CLI is installed and logged in (exit 1 if not)")
    check.add_argument("--providers", help="comma-separated, default: codex,grok,claude")
    stats = sub.add_parser("stats", help="how often findings get addressed, by severity, category, tier and version")
    stats.add_argument("--repo", action="append", required=True, help="owner/name; repeat for several")
    stats.add_argument("--prs", type=int, default=50, help="most recent PRs per repo (default 50, max 100)")
    stats.add_argument("--json", action="store_true", help="print the numbers as JSON")
    fleet = sub.add_parser("fleet", help="find every repo with the codeturtle workflow and bring it to the latest template")
    fleet.add_argument("--mode", choices=("sync", "pr", "check"), default="sync",
                       help="sync: update outdated copies through a PR merged at once (default); "
                            "pr: leave the PR open; check: only report")
    fleet.add_argument("--owner", action="append", help="only this user's or organization's repos; repeat for several")
    fleet.add_argument("--repo", action="append", help="only owner/name; repeat for several")
    fleet.add_argument("--json", action="store_true", help="print the outcome as JSON")
    rel = sub.add_parser("release", help="tag __version__ and point the major tag (v1) at the newest release")
    rel.add_argument("--dry-run", action="store_true", help="print the plan; tag nothing")
    args = parser.parse_args(argv)
    if args.command == "action":
        return action()
    if args.command == "doctor":
        return doctor(providers_arg(args.providers) or DEFAULTS["providers"], DEFAULTS["models"])
    if args.command == "stats":
        from .stats import main as stats_main
        return stats_main(args.repo, max(1, min(args.prs, 100)), args.json)
    if args.command == "fleet":
        from .fleet import main as fleet_main
        return fleet_main(args.mode, args.owner, args.repo, args.json)
    if args.command == "release":
        from .release import main as release_main
        return release_main(args.dry_run)
    req = Request(args.repo, args.pr, "cli", args.tier, min_tier=args.min_tier, fresh=args.fresh)
    return Reviewer(req, providers_arg(args.providers), args.dry_run).run()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
