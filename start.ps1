# MoonShell Spirit v14 - one-shot launcher
#
# Usage:
#   .\start.ps1            First run builds the venv and installs deps; later runs are instant.
#   .\start.ps1 -Setup     Force-reinstall dependencies (after editing requirements.txt or a broken env).
#   .\start.ps1 -Check     Run sprite/layout self-checks and rebuild the preview, then exit (no pet).
#
# Double-click start.cmd to launch.
#
# NOTE: kept ASCII-only on purpose. Windows PowerShell 5.1 reads BOM-less .ps1
# files in the system code page, so non-ASCII text here would corrupt and fail
# to parse. User-facing Chinese docs live in README.md instead.

param(
    [switch]$Setup,
    [switch]$Check
)

$ErrorActionPreference = "Stop"

# Always treat the script's own folder as the project root, whatever the caller's cwd is.
Set-Location -LiteralPath $PSScriptRoot

$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$marker = Join-Path $PSScriptRoot ".venv\.deps-installed"
$reqs   = Join-Path $PSScriptRoot "requirements.txt"

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

# ---- 1. Ensure the virtual environment exists ----
if (-not (Test-Path $venvPy)) {
    Write-Step "No .venv found. Creating virtual environment..."
    $created = $false
    foreach ($cmd in @(
        { py -3.11 -m venv .venv },
        { py -3 -m venv .venv },
        { python -m venv .venv }
    )) {
        try {
            & $cmd
            if (Test-Path $venvPy) { $created = $true; break }
        } catch { }
    }
    if (-not $created) {
        throw "Cannot create .venv. Install Python 3.11+ and add it to PATH, then run start.ps1 again."
    }
}

# ---- 2. Ensure dependencies are installed (skipped when already up to date) ----
$needInstall = [bool]$Setup
$reqHash = (Get-FileHash -LiteralPath $reqs -Algorithm SHA256).Hash
if (-not $needInstall) {
    if (-not (Test-Path $marker)) {
        $needInstall = $true
    }
    elseif ((Get-Content -LiteralPath $marker -Raw -ErrorAction SilentlyContinue).Trim() -ne $reqHash) {
        Write-Step "requirements.txt changed. Reinstalling dependencies..."
        $needInstall = $true
    }
}

if ($needInstall) {
    Write-Step "Installing/updating dependencies..."
    & $venvPy -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed." }
    & $venvPy -m pip install -r $reqs
    if ($LASTEXITCODE -ne 0) { throw "dependency install failed." }
    Set-Content -LiteralPath $marker -Value $reqHash -Encoding ascii
}

# ---- 3. Self-check mode: run the tool scripts ----
if ($Check -or $Setup) {
    Write-Step "Checking sprite assets..."
    & $venvPy ".\tools\check_sprites.py"
    if ($LASTEXITCODE -ne 0) { throw "sprite check failed." }

    Write-Step "Checking padded stage layout..."
    & $venvPy ".\tools\check_layout.py"
    if ($LASTEXITCODE -ne 0) { throw "layout check failed." }

    Write-Step "Rebuilding preview docs\v14_stage_preview.png ..."
    & $venvPy ".\tools\render_v14_preview.py"
    if ($LASTEXITCODE -ne 0) { throw "preview render failed." }

    if ($Check) {
        Write-Step "Self-check done."
        return
    }
}

# ---- 4. Launch the pet ----
$env:QT_ENABLE_HIGHDPI_SCALING = "1"
$env:QT_SCALE_FACTOR_ROUNDING_POLICY = "PassThrough"

# Launch detached with pythonw.exe so no console window lingers. Runtime errors
# go to .venv\last-run.log. Quit the pet from the tray / right-click menu.
$venvPyw = Join-Path $PSScriptRoot ".venv\Scripts\pythonw.exe"
$log = Join-Path $PSScriptRoot ".venv\last-run.log"

Write-Step "Starting MoonShell Spirit v14 (tray icon present; quit via right-click menu)."
if (Test-Path $venvPyw) {
    Start-Process -FilePath $venvPyw -ArgumentList "main.py" `
        -WorkingDirectory $PSScriptRoot -RedirectStandardError $log -WindowStyle Hidden
}
else {
    & $venvPy ".\main.py"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Pet exited with code $LASTEXITCODE" -ForegroundColor Yellow
        exit $LASTEXITCODE
    }
}
