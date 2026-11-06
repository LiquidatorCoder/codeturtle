"""Review quality and cost (1.4): production-line sizing and risk-matched passes, verification of
blockers and majors, fix-rate stats, and incremental follow-up reviews."""

import unittest

from codeturtle import security, verify
from codeturtle.config import DEFAULTS
from codeturtle.events import Request
from codeturtle.providers import Chain
from codeturtle.review import Reviewer
from codeturtle.tiers import TierDecision, floor_tier

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


if __name__ == "__main__":
    unittest.main()
