<#
Copy the Claude Code login (OAuth access token) into .env as CLAUDE_CODE_OAUTH_TOKEN, on demand.
  powershell -ExecutionPolicy Bypass -File claude-token-to-env.ps1 [-File CREDENTIALS_JSON] [-EnvFile .env] [-DryRun] [-Force]
Reads %CLAUDE_CONFIG_DIR%\.credentials.json (default %USERPROFILE%\.claude\.credentials.json).
Only the CLAUDE_CODE_OAUTH_TOKEN line of .env is changed. The token is never printed in full.
Note: this is the login's short-lived access token (hours). For a long-lived one use `claude setup-token`.
#>
param([string]$File, [string]$EnvFile = ".env", [switch]$DryRun, [switch]$Force)
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

if (-not $File) {
  $dir = if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { Join-Path $env:USERPROFILE ".claude" }
  $File = Join-Path $dir ".credentials.json"
}
if (-not (Test-Path -LiteralPath $File)) {
  Write-Error "No Claude Code login found ($File). Run 'claude' and /login first, or pass -File."; exit 1
}
try { $d = Get-Content -LiteralPath $File -Raw | ConvertFrom-Json } catch { Write-Error "Could not read $File (not JSON?)"; exit 1 }
$o = if ($d.claudeAiOauth) { $d.claudeAiOauth } else { $d }
$token = [string]$o.accessToken
if (-not $token) { Write-Error "No accessToken in ${File}: sign in again with 'claude' and /login."; exit 1 }

if ($o.expiresAt) {
  $left = [int64]$o.expiresAt - [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
  if ($left -le 0) {
    Write-Host "That token already expired. Open Claude Code once (it refreshes the login), then run this again (-Force writes it anyway)."
    if (-not $Force) { exit 1 }
  }
  else { Write-Host ("token expires in about {0} h {1} min" -f [int][math]::Floor($left / 3600000), [int]([math]::Floor($left / 60000) % 60)) }
}
Write-Host ("token: {0}... ({1} chars)" -f $token.Substring(0, [math]::Min(14, $token.Length)), $token.Length)
if ($DryRun) { Write-Host "dry run: $EnvFile not changed"; exit 0 }

$lines = @(); $found = $false
if (Test-Path -LiteralPath $EnvFile) {
  foreach ($l in (Get-Content -LiteralPath $EnvFile)) {
    if ($l -match '^\s*(export\s+)?CLAUDE_CODE_OAUTH_TOKEN\s*=') {
      if (-not $found) { $lines += "CLAUDE_CODE_OAUTH_TOKEN=$token"; $found = $true }
    } else { $lines += $l }
  }
}
if (-not $found) { $lines += "CLAUDE_CODE_OAUTH_TOKEN=$token" }
# plain UTF-8 without a byte-order mark: a BOM can break the first line for Docker Compose
$tmp = "$EnvFile.tmp"
[System.IO.File]::WriteAllText((Join-Path (Get-Location) $tmp), (($lines -join "`n") + "`n"), (New-Object System.Text.UTF8Encoding($false)))
Move-Item -LiteralPath $tmp -Destination $EnvFile -Force
Write-Host "updated ${EnvFile}: xqo uses it for the next chat (no restart). Check: docker-startup.bat auth"
