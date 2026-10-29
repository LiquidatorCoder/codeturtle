"""GitHub access through the `gh` CLI (authenticated by GH_TOKEN from the workflow)."""

from __future__ import annotations

import json
import subprocess
import tarfile
import tempfile
from pathlib import Path


class GitHubError(Exception):
    pass


def gh(args: list[str], stdin: str | None = None, binary: bool = False) -> str | bytes:
    proc = subprocess.run(
        ["gh", *args], input=stdin.encode() if stdin is not None else None, capture_output=True
    )
    if proc.returncode != 0:
        raise GitHubError(f"gh {' '.join(args[:3])} failed: {proc.stderr.decode(errors='replace').strip()[-800:]}")
    return proc.stdout if binary else proc.stdout.decode()


def api(path: str, *extra: str):
    return json.loads(gh(["api", path, *extra]))


def get_pr(repo: str, number: int) -> dict:
    return api(f"repos/{repo}/pulls/{number}")


def get_files(repo: str, number: int) -> list[dict]:
    pages = json.loads(gh(["api", "--paginate", "--slurp", f"repos/{repo}/pulls/{number}/files?per_page=100"]))
    return [item for page in pages for item in page]


def file_at_ref(repo: str, path: str, ref: str) -> str | None:
    try:
        return gh(["api", f"repos/{repo}/contents/{path}?ref={ref}", "-H", "Accept: application/vnd.github.raw"])
    except GitHubError as e:
        if "404" in str(e) or "Not Found" in str(e):
            return None
        raise


def download_snapshot(repo: str, sha: str, dest: Path) -> Path:
    """Extract the repo tree at `sha` into dest and return the extracted root."""
    data = gh(["api", f"repos/{repo}/tarball/{sha}"], binary=True)
    with tempfile.NamedTemporaryFile(suffix=".tar.gz") as tmp:
        tmp.write(data)
        tmp.flush()
        with tarfile.open(tmp.name) as tar:
            kwargs = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
            tar.extractall(dest, **kwargs)
    roots = [p for p in dest.iterdir() if p.is_dir()]
    if len(roots) != 1:
        raise GitHubError(f"unexpected tarball layout in {dest}")
    return roots[0]


def post_review(repo: str, number: int, payload: dict) -> dict:
    return json.loads(gh(["api", "-X", "POST", f"repos/{repo}/pulls/{number}/reviews", "--input", "-"],
                         stdin=json.dumps(payload)))


def post_comment(repo: str, number: int, body: str) -> int:
    out = gh(["api", "-X", "POST", f"repos/{repo}/issues/{number}/comments", "--input", "-"],
             stdin=json.dumps({"body": body}))
    return int(json.loads(out)["id"])


def edit_comment(repo: str, comment_id: int, body: str) -> None:
    gh(["api", "-X", "PATCH", f"repos/{repo}/issues/comments/{comment_id}", "--input", "-"],
       stdin=json.dumps({"body": body}))


def delete_comment(repo: str, comment_id: int) -> None:
    gh(["api", "-X", "DELETE", f"repos/{repo}/issues/comments/{comment_id}"])


def list_comments(repo: str, number: int) -> list[dict]:
    pages = json.loads(gh(["api", "--paginate", "--slurp", f"repos/{repo}/issues/{number}/comments?per_page=100"]))
    return [item for page in pages for item in page]


def react(repo: str, comment_id: int, content: str) -> None:
    try:
        gh(["api", "-X", "POST", f"repos/{repo}/issues/comments/{comment_id}/reactions", "-f", f"content={content}"])
    except GitHubError:
        pass  # a missing reaction is cosmetic
