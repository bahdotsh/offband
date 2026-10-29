"""Where a peer's file may land."""

from __future__ import annotations

import pytest

from offband import files


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
