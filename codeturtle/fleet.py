"""Keep every repository that uses codeturtle on the current workflow template.

    python3 -m codeturtle fleet [--mode sync|pr|check] [--owner LOGIN ...] [--repo owner/name ...] [--json]

The code a repository runs comes from the `@v1` tag, which the release workflow moves. Its
`.github/workflows/codeturtle.yml` is a copy of `examples/codeturtle.yml` and goes stale when the
template changes (1.4 added the `fresh` dispatch input). This finds every repository the token can
see that has that file, compares it with the template of the latest release and sorts it:

- in sync     identical to the template.
- outdated    an unmodified copy of an older template, so replacing it loses nothing: `sync` opens a
              PR that updates it and merges it at once, `pr` leaves the PR open, `check` only reports.
- pinned      an older template whose `uses:` was pinned to another ref: reported, never changed.
- customized  anything else: reported, never changed.
- opted out   the file contains `# codeturtle-sync: off`: reported, never changed.

Exit status 1 when a repository still needs a person (pinned, customized, public, a failed update, a
PR that could not be merged), when the major tag lags behind the latest release, or, with
`--mode check`, when anything is not in sync.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import subprocess
import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import github, release
from .github import GitHubError

ROOT = Path(__file__).resolve().parent.parent
ACTION_REPO = "LiquidatorCoder/codeturtle"
WORKFLOW_PATH = ".github/workflows/codeturtle.yml"
TEMPLATE_PATH = "examples/codeturtle.yml"
PROTOCOL = 2
REQUIRED_DISPATCH_INPUTS = ("pr", "min_tier", "request_id")  # what the Claude Code harness drives
SYNC_BRANCH = "codeturtle/workflow-sync"
SKIP_LABEL = "codeturtle:skip"
# Only the codeturtle workflow changes, so nothing needs to build: without this the PR and the merge
# would run the repository's CI (and push-to-main builds or deploys) and a codeturtle review.
SKIP_CI = f"Only {WORKFLOW_PATH} changes; no workflow needs to run for it. [skip ci]"
OPT_OUT_RE = re.compile(r"^\s*#\s*codeturtle-sync:\s*off\b", re.M | re.I)
USES_RE = re.compile(r"^(\s*-?\s*uses:\s*)" + re.escape(ACTION_REPO) + r"@([^\s#]+)[^\n]*$", re.M | re.I)
VERSION_RE = re.compile(r'^__version__\s*=\s*"([^"]+)"', re.M)
MODES = ("sync", "pr", "check")
PAGE_SIZE = 50  # 100 repositories with a file lookup each can time out
MERGE_DELAYS_S = (3, 10, 30)  # GitHub computes a new PR's mergeability in the background
_sleep = time.sleep

REPO_FIELDS = """
fragment R on Repository {
  nameWithOwner isArchived isPrivate viewerPermission
  squashMergeAllowed mergeCommitAllowed rebaseMergeAllowed
  defaultBranchRef { name target { ... on Commit { oid tree { oid } } } }
  object(expression: "HEAD:%s") { ... on Blob { oid text isTruncated } }
}
""" % WORKFLOW_PATH

VIEWER_QUERY = """
query($n: Int!, $after: String) {
  viewer {
    repositories(first: $n, after: $after, orderBy: {field: NAME, direction: ASC},
                 affiliations: [OWNER, COLLABORATOR, ORGANIZATION_MEMBER],
                 ownerAffiliations: [OWNER, COLLABORATOR, ORGANIZATION_MEMBER]) {
      pageInfo { hasNextPage endCursor }
      nodes { ...R }
    }
  }
}
""" + REPO_FIELDS

OWNER_QUERY = """
query($owner: String!, $n: Int!, $after: String) {
  repositoryOwner(login: $owner) {
    repositories(first: $n, after: $after, orderBy: {field: NAME, direction: ASC}) {
      pageInfo { hasNextPage endCursor }
      nodes { ...R }
    }
  }
}
""" + REPO_FIELDS

REPO_QUERY = """
query($owner: String!, $name: String!) { repository(owner: $owner, name: $name) { ...R } }
""" + REPO_FIELDS


class FleetError(Exception):
    pass


# ---- the template ----------------------------------------------------------------------------


def git(*args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    if proc.returncode != 0:
        raise FleetError(f"git {' '.join(args[:2])} failed: {proc.stderr.strip()[-300:]}")
    return proc.stdout


def git_ok(*args: str) -> str | None:
    try:
        return git(*args)
    except FleetError:
        return None


@dataclass
class Template:
    tag: str  # the latest release, e.g. v1.4.0
    commit: str
    text: str
    major: str  # v1
    major_commit: str | None  # where the major tag points now
    known: dict[str, str]  # blob oid → text of every template version up to the release


def load_template() -> Template:
    """The template of the newest release tag, and every earlier version of it, from git."""
    tags = release.read_tags()
    versions = release.releases(tags)
    if not versions:
        raise FleetError("no vX.Y.Z tag found; run from a codeturtle checkout with its tags (git fetch --tags)")
    newest = max(versions)
    tag, commit = release.tag_name(newest), versions[newest]
    known = {}
    for c in git("rev-list", commit, "--", TEMPLATE_PATH).split():
        blob = git_ok("rev-parse", f"{c}:{TEMPLATE_PATH}")
        if blob:
            known[blob.strip()] = git("cat-file", "blob", blob.strip())
    text = git("show", f"{commit}:{TEMPLATE_PATH}")
    major = f"v{newest[0]}"
    return Template(tag, commit, text, major, tags.get(major), known)


def pin(text: str) -> str | None:
    m = USES_RE.search(text)
    return m.group(2) if m else None


def normalize(text: str) -> str:
    """The workflow with its codeturtle `uses:` line reduced to the action (pin and comment dropped)."""
    return USES_RE.sub(lambda m: m.group(1) + ACTION_REPO, text)


def dispatch_inputs(text: str) -> list[str]:
    """The keys directly under `workflow_dispatch:` → `inputs:` (the one shape the template ships)."""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if re.match(r"^\s*workflow_dispatch:\s*$", line)), None)
    if start is None:
        return []
    found, indent, child = [], None, None
    for line in lines[start + 1:]:
        if not line.strip() or line.strip().startswith("#"):
            continue
        ind = len(line) - len(line.lstrip())
        if indent is None:
            if line.strip() != "inputs:":
                return []
            indent = ind
            continue
        if ind <= indent:
            break
        child = ind if child is None else child
        m = re.match(r"^\s*([A-Za-z0-9_-]+):", line)
        if ind == child and m:
            found.append(m.group(1))
    return found


def check_template(text: str, major: str) -> list[str]:
    """Why this template must not be rolled out (empty when it is fine)."""
    problems = []
    if not re.search(r"^#\s*codeturtle-protocol:\s*%d\s*$" % PROTOCOL, text, re.M):
        problems.append(f"it does not declare `# codeturtle-protocol: {PROTOCOL}`")
    uses = USES_RE.findall(text)
    if len(uses) != 1:
        problems.append(f"it uses {ACTION_REPO} {len(uses)} times, not once")
    elif uses[0][1] != major:
        problems.append(f"it pins {ACTION_REPO}@{uses[0][1]}, not the major tag @{major}")
    missing = [k for k in REQUIRED_DISPATCH_INPUTS if k not in dispatch_inputs(text)]
    if missing:
        problems.append(f"workflow_dispatch lacks input(s) {', '.join(missing)}")
    return problems


# ---- repositories ----------------------------------------------------------------------------


@dataclass
class Repo:
    slug: str
    default_branch: str
    head: str
    tree: str
    blob: str
    text: str
    private: bool
    can_write: bool
    merge_methods: list[str]

    @classmethod
    def from_node(cls, node: dict) -> Repo | None:
        """A repository that has the workflow on its default branch, else None."""
        obj = node.get("object") or {}
        ref = node.get("defaultBranchRef") or {}
        target = ref.get("target") or {}
        if node.get("isArchived") or not obj.get("oid") or obj.get("isTruncated") or not target.get("oid"):
            return None
        methods = [m for m, ok in (("squash", node.get("squashMergeAllowed")), ("merge", node.get("mergeCommitAllowed")),
                                   ("rebase", node.get("rebaseMergeAllowed"))) if ok]
        return cls(
            slug=node["nameWithOwner"], default_branch=ref["name"], head=target["oid"],
            tree=(target.get("tree") or {}).get("oid", ""), blob=obj["oid"], text=obj.get("text") or "",
            private=bool(node.get("isPrivate")), can_write=node.get("viewerPermission") in ("ADMIN", "MAINTAIN", "WRITE"),
            merge_methods=methods,
        )


def _pages(query: str, path: tuple[str, ...], **variables) -> list[dict]:
    nodes, after = [], None
    while True:
        args = dict(variables, n=PAGE_SIZE)
        if after:
            args["after"] = after
        data = github.graphql(query, **args)
        for key in path:
            data = data.get(key) or {}
        conn = data.get("repositories") or {}
        nodes += conn.get("nodes") or []
        info = conn.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            return nodes
        after = info["endCursor"]


def discover(owners: list[str], repos: list[str]) -> list[Repo]:
    """Every repository with the workflow on its default branch, sorted by name."""
    nodes = []
    for slug in repos:
        owner, _, name = slug.partition("/")
        node = github.graphql(REPO_QUERY, owner=owner, name=name).get("repository")
        if not node:
            raise FleetError(f"{slug}: repository not found (or the token cannot see it)")
        nodes.append(node)
    for owner in owners:
        nodes += _pages(OWNER_QUERY, ("repositoryOwner",), owner=owner)
    if not repos and not owners:
        nodes += _pages(VIEWER_QUERY, ("viewer",))
    found = {r.slug: r for r in (Repo.from_node(n) for n in nodes if n) if r}
    return [found[k] for k in sorted(found, key=str.lower)]


def classify(repo: Repo, tpl: Template) -> str:
    if repo.text == tpl.text:
        return "in sync"
    if OPT_OUT_RE.search(repo.text):
        return "opted out"
    if repo.blob in tpl.known:
        return "outdated"
    current = pin(repo.text)
    if current and current != pin(tpl.text):
        shape = normalize(repo.text)
        if any(shape == normalize(t) for t in (tpl.text, *tpl.known.values())):
            return "pinned"
    return "customized"


def version_at(ref: str) -> str | None:
    """codeturtle's __version__ at a tag, branch or commit of this checkout."""
    for candidate in (ref, f"origin/{ref}"):
        commit = git_ok("rev-parse", "--verify", "-q", f"{candidate}^{{commit}}")
        if commit:
            m = VERSION_RE.search(git_ok("show", f"{commit.strip()}:codeturtle/__init__.py") or "")
            return m.group(1) if m else None
    return None


def runs(text: str) -> str:
    ref = pin(text)
    if not ref:
        return "—"
    version = version_at(ref)
    return f"{version or '?'} (@{ref[:12]})"


# ---- updating a repository -------------------------------------------------------------------


def _api(method: str, path: str, payload: dict | None = None) -> dict:
    args = ["api", "-X", method, path]
    if payload is None:
        return json.loads(github.gh(args) or "{}")
    return json.loads(github.gh([*args, "--input", "-"], stdin=json.dumps(payload)) or "{}")


def _missing(e: GitHubError) -> bool:
    return "404" in str(e) or "Not Found" in str(e)


def blob_sha(text: str) -> str:
    """The git blob id of a file with this content (what GitHub reports as a file's `sha`)."""
    data = text.encode()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _has_template(slug: str, ref: str, tpl: Template) -> bool:
    try:
        found = _api("GET", f"repos/{slug}/contents/{WORKFLOW_PATH}?ref={ref}")
    except GitHubError:
        return False
    return found.get("sha") == blob_sha(tpl.text)


def pr_body(repo: Repo, tpl: Template) -> str:
    diff = "".join(difflib.unified_diff(
        repo.text.splitlines(keepends=True), tpl.text.splitlines(keepends=True),
        f"a/{WORKFLOW_PATH}", f"b/{WORKFLOW_PATH}",
    ))
    if len(diff) > 6000:
        diff = diff[:6000] + "\n… (diff truncated)\n"
    return "\n".join([
        f"Updates `{WORKFLOW_PATH}` to the workflow template of codeturtle {tpl.tag}.",
        "",
        "The file was an unmodified copy of an older codeturtle template, so nothing specific to this",
        "repository is replaced. The code the workflow runs comes from the `@" + tpl.major + "` tag and is",
        "already current; this keeps the triggers and inputs around it in step.",
        "",
        "```diff",
        diff.rstrip("\n"),
        "```",
        "",
        f"Opened by the fleet sync of [{ACTION_REPO}](https://github.com/{ACTION_REPO}); `[skip ci]` keeps CI",
        "and a review from running for it. To keep this repository's workflow as it is, add the line",
        "`# codeturtle-sync: off` to it.",
        "",
    ])


def update(repo: Repo, tpl: Template, merge: bool) -> tuple[str, bool]:
    """Puts the template on SYNC_BRANCH, opens (or reuses) the PR and merges it when asked.
    → (what happened, whether it still needs a person)."""
    slug = repo.slug
    title = f"ci(codeturtle): sync review workflow with {tpl.tag}"
    owner = slug.split("/", 1)[0]
    head = urllib.parse.quote(f"{owner}:{SYNC_BRANCH}", safe="")
    open_prs = json.loads(github.gh(["api", f"repos/{slug}/pulls?state=open&head={head}"]) or "[]")
    pr = open_prs[0] if open_prs else None
    # An open PR that already carries this template is left as it is (no daily force-push).
    commit = pr["head"]["sha"] if pr and _has_template(slug, pr["head"]["sha"], tpl) else None
    if commit is None:
        # One commit on top of the default branch, built through the Git Data API: the branch never
        # points at the base, so an open PR is updated rather than emptied.
        tree = _api("POST", f"repos/{slug}/git/trees", {
            "base_tree": repo.tree,
            "tree": [{"path": WORKFLOW_PATH, "mode": "100644", "type": "blob", "content": tpl.text}],
        })["sha"]
        commit = _api("POST", f"repos/{slug}/git/commits", {
            "message": f"{title}\n\n{SKIP_CI}\n", "tree": tree, "parents": [repo.head],
        })["sha"]
        try:
            _api("GET", f"repos/{slug}/git/ref/heads/{SYNC_BRANCH}")
            _api("PATCH", f"repos/{slug}/git/refs/heads/{SYNC_BRANCH}", {"sha": commit, "force": True})
        except GitHubError as e:
            if not _missing(e):
                raise
            _api("POST", f"repos/{slug}/git/refs", {"ref": f"refs/heads/{SYNC_BRANCH}", "sha": commit})
    body = pr_body(repo, tpl)
    if pr:
        number = pr["number"]
        _api("PATCH", f"repos/{slug}/pulls/{number}", {"title": title, "body": body})
    else:
        number = _api("POST", f"repos/{slug}/pulls", {
            "title": title, "head": SYNC_BRANCH, "base": repo.default_branch, "body": body,
        })["number"]
    # `[skip ci]` already keeps the repo's codeturtle from reviewing a template (a workflow file is a
    # full-tier path); the label covers a run that starts anyway. Best effort.
    try:
        _api("POST", f"repos/{slug}/issues/{number}/labels", {"labels": [SKIP_LABEL]})
    except GitHubError:
        pass
    if not merge:
        return f"PR #{number} open", False
    if not repo.merge_methods:
        return f"PR #{number} open (the repository allows no merge method)", True
    method = repo.merge_methods[0]
    merge_args = {"merge_method": method, "sha": commit}
    if method != "rebase":  # a rebase keeps the commit, and its message, as it is
        merge_args.update(commit_title=f"{title} (#{number})", commit_message=SKIP_CI)
    for attempt in range(len(MERGE_DELAYS_S) + 1):
        try:
            _api("PUT", f"repos/{slug}/pulls/{number}/merge", merge_args)
            break
        except GitHubError as e:
            if attempt == len(MERGE_DELAYS_S) or not re.search(r"\b(405|409)\b", str(e)):
                return f"PR #{number} open (merge refused: {_short(str(e))})", True
            _sleep(MERGE_DELAYS_S[attempt])
    try:
        _api("DELETE", f"repos/{slug}/git/refs/heads/{SYNC_BRANCH}")
    except GitHubError:
        pass  # the repo may delete merged branches itself
    return f"PR #{number} merged", False


def _short(error: str) -> str:
    """The useful part of a gh error, for one table cell."""
    m = re.search(r'"message":\s*"([^"]+)"', error)
    text = m.group(1) if m else (error.strip().splitlines() or ["unknown error"])[-1]
    text = re.sub(r"^gh [^:]*? failed: ", "", text)
    return re.sub(r"^gh: ", "", text).replace("|", "/")[:160]


# ---- the run ---------------------------------------------------------------------------------


@dataclass
class Outcome:
    repo: str
    state: str
    runs: str
    action: str = ""
    attention: bool = False
    notes: list[str] = field(default_factory=list)


def process(repo: Repo, tpl: Template, mode: str) -> Outcome:
    state = classify(repo, tpl)
    out = Outcome(repo.slug, state, runs(repo.text))
    if not repo.private:
        out.attention = True
        out.notes.append("public repository: codeturtle must only run on private ones")
    if state == "outdated":
        if mode == "check":
            out.action = "would update"
            out.attention = True
        elif not repo.can_write:
            out.action = "no write access"
            out.attention = True
        else:
            try:
                out.action, needs = update(repo, tpl, merge=mode == "sync")
                out.attention = out.attention or needs
            except GitHubError as e:
                out.action = f"update failed: {_short(str(e))}"
                out.attention = True
    elif state == "pinned":
        out.action = f"remove the pin (use @{tpl.major}) or add `# codeturtle-sync: off`"
        out.attention = True
    elif state == "customized":
        out.action = "update by hand from examples/codeturtle.yml, or add `# codeturtle-sync: off`"
        out.attention = True
    return out


def report(tpl: Template, outcomes: list[Outcome], who: str, mode: str) -> str:
    lag = tpl.major_commit != tpl.commit
    head = [
        f"### 🐢 codeturtle fleet · {tpl.tag}",
        "",
        f"`@{tpl.major}` → `{(tpl.major_commit or 'missing')[:12]}`"
        + (f" ⚠️ **not the latest release {tpl.tag}** (`{tpl.commit[:12]}`): run the release workflow"
           if lag else f" ({tpl.tag}, the latest release)")
        + f" · mode `{mode}` · as {who} · {len(outcomes)} repositor{'y' if len(outcomes) == 1 else 'ies'} with {WORKFLOW_PATH}",
        "",
    ]
    if not outcomes:
        return "\n".join(head + ["No repository has the workflow."])
    rows = ["| Repository | Workflow | Runs | Action |", "|---|---|---|---|"]
    for o in outcomes:
        action = "; ".join(filter(None, [o.action, *o.notes])) or "—"
        rows.append(f"| {'⚠️ ' if o.attention else ''}{o.repo} | {o.state} | {o.runs} | {action} |")
    return "\n".join(head + rows)


TOKEN_HELP = ("Set the CODETURTLE_FLEET_TOKEN secret to a token of yours that can write to the repositories "
              "(contents, pull requests, workflows), or run where `gh` is logged in as you.")


def identity() -> str:
    """Who the token belongs to. A workflow's own GITHUB_TOKEN (an app) cannot see other repositories,
    so it is refused instead of reporting an empty fleet."""
    source = "GH_TOKEN" if os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") else "gh login"
    try:
        login = (github.graphql("query { viewer { login } }").get("viewer") or {}).get("login") or ""
    except GitHubError as e:
        raise FleetError(f"the token cannot read your account ({_short(str(e))}). {TOKEN_HELP}")
    if not login or login.endswith("[bot]"):
        raise FleetError(f"the token belongs to {login or 'nobody'}, which cannot see your other repositories. {TOKEN_HELP}")
    return f"{login} ({source})"


def main(mode: str = "sync", owners: list[str] | None = None, repos: list[str] | None = None,
         as_json: bool = False) -> int:
    if mode not in MODES:
        print(f"fleet: unknown mode {mode!r}; expected one of {', '.join(MODES)}")
        return 2
    try:
        tpl = load_template()
        problems = check_template(tpl.text, tpl.major)
        if problems:
            raise FleetError(f"the {tpl.tag} template is not safe to roll out: " + "; ".join(problems))
        who = identity()
        if tpl.major_commit != tpl.commit and mode != "check":
            # The template would run ahead of the code `@v1` repositories get: change nothing.
            print(f"fleet: @{tpl.major} is not on {tpl.tag}; checking only")
            mode = "check"
        outcomes = [process(r, tpl, mode) for r in discover(owners or [], repos or [])]
    except (FleetError, GitHubError) as e:
        print(f"fleet: {e}")
        return 1
    text = report(tpl, outcomes, who, mode)
    if as_json:
        print(json.dumps({"release": tpl.tag, "major": tpl.major, "major_commit": tpl.major_commit,
                          "release_commit": tpl.commit, "mode": mode,
                          "repositories": [asdict(o) for o in outcomes]}, indent=2))
    else:
        print(text)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as fh:
            fh.write(text + "\n")
    lag = tpl.major_commit != tpl.commit
    return 1 if lag or any(o.attention for o in outcomes) else 0
