"""Release automation: tag `__version__` and keep the major tag on the newest release.

    python3 -m codeturtle release [--dry-run]

Repositories use `LiquidatorCoder/codeturtle@v1`, so the `v1` tag decides which code all of them
run. The release workflow runs this on every push to main:

- `__version__` has no `v<version>` tag yet: it must be newer than every released `v<major>.y.z`,
  and the tag is created at HEAD.
- `v<major>` is moved to the newest `v<major>.y.z` when it points anywhere else (a release whose
  major-tag move failed is finished by the next run).
- The GitHub release for `v<version>` is created when missing.

Running it again changes nothing. A new major (2.0.0) creates `v2` and leaves `v1` alone.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import __version__

ROOT = Path(__file__).resolve().parent.parent
VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


class ReleaseError(Exception):
    pass


def parse_version(version: str) -> tuple[int, int, int]:
    m = VERSION_RE.match(version.strip())
    if not m:
        raise ReleaseError(f"__version__ {version!r} is not MAJOR.MINOR.PATCH")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def releases(tags: dict[str, str]) -> dict[tuple[int, int, int], str]:
    """Release tags (vX.Y.Z) by version → the commit each points at."""
    out = {}
    for name, commit in tags.items():
        m = TAG_RE.match(name)
        if m:
            out[(int(m.group(1)), int(m.group(2)), int(m.group(3)))] = commit
    return out


def tag_name(version: tuple[int, int, int]) -> str:
    return "v%d.%d.%d" % version


@dataclass
class Plan:
    version: str
    tag: str  # v1.4.0
    new_tag: bool  # create `tag` at HEAD
    major: str  # v1
    major_target: str  # the commit `major` must point at
    move_major: bool

    @property
    def changes(self) -> bool:
        """Whether the code that `@v<major>` users run changes."""
        return self.new_tag or self.move_major


def plan(version: str, tags: dict[str, str], head: str) -> Plan:
    """What to tag. `tags` maps every tag name to the commit it points at (peeled)."""
    v = parse_version(version)
    name = tag_name(v)
    same_major = {k: c for k, c in releases(tags).items() if k[0] == v[0]}
    new_tag = name not in tags
    if new_tag:
        newest = max(same_major) if same_major else None
        if newest and v <= newest:
            raise ReleaseError(f"__version__ {version} is not newer than the released {tag_name(newest)}; bump it")
        same_major[v] = head
    major = f"v{v[0]}"
    target = same_major[max(same_major)]
    return Plan(version, name, new_tag, major, target, tags.get(major) != target)


def git(*args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    if proc.returncode != 0:
        raise ReleaseError(f"git {' '.join(args[:2])} failed: {proc.stderr.strip()[-500:]}")
    return proc.stdout


def read_tags() -> dict[str, str]:
    """Every local tag → its commit (annotated tags peeled)."""
    out = {}
    for line in git("for-each-ref", "--format=%(refname:strip=2) %(objectname) %(*objectname)", "refs/tags").splitlines():
        parts = line.split()
        if len(parts) >= 2:
            out[parts[0]] = parts[2] if len(parts) > 2 else parts[1]
    return out


def gh_release_exists(tag: str) -> bool:
    proc = subprocess.run(["gh", "release", "view", tag, "--json", "tagName"], cwd=ROOT, capture_output=True, text=True)
    if proc.returncode == 0:
        return True
    if "release not found" in proc.stderr.lower() or "not found" in proc.stderr.lower():
        return False
    raise ReleaseError(f"gh release view {tag} failed: {proc.stderr.strip()[-500:]}")


def set_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as fh:
            fh.write(f"{name}={value}\n")


def main(dry_run: bool = False) -> int:
    try:
        head = git("rev-parse", "HEAD").strip()
        p = plan(__version__, read_tags(), head)
        if not dry_run and not p.new_tag:
            # Recreating a missing release is only needed for the current version's tag.
            missing_release = not gh_release_exists(p.tag)
        else:
            missing_release = p.new_tag
        print(f"codeturtle {p.version}: tag {p.tag} " + ("will be created at " + head[:12] if p.new_tag else "exists"))
        print(f"{p.major} → {p.major_target[:12]}" + (" (moving it)" if p.move_major else " (already there)"))
        if dry_run:
            print("dry run: nothing changed")
            set_output("released", "false")
            return 0
        message = f"codeturtle {p.version}"
        if p.new_tag:
            git("tag", "-a", p.tag, "-m", message, head)
            git("push", "origin", f"refs/tags/{p.tag}")
        if p.move_major:
            git("tag", "-f", "-a", p.major, "-m", message, p.major_target)
            git("push", "--force", "origin", f"refs/tags/{p.major}")
        if missing_release:
            proc = subprocess.run(
                ["gh", "release", "create", p.tag, "--title", message, "--generate-notes", "--verify-tag"],
                cwd=ROOT, capture_output=True, text=True,
            )
            if proc.returncode != 0:
                raise ReleaseError(f"gh release create {p.tag} failed: {proc.stderr.strip()[-500:]}")
            print(f"created the GitHub release {p.tag}")
        set_output("released", "true" if p.changes else "false")
        print("released" if p.changes else "nothing to release")
        return 0
    except ReleaseError as e:
        print(f"release: {e}")
        return 1
