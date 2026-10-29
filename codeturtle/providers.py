"""Headless runs of the Codex, Grok and Claude CLIs, and the usage-limit fallback chain.

Only a usage/rate limit moves on to the next provider. Every other failure (auth, timeout,
crash, unusable output) stops the review, so a broken setup is never hidden behind a fallback.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"

LIMIT_RE = re.compile(
    r"usage[ _-]?limit|rate[ _-]?limit|quota|credit[ _-]?limit|out of credits|insufficient credits"
    r"|too many requests|\b429\b|limit reached|hit your (?:usage )?limit|limit exceeded"
    r"|resource[ _-]?exhausted|try again (?:at|in|later)",
    re.IGNORECASE,
)

EXTRA_PATH = ["~/.local/bin", "~/.grok/bin", "/opt/homebrew/bin", "/usr/local/bin"]
ENV_KEEP = {
    "HOME", "USER", "LOGNAME", "PATH", "SHELL", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TERM",
    "__CF_USER_TEXT_ENCODING", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN",
}
CLAUDE_TOKEN_FILE = Path("~/.codeturtle/claude-oauth-token").expanduser()
LABELS = {"codex": "Codex", "grok": "Grok", "claude": "Claude"}


class LimitHit(Exception):
    """The provider's subscription or rate limit is exhausted: try the next one."""


class ProviderError(Exception):
    """Any other failure: stop the review."""


class AllLimited(Exception):
    """Every provider in the chain is at its limit."""


@dataclass
class Call:
    kind: str  # triage | fast | normal | full | merge
    prompt: str
    schema: str  # "review" or "triage"
    cwd: str
    effort: str
    timeout_s: int
    read_repo: bool = False  # full tier: the model may read files under cwd


@dataclass
class Result:
    provider: str
    model: str
    data: dict


def agent_env() -> dict:
    """Minimal environment for the CLIs: no GitHub token or runner secrets reach the model."""
    env = {k: v for k, v in os.environ.items() if k in ENV_KEEP}
    extra = [os.path.expanduser(p) for p in EXTRA_PATH]
    env["PATH"] = ":".join(dict.fromkeys(env.get("PATH", "").split(":") + extra))
    if "CLAUDE_CODE_OAUTH_TOKEN" not in env and CLAUDE_TOKEN_FILE.is_file():
        env["CLAUDE_CODE_OAUTH_TOKEN"] = CLAUDE_TOKEN_FILE.read_text().strip()
    return env


def find_bin(name: str) -> str:
    override = os.environ.get(f"CODETURTLE_{name.upper()}_BIN")
    found = override or shutil.which(name, path=agent_env()["PATH"])
    if not found:
        raise ProviderError(f"{name} CLI not found on PATH (set CODETURTLE_{name.upper()}_BIN)")
    return found


def run_process(cmd: list[str], stdin: str, cwd: str, timeout_s: int) -> tuple[int, str, str]:
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=agent_env(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(stdin, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        raise ProviderError(f"{Path(cmd[0]).name} timed out after {timeout_s // 60} min")
    except BaseException:
        # Job cancelled (SIGTERM/SIGINT): don't leave the model CLI running in its own session.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        raise
    return proc.returncode, out, err


def tail(text: str, n: int = 600) -> str:
    text = (text or "").strip()
    return text[-n:]


def check_limit(provider: str, *texts: str) -> None:
    blob = "\n".join(t for t in texts if t)
    m = LIMIT_RE.search(blob)
    if m:
        start = max(0, m.start() - 120)
        raise LimitHit(f"{LABELS[provider]}: {blob[start:m.end() + 160].strip()}")


def parse_json(text: str) -> dict | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        pass
    # Tolerate prose or code fences around the object: take the last top-level {...} that parses.
    for start in [i for i, ch in enumerate(text) if ch == "{"][::-1]:
        try:
            value, _ = json.JSONDecoder().raw_decode(text[start:])
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            continue
    return None


def run_codex(call: Call, model: str) -> dict | None:
    with tempfile.TemporaryDirectory(prefix="codeturtle-codex-") as tmp:
        last = Path(tmp) / "last.json"
        cmd = [
            find_bin("codex"), "exec", "--ephemeral", "--skip-git-repo-check", "--ignore-user-config",
            "-s", "read-only", "-C", call.cwd, "-m", model,
            "-c", f"model_reasoning_effort={call.effort}",
            "--output-schema", str(SCHEMA_DIR / f"{call.schema}.schema.json"),
            "-o", str(last), "--json", "-",
        ]
        code, out, err = run_process(cmd, call.prompt, call.cwd, call.timeout_s)
        errors = []
        for line in out.splitlines():
            event = parse_json(line) or {}
            if event.get("type") == "error":
                errors.append(str(event.get("message", "")))
            elif event.get("type") == "turn.failed":
                errors.append(str((event.get("error") or {}).get("message", "")))
        data = parse_json(last.read_text()) if last.is_file() else None
        if data is None:
            check_limit("codex", *errors, tail(err, 4000))
            if code != 0 or errors:
                raise ProviderError(f"codex exited {code}: {'; '.join(errors) or tail(err)}")
        return data


def run_grok(call: Call, model: str) -> dict | None:
    schema = (SCHEMA_DIR / f"{call.schema}.schema.json").read_text()
    cmd = [
        find_bin("grok"), "-p", "-", "-m", model, "--reasoning-effort", call.effort,
        "--cwd", call.cwd, "--permission-mode", "plan", "--disable-web-search",
        "--json-schema", schema,
    ]
    code, out, err = run_process(cmd, call.prompt, call.cwd, call.timeout_s)
    payload = parse_json(out) or {}
    data = payload.get("structuredOutput")
    if not isinstance(data, dict):
        data = parse_json(str(payload.get("text", "")))
    if data is None:
        check_limit("grok", tail(out, 4000), tail(err, 4000))
        if code != 0:
            raise ProviderError(f"grok exited {code}: {tail(err) or tail(out)}")
    return data


def run_claude(call: Call, model: str) -> dict | None:
    schema = (SCHEMA_DIR / f"{call.schema}.schema.json").read_text()
    cmd = [
        find_bin("claude"), "-p", "--restricted", "--strict-mcp-config", "--disable-slash-commands",
        "--no-session-persistence", "--model", model, "--effort", call.effort,
        "--output-format", "json", "--json-schema", schema,
        "--tools", "Read,Grep,Glob" if call.read_repo else "",
    ]
    code, out, err = run_process(cmd, call.prompt, call.cwd, call.timeout_s)
    payload = parse_json(out) or {}
    if payload.get("is_error") or code != 0:
        status = payload.get("api_error_status")
        if status == 429:
            raise LimitHit(f"Claude: HTTP 429 {tail(str(payload.get('result', '')), 200)}")
        check_limit("claude", str(payload.get("result", "")), tail(err, 4000))
        raise ProviderError(f"claude exited {code}: {tail(str(payload.get('result', ''))) or tail(err)}")
    data = payload.get("structured_output")
    if not isinstance(data, dict):
        data = parse_json(str(payload.get("result", "")))
    return data


RUNNERS = {"codex": run_codex, "grok": run_grok, "claude": run_claude}


def valid(schema: str, data: dict | None) -> bool:
    if not isinstance(data, dict):
        return False
    if schema == "triage":
        return isinstance(data.get("tier"), str) and isinstance(data.get("reason"), str)
    return isinstance(data.get("summary"), str) and isinstance(data.get("findings"), list)


@dataclass
class Chain:
    providers: list[str]
    models: dict
    log: callable = print
    exhausted: list[str] = field(default_factory=list)
    limit_notes: list[str] = field(default_factory=list)
    used: list[str] = field(default_factory=list)
    on_attempt: callable = None  # (kind, "Codex (gpt-6-sol)") when a provider starts a call
    on_limit: callable = None  # ("Codex") when a provider hits its limit

    def run(self, call: Call) -> Result:
        for name in self.providers:
            if name in self.exhausted:
                continue
            if name not in RUNNERS:
                raise ProviderError(f"unknown provider {name!r}")
            model = self.models[name]
            for attempt in (1, 2):
                self.log(f"[{call.kind}] {LABELS[name]} ({model}, effort {call.effort}) attempt {attempt}")
                if self.on_attempt:
                    self.on_attempt(call.kind, f"{LABELS[name]} ({model})")
                try:
                    data = RUNNERS[name](call, model)
                except LimitHit as hit:
                    self.log(f"[{call.kind}] {hit}")
                    self.exhausted.append(name)
                    self.limit_notes.append(LABELS[name])
                    if self.on_limit:
                        self.on_limit(LABELS[name])
                    break
                if valid(call.schema, data):
                    if name not in self.used:
                        self.used.append(name)
                    return Result(name, model, data)
                self.log(f"[{call.kind}] {LABELS[name]} returned unusable output")
            else:
                raise ProviderError(f"{LABELS[name]} returned unusable output twice")
        raise AllLimited("all providers are at their usage limit: " + ", ".join(self.limit_notes))
