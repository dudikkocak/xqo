@echo off
rem Build and run xqo in Docker Desktop (Windows). Run from the xqo folder (next to Dockerfile).
rem   docker-startup.bat [start^|stop^|restart^|logs^|status^|auth^|token^|mock] [--node] [--port N] [--no-build]
rem   start (default): build the image, start in the background, wait until healthy, open the dashboard
rem   --node:          Node.js in the image (local MCP servers started with npx)
rem   (xqo always includes the Claude Agent SDK; --sdk is accepted and does nothing)
rem   auth:            check the Claude Agent SDK sign-in in the running container (one tiny model call)
rem   token:           copy the Claude Code login (%USERPROFILE%\.claude) into .env as CLAUDE_CODE_OAUTH_TOKEN (short-lived)
rem   mock:            run in the foreground with the offline scripted LLM (no API key needed)
setlocal EnableDelayedExpansion
cd /d "%~dp0"

set "CMD=start"
set "BUILD=1"
if not defined WITH_NODE set "WITH_NODE=0"
if not defined XQO_HOST_PORT set "XQO_HOST_PORT=8000"

:args
if "%~1"=="" goto argsdone
if /i "%~1"=="start"   ( set "CMD=start"   & goto next )
if /i "%~1"=="stop"    ( set "CMD=stop"    & goto next )
if /i "%~1"=="restart" ( set "CMD=restart" & goto next )
if /i "%~1"=="logs"    ( set "CMD=logs"    & goto next )
if /i "%~1"=="status"  ( set "CMD=status"  & goto next )
if /i "%~1"=="auth"    ( set "CMD=auth"    & goto next )
if /i "%~1"=="token"   ( set "CMD=token"   & goto next )
if /i "%~1"=="mock"    ( set "CMD=mock"    & goto next )
if /i "%~1"=="--sdk"   ( goto next )
if /i "%~1"=="--node"  ( set "WITH_NODE=1" & goto next )
if /i "%~1"=="--toolchains" ( echo --toolchains: needs the xqo-toolchains plugin package, not published yet & exit /b 2 )
if /i "%~1"=="--no-build" ( set "BUILD=0" & goto next )
if /i "%~1"=="--docker-sandbox" ( echo --docker-sandbox: needs the xqo-docker plugin package, not published yet & exit /b 2 )
if /i "%~1"=="--port"  ( set "XQO_HOST_PORT=%~2" & shift & goto next )
if /i "%~1"=="--help"  ( findstr /b "rem" "%~f0" & exit /b 0 )
echo unknown argument: %~1
exit /b 2
:next
shift
goto args
:argsdone

if /i "%CMD%"=="token" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0claude-token-to-env.ps1"
  exit /b !errorlevel!
)
where docker >nul 2>&1 || ( echo Docker is not installed: https://docs.docker.com/desktop/setup/install/windows-install/ & exit /b 1 )
docker info >nul 2>&1 || ( echo Docker Desktop is not running. Start it and try again. & exit /b 1 )
docker compose version >nul 2>&1 || ( echo Docker Compose v2 is missing. & exit /b 1 )
if not exist Dockerfile ( echo Run this from the xqo folder ^(Dockerfile and docker-compose.yml^). & exit /b 1 )
if not exist docker-compose.yml ( echo Run this from the xqo folder ^(Dockerfile and docker-compose.yml^). & exit /b 1 )

rem docker-compose.yml maps /data to ${PWD}/data for the dashboard's folder links
set "PWD=%CD%"
set "URL=http://127.0.0.1:%XQO_HOST_PORT%"
if not exist data mkdir data
if not exist data\agents.json echo note: no data\agents.json, using the roster packaged in the image ^(copy agents.json into data\ to change it^)
rem a sign-in from the environment, or from .env (Compose reads .env by itself)
set "HAS_LOGIN=0"
if defined ANTHROPIC_API_KEY set "HAS_LOGIN=1"
if defined CLAUDE_CODE_OAUTH_TOKEN set "HAS_LOGIN=1"
if exist .env findstr /r /i /c:"^ *ANTHROPIC_API_KEY *= *[^ #]" /c:"^ *CLAUDE_CODE_OAUTH_TOKEN *= *[^ #]" .env >nul 2>&1 && set "HAS_LOGIN=1"
if "%HAS_LOGIN%"=="0" if /i not "%CMD%"=="auth" if /i not "%CMD%"=="mock" if /i not "%CMD%"=="stop" echo warning: neither ANTHROPIC_API_KEY nor CLAUDE_CODE_OAUTH_TOKEN is set; agents cannot call the model ^(use "mock" to try it offline^)

if /i "%CMD%"=="stop"   ( docker compose down & exit /b !errorlevel! )
if /i "%CMD%"=="auth" (
  docker compose ps --status running -q xqo | findstr . >nul || ( echo xqo is not running: start it first ^(docker-startup.bat^) & exit /b 1 )
  docker compose exec -T xqo python -m xqo.sdk_check
  exit /b !errorlevel!
)
if /i "%CMD%"=="logs"   ( docker compose logs -f xqo & exit /b !errorlevel! )
if /i "%CMD%"=="status" (
  docker compose ps
  curl.exe -fsS "%URL%/api/health" || echo not answering on %URL%
  exit /b 0
)
if /i "%CMD%"=="mock" (
  if "%BUILD%"=="1" ( docker compose build || exit /b 1 )
  echo mock mode ^(offline LLM^) on %URL%, Ctrl+C to stop
  docker compose run --rm --service-ports xqo xqo --mock --config /data/agents.json serve
  exit /b !errorlevel!
)

rem start / restart
if /i "%CMD%"=="restart" docker compose down
if "%BUILD%"=="1" ( docker compose up -d --build || exit /b 1 ) else ( docker compose up -d || exit /b 1 )
<nul set /p "=waiting for %URL% "
for /l %%i in (1,1,60) do (
  curl.exe -fsS "%URL%/api/health" >nul 2>&1 && (
    echo.
    echo xqo is up: %URL%
    start "" "%URL%"
    exit /b 0
  )
  <nul set /p "=."
  timeout /t 2 /nobreak >nul
)
echo.
echo xqo did not answer in 2 minutes; recent logs:
docker compose logs --tail 50 xqo
exit /b 1
