"""Live progress: a PR comment edited as the review moves through its steps, deleted at the end,
plus an elapsed-time heartbeat in the job log so a long model call never looks frozen."""

from __future__ import annotations

import threading
import time

from . import github

MARKER = "<!-- codeturtle:progress -->"
LOG_EVERY_S = 30
EDIT_EVERY_S = 60


def elapsed(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 60}m {seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


def step_label(kind: str, parts: int = 0) -> str:
    if kind.startswith("full:part"):
        return f"Part {kind[len('full:part'):]} of {parts}"
    return {
        "triage": "Choose review tier",
        "fast": "Fast review",
        "normal": "Normal review",
        "full:correctness": "Correctness pass",
        "full:security": "Security pass",
        "full:tests": "Tests pass",
        "merge": "Merge findings",
        "post": "Post review",
    }.get(kind, kind)


class Progress:
    def __init__(self, repo: str, number: int, enabled: bool, log=print):
        self.repo, self.number, self.enabled, self.log = repo, number, enabled, log
        self.comment_id: int | None = None
        self.started = time.time()
        self.tier_line = ""
        self.steps: list[dict] = []  # {kind, label, state: pending|running|done|skipped, who, t0, t1}
        self.notes: list[str] = []
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None

    # ---- lifecycle ---------------------------------------------------------------------------

    def start(self) -> None:
        self.plan(["triage"])
        if self.enabled:
            self._delete_stale()
            try:
                self.comment_id = github.post_comment(self.repo, self.number, self.render())
            except github.GitHubError as e:
                self.log(f"Could not post the progress comment ({e}); continuing without it")
        self.thread = threading.Thread(target=self._heartbeat, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.stop.set()
        if self.comment_id:
            try:
                github.delete_comment(self.repo, self.comment_id)
            except github.GitHubError as e:
                self.log(f"Could not delete the progress comment: {e}")
            self.comment_id = None

    def _delete_stale(self) -> None:
        """A cancelled earlier run can leave its progress comment behind."""
        try:
            for comment in github.list_comments(self.repo, self.number):
                if MARKER in (comment.get("body") or ""):
                    github.delete_comment(self.repo, comment["id"])
        except github.GitHubError:
            pass

    # ---- updates -----------------------------------------------------------------------------

    def plan(self, kinds: list[str], parts: int = 0) -> None:
        """Append steps that are not planned yet (keeps the ones already shown)."""
        with self.lock:
            known = {s["kind"] for s in self.steps}
            for kind in kinds:
                if kind not in known:
                    self.steps.append({"kind": kind, "label": step_label(kind, parts), "state": "pending",
                                       "who": "", "t0": 0.0, "t1": 0.0})
        self.push()

    def tier(self, tier: str, reason: str, decided_by: str) -> None:
        self.tier_line = f"**Tier:** {tier.capitalize()} — {reason}"
        self._finish_running()
        with self.lock:
            triage = self._step("triage")
            if triage["state"] == "pending":  # decided without a model
                now = time.time()
                triage.update(state="done", who=decided_by, t0=now, t1=now)
        self.push()

    def skip(self, kind: str) -> None:
        with self.lock:
            self._step(kind)["state"] = "skipped"
        self.push()

    def begin(self, kind: str, who: str) -> None:
        with self.lock:
            step = self._step(kind)
            if step["state"] != "running":
                step["t0"] = time.time()
            step["state"], step["who"] = "running", who
        self.push()

    def done(self, kind: str) -> None:
        with self.lock:
            step = self._step(kind)
            step["state"], step["t1"] = "done", time.time()
        self.push()

    def note(self, text: str) -> None:
        with self.lock:
            self.notes.append(text)
        self.push()

    def _finish_running(self) -> None:
        with self.lock:
            for s in self.steps:
                if s["state"] == "running":
                    s["state"], s["t1"] = "done", time.time()

    def _step(self, kind: str) -> dict:
        for s in self.steps:
            if s["kind"] == kind:
                return s
        step = {"kind": kind, "label": step_label(kind), "state": "pending", "who": "", "t0": 0.0, "t1": 0.0}
        self.steps.append(step)
        return step

    # ---- output ------------------------------------------------------------------------------

    def render(self) -> str:
        now = time.time()
        with self.lock:
            lines = [MARKER, f"### 🐢 codeturtle is reviewing… ({elapsed(now - self.started)})", ""]
            if self.tier_line:
                lines += [self.tier_line, ""]
            for s in self.steps:
                who = f" — {s['who']}" if s["who"] else ""
                if s["state"] == "done":
                    lines.append(f"- [x] {s['label']}{who} · {elapsed(s['t1'] - s['t0'])}")
                elif s["state"] == "skipped":
                    lines.append(f"- [x] ~~{s['label']}~~ · not needed")
                elif s["state"] == "running":
                    lines.append(f"- [ ] **{s['label']}**{who} · running {elapsed(now - s['t0'])}")
                else:
                    lines.append(f"- [ ] {s['label']}")
            for note in self.notes:
                lines += ["", f"> {note}"]
            lines += ["", "<sub>This comment is deleted when the review is posted.</sub>"]
        return "\n".join(lines)

    def push(self) -> None:
        if not self.comment_id:
            return
        try:
            # No retries: a missed edit is cosmetic and the next one catches up.
            github.edit_comment(self.repo, self.comment_id, self.render(), retry=False)
        except github.GitHubError as e:
            self.log(f"Progress update failed: {e}")

    def _heartbeat(self) -> None:
        ticks = 0
        while not self.stop.wait(LOG_EVERY_S):
            ticks += 1
            with self.lock:
                running = [s for s in self.steps if s["state"] == "running"]
            if running:
                now = time.time()
                parts = "; ".join(f"{s['label']} — {s['who']} {elapsed(now - s['t0'])}" for s in running)
                self.log(f"… still running: {parts} · total {elapsed(now - self.started)}")
            if ticks % (EDIT_EVERY_S // LOG_EVERY_S) == 0:
                self.push()
