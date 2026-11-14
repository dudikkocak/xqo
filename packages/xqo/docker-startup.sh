#!/usr/bin/env bash
# Build and run xqo in Docker (Linux / macOS). Run from the xqo folder (next to Dockerfile).
#   ./docker-startup.sh [start|stop|restart|logs|status|auth|token|mock] [--node] [--port N] [--no-build]
#   start (default): build the image, start in the background, wait until healthy
#   --node:          Node.js in the image (local MCP servers started with npx)
#   (xqo-lite always includes the Claude Agent SDK; --sdk is accepted and does nothing)
#   auth:            check the Claude Agent SDK sign-in in the running container (one tiny model call)
#   token:           copy the Claude Code login (~/.claude) into .env as CLAUDE_CODE_OAUTH_TOKEN (short-lived)
#   mock:            run in the foreground with the offline scripted LLM (no API key needed)
set -euo pipefail
cd "$(dirname "$0")"

cmd=start; build=1
export WITH_NODE="${WITH_NODE:-0}" XQO_HOST_PORT="${XQO_HOST_PORT:-8000}"
while [ $# -gt 0 ]; do
  case "$1" in
    start|stop|restart|logs|status|auth|token|mock) cmd=$1 ;;
    --sdk) ;;  # always included in xqo-lite
    --node) WITH_NODE=1 ;;
    --toolchains|--docker-sandbox) echo "$1: not included in xqo-lite (use full xqo)" >&2; exit 2 ;;
    --port) XQO_HOST_PORT="$2"; shift ;;
    --no-build) build=0 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

[ "$cmd" = token ] && exec ./claude-token-to-env.sh
command -v docker >/dev/null || { echo "Docker is not installed: https://docs.docker.com/get-docker/" >&2; exit 1; }
docker info >/dev/null 2>&1 || { echo "The Docker daemon is not running (or you lack permission: add yourself to the docker group)." >&2; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "Docker Compose v2 is missing (docker compose ...)." >&2; exit 1; }
[ -f Dockerfile ] && [ -f docker-compose.yml ] || { echo "Run this from the xqo folder (Dockerfile and docker-compose.yml)." >&2; exit 1; }

export PWD="$(pwd)"
url="http://127.0.0.1:${XQO_HOST_PORT}"

# ./data is mounted at /data; the container runs as uid 1000, so make sure it can write there
mkdir -p data
if [ "$(id -u)" != "1000" ] && [ "$(stat -c %u data 2>/dev/null || echo 1000)" != "1000" ]; then
  chmod a+rwx data
fi
[ -f data/agents.json ] || echo "note: no data/agents.json, using the roster packaged in the image (copy agents.json into data/ to change it)"
# a sign-in from the shell, or from .env (Compose reads .env by itself)
has_login=0
[ -n "${ANTHROPIC_API_KEY:-}${CLAUDE_CODE_OAUTH_TOKEN:-}" ] && has_login=1
[ -f .env ] && grep -Eq '^[[:space:]]*(export[[:space:]]+)?(ANTHROPIC_API_KEY|CLAUDE_CODE_OAUTH_TOKEN)[[:space:]]*=[[:space:]]*[^[:space:]#]' .env && has_login=1
[ "$has_login" = 1 ] || [ "$cmd" = mock ] || [ "$cmd" = stop ] || [ "$cmd" = logs ] || [ "$cmd" = status ] || [ "$cmd" = auth ] \
  || echo "warning: neither ANTHROPIC_API_KEY nor CLAUDE_CODE_OAUTH_TOKEN is set; agents cannot call the model (use 'mock' to try it offline)"

case "$cmd" in
  start|restart)
    [ "$cmd" = restart ] && docker compose down
    if [ "$build" = 1 ]; then docker compose up -d --build; else docker compose up -d; fi
    printf 'waiting for %s ' "$url"
    for _ in $(seq 1 60); do
      if curl -fsS "$url/api/health" >/dev/null 2>&1; then echo; echo "xqo is up: $url"; exit 0; fi
      printf '.'; sleep 2
    done
    echo; echo "xqo did not answer in 2 minutes; recent logs:" >&2; docker compose logs --tail 50 xqo >&2; exit 1 ;;
  mock)
    [ "$build" = 1 ] && docker compose build
    echo "mock mode (offline LLM) on $url, Ctrl+C to stop"
    docker compose run --rm --service-ports xqo xqo --mock --config /data/agents.json serve ;;
  stop)   docker compose down ;;
  logs)   docker compose logs -f xqo ;;
  auth)
    docker compose ps --status running -q xqo | grep -q . || { echo "xqo is not running: start it first (./docker-startup.sh)" >&2; exit 1; }
    docker compose exec -T xqo python -m xqo.sdk_check ;;
  status) docker compose ps; curl -fsS "$url/api/health" && echo || echo "not answering on $url" ;;
esac
