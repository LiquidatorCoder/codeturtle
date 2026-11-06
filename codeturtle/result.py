"""The machine-readable review result, uploaded as the `codeturtle-result-attempt-<n>` artifact.

Human readers get the PR review and its inline comments. Tools (such as the Claude Code harness)
read this file instead, after checking through the GitHub API that the artifact belongs to a
completed run of the repository's own `.github/workflows/codeturtle.yml` on its default branch.

Everything here is redacted like the posted review. It never holds a token, an environment
variable, a prompt or model reasoning: only what the review already shows on the PR, plus ids.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from . import RESULT_SCHEMA_VERSION, __version__
from .post import SEVERITIES, redact

RESULT_ENV = "CODETURTLE_RESULT_PATH"


def iso(ts: float | None) -> str | None:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)) if ts else None


def finding_id(f: dict) -> str:
    """Stable across reruns of the same finding: path, line, severity and title."""
    key = "\0".join([f.get("path", ""), str(f.get("line", 0)), f.get("severity", ""), f.get("title", "").strip().lower()])
    return "ct-" + hashlib.sha256(key.encode()).hexdigest()[:12]


def with_ids(findings: list[dict], inline: list[dict]) -> list[dict]:
    inline_ids = {id(f) for f in inline}
    out, seen = [], set()
    for f in findings:
        fid = finding_id(f)
        n = 2
        base = fid
        while fid in seen:  # same key twice cannot survive post.normalize, but never collide
            fid, n = f"{base}-{n}", n + 1
        seen.add(fid)
        out.append({
            "id": fid,
            "severity": f["severity"],
            "category": f["category"],
            "path": f["path"],
            "line": f["line"],
            "title": redact(f["title"]),
            "body": redact(f["body"]),
            "placement": "inline" if id(f) in inline_ids else "body",
        })
    return out


def run_identity() -> dict:
    env = os.environ
    def num(name):
        try:
            return int(env.get(name, ""))
        except ValueError:
            return None
    return {
        "id": num("GITHUB_RUN_ID"),
        "attempt": num("GITHUB_RUN_ATTEMPT"),
        "event": env.get("GITHUB_EVENT_NAME") or None,
        "workflow_ref": env.get("GITHUB_WORKFLOW_REF") or None,
        "workflow_sha": env.get("GITHUB_WORKFLOW_SHA") or None,
        "actor": env.get("GITHUB_ACTOR") or None,
        "repository_id": num("GITHUB_REPOSITORY_ID"),
    }


class ResultWriter:
    """Collects the review's outcome as it happens; `write()` is called once, on every exit path."""

    def __init__(self, req):
        self.req = req
        self.started = time.time()
        self.pr: dict | None = None
        self.tier: dict | None = None
        self.summary = ""
        self.findings: list[dict] = []
        self.notes: list[str] = []
        self.review: dict | None = None
        self.reviewers: dict = {"providers_used": [], "models": {}, "usage_limited": [], "calls": 0}
        self.retry_at: str | None = None  # status usage_limited: earliest reported limit reset
        self.security: dict | None = None  # 1.3: what the security check did, incl. needs_validation leads
        self.verification: dict | None = None  # 1.4: what verification did to the blockers and majors

    def set_pr(self, raw: dict) -> None:
        base, head = raw.get("base") or {}, raw.get("head") or {}
        self.pr = {
            "number": self.req.number,
            "base_ref": base.get("ref"),
            "base_sha": base.get("sha"),
            "head_ref": head.get("ref"),
            "head_sha": head.get("sha"),
        }

    def payload(self, status: str, error: str | None = None, skip_reason: str | None = None) -> dict:
        counts = {s: sum(f["severity"] == s for f in self.findings) for s in SEVERITIES}
        return {
            "schema": "codeturtle.result",
            "schema_version": RESULT_SCHEMA_VERSION,
            "codeturtle_version": __version__,
            "repository": self.req.repo,
            "pr": self.pr or {"number": self.req.number},
            "run": run_identity(),
            "trigger": self.req.trigger,
            "request_id": self.req.request_id,
            "min_tier": self.req.min_tier,
            "status": status,  # completed | failed | usage_limited | skipped
            "error": redact(error)[:1000] if error else None,
            "skip_reason": skip_reason,
            "retry_at": self.retry_at,
            "tier": self.tier,
            "reviewers": self.reviewers,
            "summary": redact(self.summary),
            "findings": self.findings,
            "findings_total": len(self.findings),
            "counts": counts,
            "notes": [redact(n) for n in self.notes],
            "security": self.security if status == "completed" else None,
            "verification": self.verification if status == "completed" else None,
            "review": self.review,
            "started_at": iso(self.started),
            "finished_at": iso(time.time()),
        }

    def write(self, status: str, error: str | None = None, skip_reason: str | None = None) -> Path | None:
        target = os.environ.get(RESULT_ENV)
        if not target:
            return None
        path = Path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.payload(status, error, skip_reason), indent=2) + "\n")
        tmp.replace(path)
        return path
