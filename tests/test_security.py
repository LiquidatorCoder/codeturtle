"""The security check on every review (security.py, adapted from Cloudflare's security-audit skill)."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import codeturtle.review as review_mod
from codeturtle import github, health, post, prompts, providers, security
from codeturtle.diff import FileChange
from codeturtle.events import Request
from codeturtle.review import Reviewer

PR = {"repo": "o/r", "number": 7, "title": "t", "body": "", "author": "dev"}


def change(path, added="x = 1", status="modified"):
    patch = "@@ -1,1 +1,2 @@\n context\n" + "\n".join("+" + line for line in added.splitlines())
    return FileChange(path, status, len(added.splitlines()), 0, patch)


def candidate(**kw):
    base = {"proposed_verdict": "confirmed", "severity": "high", "attack_class": "injection", "path": "a.py",
            "line": 2, "title": "SQL built from the search box", "principal": "any signed-in user",
            "boundary": "no parameterized query", "trace": "a.py:2 -> db.py:10", "impact": "reads every tenant",
            "fix": "bind the parameter", "unresolved": ""}
    return {**base, **kw}


class CompanionTests(unittest.TestCase):
    def test_picked_from_paths_and_added_lines(self):
        files = [
            change(".github/workflows/ci.yml", "on: pull_request_target"),
            change("backend/src/auth/session.ts", "res.setHeader('Set-Cookie', token)"),
            change("app/lib/chat/client.dart", "final reply = await anthropic.messages.create()"),
        ]
        keys = [c.key for c in security.select_companions(files, limit=10)]
        self.assertEqual(set(keys[:2]), {"supply", "web"})  # path and content both match: 3 points each
        self.assertIn("ai", keys)

    def test_docs_select_nothing_and_the_limit_holds(self):
        self.assertEqual(security.select_companions([change("README.md", "Hello")]), [])
        files = [change("x/auth/a.ts", "cookie"), change("db/migrations/1.sql", "tenant_id"),
                 change(".github/workflows/a.yml", "secrets.TOKEN"), change("ios/App/AppDelegate.swift", "openURL")]
        self.assertEqual(len(security.select_companions(files, limit=2)), 2)

    def test_lockfile_path_counts_but_its_content_does_not(self):
        lock = change("package-lock.json", "postinstall")
        lock.ignored = True
        self.assertEqual([c.key for c in security.select_companions([lock])], ["supply"])


class CandidateTests(unittest.TestCase):
    def test_normalize_cleans_dedupes_and_numbers(self):
        raw = [candidate(), candidate(title="same line, other words"), candidate(line=9, severity="none"),
               {"title": "no verdict"}, "junk", candidate(path="./b.py", line=0, proposed_verdict="needs_validation",
                                                          severity="none", unresolved="proxy strips the header?")]
        out = security.normalize(raw)
        self.assertEqual([c["id"] for c in out], ["c1", "c2", "c3"])
        self.assertEqual(out[1]["proposed_verdict"], "needs_validation")  # confirmed with no severity
        self.assertEqual(out[2]["path"], "b.py")

    def test_findings_from_other_passes_become_candidates(self):
        out = security.normalize(security.from_findings([
            {"severity": "major", "category": "security", "path": "a.py", "line": 3, "title": "t", "body": "b"}]))
        self.assertEqual((out[0]["severity"], out[0]["impact"]), ("medium", "b"))

    def test_verdicts(self):
        cands = security.normalize([candidate(), candidate(line=5, severity="medium"), candidate(line=6),
                                    candidate(line=7)])
        verdicts = [
            {"id": "c1", "verdict": "confirmed", "severity": "high", "reason": "route has no auth", "unresolved": "", "fix": ""},
            {"id": "c2", "verdict": "confirmed", "severity": "medium", "reason": "r", "unresolved": "", "fix": "f2"},
            {"id": "c3", "verdict": "rejected", "severity": "none", "reason": "ORM binds it", "unresolved": "", "fix": ""},
        ]
        confirmed, leads, rejected = security.apply_verdicts(cands, verdicts)
        self.assertEqual([f["severity"] for f in confirmed], ["blocker", "major"])
        self.assertEqual(rejected, 1)
        self.assertEqual(len(leads), 1)  # c4 got no verdict: a lead, never a posted finding
        self.assertIn("no verdict", leads[0]["unresolved"])
        body = confirmed[0]["body"]
        for part in ("reads every tenant", "**Path:** a.py:2", "**Boundary:**", "**Verified:** route has no auth",
                     "**Fix:** bind the parameter"):
            self.assertIn(part, body)
        self.assertEqual(confirmed[1]["body"].split("**Fix:** ")[1], "f2")  # the verifier's fix wins

    def test_needs_validation_keeps_the_missing_fact(self):
        cands = security.normalize([candidate()])
        _, leads, _ = security.apply_verdicts(cands, [{"id": "c1", "verdict": "needs_validation", "severity": "none",
                                                       "reason": "", "unresolved": "is the admin route behind the VPN?",
                                                       "fix": ""}])
        self.assertEqual(leads[0]["unresolved"], "is the admin route behind the VPN?")

    def test_unverified_posts_proposed_confirmations_only(self):
        cands = security.normalize([candidate(), candidate(line=4, proposed_verdict="needs_validation", severity="none")])
        confirmed, leads = security.unverified(cands)
        self.assertEqual((len(confirmed), len(leads)), (1, 1))


class PromptTests(unittest.TestCase):
    def test_security_pass_prompt(self):
        comps = security.select_companions([change("api/auth/login.ts", "cookie")])
        text = security.pass_prompt(pr=PR, depth="repo", diff_text="R1 + x", companions=comps,
                                    notes={".github/codeturtle-security.md": "Tenants never share rows."})
        for part in (security.RULES, security.METHOD, security.DEPTH["repo"], "### HTTP, sessions and identity",
                     "Tenants never share rows.", '<untrusted kind="diff">'):
            self.assertIn(part, text)

    def test_verify_prompt_marks_candidates_untrusted(self):
        text = security.verify_prompt(pr=PR, candidates=security.normalize([candidate()]), diff_text="R1 + x")
        self.assertIn('<untrusted kind="candidates">', text)
        self.assertIn('"id": "c1"', text)
        self.assertIn("You did\nnot write these candidates", text)

    def test_every_review_prompt_carries_the_security_bar(self):
        for tier in ("fast", "normal", "full"):
            self.assertIn(prompts.SECURITY_BAR, prompts.review_prompt(pr=PR, tier=tier, diff_text="d"))


class ReportTests(unittest.TestCase):
    def test_summary_line_and_body(self):
        rep = security.Report(dedicated=True, companions=["Supply chain, CI and release"], candidates=3,
                              confirmed=1, rejected=1, verified=True,
                              leads=[{"path": "a.yml", "line": 4, "title": "Fork code with secrets?",
                                      "attack_class": "ci", "unresolved": "is the fork-PR approval required?"}])
        self.assertEqual(rep.summary_line(), "**Security:** dedicated pass (Supply chain, CI and release) · "
                                             "1 confirmed · 1 needs validation · 1 rejected by an independent check")
        body = post.build_body(tier="fast", tier_reason="r", reviewers="Codex", summary="s", findings=[], inline=[],
                               rest=[], head_sha="a" * 40, notes=[], security=rep)
        self.assertIn(rep.summary_line(), body)
        self.assertIn("Need validation (1)", body)
        self.assertIn("Check: is the fork-PR approval required?", body)
        quiet = security.Report(skipped="only docs or assets changed")
        self.assertEqual(quiet.summary_line(),
                         "**Security:** checked by the main review (only docs or assets changed) · no issues found")


class ReviewFlowTests(unittest.TestCase):
    PR = {"head": {"repo": {"full_name": "o/r"}, "sha": "a" * 40, "ref": "feat"},
          "base": {"sha": "b" * 40, "ref": "main"}, "state": "open", "labels": [], "title": "t", "body": "",
          "user": {"login": "dev"}}
    CODE = [{"filename": "api/search.py", "status": "modified", "additions": 1, "deletions": 0,
             "patch": "@@ -1,1 +1,2 @@\n x = 1\n+q = f'select * from t where n = {term}'"}]
    DOCS = [{"filename": "README.md", "status": "modified", "additions": 1, "deletions": 0,
             "patch": "@@ -1,1 +1,2 @@\n # r\n+more"}]

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        patcher = mock.patch.dict(os.environ, {"CODETURTLE_HOME": tmp.name,
                                               "CODETURTLE_RESULT_PATH": str(self.home / "result.json")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.calls = []

    def fake_snapshot(self, repo, sha, dest):
        root = Path(dest) / "r-sha"
        (root / "api").mkdir(parents=True)
        (root / "api" / "search.py").write_text("x = 1\n")
        return root

    def review(self, files, answers, tier="fast"):
        """answers: schema -> dict, or an exception to raise."""
        def codex(call, model):
            self.calls.append((call.kind, call.schema, call.read_repo, call.prompt))
            answer = answers[call.schema]
            if isinstance(answer, Exception):
                raise answer
            return answer

        def never(call, model):
            raise AssertionError("not needed")

        gh = mock.patch.multiple(github, get_pr=mock.DEFAULT, file_at_ref=mock.DEFAULT, get_files=mock.DEFAULT,
                                 post_comment=mock.DEFAULT, edit_comment=mock.DEFAULT, delete_comment=mock.DEFAULT,
                                 list_comments=mock.DEFAULT, post_review=mock.DEFAULT,
                                 list_reviews=mock.DEFAULT,
                                 download_snapshot=mock.DEFAULT)
        found = {n: health.Health(n, "ok") for n in ("codex", "grok", "claude")}
        with gh as m, mock.patch.object(health, "check_all", return_value=found), \
                mock.patch.dict(providers.RUNNERS, {"codex": codex, "grok": codex, "claude": codex}, clear=True), \
                mock.patch.object(review_mod, "log", lambda msg: None):
            os.environ.pop("GITHUB_STEP_SUMMARY", None)
            m["get_pr"].return_value = self.PR
            m["file_at_ref"].return_value = None
            m["get_files"].return_value = files
            m["post_comment"].return_value = 11
            m["list_comments"].return_value = []
            m["list_reviews"].return_value = []
            m["post_review"].return_value = {"id": 5, "html_url": "https://example.test/review/5"}
            m["download_snapshot"].side_effect = self.fake_snapshot
            code = Reviewer(Request("o/r", 1, "dispatch", tier)).run()
            payload = m["post_review"].call_args.args[2] if m["post_review"].called else {"body": "", "comments": []}
        # The review body plus its inline comments: everything the PR shows.
        text = "\n".join([payload["body"], *(c["body"] for c in payload["comments"])])
        return code, text, json.loads((self.home / "result.json").read_text())

    MAIN = {"summary": "Adds search.", "findings": [
        {"severity": "major", "category": "security", "path": "api/search.py", "line": 2,
         "title": "SQL from user input", "body": "string-built query"},
        {"severity": "major", "category": "correctness", "path": "api/search.py", "line": 2,
         "title": "Unclosed cursor", "body": "leaks"}]}
    SECURITY = {"summary": "Search input reaches SQL.", "candidates": [
        candidate(path="api/search.py", line=2, title="SQL injection in search"),
        candidate(path="api/search.py", line=0, title="Admin search may skip tenant scope",
                  proposed_verdict="needs_validation", severity="none", unresolved="which role reaches /admin?")]}
    VERIFY = {"verdicts": [
        {"id": "c1", "verdict": "confirmed", "severity": "high", "reason": "term flows to execute()",
         "unresolved": "", "fix": "use a bound parameter"},
        {"id": "c2", "verdict": "needs_validation", "severity": "none", "reason": "",
         "unresolved": "which role reaches /admin?", "fix": ""}]}

    def test_fast_review_runs_a_verified_security_pass(self):
        code, body, result = self.review(self.CODE, {"review": self.MAIN, "security": self.SECURITY,
                                                     "verify": self.VERIFY})
        self.assertEqual(code, 0)
        kinds = [(k, s) for k, s, *_ in self.calls]
        self.assertIn(("security", "security"), kinds)
        self.assertEqual(kinds[-1], ("verify", "verify"))
        self.assertTrue(self.calls[-1][2])  # the verifier reads the (lazily downloaded) snapshot
        self.assertIn("SQL injection in search", body)
        self.assertNotIn("SQL from user input", body)  # the main pass's duplicate on the same line
        self.assertIn("1 confirmed · 1 needs validation · 0 rejected by an independent check", body)
        self.assertIn("which role reaches /admin?", body)
        sec = result["security"]
        self.assertEqual((sec["candidates"], sec["confirmed"], sec["verified"]), (2, 1, True))
        self.assertTrue(sec["needs_validation"][0]["id"].startswith("ct-"))
        self.assertEqual(sorted(f["severity"] for f in result["findings"]), ["blocker", "major"])

    def test_failed_security_pass_does_not_sink_the_review(self):
        code, body, result = self.review(self.CODE, {"review": self.MAIN, "security": providers.ProviderError("boom"),
                                                     "verify": self.VERIFY})
        self.assertEqual(code, 0)
        self.assertIn("dedicated security pass failed", body)
        self.assertEqual(result["security"]["candidates"], 1)  # the main pass's security finding, still verified

    def test_failed_verifier_posts_candidates_unverified(self):
        code, body, result = self.review(self.CODE, {"review": self.MAIN, "security": self.SECURITY,
                                                     "verify": providers.ProviderError("verifier down")})
        self.assertEqual(code, 0)
        self.assertIn("not independently verified", body)
        self.assertFalse(result["security"]["verified"])
        self.assertIn("SQL injection in search", body)

    def test_full_review_verifies_before_merging(self):
        code, text, result = self.review(self.CODE, {"review": self.MAIN, "security": self.SECURITY,
                                                     "verify": self.VERIFY}, tier="full")
        self.assertEqual(code, 0)
        kinds = [k for k, *_ in self.calls]
        self.assertEqual(sorted(kinds[:3]), ["full:correctness", "full:security", "full:tests"])
        self.assertEqual(kinds[3:], ["verify", "merge"])
        self.assertTrue(all(read for _, _, read, _ in self.calls[:4]))  # passes and verifier read the snapshot
        merge_prompt = self.calls[-1][3]
        self.assertIn("SQL injection in search", merge_prompt)  # the verified finding reaches the merge
        self.assertNotIn("SQL from user input", merge_prompt)  # the unverified duplicates do not
        self.assertEqual(result["security"]["confirmed"], 1)

    def test_docs_only_change_skips_the_dedicated_pass(self):
        code, body, result = self.review(self.DOCS, {"review": {"summary": "Docs.", "findings": []}})
        self.assertEqual(code, 0)
        self.assertEqual([k for k, *_ in self.calls], ["fast"])
        self.assertIn("**Security:** checked by the main review (only docs or assets changed) · no issues found", body)
        self.assertFalse(result["security"]["dedicated_pass"])


if __name__ == "__main__":
    unittest.main()
