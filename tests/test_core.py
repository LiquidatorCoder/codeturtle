import json
import unittest
from unittest import mock

from codeturtle import post, providers
from codeturtle.config import DEFAULTS, glob_match, merge
from codeturtle.diff import FileChange, annotate, chunk_files, commentable_lines, render_diff
from codeturtle.events import from_event
from codeturtle.tiers import combine, floor_tier, label_override, parse_command

PATCH = """@@ -1,4 +1,5 @@
 import os
-x = 1
+x = 2
+y = 3
 print(x)
\\ No newline at end of file
@@ -20,2 +21,2 @@ def f():
-    return a
+    return b
     pass"""


def fc(path, add=1, delete=0, patch=PATCH, status="modified"):
    return FileChange(path, status, add, delete, patch)


class DiffTests(unittest.TestCase):
    def test_commentable_lines(self):
        self.assertEqual(commentable_lines(PATCH), {1, 2, 3, 4, 21, 22})

    def test_annotate_numbers_new_side(self):
        text = annotate(PATCH)
        self.assertIn("R2     + x = 2", text)
        self.assertIn("R4       print(x)", text)
        self.assertIn("       - x = 1", text)
        self.assertNotIn("No newline", text)

    def test_render_budget_omits_later_files(self):
        files = [fc("a.py"), fc("b.py")]
        text, omitted = render_diff(files, budget=len(annotate(PATCH)) + 60)
        self.assertIn("### a.py", text)
        self.assertEqual(omitted, ["b.py"])

    def test_chunks(self):
        files = [fc(f"f{i}.py") for i in range(5)]
        chunks = chunk_files(files, budget=len(annotate(PATCH)) * 2 + 100)
        self.assertEqual(sum(len(c) for c in chunks), 5)
        self.assertGreater(len(chunks), 1)


class GlobTests(unittest.TestCase):
    def test_patterns(self):
        self.assertTrue(glob_match("README.md", "**/*.md"))
        self.assertTrue(glob_match("docs/a/b.md", "**/*.md"))
        self.assertTrue(glob_match("lib/auth/login.dart", "**/auth/**"))
        self.assertFalse(glob_match("lib/author.dart", "**/auth/**"))
        self.assertTrue(glob_match(".github/workflows/ci.yml", ".github/workflows/**"))
        self.assertTrue(glob_match("sub/LICENSE.txt", "LICENSE*"))
        self.assertFalse(glob_match("src/main.py", "docs/**"))

    def test_merge_overrides_nested_keys_only(self):
        cfg = merge(DEFAULTS, {"tiers": {"full_if_lines_over": 10}})
        self.assertEqual(cfg["tiers"]["full_if_lines_over"], 10)
        self.assertEqual(cfg["tiers"]["fast_if_lines_under"], 40)


class TierTests(unittest.TestCase):
    rules = DEFAULTS["tiers"]

    def test_sensitive_path_forces_full(self):
        tier, reason = floor_tier([fc("lib/auth/session.dart", 2)], self.rules)
        self.assertEqual(tier, "full")
        self.assertIn("lib/auth/session.dart", reason)

    def test_renamed_from_sensitive_path(self):
        f = fc("lib/session.dart", 2)
        f.previous_path = "lib/auth/session.dart"
        self.assertEqual(floor_tier([f], self.rules)[0], "full")

    def test_size_thresholds(self):
        self.assertEqual(floor_tier([fc("a.py", 900)], self.rules)[0], "full")
        self.assertEqual(floor_tier([fc("a.py", 10)], self.rules)[0], "fast")
        self.assertEqual(floor_tier([fc("a.py", 100)], self.rules)[0], "normal")

    def test_docs_only_is_fast(self):
        self.assertEqual(floor_tier([fc("README.md", 300), fc("docs/x.md", 200)], self.rules)[0], "fast")

    def test_ignored_files_do_not_count(self):
        lock = fc("pubspec.lock", 5000)
        lock.ignored = True
        self.assertEqual(floor_tier([lock, fc("a.py", 100)], self.rules)[0], "normal")

    def test_triage_can_raise_not_lower(self):
        self.assertEqual(combine("normal", "r", ("fast", "t")).tier, "normal")
        self.assertEqual(combine("normal", "r", ("full", "t")).tier, "full")
        self.assertEqual(combine("normal", "r", ("full", "t")).source, "triage")
        self.assertEqual(combine("fast", "r", ("bogus", "t")).tier, "fast")

    def test_commands_and_labels(self):
        self.assertEqual(parse_command("/review"), (True, None))
        self.assertEqual(parse_command("/review FULL\nplease"), (True, "full"))
        self.assertEqual(parse_command("/reviewer"), (False, None))
        self.assertEqual(parse_command("please /review"), (False, None))
        self.assertEqual(label_override(["bug", "review:fast", "review:full"]), "full")
        self.assertIsNone(label_override(["review:huge"]))


class EventTests(unittest.TestCase):
    def test_pull_request(self):
        req, _ = from_event("pull_request", {"action": "opened", "pull_request": {"number": 3}}, "o/r")
        self.assertEqual((req.number, req.trigger), (3, "pull_request"))
        req, why = from_event("pull_request", {"action": "synchronize", "pull_request": {"number": 3}}, "o/r")
        self.assertIsNone(req)
        req, why = from_event("pull_request", {"action": "opened", "pull_request": {"number": 3, "draft": True}}, "o/r")
        self.assertIn("draft", why)

    def test_comment_needs_trusted_author(self):
        event = {
            "action": "created",
            "issue": {"number": 7, "pull_request": {}},
            "comment": {"id": 99, "body": "/review full", "author_association": "OWNER"},
        }
        req, _ = from_event("issue_comment", event, "o/r")
        self.assertEqual((req.number, req.override, req.comment_id), (7, "full", 99))
        event["comment"]["author_association"] = "CONTRIBUTOR"
        self.assertIsNone(from_event("issue_comment", event, "o/r")[0])


class ProviderTests(unittest.TestCase):
    def test_parse_json_tolerates_prose(self):
        self.assertEqual(providers.parse_json('Sure:\n```json\n{"a": 1}\n```'), {"a": 1})
        self.assertIsNone(providers.parse_json("no json here"))

    def test_limit_detection(self):
        for text in ["You've hit your usage limit. Try again at 5pm", "HTTP 429 Too Many Requests",
                     "rate_limit_error", "Credit limit reached"]:
            with self.assertRaises(providers.LimitHit):
                providers.check_limit("grok", text)
        providers.check_limit("grok", "authentication failed: invalid token")

    def test_env_drops_github_secrets(self):
        with mock.patch.dict("os.environ", {"GH_TOKEN": "x", "GITHUB_TOKEN": "y", "ACTIONS_RUNTIME_TOKEN": "z", "HOME": "/h"}):
            env = providers.agent_env()
        self.assertNotIn("GH_TOKEN", env)
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertNotIn("ACTIONS_RUNTIME_TOKEN", env)
        self.assertEqual(env["HOME"], "/h")

    def test_chain_falls_back_only_on_limits(self):
        call = providers.Call("fast", "p", "review", "/tmp", "low", 60)
        ok = {"summary": "s", "findings": []}

        def limited(call, model):
            raise providers.LimitHit("limit")

        with mock.patch.dict(providers.RUNNERS, {"codex": limited, "grok": lambda c, m: ok}):
            chain = providers.Chain(["codex", "grok"], DEFAULTS["models"], log=lambda m: None)
            result = chain.run(call)
            self.assertEqual(result.provider, "grok")
            self.assertEqual(chain.limit_notes, ["Codex"])
            # Codex stays skipped for later calls in the same review.
            self.assertEqual(chain.run(call).provider, "grok")

        def broken(call, model):
            raise providers.ProviderError("auth failed")

        with mock.patch.dict(providers.RUNNERS, {"codex": broken, "grok": lambda c, m: ok}):
            chain = providers.Chain(["codex", "grok"], DEFAULTS["models"], log=lambda m: None)
            with self.assertRaises(providers.ProviderError):
                chain.run(call)

        with mock.patch.dict(providers.RUNNERS, {"codex": limited, "grok": limited}):
            chain = providers.Chain(["codex", "grok"], DEFAULTS["models"], log=lambda m: None)
            with self.assertRaises(providers.AllLimited):
                chain.run(call)

    def test_chain_retries_unusable_output_once(self):
        call = providers.Call("fast", "p", "review", "/tmp", "low", 60)
        outputs = iter([None, {"summary": "s", "findings": []}])
        with mock.patch.dict(providers.RUNNERS, {"codex": lambda c, m: next(outputs)}):
            chain = providers.Chain(["codex"], DEFAULTS["models"], log=lambda m: None)
            self.assertEqual(chain.run(call).provider, "codex")


class PostTests(unittest.TestCase):
    def test_normalize_filters_fast_and_dedupes(self):
        findings = [
            {"severity": "minor", "category": "tests", "path": "a.py", "line": 2, "title": "t1", "body": ""},
            {"severity": "major", "category": "correctness", "path": "./a.py", "line": 2, "title": "Bug", "body": "b"},
            {"severity": "major", "category": "correctness", "path": "a.py", "line": 2, "title": "bug", "body": "dup"},
            {"severity": "weird", "title": "x"},
        ]
        out = post.normalize(findings, "fast")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["path"], "a.py")
        self.assertEqual(len(post.normalize(findings, "normal")), 2)

    def test_inline_only_on_commentable_lines(self):
        files = [fc("a.py")]
        findings = post.normalize([
            {"severity": "major", "category": "x", "path": "a.py", "line": 3, "title": "in diff", "body": ""},
            {"severity": "major", "category": "x", "path": "a.py", "line": 50, "title": "outside", "body": ""},
            {"severity": "minor", "category": "x", "path": "other.py", "line": 1, "title": "other file", "body": ""},
        ], "normal")
        inline, rest = post.split_inline(findings, files, max_inline=25)
        self.assertEqual([f["title"] for f in inline], ["in diff"])
        self.assertEqual(len(rest), 2)
        payload = post.build_payload(head_sha="abc", body="b", inline=inline)
        self.assertEqual(payload["event"], "COMMENT")
        self.assertEqual(payload["comments"][0]["side"], "RIGHT")

    def test_redaction(self):
        text = post.redact("token ghp_" + "a" * 36 + " and sk-ant-" + "b" * 30)
        self.assertNotIn("ghp_", text)
        self.assertNotIn("sk-ant-", text)
        self.assertEqual(text.count("[redacted]"), 2)

    def test_body_lists_unplaced_findings(self):
        findings = post.normalize([
            {"severity": "blocker", "category": "security", "path": "x.py", "line": 0, "title": "Leak", "body": "why"},
        ], "full")
        body = post.build_body(tier="full", tier_reason="r", reviewers="Grok (grok-4.7)", summary="S",
                               findings=findings, inline=[], rest=findings, head_sha="abcdef123", notes=[])
        self.assertIn("Full review", body)
        self.assertIn("Leak", body)
        self.assertIn("abcdef1", body)


class ProgressTests(unittest.TestCase):
    def test_render_tracks_steps(self):
        from codeturtle.progress import MARKER, Progress
        pr = Progress("o/r", 1, enabled=False, log=lambda m: None)
        pr.plan(["triage"])
        pr.tier("full", "touches auth", decided_by="rules")
        pr.plan(["full:correctness", "full:security", "merge", "post"])
        pr.begin("full:correctness", "Codex (gpt-6-sol)")
        pr.begin("full:security", "Grok (grok-4.7)")
        pr.note("Codex hit its usage limit")
        pr.skip("merge")
        text = pr.render()
        self.assertTrue(text.startswith(MARKER))
        self.assertIn("- [x] Choose review tier — rules", text)
        self.assertIn("- [x] Correctness pass — Codex (gpt-6-sol)", text)
        self.assertIn("**Security pass** — Grok (grok-4.7) · running", text)
        self.assertIn("~~Merge findings~~", text)
        self.assertIn("- [ ] Post review", text)
        self.assertIn("> Codex hit its usage limit", text)


class SchemaTests(unittest.TestCase):
    def test_schemas_are_strict(self):
        """Codex structured output needs every property required and no extra properties."""
        def walk(node):
            if node.get("type") == "object":
                self.assertFalse(node.get("additionalProperties", True))
                self.assertEqual(set(node["required"]), set(node["properties"]))
                for child in node["properties"].values():
                    walk(child)
            if node.get("type") == "array":
                walk(node["items"])

        for name in ("review", "triage"):
            walk(json.loads((providers.SCHEMA_DIR / f"{name}.schema.json").read_text()))


if __name__ == "__main__":
    unittest.main()
