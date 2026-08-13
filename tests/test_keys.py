"""Key resolution.

The security-relevant assertions are the masking and the gitignore: a key must
never reach stdout, a committed transcript, or the repo.
"""

from __future__ import annotations

import pathlib

import pytest

from tracelens import keys

REAL_LOOKING = "sk-ant-api03-" + "x" * 60


@pytest.fixture
def dotenv(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    monkeypatch.setattr(keys, "DOTENV", path)
    monkeypatch.delenv(keys.ENV_VAR, raising=False)
    return path


def test_no_key_anywhere(dotenv):
    status = keys.resolve()
    assert not status.present
    assert status.source == "none"
    assert status.masked == "not set"


def test_flag_beats_environment_and_file(dotenv, monkeypatch):
    keys.write_dotenv_key("sk-ant-from-file", dotenv)
    monkeypatch.setenv(keys.ENV_VAR, "sk-ant-from-env")
    status = keys.resolve("sk-ant-from-flag")
    assert status.key == "sk-ant-from-flag"
    assert status.source == "flag"


def test_environment_beats_file(dotenv, monkeypatch):
    keys.write_dotenv_key("sk-ant-from-file", dotenv)
    monkeypatch.setenv(keys.ENV_VAR, "sk-ant-from-env")
    status = keys.resolve()
    assert status.key == "sk-ant-from-env"
    assert status.source == "environment"


def test_file_is_the_fallback(dotenv):
    keys.write_dotenv_key(REAL_LOOKING, dotenv)
    status = keys.resolve()
    assert status.key == REAL_LOOKING
    assert status.source == ".env"
    assert status.looks_valid


def test_set_then_clear_round_trip(dotenv):
    keys.write_dotenv_key(REAL_LOOKING, dotenv)
    assert keys.resolve().present
    assert keys.clear_dotenv_key(dotenv) is True
    assert not keys.resolve().present
    assert keys.clear_dotenv_key(dotenv) is False, "clearing twice is not an error"


def test_clear_preserves_other_variables(dotenv):
    dotenv.write_text(f"OTHER=keepme\n{keys.ENV_VAR}={REAL_LOOKING}\n", encoding="utf-8")
    keys.clear_dotenv_key(dotenv)
    assert dotenv.exists()
    assert keys.read_dotenv(dotenv) == {"OTHER": "keepme"}


def test_masking_never_exposes_the_middle(dotenv):
    status = keys.resolve(REAL_LOOKING)
    masked = status.masked
    assert REAL_LOOKING not in masked
    assert "x" * 20 not in masked
    assert masked.startswith("sk-ant-") and masked.endswith("(73 chars)")


def test_short_secret_is_still_masked(dotenv):
    assert "abc" not in keys.resolve("abcdefgh").masked


def test_malformed_key_is_flagged_not_rejected(dotenv):
    """Wrong-looking keys are surfaced as a warning, not silently swallowed --
    a typo'd key should say so rather than falling back to the stub in silence."""
    status = keys.resolve("hunter2")
    assert status.present
    assert not status.looks_valid


def test_dotenv_parser_ignores_comments_and_quotes(dotenv):
    dotenv.write_text(
        f'# a comment\n\n{keys.ENV_VAR}="{REAL_LOOKING}"\nBARE\n', encoding="utf-8"
    )
    assert keys.read_dotenv(dotenv)[keys.ENV_VAR] == REAL_LOOKING


def test_dotenv_is_gitignored():
    ignore = (pathlib.Path(__file__).resolve().parent.parent / ".gitignore").read_text()
    assert ".env" in ignore, "a key file that can be committed is worse than no key file"


def test_recorded_transcripts_contain_no_key():
    directory = pathlib.Path(__file__).resolve().parent.parent / "examples"
    for path in directory.glob("*.json"):
        assert "sk-ant-" not in path.read_text(encoding="utf-8")
