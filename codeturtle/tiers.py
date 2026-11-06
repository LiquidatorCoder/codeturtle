"""Tier selection: a rule-based floor that a triage model may raise but never lower."""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import TIERS, max_tier, tier_rank
from .config import matches_any
from .diff import FileChange

# `/review`, `/review full`, `/review fresh`, `/review fresh full`. "fresh" reviews the whole PR
# instead of following up on the last review.
COMMAND_RE = re.compile(r"^\s*/review(\s+fresh)?(?:\s+(fast|normal|full))?\s*$", re.IGNORECASE)
LABEL_PREFIX = "review:"


@dataclass
class TierDecision:
    tier: str
    source: str  # "override", "rules", "triage"
    reason: str


def is_sensitive(f: FileChange, rules: dict) -> bool:
    return matches_any(f.path, rules["full_paths"]) or bool(
        f.previous_path and matches_any(f.previous_path, rules["full_paths"]))


def is_production(f: FileChange, rules: dict) -> bool:
    """Shipped code: not generated, not a test, not docs or assets. Only these lines count toward
    the size rules, so a PR that is mostly tests or docs doesn't get an expensive review."""
    return not f.ignored and not matches_any(f.path, rules.get("test_paths", [])) \
        and not matches_any(f.path, rules["fast_paths"])


def floor_tier(files: list[FileChange], rules: dict) -> tuple[str, str]:
    """Lowest tier this change may get, from paths and size alone (no model involved)."""
    reviewed = [f for f in files if not f.ignored]
    production = [f for f in reviewed if is_production(f, rules)]
    lines = sum(f.changes for f in production)

    risky = [f.path for f in files if is_sensitive(f, rules)]
    if risky:
        shown = ", ".join(risky[:3]) + (f" and {len(risky) - 3} more" if len(risky) > 3 else "")
        return "full", f"touches sensitive paths ({shown})"
    if lines > rules["full_if_lines_over"]:
        return "full", f"{lines} changed production lines (over {rules['full_if_lines_over']})"
    if not reviewed:
        return "fast", "only generated or lock files changed"
    if all(matches_any(f.path, rules["fast_paths"]) for f in reviewed):
        return "fast", "only docs or assets changed"
    if not production:
        return "fast", "only tests, docs or assets changed"
    if lines < rules["fast_if_lines_under"]:
        return "fast", f"small change ({lines} production lines)"
    return "normal", f"{lines} changed production lines across {len(production)} files"


def raise_floor(floor: str, reason: str, min_tier: str | None) -> tuple[str, str]:
    """Apply a caller's minimum tier. It can only raise the rule floor, never lower it."""
    if not min_tier or tier_rank(min_tier) <= tier_rank(floor):
        return floor, reason
    return min_tier, f"minimum tier {min_tier} requested by the caller ({reason})"


def _command(body: str):
    first = (body or "").strip().splitlines()[0] if (body or "").strip() else ""
    return COMMAND_RE.match(first)


def parse_command(body: str) -> tuple[bool, str | None]:
    """`/review [fresh] [<tier>]` on the comment's first line -> (is_command, tier or None)."""
    m = _command(body)
    if not m:
        return False, None
    return True, (m.group(2) or "").lower() or None


def wants_fresh(body: str) -> bool:
    """`/review fresh ...`: review the whole PR, not just what changed since the last review."""
    m = _command(body)
    return bool(m and m.group(1))


def label_override(labels: list[str]) -> str | None:
    """Highest `review:<tier>` label on the PR, if any."""
    found = [
        name[len(LABEL_PREFIX):].lower()
        for name in labels
        if name.lower().startswith(LABEL_PREFIX) and name[len(LABEL_PREFIX):].lower() in TIERS
    ]
    return max_tier(*found) if found else None


def combine(floor: str, floor_reason: str, triage: tuple[str, str] | None) -> TierDecision:
    if triage is None:
        return TierDecision(floor, "rules", floor_reason)
    tier, reason = triage
    if tier not in TIERS:
        return TierDecision(floor, "rules", floor_reason)
    final = max_tier(floor, tier)
    if final == floor:
        return TierDecision(floor, "rules", floor_reason)
    return TierDecision(final, "triage", reason)
