"""Turn model findings into one GitHub PR review: inline comments where GitHub allows them,
everything else in the review body. Secrets are redacted from anything posted."""

from __future__ import annotations

import re

from . import __version__
from .diff import FileChange

SEVERITIES = ["blocker", "major", "minor", "nit"]
ICON = {"blocker": "🛑", "major": "🔴", "minor": "🟡", "nit": "⚪"}

SECRET_RES = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"\b(?:ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bxai-[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
]


def redact(text: str) -> str:
    for pattern in SECRET_RES:
        text = pattern.sub("[redacted]", text)
    return text


def normalize(findings: list, tier: str) -> list[dict]:
    """Drop malformed entries, apply the tier's severity cut, de-duplicate and sort."""
    out, seen = [], set()
    for f in findings:
        if not isinstance(f, dict):
            continue
        severity = str(f.get("severity", "")).lower()
        if severity not in SEVERITIES or not str(f.get("title", "")).strip():
            continue
        if tier == "fast" and severity not in ("blocker", "major"):
            continue
        try:
            line = max(0, int(f.get("line") or 0))
        except (TypeError, ValueError):
            line = 0
        path = str(f.get("path") or "").strip()
        if path.startswith("./"):
            path = path[2:]
        item = {
            "severity": severity,
            "category": str(f.get("category") or "other"),
            "path": path,
            "line": line,
            "title": str(f["title"]).strip(),
            "body": str(f.get("body") or "").strip(),
        }
        key = (item["path"], item["line"], item["title"].lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    out.sort(key=lambda f: (SEVERITIES.index(f["severity"]), f["path"], f["line"]))
    return out


def comment_body(f: dict) -> str:
    return redact(f"{ICON[f['severity']]} **{f['severity'].capitalize()}** · {f['category']} — **{f['title']}**\n\n{f['body']}")


def split_inline(findings: list[dict], files: list[FileChange], max_inline: int) -> tuple[list[dict], list[dict]]:
    by_path = {f.path: f for f in files}
    inline, rest = [], []
    for f in findings:
        fc = by_path.get(f["path"])
        if fc and f["line"] in fc.commentable_lines and len(inline) < max_inline:
            inline.append(f)
        else:
            rest.append(f)
    return inline, rest


def counts(findings: list[dict]) -> str:
    return " · ".join(f"{ICON[s]} {sum(f['severity'] == s for f in findings)} {s}" for s in SEVERITIES)


def build_body(
    *,
    tier: str,
    tier_reason: str,
    reviewers: str,
    summary: str,
    findings: list[dict],
    inline: list[dict],
    rest: list[dict],
    head_sha: str,
    notes: list[str],
) -> str:
    lines = [
        f"### 🐢 codeturtle · {tier.capitalize()} review",
        "",
        summary.strip() or "_No summary returned._",
        "",
        f"**Tier:** {tier.capitalize()} — {tier_reason}  ",
        f"**Reviewed by:** {reviewers}  ",
        f"**Findings:** {counts(findings)}" + (f" ({len(inline)} inline)" if findings else ""),
    ]
    if not findings:
        lines += ["", "No issues found at this depth."]
    if rest:
        lines += ["", f"<details open><summary>Not attached to a diff line ({len(rest)})</summary>", ""]
        for f in rest:
            where = f"`{f['path']}:{f['line']}`" if f["line"] else f"`{f['path'] or 'general'}`"
            lines.append(f"- {ICON[f['severity']]} **{f['title']}** — {where}  ")
            if f["body"]:
                lines.append("  " + f["body"].replace("\n", "\n  "))
        lines += ["", "</details>"]
    for note in notes:
        lines += ["", f"> {note}"]
    lines += [
        "",
        f"<sub>Commit {head_sha[:7]} · re-run with `/review` or `/review fast|normal|full` · codeturtle v{__version__}</sub>",
    ]
    return redact("\n".join(lines))


def build_payload(*, head_sha: str, body: str, inline: list[dict]) -> dict:
    return {
        "commit_id": head_sha,
        "event": "COMMENT",
        "body": body,
        "comments": [
            {"path": f["path"], "line": f["line"], "side": "RIGHT", "body": comment_body(f)} for f in inline
        ],
    }
