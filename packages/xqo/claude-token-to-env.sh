#!/usr/bin/env bash
# Copy the Claude Code login (OAuth access token) into .env as CLAUDE_CODE_OAUTH_TOKEN, on demand.
#   ./claude-token-to-env.sh [--file CREDENTIALS_JSON] [--env .env] [--dry-run] [--force]
# Reads $CLAUDE_CONFIG_DIR/.credentials.json (default ~/.claude/.credentials.json; on macOS the Keychain entry
# "Claude Code-credentials"). Only the CLAUDE_CODE_OAUTH_TOKEN line of .env is changed. The token is never printed in full.
# Note: this is the login's short-lived access token (hours). For a long-lived one use `claude setup-token`.
set -euo pipefail
cd "$(dirname "$0")"

creds="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/.credentials.json"; envfile=".env"; dry=0; force=0
while [ $# -gt 0 ]; do
  case "$1" in
    --file) creds="$2"; shift ;;
    --env) envfile="$2"; shift ;;
    --dry-run) dry=1 ;;
    --force) force=1 ;;
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

json=""
if [ -f "$creds" ]; then
  json=$(cat "$creds")
elif [ "$(uname)" = Darwin ] && command -v security >/dev/null; then
  json=$(security find-generic-password -s "Claude Code-credentials" -w 2>/dev/null || true)
fi
[ -n "$json" ] || { echo "No Claude Code login found ($creds). Run \`claude\` and /login first, or pass --file." >&2; exit 1; }

# prints "<token>\t<expiresAt ms or empty>"
if command -v python3 >/dev/null; then
  parsed=$(printf '%s' "$json" | python3 -c '
import json, sys
d = json.load(sys.stdin)
o = d.get("claudeAiOauth") or d
t = o.get("accessToken") or ""
print(t + "\t" + str(o.get("expiresAt") or ""))') || { echo "Could not read $creds (not JSON?)" >&2; exit 1; }
elif command -v jq >/dev/null; then
  parsed=$(printf '%s' "$json" | jq -r '(.claudeAiOauth // .) | "\(.accessToken // "")\t\(.expiresAt // "")"')
else
  echo "Needs python3 or jq." >&2; exit 1
fi
token="${parsed%%$'\t'*}"; expires="${parsed#*$'\t'}"
[ -n "$token" ] || { echo "No accessToken in $creds: sign in again with \`claude\` and /login." >&2; exit 1; }

now_ms=$(( $(date +%s) * 1000 ))
if [ -n "$expires" ] && [ "$expires" -le "$now_ms" ] 2>/dev/null; then
  echo "That token already expired. Open Claude Code once (it refreshes the login), then run this again (--force writes it anyway)." >&2
  [ "$force" = 1 ] || exit 1
elif [ -n "$expires" ]; then
  echo "token expires in about $(( (expires - now_ms) / 3600000 )) h $(( (expires - now_ms) / 60000 % 60 )) min"
fi
echo "token: ${token:0:14}... (${#token} chars)"

if [ "$dry" = 1 ]; then echo "dry run: $envfile not changed"; exit 0; fi

tmp=$(mktemp "${envfile}.XXXXXX")
trap 'rm -f "$tmp"' EXIT
found=0
if [ -f "$envfile" ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    if [[ "$line" =~ ^[[:space:]]*(export[[:space:]]+)?CLAUDE_CODE_OAUTH_TOKEN[[:space:]]*= ]]; then
      if [ "$found" = 0 ]; then printf 'CLAUDE_CODE_OAUTH_TOKEN=%s\n' "$token" >> "$tmp"; found=1; fi
    else
      printf '%s\n' "$line" >> "$tmp"
    fi
  done < "$envfile"
fi
[ "$found" = 1 ] || printf 'CLAUDE_CODE_OAUTH_TOKEN=%s\n' "$token" >> "$tmp"
chmod 600 "$tmp"
mv "$tmp" "$envfile"; trap - EXIT
echo "updated $envfile: xqo uses it for the next chat (no restart). Check: ./docker-startup.sh auth"
