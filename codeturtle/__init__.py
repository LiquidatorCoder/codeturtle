"""codeturtle: AI pull request review with a Codex -> Grok -> Claude fallback chain."""

__version__ = "1.0.0"

TIERS = ("fast", "normal", "full")


def tier_rank(tier: str) -> int:
    return TIERS.index(tier)


def max_tier(*tiers: str) -> str:
    return max(tiers, key=tier_rank)
