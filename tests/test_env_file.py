"""Unit tests for the ``.env`` readers in :mod:`src.env_file`.

The exporters read their guard settings straight from the deployment ``.env``
file instead of the process environment, so the parsing quirks (quoting,
comments, unparsable numbers) are the contract under test.
"""
from __future__ import annotations

import pytest

from src.env_file import env_bool, read_env_value, safe_float_env, safe_int_env


@pytest.fixture
def env_file(tmp_path):
    def _write(text: str) -> str:
        path = tmp_path / ".env"
        path.write_text(text, encoding="utf-8")
        return str(path)

    return _write


def test_missing_file_returns_empty_string(tmp_path):
    assert read_env_value("ANY_KEY", str(tmp_path / "nope.env")) == ""


def test_reads_first_match_and_strips_quotes(env_file):
    path = env_file(
        "# comment\n"
        "OTHER=1\n"
        "  SPACED_KEY  =  value with spaces  \n"
        'DQUOTED="double"\n'
        "SQUOTED='single'\n"
    )
    assert read_env_value("SPACED_KEY", path) == "value with spaces"
    assert read_env_value("DQUOTED", path) == "double"
    assert read_env_value("SQUOTED", path) == "single"


def test_splits_on_first_equals_only(env_file):
    path = env_file("URL=https://example.com/?a=1&b=2\n")
    assert read_env_value("URL", path) == "https://example.com/?a=1&b=2"


def test_ignores_comments_blank_lines_and_keyless_lines(env_file):
    path = env_file("\n#KEY=commented\nno_equals_here\nKEY=value\n")
    assert read_env_value("KEY", path) == "value"


def test_key_must_match_exactly(env_file):
    path = env_file("PREFIX_KEY=1\nKEY=2\n")
    assert read_env_value("KEY", path) == "2"
    assert read_env_value("PREFIX", path) == ""


def test_empty_value_is_returned_as_empty_string(env_file):
    path = env_file("KEY=\n")
    assert read_env_value("KEY", path) == ""


def test_unreadable_file_is_reported(tmp_path, capsys):
    directory = tmp_path / "not-a-file"
    directory.mkdir()
    assert read_env_value("KEY", str(directory)) == ""
    assert "Error reading environment file" in capsys.readouterr().err


def test_quiet_suppresses_the_error(tmp_path, capsys):
    directory = tmp_path / "not-a-file"
    directory.mkdir()
    assert read_env_value("KEY", str(directory), quiet=True) == ""
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1", True),
        ("true", True),
        ("TRUE", True),
        ("Yes", True),
        (" on ", True),
        ("0", False),
        ("false", False),
        ("off", False),
        ("maybe", False),
    ],
)
def test_env_bool_values(env_file, raw, expected):
    assert env_bool("FLAG", env_path=env_file(f"FLAG={raw}\n")) is expected


def test_env_bool_defaults_when_absent_or_empty(env_file):
    assert env_bool("FLAG", True, env_path=env_file("other=1\n")) is True
    assert env_bool("FLAG", False, env_path=env_file("FLAG=\n")) is False
    assert env_bool("FLAG", env_path=env_file("FLAG=\n")) is False


def test_safe_int_env_parses_or_falls_back(env_file):
    path = env_file("GOOD=12\nBAD=abc\nFLOATY=1.5\n")
    assert safe_int_env("GOOD", 7, env_path=path) == 12
    assert safe_int_env("BAD", 7, env_path=path) == 7
    assert safe_int_env("FLOATY", 7, env_path=path) == 7
    assert safe_int_env("MISSING", 7, env_path=path) == 7


def test_safe_float_env_parses_or_falls_back(env_file):
    path = env_file("GOOD=1.5\nEXP=1e3\nBAD=abc\n")
    assert safe_float_env("GOOD", 0.25, env_path=path) == 1.5
    assert safe_float_env("EXP", 0.25, env_path=path) == 1000.0
    assert safe_float_env("BAD", 0.25, env_path=path) == 0.25
    assert safe_float_env("MISSING", 0.25, env_path=path) == 0.25
