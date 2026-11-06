"""GitHub access through the `gh` CLI (authenticated by GH_TOKEN from the workflow)."""

from __future__ import annotations

import json
import re
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

# The request never reached GitHub, so repeating it is safe even when it writes.
CONNECT_RE = re.compile(
    r"dial tcp|no route to host|connection refused|could not resolve host|no such host"
    r"|network is unreachable|tls handshake timeout",
    re.IGNORECASE,
)
# May have reached GitHub: repeated only for reads and idempotent writes.
TRANSIENT_RE = re.compile(
    r"connection reset|i/o timeout|timeout awaiting|unexpected eof|http 50[0234]\b|server error"
    r"|bad gateway|service unavailable|gateway timeout",
    re.IGNORECASE,
)
RETRY_DELAYS_S = (5, 15, 45)
_sleep = time.sleep


def _log(msg: str) -> None:
    print(msg, flush=True)


class GitHubError(Exception):
    pass


def gh(args: list[str], stdin: str | None = None, binary: bool = False, retry: bool = True) -> str | bytes:
    """Run `gh`, retrying network blips and GitHub 5xx errors (writes only when the request
    provably never left this machine). `retry=False` for cosmetic calls that must not stall."""
    method = args[args.index("-X") + 1].upper() if "-X" in args else "GET"
    idempotent = method in ("GET", "HEAD", "PUT", "PATCH", "DELETE")
    delays = RETRY_DELAYS_S if retry else ()
    for attempt in range(len(delays) + 1):
        proc = subprocess.run(
            ["gh", *args], input=stdin.encode() if stdin is not None else None, capture_output=True
        )
        if proc.returncode == 0:
            return proc.stdout if binary else proc.stdout.decode()
        err = proc.stderr.decode(errors="replace").strip()
        again = CONNECT_RE.search(err) or (idempotent and TRANSIENT_RE.search(err))
        if not again or attempt == len(delays):
            raise GitHubError(f"gh {' '.join(args[:3])} failed: {err[-800:]}")
        _log(f"gh {' '.join(args[:3])} failed ({again.group(0)}); retrying in {delays[attempt]}s")
        _sleep(delays[attempt])
    raise AssertionError("unreachable")


def api(path: str, *extra: str):
    return json.loads(gh(["api", path, *extra]))


def graphql(query: str, retry: bool = True, **variables) -> dict:
    """A GraphQL query or mutation; `retry=False` for a mutation that must not run twice."""
    args = ["api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        args += ["-F" if isinstance(value, (bool, int)) else "-f", f"{key}={json.dumps(value) if isinstance(value, bool) else value}"]
    data = json.loads(gh(args, retry=retry))
    if data.get("errors"):
        raise GitHubError("GraphQL: " + "; ".join(str(e.get("message", e)) for e in data["errors"])[:800])
    return data.get("data") or {}


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


def edit_comment(repo: str, comment_id: int, body: str, retry: bool = True) -> None:
    gh(["api", "-X", "PATCH", f"repos/{repo}/issues/comments/{comment_id}", "--input", "-"],
       stdin=json.dumps({"body": body}), retry=retry)


def delete_comment(repo: str, comment_id: int) -> None:
    gh(["api", "-X", "DELETE", f"repos/{repo}/issues/comments/{comment_id}"])


def list_comments(repo: str, number: int) -> list[dict]:
    pages = json.loads(gh(["api", "--paginate", "--slurp", f"repos/{repo}/issues/{number}/comments?per_page=100"]))
    return [item for page in pages for item in page]


def react(repo: str, comment_id: int, content: str) -> None:
    try:
        gh(["api", "-X", "POST", f"repos/{repo}/issues/comments/{comment_id}/reactions", "-f", f"content={content}"],
           retry=False)
    except GitHubError:
        pass  # a missing reaction is cosmetic
