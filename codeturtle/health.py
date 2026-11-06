"""Provider health check: is each model CLI installed and logged in on this runner?

It runs each CLI's own status command (no model call, no quota) in the same minimal environment
the reviews use, so it sees what a review would see. Every review runs it first: a provider that
fails is tried last and named on the PR with the fix, instead of failing a review the first time
it is needed. `python3 -m codeturtle doctor` prints the same check.
"""

from __future__ import annotations

import re
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from . import limits
from .providers import SETUP_RE, MissingCLI, ProviderError, find_bin, label, parse_json, run_process

STATUS_COMMAND = {
    "codex": ["login", "status"],
    "claude": ["auth", "status", "--json"],
    "grok": ["models"],  # prints "You are logged in with ..." before the model list
}
TIMEOUT_S = 20
FIX = {
    "codex": "run `codex login` on the runner Mac",
    "grok": "run `grok login` on the runner Mac",
    "claude": "run `claude setup-token` on the runner Mac and save the token to "
              "`~/.codeturtle/claude-oauth-token` (mode 600)",
}
LOGGED_IN_RE = re.compile(r"\blogged in\b", re.IGNORECASE)


@dataclass
class Health:
    provider: str
    status: str  # ok | missing | logged_out | unknown (the check itself could not tell)
    detail: str = ""

    @property
    def broken(self) -> bool:
        return self.status in ("missing", "logged_out")

    def fix(self) -> str:
        if self.status == "missing":
            name = self.provider
            return f"install the {name} CLI on the runner Mac or set `CODETURTLE_{name.upper()}_BIN`"
        return FIX.get(self.provider, f"log in to {self.provider} on the runner Mac")

    def warning(self) -> str:
        """The note shown on the PR for a broken provider."""
        problem = "its CLI is not installed" if self.status == "missing" else "it is not logged in"
        return f"⚠️ {label(self.provider)} can't review on this runner: {problem}. To fix it, {self.fix()}."


def _first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "")[:200]


def check(provider: str) -> Health:
    if provider not in STATUS_COMMAND:
        return Health(provider, "unknown", "no health check for this provider")
    try:
        binary = find_bin(provider)
    except MissingCLI as e:
        return Health(provider, "missing", str(e))
    with tempfile.TemporaryDirectory(prefix="codeturtle-health-") as tmp:
        try:
            code, out, err = run_process([binary, *STATUS_COMMAND[provider]], "", tmp, TIMEOUT_S)
        except ProviderError:
            return Health(provider, "unknown", f"status check timed out after {TIMEOUT_S}s")
        except OSError as e:
            return Health(provider, "unknown", f"status check could not start: {e}")
    if provider == "claude":
        data = parse_json(out) or {}
        if data.get("loggedIn") is True:
            return Health(provider, "ok", f"logged in ({data.get('authMethod') or 'unknown method'})")
        if data.get("loggedIn") is False:
            return Health(provider, "logged_out", "claude auth status: not logged in")
    text = f"{out}\n{err}"
    m = SETUP_RE.search(text)
    if m:
        return Health(provider, "logged_out", _first_line(text[m.start():]))
    m = LOGGED_IN_RE.search(text)
    if code == 0 and m:
        return Health(provider, "ok", _first_line(text[m.start():]))
    return Health(provider, "unknown", f"status check exited {code}: {_first_line(text)}")


def check_all(providers: list[str]) -> dict[str, Health]:
    """Check every provider at once (each check takes a second or two)."""
    names = list(dict.fromkeys(providers))
    if not names:
        return {}
    with ThreadPoolExecutor(max_workers=len(names)) as pool:
        return dict(zip(names, pool.map(check, names)))


def doctor(providers: list[str], models: dict, out=print) -> int:
    """Print each provider's state; exit status 1 when any provider can't run here."""
    results = check_all(providers)
    remembered = limits.active()
    width = max((len(label(p)) for p in providers), default=0)
    state = {"ok": "ok", "missing": "NOT INSTALLED", "logged_out": "NOT LOGGED IN", "unknown": "unknown"}
    for name in providers:
        h = results[name]
        line = f"{label(name):<{width}}  {models.get(name, ''):<18} {state[h.status]}"
        if h.detail:
            line += f" — {h.detail}"
        out(line)
        if h.broken:
            out(f"{'':<{width}}  fix: {h.fix()}")
        if name in remembered:
            out(f"{'':<{width}}  usage limit until {limits.fmt_time(remembered[name])} (seen by an earlier review)")
    broken = [label(n) for n, h in results.items() if h.broken]
    names = " and ".join([", ".join(broken[:-1]), broken[-1]] if len(broken) > 1 else broken)
    out(f"\n{names} can't review on this runner." if broken else "\nEvery provider can review.")
    return 1 if broken else 0
