"""Tier selection: a rule-based floor that a triage model may raise but never lower."""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import TIERS, max_tier
from .config import matches_any
from .diff import FileChange

COMMAND_RE = re.compile(r"^\s*/review(?:\s+(fast|normal|full))?\s*$", re.IGNORECASE)
LABEL_PREFIX = "review:"


@dataclass
class TierDecision:
    tier: str
    source: str  # "override", "rules", "triage"
    reason: str


def floor_tier(files: list[FileChange], rules: dict) -> tuple[str, str]:
    """Lowest tier this change may get, from paths and size alone (no model involved)."""
    reviewed = [f for f in files if not f.ignored]
    lines = sum(f.changes for f in reviewed)

    risky = [
        f.path
        for f in files
        if matches_any(f.path, rules["full_paths"])
        or (f.previous_path and matches_any(f.previous_path, rules["full_paths"]))
    ]
    if risky:
        shown = ", ".join(risky[:3]) + (f" and {len(risky) - 3} more" if len(risky) > 3 else "")
        return "full", f"touches sensitive paths ({shown})"
    if lines > rules["full_if_lines_over"]:
        return "full", f"{lines} changed lines (over {rules['full_if_lines_over']})"
    if not reviewed:
        return "fast", "only generated or lock files changed"
    if all(matches_any(f.path, rules["fast_paths"]) for f in reviewed):
        return "fast", "only docs or assets changed"
    if lines < rules["fast_if_lines_under"]:
        return "fast", f"small change ({lines} lines)"
    return "normal", f"{lines} changed lines across {len(reviewed)} files"


def parse_command(body: str) -> tuple[bool, str | None]:
    """`/review` or `/review <tier>` on the comment's first line -> (is_command, tier or None)."""
    first = (body or "").strip().splitlines()[0] if (body or "").strip() else ""
    m = COMMAND_RE.match(first)
    if not m:
        return False, None
    return True, (m.group(1) or "").lower() or None


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
