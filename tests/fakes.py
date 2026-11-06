"""Run a whole Reviewer against fake GitHub and fake model CLIs."""

import json
import os
import tempfile
from pathlib import Path
from unittest import mock

import codeturtle.review as review_mod
from codeturtle import github, health, providers
from codeturtle.diff import FileChange
from codeturtle.events import Request
from codeturtle.review import Reviewer

HEAD = "a" * 40
BASE = "b" * 40
PR_RAW = {"head": {"repo": {"full_name": "o/r"}, "sha": HEAD, "ref": "feat"},
          "base": {"sha": BASE, "ref": "main"}, "state": "open", "labels": [], "title": "t", "body": "",
          "user": {"login": "dev"}}
OK = {"summary": "s", "findings": []}


def api_file(path, added="x = 1", status="modified"):
    """A PR file as the GitHub API returns it."""
    lines = added.splitlines()
    patch = "@@ -1,1 +1,%d @@\n context\n" % (len(lines) + 1) + "\n".join("+" + line for line in lines)
    return {"filename": path, "status": status, "additions": len(lines), "deletions": 0, "patch": patch}


def change(path, n=1, status="modified"):
    """A FileChange with n added lines."""
    return FileChange.from_api(api_file(path, "\n".join(f"x{i} = {i}" for i in range(n)), status))


def finding(title, severity="major", category="correctness", path="api/search.py", line=2, body="b"):
    return {"severity": severity, "category": category, "path": path, "line": line, "title": title, "body": body}


class FakeRun:
    """One fake environment per test: a temp CODETURTLE_HOME, a result path and a call log."""

    def __init__(self, testcase):
        tmp = tempfile.TemporaryDirectory()
        testcase.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        patcher = mock.patch.dict(os.environ, {"CODETURTLE_HOME": tmp.name,
                                               "CODETURTLE_RESULT_PATH": str(self.home / "result.json")})
        patcher.start()
        testcase.addCleanup(patcher.stop)
        os.environ.pop("GITHUB_STEP_SUMMARY", None)  # restored by the patcher
        self.calls = []  # dicts: kind, schema, effort, read_repo, provider, prompt
        self.mocks = None

    def snapshot(self, repo, sha, dest):
        root = Path(dest) / "repo-sha"
        (root / "api").mkdir(parents=True)
        (root / "api" / "search.py").write_text("x = 1\n")
        return root

    def run(self, files, answer, request=None, providers_ok=("codex", "grok", "claude"), gh=None):
        """`answer(call, provider)` returns the model's JSON or raises. Returns (exit code, posted
        review payload or None, result.json)."""
        def runner(name):
            def run(call, model):
                self.calls.append({"kind": call.kind, "schema": call.schema, "effort": call.effort,
                                   "read_repo": call.read_repo, "provider": name, "prompt": call.prompt})
                return answer(call, name)
            return run

        defaults = dict(get_pr=PR_RAW, file_at_ref=None, get_files=files, post_comment=11, list_comments=[],
                        post_review={"id": 5, "html_url": "https://example.test/review/5"},
                        list_reviews=[], compare=None, review_threads=[], resolve_thread=None,
                        reply_to_comment=None)
        defaults.update(gh or {})
        found = {n: health.Health(n, "ok" if n in providers_ok else "logged_out") for n in ("codex", "grok", "claude")}
        names = [n for n in defaults if hasattr(github, n)] + ["edit_comment", "delete_comment", "download_snapshot"]
        with mock.patch.multiple(github, **{n: mock.DEFAULT for n in names}) as m, \
                mock.patch.object(health, "check_all", return_value=found), \
                mock.patch.dict(providers.RUNNERS, {n: runner(n) for n in ("codex", "grok", "claude")}, clear=True), \
                mock.patch.object(review_mod, "log", lambda msg: None):
            for name, value in defaults.items():
                if name in m:
                    if callable(value) and not isinstance(value, (dict, list)):
                        m[name].side_effect = value
                    else:
                        m[name].return_value = value
            m["download_snapshot"].side_effect = self.snapshot
            self.mocks = m
            code = Reviewer(request or Request("o/r", 1, "dispatch")).run()
            payload = m["post_review"].call_args.args[2] if m["post_review"].called else None
        return code, payload, json.loads((self.home / "result.json").read_text())

    def kinds(self):
        return [c["kind"] for c in self.calls]

    def call(self, kind):
        return next(c for c in self.calls if c["kind"] == kind)
