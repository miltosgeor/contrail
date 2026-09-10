"""Tests for the command line.

These exist because of one bug that a clean-clone smoke test found and the
whole rest of the suite missed: `contrail show` -- step three of the README
quick start -- crashed with UnicodeEncodeError on a fresh Windows install.
The tree was drawn with box-drawing characters and the summary separated with
a middot, and the Windows console codepage (cp1252) can encode neither.

Nothing caught it locally because the dev shell happened to be UTF-8. So the
canary here is not "does it produce the right tree" but "can its output be
written to the console the target platform actually has".
"""

from __future__ import annotations

import io
from contextlib import redirect_stdout

import pytest

from contrail import cli

# The Windows console codepage named in CLAUDE.md as the target environment.
CONSOLE_ENCODING = "cp1252"


def run(argv, db):
    """Run one CLI command, returning what it printed."""
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = cli.main(["--db", str(db), *argv])
    return code, buffer.getvalue()


@pytest.fixture()
def seeded(tmp_path):
    db = tmp_path / "cli.db"
    code, _ = run(["demo"], db)
    assert code == 0
    return db


# ------------------------------------------------- the encoding canary

def test_show_output_survives_the_windows_console(seeded):
    """The exact failure a clean install hit. cp1252 cannot encode box-drawing
    characters, so printing one raised and took the command down."""
    code, text = run(["show", "a1b2c3d4"], seeded)
    assert code == 0
    text.encode(CONSOLE_ENCODING)  # raises UnicodeEncodeError on regression


@pytest.mark.parametrize(
    "argv",
    [
        ["demo"],
        ["traces"],
        ["show", "a1b2c3d4"],
        ["sessions"],
        ["tree", "nope"],
        ["cost", "nope"],
        ["findings", "nope"],
        ["reconcile", "nope"],
    ],
)
def test_every_command_prints_console_safe_output(argv, seeded):
    """Applied to all of them, including the not-found paths, because an
    error message that cannot be printed is worse than the error."""
    _code, text = run(argv, seeded)
    text.encode(CONSOLE_ENCODING)


def test_no_module_emits_a_character_the_console_cannot_encode():
    """A static sweep, so a stray glyph fails here rather than on a stranger's
    first run. Comments and docstrings count -- they are cheap to keep ASCII
    and a docstring reaches the console through `--help`."""
    import pathlib

    package = pathlib.Path(cli.__file__).parent
    offenders = {}
    for path in sorted(package.glob("*.py")):
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            try:
                line.encode(CONSOLE_ENCODING)
            except UnicodeEncodeError:
                offenders.setdefault(path.name, []).append(number)
    assert offenders == {}, f"unencodable characters: {offenders}"


# --------------------------------------------------- ordinary behaviour

def test_demo_then_show_is_the_readme_quick_start(seeded):
    """The three commands the README tells a stranger to run first."""
    code, text = run(["traces"], seeded)
    assert code == 0
    assert "claude_code.interaction" in text

    code, text = run(["show", "a1b2c3d4"], seeded)
    assert code == 0
    assert "claude_code.tool Read" in text
    assert "ERROR" in text, "the demo trace has a failing tool call"


def test_an_unknown_trace_exits_non_zero(seeded):
    code, _ = run(["show", "ffffffff"], seeded)
    assert code == 1


def test_sessions_says_what_to_do_when_there_is_nothing(tmp_path):
    """The empty state on the command line, not just on the screen."""
    code, text = run(["sessions"], tmp_path / "empty.db")
    assert code == 0
    assert "contrail parse" in text


def test_findings_on_an_unknown_session_exits_non_zero(seeded):
    code, _ = run(["findings", "nope"], seeded)
    assert code == 1
