"""Review quality and cost (1.4): production-line sizing and risk-matched passes, verification of
blockers and majors, fix-rate stats, and incremental follow-up reviews."""

import unittest

from codeturtle.config import DEFAULTS
from codeturtle.events import Request
from codeturtle.review import Reviewer
from codeturtle.tiers import TierDecision, floor_tier

from tests.fakes import OK, FakeRun, api_file, change


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


if __name__ == "__main__":
    unittest.main()
