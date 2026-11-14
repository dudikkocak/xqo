"""python -m xqo.sdk_check: credential hygiene, xqo's choice and the live call (with a fake Claude Code)."""
import json
import os
import stat

import pytest

from xqo import sdk_check

TOKEN = "sk-ant-oat01-" + "x" * 60


@pytest.fixture
def env(monkeypatch):
    for v in sdk_check.CREDENTIAL_VARS:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr("xqo.providers.sdk_installed", lambda: True)
    monkeypatch.delenv("XQO_PROVIDER", raising=False)
    monkeypatch.setenv("HOME", "/nonexistent-home")
    return monkeypatch


def fake_cli(tmp_path, result: dict, code: int = 0):
    p = tmp_path / "claude"
    p.write_text(f"#!/bin/sh\necho '{json.dumps(result)}'\nexit {code}\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


def test_problems_spot_paste_mistakes():
    assert sdk_check.problems("CLAUDE_CODE_OAUTH_TOKEN", TOKEN) == []
    assert any("quotes" in p for p in sdk_check.problems("CLAUDE_CODE_OAUTH_TOKEN", f'"{TOKEN}"'))
    assert any("spaces" in p for p in sdk_check.problems("CLAUDE_CODE_OAUTH_TOKEN", TOKEN + " "))
    assert any("byte-order" in p for p in sdk_check.problems("CLAUDE_CODE_OAUTH_TOKEN", "﻿" + TOKEN))
    assert any("sk-ant-oat" in p for p in sdk_check.problems("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-api03-" + "x" * 60))
    assert any("short" in p for p in sdk_check.problems("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-abc"))
    assert TOKEN not in sdk_check.mask(TOKEN) and "73 chars" in sdk_check.mask(TOKEN)


def test_ok_with_a_working_token(env, tmp_path, capsys):
    env.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOKEN)
    env.setenv("XQO_CLAUDE_CLI", fake_cli(tmp_path, {"type": "result", "is_error": False, "result": "OK"}))
    assert sdk_check.main([]) == 0
    out = capsys.readouterr().out
    assert "oauth token (CLAUDE_CODE_OAUTH_TOKEN)" in out and "OK: answered 'OK'" in out and "sign-in works" in out
    assert TOKEN not in out


def test_rejected_token_says_what_to_do(env, tmp_path, capsys):
    env.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOKEN)
    env.setenv("XQO_CLAUDE_CLI", fake_cli(tmp_path, {"type": "result", "is_error": True,
                                                     "result": "Invalid API key · Please run /login"}, code=1))
    assert sdk_check.main([]) == 1
    out = capsys.readouterr().out
    assert "FAILED: Invalid API key" in out and "claude setup-token" in out


def test_api_key_wins_and_empty_values_are_reported(env, capsys):
    env.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOKEN)
    env.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-" + "y" * 60)
    env.setenv("ANTHROPIC_AUTH_TOKEN", "")
    assert sdk_check.main(["--no-call"]) == 0
    out = capsys.readouterr().out
    assert "ANTHROPIC_API_KEY wins" in out and "api key (ANTHROPIC_API_KEY)" in out and "set but EMPTY" in out


def test_nothing_set_fails(env, capsys):
    assert sdk_check.main(["--no-call"]) == 1
    assert "none found" in capsys.readouterr().out


def test_without_the_sdk(env, capsys):
    env.setattr("xqo.providers.sdk_installed", lambda: False)
    assert sdk_check.main(["--no-call"]) == 1
    assert "NOT installed" in capsys.readouterr().out


def test_token_file_is_read_fresh_for_each_session(env, tmp_path, monkeypatch):
    from xqo import sdk_backend
    monkeypatch.setenv("XQO_DATA", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-old" + "o" * 50)
    f = tmp_path / "secrets" / "claude-oauth-token"
    assert sdk_backend.file_oauth_token() is None
    assert sdk_backend.auth_method() == "oauth token (CLAUDE_CODE_OAUTH_TOKEN)"
    f.parent.mkdir()
    f.write_text("﻿# from claude setup-token\nCLAUDE_CODE_OAUTH_TOKEN=\"" + TOKEN + "\"\r\n", encoding="utf-8")
    assert sdk_backend.file_oauth_token() == TOKEN
    assert sdk_backend.auth_method().startswith("oauth token (file ")
    llm = sdk_backend.ClaudeSdkLLM({"type": "claude-sdk"})
    assert llm.env()["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN
    f.write_text("sk-ant-oat01-new" + "n" * 50 + "\n")        # renewed: the next session uses it
    assert llm.env()["CLAUDE_CODE_OAUTH_TOKEN"].startswith("sk-ant-oat01-new")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-" + "k" * 60)  # a key still wins
    assert sdk_backend.auth_method() == "api key (ANTHROPIC_API_KEY)"
    f.unlink()
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in llm.env()   # back to the environment variable


def test_check_reports_the_token_file(env, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XQO_OAUTH_TOKEN_FILE", str(tmp_path / "tok"))
    (tmp_path / "tok").write_text(TOKEN)
    seen = tmp_path / "seen"
    cli = tmp_path / "claude"
    cli.write_text(f"#!/bin/sh\nprintf '%s' \"$CLAUDE_CODE_OAUTH_TOKEN\" > {seen}\n"
                   "echo '{\"type\":\"result\",\"is_error\":false,\"result\":\"OK\"}'\n")
    cli.chmod(0o755)
    monkeypatch.setenv("XQO_CLAUDE_CLI", str(cli))
    assert sdk_check.main([]) == 0
    out = capsys.readouterr().out
    assert "token file" in out and "oauth token (file" in out and TOKEN not in out
    assert seen.read_text() == TOKEN


def test_a_dotenv_file_works_as_the_token_file(env, tmp_path, monkeypatch):
    from xqo import sdk_backend
    f = tmp_path / ".env"
    monkeypatch.setenv("XQO_OAUTH_TOKEN_FILE", str(f))
    f.write_text("ANTHROPIC_API_KEY=\nDOTNET_IMAGE=mcr.microsoft.com/dotnet/sdk:10.0\n")
    assert sdk_backend.file_oauth_token() is None            # no token line: the environment variable applies
    f.write_text("﻿DOTNET_IMAGE=x\r\nexport CLAUDE_CODE_OAUTH_TOKEN='" + TOKEN + "'  # renewed\r\n",
                 encoding="utf-8")
    assert sdk_backend.file_oauth_token() == TOKEN
    f.write_text("CLAUDE_CODE_OAUTH_TOKEN=\n")
    assert sdk_backend.file_oauth_token() is None
