"""Where a peer's file may land, judged after symlinks."""

from __future__ import annotations

from pathlib import Path

import pytest

from offband import files


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    return workspace


def test_a_game_folder_that_links_inside_the_workspace_is_written(ws):
    (ws / "src" / "game").mkdir(parents=True)
    (ws / "game").symlink_to(ws / "src" / "game")
    stored = files.store(ws, ws / ".offband" / "received", "off1a", "game%2Fui.js", b"ok")
    assert stored.note is None and (ws / "src" / "game" / "ui.js").read_bytes() == b"ok"


def test_a_game_folder_that_links_to_the_workspace_itself_is_quarantined(ws):
    (ws / "game").symlink_to(ws)
    stored = files.store(ws, ws / ".offband" / "received", "off1a", "game%2FMakefile", b"evil")
    assert stored.note and stored.note.startswith("quarantined")
    assert not (ws / "Makefile").exists()


def test_a_game_folder_that_links_to_a_protected_folder_is_quarantined(ws):
    (ws / ".claude").mkdir()
    (ws / "game").symlink_to(ws / ".claude")
    stored = files.store(ws, ws / ".offband" / "received", "off1a", "game%2Fsettings.json", b"{}")
    assert stored.note and stored.note.startswith("quarantined")
    assert not (ws / ".claude" / "settings.json").exists()


def test_a_game_folder_that_links_out_of_the_workspace_is_not_written(ws, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (ws / "game").symlink_to(outside)
    stored = files.store(ws, ws / ".offband" / "received", "off1a", "game%2Fx.js", b"x")
    assert stored.saved_to is None and not (outside / "x.js").exists()


def test_a_link_inside_the_game_to_a_hidden_file_is_quarantined(ws):
    (ws / "game" / ".hidden").mkdir(parents=True)
    (ws / "game" / "ok").symlink_to(ws / "game" / ".hidden")
    stored = files.store(ws, ws / ".offband" / "received", "off1a", "game%2Fok%2Fx.js", b"x")
    assert stored.note and stored.note.startswith("quarantined")
    assert not (ws / "game" / ".hidden" / "x.js").exists()


def test_without_a_workspace_files_go_to_the_quarantine_without_a_note(tmp_path):
    stored = files.store(None, tmp_path / "in", "off1a", "notes.txt", b"x")
    assert stored.note is None and stored.saved_to == (tmp_path / "in" / "off1a" / "notes.txt").resolve()


def test_with_nowhere_to_put_it_nothing_is_written(tmp_path):
    stored = files.store(None, None, "off1a", "notes.txt", b"x")
    assert stored.saved_to is None and "nowhere" in (stored.note or "")


@pytest.mark.parametrize("name, parts", [("a%2Fb.js", ["a", "b.js"]), ("..%2Fx", ["x"]), ("%2Fetc%2Fpasswd", ["passwd"]),
                                         ("a\\b.js", ["a", "b.js"]), ("..", ["received.bin"]), ("", ["received.bin"])])
def test_names_are_cleaned(name, parts):
    assert files.clean_relative(name) == parts
