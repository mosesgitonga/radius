"""Repository hygiene checks.

These guard against failures that show up nowhere near their cause.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SHELL_SCRIPTS = sorted((ROOT / "docker").rglob("*.sh"))
CRLF = bytes([13, 10])


def test_there_are_shell_scripts_to_check() -> None:
    """So the checks below cannot pass vacuously after a directory move."""
    assert SHELL_SCRIPTS


@pytest.mark.parametrize("script", SHELL_SCRIPTS, ids=lambda path: path.name)
def test_shell_scripts_use_unix_line_endings(script: Path) -> None:
    """A CRLF shell script fails inside the Linux container with
    `set: pipefail: invalid option name` and a restart loop — nothing about it
    points at line endings.

    `.gitattributes` makes Git check these out as LF, but it cannot help a file
    written directly on a Windows machine, which is exactly how this happened
    once: a script was edited by a tool that translated newlines, and the compose
    bind mount handed the CRLF working copy straight to the container.
    """
    assert CRLF not in script.read_bytes(), f"{script.name} has CRLF line endings"


@pytest.mark.parametrize("script", SHELL_SCRIPTS, ids=lambda path: path.name)
def test_shell_scripts_have_a_shebang(script: Path) -> None:
    assert script.read_bytes().startswith(b"#!"), f"{script.name} has no shebang"
