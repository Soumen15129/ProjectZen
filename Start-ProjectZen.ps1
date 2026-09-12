#Requires -Version 5.1
<#
  Start-ProjectZen.ps1 - launcher for both an installed copy and a dev checkout.

  Steps, in order, each skipped when already satisfied:
    1. Locate Python        (bundled with the install, else system)
    2. Verify dependencies  (installed builds ship them; a checkout may pip install)
    3. Locate the Claude CLI (ships inside claude-agent-sdk - no Node, no npm)
    4. Confirm the Claude sign-in, running the right browser flow once if needed
       (personal Pro/Max accounts and company SSO accounts need different flows)
    5. Pick a free port and start the server on 127.0.0.1
    6. Wait for health, then open the browser

  Two deliberate changes from the earlier version:
    * Binds 127.0.0.1, not 0.0.0.0. The old binding exposed the app to the whole
      office network and triggered a Windows Firewall prompt on first run.
    * Asks the OS for a free port instead of assuming 8001, which failed outright
      if a second user, a stale process or any unrelated service held that port.
#>

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot

function Say($m)  { Write-Host $m }
function Step($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "    $m" -ForegroundColor Green }
function Warn($m) { Write-Host "    $m" -ForegroundColor Yellow }
function Fail($m) { Write-Host "`n[ERROR] $m" -ForegroundColor Red }

function Stop-Here($code) {
    Write-Host "`nPress any key to close..."
    try { $null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown") } catch { Start-Sleep 5 }
    exit $code
}

Write-Host "======================================================"
Write-Host "   ProjectZen" -ForegroundColor White
Write-Host "======================================================"

# -- 1. Python -------------------------------------------------------------
Step "Locating Python"
$Python = Join-Path $Root "python\python.exe"       # installed build
if (-not (Test-Path $Python)) {
    $sys = Get-Command python -ErrorAction SilentlyContinue
    if (-not $sys) { $sys = Get-Command py -ErrorAction SilentlyContinue }
    if (-not $sys) {
        Fail "Python was not found."
        Say  "The installed build ships its own Python. If you are running from a"
        Say  "source checkout, install Python 3.10+ from https://python.org and retry."
        Stop-Here 1
    }
    $Python = $sys.Source
}
$pyVer = & $Python -c "import sys;print(sys.version.split()[0])"
Ok "Python $pyVer"

$Middleware = Join-Path $Root "middleware"
if (-not (Test-Path (Join-Path $Middleware "app.py"))) {
    Fail "middleware\app.py was not found next to this script. The installation looks incomplete."
    Stop-Here 1
}

# -- 2. Dependencies -------------------------------------------------------
Step "Checking dependencies"
& $Python -c "import fastapi, uvicorn, claude_agent_sdk, openpyxl, langgraph" 2>$null
if ($LASTEXITCODE -ne 0) {
    $wheels = Join-Path $Root "wheels"
    if (Test-Path $wheels) {
        Warn "Installing bundled packages (one time, offline)..."
        & $Python -m pip install --no-index --find-links "$wheels" -r (Join-Path $Middleware "requirements.txt") --quiet
    } else {
        Warn "Installing packages from PyPI (one time)..."
        & $Python -m pip install -r (Join-Path $Middleware "requirements.txt") --quiet
    }
    if ($LASTEXITCODE -ne 0) { Fail "Dependency installation failed."; Stop-Here 1 }
}
Ok "All packages present"

# -- 3. Claude CLI ---------------------------------------------------------
# The CLI ships inside claude-agent-sdk and the SDK prefers its bundled copy over
# PATH, so there is nothing for the user to install. We only need its location to
# drive the sign-in check below.
Step "Locating the Claude CLI"
$FindCli = Join-Path $Middleware "find_cli.py"
$Cli = ""
if (Test-Path $FindCli) { $Cli = (& $Python $FindCli).Trim() }
if (-not $Cli -or -not (Test-Path $Cli)) {
    Fail "No Claude CLI was found."
    Say  "It normally ships inside claude-agent-sdk. Repair it with:"
    Say  "    `"$Python`" -m pip install --force-reinstall claude-agent-sdk"
    Stop-Here 1
}
Ok "Using $Cli"

# -- 4. Sign-in ------------------------------------------------------------
# ProjectZen runs on whatever Claude Code access the user already has - Pro, Max
# or a company account. The CLI exposes two different browser flows for that and
# picking the wrong one is a hard stop, not a fallback:
#
#     claude auth login          -> defaults to --claudeai, the personal-subscription
#                                   flow, which answers a company account with
#                                   "Claude Max or Pro is required to connect".
#     claude auth login --sso    -> the single sign-on flow company accounts need.
#
# The earlier version only ever ran the first form, so every SSO user was refused
# at step 4 no matter what access they held. We ask once instead of guessing.
Step "Checking your Claude sign-in"

function Test-ClaudeSignIn($cli) {
    try {
        $st = & $cli auth status --json 2>$null | ConvertFrom-Json
        if ($st -and ($st.loggedIn -or $st.authenticated -or $st.account)) { return $true }
    } catch {}
    return $false
}

# Describe whoever is currently signed in, for the prompts below.
function Get-ClaudeAccount($cli) {
    try { return (& $cli auth status --json 2>$null | ConvertFrom-Json) } catch { return $null }
}

function Format-ClaudeAccount($st) {
    if (-not $st) { return "an unknown account" }
    $tag = ""
    if     ($st.subscriptionType) { $tag = $st.subscriptionType }
    elseif ($st.orgName)          { $tag = $st.orgName }
    if     ($st.email -and $tag)  { return "$($st.email) ($tag)" }
    elseif ($st.email)            { return $st.email }
    return "an unknown account"
}

# The sign-in conversation, shared by first-run and switch-account.
#
# Extracted because there was no way to CHANGE accounts: the launcher only offered
# sign-in when signed OUT, so a user who had been using a personal account and later
# received a company one had no route to it — the launcher silently kept using the old
# session and billed the wrong seat. Logging out of claude.ai in a BROWSER does not
# touch this, because the CLI keeps its own token in ~/.claude/.credentials.json.
function Invoke-ClaudeSignIn($cli) {
    Say  ""
    Say  "  ProjectZen never asks for or stores an API key. It signs in with your"
    Say  "  own Claude account and uses your own Claude Code access."
    Say  ""
    Say  "  How do you sign in to Claude?"
    Say  ""
    Say  "    [1] Personal Claude account   (Pro or Max)"
    Say  "    [2] Company / work account    (single sign-on)"
    Say  ""

    $choice = ""
    while ($choice -ne "1" -and $choice -ne "2") {
        $choice = (Read-Host "  Choose 1 or 2").Trim()
        if ($choice -ne "1" -and $choice -ne "2") { Warn "Please type 1 or 2." }
    }

    # Built as an array and splatted so the personal path stays byte-for-byte the
    # command that already works today.
    $loginArgs = @("auth", "login")
    if ($choice -eq "2") {
        $loginArgs += "--sso"
        Say ""
        $email = (Read-Host "  Work email address (optional - press Enter to skip)").Trim()
        if ($email) { $loginArgs += @("--email", $email) }
    }

    Say  ""
    Say  "  A browser window will open. Complete the sign-in there, then come back."
    Say  "  Press Enter to continue..."
    [void](Read-Host)

    & $cli @loginArgs
    $ok = Test-ClaudeSignIn $cli

    # The one wrong answer that is worth recovering from in place. A user with a
    # company account will not necessarily know that is what they have, and making
    # them rerun the whole launcher to try the other button is a poor trade.
    if (-not $ok -and $choice -eq "1") {
        Say  ""
        Warn "That sign-in did not complete."
        Say  "  If the page said 'Claude Max or Pro is required', this is very likely"
        Say  "  a company account rather than a personal one."
        Say  ""
        $again = (Read-Host "  Try again with single sign-on? (y/n)").Trim().ToLower()
        if ($again -eq "y") {
            & $cli auth login --sso
            $ok = Test-ClaudeSignIn $cli
        }
    }
    return $ok
}

function Show-SignInHelp($cli) {
    Say  ""
    Say  "  Two things to check, in this order:"
    Say  ""
    Say  "   1. Account type. Personal accounts use option 1. Accounts you sign"
    Say  "      into through your employer use option 2. You can also run the"
    Say  "      sign-in by hand and read the full error:"
    Say  "          `"$cli`" auth login --sso"
    Say  ""
    Say  "   2. Claude Code access. ProjectZen runs on Claude Code, which has to"
    Say  "      be enabled for your Claude account. If sign-in is refused whichever"
    Say  "      option you pick, ask your Claude administrator to enable Claude"
    Say  "      Code for your seat, then run this launcher again."
    Say  ""
}

$loggedIn = Test-ClaudeSignIn $Cli

if (-not $loggedIn) {
    Warn "Not signed in yet - this is a one-time step."
    $loggedIn = Invoke-ClaudeSignIn $Cli
    if (-not $loggedIn) {
        Fail "Sign-in did not complete."
        Show-SignInHelp $Cli
        Stop-Here 1
    }
} else {
    # Offer to switch, on a timer. A prompt that BLOCKS would break every unattended
    # start and be an irritation on every attended one, so continuing is the default
    # and switching is opt-in. Shown every launch because the account is the thing
    # that decides which seat gets billed.
    $acct = Get-ClaudeAccount $Cli
    Say  ""
    Say  "  Currently signed in as $(Format-ClaudeAccount $acct)"
    Say  "  Press S within 5 seconds to sign in as someone else, or wait to continue."

    $switch = $false
    try {
        $deadline = (Get-Date).AddSeconds(5)
        while ((Get-Date) -lt $deadline) {
            if ($Host.UI.RawUI.KeyAvailable) {
                $k = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
                if ("$($k.Character)".ToLower() -eq "s") { $switch = $true; break }
            }
            Start-Sleep -Milliseconds 150
        }
    } catch {
        # No interactive console (scheduled task, redirected input). Continuing with
        # the existing account is the only safe behaviour - never hang a startup.
        $switch = $false
    }

    if ($switch) {
        Say ""
        Step "Switching Claude account"
        # Logging out FIRST matters: `auth login` on an active session can re-use it
        # and hand back the same account, which looks like the switch silently failed.
        & $Cli auth logout 2>$null | Out-Null
        $loggedIn = Invoke-ClaudeSignIn $Cli
        if (-not $loggedIn) {
            Fail "Sign-in did not complete - you are now signed out."
            Show-SignInHelp $Cli
            Stop-Here 1
        }
    }
}
# Name the account and the plan. `auth status --json` already carries email,
# orgName and subscriptionType, and printing them means a support question never
# has to start with "which Claude account is this actually running as?".
# Re-read rather than reuse the earlier value: after a switch the old one is stale,
# and printing the account that is no longer in use would be worse than printing none.
$st = Get-ClaudeAccount $Cli
if ($st -and $st.email) {
    Ok "Signed in as $(Format-ClaudeAccount $st)"
    if ($st.orgName -and $st.orgName -notmatch [regex]::Escape("$($st.email)'s Organization")) {
        Say "    Organisation: $($st.orgName)"
    }
} else {
    Ok "Signed in"
}

# -- 5. Port + server ------------------------------------------------------
Step "Starting the ProjectZen server"
$Port = (& $Python -c "import socket;s=socket.socket();s.bind(('127.0.0.1',0));print(s.getsockname()[1]);s.close()").ToString().Trim()
$Base = "http://127.0.0.1:$Port"

# Capture the server's output. It used to run in a minimised window with nothing
# recorded, so when a generation failed the only real explanation - the traceback -
# was unreachable. The log lives next to the user's data, not in the install folder,
# which may be read-only.
$LogDir = Join-Path $env:LOCALAPPDATA "ProjectZen\logs"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
$LogFile = Join-Path $LogDir ("server-" + (Get-Date -Format "yyyyMMdd-HHmmss") + ".log")

$server = Start-Process -WorkingDirectory $Middleware -FilePath $Python `
    -ArgumentList "-m","uvicorn","app:app","--host","127.0.0.1","--port","$Port" `
    -PassThru -WindowStyle Minimized `
    -RedirectStandardOutput $LogFile -RedirectStandardError ($LogFile -replace '\.log$','.err.log')

Say "    Port $Port (local only - not reachable from the network)"
Say "    Log  $LogFile"

$ready = $false
foreach ($i in 1..60) {
    Start-Sleep -Milliseconds 700
    if ($server.HasExited) { Fail "The server stopped unexpectedly."; Stop-Here 1 }
    try {
        $h = Invoke-RestMethod "$Base/health" -TimeoutSec 3 -ErrorAction Stop
        if ($h.status -eq "ok") { $ready = $true; break }
    } catch {}
}
if (-not $ready) {
    Fail "The server did not become ready in time."
    Say  "See the log for the reason: $LogFile"
    Stop-Here 1
}
Ok "Server ready"

# -- 6. Confirm Claude is reachable, then open the app ---------------------
Step "Verifying Claude connectivity"
try {
    $llm = Invoke-RestMethod "$Base/health/llm" -TimeoutSec 180 -ErrorAction Stop
    if ($llm.ok) {
        Ok "Claude reachable ($($llm.cli_source))"
    } else {
        Warn "Claude not reachable yet: $($llm.detail)"
        # Signed in but still refused is the case step 4 cannot catch: the account
        # authenticated fine, and Claude Code simply is not enabled on that seat.
        # Without this the user only ever sees documents failing.
        if ("$($llm.detail)" -match "(?i)(authenticat|unauthoriz|not authoriz|forbidden|permission|subscription|entitl|sign[ -]?in|Max or Pro|\b401\b|\b403\b)") {
            Say ""
            Say "  This reads as a Claude access problem rather than a ProjectZen one."
            Say "  You are signed in, but this account may not have Claude Code enabled."
            Say "  Ask your Claude administrator to enable Claude Code for your seat."
            Say ""
        }
    }
} catch {
    Warn "Could not verify Claude connectivity - the app will still open."
}

Step "Opening ProjectZen"
Start-Process "$Base/index.html"
Say ""
Say "ProjectZen is running at $Base"
Say ""
Write-Host "Press any key to STOP ProjectZen and close this window..." -ForegroundColor DarkGray
try { $null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown") } catch { while ($true) { Start-Sleep 3600 } }

if (-not $server.HasExited) { Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue }
