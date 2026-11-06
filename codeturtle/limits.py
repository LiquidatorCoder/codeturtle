"""Usage limits remembered between reviews.

When a provider reports its usage limit, the reset time in its message ("try again at 7:39 PM",
"try again in 20 seconds", ...) is saved in `<state dir>/limits.json`, so later reviews try that
provider last until it resets instead of rediscovering the limit on every call. Only a short,
redacted excerpt of the provider's own message is kept: never a prompt or a token.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import time
from datetime import datetime, timedelta

from . import state_dir
from .post import redact

FILE = "limits.json"
DEFAULT_S = 30 * 60  # skip a provider this long when its message gives no reset time
MAX_S = 12 * 3600  # never skip one longer; a longer limit is re-checked with a single call
HORIZON_S = 8 * 86400  # a parsed reset further away than this is treated as unparsed

_MONTHS = "jan feb mar apr may jun jul aug sep oct nov dec".split()
_UNIT_S = {"d": 86400, "h": 3600, "m": 60, "s": 1}

# "Claude AI usage limit reached|1793635200"
_EPOCH_RE = re.compile(r"\|\s*(\d{10})(?!\d)")
# "rate limited until 2026-11-02T14:09:00Z"
_ISO_RE = re.compile(
    r"(?:reset|until|again|after|available)\D{0,24}?"
    r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)",
    re.IGNORECASE,
)
# "Retry-After: 30"
_RETRY_AFTER_RE = re.compile(r"retry[- _]after[\"':=\s]+(\d+(?:\.\d+)?)\s*(ms|s|secs?|seconds?)?\b", re.IGNORECASE)
# "try again in 2 hours 5 minutes", "resets in 1h 20m"
_DURATION = r"\d+(?:\.\d+)?\s*(?:days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b"
_RELATIVE_RE = re.compile(
    rf"(?:try again|retry|resets?|available again)\s+in\s+((?:{_DURATION}[\s,]*(?:and\s+)?)+)",
    re.IGNORECASE,
)
_PART_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([a-z]+)", re.IGNORECASE)
# "try again at 7:39 PM", "resets 3pm (Asia/Kolkata)", "try again at Nov 8th, 2026 9:12 AM"
_CLOCK_RE = re.compile(
    r"(?:try again|retry|resets?|available again)\s+(?:at\s+|on\s+)?"
    r"(?:(?P<mon>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s+"
    r"(?:(?P<year>\d{4}),?\s+)?(?:at\s+)?)?"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>[ap]\.?m\b\.?)?"
    r"(?:\s*\((?P<tz>[A-Za-z_]+(?:/[A-Za-z0-9_+-]+)+)\))?",
    re.IGNORECASE,
)


def parse_reset(text: str, now: float | None = None) -> float | None:
    """When the usage limit described in `text` resets (Unix time), or None if it doesn't say."""
    now = time.time() if now is None else now
    text = text or ""
    m = _EPOCH_RE.search(text)
    if m:
        return _accept(float(m.group(1)), now)
    m = _ISO_RE.search(text)
    if m and (t := _iso(m.group(1))) is not None:
        return _accept(t, now)
    m = _RETRY_AFTER_RE.search(text)
    if m:
        seconds = float(m.group(1)) / (1000 if (m.group(2) or "").lower() == "ms" else 1)
        return _accept(now + max(seconds, 1.0), now)
    m = _RELATIVE_RE.search(text)
    if m:
        seconds = sum(float(n) * _UNIT_S[unit[0].lower()] for n, unit in _PART_RE.findall(m.group(1)))
        return _accept(now + seconds, now) if seconds > 0 else None
    for m in _CLOCK_RE.finditer(text):
        t = _clock(m, now)
        if t is not None:
            return _accept(t, now)
    return None


def _accept(t: float, now: float) -> float | None:
    return t if now < t <= now + HORIZON_S else None


def _iso(value: str) -> float | None:
    value = value.replace(" ", "T", 1)
    if value.endswith(("Z", "z")):
        value = value[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.astimezone()).timestamp()


def _zone(name: str | None):
    if not name:
        return None
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # unknown zone name or no tz database: fall back to local time
        return None


def _clock(m: re.Match, now: float) -> float | None:
    """A wall-clock reset time ("7:39 PM"), today or on the given date, in the runner's zone."""
    hour, minute = int(m.group("hour")), int(m.group("minute") or 0)
    ampm = (m.group("ampm") or "").lower().replace(".", "")
    if not ampm and m.group("minute") is None:
        return None  # "retry 3 times" is not a time
    if ampm:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if ampm == "pm" else 0)
    elif hour > 23:
        return None
    if minute > 59:
        return None
    zone = _zone(m.group("tz"))
    base = datetime.fromtimestamp(now, zone) if zone else datetime.fromtimestamp(now).astimezone()
    try:
        if m.group("mon"):
            month, day = _MONTHS.index(m.group("mon").lower()[:3]) + 1, int(m.group("day"))
            when = base.replace(year=int(m.group("year") or base.year), month=month, day=day,
                                hour=hour, minute=minute, second=0, microsecond=0)
            if not m.group("year") and when.timestamp() < now - 600:
                when = when.replace(year=when.year + 1)  # "Jan 2" seen on Dec 30
            return when.timestamp()
        when = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    except ValueError:
        return None
    t = when.timestamp()
    if t < now - 600:
        return (when + timedelta(days=1)).timestamp()  # that time has passed today: it means tomorrow
    return t if t > now else now + 60  # just passed: re-check in a minute


# ---- storage ---------------------------------------------------------------------------------


@contextlib.contextmanager
def _locked():
    """The stored limits, read and rewritten under an exclusive lock (every review on this Mac
    shares the file). Nothing is written when the body raises."""
    root = state_dir()
    root.mkdir(parents=True, exist_ok=True)
    path = root / FILE
    with open(root / "limits.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = _read()
        yield data
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        os.chmod(tmp, 0o600)
        tmp.replace(path)


def _read() -> dict:
    try:
        data = json.loads((state_dir() / FILE).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _until(entry) -> float | None:
    try:
        return float(entry["until"])
    except (KeyError, TypeError, ValueError):
        return None


def active(now: float | None = None) -> dict[str, float]:
    """Provider -> reset time, for remembered limits that have not reset yet."""
    now = time.time() if now is None else now
    out = {}
    for name, entry in _read().items():
        until = _until(entry)
        if until is not None and until > now:
            out[name] = until
    return out


def remember(provider: str, reset_at: float | None, message: str = "", now: float | None = None) -> float:
    """Record that `provider` hit its limit. Returns the time until which later reviews try it last."""
    now = time.time() if now is None else now
    until = min(reset_at if reset_at and reset_at > now else now + DEFAULT_S, now + MAX_S)
    with contextlib.suppress(OSError):
        with _locked() as data:
            for name in [n for n, e in data.items() if (_until(e) or 0) <= now]:
                del data[name]  # drop limits that have reset
            data[provider] = {
                "until": until,
                "known": bool(reset_at),
                "seen": now,
                "message": redact(" ".join((message or "").split()))[:160],
            }
    return until


def forget(provider: str) -> None:
    """The provider just worked: it is not at its limit any more."""
    with contextlib.suppress(OSError):
        with _locked() as data:
            data.pop(provider, None)


def fmt_time(ts: float, now: float | None = None) -> str:
    """'7:39 PM IST' in the runner's time zone, with the date when it isn't today."""
    now = time.time() if now is None else now
    lt, today = time.localtime(ts), time.localtime(now)
    clock = f"{(lt.tm_hour % 12) or 12}:{lt.tm_min:02d} {'AM' if lt.tm_hour < 12 else 'PM'}"
    day = "" if lt[:3] == today[:3] else f"{time.strftime('%b', lt)} {lt.tm_mday}, "
    zone = time.strftime("%Z", lt)
    return f"{day}{clock}{' ' + zone if zone else ''}"
