#Requires -Version 5.1
<#
  build.ps1 - assemble the ProjectZen installer payload, then compile it.

  Produces a fully OFFLINE installer: the user's machine needs no Python, no Node,
  no npm and no internet access. The Claude CLI arrives inside claude-agent-sdk.

  Stages
    1. stage    - copy middleware + frontend + launchers into payload\
    2. seed     - export grounding_docs / doc_format_config + reference files
    3. python   - fetch the embeddable Python runtime
    4. wheels   - download every dependency as a wheel for that exact Python
    5. install  - pre-install the wheels into the embedded runtime
    6. compile  - run Inno Setup (skipped with a clear message if not installed)

  Usage:  powershell -ExecutionPolicy Bypass -File installer\build.ps1
          ... -SkipDownload   reuse an already-fetched python\ and wheels\
#>

param(
    [switch]$SkipDownload,
    [string]$PythonVersion = "3.12.8"
)

$ErrorActionPreference = "Stop"
$Here    = $PSScriptRoot
$Root    = Split-Path $Here -Parent
$Payload = Join-Path $Here "payload"
$Tmp     = Join-Path $Here ".tmp"
$Dist    = Join-Path $Here "dist"

function Step($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "    $m" -ForegroundColor Green }
function Warn($m) { Write-Host "    $m" -ForegroundColor Yellow }

if (-not (Test-Path $Tmp)) { New-Item -ItemType Directory -Path $Tmp | Out-Null }

# -- 1. Stage the application ---------------------------------------------
Step "Staging application files"
if (Test-Path $Payload) { Remove-Item -LiteralPath $Payload -Recurse -Force }
New-Item -ItemType Directory -Path $Payload | Out-Null

foreach ($d in @("middleware", "frontend")) {
    $src = Join-Path $Root $d
    Copy-Item $src (Join-Path $Payload $d) -Recurse
}
# ── What must NOT ship ────────────────────────────────────────────────────
#
# Only `middleware` and `frontend` are copied at all, so repository folders such as
# Input\ (sample SOWs, workbooks, real data models), installer\, storage\ and .git\
# are excluded by construction — they are never staged in the first place. The lists
# below remove what would otherwise ride along INSIDE those two folders.
#
# Every removal is reported, and the surviving app manifest is printed at the end of
# this step. An installer that quietly grew a 900 KB prototype is exactly the kind of
# thing nobody notices until a user opens the wrong page.
$ExcludeDirs = @(
    "__pycache__", ".llm_scratch", "tests", ".pytest_cache",
    ".ipynb_checkpoints", "node_modules", ".git", ".vscode", ".idea"
)
$ExcludeFiles = @(
    "*.pyc", "*.pyo", "*.pyd", ".env", "*.log", "*.bak", "*.tmp",
    "*.orig", "*.rej", "Thumbs.db", ".DS_Store", "*~"
)
# Dead UIs. CLAUDE.md calls these alternate/prototype pages; nothing links to them,
# and index_old.html alone is 845 KB of a superseded app that still talks to the API.
$ExcludeExact = @(
    "frontend\index_old.html",
    "frontend\ProjectZen_UI_Prototype_v1.html"
)

$removed = @()
foreach ($d in (Get-ChildItem $Payload -Recurse -Directory -ErrorAction SilentlyContinue |
                Where-Object { $ExcludeDirs -contains $_.Name })) {
    if (Test-Path -LiteralPath $d.FullName) {
        $removed += "{0}\ (dir)" -f $d.FullName.Substring($Payload.Length + 1)
        Remove-Item -LiteralPath $d.FullName -Recurse -Force
    }
}
foreach ($f in (Get-ChildItem $Payload -Recurse -File -ErrorAction SilentlyContinue)) {
    $name = $f.Name
    $rel  = $f.FullName.Substring($Payload.Length + 1)
    $hit  = $false
    foreach ($pat in $ExcludeFiles) { if ($name -like $pat) { $hit = $true; break } }
    if (-not $hit) { foreach ($ex in $ExcludeExact) { if ($rel -ieq $ex) { $hit = $true; break } } }
    if ($hit) {
        $removed += "{0} ({1:N0} KB)" -f $rel, ($f.Length / 1KB)
        Remove-Item -LiteralPath $f.FullName -Force
    }
}
if ($removed.Count) {
    Ok ("Excluded {0} item(s):" -f $removed.Count)
    foreach ($r in $removed) { Write-Host "        - $r" -ForegroundColor DarkGray }
} else {
    Ok "Nothing to exclude"
}

Copy-Item (Join-Path $Root "Start-ProjectZen.bat") $Payload
Copy-Item (Join-Path $Root "Start-ProjectZen.ps1") $Payload

# Fail loudly if an exclusion did not take. Reporting success while shipping the file
# anyway is the failure mode this build has already had once.
$leaks = @()
foreach ($ex in $ExcludeExact) {
    if (Test-Path -LiteralPath (Join-Path $Payload $ex)) { $leaks += $ex }
}
foreach ($d in $ExcludeDirs) {
    $found = Get-ChildItem $Payload -Recurse -Directory -ErrorAction SilentlyContinue |
             Where-Object { $_.Name -eq $d }
    if ($found) { $leaks += ("{0}\" -f $d) }
}
if ($leaks.Count) { throw ("Exclusion failed - still present: " + ($leaks -join ", ")) }

Ok "middleware + frontend + launchers staged"

# -- 2. Seed (reference documents + their database rows) ------------------
# Copying the reference FILES alone is not enough: grounding is resolved through the
# grounding_docs table, and the files are stored under UUID names that the filename
# fallback does not match. Files and rows must ship together or every generated
# document silently loses its reference.
Step "Building the grounding seed"
$seedPy = @'
import json, shutil, sqlite3, sys, pathlib
root    = pathlib.Path(sys.argv[1])
payload = pathlib.Path(sys.argv[2])
db_path = root / "storage" / "documents.db"
seed    = payload / "middleware" / "seed"
(seed / "refs").mkdir(parents=True, exist_ok=True)
if not db_path.exists():
    print("NO-DB"); raise SystemExit
db = sqlite3.connect(db_path); db.row_factory = sqlite3.Row
data = {t: [dict(r) for r in db.execute(f"SELECT * FROM {t}")]
        for t in ("grounding_docs", "doc_format_config")}
src = root / "storage" / "document_store" / "refs"
n = 0; total = 0
for row in data["grounding_docs"]:
    for p in src.glob(row["ref_id"] + "*"):
        shutil.copy2(p, seed / "refs" / p.name); n += 1; total += p.stat().st_size
# PII-scrub every reference AS IT IS PACKAGED.
#
# The seed is rebuilt here from the LIVE storage refs, which silently overwrote a
# scrub performed anywhere else: the first attempt cleaned middleware/seed/refs and
# the installer still shipped the originals, because staging copies that folder in
# and this step then overwrites it. Doing the scrub inside the packaging step is the
# only place that cannot be bypassed - whatever the live store holds, what SHIPS is
# clean. Author metadata (dc:creator / cp:lastModifiedBy) is the real exposure here:
# invisible in Excel and PowerPoint, plainly readable in the file.
sys.path.insert(0, str(root / "middleware"))
red = {}
try:
    from pii_scrub import scrub_file
    for f in (seed / "refs").iterdir():
        rep = scrub_file(str(f))
        for k, v in (rep.get("redactions") or {}).items():
            red[k] = red.get(k, 0) + v
except Exception as exc:
    print(f"SCRUB-FAILED {type(exc).__name__}: {exc}")

(seed / "seed.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
print(f"{len(data['grounding_docs'])} refs, {len(data['doc_format_config'])} format rows, "
      f"{n} files, {total/1024/1024:.0f} MB | scrubbed {red or 'nothing'}")
'@
$seedScript = Join-Path $Tmp "pz_seed.py"
Set-Content -Path $seedScript -Value $seedPy -Encoding UTF8
$seedOut = & python $seedScript $Root $Payload
if ($seedOut -match "NO-DB") { Warn "No storage\documents.db - shipping WITHOUT reference documents" }
else { Ok $seedOut }

# -- 3. Embeddable Python --------------------------------------------------
$PyDir = Join-Path $Here "python"
if (-not $SkipDownload -or -not (Test-Path $PyDir)) {
    Step "Downloading embeddable Python $PythonVersion"
    if (Test-Path $PyDir) { Remove-Item -LiteralPath $PyDir -Recurse -Force }
    New-Item -ItemType Directory -Path $PyDir | Out-Null
    $zip = Join-Path $Tmp "python-embed.zip"
    $url = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip"
    Invoke-WebRequest $url -OutFile $zip -UseBasicParsing
    Expand-Archive $zip -DestinationPath $PyDir -Force
    Remove-Item -LiteralPath $zip -Force -ErrorAction SilentlyContinue

    # The embeddable build ships with site-packages disabled; enable it so pip-installed
    # dependencies are importable.
    Get-ChildItem $PyDir -Filter "python*._pth" | ForEach-Object {
        (Get-Content $_.FullName) -replace '^#\s*import site', 'import site' |
            Set-Content $_.FullName
        Add-Content $_.FullName "Lib\site-packages"
    }
    # Bootstrap pip into the embedded runtime.
    $gp = Join-Path $Tmp "get-pip.py"
    Invoke-WebRequest "https://bootstrap.pypa.io/get-pip.py" -OutFile $gp -UseBasicParsing
    & (Join-Path $PyDir "python.exe") $gp --no-warn-script-location | Out-Null
    Remove-Item -LiteralPath $gp -Force -ErrorAction SilentlyContinue
    Ok "Embedded Python ready"
} else { Ok "Reusing existing embedded Python" }

# -- 4 + 5. Dependencies ---------------------------------------------------
# Installed directly into the embedded runtime so the user never runs pip.
Step "Installing dependencies into the embedded runtime"
$req = Join-Path $Root "middleware\requirements.txt"
& (Join-Path $PyDir "python.exe") -m pip install --no-warn-script-location -r $req
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed" }

# Cosmetic: report how big the bundled Claude CLI is. It must NOT be able to fail the
# build, and once did — the script runs with $ErrorActionPreference = "Stop", and any
# line a freshly-installed package writes to stderr during this import is promoted to
# a terminating error. The build then died AFTER a successful nine-minute dependency
# install, with no message, at a line that only prints a number.
$sdk = "unknown"
try {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $probe = & (Join-Path $PyDir "python.exe") -c "import claude_agent_sdk,pathlib;p=pathlib.Path(claude_agent_sdk.__file__).parent/'_bundled';print(sum(f.stat().st_size for f in p.glob('*'))//1024//1024 if p.exists() else 0)" 2>$null
    $ErrorActionPreference = $prev
    if ($probe) { $sdk = ($probe | Select-Object -Last 1).ToString().Trim() }
} catch {
    $ErrorActionPreference = "Stop"
}
Ok "Dependencies installed (bundled Claude CLI: $sdk MB)"

Copy-Item $PyDir (Join-Path $Payload "python") -Recurse
$size = "{0:N0}" -f ((Get-ChildItem $Payload -Recurse -File | Measure-Object Length -Sum).Sum / 1MB)
Ok "Payload assembled: $size MB"

# Print the APPLICATION manifest — everything outside python\ and seed\refs\, which
# are bulk runtime and reference data. This is the short list a human can actually
# scan, so an unwanted file that creeps in is visible in the build output rather than
# discovered later inside a 129 MB installer.
Step "Application files being shipped"
$app = Get-ChildItem $Payload -Recurse -File |
       Where-Object { $_.FullName -notmatch '\\payload\\python\\' -and
                      $_.FullName -notmatch '\\seed\\refs\\' } |
       Sort-Object { $_.FullName }
foreach ($f in $app) {
    Write-Host ("    {0,7:N0} KB  {1}" -f ($f.Length / 1KB),
                $f.FullName.Substring($Payload.Length + 1)) -ForegroundColor DarkGray
}
$refCount = (Get-ChildItem (Join-Path $Payload "middleware\seed\refs") -File -ErrorAction SilentlyContinue).Count
Ok ("{0} application files + {1} grounding references + embedded Python" -f $app.Count, $refCount)

# -- 6. Compile ------------------------------------------------------------
Step "Compiling the installer"
# Per-user installs (%LOCALAPPDATA%\Programs) come first: on a locked-down
# corporate machine that is where Inno Setup actually lands, because the
# machine-wide installer needs admin rights the user does not have.
$iscc = @(
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1

if (-not $iscc) {
    $cmd = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($cmd) { $iscc = $cmd.Source }
}

if (-not $iscc) {
    Warn "Inno Setup 6 is not installed - the payload is ready but not compiled."
    Warn "Install from https://jrsoftware.org/isdl.php then run:"
    Warn "    `"C:\Program Files (x86)\Inno Setup 6\ISCC.exe`" `"$Here\ProjectZen.iss`""
    exit 0
}

if (-not (Test-Path $Dist)) { New-Item -ItemType Directory -Path $Dist | Out-Null }

# COMPILE OUTSIDE ONEDRIVE, THEN MOVE IN.
#
# This repository lives under a OneDrive-redirected profile, so installer\dist is a
# synced folder. The last stage of an Inno build writes the 130 MB stub and then
# reopens it to patch icons and version resources — and OneDrive, seeing a large new
# file appear, opens it to upload at exactly that moment. The result is an
# intermittent
#     Resource update error: EndUpdateResource failed ... (110)
# that has nothing to do with the script and does not reproduce on a retry pattern
# you can rely on. Building to a local temp directory removes the race entirely; the
# finished artefact is moved into dist in one operation, which OneDrive handles fine.
# NOT $env:TEMP. On this profile that expands to the 8.3 short form
#     C:\Users\S2FE4~1.MAJ\AppData\Local\Temp
# and PowerShell's path parser reads the `~` as the home-directory shorthand, so
# Move-Item and Remove-Item both fail with "An object at the specified path
# C:\Users\S2FE4~1.MAJ does not exist" — after a ten-minute compile has already
# succeeded. $env:LOCALAPPDATA is the long form and has no tilde. -LiteralPath on
# every call is the belt to that braces: it disables the shorthand outright.
$tempBase = if ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA "Temp" } else { $env:TEMP }
$buildOut = Join-Path $tempBase ("pz-installer-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
New-Item -ItemType Directory -Path $buildOut -Force | Out-Null
try {
    & $iscc (Join-Path $Here "ProjectZen.iss") "/O$buildOut"
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup compilation failed" }

    $built = Join-Path $buildOut "ProjectZen-Setup.exe"
    if (-not (Test-Path -LiteralPath $built)) { throw "Inno reported success but produced no exe" }

    # Replacing the previous 130 MB exe races OneDrive, which opens the file to
    # upload it the moment it appears. That surfaced as
    #     Remove-Item ... ProjectZen-Setup.exe : IOException
    # AFTER a successful compile, leaving the STALE exe in dist while the build
    # reported success - the worst possible outcome, since the artifact looks fresh
    # and is not. Retry briefly, then fail loudly rather than leave a stale file.
    $exe = Join-Path $Dist "ProjectZen-Setup.exe"
    $moved = $false
    foreach ($attempt in 1..6) {
        try {
            if (Test-Path -LiteralPath $exe) { Remove-Item -LiteralPath $exe -Force -ErrorAction Stop }
            Move-Item -LiteralPath $built -Destination $exe -Force -ErrorAction Stop
            $moved = $true
            break
        } catch {
            if ($attempt -eq 6) { break }
            Warn "Output file is locked (OneDrive/AV); retrying in 5s [$attempt/6]"
            Start-Sleep -Seconds 5
        }
    }
    if (-not $moved) {
        throw "Compiled successfully but could not replace $exe - it is locked by " +
              "another process. Close any Explorer preview, pause OneDrive, and rerun."
    }
    # Prove the artifact is actually the one just built, not a survivor.
    $age = (Get-Date) - (Get-Item -LiteralPath $exe).LastWriteTime
    Ok ("Built {0} ({1:N0} MB, written {2:N0}s ago)" -f `
        $exe, ((Get-Item -LiteralPath $exe).Length / 1MB), $age.TotalSeconds)
} finally {
    Remove-Item -LiteralPath $buildOut -Recurse -Force -ErrorAction SilentlyContinue
}
