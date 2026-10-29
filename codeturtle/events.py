"""Decide from the GitHub event whether to review, which PR, and whether a tier was requested."""

from __future__ import annotations

from dataclasses import dataclass

from .tiers import parse_command

TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
PR_ACTIONS = {"opened", "reopened", "ready_for_review"}


@dataclass
class Request:
    repo: str
    number: int
    trigger: str  # pull_request | comment | dispatch | cli
    override: str | None = None
    comment_id: int | None = None


def from_event(name: str, event: dict, repo: str, tier_input: str = "", pr_input: str = "") -> tuple[Request | None, str]:
    """Returns (request, "") to review, or (None, reason) to skip."""
    tier_input = (tier_input or "").strip().lower() or None
    if name in ("pull_request", "pull_request_target"):
        if event.get("action") not in PR_ACTIONS:
            return None, f"pull_request action {event.get('action')!r} is not reviewed"
        pr = event["pull_request"]
        if pr.get("draft"):
            return None, "draft PR (comment /review to review it anyway)"
        return Request(repo, int(pr["number"]), "pull_request", tier_input), ""

    if name == "issue_comment":
        issue, comment = event.get("issue", {}), event.get("comment", {})
        if event.get("action") != "created" or issue.get("pull_request") is None:
            return None, "not a new comment on a pull request"
        is_command, tier = parse_command(comment.get("body", ""))
        if not is_command:
            return None, "comment is not a /review command"
        if comment.get("author_association") not in TRUSTED_ASSOCIATIONS:
            return None, f"/review from {comment.get('author_association')} is ignored (owner/member/collaborator only)"
        return Request(repo, int(issue["number"]), "comment", tier or tier_input, int(comment["id"])), ""

    if name == "workflow_dispatch":
        inputs = event.get("inputs") or {}
        number = pr_input or inputs.get("pr")
        if not number:
            return None, "workflow_dispatch needs a `pr` input"
        return Request(repo, int(number), "dispatch", (inputs.get("tier") or "").lower() or tier_input), ""

    return None, f"event {name!r} is not handled"
