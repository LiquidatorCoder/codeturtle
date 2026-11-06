"""Review settings: built-in defaults, deep-merged with the repo's `.github/codeturtle.json`.

The repo config is always read from the PR's *base* branch, so a PR cannot loosen its own rules.
"""

from __future__ import annotations

import copy
import functools
import re

CONFIG_PATH = ".github/codeturtle.json"

DEFAULTS: dict = {
    # Tried in order; the next one is used only when the previous one hits its usage limit.
    "providers": ["codex", "grok", "claude"],
    "models": {"codex": "gpt-6-sol", "grok": "grok-4.7", "claude": "claude-opus-5-5"},
    # The merge step only de-duplicates, so it runs at low effort.
    "effort": {"triage": "low", "fast": "low", "normal": "medium", "full": "high", "merge": "low", "verify": "medium"},
    "timeout_minutes": {"triage": 5, "fast": 10, "normal": 20, "full": 40, "merge": 10, "verify": 15},
    # Review only what changed since the last review of the same PR, re-check its findings, and
    # resolve the threads of fixed ones (followup.py). `/review fresh` reviews the whole PR.
    "incremental": True,
    # Before posting, an independent call re-reads the code for these findings and drops or
    # downgrades any it can't confirm (verify.py). Empty list: post everything as written.
    "verify": {
        "severities": ["blocker", "major"],
        # Prefer a provider other than the one that wrote the findings.
        "different_provider": True,
    },
    # Security on every review (security.py, adapted from Cloudflare's security-audit skill).
    "security": {
        # "auto": a dedicated security pass on every review except docs- or assets-only changes;
        # "always"; or "off" (the main review still applies the security bar).
        "pass": "auto",
        # A fresh call tries to refute each security finding before it is posted.
        "verify": True,
        # The repository's own threat model or security rules, read from the base branch.
        "rules_files": [".github/codeturtle-security.md"],
        # Domain checks (AI, web and auth, CI, cloud, data, mobile, ...) picked from the changed files.
        "max_companions": 4,
    },
    # When every provider is out and one says its usage limit resets within this many minutes,
    # wait for it instead of failing the review. 0 fails at once.
    "limit_wait_minutes": 30,
    "tiers": {
        # Any changed file matching these forces a full review.
        "full_paths": [
            "**/auth/**",
            "**/security/**",
            "**/payment*/**",
            "**/billing/**",
            "**/migrations/**",
            "**/*.sql",
            ".github/workflows/**",
            "**/Dockerfile",
            "**/*.entitlements",
            "**/Info.plist",
            "**/AndroidManifest.xml",
        ],
        # A PR whose changed files ALL match these gets at least a fast review.
        "fast_paths": ["**/*.md", "**/*.txt", "docs/**", "**/*.png", "**/*.jpg", "**/*.svg", "LICENSE*"],
        # Tests and fixtures: reviewed, but they don't count toward the size thresholds below.
        "test_paths": [
            "**/test/**", "**/tests/**", "**/__tests__/**", "**/spec/**", "**/integration_test/**",
            "**/testdata/**", "**/fixtures/**", "**/__snapshots__/**", "**/*_test.*", "**/*.test.*",
            "**/*.spec.*", "**/*_spec.*", "**/test_*.py", "**/*Tests.swift", "**/*Test.swift",
            "**/*Test.kt", "**/*Tests.kt", "**/*Test.java", "**/*UITests/**", "**/RunnerTests/**",
        ],
        # Changed production lines (not tests, docs, assets or generated files).
        "full_if_lines_over": 800,
        "fast_if_lines_under": 40,
        # When a full review is due only to sensitive paths, those files get the full passes and
        # the rest one normal-depth pass, instead of three high-effort passes over everything.
        "split_full": True,
    },
    # Left out of the diff the models see (still counted in stats and listed by name).
    "ignore_paths": [
        "**/*.lock",
        "**/package-lock.json",
        "**/pnpm-lock.yaml",
        "**/yarn.lock",
        "**/Podfile.lock",
        "**/*.g.dart",
        "**/*.freezed.dart",
        "**/*.mocks.dart",
        "**/*.pb.go",
        "**/*.min.js",
        "**/*.snap",
    ],
    # Read from the base branch and given to the models as repository review guidelines.
    "guideline_files": [".github/codeturtle.md", "AGENTS.md", "CLAUDE.md"],
    "max_inline_comments": 25,
    # Full-tier passes (or diff parts) run at the same time; 1 runs them one after another.
    "full_parallel": 3,
    # Characters of annotated diff per model call; larger diffs are split into chunks (full) or cut.
    "diff_budget_chars": 240_000,
    "context_budget_chars": 120_000,
}


def merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge(out[key], value)
        else:
            out[key] = value
    return out


@functools.lru_cache(maxsize=512)
def _glob_regex(pattern: str) -> re.Pattern:
    """Gitignore-style glob: `**/` = any directories (incl. none), `*` = within one segment."""
    i, out = 0, []
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def glob_match(path: str, pattern: str) -> bool:
    if "/" not in pattern:
        # A bare pattern such as `LICENSE*` matches the file name at any depth.
        return bool(_glob_regex(pattern).match(path.rsplit("/", 1)[-1]))
    return bool(_glob_regex(pattern).match(path))


def matches_any(path: str, patterns: list[str]) -> bool:
    return any(glob_match(path, p) for p in patterns)
