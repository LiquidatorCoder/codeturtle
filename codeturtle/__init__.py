"""codeturtle: AI pull request review with a Codex -> Grok -> Claude fallback chain."""

__version__ = "1.1.0"

# Version of the machine-readable result (`codeturtle-result-attempt-<n>` artifact); see result.py.
RESULT_SCHEMA_VERSION = 1

TIERS = ("fast", "normal", "full")


def tier_rank(tier: str) -> int:
    return TIERS.index(tier)


def max_tier(*tiers: str) -> str:
    return max(tiers, key=tier_rank)
