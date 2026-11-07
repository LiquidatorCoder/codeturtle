"""codeturtle: AI pull request review with a Codex -> Grok -> Claude fallback chain."""

import os
from pathlib import Path

__version__ = "1.5.0"

# Version of the machine-readable result (`codeturtle-result-attempt-<n>` artifact); see result.py.
# New fields are added without bumping it; a bump means an existing field changed meaning.
RESULT_SCHEMA_VERSION = 1

TIERS = ("fast", "normal", "full")


def tier_rank(tier: str) -> int:
    return TIERS.index(tier)


def max_tier(*tiers: str) -> str:
    return max(tiers, key=tier_rank)


def state_dir() -> Path:
    """Runner-local state: review slots, remembered usage limits and the Claude token file."""
    return Path(os.environ.get("CODETURTLE_HOME") or "~/.codeturtle").expanduser()
