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

from . import followup, github, health, limits, post, prompts, security, verify
from . import max_tier, state_dir
from .config import CONFIG_PATH, DEFAULTS, matches_any, merge
from .diff import FileChange, chunk_files, render_diff
from .events import Request
from .progress import Progress
from .providers import AllLimited, Call, Chain, ProviderError, Unavailable, kill_all, label
from .result import ResultWriter, finding_id, iso, with_ids
from .tiers import TierDecision, combine, floor_tier, is_production, is_sensitive, label_override, raise_floor

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
    root = state_dir() / "slots"
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
        self.health: dict[str, health.Health] = {}
        self.health_notes: list[str] = []  # broken providers, with the fix; shown on the PR
        self.sec = security.Report()  # what this review's security check did
        self.ver = verify.Report()  # what verification did to the blockers and majors
        self.companions: list[security.Companion] = []
        self.security_notes: dict[str, str] = {}  # the repo's own security rules (base branch)
        self.tmp: Path | None = None
        self.snapshot: Path | None = None
        self.followup: followup.Plan | None = None  # set when this review follows up on the last one
        self.pr_files: list[FileChange] = []

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
        self.check_providers(providers)
        self.chain = Chain(
            providers, self.cfg["models"], log=log,
            on_attempt=self.progress.begin,
            on_limit=self.on_limit,
            on_down=lambda name, why: self.progress.note(f"{name} {why}, so the next reviewer took over."),
            on_wait=lambda until: self.progress.note(
                f"Every reviewer is out for now, so the review waits until {limits.fmt_time(until)} "
                "for a usage limit to reset, then carries on."),
            broken={n: h.detail for n, h in self.health.items() if h.broken},
            remembered={n: t for n, t in limits.active().items() if n in providers},
            wait_minutes=float(self.cfg["limit_wait_minutes"]),
            persist=True,
        )
        self.note_remembered_limits()

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
        self.pr_files = list(self.files)  # the whole PR; self.files narrows to the delta in a follow-up
        self.labels = labels
        self.guidelines = {}
        for path in self.cfg["guideline_files"]:
            text = github.file_at_ref(req.repo, path, base_sha)
            if text and text.strip():
                self.guidelines[path] = text[:20_000]
        for path in self.cfg["security"].get("rules_files", []):
            text = github.file_at_ref(req.repo, path, base_sha)
            if text and text.strip():
                self.security_notes[path] = text[:20_000]
        return None

    def plan_followup(self) -> None:
        """Follow up on the last review when this PR's new head descends from the one it saw:
        review only the PR files changed since, and re-check the earlier findings in them."""
        if self.req.fresh or not self.cfg.get("incremental", True):
            return
        try:
            state = followup.latest_state(github.list_reviews(self.req.repo, self.req.number))
        except github.GitHubError as e:
            log(f"Could not read earlier reviews ({e}); reviewing the whole PR")
            return
        if not state or state["sha"] == self.head_sha:
            return
        try:
            change = github.compare(self.req.repo, state["sha"], self.head_sha)
        except github.GitHubError as e:
            log(f"Could not compare with the last reviewed commit ({e}); reviewing the whole PR")
            return
        if change.get("status") != "ahead":
            log(f"The PR's history changed since the last review ({change.get('status')}); reviewing the whole PR")
            return
        in_pr = {f.path for f in self.pr_files}
        delta = [FileChange.from_api(item) for item in change.get("files") or [] if item.get("filename") in in_pr]
        for f in delta:
            f.ignored = matches_any(f.path, self.cfg["ignore_paths"])
        if not any(not f.ignored for f in delta):
            log("None of the PR's reviewable files changed since the last review; reviewing the whole PR")
            return
        self.followup = followup.Plan(since=state["sha"], previous=state["findings"], delta_paths={f.path for f in delta})
        self.files = delta
        log(f"Follow-up: {len(delta)} files changed since {state['sha'][:7]}; "
            f"{len(state['findings'])} earlier findings, {len(self.followup.to_check())} in changed files")

    def check_previous(self) -> dict | None:
        """Statuses (by id) of the earlier findings in changed files, or None when the check failed."""
        fu = self.followup
        diff_text, _ = render_diff(self.files, self.cfg["diff_budget_chars"])
        prompt = followup.prompt(pr=self.pr, since=fu.since, previous=fu.to_check(), diff_text=diff_text)
        try:
            data = self.call("followup", "verify", prompt, self.snapshot, read_repo=True, schema="followup", wait=False)
        except (ProviderError, AllLimited) as e:
            fu.checked = False
            fu.notes.append(f"Earlier findings could not be re-checked ({e}), so they are listed as still open.")
            self.progress.fail("followup")
            return None
        fu.checked = True
        return followup.by_id(data.get("previous"))

    def plan_security(self) -> None:
        """Pick the domain checks for this change and decide whether a dedicated security pass
        runs (every review, unless the change is docs or assets only, or the config turns it off)."""
        cfg = self.cfg["security"]
        self.companions = security.select_companions(self.files, int(cfg.get("max_companions", 4)))
        self.sec.companions = [c.title for c in self.companions]
        reviewed = [f for f in self.files if not f.ignored]
        docs_only = not reviewed or all(matches_any(f.path, self.cfg["tiers"]["fast_paths"]) for f in reviewed)
        mode = str(cfg.get("pass", "auto")).lower()
        if mode == "off":
            self.sec.skipped = "the dedicated pass is turned off in the config"
        elif mode == "auto" and docs_only:
            self.sec.skipped = "only docs or assets changed"
        else:
            self.sec.dedicated = True
        log("Security: " + ("dedicated pass" if self.sec.dedicated else f"main review only ({self.sec.skipped})")
            + (f"; domain checks: {', '.join(self.sec.companions)}" if self.sec.companions else ""))

    # ---- providers ---------------------------------------------------------------------------

    def check_providers(self, providers: list[str]) -> None:
        """Health-check every provider first, so a broken backup shows on this PR now instead of
        failing a later review at the moment it is needed."""
        with group("Checking providers"):
            self.health = health.check_all(providers)
            for name in providers:
                h = self.health[name]
                log(f"{label(name)}: {h.status}" + (f" ({h.detail})" if h.detail else ""))
        self.health_notes = [self.health[n].warning() for n in providers if self.health[n].broken]
        for note in self.health_notes:
            self.progress.note(note)

    def note_remembered_limits(self) -> None:
        chain = self.chain
        ready = [i for i, n in enumerate(chain.providers) if n not in chain.remembered and n not in chain.broken]
        for i, name in enumerate(chain.providers):
            if name in chain.remembered and ready and i < ready[0]:
                self.progress.note(f"{label(name)} is at its usage limit until {limits.fmt_time(chain.remembered[name])} "
                                   "(seen by an earlier review), so other reviewers go first.")

    def on_limit(self, name: str, reset: float | None) -> None:
        when = f" (it resets {limits.fmt_time(reset)})" if reset else ""
        self.progress.note(f"{name} hit its usage limit{when}, so the next reviewer took over.")

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

    def call(self, kind: str, effort_key: str, prompt: str, cwd: Path, read_repo: bool = False,
             schema: str = "review", wait: bool = True, avoid=(), full_result: bool = False):
        """One model call; returns its JSON (or the whole Result with `full_result`). `wait=False`
        for a step with its own fallback (merge, verify): it never waits for a usage limit to reset.
        `avoid`: providers to use only when no other can run."""
        result = self.chain.run(Call(
            kind=kind,
            prompt=prompt,
            schema=schema,
            cwd=str(cwd),
            effort=self.cfg["effort"][effort_key],
            timeout_s=self.cfg["timeout_minutes"][effort_key] * 60,
            read_repo=read_repo,
        ), wait=wait, avoid=avoid)
        self.progress.done(kind)
        return result if full_result else result.data

    def run_jobs(self, jobs: list[tuple[str, callable, bool]]) -> dict:
        """Run (key, call, optional) jobs, up to `full_parallel` at once. A required job's failure
        stops the others and is raised; an optional job's failure is returned as its result."""
        workers = max(1, min(int(self.cfg["full_parallel"]), len(jobs)))
        results = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [(key, pool.submit(fn), optional) for key, fn, optional in jobs]
            try:
                for key, future, optional in futures:  # in order; raises the first required failure
                    try:
                        results[key] = future.result()
                    except Exception as e:
                        if not optional:
                            raise
                        log(f"[{key}] failed ({e}); continuing without it")
                        results[key] = e
            except BaseException:
                for _, future, _ in futures:
                    future.cancel()
                kill_all()  # stop sibling passes instead of waiting for them
                raise
        return results

    def ensure_snapshot(self) -> Path:
        """The sanitized repository snapshot at the PR head, downloaded on first use."""
        if self.snapshot is None:
            with group("Downloading repository snapshot"):
                self.snapshot = github.download_snapshot(self.req.repo, self.head_sha, self.tmp / "repo")
                sanitize_snapshot(self.snapshot)
        return self.snapshot

    def context_files(self, snapshot: Path, files: list[FileChange] | None = None) -> dict[str, str]:
        out, used = {}, 0
        budget = self.cfg["context_budget_chars"]
        for f in sorted(self.files if files is None else files, key=lambda f: f.changes, reverse=True):
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
        jobs = [("main", lambda: self.call(tier, tier, prompt, work), False)]
        if self.sec.dedicated:
            # Fast reviews have only the diff; normal ones let the security pass read the snapshot.
            sec_prompt = security.pass_prompt(
                pr=self.pr, depth="repo" if snapshot else "fast", diff_text=diff_text,
                companions=self.companions, notes=self.security_notes, extra=notes,
            )
            jobs.append(("security", lambda: self.call(
                "security", tier, sec_prompt, snapshot or work, read_repo=snapshot is not None, schema="security"), True))
        results = self.run_jobs(jobs)
        data = results["main"]
        findings = self.check_security(data["findings"], results.get("security"), "security", tier, snapshot)
        return Outcome(data["summary"], findings, notes)

    def risk_split(self, decision: TierDecision) -> tuple[list[FileChange], list[FileChange]]:
        """(files for the full passes, files for a normal-depth pass). The rules chose `full` only
        because of sensitive paths: those files get the full passes and the rest a normal one. A
        requested or triaged full review, or one the size rule alone would force, covers everything."""
        rules = self.cfg["tiers"]
        reviewed = [f for f in self.files if not f.ignored]
        production_lines = sum(f.changes for f in reviewed if is_production(f, rules))
        if (not rules.get("split_full", True) or decision.source not in ("rules", "min_tier")
                or production_lines > rules["full_if_lines_over"]):
            return self.files, []
        rest = [f for f in reviewed if not is_sensitive(f, rules)]
        full = [f for f in self.files if f not in rest]  # sensitive files, plus generated ones as headers
        if not rest or not any(is_sensitive(f, rules) for f in full):
            return self.files, []
        return full, rest

    def review_full(self, snapshot: Path, decision: TierDecision) -> Outcome:
        budget = self.cfg["diff_budget_chars"]
        full_files, rest = self.risk_split(decision)
        chunks = chunk_files(full_files, budget)
        passes = []  # (kind, prompt, schema, optional, effort, cwd, read_repo)
        sensitive = len([f for f in full_files if not f.ignored])
        split_note = [f"Only {sensitive} of the changed files touch sensitive paths, and this pass reviews "
                      f"those. The other {len(rest)} files get a normal-depth review separately; you may "
                      "read them for context."] if rest else []

        def review_pass(kind, focus, diff_text, notes):
            prompt = prompts.review_prompt(pr=self.pr, tier="full", diff_text=diff_text, focus=focus,
                                           guidelines=self.guidelines, notes=notes)
            passes.append((kind, prompt, "review", False, "full", snapshot, True))

        def security_pass(diff_text, notes):
            if self.sec.dedicated:
                prompt = security.pass_prompt(pr=self.pr, depth="repo", diff_text=diff_text, companions=self.companions,
                                              notes=self.security_notes, extra=notes)
                passes.append(("full:security", prompt, "security", True, "full", snapshot, True))

        if len(chunks) == 1:
            diff_text, _ = render_diff(chunks[0], budget)
            review_pass("full:correctness", "correctness", diff_text, split_note)
            # The security pass always sees the whole change, sensitive files first.
            security_pass(*(self.security_diff() if rest else (diff_text, [])))
            review_pass("full:tests", "tests", diff_text, split_note)
        else:
            for i, chunk in enumerate(chunks, 1):
                diff_text, _ = render_diff(chunk, budget)
                note = (f"The diff is large, so it is reviewed in {len(chunks)} parts. This is part {i}; "
                        "other files are reviewed separately but you may read them for context.")
                review_pass(f"full:part{i}", "all", diff_text, [note] + split_note)
            security_pass(*self.security_diff())
        rest_chunks = chunk_files(rest, budget) if rest else []
        for i, chunk in enumerate(rest_chunks, 1):
            diff_text, _ = render_diff(chunk, budget)
            note = ("These files don't touch sensitive paths, so they get a normal-depth review; the sensitive "
                    "files get a full review separately."
                    + (f" This is part {i} of {len(rest_chunks)}." if len(rest_chunks) > 1 else ""))
            prompt = prompts.review_prompt(pr=self.pr, tier="normal", diff_text=diff_text, guidelines=self.guidelines,
                                           context_files=self.context_files(snapshot, chunk), notes=[note])
            kind = "full:rest" if len(rest_chunks) == 1 else f"full:rest{i}"
            passes.append((kind, prompt, "review", False, "normal", self.work, False))
        self.progress.plan([kind for kind, *_ in passes] + ["verify", "merge", "post"], parts=len(chunks))

        results = self.run_jobs([
            (kind, (lambda kind=kind, prompt=prompt, schema=schema, effort=effort, cwd=cwd, read=read:
                    self.call(kind, effort, prompt, cwd, read_repo=read, schema=schema)), optional)
            for kind, prompt, schema, optional, effort, cwd, read in passes
        ])
        reviews = [results[kind] for kind, _, schema, *_ in passes if schema == "review"]
        summaries = [r["summary"] for r in reviews]
        findings = [item for r in reviews for item in r["findings"]]
        findings = self.check_security(findings, results.get("full:security"), "full:security", "full", snapshot)

        notes = [f"Reviewed in {len(chunks)} parts because of its size."] if len(chunks) > 1 else []
        if rest:
            notes.append(f"Full review on the {sensitive} files that touch sensitive paths; normal-depth "
                         f"review on the other {len(rest)}.")
        if not findings:
            self.progress.skip("merge")
            return Outcome(summaries[0], [], notes)
        try:
            merge_prompt = prompts.merge_prompt(pr=self.pr, summaries=summaries, findings=findings)
            merged = self.call("merge", "merge", merge_prompt, self.work, wait=False)
            return Outcome(merged["summary"], merged["findings"], notes)
        except (ProviderError, AllLimited) as e:
            log(f"[merge] failed ({e}); merging passes without a model")
            return Outcome(summaries[0], findings, notes)

    def security_diff(self) -> tuple[str, list[str]]:
        """For a diff too large for one pass: the most security-relevant files first (sensitive
        paths, then files a domain check selected), within the budget."""
        sensitive = self.cfg["tiers"]["full_paths"]

        def rank(f: FileChange) -> int:
            if matches_any(f.path, sensitive):
                return 0
            text = security.added_text(f)
            return 1 if any(matches_any(f.path, list(c.paths)) or c.content.search(text) for c in self.companions) else 2

        files = sorted((f for f in self.files if not f.ignored), key=rank)
        diff_text, omitted = render_diff(files, self.cfg["diff_budget_chars"])
        notes = ["The diff is too large for one pass, so this security pass sees the most security-relevant "
                 "files first. You may read every other file in the snapshot."]
        if omitted:
            notes.append(f"{len(omitted)} lower-priority files did not fit: " + ", ".join(omitted[:10])
                         + (" ..." if len(omitted) > 10 else ""))
        return diff_text, notes

    def check_security(self, findings: list, pass_result, pass_kind: str, tier: str, snapshot: Path | None) -> list:
        """Independent verification before anything is posted: every security candidate (the
        dedicated pass's, plus security findings from the other passes) and every blocker and major,
        in one call on a different provider where one is available. Returns the findings to post."""
        rep, ver = self.sec, self.ver
        if isinstance(pass_result, Exception):
            rep.notes.append(f"The dedicated security pass failed ({pass_result}), so only the main review "
                             "checked security.")
            self.progress.fail(pass_kind)
            pass_result = None
        findings = [f for f in findings if isinstance(f, dict)]
        severities = {str(s).lower() for s in self.cfg["verify"].get("severities") or []}
        plain = [f for f in findings if not security.is_security(f)]
        bugs = verify.bug_items([f for f in plain if str(f.get("severity", "")).lower() in severities])
        others = [f for f in plain if str(f.get("severity", "")).lower() not in severities]
        raw = list((pass_result or {}).get("candidates") or [])
        raw += security.from_findings([f for f in findings if security.is_security(f)])
        candidates = security.normalize(raw)
        rep.candidates, ver.checked = len(candidates), len(bugs)
        check_security = bool(candidates) and self.cfg["security"].get("verify", True)
        confirmed = []
        if candidates and not check_security:
            confirmed, rep.leads = security.unverified(candidates)
            rep.verified = False
        kept = [{k: b[k] for k in ("severity", "category", "path", "line", "title", "body")} for b in bugs]
        if not (bugs or check_security):
            self.progress.skip("verify")
        else:
            try:
                snapshot = snapshot or self.ensure_snapshot()
                cited = {c["path"] for c in candidates} | {b["path"] for b in bugs}
                diff_text, _ = render_diff([f for f in self.files if f.path in cited] or self.files,
                                           self.cfg["context_budget_chars"])
                prompt = verify.prompt(pr=self.pr, security_items=candidates if check_security else [], bugs=bugs,
                                       diff_text=diff_text, notes=self.security_notes)
                # The agent that checks a finding should not be the one that found it.
                avoid = set(self.chain.used) if self.cfg["verify"].get("different_provider", True) else set()
                result = self.call("verify", "verify", prompt, snapshot, read_repo=True, schema="verify",
                                   wait=False, avoid=avoid, full_result=True)
                verdicts = verify.by_id(result.data["verdicts"])
                ver.provider = f"{label(result.provider)} ({result.model})"
                ver.same_provider = result.provider in avoid
                if check_security:
                    confirmed, rep.leads, rep.rejected = security.apply_verdicts(candidates, result.data["verdicts"])
                    rep.verified = True
                kept = verify.apply(bugs, verdicts, ver)
            except (ProviderError, AllLimited, github.GitHubError) as e:
                log(f"[verify] failed ({e}); posting the findings unverified")
                if check_security:
                    confirmed, rep.leads = security.unverified(candidates)
                    rep.verified = False
                    rep.notes.append(f"The security findings were not independently verified ({e}).")
                if bugs:
                    ver.unchecked = len(bugs)
                    ver.notes.append(f"The blocker and major findings were not independently verified ({e}).")
                self.progress.fail("verify")
        if tier == "fast":  # a fast review posts only blockers and majors
            confirmed = [f for f in confirmed if f["severity"] in ("blocker", "major")]
        rep.confirmed = len(confirmed)
        return others + kept + confirmed

    def verification_result(self) -> dict:
        v = self.ver
        return {
            "checked": v.checked, "confirmed": v.confirmed, "downgraded": v.downgraded, "rejected": v.rejected,
            "unchecked": v.unchecked, "provider": v.provider, "same_provider": v.same_provider,
            "needs_validation": [{
                "id": finding_id({**lead, "severity": "needs_validation"}),
                "path": lead["path"], "line": lead["line"], "category": lead["attack_class"],
                "title": post.redact(lead["title"]), "unresolved": post.redact(lead["unresolved"]),
            } for lead in v.leads],
        }

    def security_result(self) -> dict:
        rep = self.sec
        return {
            "dedicated_pass": rep.dedicated,
            "skipped": rep.skipped,
            "companions": list(rep.companions),
            "candidates": rep.candidates,
            "confirmed": rep.confirmed,
            "rejected": rep.rejected,
            "verified": rep.verified,
            "needs_validation": [{
                "id": finding_id({**lead, "severity": "needs_validation"}),
                "path": lead["path"], "line": lead["line"], "attack_class": lead["attack_class"],
                "title": post.redact(lead["title"]), "unresolved": post.redact(lead["unresolved"]),
            } for lead in rep.leads],
            "notes": [post.redact(n) for n in rep.notes],
        }

    # ---- output ------------------------------------------------------------------------------

    def reviewers(self) -> str:
        chain = self.chain
        used = ", ".join(f"{label(p)} ({chain.models[p]})" for p in chain.used) or "none"
        limited = [label(n) + (f" (until {limits.fmt_time(r)})" if r else "") for n, r in chain.at_limit()]
        if limited:
            used += f" · usage limit: {', '.join(limited)}"
        why = {"setup": "could not authenticate", "missing": "not installed", "transient": "temporary errors"}
        down = [f"{label(n)} ({why[chain.down[n][0]]})" for n in chain.providers if n in chain.down]
        if down:
            used += f" · unavailable: {', '.join(down)}"
        return used

    def publish(self, decision: TierDecision, outcome: Outcome) -> None:
        fu = self.followup
        carried = list(fu.open) if fu else []  # still-open earlier findings: listed, not re-posted inline
        findings = post.normalize(outcome.findings, decision.tier)
        if fu:
            findings = fu.drop_repeats(findings)
            fu.new = len(findings)
        self.assign_ids(findings, taken={f["id"] for f in carried})
        inline, rest = post.split_inline(findings, self.pr_files, self.cfg["max_inline_comments"])
        notes = self.health_notes + self.sec.notes + self.ver.notes + (fu.notes if fu else []) + outcome.notes
        body_args = dict(
            tier=decision.tier, tier_reason=decision.reason, reviewers=self.reviewers(),
            summary=outcome.summary, findings=carried + findings, head_sha=self.head_sha, notes=notes,
            security=self.sec, verification=self.ver, followup=fu,
        )
        # The hidden state lets the next review of this PR follow up on this one.
        state = followup.encode_state(self.head_sha, [
            {**f, "title": post.redact(f["title"]), "body": post.redact(f["body"])} for f in carried + findings])
        body = post.build_body(inline=inline, rest=rest, **body_args) + "\n\n" + state
        payload = post.build_payload(head_sha=self.head_sha, body=body, inline=inline)
        self.result.summary = outcome.summary
        self.result.notes = list(notes)
        self.result.findings = with_ids(carried + findings, inline)
        self.result.security = self.security_result()
        self.result.verification = self.verification_result()
        self.result.followup = fu.result() if fu else None
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
            body = post.build_body(inline=[], rest=findings, **body_args) + "\n\n" + state
            posted = github.post_review(self.req.repo, self.req.number, post.build_payload(head_sha=self.head_sha, body=body, inline=[]))
            self.result.findings = with_ids(carried + findings, [])
        self.result.review = {"id": posted.get("id"), "url": posted.get("html_url")}
        log(f"Posted review: {len(inline)} inline comments, {len(rest)} in the body")
        if fu:
            self.resolve_fixed(fu)

    @staticmethod
    def assign_ids(findings: list[dict], taken: set[str]) -> None:
        """Give each new finding a stable id now, so its inline comment can carry it."""
        for f in findings:
            if not f.get("id"):
                base = fid = finding_id(f)
                n = 2
                while fid in taken:
                    fid, n = f"{base}-{n}", n + 1
                f["id"] = fid
            taken.add(f["id"])

    def resolve_fixed(self, fu: followup.Plan) -> None:
        """Reply to and resolve the threads of earlier findings that are fixed or no longer apply.
        Best effort: a failure here never fails the review."""
        done = {f["id"]: f for f in fu.fixed + fu.obsolete}
        if not done:
            return
        try:
            threads = github.review_threads(self.req.repo, self.req.number)
        except github.GitHubError as e:
            log(f"Could not list review threads ({e}); leaving them open")
            return
        resolved = 0
        for thread in threads:
            first = ((thread.get("comments") or {}).get("nodes") or [{}])[0]
            m = followup.FINDING_RE.search(first.get("body") or "")
            if not m or m.group(1) not in done or thread.get("isResolved"):
                continue
            f = done[m.group(1)]
            word = "Fixed" if f in fu.fixed else "No longer applies"
            note = post.redact(f": {f['note']}" if f.get("note") else "")
            try:
                if first.get("databaseId"):
                    github.reply_to_comment(self.req.repo, self.req.number, first["databaseId"],
                                            f"✅ {word} as of `{self.head_sha[:7]}`{note}")
                github.resolve_thread(thread["id"])
                resolved += 1
            except github.GitHubError as e:
                log(f"Could not resolve the thread for {m.group(1)}: {e}")
        log(f"Resolved {resolved} review threads of fixed findings")

    def fail(self, message: str) -> None:
        notes = "".join(f"\n\n> {n}" for n in self.health_notes)
        body = (f"### 🐢 codeturtle could not review this PR\n\n{post.redact(message + notes)}"
                "\n\n<sub>Re-run with `/review` once fixed.</sub>")
        write_step_summary(body)
        if not self.dry_run:
            with contextlib.suppress(github.GitHubError):
                github.post_comment(self.req.repo, self.req.number, body)

    # ---- entry -------------------------------------------------------------------------------

    def record_reviewers(self) -> None:
        if self.chain:
            limited = self.chain.at_limit()
            self.result.reviewers = {
                "providers_used": list(self.chain.used),
                "models": {p: self.chain.models[p] for p in self.chain.used},
                "usage_limited": [label(n) for n, _ in limited],
                "calls": self.chain.calls,
                # 1.2: why a provider was not used, and what the health check found.
                "limits": {n: iso(r) for n, r in limited},
                "unavailable": {n: self.chain.down[n][0] for n in self.chain.providers if n in self.chain.down},
                "health": {n: {"status": h.status, "detail": post.redact(h.detail)[:200]} for n, h in self.health.items()},
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
                self.tmp = Path(tmp)
                self.work = self.tmp / "empty"
                self.work.mkdir()
                with group("Checking for an earlier review"):
                    self.plan_followup()
                with group("Choosing review tier"):
                    decision = self.choose_tier(self.work)
                    log(f"Tier: {decision.tier} ({decision.source}: {decision.reason})")
                    self.result.tier = {"name": decision.tier, "source": decision.source, "reason": decision.reason}
                    self.progress.tier(decision.tier, decision.reason, decided_by=decision.source)
                    self.plan_security()
                    recheck = ["followup"] if self.followup and self.followup.to_check() else []
                    if decision.tier != "full":
                        self.progress.plan(recheck + [decision.tier] + (["security"] if self.sec.dedicated else [])
                                           + ["verify", "post"])
                    elif recheck:
                        self.progress.plan(recheck)
                snapshot = self.ensure_snapshot() if decision.tier in ("normal", "full") or recheck else None
                # Earlier findings are re-checked while the new commits are reviewed.
                with ThreadPoolExecutor(max_workers=1) as pool:
                    rechecking = pool.submit(self.check_previous) if recheck else None
                    with group(f"{decision.tier.capitalize()} review"):
                        if decision.tier == "full":
                            outcome = self.review_full(snapshot, decision)
                        else:
                            outcome = self.review_single(decision.tier, self.work, snapshot)
                    if self.followup:
                        self.followup.apply(rechecking.result() if rechecking else None)
                self.publish(decision, outcome)
            status, error = "completed", None
            return 0
        except AllLimited as e:
            self.progress.close()
            when = f"after {limits.fmt_time(e.retry_at)}" if e.retry_at else "once a limit resets"
            self.fail(f"No reviewer could run: {'; '.join(e.parts)}. Comment `/review` to try again {when}.")
            self.result.retry_at = iso(e.retry_at)
            log(str(e))
            status, error = "usage_limited", str(e)
            return 1
        except Unavailable as e:
            self.progress.close()
            self.fail(f"No reviewer could run: {'; '.join(e.parts)}. Comment `/review` to try again once that is fixed.")
            log(str(e))
            status, error = "failed", str(e)
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
