# Claude Agent SDK in Docker

With the SDK backend every agent runs as a Claude Code session. xqo always installs it, in the image too:

```bash
./docker-startup.sh                # Linux / macOS / WSL2
docker-startup.bat                 # Windows
```

Claude Code signs in with the first of these that is set (the dashboard shows which one, never the secret):

| Order | Sign-in | Set it with |
|---|---|---|
| 1 | API key | `ANTHROPIC_API_KEY` (or an agent's `api_key_env`) |
| 2 | Auth token | `ANTHROPIC_AUTH_TOKEN` |
| 3 | claude.ai subscription (OAuth) | `CLAUDE_CODE_OAUTH_TOKEN` |
| 4 | Bedrock / Vertex | `CLAUDE_CODE_USE_BEDROCK` / `CLAUDE_CODE_USE_VERTEX` |
| 5 | Claude Code login | `~/.claude/.credentials.json` in the container |

An API key always wins, so leave `ANTHROPIC_API_KEY` unset when you want the subscription.

## Use a claude.ai subscription (OAuth token)

1. On any machine with Claude Code: `claude setup-token`. It opens the claude.ai login and prints a token
   (`sk-ant-oat…`).
2. Put it in a `.env` file next to `docker-compose.yml` (Compose reads it by itself; keep the file private and out
   of git):

   ```
   CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat...
   ```

   Or export it in the shell: `export CLAUDE_CODE_OAUTH_TOKEN=...` (Windows: `set CLAUDE_CODE_OAUTH_TOKEN=...`).
3. Start: `./docker-startup.sh`.
4. Check: `./docker-startup.sh auth`; the dashboard's provider info says `oauth token (file /run/xqo/config/.env)`.

`docker-compose.yml` passes `ANTHROPIC_API_KEY` and `CLAUDE_CODE_OAUTH_TOKEN` into the container. The startup
scripts warn only when neither is set.

## Check the sign-in

One command, while xqo runs:

```
./docker-startup.sh auth        # Windows: docker-startup.bat auth
```

It runs `python -m xqo.sdk_check` in the container and prints:

1. the credentials in the container's environment (first characters and length only) and common paste mistakes:
   quotes, spaces, a byte-order mark, a cut-off or wrong kind of token, both a key and a token set;
2. the sign-in xqo will use for claude-sdk agents;
3. the result of one tiny call with the bundled Claude Code ("Reply with exactly: OK"), e.g.
   `FAILED: Failed to authenticate. API Error: 401 OAuth access token is invalid.`

Exit code 0 means the sign-in works. `--no-call` (when run directly) skips the model call. Outside Docker:
`python -m xqo.sdk_check`.

The same checks by hand:

1. The token is in the container and no key overrides it:
   `docker compose exec xqo sh -c "printenv CLAUDE_CODE_OAUTH_TOKEN | cut -c1-14; echo key=[$(printenv ANTHROPIC_API_KEY)]"`
   shows `sk-ant-oat01-…` and `key=[]`.
2. xqo picked it: the agent's provider chip in the **Agents** panel says `sign-in: oauth token
   (file /run/xqo/config/.env)` (also `provider_info.auth` in `/api/agents`).
3. The token works: one call with the Claude Code bundled in the image:
   `docker compose exec xqo /usr/local/lib/python3.12/site-packages/claude_agent_sdk/_bundled/claude -p "Reply with OK"`
   prints `OK`; an authentication error means a wrong or expired token.

When Claude Code can't sign in, the task fails with "The Claude Agent SDK could not sign in … (authentication_failed)".
xqo falls back to the Anthropic API only when `ANTHROPIC_API_KEY` is set.

## When the token expires (no restart needed)

A `setup-token` token lasts about a year. It also stops working if you sign out everywhere or revoke it on
claude.ai. Tasks then fail with "The Claude Agent SDK could not sign in … (authentication_failed)".

1. `claude setup-token` again.
2. Replace the value in `.env`. That's all: the **next** chat or task uses the new token. No restart, no rebuild.
3. Check: `./docker-startup.sh auth` (Windows: `docker-startup.bat auth`).

How it works: `docker-compose.yml` mounts the xqo folder read-only at `/run/xqo/config` and sets
`XQO_OAUTH_TOKEN_FILE=/run/xqo/config/.env`. Before every Claude Code session xqo reads the `CLAUDE_CODE_OAUTH_TOKEN=`
line from that file; it wins over the environment variable the container started with, which is only the fallback
when `.env` has no such line. Sessions already running keep the token they started with. Only this one variable is
read live: other `.env` changes (an `ANTHROPIC_API_KEY`, `*_IMAGE`) still need `docker-startup restart --no-build`
or a rebuild. The first time, after updating to this version, recreate the container once
(`docker-startup restart --no-build`) so the mount exists.

Outside Docker, put the token (alone, or as a `CLAUDE_CODE_OAUTH_TOKEN=` line) in
`<data dir>/secrets/claude-oauth-token`, or point `XQO_OAUTH_TOKEN_FILE` at any file. It is outside `artifacts/`,
so it is never served, zipped or backed up.

Note: code that agents run on the `local` sandbox backend runs in the same container and could read `.env` there,
as it could read the token file; the Docker sandbox backend (`pip install xqo-docker`) is the option when that matters.

## Copy the Claude Code login into .env, on demand

If you are signed in to Claude Code on the machine that runs Docker, a script copies its login token into `.env`:

```
./docker-startup.sh token       # Windows: docker-startup.bat token
```

(or `claude-token-to-env.sh` / `claude-token-to-env.ps1` directly; `--dry-run` / `-DryRun` shows without writing).
It reads `~/.claude/.credentials.json` (`%USERPROFILE%\.claude\.credentials.json`, or `$CLAUDE_CONFIG_DIR`; on macOS
the Keychain entry "Claude Code-credentials"), replaces only the `CLAUDE_CODE_OAUTH_TOKEN` line in `.env` (other lines
stay, duplicates collapse) and prints the token masked. xqo uses it for the next chat, no restart.

**This is the login's short-lived access token** (a few hours), not the long-lived `setup-token` one. Claude Code
refreshes it only while it runs, so run `claude` once, then the script, whenever the token has expired. An expired
token is refused (`--force` / `-Force` writes it anyway). For something you set once, use `claude setup-token`.

## Alternative: mount a Claude Code login (Linux)

Add to `docker-compose.yml` under `volumes:`

```yaml
      - ${HOME}/.claude:/home/app/.claude
```

Claude Code refreshes this login by itself, so there is no yearly renewal. It needs a logged-in `~/.claude` with
`.credentials.json` (macOS keeps the login in the Keychain, so this does not work there), and the container
(uid 1000) must be able to write the folder.

## Notes

- Usage counts against your claude.ai plan's limits, shared by every agent and by your own Claude Code use.
- Anyone with the token can use your plan: keep it out of shared files, images and logs.
- Agents with `"provider": "anthropic"` (and the API path in general) still need `ANTHROPIC_API_KEY`.
- To stay on the API although the SDK is installed: `"default_provider": "anthropic"` in agents.json, or
  `XQO_PROVIDER=anthropic`.
- Untested here: a live sign-in with a real token or key.
