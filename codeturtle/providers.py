"""Headless runs of the Codex, Grok and Claude CLIs, and the fallback chain.

The chain moves on to the next provider when one hits its usage limit, can't authenticate on
this runner, or keeps failing with a temporary error (capacity, server error, network) after a
couple of retries. Each switch is shown on the PR. Any other failure (timeout, crash, unusable
output) stops the review, so an unknown problem is never hidden behind a fallback.
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
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import limits, state_dir

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"

LIMIT_RE = re.compile(
    r"usage[ _-]?limit|rate[ _-]?limit|quota|credit[ _-]?limit|out of credits|insufficient credits"
    r"|too many requests|\b429\b|limit reached|hit your (?:usage )?limit|limit exceeded"
    r"|resource[ _-]?exhausted|try again (?:at|in|later)"
    r"|payment required|balance exhausted|insufficient balance",
    re.IGNORECASE,
)
# The CLI can't authenticate here: no point retrying, but the next provider may work.
SETUP_RE = re.compile(
    r"not logged in|please (?:run )?/login|please (?:log|sign) ?in|(?:log ?in|sign ?in) required"
    r"|not authenticated|authentication (?:failed|required|error)|unauthori[sz]ed"
    r"|invalid (?:api[ _-]?key|x-api-key|token|credentials|bearer)"
    r"|(?:token|session|credentials?) (?:has |have )?expired|(?:status|http) 40[13]\b"
    r"|\b40[13] (?:unauthorized|forbidden)|run `?(?:codex|grok) login|run `?claude setup-token"
    r"|(?:sign|log) ?in again|re-?authenticate|refresh token",
    re.IGNORECASE,
)
# Worth another try in a moment: overloaded or unreachable service, dropped connection.
TRANSIENT_RE = re.compile(
    r"at capacity|overloaded|temporarily unavailable|service unavailable|internal (?:server )?error"
    r"|bad gateway|gateway time-?out|(?:status|http|error)[ :]+5\d\d\b|\b5\d\d (?:internal|bad gateway|service)"
    r"|connection (?:reset|refused|closed|error|aborted)|network (?:error|is unreachable)|no route to host"
    r"|could not resolve host|dns error|failed to lookup address|(?:connect|connection) timed out"
    r"|stream (?:disconnected|error|closed)|error sending request|broken pipe|unexpected eof"
    r"|econnreset|econnrefused|etimedout|enotfound|upstream connect error|try a different model"
    r"|request timed out|high demand",
    re.IGNORECASE,
)

EXTRA_PATH = ["~/.local/bin", "~/.grok/bin", "/opt/homebrew/bin", "/usr/local/bin"]
ENV_KEEP = {
    "HOME", "USER", "LOGNAME", "PATH", "SHELL", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TERM",
    "__CF_USER_TEXT_ENCODING", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN",
}
LABELS = {"codex": "Codex", "grok": "Grok", "claude": "Claude"}

# A provider failing with a temporary error is tried again after each of these waits, then skipped.
TRANSIENT_DELAYS_S = (20, 60)
# Extra seconds after a reported reset time before trying a provider again.
RESET_GRACE_S = 15

# Model CLI process groups still running, so a failed parallel pass can stop its siblings.
_ACTIVE: set[int] = set()
_ACTIVE_LOCK = threading.Lock()
# Set when the review is being stopped, so a pass waiting to retry stops waiting.
_STOP = threading.Event()


def kill_all() -> None:
    _STOP.set()
    with _ACTIVE_LOCK:
        pids = list(_ACTIVE)
    for pid in pids:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pid, signal.SIGKILL)


def pause(seconds: float) -> None:
    """Sleep, but give up (ProviderError) as soon as the review is being stopped."""
    if _STOP.wait(max(0.0, seconds)):
        raise ProviderError("stopped while waiting to retry")


def label(name: str) -> str:
    return LABELS.get(name, name)


class LimitHit(Exception):
    """The provider's subscription or rate limit is exhausted: try the next one."""

    def __init__(self, message: str, reset_at: float | None = None):
        super().__init__(message)
        self.reset_at = reset_at  # Unix time the provider said its limit resets, if it said


class SetupError(Exception):
    """The provider can't authenticate on this runner: skip it, try the next one."""


class MissingCLI(SetupError):
    """The provider's CLI is not installed (or not where CODETURTLE_<NAME>_BIN says)."""


class TransientError(Exception):
    """A temporary failure (capacity, server error, network): retry, then try the next one."""


class ProviderError(Exception):
    """Any other failure: stop the review."""


class AllLimited(Exception):
    """No provider can run, and at least one of them is only waiting for its usage limit to reset."""

    def __init__(self, parts: list[str], retry_at: float | None = None):
        super().__init__("no provider can run: " + "; ".join(parts))
        self.parts = parts
        self.retry_at = retry_at  # earliest reported reset, if any


class Unavailable(ProviderError):
    """No provider can run, and none of them is just waiting for a usage limit."""

    def __init__(self, parts: list[str]):
        super().__init__("no provider can run: " + "; ".join(parts))
        self.parts = parts


@dataclass
class Call:
    kind: str  # triage | fast | normal | full:<focus> | security | verify | merge
    prompt: str
    schema: str  # "review", "triage", "security" or "verify" (schemas/<name>.schema.json)
    cwd: str
    effort: str
    timeout_s: int
    read_repo: bool = False  # full tier: the model may read files under cwd


@dataclass
class Result:
    provider: str
    model: str
    data: dict


def claude_token_file() -> Path:
    return state_dir() / "claude-oauth-token"


def agent_env() -> dict:
    """Minimal environment for the CLIs: no GitHub token or runner secrets reach the model."""
    env = {k: v for k, v in os.environ.items() if k in ENV_KEEP}
    extra = [os.path.expanduser(p) for p in EXTRA_PATH]
    env["PATH"] = ":".join(dict.fromkeys(env.get("PATH", "").split(":") + extra))
    token_file = claude_token_file()
    if "CLAUDE_CODE_OAUTH_TOKEN" not in env and token_file.is_file():
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token_file.read_text().strip()
    return env


def find_bin(name: str) -> str:
    override = os.environ.get(f"CODETURTLE_{name.upper()}_BIN")
    found = override or shutil.which(name, path=agent_env()["PATH"])
    if not found or not (os.path.isfile(found) and os.access(found, os.X_OK)):
        where = f"at {found}" if found else "on PATH"
        raise MissingCLI(f"{label(name)}: {name} CLI not found {where} (set CODETURTLE_{name.upper()}_BIN)")
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
    with _ACTIVE_LOCK:
        _ACTIVE.add(proc.pid)
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
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE.discard(proc.pid)
    return proc.returncode, out, err


def tail(text: str, n: int = 600) -> str:
    text = (text or "").strip()
    return text[-n:]


def _excerpt(blob: str, m: re.Match) -> str:
    start = max(0, m.start() - 120)
    return blob[start:m.end() + 160].strip()


def check_limit(provider: str, *texts: str) -> None:
    blob = "\n".join(t for t in texts if t)
    m = LIMIT_RE.search(blob)
    if m:
        raise LimitHit(f"{label(provider)}: {_excerpt(blob, m)}", limits.parse_reset(blob))


def classify(provider: str, *texts: str) -> None:
    """Raise what the chain should do with a failed call, if its output says: LimitHit, then
    SetupError, then TransientError. Returns when the failure is not recognised."""
    check_limit(provider, *texts)
    blob = "\n".join(t for t in texts if t)
    for pattern, error in ((SETUP_RE, SetupError), (TRANSIENT_RE, TransientError)):
        m = pattern.search(blob)
        if m:
            raise error(f"{label(provider)}: {_excerpt(blob, m)}")


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
                classify("codex", *errors, tail(err, 4000))
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
            classify("grok", tail(out, 4000), tail(err, 4000))
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
        result = str(payload.get("result", ""))
        if status == 429:
            raise LimitHit(f"Claude: HTTP 429 {tail(result, 200)}", limits.parse_reset(result))
        check_limit("claude", result, tail(err, 4000))
        if status in (401, 403):
            raise SetupError(f"Claude: HTTP {status} {tail(result, 200)}")
        if isinstance(status, int) and status >= 500:
            raise TransientError(f"Claude: HTTP {status} {tail(result, 200)}")
        classify("claude", result, tail(err, 4000))
        raise ProviderError(f"claude exited {code}: {tail(result) or tail(err)}")
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
    if schema == "security":
        return isinstance(data.get("summary"), str) and isinstance(data.get("candidates"), list)
    if schema == "verify":
        return isinstance(data.get("verdicts"), list)
    return isinstance(data.get("summary"), str) and isinstance(data.get("findings"), list)


DOWN_REASONS = {
    "setup": "could not authenticate on this runner",
    "missing": "is not installed on this runner",
    "transient": "kept failing with a temporary error",
}


@dataclass
class Chain:
    providers: list[str]
    models: dict
    log: callable = print
    limit_notes: list[str] = field(default_factory=list)  # providers that hit a usage limit in this review
    used: list[str] = field(default_factory=list)
    calls: int = 0  # model CLI invocations, every attempt counted
    on_attempt: callable = None  # (kind, "Codex (gpt-6-sol)") when a provider starts a call
    on_limit: callable = None  # ("Codex", reset time or None) when a provider hits its limit
    on_down: callable = None  # ("Claude", reason) when a provider is skipped for another reason
    on_wait: callable = None  # (until) before waiting for a usage limit to reset
    broken: dict = field(default_factory=dict)  # name -> problem the health check found; tried last
    remembered: dict = field(default_factory=dict)  # name -> reset time an earlier review saw; tried late
    wait_minutes: float = 0  # when every provider is out, wait at most this long for a reset
    persist: bool = False  # save limits hit here (limits.json), so later reviews try them last
    sleep: callable = None  # defaults to pause(); tests pass a fake
    clock: callable = time.time
    limited: dict = field(default_factory=dict)  # name -> reset time (None = not given), hit here
    down: dict = field(default_factory=dict)  # name -> ("setup" | "transient", detail), hit here
    wait_until: float | None = None  # the one wait for a reset this review may do (shared by passes)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)  # passes may run in parallel

    # ---- running a call ----------------------------------------------------------------------

    def run(self, call: Call, wait: bool = True) -> Result:
        """Run `call` on the best available provider. `wait=False` never waits for a reset (for a
        step that has its own fallback)."""
        rounds = 0
        while True:
            for name in self.order():
                result = self._attempt(name, call)
                if result is not None:
                    return result
            rounds += 1
            if self.order() and rounds < 3:
                continue  # a short limit ("retry after 5s") reset meanwhile
            if not (wait and self._wait_for_reset()):
                raise self._out_of_providers()
            rounds = 0

    def order(self) -> list[str]:
        """Providers still worth trying, best first: healthy ones in the configured order, then
        ones an earlier review saw at their limit, then ones the health check flagged."""
        now = self.clock()

        def rank(name: str) -> int:
            if name in self.broken:
                return 2
            return 1 if self.remembered.get(name, 0) > now else 0

        live = [n for n in self.providers if not self._skipped(n)]
        return sorted(live, key=lambda n: (rank(n), self.providers.index(n)))

    def _skipped(self, name: str) -> bool:
        if name in self.down:
            return True
        if name in self.limited:
            reset = self.limited[name]
            return reset is None or reset > self.clock()
        return False

    def _attempt(self, name: str, call: Call) -> Result | None:
        """Result, or None when this provider is out for the rest of the review."""
        if name not in RUNNERS:
            raise ProviderError(f"unknown provider {name!r}")
        model = self.models[name]
        attempt = unusable = temporary = 0
        while not self._skipped(name):  # a parallel pass may have ruled it out meanwhile
            attempt += 1
            self.log(f"[{call.kind}] {label(name)} ({model}, effort {call.effort}) attempt {attempt}")
            if self.on_attempt:
                self.on_attempt(call.kind, f"{label(name)} ({model})")
            with self.lock:
                self.calls += 1
            try:
                data = RUNNERS[name](call, model)
            except LimitHit as hit:
                self.log(f"[{call.kind}] {hit}")
                self._mark_limited(name, hit)
                return None
            except SetupError as e:
                self.log(f"[{call.kind}] {e}")
                self._mark_down(name, "missing" if isinstance(e, MissingCLI) else "setup", str(e))
                return None
            except TransientError as e:
                self.log(f"[{call.kind}] {e}")
                if temporary >= len(TRANSIENT_DELAYS_S):
                    self._mark_down(name, "transient", str(e))
                    return None
                delay = TRANSIENT_DELAYS_S[temporary]
                temporary += 1
                self.log(f"[{call.kind}] temporary error; trying {label(name)} again in {delay}s")
                (self.sleep or pause)(delay)
                continue
            if valid(call.schema, data):
                with self.lock:
                    if name not in self.used:
                        self.used.append(name)
                    self.limited.pop(name, None)  # it worked after a wait: not limited any more
                    was_remembered = self.remembered.pop(name, None) is not None
                if was_remembered and self.persist:
                    limits.forget(name)  # it works again
                return Result(name, model, data)
            unusable += 1
            self.log(f"[{call.kind}] {label(name)} returned unusable output")
            if unusable >= 2:
                raise ProviderError(f"{label(name)} returned unusable output twice")
        return None

    def _mark_limited(self, name: str, hit: LimitHit) -> None:
        with self.lock:
            first = name not in self.limited
            self.limited[name] = hit.reset_at
            if label(name) not in self.limit_notes:
                self.limit_notes.append(label(name))
        if self.persist:
            limits.remember(name, hit.reset_at, str(hit), now=self.clock())
        if first and self.on_limit:
            self.on_limit(label(name), hit.reset_at)

    def _mark_down(self, name: str, kind: str, detail: str) -> None:
        with self.lock:
            first = name not in self.down
            self.down[name] = (kind, detail)
        self.log(f"{label(name)} {DOWN_REASONS[kind]}; skipping it for this review")
        if first and self.on_down:
            self.on_down(label(name), DOWN_REASONS[kind])

    def _wait_for_reset(self) -> bool:
        """Every provider is out. If a usage limit resets within `wait_minutes`, wait for it and
        return True. Parallel passes share one wait; a review waits at most once."""
        with self.lock:
            now = self.clock()
            if self.wait_until is not None:
                if now >= self.wait_until:
                    return False  # already waited; the limits came back
                target, announce = self.wait_until, False
            else:
                resets = [r for r in self.limited.values() if r is not None]
                if not resets or self.wait_minutes <= 0:
                    return False
                target = max(now, min(resets)) + RESET_GRACE_S
                if target - now > self.wait_minutes * 60:
                    return False
                self.wait_until, announce = target, True
        if announce:
            self.log(f"Every provider is out; waiting until {limits.fmt_time(target)} for a usage limit to reset")
            if self.on_wait:
                self.on_wait(target)
        (self.sleep or pause)(max(0.0, target - self.clock()))
        with self.lock:  # a temporary failure has had time to clear: allow those providers again
            self.down = {n: d for n, d in self.down.items() if d[0] != "transient"}
        return True

    # ---- reporting ---------------------------------------------------------------------------

    def describe(self) -> list[str]:
        """One phrase per provider this review could not use, in the configured order."""
        parts = []
        for name in self.providers:
            if name in self.limited:
                reset = self.limited[name]
                parts.append(f"{label(name)} is at its usage limit"
                             + (f" until {limits.fmt_time(reset)}" if reset else ""))
            elif name in self.down:
                parts.append(f"{label(name)} {DOWN_REASONS[self.down[name][0]]}")
        return parts

    def at_limit(self) -> list[tuple[str, float | None]]:
        """(provider, reset) for each provider not used because of a usage limit: limits hit in
        this review, and remembered ones skipped in favour of a provider later in the order."""
        first_used = min((self.providers.index(p) for p in self.used), default=len(self.providers))
        out = []
        for i, name in enumerate(self.providers):
            if name in self.limited:
                out.append((name, self.limited[name]))
            elif name in self.remembered and i < first_used:
                out.append((name, self.remembered[name]))
        return out

    def _out_of_providers(self) -> Exception:
        parts = self.describe()
        if not parts:
            return ProviderError("no providers configured")
        if any(n in self.limited for n in self.providers):
            now = self.clock()
            resets = [r for r in self.limited.values() if r is not None and r > now]
            return AllLimited(parts, retry_at=min(resets) if resets else None)
        return Unavailable(parts)
