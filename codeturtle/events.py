"""Decide from the GitHub event whether to review, which PR, and whether a tier was requested."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .tiers import parse_command

TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
PR_ACTIONS = {"opened", "reopened", "ready_for_review"}
# Opaque correlation id a caller (e.g. the harness) passes to workflow_dispatch; echoed in the result.
REQUEST_ID_RE = re.compile(r"\A[A-Za-z0-9._:-]{1,128}\Z")
# A PR description may ask for a deeper automatic review (never a shallower one):
#   <!-- codeturtle:min-tier=full -->
MIN_TIER_MARKER_RE = re.compile(r"<!--\s*codeturtle:min-tier=(fast|normal|full)\s*-->", re.IGNORECASE)


@dataclass
class Request:
    repo: str
    number: int
    trigger: str  # pull_request | comment | dispatch | cli
    override: str | None = None
    comment_id: int | None = None
    min_tier: str | None = None  # floor raise: max(rules floor, min_tier); never lowers anything
    request_id: str | None = None


class BadRequest(ValueError):
    """The trigger asked for something invalid (unknown tier, malformed request id)."""


def _clean_min_tier(value: str | None) -> str | None:
    value = (value or "").strip().lower() or None
    if value is not None and value not in ("fast", "normal", "full"):
        raise BadRequest(f"unknown min_tier {value!r}; expected fast, normal or full")
    return value


def _clean_request_id(value: str | None) -> str | None:
    value = (value or "").strip() or None
    if value is not None and not REQUEST_ID_RE.match(value):
        raise BadRequest("request_id must be 1-128 characters of A-Z a-z 0-9 . _ : -")
    return value


def from_event(
    name: str, event: dict, repo: str, tier_input: str = "", pr_input: str = "",
    min_tier_input: str = "", request_id_input: str = "",
) -> tuple[Request | None, str]:
    """Returns (request, "") to review, or (None, reason) to skip. Raises BadRequest on bad inputs."""
    tier_input = (tier_input or "").strip().lower() or None
    min_tier_input = _clean_min_tier(min_tier_input)
    request_id_input = _clean_request_id(request_id_input)
    if name in ("pull_request", "pull_request_target"):
        if event.get("action") not in PR_ACTIONS:
            return None, f"pull_request action {event.get('action')!r} is not reviewed"
        pr = event["pull_request"]
        if pr.get("draft"):
            return None, "draft PR (comment /review to review it anyway)"
        marker = MIN_TIER_MARKER_RE.search(pr.get("body") or "")
        min_tier = min_tier_input or (marker.group(1).lower() if marker else None)
        return Request(repo, int(pr["number"]), "pull_request", tier_input, min_tier=min_tier,
                       request_id=request_id_input), ""

    if name == "issue_comment":
        issue, comment = event.get("issue", {}), event.get("comment", {})
        if event.get("action") != "created" or issue.get("pull_request") is None:
            return None, "not a new comment on a pull request"
        is_command, tier = parse_command(comment.get("body", ""))
        if not is_command:
            return None, "comment is not a /review command"
        if comment.get("author_association") not in TRUSTED_ASSOCIATIONS:
            return None, f"/review from {comment.get('author_association')} is ignored (owner/member/collaborator only)"
        return Request(repo, int(issue["number"]), "comment", tier or tier_input, int(comment["id"]),
                       min_tier=min_tier_input, request_id=request_id_input), ""

    if name == "workflow_dispatch":
        inputs = event.get("inputs") or {}
        number = pr_input or inputs.get("pr")
        if not number:
            return None, "workflow_dispatch needs a `pr` input"
        return Request(
            repo, int(number), "dispatch", (inputs.get("tier") or "").lower() or tier_input,
            min_tier=_clean_min_tier(inputs.get("min_tier")) or min_tier_input,
            request_id=_clean_request_id(inputs.get("request_id")) or request_id_input,
        ), ""

    return None, f"event {name!r} is not handled"
