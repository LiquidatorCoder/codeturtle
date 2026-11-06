"""Provider health, error classification, remembered limits, waiting for a reset, gh retries."""

import json
import os
import subprocess
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import codeturtle.review as review_mod
from codeturtle import github, health, limits, providers
from codeturtle.config import DEFAULTS
from codeturtle.events import Request
from codeturtle.result import ResultWriter
from codeturtle.review import Reviewer

CALL = providers.Call("fast", "p", "review", "/tmp", "low", 60)
OK = {"summary": "s", "findings": []}
GROK_402 = ('Error: Internal error: { "message": "API error (status 402 Payment Required): '
            'Grok Build usage balance exhausted", "http_status": 402 }')


class TempHome(unittest.TestCase):
    """Each test gets its own CODETURTLE_HOME, so nothing touches the real ~/.codeturtle."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        patcher = mock.patch.dict(os.environ, {"CODETURTLE_HOME": tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)


class Clock:
    def __init__(self, t: float = 1_800_000_000.0):
        self.t, self.slept = t, []

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


def chain(names, clock=None, **kw):
    clock = clock or Clock()
    kw.setdefault("log", lambda m: None)
    return providers.Chain(names, DEFAULTS["models"], sleep=clock.sleep, clock=clock, **kw)


def runner(*steps):
    """A fake provider run: raises or returns each step in turn; the last one repeats."""
    steps = list(steps)

    def run(call, model):
        step = steps.pop(0) if len(steps) > 1 else steps[0]
        if isinstance(step, BaseException):
            raise step
        return step

    return run


class ClassifyTests(unittest.TestCase):
    def test_failures_are_sorted_into_limit_setup_and_temporary(self):
        cases = {
            "Selected model is at capacity. Please try a different model.": providers.TransientError,
            'API Error: 500 {"type":"error","error":{"message":"Internal server error"}}': providers.TransientError,
            "stream disconnected before completion: error sending request": providers.TransientError,
            "dial tcp 20.207.73.85:443: connect: no route to host": providers.TransientError,
            "API Error: Request timed out.": providers.TransientError,
            "Not logged in · Please run /login": providers.SetupError,
            "Error: 401 Unauthorized": providers.SetupError,
            "Your refresh token was already used. Please log out and sign in again.": providers.SetupError,
            GROK_402: providers.LimitHit,  # a limit wins over the "Internal error" wrapper
            "You've hit your usage limit. Try again at 7:39 PM.": providers.LimitHit,
        }
        for text, error in cases.items():
            with self.subTest(text=text), self.assertRaises(error):
                providers.classify("grok", text)

    def test_unrecognised_failures_are_left_to_the_caller(self):
        for text in ["could not fetch PR #401", "PR #503 is ready", "segmentation fault", ""]:
            providers.classify("grok", text)

    def test_runners_raise_the_right_error(self):
        capacity = '{"type":"error","message":"Selected model is at capacity. Please try a different model."}\n'
        with mock.patch.object(providers, "find_bin", return_value="codex"), \
                mock.patch.object(providers, "run_process", return_value=(1, capacity, "")):
            with self.assertRaises(providers.TransientError):
                providers.run_codex(CALL, "gpt-6-sol")
        logged_out = json.dumps({"is_error": True, "result": "Not logged in · Please run /login"})
        overloaded = json.dumps({"is_error": True, "api_error_status": 529, "result": "Overloaded"})
        for out, error in ((logged_out, providers.SetupError), (overloaded, providers.TransientError)):
            with mock.patch.object(providers, "find_bin", return_value="claude"), \
                    mock.patch.object(providers, "run_process", return_value=(1, out, "")):
                with self.assertRaises(error):
                    providers.run_claude(CALL, "claude-opus-5-5")

    def test_limit_carries_its_reset_time(self):
        with self.assertRaises(providers.LimitHit) as ctx:
            providers.check_limit("codex", "You've hit your usage limit. Try again in 20 minutes.")
        self.assertAlmostEqual(ctx.exception.reset_at, time.time() + 1200, delta=5)

    def test_bad_bin_override_is_a_missing_cli(self):
        with mock.patch.dict(os.environ, {"CODETURTLE_GROK_BIN": "/nonexistent/grok"}):
            with self.assertRaises(providers.MissingCLI):
                providers.find_bin("grok")


class ChainTests(TempHome):
    def test_temporary_error_is_retried_on_the_same_provider(self):
        clock = Clock()
        with mock.patch.dict(providers.RUNNERS, {"codex": runner(providers.TransientError("Codex: at capacity"), OK),
                                                 "grok": runner(OK)}):
            c = chain(["codex", "grok"], clock)
            self.assertEqual(c.run(CALL).provider, "codex")
        self.assertEqual(clock.slept, [20])
        self.assertEqual(c.calls, 2)

    def test_lasting_temporary_error_moves_on_and_says_so(self):
        clock, notes = Clock(), []
        with mock.patch.dict(providers.RUNNERS, {"codex": runner(providers.TransientError("Codex: at capacity")),
                                                 "grok": runner(OK)}):
            c = chain(["codex", "grok"], clock, on_down=lambda name, why: notes.append((name, why)))
            self.assertEqual(c.run(CALL).provider, "grok")
            self.assertEqual(c.run(CALL).provider, "grok")  # Codex stays skipped for the review
        self.assertEqual(clock.slept, [20, 60])
        self.assertEqual(c.down["codex"][0], "transient")
        self.assertEqual(notes, [("Codex", "kept failing with a temporary error")])

    def test_setup_error_moves_on_without_retrying(self):
        clock = Clock()
        with mock.patch.dict(providers.RUNNERS, {"codex": runner(providers.SetupError("Codex: Not logged in")),
                                                 "claude": runner(OK)}):
            c = chain(["codex", "claude"], clock)
            self.assertEqual(c.run(CALL).provider, "claude")
        self.assertEqual(clock.slept, [])
        self.assertEqual(c.describe(), ["Codex could not authenticate on this runner"])

    def test_unknown_error_still_stops_the_review(self):
        with mock.patch.dict(providers.RUNNERS, {"codex": runner(providers.ProviderError("codex exited 139")),
                                                 "grok": runner(OK)}):
            with self.assertRaises(providers.ProviderError):
                chain(["codex", "grok"]).run(CALL)

    def test_provider_flagged_by_the_health_check_is_tried_last(self):
        calls = []

        def ok(name):
            return lambda call, model: calls.append(name) or OK

        with mock.patch.dict(providers.RUNNERS, {"codex": ok("codex"), "grok": ok("grok")}):
            self.assertEqual(chain(["codex", "grok"], broken={"codex": "x"}).run(CALL).provider, "grok")
        self.assertEqual(calls, ["grok"])
        # Still a last resort: the check could be wrong.
        with mock.patch.dict(providers.RUNNERS, {"codex": ok("codex"), "grok": runner(providers.LimitHit("Grok: limit"))}):
            self.assertEqual(chain(["codex", "grok"], broken={"codex": "x"}).run(CALL).provider, "codex")

    def test_remembered_limit_goes_after_healthy_providers(self):
        clock = Clock()
        with mock.patch.dict(providers.RUNNERS, {"codex": runner(OK), "grok": runner(OK)}):
            c = chain(["codex", "grok"], clock, remembered={"codex": clock() + 600})
            self.assertEqual(c.run(CALL).provider, "grok")
            self.assertEqual(c.at_limit(), [("codex", clock() + 600)])
            c = chain(["codex", "grok"], clock, remembered={"codex": clock() - 1})  # reset already
            self.assertEqual(c.run(CALL).provider, "codex")

    def test_waits_for_a_reset_that_is_close(self):
        clock, waits = Clock(), []
        reset = clock() + 120
        with mock.patch.dict(providers.RUNNERS, {"codex": runner(providers.LimitHit("Codex: limit", reset), OK),
                                                 "grok": runner(providers.LimitHit("Grok: limit"))}):
            c = chain(["codex", "grok"], clock, wait_minutes=30, on_wait=waits.append)
            self.assertEqual(c.run(CALL).provider, "codex")
        self.assertEqual(waits, [reset + providers.RESET_GRACE_S])
        self.assertEqual(clock.slept, [120 + providers.RESET_GRACE_S])
        self.assertEqual(c.at_limit(), [("grok", None)])  # Codex worked after the wait

    def test_does_not_wait_when_the_reset_is_far_off_or_waiting_is_off(self):
        clock = Clock()
        reset = clock() + 3 * 3600
        for minutes in (30, 0):
            with mock.patch.dict(providers.RUNNERS, {"codex": runner(providers.LimitHit("Codex: limit", reset))}):
                with self.assertRaises(providers.AllLimited) as ctx:
                    chain(["codex"], clock, wait_minutes=minutes).run(CALL)
            self.assertEqual(ctx.exception.retry_at, reset)
        self.assertEqual(clock.slept, [])

    def test_a_step_with_its_own_fallback_never_waits(self):
        clock = Clock()
        with mock.patch.dict(providers.RUNNERS, {"codex": runner(providers.LimitHit("Codex: limit", clock() + 60))}):
            with self.assertRaises(providers.AllLimited):
                chain(["codex"], clock, wait_minutes=30).run(CALL, wait=False)
        self.assertEqual(clock.slept, [])

    def test_out_of_providers_names_each_reason(self):
        clock = Clock()
        with mock.patch.dict(providers.RUNNERS, {"codex": runner(providers.LimitHit("Codex: limit", clock() + 3600)),
                                                 "claude": runner(providers.SetupError("Claude: Not logged in"))}):
            with self.assertRaises(providers.AllLimited) as ctx:
                chain(["codex", "claude"], clock).run(CALL)
        self.assertTrue(ctx.exception.parts[0].startswith("Codex is at its usage limit until "))
        self.assertEqual(ctx.exception.parts[1], "Claude could not authenticate on this runner")
        # Nothing to wait for: a plain failure.
        with mock.patch.dict(providers.RUNNERS, {"claude": runner(providers.SetupError("Claude: Not logged in"))}):
            with self.assertRaises(providers.Unavailable):
                chain(["claude"]).run(CALL)

    def test_limits_are_remembered_and_forgotten(self):
        clock = Clock(time.time())
        reset = clock() + 900
        with mock.patch.dict(providers.RUNNERS, {"codex": runner(providers.LimitHit("Codex: limit", reset)),
                                                 "grok": runner(OK)}):
            chain(["codex", "grok"], clock, persist=True).run(CALL)
        self.assertEqual(limits.active(), {"codex": reset})
        with mock.patch.dict(providers.RUNNERS, {"codex": runner(OK)}):
            chain(["codex"], clock, persist=True, remembered=limits.active()).run(CALL)
        self.assertEqual(limits.active(), {})


class ResetParsingTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"TZ": "Asia/Kolkata"})
        patcher.start()
        time.tzset()
        self.addCleanup(time.tzset)
        self.addCleanup(patcher.stop)

    def at(self, text: str) -> float:
        return datetime.strptime(text, "%Y-%m-%d %H:%M").astimezone().timestamp()

    def test_clock_times(self):
        now = self.at("2026-11-02 18:28")
        codex = "visit https://chatgpt.com/codex/settings/usage to purchase more credits or try again at 7:39 PM."
        self.assertEqual(limits.parse_reset(codex, now), self.at("2026-11-02 19:39"))
        self.assertEqual(limits.parse_reset("try again at 7:39 PM", self.at("2026-11-02 20:00")),
                         self.at("2026-11-03 19:39"))  # that time has passed today: tomorrow
        self.assertEqual(limits.parse_reset("limit hit · resets 3pm (Asia/Kolkata)", self.at("2026-11-02 13:00")),
                         self.at("2026-11-02 15:00"))
        self.assertEqual(limits.parse_reset("try again at Nov 8th, 2026 9:12 AM", now), self.at("2026-11-08 09:12"))
        self.assertEqual(limits.parse_reset("resets at 19:39", now), self.at("2026-11-02 19:39"))

    def test_relative_and_absolute_forms(self):
        now = self.at("2026-11-02 18:28")
        self.assertEqual(limits.parse_reset("Rate limited. Try again in 20 seconds.", now), now + 20)
        self.assertEqual(limits.parse_reset("try again in 2 hours 5 minutes", now), now + 7500)
        self.assertEqual(limits.parse_reset("resets in 1h 20m", now), now + 4800)
        self.assertEqual(limits.parse_reset('{"retry_after": 30}', now), now + 30)
        self.assertEqual(limits.parse_reset(f"Claude AI usage limit reached|{int(now) + 3600}", now), int(now) + 3600)
        self.assertEqual(limits.parse_reset("rate limited until 2026-11-02T14:09:00Z", now), self.at("2026-11-02 19:39"))

    def test_no_usable_reset_time(self):
        now = self.at("2026-11-02 18:28")
        for text in ["usage limit reached", "Grok Build usage balance exhausted", "retry 3 times",
                     "try again later", "try again in 30 days"]:
            with self.subTest(text=text):
                self.assertIsNone(limits.parse_reset(text, now))

    def test_fmt_time(self):
        now = self.at("2026-11-02 18:28")
        self.assertEqual(limits.fmt_time(self.at("2026-11-02 19:39"), now), "7:39 PM IST")
        self.assertEqual(limits.fmt_time(self.at("2026-11-03 09:05"), now), "Nov 3, 9:05 AM IST")


class LimitStoreTests(TempHome):
    def test_remember_defaults_and_caps(self):
        now = time.time()
        self.assertEqual(limits.remember("codex", now + 600, "Codex: limit", now=now), now + 600)
        self.assertEqual(limits.remember("grok", None, "Grok: balance exhausted", now=now), now + limits.DEFAULT_S)
        self.assertEqual(limits.remember("claude", now + 5 * 86400, "", now=now), now + limits.MAX_S)
        self.assertEqual(set(limits.active()), {"codex", "grok", "claude"})
        self.assertEqual((self.home / "limits.json").stat().st_mode & 0o777, 0o600)

    def test_reset_and_corrupt_entries_are_dropped(self):
        (self.home / "limits.json").write_text("not json")
        self.assertEqual(limits.active(), {})
        now = time.time()
        limits.remember("codex", now + 60, now=now)
        limits.remember("grok", now + 120, now=now + 100)  # Codex has reset by then
        self.assertEqual(set(json.loads((self.home / "limits.json").read_text())), {"grok"})

    def test_messages_are_redacted(self):
        limits.remember("codex", None, "token ghp_" + "a" * 30)
        self.assertNotIn("ghp_", (self.home / "limits.json").read_text())


class HealthTests(TempHome):
    def fake(self, name: str, script: str):
        path = self.home / f"fake-{name}"
        path.write_text("#!/bin/sh\n" + script + "\n")
        path.chmod(0o755)
        return mock.patch.dict(os.environ, {f"CODETURTLE_{name.upper()}_BIN": str(path)})

    def test_status_from_each_cli(self):
        cases = [
            ("codex", 'echo "Logged in using ChatGPT" >&2', "ok"),
            ("codex", 'echo "Not logged in" >&2; exit 1', "logged_out"),
            ("claude", "echo '{\"loggedIn\": true, \"authMethod\": \"oauth_token\"}'", "ok"),
            ("claude", "echo '{\"loggedIn\": false}'; exit 1", "logged_out"),
            ("grok", 'echo "You are logged in with grok.com."', "ok"),
            ("grok", 'echo "boom"; exit 3', "unknown"),
        ]
        for name, script, status in cases:
            with self.subTest(name=name, status=status), self.fake(name, script):
                self.assertEqual(health.check(name).status, status)

    def test_missing_cli(self):
        with mock.patch.dict(os.environ, {"CODETURTLE_GROK_BIN": str(self.home / "nope")}):
            h = health.check("grok")
        self.assertEqual(h.status, "missing")
        self.assertIn("CODETURTLE_GROK_BIN", h.warning())

    def test_warning_names_the_fix(self):
        h = health.Health("claude", "logged_out", "not logged in")
        self.assertTrue(h.broken)
        self.assertIn("claude setup-token", h.warning())
        self.assertFalse(health.Health("codex", "unknown").broken)

    def test_doctor_exit_status(self):
        lines = []
        found = {"codex": health.Health("codex", "ok", "Logged in"), "claude": health.Health("claude", "logged_out", "x")}
        with mock.patch.object(health, "check_all", return_value=found):
            self.assertEqual(health.doctor(["codex", "claude"], DEFAULTS["models"], out=lines.append), 1)
        self.assertIn("NOT LOGGED IN", "\n".join(lines))
        found["claude"] = health.Health("claude", "ok", "logged in")
        with mock.patch.object(health, "check_all", return_value=found):
            self.assertEqual(health.doctor(["codex", "claude"], DEFAULTS["models"], out=lines.append), 0)


class GhRetryTests(unittest.TestCase):
    NO_ROUTE = b'Get "https://api.github.com/x": dial tcp 20.207.73.85:443: connect: no route to host'

    def setUp(self):
        for name, value in (("_sleep", mock.Mock()), ("_log", lambda m: None)):
            patcher = mock.patch.object(github, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def proc(self, code, out=b"", err=b""):
        return subprocess.CompletedProcess(["gh"], code, out, err)

    def test_network_blip_is_retried(self):
        fail = self.proc(1, err=self.NO_ROUTE)
        with mock.patch.object(github.subprocess, "run", side_effect=[fail, fail, self.proc(0, b'{"a": 1}')]) as run:
            self.assertEqual(github.api("repos/o/r/pulls/1"), {"a": 1})
        self.assertEqual(run.call_count, 3)
        self.assertEqual([c.args[0] for c in github._sleep.call_args_list], [5, 15])

    def test_writes_are_retried_only_when_the_request_never_left(self):
        reset = self.proc(1, err=b"read tcp: connection reset by peer")
        with mock.patch.object(github.subprocess, "run", side_effect=[reset, self.proc(0, b'{"id": 7}')]) as run:
            with self.assertRaises(github.GitHubError):
                github.post_comment("o/r", 1, "hi")
        self.assertEqual(run.call_count, 1)
        with mock.patch.object(github.subprocess, "run", side_effect=[self.proc(1, err=self.NO_ROUTE), self.proc(0, b'{"id": 7}')]):
            self.assertEqual(github.post_comment("o/r", 1, "hi"), 7)

    def test_server_errors_are_retried_for_reads_but_404_is_not(self):
        with mock.patch.object(github.subprocess, "run", side_effect=[self.proc(1, err=b"gh: Server Error (HTTP 502)"),
                                                                      self.proc(0, b"{}")]) as run:
            github.api("repos/o/r")
        self.assertEqual(run.call_count, 2)
        with mock.patch.object(github.subprocess, "run", return_value=self.proc(1, err=b"gh: Not Found (HTTP 404)")) as run:
            self.assertIsNone(github.file_at_ref("o/r", "x.json", "sha"))
        self.assertEqual(run.call_count, 1)

    def test_gives_up_after_the_last_retry_and_cosmetic_calls_never_retry(self):
        with mock.patch.object(github.subprocess, "run", return_value=self.proc(1, err=self.NO_ROUTE)) as run:
            with self.assertRaises(github.GitHubError):
                github.api("repos/o/r")
            self.assertEqual(run.call_count, 4)
            run.reset_mock()
            with self.assertRaises(github.GitHubError):
                github.edit_comment("o/r", 5, "body", retry=False)
            self.assertEqual(run.call_count, 1)


class ReviewerTests(TempHome):
    PR = {"head": {"repo": {"full_name": "o/r"}, "sha": "a" * 40, "ref": "feat"},
          "base": {"sha": "b" * 40, "ref": "main"}, "state": "open", "labels": [], "title": "t", "body": "",
          "user": {"login": "dev"}}
    FILES = [{"filename": "a.py", "status": "modified", "additions": 1, "deletions": 0,
              "patch": "@@ -1,1 +1,2 @@\n x = 1\n+y = 2"}]

    def review(self, runners, found):
        result_path = self.home / "result.json"
        gh = mock.patch.multiple(github, get_pr=mock.DEFAULT, file_at_ref=mock.DEFAULT, get_files=mock.DEFAULT,
                                 post_comment=mock.DEFAULT, edit_comment=mock.DEFAULT, delete_comment=mock.DEFAULT,
                                 list_comments=mock.DEFAULT, post_review=mock.DEFAULT)
        with gh as m, mock.patch.object(health, "check_all", return_value=found), \
                mock.patch.dict(providers.RUNNERS, runners, clear=True), \
                mock.patch.dict(os.environ, {"CODETURTLE_RESULT_PATH": str(result_path)}), \
                mock.patch.object(review_mod, "log", lambda msg: None):
            os.environ.pop("GITHUB_STEP_SUMMARY", None)
            m["get_pr"].return_value = self.PR
            m["file_at_ref"].return_value = None
            m["get_files"].return_value = self.FILES
            m["post_comment"].return_value = 11
            m["list_comments"].return_value = []
            m["post_review"].return_value = {"id": 5, "html_url": "https://example.test/review/5"}
            code = Reviewer(Request("o/r", 1, "dispatch")).run()
        return code, m, json.loads(result_path.read_text())

    def test_broken_backup_is_named_on_the_review(self):
        def codex(call, model):
            answers = {"triage": {"tier": "fast", "reason": "small"}, "security": {"summary": "s", "candidates": []}}
            return answers.get(call.schema, OK)

        def never(call, model):
            raise AssertionError("not needed")

        found = {"codex": health.Health("codex", "ok"), "grok": health.Health("grok", "ok"),
                 "claude": health.Health("claude", "logged_out", "claude auth status: not logged in")}
        code, m, result = self.review({"codex": codex, "grok": never, "claude": never}, found)
        self.assertEqual(code, 0)
        body = m["post_review"].call_args.args[2]["body"]
        self.assertIn("Claude can't review on this runner: it is not logged in", body)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["notes"][0].startswith("⚠️ Claude"))
        self.assertEqual(result["reviewers"]["health"]["claude"]["status"], "logged_out")
        self.assertIsNone(result["retry_at"])

    def test_every_provider_limited_reports_when_to_retry(self):
        reset = time.time() + 3 * 3600
        runners = {name: runner(providers.LimitHit(f"{name}: limit", reset)) for name in ("codex", "grok", "claude")}
        found = {name: health.Health(name, "ok") for name in runners}
        code, m, result = self.review(runners, found)
        self.assertEqual(code, 1)
        comments = [c.args[2] for c in m["post_comment"].call_args_list]
        self.assertTrue(any("No reviewer could run" in c and "try again after" in c for c in comments))
        self.assertEqual(result["status"], "usage_limited")
        self.assertIsNotNone(result["retry_at"])
        self.assertEqual(result["reviewers"]["usage_limited"], ["Codex", "Grok", "Claude"])
        self.assertEqual(set(limits.active()), {"codex", "grok", "claude"})


class ResultFieldTests(unittest.TestCase):
    def test_retry_at_is_additive(self):
        writer = ResultWriter(Request("o/r", 1, "dispatch"))
        writer.retry_at = "2026-11-02T14:09:00Z"
        payload = writer.payload("usage_limited")
        self.assertEqual(payload["retry_at"], "2026-11-02T14:09:00Z")
        self.assertEqual(payload["schema_version"], 1)


if __name__ == "__main__":
    unittest.main()
