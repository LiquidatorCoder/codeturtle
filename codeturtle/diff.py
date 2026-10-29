"""Changed files, unified-diff parsing and the line-numbered diff the models read."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


@dataclass
class FileChange:
    path: str
    status: str
    additions: int
    deletions: int
    patch: str | None
    previous_path: str | None = None
    ignored: bool = False
    _lines: set[int] | None = field(default=None, repr=False)

    @classmethod
    def from_api(cls, item: dict) -> "FileChange":
        return cls(
            path=item["filename"],
            status=item.get("status", "modified"),
            additions=int(item.get("additions", 0)),
            deletions=int(item.get("deletions", 0)),
            patch=item.get("patch"),
            previous_path=item.get("previous_filename"),
        )

    @property
    def changes(self) -> int:
        return self.additions + self.deletions

    @property
    def commentable_lines(self) -> set[int]:
        """New-file lines GitHub accepts a review comment on (added or context lines in a hunk)."""
        if self._lines is None:
            self._lines = commentable_lines(self.patch or "")
        return self._lines


def commentable_lines(patch: str) -> set[int]:
    lines: set[int] = set()
    right = None
    for raw in patch.splitlines():
        m = HUNK_RE.match(raw)
        if m:
            right = int(m.group(3))
            continue
        if right is None or raw.startswith("\\"):
            continue
        if raw.startswith("-"):
            continue
        lines.add(right)
        right += 1
    return lines


def annotate(patch: str) -> str:
    """Prefix every new-side line with `R<n>` so models can cite exact line numbers."""
    out = []
    right = None
    for raw in patch.splitlines():
        m = HUNK_RE.match(raw)
        if m:
            right = int(m.group(3))
            out.append(raw)
            continue
        if right is None or raw.startswith("\\"):
            continue
        if raw.startswith("-"):
            out.append(f"{'':7}- {raw[1:]}")
            continue
        marker = "+" if raw.startswith("+") else " "
        out.append(f"R{right:<6}{marker} {raw[1:]}")
        right += 1
    return "\n".join(out)


def header(fc: FileChange) -> str:
    rename = f" (renamed from {fc.previous_path})" if fc.previous_path else ""
    return f"### {fc.path}{rename} [{fc.status}, +{fc.additions} -{fc.deletions}]"


def render_file(fc: FileChange) -> str:
    if fc.ignored:
        return f"{header(fc)}\n(generated or lock file; diff omitted)"
    if not fc.patch:
        return f"{header(fc)}\n(no textual diff: binary or too large for the GitHub API)"
    return f"{header(fc)}\n{annotate(fc.patch)}"


def render_diff(files: list[FileChange], budget: int) -> tuple[str, list[str]]:
    """Render files until the budget runs out; returns (text, paths that did not fit)."""
    parts, omitted, used = [], [], 0
    for fc in files:
        block = render_file(fc)
        if used + len(block) > budget and parts:
            omitted.append(fc.path)
            continue
        if len(block) > budget:
            block = block[:budget] + "\n(diff truncated)"
        parts.append(block)
        used += len(block) + 2
    return "\n\n".join(parts), omitted


def chunk_files(files: list[FileChange], budget: int) -> list[list[FileChange]]:
    """Group files (keeping directory order) into chunks whose rendered diff fits the budget."""
    chunks: list[list[FileChange]] = []
    current: list[FileChange] = []
    used = 0
    for fc in sorted(files, key=lambda f: f.path):
        size = len(render_file(fc))
        if current and used + size > budget:
            chunks.append(current)
            current, used = [], 0
        current.append(fc)
        used += size
    if current:
        chunks.append(current)
    return chunks
