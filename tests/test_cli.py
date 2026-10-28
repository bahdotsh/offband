"""The offband command's workspace and node home handling."""

from __future__ import annotations

from offband import cli


def test_init_keeps_the_rest_of_claude_md(tmp_path):
    md = tmp_path / "CLAUDE.md"
    md.write_text("# Mine\n\nKeep this.\n")
    cli.init_workspace(tmp_path, "Ada", 7777, quiet=True)
    cli.init_workspace(tmp_path, "Ada", 7778, quiet=True)
    text = md.read_text()
    assert text.startswith("# Mine\n\nKeep this.\n")
    assert text.count(cli.MARK_START) == 1 and "7778" in text and "7777/" not in text
