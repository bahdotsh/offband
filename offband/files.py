"""Where a file a peer sends may land.

A peer's file goes into the workspace at its relative path only under a
writable root (``game/``), and never on a dot path or on the agent's own
instruction files. Everything else goes to the quarantine under
``.offband/received/<sender>/``, where every part that made the path unsafe
gets a ``.quarantined`` suffix. Paths are judged where they land, after
symlinks, not only as the peer wrote them.

The functions here touch the file system only in ``store``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

# A peer's file lands in the workspace only under these folders: the game
# is the only thing agents share (OFFBAND.md). Anywhere else, a crew member
# could replace a file the agent runs (Makefile, package.json, a test).
WRITABLE_ROOTS = ("game",)
# Nor, even inside them, on these names at any depth (the agent's own
# instructions) or on any path with a component that starts with "."
# (.mcp.json, .claude/settings.json, .git/...).
PROTECTED_NAMES = frozenset({"claude.md", "claude.local.md", "offband.md", "agents.md", "gemini.md"})


@dataclass(frozen=True)
class Stored:
    """What became of a received file."""

    path: str  # the path the peer asked for, cleaned
    saved_to: Path | None  # where it was written, or None if nowhere
    note: str | None = None  # for the agent: why it is not in place


def protected_path(parts: list[str] | tuple[str, ...]) -> str | None:
    """The component that makes a relative path unsafe for a peer to write,
    or None."""
    for part in parts:
        if part.startswith(".") or part.lower() in PROTECTED_NAMES:
            return part
    return None


def writable_path(parts: list[str]) -> str | None:
    """Why a peer may not write this workspace path, or None if it may."""
    if len(parts) < 2 or parts[0] not in WRITABLE_ROOTS:
        return f"a peer may write only under {', '.join(r + '/' for r in WRITABLE_ROOTS)}"
    blocked = protected_path(parts)
    return f"a peer may not write {blocked!r} in the workspace" if blocked is not None else None


def quarantine_parts(parts: list[str]) -> list[str]:
    """The path a quarantined file is saved under. Every part that made it
    unsafe gets a suffix, so a peer's CLAUDE.md or .claude/ never sits in
    the workspace tree under a name agent tools load."""
    return [p + ".quarantined" if protected_path([p]) is not None else p for p in parts]


def clean_relative(encoded_name: str) -> list[str]:
    """The path a peer asked for, as safe relative parts. ``..`` and
    absolute paths are reduced to the file name."""
    relative = unquote(encoded_name).replace("\\", "/")
    parts = [p for p in relative.split("/") if p not in ("", ".")]
    if not parts or ".." in parts or relative.startswith("/"):
        name = parts[-1] if parts and parts[-1] != ".." else ""
        parts = [name or "received.bin"]
    return parts


class _Leaves(Exception):
    """The path resolves outside the workspace."""


def _in_place(workspace: Path, parts: list[str]) -> tuple[Path | None, str | None]:
    """Where ``parts`` lands in the workspace, judged after symlinks: the
    target and None, or None and why not. Raises _Leaves for a path that
    resolves outside the workspace."""
    root = workspace.resolve()
    # The writable root may itself be a link (game -> src/game): what counts
    # is the folder it names, which must be inside the workspace, below it,
    # and not protected.
    game = (root / parts[0]).resolve()
    try:
        game_inside = game.relative_to(root)
    except ValueError:
        raise _Leaves from None
    if not game_inside.parts:
        return None, f"{parts[0]}/ is the workspace itself"
    blocked = protected_path(game_inside.parts)
    if blocked is not None:
        return None, f"a peer may not write {blocked!r} in the workspace"
    target = (root / "/".join(parts)).resolve()
    try:
        rest = target.relative_to(game)
    except ValueError:
        # A link inside the root points elsewhere (game/cfg -> ../.claude).
        try:
            target.relative_to(root)
        except ValueError:
            raise _Leaves from None
        return None, f"a peer may write only under {', '.join(r + '/' for r in WRITABLE_ROOTS)}"
    blocked = protected_path(rest.parts)
    if blocked is not None:
        return None, f"a peer may not write {blocked!r} in the workspace"
    return target, None


def store(workspace: Path | None, quarantine: Path | None, sender: str, encoded_name: str, data: bytes) -> Stored:
    """Writes a received file into the workspace, or into the quarantine.
    Raises OSError, or ValueError for a NUL in the name."""
    parts = clean_relative(encoded_name)
    relative = "/".join(parts)
    blocked = writable_path(parts)
    if workspace is not None and blocked is None:
        try:
            target, blocked = _in_place(workspace, parts)
        except _Leaves:
            return Stored(relative, None, "not saved: the path leaves the workspace")
        if target is not None:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            return Stored(relative, target)
    if quarantine is None:
        return Stored(relative, None, "not saved: this node has nowhere to put files")
    root = (quarantine / (re.sub(r"[^A-Za-z0-9]", "", sender)[:24] or "unknown")).resolve()
    target = (root / "/".join(quarantine_parts(parts))).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return Stored(relative, None, "not saved: the path leaves the quarantine")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    # Without a workspace the quarantine is simply where files go.
    return Stored(relative, target, f"quarantined: {blocked}" if workspace is not None else None)
