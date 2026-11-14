"""Check the Claude Agent SDK sign-in: `python -m xqo.sdk_check` (in Docker: `docker-startup.sh auth`).

1. which credentials are in the environment (never printed in full) and common mistakes in them,
2. which sign-in xqo will use for claude-sdk agents,
3. one real call with the Claude Code that ships with the SDK ("Reply with OK").

Exit code 0 when the call answered, 1 otherwise. `--no-call` skips step 3.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

PROMPT = "Reply with exactly: OK"
CREDENTIAL_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")


def mask(value: str) -> str:
    v = value.strip()
    return f"{v[:14]}… ({len(value)} chars)" if len(v) > 18 else f"({len(value)} chars)"


def problems(name: str, value: str) -> list[str]:
    """Typical copy-paste mistakes in a credential (from .env files, Windows editors, shells)."""
    out = []
    if value != value.strip():
        out.append("has spaces or line breaks around it")
    if value[:1] in "\"'" or value[-1:] in "\"'":
        out.append("is wrapped in quotes")
    if "﻿" in value or "\r" in value:
        out.append("contains a byte-order mark or a Windows line ending (re-save .env as plain UTF-8)")
    v = value.strip().strip("\"'")
    if name == "CLAUDE_CODE_OAUTH_TOKEN" and v and not v.startswith("sk-ant-oat"):
        out.append("does not start with sk-ant-oat (expected the token from `claude setup-token`)")
    if name == "ANTHROPIC_API_KEY" and v and not v.startswith("sk-ant-"):
        out.append("does not start with sk-ant- (expected an API key from the Anthropic Console)")
    if v and len(v) < 40:
        out.append("looks too short (cut off when pasted?)")
    return out


def find_cli() -> str | None:
    """XQO_CLAUDE_CLI, else the Claude Code bundled with claude-agent-sdk, else `claude` on PATH."""
    if os.getenv("XQO_CLAUDE_CLI"):
        return os.environ["XQO_CLAUDE_CLI"]
    try:
        import claude_agent_sdk
        bundled = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
        if bundled.is_file():
            return str(bundled)
    except ImportError:
        pass
    return shutil.which("claude")


def live_call(cli: str, timeout: float) -> tuple[bool, str]:
    try:
        from .sdk_backend import file_oauth_token
        env = dict(os.environ)
        if not env.get("ANTHROPIC_API_KEY") and (token := file_oauth_token()):
            env["CLAUDE_CODE_OAUTH_TOKEN"] = token  # as xqo does for each session
        p = subprocess.run([cli, "-p", PROMPT, "--output-format", "json"], capture_output=True, text=True,
                           timeout=timeout, stdin=subprocess.DEVNULL, env=env)
    except subprocess.TimeoutExpired:
        return False, f"no answer within {timeout:.0f}s (network, proxy or firewall?)"
    except OSError as e:
        return False, f"Claude Code could not start: {e}"
    out, err = (p.stdout or "").strip(), (p.stderr or "").strip()
    try:
        data = json.loads(out.splitlines()[-1]) if out else {}
    except (json.JSONDecodeError, IndexError):
        data = {}
    if data:
        text = str(data.get("result") or "").strip()
        if not data.get("is_error") and p.returncode == 0:
            return True, f"answered {text[:60]!r}"
        return False, (text or err or f"exit code {p.returncode}")[:500]
    if p.returncode == 0 and out:
        return True, f"answered {out[:60]!r}"
    return False, (err or out or f"exit code {p.returncode}")[:500]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m xqo.sdk_check", description=__doc__.splitlines()[0])
    ap.add_argument("--no-call", action="store_true", help="only check the environment, no model call")
    ap.add_argument("--timeout", type=float, default=120, help="seconds to wait for the call (default 120)")
    a = ap.parse_args(argv)
    ok = True

    print("1. Credentials in the environment")
    found = False
    for name in CREDENTIAL_VARS:
        value = os.environ.get(name)
        if value is None:
            print(f"   {name}: not set")
            continue
        if not value.strip():
            print(f"   {name}: set but EMPTY")
            continue
        found = True
        issues = problems(name, value)
        print(f"   {name}: {mask(value)}" + ("" if not issues else "  <- " + "; ".join(issues)))
        ok = ok and not issues
    from .sdk_backend import file_oauth_token, oauth_token_file
    tf, ftoken = oauth_token_file(), file_oauth_token()
    if ftoken:
        found = True
        issues = [i for i in problems("CLAUDE_CODE_OAUTH_TOKEN", ftoken) if "quotes" not in i and "spaces" not in i]
        print(f"   token file {tf}: {mask(ftoken)}  (read before every session; wins over CLAUDE_CODE_OAUTH_TOKEN)"
              + ("" if not issues else "  <- " + "; ".join(issues)))
        ok = ok and not issues
    else:
        print(f"   token file {tf}: " + ("empty" if tf.is_file() else "not there"))
    if os.getenv("ANTHROPIC_API_KEY") and (ftoken or os.getenv("CLAUDE_CODE_OAUTH_TOKEN")):
        print("   note: both are set; ANTHROPIC_API_KEY wins, the OAuth token is not used")
    if not found:
        print("   none set (Claude Code can still use a login in ~/.claude)")

    print("2. What xqo will use")
    from .providers import default_provider, sdk_installed
    from .sdk_backend import auth_method
    if not sdk_installed():
        print("   claude-agent-sdk is NOT installed: agents use the Anthropic API (build with --sdk / EXTRAS=mcp,sdk)")
        return 1
    method = auth_method()
    print(f"   default provider: {default_provider()}")
    print(f"   sign-in for claude-sdk agents: {method}")
    if method == "none found":
        ok = False

    if a.no_call:
        print("3. Live call: skipped (--no-call)")
    else:
        cli = find_cli()
        print(f"3. Live call with Claude Code ({cli or 'not found'})")
        if not cli:
            print("   FAILED: Claude Code not found (reinstall claude-agent-sdk)")
            return 1
        good, detail = live_call(cli, a.timeout)
        print(f"   {'OK' if good else 'FAILED'}: {detail}")
        if not good:
            low = detail.lower()
            if any(w in low for w in ("401", "auth", "invalid", "expired", "login", "credential")):
                print("   -> the credential was rejected: run `claude setup-token` again and put the new token in .env "
                      "(Docker) or the token file above; the next session uses it, no restart needed")
        ok = ok and good

    print("RESULT:", "sign-in works" if ok else "problems found (see above)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
