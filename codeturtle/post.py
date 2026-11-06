"""Turn model findings into one GitHub PR review: inline comments where GitHub allows them,
everything else in the review body. Secrets are redacted from anything posted."""

from __future__ import annotations

import re

from . import __version__
from .diff import FileChange

SEVERITIES = ["blocker", "major", "minor", "nit"]
ICON = {"blocker": "🛑", "major": "🔴", "minor": "🟡", "nit": "⚪"}
ID_RE = re.compile(r"\Act-[0-9a-f]{12}(?:-\d+)?\Z")

SECRET_RES = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"\b(?:ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bxai-[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    # 1.1: the result artifact is machine-read too, so also catch the shapes of the runner's own
    # login files (OAuth refresh tokens, `"access_token": "..."`, `password=...`) a prompt-injected
    # model could be asked to quote.
    re.compile(r"\b(?:rt|ort|sk-ant-oat01|sk-ant-ort01)[-_][A-Za-z0-9_.-]{20,}"),
    re.compile(r"(?i)([\"']?(?:access|refresh|id|oauth|auth|api)[_-]?(?:token|key)|client[_-]?secret|password|passwd|secret)([\"']?\s*[:=]\s*[\"']?)[^\s\"',}]{12,}"),
]


def redact(text: str) -> str:
    for pattern in SECRET_RES:
        if pattern.groups >= 2:
            text = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}[redacted]", text)
        else:
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
        if isinstance(f.get("id"), str) and ID_RE.match(f["id"]):
            item["id"] = f["id"]  # carried from an earlier review
        key = (item["path"], item["line"], item["title"].lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    out.sort(key=lambda f: (SEVERITIES.index(f["severity"]), f["path"], f["line"]))
    return out


def comment_body(f: dict) -> str:
    text = redact(f"{ICON[f['severity']]} **{f['severity'].capitalize()}** · {f['category']} — **{f['title']}**\n\n{f['body']}")
    if f.get("id"):  # lets a follow-up review find and resolve this thread
        text += f"\n\n<!-- codeturtle:finding {f['id']} -->"
    return text


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
    security=None,  # security.Report: the security check's summary line and leads
    verification=None,  # verify.Report: what verification did to the blockers and majors
    followup=None,  # followup.Plan: fixed, still open and new since the last review
) -> str:
    extra = [line for line in (followup.summary_line() if followup else None,
                               security.summary_line() if security else None,
                               verification.summary_line() if verification else None) if line]
    lines = [
        f"### 🐢 codeturtle · {tier.capitalize()} {'follow-up ' if followup else ''}review",
        "",
        summary.strip() or "_No summary returned._",
        "",
        f"**Tier:** {tier.capitalize()} — {tier_reason}  ",
        f"**Reviewed by:** {reviewers}  ",
        f"**Findings:** {counts(findings)}" + (f" ({len(inline)} inline)" if findings else ""),
    ]
    for line in extra:
        lines[-1] += "  "  # a Markdown line break before the next status line
        lines.append(line)
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
    leads = list(getattr(security, "leads", None) or []) + list(getattr(verification, "leads", None) or [])
    if leads:
        lines += ["", f"<details><summary>Need validation ({len(leads)}): not findings and no severity; "
                      "each hangs on one fact outside the code</summary>", ""]
        for lead in leads:
            where = f"`{lead['path']}:{lead['line']}`" if lead["line"] else f"`{lead['path'] or 'general'}`"
            lines.append(f"- 🔍 **{lead['title']}** — {where} · {lead['attack_class']}  ")
            lines.append("  Check: " + lead["unresolved"].replace("\n", " "))
        lines += ["", "</details>"]
    if followup and followup.open:
        lines += ["", f"<details><summary>Still open from earlier reviews ({len(followup.open)})</summary>", ""]
        for f in followup.open:
            where = f"`{f['path']}:{f['line']}`" if f["line"] else f"`{f['path'] or 'general'}`"
            lines.append(f"- {ICON[f['severity']]} **{f['title']}** — {where}")
        lines += ["", "</details>"]
    done = (followup.fixed + followup.obsolete) if followup else []
    if done:
        lines += ["", f"<details><summary>Fixed or no longer applying since `{followup.since[:7]}` ({len(done)})"
                      "</summary>", ""]
        for f in done:
            lines.append(f"- ✅ **{f['title']}** — `{f['path']}`" + (f": {f['note']}" if f.get("note") else ""))
        lines += ["", "</details>"]
    for note in notes:
        lines += ["", f"> {note}"]
    lines += [
        "",
        f"<sub>Commit {head_sha[:7]} · re-run with `/review`, `/review fast|normal|full` or `/review fresh` "
        f"(the whole PR) · codeturtle v{__version__}</sub>",
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
