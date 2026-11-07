import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codeturtle import fleet, github, release
from codeturtle.github import GitHubError

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = (ROOT / "examples" / "codeturtle.yml").read_text()
FRESH = """      fresh:
        description: review the whole PR instead of following up on the last review (true/false)
        required: false
"""
OLD = TEMPLATE.replace(FRESH, "")  # the 1.1-1.3 template, before the `fresh` input
PINNED = OLD.replace("uses: LiquidatorCoder/codeturtle@v1",
                     "uses: LiquidatorCoder/codeturtle@12eff70fb0de64b8bb9f112691b769427f285c4f # codeturtle 1.1.0")
A, B, C, HEAD = "a" * 40, "b" * 40, "c" * 40, "d" * 40


def tpl(major_commit=C):
    return fleet.Template("v1.4.0", C, TEMPLATE, "v1", major_commit, {"old-blob": OLD, "new-blob": TEMPLATE})


def repo(text=OLD, blob="old-blob", private=True, can_write=True, methods=("squash", "merge")):
    return fleet.Repo("o/app", "main", HEAD, "t" * 40, blob, text, private, can_write, list(methods))


class ReleasePlanTests(unittest.TestCase):
    def test_new_version_is_tagged_and_the_major_tag_follows(self):
        p = release.plan("1.5.0", {"v1": A, "v1.4.0": A}, HEAD)
        self.assertEqual((p.tag, p.new_tag, p.major, p.major_target, p.move_major), ("v1.5.0", True, "v1", HEAD, True))
        self.assertTrue(p.changes)

    def test_released_version_changes_nothing(self):
        p = release.plan("1.4.0", {"v1": A, "v1.4.0": A, "v1.1.1": B}, HEAD)
        self.assertFalse(p.new_tag or p.move_major or p.changes)

    def test_lagging_major_tag_is_moved_to_the_newest_release(self):
        # 1.4.0 was tagged but v1 was never moved (how 1.2 and 1.3 went unshipped).
        p = release.plan("1.4.0", {"v1": B, "v1.1.1": B, "v1.4.0": A}, HEAD)
        self.assertFalse(p.new_tag)
        self.assertEqual((p.major_target, p.move_major), (A, True))

    def test_version_must_be_newer_than_the_newest_release(self):
        with self.assertRaisesRegex(release.ReleaseError, "not newer than the released v1.4.0"):
            release.plan("1.3.0", {"v1": A, "v1.4.0": A}, HEAD)

    def test_version_must_be_semver(self):
        with self.assertRaisesRegex(release.ReleaseError, "MAJOR.MINOR.PATCH"):
            release.plan("1.5", {}, HEAD)

    def test_new_major_leaves_the_old_major_tag_alone(self):
        p = release.plan("2.0.0", {"v1": A, "v1.4.0": A}, HEAD)
        self.assertEqual((p.tag, p.major, p.major_target, p.move_major), ("v2.0.0", "v2", HEAD, True))

    def test_first_release(self):
        p = release.plan("1.0.0", {}, HEAD)
        self.assertEqual((p.new_tag, p.major, p.move_major), (True, "v1", True))


class TemplateTests(unittest.TestCase):
    def test_shipped_template_can_be_rolled_out(self):
        self.assertEqual(fleet.check_template(TEMPLATE, "v1"), [])
        self.assertEqual(fleet.pin(TEMPLATE), "v1")
        self.assertEqual(fleet.dispatch_inputs(TEMPLATE), ["pr", "tier", "min_tier", "request_id", "fresh"])

    def test_broken_templates_are_refused(self):
        self.assertIn("codeturtle-protocol", " ".join(fleet.check_template(TEMPLATE.replace("codeturtle-protocol: 2", ""), "v1")))
        self.assertIn("not the major tag @v1", " ".join(fleet.check_template(PINNED, "v1")))
        no_inputs = re.sub(r"      request_id:\n.*\n.*\n", "", TEMPLATE)
        self.assertIn("request_id", " ".join(fleet.check_template(no_inputs, "v1")))
        self.assertIn("2 times", " ".join(fleet.check_template(TEMPLATE + "      - uses: LiquidatorCoder/codeturtle@v1\n", "v1")))

    def test_normalize_drops_pin_and_comment(self):
        self.assertEqual(fleet.normalize(PINNED), fleet.normalize(OLD))
        self.assertNotEqual(fleet.normalize(OLD), fleet.normalize(TEMPLATE))


class ClassifyTests(unittest.TestCase):
    def test_states(self):
        t = tpl()
        self.assertEqual(fleet.classify(repo(TEMPLATE, "new-blob"), t), "in sync")
        self.assertEqual(fleet.classify(repo(OLD, "old-blob"), t), "outdated")
        self.assertEqual(fleet.classify(repo(PINNED, "x"), t), "pinned")
        self.assertEqual(fleet.classify(repo(OLD + "      - run: echo hi\n", "y"), t), "customized")
        self.assertEqual(fleet.classify(repo("# codeturtle-sync: off\n" + OLD, "z"), t), "opted out")

    def test_same_pin_with_a_comment_is_customized_not_pinned(self):
        text = OLD.replace("codeturtle@v1", "codeturtle@v1 # mine")
        self.assertEqual(fleet.classify(repo(text, "q"), tpl()), "customized")

    def test_repo_from_node(self):
        node = {"nameWithOwner": "o/app", "isArchived": False, "isPrivate": True, "viewerPermission": "WRITE",
                "squashMergeAllowed": False, "mergeCommitAllowed": True, "rebaseMergeAllowed": True,
                "defaultBranchRef": {"name": "main", "target": {"oid": HEAD, "tree": {"oid": B}}},
                "object": {"oid": "old-blob", "text": OLD, "isTruncated": False}}
        r = fleet.Repo.from_node(node)
        self.assertEqual((r.slug, r.head, r.tree, r.can_write, r.merge_methods), ("o/app", HEAD, B, True, ["merge", "rebase"]))
        self.assertIsNone(fleet.Repo.from_node(dict(node, object=None)))  # no workflow
        self.assertIsNone(fleet.Repo.from_node(dict(node, isArchived=True)))
        self.assertFalse(fleet.Repo.from_node(dict(node, viewerPermission="READ")).can_write)


class LoadTemplateTests(unittest.TestCase):
    """Reads tags and the template's history from a real (temporary) git repository."""

    def git(self, *args):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=self.root,
                       check=True, capture_output=True)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "examples").mkdir()
        (self.root / "codeturtle").mkdir()
        self.git("init", "-q")
        for version, text in (("1.0.0", OLD), ("1.1.0", TEMPLATE)):
            (self.root / "examples" / "codeturtle.yml").write_text(text)
            (self.root / "codeturtle" / "__init__.py").write_text(f'__version__ = "{version}"\n')
            self.git("add", "-A")
            self.git("commit", "-q", "-m", version)
            self.git("tag", "-a", f"v{version}", "-m", version)
        self.git("tag", "-a", "v1", "-m", "major", "v1.0.0")  # the major tag lags behind
        for mod in (fleet, release):
            patcher = mock.patch.object(mod, "ROOT", self.root)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_latest_release_and_every_earlier_template(self):
        t = fleet.load_template()
        self.assertEqual((t.tag, t.major, t.text), ("v1.1.0", "v1", TEMPLATE))
        self.assertEqual(sorted(t.known.values()), sorted([OLD, TEMPLATE]))
        self.assertNotEqual(t.major_commit, t.commit)  # reported as a lagging major tag
        self.assertEqual(fleet.version_at("v1"), "1.0.0")
        self.assertEqual(fleet.version_at(t.commit), "1.1.0")
        self.assertIsNone(fleet.version_at("nope"))

    def test_release_plan_from_tags(self):
        p = release.plan("1.1.0", release.read_tags(), "f" * 40)
        self.assertEqual((p.new_tag, p.move_major, p.major_target), (False, True, fleet.load_template().commit))


class FakeGH:
    """Answers `gh api` calls from (method, path regex) routes; a list answers once per call."""

    def __init__(self, *routes):
        self.routes = list(routes)
        self.calls = []

    def __call__(self, args, stdin=None, binary=False, retry=True):
        method = args[args.index("-X") + 1] if "-X" in args else "GET"
        path = args[args.index("-X") + 2] if "-X" in args else args[1]
        self.calls.append((method, path, json.loads(stdin) if stdin else None))
        for m, pattern, answer in self.routes:
            if m == method and re.search(pattern, path):
                if isinstance(answer, list):
                    answer = answer.pop(0)
                if isinstance(answer, Exception):
                    raise answer
                return json.dumps(answer)
        raise AssertionError(f"unexpected gh api {method} {path}")

    def methods(self):
        return [(m, re.sub(r"^repos/o/app/", "", p)) for m, p, _ in self.calls]


NOT_FOUND = GitHubError("gh api -X GET failed: gh: Not Found (HTTP 404)")
NOT_MERGEABLE = GitHubError("gh api -X PUT failed: gh: Pull Request is not mergeable (HTTP 405)")


def routes(ref=NOT_FOUND, prs=None, merge=None, branch_blob="old-blob"):
    return (
        ("POST", r"git/trees$", {"sha": "tree2"}),
        ("POST", r"git/commits$", {"sha": "commit2"}),
        ("GET", r"git/ref/heads/codeturtle/workflow-sync$", ref),
        ("PATCH", r"git/refs/heads/codeturtle/workflow-sync$", {}),
        ("POST", r"git/refs$", {}),
        ("GET", r"pulls\?state=open&head=o%3Acodeturtle%2Fworkflow-sync$", [prs or []]),
        ("GET", r"contents/\.github/workflows/codeturtle\.yml\?ref=prhead$", {"sha": branch_blob}),
        ("PATCH", r"pulls/\d+$", {}),
        ("POST", r"pulls$", {"number": 7}),
        ("POST", r"issues/\d+/labels$", [[{"name": "codeturtle:skip"}]]),
        ("PUT", r"pulls/\d+/merge$", merge if merge is not None else {"merged": True}),
        ("DELETE", r"git/refs/heads/codeturtle/workflow-sync$", {}),
    )


class UpdateTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(fleet, "_sleep", lambda s: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_update(self, fake, merge=True, r=None):
        with mock.patch.object(github, "gh", fake):
            return fleet.update(r or repo(), tpl(), merge=merge)

    def test_new_pr_is_built_on_the_default_branch_and_merged(self):
        fake = FakeGH(*routes())
        self.assertEqual(self.run_update(fake), ("PR #7 merged", False))
        self.assertEqual(fake.methods(), [
            ("GET", "pulls?state=open&head=o%3Acodeturtle%2Fworkflow-sync"),
            ("POST", "git/trees"), ("POST", "git/commits"), ("GET", "git/ref/heads/codeturtle/workflow-sync"),
            ("POST", "git/refs"), ("POST", "pulls"),
            ("POST", "issues/7/labels"), ("PUT", "pulls/7/merge"), ("DELETE", "git/refs/heads/codeturtle/workflow-sync"),
        ])
        payloads = {(m, p): body for m, p, body in fake.calls}
        tree = payloads[("POST", "repos/o/app/git/trees")]
        self.assertEqual(tree["base_tree"], "t" * 40)
        self.assertEqual(tree["tree"], [{"path": ".github/workflows/codeturtle.yml", "mode": "100644", "type": "blob",
                                         "content": TEMPLATE}])
        commit = payloads[("POST", "repos/o/app/git/commits")]
        self.assertEqual(commit["parents"], [HEAD])
        self.assertTrue(commit["message"].startswith("ci(codeturtle): sync review workflow with v1.4.0\n\n"))
        self.assertIn("[skip ci]", commit["message"])
        self.assertEqual(payloads[("POST", "repos/o/app/git/refs")], {"ref": "refs/heads/codeturtle/workflow-sync", "sha": "commit2"})
        pr = payloads[("POST", "repos/o/app/pulls")]
        self.assertEqual((pr["head"], pr["base"]), ("codeturtle/workflow-sync", "main"))
        self.assertIn("+      fresh:", pr["body"])
        self.assertIn("codeturtle-sync: off", pr["body"])
        self.assertEqual(payloads[("POST", "repos/o/app/issues/7/labels")], {"labels": ["codeturtle:skip"]})
        merge = payloads[("PUT", "repos/o/app/pulls/7/merge")]
        self.assertEqual((merge["merge_method"], merge["sha"]), ("squash", "commit2"))
        self.assertEqual(merge["commit_title"], "ci(codeturtle): sync review workflow with v1.4.0 (#7)")
        self.assertIn("[skip ci]", merge["commit_message"])  # the push to main builds nothing

    def test_open_pr_and_branch_are_reused(self):
        fake = FakeGH(*routes(ref={"object": {"sha": "old"}}, prs=[{"number": 3, "head": {"sha": "prhead"}}]))
        self.assertEqual(self.run_update(fake, merge=False), ("PR #3 open", False))
        methods = fake.methods()
        self.assertIn(("PATCH", "git/refs/heads/codeturtle/workflow-sync"), methods)
        self.assertIn(("PATCH", "pulls/3"), methods)
        self.assertNotIn(("POST", "pulls"), methods)
        self.assertFalse(any(m == "PUT" for m, _ in methods))  # pr mode never merges
        force = [b for m, p, b in fake.calls if m == "PATCH" and p.endswith("workflow-sync")][0]
        self.assertEqual(force, {"sha": "commit2", "force": True})

    def test_open_pr_that_is_already_current_gets_no_new_commit(self):
        prs = [{"number": 3, "head": {"sha": "prhead"}}]
        fake = FakeGH(*routes(prs=prs, branch_blob=fleet.blob_sha(TEMPLATE)))
        self.assertEqual(self.run_update(fake), ("PR #3 merged", False))
        methods = fake.methods()
        self.assertFalse(any(p.startswith("git/") and m in ("POST", "PATCH") for m, p in methods), methods)
        self.assertEqual([b for m, _, b in fake.calls if m == "PUT"][0]["sha"], "prhead")

    def test_blob_sha_matches_git(self):
        self.assertEqual(fleet.blob_sha("hello\n"), "ce013625030ba8dba906f756967f9e9ca394464a")

    def test_merge_waits_for_mergeability(self):
        fake = FakeGH(*routes(merge=[NOT_MERGEABLE, NOT_MERGEABLE, {"merged": True}]))
        self.assertEqual(self.run_update(fake), ("PR #7 merged", False))
        self.assertEqual(sum(1 for m, _ in fake.methods() if m == "PUT"), 3)

    def test_refused_merge_leaves_the_pr_open_for_a_person(self):
        fake = FakeGH(*routes(merge=[NOT_MERGEABLE] * 4))
        action, needs = self.run_update(fake)
        self.assertEqual((action, needs), ("PR #7 open (merge refused: Pull Request is not mergeable (HTTP 405))", True))
        self.assertNotIn(("DELETE", "git/refs/heads/codeturtle/workflow-sync"), fake.methods())

    def test_other_merge_errors_are_not_retried(self):
        fake = FakeGH(*routes(merge=[GitHubError("gh api -X PUT failed: gh: Resource not accessible (HTTP 403)")]))
        self.assertEqual(self.run_update(fake)[1], True)
        self.assertEqual(sum(1 for m, _ in fake.methods() if m == "PUT"), 1)

    def test_merge_method_follows_the_repository(self):
        fake = FakeGH(*routes())
        self.run_update(fake, r=repo(methods=("merge",)))
        self.assertEqual([b for m, _, b in fake.calls if m == "PUT"][0]["merge_method"], "merge")
        fake = FakeGH(*routes())
        self.run_update(fake, r=repo(methods=("rebase",)))
        self.assertEqual([b for m, _, b in fake.calls if m == "PUT"][0], {"merge_method": "rebase", "sha": "commit2"})


class ProcessTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(fleet, "version_at", lambda ref: "1.4.0")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_check_mode_writes_nothing(self):
        with mock.patch.object(github, "gh", FakeGH()):  # any call would fail the test
            out = fleet.process(repo(), tpl(), "check")
        self.assertEqual((out.state, out.action, out.attention, out.runs), ("outdated", "would update", True, "1.4.0 (@v1)"))

    def test_in_sync_and_opted_out_need_nobody(self):
        self.assertFalse(fleet.process(repo(TEMPLATE, "new-blob"), tpl(), "sync").attention)
        self.assertFalse(fleet.process(repo("# codeturtle-sync: off\n" + OLD, "z"), tpl(), "sync").attention)

    def test_no_write_access_and_public_repos_need_a_person(self):
        out = fleet.process(repo(can_write=False), tpl(), "sync")
        self.assertEqual((out.action, out.attention), ("no write access", True))
        out = fleet.process(repo(TEMPLATE, "new-blob", private=False), tpl(), "sync")
        self.assertTrue(out.attention)
        self.assertIn("public repository", out.notes[0])

    def test_failed_update_is_reported(self):
        fake = FakeGH(("GET", r"pulls\?", [[]]),
                      ("POST", r"git/trees$", GitHubError("gh api -X POST failed: gh: Not Found (HTTP 404)")))
        with mock.patch.object(github, "gh", fake):
            out = fleet.process(repo(), tpl(), "sync")
        self.assertEqual((out.action, out.attention), ("update failed: Not Found (HTTP 404)", True))


class MainTests(unittest.TestCase):
    def run_main(self, outcomes_repos, mode="sync", template=None, env=None):
        summary = tempfile.NamedTemporaryFile("r", suffix=".md", delete=False)
        self.addCleanup(os.unlink, summary.name)
        with mock.patch.object(fleet, "load_template", lambda: template or tpl()), \
                mock.patch.object(fleet, "identity", lambda: "me (gh login)"), \
                mock.patch.object(fleet, "discover", lambda owners, repos: outcomes_repos), \
                mock.patch.object(fleet, "version_at", lambda ref: "1.4.0"), \
                mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": summary.name, "GITHUB_ACTIONS": "",
                                             **(env or {})}), \
                mock.patch("builtins.print") as out:
            code = fleet.main(mode)
        self.printed = str(out.call_args_list)
        return code, Path(summary.name).read_text()

    def test_everything_in_sync_exits_zero(self):
        code, text = self.run_main([repo(TEMPLATE, "new-blob")])
        self.assertEqual(code, 0)
        self.assertIn("| o/app | in sync | 1.4.0 (@v1) | — |", text)
        self.assertIn("(v1.4.0, the latest release)", text)

    def test_attention_and_lagging_major_exit_one(self):
        self.assertEqual(self.run_main([repo(PINNED, "x")])[0], 1)
        with mock.patch.object(github, "gh", FakeGH()):  # a lagging major tag writes nothing
            code, text = self.run_main([repo()], template=tpl(major_commit=A))
        self.assertEqual(code, 1)
        self.assertIn("not the latest release v1.4.0", text)
        self.assertIn("mode `check`", text)
        self.assertIn("| ⚠️ o/app | outdated | 1.4.0 (@v1) | would update |", text)

    def test_private_names_stay_out_of_the_public_log(self):
        home = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, home)
        secret = fleet.Repo("o/secret-app", "main", HEAD, "t" * 40, "x", PINNED, True, True, ["merge"])
        shown = fleet.Repo("o/open-app", "main", HEAD, "t" * 40, "new-blob", TEMPLATE, False, True, ["merge"])
        code, text = self.run_main([secret, shown], env={"GITHUB_ACTIONS": "true", "CODETURTLE_HOME": home})
        self.assertEqual(code, 1)
        for public in (text, self.printed):
            self.assertNotIn("secret-app", public)
            self.assertIn(fleet.label("o/secret-app"), public)
            self.assertIn("o/open-app", public)  # a public repository's name is no secret
        full = (Path(home) / fleet.REPORT_FILE).read_text()
        self.assertIn("| ⚠️ o/secret-app | pinned |", full)

    def test_unsafe_template_is_never_rolled_out(self):
        bad = fleet.Template("v1.4.0", C, PINNED, "v1", C, {})
        with mock.patch.object(fleet, "load_template", lambda: bad), mock.patch("builtins.print") as out:
            self.assertEqual(fleet.main("sync"), 1)
        self.assertIn("not safe to roll out", str(out.call_args))

    def test_bot_tokens_are_refused(self):
        with mock.patch.object(github, "graphql", lambda q, **kw: {"viewer": {"login": "github-actions[bot]"}}):
            with self.assertRaisesRegex(fleet.FleetError, "CODETURTLE_FLEET_TOKEN"):
                fleet.identity()


if __name__ == "__main__":
    unittest.main()
