"""Review quality and cost (1.4): production-line sizing and risk-matched passes, verification of
blockers and majors, fix-rate stats, and incremental follow-up reviews."""

import unittest
from unittest import mock

from codeturtle import followup, security, stats, verify
from codeturtle.config import DEFAULTS
from codeturtle.events import Request, from_event
from codeturtle.providers import Chain
from codeturtle.review import Reviewer
from codeturtle.tiers import TierDecision, floor_tier, parse_command, wants_fresh

from tests.fakes import OK, FakeRun, api_file, change, finding


class SizeRuleTests(unittest.TestCase):
    rules = DEFAULTS["tiers"]

    def test_only_production_lines_count(self):
        files = [change("app/test/widget_test.dart", 900), change("lib/a.dart", 60), change("README.md", 300)]
        self.assertEqual(floor_tier(files, self.rules), ("normal", "60 changed production lines across 1 files"))
        self.assertEqual(floor_tier([change("src/a.ts", 801)], self.rules)[0], "full")

    def test_tests_only_change_is_fast(self):
        self.assertEqual(floor_tier([change("tests/test_a.py", 300), change("docs/x.md", 5)], self.rules),
                         ("fast", "only tests, docs or assets changed"))


class RiskSplitTests(unittest.TestCase):
    def split(self, files, source="rules"):
        reviewer = Reviewer(Request("o/r", 1, "dispatch"))
        reviewer.files = files
        full, rest = reviewer.risk_split(TierDecision("full", source, "r"))
        return [f.path for f in full], [f.path for f in rest]

    def test_sensitive_files_get_full_passes_and_the_rest_a_normal_one(self):
        files = [change(".github/workflows/ci.yml", 3), change("lib/a.dart", 120), change("lib/b.dart", 30)]
        self.assertEqual(self.split(files), ([".github/workflows/ci.yml"], ["lib/a.dart", "lib/b.dart"]))

    def test_no_split_when_full_was_asked_for_or_is_due_to_size(self):
        files = [change(".github/workflows/ci.yml", 3), change("lib/a.dart", 120)]
        self.assertEqual(self.split(files, "override")[1], [])
        self.assertEqual(self.split(files, "triage")[1], [])
        self.assertEqual(self.split([change("auth/x.py", 3), change("lib/a.dart", 900)])[1], [])
        self.assertEqual(self.split([change("auth/x.py", 3)])[1], [])  # nothing else to split off

    def test_split_full_review_runs(self):
        run = FakeRun(self)
        files = [api_file(".github/workflows/ci.yml", "on: push"), api_file("lib/a.dart", "\n".join(["a"] * 50))]

        def answer(call, provider):
            return {"summary": "s", "candidates": []} if call.schema == "security" else OK

        code, payload, result = run.run(files, answer)
        self.assertEqual(code, 0)
        self.assertEqual(sorted(run.kinds()), ["full:correctness", "full:rest", "full:security", "full:tests"])
        rest = run.call("full:rest")
        self.assertEqual((rest["effort"], rest["read_repo"]), ("medium", False))
        self.assertIn("lib/a.dart", rest["prompt"])
        self.assertNotIn("lib/a.dart", run.call("full:correctness")["prompt"].split("Diff (R<n>")[1])
        self.assertIn("lib/a.dart", run.call("full:security")["prompt"])  # security sees everything
        self.assertEqual(result["tier"]["name"], "full")
        self.assertIn("normal-depth review on the other 1", payload["body"])


class MergeEffortTests(unittest.TestCase):
    def test_merge_runs_at_low_effort(self):
        self.assertEqual(DEFAULTS["effort"]["merge"], "low")


class VerifyTests(unittest.TestCase):
    def test_verdicts_drop_downgrade_and_never_raise(self):
        bugs = verify.bug_items([finding("a", "major"), finding("b", "major"), finding("c", "blocker"),
                                 finding("d", "major"), finding("e", "major"), finding("f", "major")])
        verdicts = verify.by_id([
            {"id": "b1", "verdict": "confirmed", "severity": "medium", "reason": "reachable from /search"},
            {"id": "b2", "verdict": "confirmed", "severity": "low", "reason": "only on retry"},
            {"id": "b3", "verdict": "rejected", "severity": "none", "reason": "guarded at a.py:4"},
            {"id": "b4", "verdict": "confirmed", "severity": "high", "reason": "worse than claimed"},
            {"id": "b5", "verdict": "needs_validation", "severity": "none", "unresolved": "is the cache shared?"},
        ])
        report = verify.Report(checked=len(bugs))
        kept = verify.apply(bugs, verdicts, report)
        self.assertEqual([(f["title"], f["severity"]) for f in kept],
                         [("a", "major"), ("b", "minor"), ("d", "major"), ("f", "major")])
        self.assertIn("**Verified:** reachable from /search", kept[0]["body"])
        self.assertEqual((report.confirmed, report.downgraded, report.rejected, report.unchecked), (2, 1, 1, 1))
        self.assertEqual(report.leads[0]["unresolved"], "is the cache shared?")

    def test_prompt_has_only_the_methods_it_needs(self):
        bugs = verify.bug_items([finding("a")])
        text = verify.prompt(pr={"repo": "o/r", "number": 1, "title": "t", "body": "", "author": "a"},
                             security_items=[], bugs=bugs, diff_text="d")
        self.assertIn(verify.BUG_METHOD, text)
        self.assertNotIn(security.VERIFY_METHOD, text)
        self.assertIn('"claimed_severity": "medium"', text)

    def test_chain_prefers_a_different_provider(self):
        chain = Chain(["codex", "grok", "claude"], DEFAULTS["models"], log=lambda m: None)
        self.assertEqual(chain.order(avoid={"codex"}), ["grok", "claude", "codex"])
        chain.broken = {"grok": "logged out"}
        self.assertEqual(chain.order(avoid={"codex"}), ["claude", "codex", "grok"])
        chain.broken = {"grok": "x", "claude": "x"}
        self.assertEqual(chain.order(avoid={"codex"})[0], "codex")  # the same provider beats a broken one

    def answers(self, verdicts):
        def answer(call, provider):
            if call.schema == "security":
                return {"summary": "s", "candidates": []}
            if call.schema == "verify":
                return {"verdicts": verdicts}
            return {"summary": "Adds search.", "findings": [finding("Cursor leaks", "major"),
                                                            finding("Typo in log", "minor", line=3)]}
        return answer

    def test_a_different_provider_drops_an_unconfirmed_major(self):
        run = FakeRun(self)
        rejected = [{"id": "b1", "verdict": "rejected", "severity": "none", "reason": "closed by the context manager",
                     "unresolved": "", "fix": ""}]
        code, payload, result = run.run([api_file("api/search.py", "q = 1\nr = 2")], self.answers(rejected),
                                        request=Request("o/r", 1, "dispatch", "normal"))
        self.assertEqual(code, 0)
        self.assertEqual(run.call("normal")["provider"], "codex")
        self.assertEqual(run.call("verify")["provider"], "grok")
        self.assertIn("**Verified by Grok (grok-4.7):** 1 blocker and major findings · 0 confirmed · 1 dropped",
                      payload["body"])
        self.assertEqual([f["title"] for f in result["findings"]], ["Typo in log"])  # the minor is not checked
        self.assertEqual(result["verification"]["rejected"], 1)

    def test_same_provider_when_no_other_can_run(self):
        run = FakeRun(self)
        confirmed = [{"id": "b1", "verdict": "confirmed", "severity": "medium", "reason": "r", "unresolved": "",
                      "fix": ""}]
        code, payload, result = run.run([api_file("api/search.py", "q = 1\nr = 2")], self.answers(confirmed),
                                        request=Request("o/r", 1, "dispatch", "normal"), providers_ok=("codex",))
        self.assertEqual(run.call("verify")["provider"], "codex")
        self.assertIn("the same provider as the review", payload["body"])
        self.assertTrue(result["verification"]["same_provider"])


class StatsTests(unittest.TestCase):
    def thread(self, body, outdated=False, resolved=False, author="github-actions",
               review="### 🐢 codeturtle · Full review\n\n<sub>codeturtle v1.3.0</sub>"):
        return {"isOutdated": outdated, "isResolved": resolved, "comments": {"nodes": [
            {"author": {"login": author}, "body": body, "pullRequestReview": {"body": review}}]}}

    def test_counts_addressed_findings(self):
        data = {"repository": {"pullRequests": {"nodes": [{"number": 3, "state": "MERGED", "reviewThreads": {"nodes": [
            self.thread("🔴 **Major** · correctness — **Cursor leaks**\n\nbody", outdated=True),
            self.thread("🟡 **Minor** · tests — **No test for empty input**"),
            self.thread("🛑 **Blocker** · security — **SQL injection**", resolved=True),
            self.thread("🔴 **Major** · correctness — **Not ours**", author="dev"),  # a human's thread
            self.thread("just a reply"),
        ]}}]}}}
        with mock.patch.object(stats.github, "graphql", return_value=data):
            records = stats.fetch("o/r", 10)
        agg = stats.aggregate(records)
        self.assertEqual((agg["posted"], agg["addressed"], agg["prs"]), (3, 2, 1))
        self.assertEqual(agg["by_severity"][0], {"severity": "blocker", "posted": 1, "addressed": 1})
        self.assertEqual(agg["by_version"], [{"version": "1.3.0", "posted": 3, "addressed": 2}])
        text = stats.render(agg, ["o/r"], 10)
        self.assertIn("Inline findings: 3 on 1 PRs · addressed 2 (67%)", text)
        self.assertIn("tests", text)


PREV = "c" * 40
F1 = {"id": "ct-111111111111", "severity": "major", "category": "security", "path": "api/search.py", "line": 2,
      "title": "SQL built from input", "body": "b1"}
F2 = {"id": "ct-222222222222", "severity": "minor", "category": "tests", "path": "lib/other.py", "line": 5,
      "title": "No test for empty input", "body": "b2"}


def bot_review(sha=PREV, findings=(F1, F2), login="github-actions[bot]"):
    return {"user": {"login": login}, "body": "### 🐢 codeturtle · Normal review\n\n"
            + followup.encode_state(sha, list(findings))}


class FollowupStateTests(unittest.TestCase):
    def test_state_round_trip_and_trust(self):
        state = followup.latest_state([bot_review()])
        self.assertEqual(state["sha"], PREV)
        self.assertEqual([f["id"] for f in state["findings"]], [F1["id"], F2["id"]])
        # Only codeturtle's own bot can leave a state; anyone else's marker is ignored.
        self.assertIsNone(followup.latest_state([bot_review(login="dev")]))
        self.assertIsNone(followup.decode_state("<!-- codeturtle:state not-base64!! -->"))
        newer = bot_review(sha="d" * 40, findings=[F2])
        self.assertEqual(followup.latest_state([bot_review(), newer])["sha"], "d" * 40)

    def test_fresh_command(self):
        self.assertEqual(parse_command("/review fresh"), (True, None))
        self.assertEqual(parse_command("/review fresh FULL"), (True, "full"))
        self.assertTrue(wants_fresh("/review fresh full"))
        self.assertFalse(wants_fresh("/review full"))
        event = {"action": "created", "issue": {"number": 4, "pull_request": {}},
                 "comment": {"id": 9, "body": "/review fresh", "author_association": "OWNER"}}
        self.assertTrue(from_event("issue_comment", event, "o/r")[0].fresh)

    def test_plan_sorts_earlier_findings(self):
        plan = followup.Plan(since=PREV, previous=[F1, F2], delta_paths={"api/search.py"})
        self.assertEqual(plan.to_check(), [F1])
        plan.apply({F1["id"]: {"id": F1["id"], "status": "fixed", "line": 0, "note": "now bound"}})
        self.assertEqual(([f["id"] for f in plan.fixed], [f["id"] for f in plan.open]), ([F1["id"]], [F2["id"]]))
        unchecked = followup.Plan(since=PREV, previous=[F1, F2], delta_paths={"api/search.py"})
        unchecked.apply(None)  # the re-check failed: everything stays open
        self.assertEqual(len(unchecked.open), 2)
        moved = followup.Plan(since=PREV, previous=[F1], delta_paths={"api/search.py"})
        moved.apply({F1["id"]: {"id": F1["id"], "status": "open", "line": 7, "note": "still built by hand"}})
        self.assertEqual(moved.open[0]["line"], 7)
        self.assertEqual(moved.drop_repeats([finding("SQL built from input", line=40), finding("Other", line=20)]),
                         [finding("Other", line=20)])


class FollowupFlowTests(unittest.TestCase):
    def answer(self, call, provider):
        if call.schema == "followup":
            return {"summary": "One fixed.", "previous": [
                {"id": F1["id"], "status": "fixed", "line": 0, "note": "the query is parameterized"}]}
        if call.schema == "security":
            return {"summary": "s", "candidates": []}
        if call.schema == "verify":
            return {"verdicts": []}
        return {"summary": "Adds a timeout.", "findings": [finding("Timeout is never cancelled", "minor", line=3)]}

    def gh(self, status="ahead"):
        return {
            "get_files": [api_file("api/search.py", "q = 1\nfixed = True"), api_file("lib/other.py", "x = 1")],
            "list_reviews": [bot_review()],
            "compare": {"status": status, "files": [api_file("api/search.py", "fixed = True")]},
            "review_threads": [{"id": "T1", "isResolved": False, "comments": {"nodes": [
                {"databaseId": 99, "body": "🔴 **Major** ...\n\n" + followup.FINDING_MARK.format(id=F1["id"])}]}}],
        }

    def test_follow_up_reviews_the_delta_and_resolves_fixed_threads(self):
        run = FakeRun(self)
        code, payload, result = run.run(None, self.answer, request=Request("o/r", 1, "dispatch", "normal"),
                                        gh=self.gh())
        self.assertEqual(code, 0)
        self.assertIn("followup", run.kinds())
        diff = run.call("normal")["prompt"].split("Diff (R<n>")[1]
        self.assertIn("api/search.py", diff)
        self.assertNotIn("lib/other.py", diff)  # unchanged since the last review
        body = payload["body"]
        self.assertIn("### 🐢 codeturtle · Normal follow-up review", body)
        self.assertIn("**Since `ccccccc`:** ✅ 1 fixed · 🔁 1 still open · 🆕 1 new", body)
        self.assertIn("Still open from earlier reviews (1)", body)
        self.assertEqual(len(payload["comments"]), 1)  # only the new finding goes inline
        self.assertIn("<!-- codeturtle:finding ct-", payload["comments"][0]["body"])
        m = run.mocks
        m["reply_to_comment"].assert_called_once()
        self.assertIn("Fixed as of `aaaaaaa`: the query is parameterized", m["reply_to_comment"].call_args.args[3])
        m["resolve_thread"].assert_called_once_with("T1")
        placements = {f["id"]: f["placement"] for f in result["findings"]}
        self.assertEqual(placements[F2["id"]], "earlier")
        self.assertEqual(result["followup"]["fixed"], [F1["id"]])
        state = followup.decode_state(body)
        self.assertEqual(state["sha"], "a" * 40)
        self.assertEqual({f["title"] for f in state["findings"]}, {F2["title"], "Timeout is never cancelled"})

    def test_rewritten_history_gets_a_whole_review(self):
        run = FakeRun(self)
        code, payload, result = run.run(None, self.answer, request=Request("o/r", 1, "dispatch", "normal"),
                                        gh=self.gh(status="diverged"))
        self.assertNotIn("followup", run.kinds())
        self.assertIn("lib/other.py", run.call("normal")["prompt"])
        self.assertIsNone(result["followup"])

    def test_fresh_skips_the_follow_up(self):
        run = FakeRun(self)
        run.run(None, self.answer, request=Request("o/r", 1, "dispatch", "normal", fresh=True), gh=self.gh())
        run.mocks["list_reviews"].assert_not_called()
        self.assertNotIn("followup", run.kinds())


if __name__ == "__main__":
    unittest.main()
