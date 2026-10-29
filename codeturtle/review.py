"""One end-to-end review: fetch the PR, pick the tier, run the model passes, post the review."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from . import github, post, prompts
from . import max_tier
from .config import CONFIG_PATH, DEFAULTS, matches_any, merge
from .diff import FileChange, chunk_files, render_diff
from .events import Request
from .progress import Progress
from .providers import LABELS, AllLimited, Call, Chain, ProviderError, kill_all
from .result import ResultWriter, with_ids
from .tiers import TierDecision, combine, floor_tier, label_override, raise_floor

AGENT_CONFIG_DIRS = {".claude", ".codex", ".grok", ".cursor", ".agents", ".gemini", ".windsurf"}
AGENT_CONFIG_FILES = {
    "AGENTS.md", "AGENTS.override.md", "CLAUDE.md", "CLAUDE.local.md", "GROK.md", "GEMINI.md",
    ".mcp.json", ".cursorrules",
}
SKIP_LABEL = "codeturtle:skip"


def log(msg: str) -> None:
    print(msg, flush=True)


@contextlib.contextmanager
def group(title: str):
    in_actions = os.environ.get("GITHUB_ACTIONS") == "true"
    log(f"::group::{title}" if in_actions else f"== {title}")
    try:
        yield
    finally:
        if in_actions:
            log("::endgroup::")


@contextlib.contextmanager
def review_slot(max_parallel: int, wait_minutes: int, on_wait=None):
    """At most `max_parallel` reviews at once on this Mac, across every repo's runners."""
    root = Path("~/.codeturtle/slots").expanduser()
    root.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + wait_minutes * 60
    announced = False
    while True:
        for i in range(max_parallel):
            slot = root / f"slot-{i}"
            try:
                slot.mkdir()
            except FileExistsError:
                if _slot_is_stale(slot):
                    shutil.rmtree(slot, ignore_errors=True)
                continue
            (slot / "pid").write_text(str(os.getpid()))
            try:
                yield
            finally:
                shutil.rmtree(slot, ignore_errors=True)
            return
        if time.time() > deadline:
            raise ProviderError(f"waited {wait_minutes} min for a free review slot")
        if not announced:
            log("Waiting for another codeturtle review on this Mac to finish...")
            if on_wait:
                on_wait()
            announced = True
        time.sleep(10)


def _slot_is_stale(slot: Path) -> bool:
    try:
        pid = int((slot / "pid").read_text())
    except FileNotFoundError:
        return time.time() - slot.stat().st_mtime > 60  # pid not written yet, or a crashed writer
    except ValueError:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def sanitize_snapshot(root: Path) -> None:
    """Remove agent config from the PR's tree so a PR cannot steer the reviewer through it."""
    for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path.is_symlink():
            path.unlink()
        elif path.is_dir() and path.name in AGENT_CONFIG_DIRS:
            shutil.rmtree(path, ignore_errors=True)
        elif path.is_file() and path.name in AGENT_CONFIG_FILES:
            path.unlink()


@dataclass
class Outcome:
    summary: str
    findings: list
    notes: list[str] = field(default_factory=list)


class Reviewer:
    def __init__(self, req: Request, providers: list[str] | None = None, dry_run: bool = False):
        self.req = req
        self.dry_run = dry_run
        self.providers_override = providers
        self.cfg = DEFAULTS
        self.chain: Chain | None = None
        self.progress = Progress(req.repo, req.number, enabled=not dry_run, log=log)
        self.result = ResultWriter(req)

    # ---- setup -------------------------------------------------------------------------------

    def load(self) -> str | None:
        """Fetch PR, config and files. Returns a skip reason, or None to continue."""
        req = self.req
        self.pr_raw = github.get_pr(req.repo, req.number)
        pr = self.pr_raw
        self.result.set_pr(pr)
        head_repo = (pr.get("head") or {}).get("repo") or {}
        if head_repo.get("full_name") != req.repo:
            return "PR comes from a fork; codeturtle never reviews fork code on a self-hosted runner"
        if pr.get("state") != "open":
            return "PR is not open"
        labels = [label["name"] for label in pr.get("labels", [])]
        if SKIP_LABEL in labels and req.trigger == "pull_request":
            return f"PR has the {SKIP_LABEL} label"

        base_sha = pr["base"]["sha"]
        raw_cfg = github.file_at_ref(req.repo, CONFIG_PATH, base_sha)
        if raw_cfg:
            try:
                self.cfg = merge(DEFAULTS, json.loads(raw_cfg))
            except json.JSONDecodeError as e:
                raise ProviderError(f"{CONFIG_PATH} on the base branch is not valid JSON: {e}")
        providers = self.providers_override or self.cfg["providers"]
        self.chain = Chain(
            providers, self.cfg["models"], log=log,
            on_attempt=self.progress.begin,
            on_limit=lambda name: self.progress.note(f"{name} hit its usage limit, so the next reviewer took over."),
        )

        self.head_sha = pr["head"]["sha"]
        self.pr = {
            "repo": req.repo,
            "number": req.number,
            "title": pr.get("title") or "",
            "body": pr.get("body") or "",
            "author": (pr.get("user") or {}).get("login", "unknown"),
        }
        self.files = [FileChange.from_api(item) for item in github.get_files(req.repo, req.number)]
        for f in self.files:
            f.ignored = matches_any(f.path, self.cfg["ignore_paths"])
        self.labels = labels
        self.guidelines = {}
        for path in self.cfg["guideline_files"]:
            text = github.file_at_ref(req.repo, path, base_sha)
            if text and text.strip():
                self.guidelines[path] = text[:20_000]
        return None

    # ---- tier --------------------------------------------------------------------------------

    def choose_tier(self, work: Path) -> TierDecision:
        override = self.req.override or label_override(self.labels)
        if override:
            sources = {"comment": "/review command", "cli": "--tier flag"}
            source = sources.get(self.req.trigger, "tier input") if self.req.override else "review label"
            if self.req.min_tier and max_tier(override, self.req.min_tier) != override:
                return TierDecision(self.req.min_tier, "min_tier", f"minimum tier {self.req.min_tier} requested by the caller (over {override} from {source})")
            return TierDecision(override, "override", f"requested by {source}")
        floor, reason = floor_tier(self.files, self.cfg["tiers"])
        floor, reason = raise_floor(floor, reason, self.req.min_tier)
        if floor == "full":
            return TierDecision("full", "rules", reason)
        call = Call(
            kind="triage",
            prompt=prompts.triage_prompt(pr=self.pr, files=self.files, floor=floor, floor_reason=reason),
            schema="triage",
            cwd=str(work),
            effort=self.cfg["effort"]["triage"],
            timeout_s=self.cfg["timeout_minutes"]["triage"] * 60,
        )
        result = self.chain.run(call)
        return combine(floor, reason, (str(result.data["tier"]).lower(), str(result.data["reason"])))

    # ---- passes ------------------------------------------------------------------------------

    def call(self, kind: str, tier: str, prompt: str, cwd: Path, read_repo: bool = False) -> dict:
        effort_key = "merge" if kind == "merge" else tier
        data = self.chain.run(Call(
            kind=kind,
            prompt=prompt,
            schema="review",
            cwd=str(cwd),
            effort=self.cfg["effort"][effort_key],
            timeout_s=self.cfg["timeout_minutes"][effort_key] * 60,
            read_repo=read_repo,
        )).data
        self.progress.done(kind)
        return data

    def context_files(self, snapshot: Path) -> dict[str, str]:
        out, used = {}, 0
        budget = self.cfg["context_budget_chars"]
        for f in sorted(self.files, key=lambda f: f.changes, reverse=True):
            if f.ignored or f.status == "removed":
                continue
            path = snapshot / f.path
            if not path.is_file() or path.stat().st_size > 60_000:
                continue
            try:
                text = path.read_text()
            except UnicodeDecodeError:
                continue
            if used + len(text) > budget:
                continue
            out[f.path] = text
            used += len(text)
        return out

    def review_single(self, tier: str, work: Path, snapshot: Path | None) -> Outcome:
        diff_text, omitted = render_diff(self.files, self.cfg["diff_budget_chars"])
        notes = []
        if omitted:
            notes.append(f"{len(omitted)} files did not fit the review budget and were not reviewed: "
                         + ", ".join(omitted[:10]) + (" ..." if len(omitted) > 10 else ""))
        prompt = prompts.review_prompt(
            pr=self.pr, tier=tier, diff_text=diff_text, guidelines=self.guidelines,
            context_files=self.context_files(snapshot) if (tier == "normal" and snapshot) else None,
            notes=notes,
        )
        data = self.call(tier, tier, prompt, work)
        return Outcome(data["summary"], data["findings"], notes)

    def review_full(self, snapshot: Path) -> Outcome:
        chunks = chunk_files(self.files, self.cfg["diff_budget_chars"])
        passes = []
        if len(chunks) == 1:
            diff_text, _ = render_diff(chunks[0], self.cfg["diff_budget_chars"])
            for focus in ("correctness", "security", "tests"):
                passes.append((f"full:{focus}", focus, diff_text, []))
        else:
            for i, chunk in enumerate(chunks, 1):
                diff_text, _ = render_diff(chunk, self.cfg["diff_budget_chars"])
                note = (f"The diff is large, so it is reviewed in {len(chunks)} parts. This is part {i}; "
                        "other files are reviewed separately but you may read them for context.")
                passes.append((f"full:part{i}", "all", diff_text, [note]))
        self.progress.plan([kind for kind, *_ in passes] + ["merge", "post"], parts=len(chunks))

        def run_pass(spec):
            kind, focus, diff_text, notes = spec
            prompt = prompts.review_prompt(
                pr=self.pr, tier="full", diff_text=diff_text, focus=focus,
                guidelines=self.guidelines, notes=notes,
            )
            return self.call(kind, "full", prompt, snapshot, read_repo=True)

        workers = max(1, min(int(self.cfg["full_parallel"]), len(passes)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(run_pass, spec) for spec in passes]
            try:
                results = [f.result() for f in futures]  # keeps pass order; raises the first failure
            except BaseException:
                for f in futures:
                    f.cancel()
                kill_all()  # stop sibling passes instead of waiting for them
                raise
        summaries = [r["summary"] for r in results]
        findings = [item for r in results for item in r["findings"]]

        notes = [f"Reviewed in {len(chunks)} parts because of its size."] if len(chunks) > 1 else []
        if not findings:
            self.progress.skip("merge")
            return Outcome(summaries[0], [], notes)
        try:
            merge_prompt = prompts.merge_prompt(pr=self.pr, summaries=summaries, findings=findings)
            merged = self.call("merge", "full", merge_prompt, self.work)
            return Outcome(merged["summary"], merged["findings"], notes)
        except (ProviderError, AllLimited) as e:
            log(f"[merge] failed ({e}); merging passes without a model")
            return Outcome(summaries[0], findings, notes)

    # ---- output ------------------------------------------------------------------------------

    def reviewers(self) -> str:
        used = ", ".join(f"{LABELS[p]} ({self.chain.models[p]})" for p in self.chain.used) or "none"
        if self.chain.limit_notes:
            used += f" · usage limit hit: {', '.join(self.chain.limit_notes)}"
        return used

    def publish(self, decision: TierDecision, outcome: Outcome) -> None:
        findings = post.normalize(outcome.findings, decision.tier)
        inline, rest = post.split_inline(findings, self.files, self.cfg["max_inline_comments"])
        body_args = dict(
            tier=decision.tier, tier_reason=decision.reason, reviewers=self.reviewers(),
            summary=outcome.summary, findings=findings, head_sha=self.head_sha, notes=outcome.notes,
        )
        payload = post.build_payload(head_sha=self.head_sha, body=post.build_body(inline=inline, rest=rest, **body_args), inline=inline)
        self.result.summary = outcome.summary
        self.result.notes = list(outcome.notes)
        self.result.findings = with_ids(findings, inline)
        write_step_summary(payload["body"])
        self.progress.begin("post", "GitHub")
        if self.dry_run:
            out = Path("out")
            out.mkdir(exist_ok=True)
            target = out / f"review-{self.req.repo.replace('/', '_')}-{self.req.number}.json"
            target.write_text(json.dumps(payload, indent=2))
            log(f"Dry run: review written to {target} ({len(inline)} inline, {len(rest)} in body)")
            return
        try:
            posted = github.post_review(self.req.repo, self.req.number, payload)
        except github.GitHubError as e:
            log(f"Posting inline comments failed ({e}); posting everything in the review body")
            body = post.build_body(inline=[], rest=findings, **body_args)
            posted = github.post_review(self.req.repo, self.req.number, post.build_payload(head_sha=self.head_sha, body=body, inline=[]))
            self.result.findings = with_ids(findings, [])
        self.result.review = {"id": posted.get("id"), "url": posted.get("html_url")}
        log(f"Posted review: {len(inline)} inline comments, {len(rest)} in the body")

    def fail(self, message: str) -> None:
        body = f"### 🐢 codeturtle could not review this PR\n\n{post.redact(message)}\n\n<sub>Re-run with `/review` once fixed.</sub>"
        write_step_summary(body)
        if not self.dry_run:
            with contextlib.suppress(github.GitHubError):
                github.post_comment(self.req.repo, self.req.number, body)

    # ---- entry -------------------------------------------------------------------------------

    def record_reviewers(self) -> None:
        if self.chain:
            self.result.reviewers = {
                "providers_used": list(self.chain.used),
                "models": {p: self.chain.models[p] for p in self.chain.used},
                "usage_limited": list(self.chain.limit_notes),
                "calls": self.chain.calls,
            }

    def run(self) -> int:
        status, error, skip = "failed", "the review stopped before it finished (cancelled or crashed)", None
        try:
            if self.req.comment_id and not self.dry_run:
                github.react(self.req.repo, self.req.comment_id, "eyes")
            skip = self.load()
            if skip:
                log(f"Skipping: {skip}")
                status, error = "skipped", None
                return 0
            self.progress.start()
            max_parallel = int(os.environ.get("CODETURTLE_MAX_PARALLEL", "1"))
            on_wait = lambda: self.progress.note("Waiting for another codeturtle review on this Mac to finish.")
            with review_slot(max_parallel, 90, on_wait), tempfile.TemporaryDirectory(prefix="codeturtle-") as tmp:
                self.work = Path(tmp) / "empty"
                self.work.mkdir()
                with group("Choosing review tier"):
                    decision = self.choose_tier(self.work)
                    log(f"Tier: {decision.tier} ({decision.source}: {decision.reason})")
                    self.result.tier = {"name": decision.tier, "source": decision.source, "reason": decision.reason}
                    self.progress.tier(decision.tier, decision.reason, decided_by=decision.source)
                    if decision.tier != "full":
                        self.progress.plan([decision.tier, "post"])
                snapshot = None
                if decision.tier in ("normal", "full"):
                    with group("Downloading repository snapshot"):
                        snapshot = github.download_snapshot(self.req.repo, self.head_sha, Path(tmp) / "repo")
                        sanitize_snapshot(snapshot)
                with group(f"{decision.tier.capitalize()} review"):
                    if decision.tier == "full":
                        outcome = self.review_full(snapshot)
                    else:
                        outcome = self.review_single(decision.tier, self.work, snapshot)
                self.publish(decision, outcome)
            status, error = "completed", None
            return 0
        except AllLimited as e:
            self.progress.close()
            self.fail(f"Every reviewer is at its usage limit ({', '.join(self.chain.limit_notes)}). "
                      "Comment `/review` to try again once a limit resets.")
            log(str(e))
            status, error = "usage_limited", str(e)
            return 1
        except (ProviderError, github.GitHubError) as e:
            self.progress.close()
            self.fail(f"The review failed: `{e}`. See the workflow run log for details.")
            log(f"Error: {e}")
            status, error = "failed", str(e)
            return 1
        finally:
            self.progress.close()
            self.record_reviewers()
            if status != "completed":
                self.result.findings = []  # a review that did not finish carries no verdict
            self.result.write(status, error, skip)


def write_step_summary(markdown: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as fh:
            fh.write(markdown + "\n")
