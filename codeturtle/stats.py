"""How often codeturtle's findings get addressed: a way to measure prompt, tier and model changes.

    python3 -m codeturtle stats --repo owner/name [--repo ...] [--prs 50] [--json]

It reads the inline comment threads codeturtle posted on the most recent PRs. A finding counts as
addressed when its line changed in a later commit (GitHub marks the thread outdated) or its thread
was resolved. That is a proxy: a changed line is usually, not always, a fix. Findings posted only in
the review body have no thread and are not counted.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict

from . import github

BOT_LOGINS = {"github-actions", "github-actions[bot]"}
COMMENT_RE = re.compile(r"^\S+ \*\*(Blocker|Major|Minor|Nit)\*\* · ([\w-]+) — \*\*(.+?)\*\*", re.S)
TIER_RE = re.compile(r"### 🐢 codeturtle · (Fast|Normal|Full) (?:follow-up )?review")
VERSION_RE = re.compile(r"codeturtle v(\d+\.\d+\.\d+)")
SEVERITIES = ["blocker", "major", "minor", "nit"]

QUERY = """
query($owner: String!, $name: String!, $prs: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequests(last: $prs) {
      nodes {
        number
        state
        reviewThreads(first: 100) {
          nodes {
            isResolved
            isOutdated
            comments(first: 1) { nodes { author { login } body pullRequestReview { body } } }
          }
        }
      }
    }
  }
}
"""


def parse(thread: dict, repo: str, pr: dict) -> dict | None:
    """One codeturtle finding from a review thread, or None for anyone else's thread."""
    comments = ((thread.get("comments") or {}).get("nodes") or [])
    if not comments:
        return None
    first = comments[0]
    if ((first.get("author") or {}).get("login") or "") not in BOT_LOGINS:
        return None
    m = COMMENT_RE.match(first.get("body") or "")
    if not m:
        return None
    review = ((first.get("pullRequestReview") or {}).get("body") or "")
    tier, version = TIER_RE.search(review), VERSION_RE.search(review)
    return {
        "repo": repo, "pr": pr.get("number"), "pr_state": (pr.get("state") or "").lower(),
        "severity": m.group(1).lower(), "category": m.group(2).lower(), "title": m.group(3),
        "tier": tier.group(1).lower() if tier else "unknown", "version": version.group(1) if version else "unknown",
        "addressed": bool(thread.get("isOutdated") or thread.get("isResolved")),
    }


def fetch(repo: str, prs: int) -> list[dict]:
    owner, name = repo.split("/", 1)
    data = github.graphql(QUERY, owner=owner, name=name, prs=prs)
    out = []
    for pr in (((data.get("repository") or {}).get("pullRequests") or {}).get("nodes") or []):
        for thread in ((pr.get("reviewThreads") or {}).get("nodes") or []):
            record = parse(thread, repo, pr)
            if record:
                out.append(record)
    return out


def aggregate(records: list[dict]) -> dict:
    def tally(key):
        groups = defaultdict(lambda: [0, 0])
        for r in records:
            groups[r[key]][0] += 1
            groups[r[key]][1] += r["addressed"]
        order = SEVERITIES if key == "severity" else sorted(groups)
        return [{key: k, "posted": groups[k][0], "addressed": groups[k][1]} for k in order if k in groups]

    return {
        "posted": len(records),
        "addressed": sum(r["addressed"] for r in records),
        "prs": len({(r["repo"], r["pr"]) for r in records}),
        "by_severity": tally("severity"),
        "by_category": tally("category"),
        "by_tier": tally("tier"),
        "by_version": tally("version"),
        "by_repo": tally("repo"),
    }


def pct(part: int, whole: int) -> str:
    return f"{round(100 * part / whole)}%" if whole else "-"


def render(agg: dict, repos: list[str], prs: int) -> str:
    lines = [
        f"codeturtle findings on the last {prs} PRs of {', '.join(repos)}",
        f"Inline findings: {agg['posted']} on {agg['prs']} PRs · addressed {agg['addressed']} "
        f"({pct(agg['addressed'], agg['posted'])})",
        "Addressed = the line changed in a later commit or the thread was resolved (a proxy for fixed).",
    ]
    for title, key in (("By severity", "severity"), ("By category", "category"), ("By tier", "tier"),
                       ("By codeturtle version", "version"), ("By repository", "repo")):
        rows = agg[f"by_{key}"]
        if not rows:
            continue
        width = max(len(str(r[key])) for r in rows)
        lines += ["", f"{title}:"]
        for r in rows:
            lines.append(f"  {str(r[key]):<{width}}  {r['posted']:>5} posted  {r['addressed']:>5} addressed "
                         f"({pct(r['addressed'], r['posted'])})")
    return "\n".join(lines)


def main(repos: list[str], prs: int = 50, as_json: bool = False, out=print) -> int:
    records = []
    for repo in repos:
        records += fetch(repo, prs)
    agg = aggregate(records)
    out(json.dumps(agg, indent=2) if as_json else render(agg, repos, prs))
    return 0
