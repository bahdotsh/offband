"""The offband command's workspace and node home handling."""

from __future__ import annotations

import pytest

from fakes import ADA
from offband import cli


def test_init_keeps_the_rest_of_claude_md(tmp_path):
    md = tmp_path / "CLAUDE.md"
    md.write_text("# Mine\n\nKeep this.\n")
    cli.init_workspace(tmp_path, "Ada", 7777, quiet=True)
    cli.init_workspace(tmp_path, "Ada", 7778, quiet=True)
    text = md.read_text()
    assert text.startswith("# Mine\n\nKeep this.\n")
    assert text.count(cli.MARK_START) == 1 and "7778" in text and "7777/" not in text


def test_fresh_deletes_a_node_home_and_nothing_else(tmp_path):
    home = tmp_path / "ada"
    (home / "keys").mkdir(parents=True)
    (home / "store.key").write_text("k")
    cli.reset_home(home)
    assert not home.exists()

    stranger = tmp_path / "docs"
    stranger.mkdir()
    (stranger / "thesis.tex").write_text("years of work")
    with pytest.raises(cli.HomeError):
        cli.reset_home(stranger)
    (stranger / "config.json").write_text("{}")
    with pytest.raises(cli.HomeError):
        cli.reset_home(stranger)  # looks like a home, but holds other files
    assert (stranger / "thesis.tex").exists()


def test_allow_needs_full_addresses():
    assert cli._check_allow([ADA]) is None
    assert cli._check_allow(["off1"]) is not None
    assert cli._check_allow(["Ada"]) is not None
    assert cli._check_allow([f"{ADA},{ADA}"]) is not None
    assert cli._check_allow([f" {ADA}"]) is not None


def test_fresh_tolerates_finder_metadata(tmp_path):
    home = tmp_path / "ada"
    (home / "keys").mkdir(parents=True)
    (home / ".DS_Store").write_bytes(b"\0")
    cli.reset_home(home)
    assert not home.exists()


def test_fresh_refuses_a_symlinked_home(tmp_path):
    real = tmp_path / "real"
    (real / "keys").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(cli.HomeError, match="symbolic link"):
        cli.reset_home(link)
    assert (real / "keys").exists()


def test_rehearse_refuses_a_bad_home_before_starting_anything(tmp_path, capsys):
    home = tmp_path / ".ada-node"
    home.mkdir()
    (home / "thesis.tex").write_text("years of work")
    assert cli.main(["rehearse", "--root", str(tmp_path), "--fresh"]) == 2
    assert "not an Offband node home" in capsys.readouterr().err
    assert (home / "thesis.tex").exists()
    assert not (tmp_path / "ada").exists()  # no workspace made, no service spawned
