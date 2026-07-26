# MoonShell Spirit - one-shot launcher
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

function Invoke-QuietPython {
    param([string[]]$Arguments)

    # Windows PowerShell 5.1 turns redirected native stderr into an error
    # record. Temporarily relaxing ErrorActionPreference lets us inspect the
    # native exit code instead of aborting before the repair branch can run.
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & $venvPy @Arguments *> $null
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }
    return $exitCode
}

function Test-Dependencies {
    $importExit = Invoke-QuietPython -Arguments @(
        "-c",
        "from PySide6 import QtCore, QtGui, QtNetwork, QtWidgets; import psutil; import PIL"
    )
    if ($importExit -ne 0) { return $false }
    $checkExit = Invoke-QuietPython -Arguments @("-m", "pip", "check")
    return ($checkExit -eq 0)
}

# ---- 1. Ensure the virtual environment exists ----
if (-not (Test-Path $venvPy)) {
    Write-Step "No .venv found. Creating virtual environment..."
    $created = $false
    foreach ($cmd in @(
        { py -3.11 -m venv .venv },
        {
            py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
            if ($LASTEXITCODE -ne 0) { throw "py -3 is too old" }
            py -3 -m venv .venv
        },
        {
            python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
            if ($LASTEXITCODE -ne 0) { throw "python is too old" }
            python -m venv .venv
        }
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

# Refuse an old interpreter even if a stale .venv already exists.
& $venvPy -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
if ($LASTEXITCODE -ne 0) {
    throw "The existing .venv uses Python older than 3.11. Delete .venv and run start.ps1 again."
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
    elseif (-not (Test-Dependencies)) {
        Write-Step "The virtual environment is incomplete or inconsistent. Repairing dependencies..."
        $needInstall = $true
    }
}

if ($needInstall) {
    if ($Setup) {
        $legacyQtExit = Invoke-QuietPython -Arguments @("-m", "pip", "show", "PySide6")
        if ($legacyQtExit -eq 0) {
            throw "This .venv contains the old full PySide6 bundle. Delete .venv, then run start.ps1 -Setup again."
        }
    }

    Write-Step "Installing/updating dependencies..."
    & $venvPy -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed." }

    $installArgs = @("-m", "pip", "install")
    if ($Setup) {
        $installArgs += @("--upgrade", "--force-reinstall")
    }
    $installArgs += @("-r", $reqs)
    & $venvPy @installArgs
    if ($LASTEXITCODE -ne 0) { throw "dependency install failed." }
    & $venvPy -c "from PySide6 import QtCore, QtGui, QtNetwork, QtWidgets; import psutil; import PIL"
    if ($LASTEXITCODE -ne 0) { throw "dependency verification failed." }
    & $venvPy -m pip check
    if ($LASTEXITCODE -ne 0) {
        throw "dependency versions conflict. Delete .venv and run start.ps1 again."
    }
    Set-Content -LiteralPath $marker -Value $reqHash -Encoding ascii
}

# ---- 3. Self-check mode: run the tool scripts ----
if ($Check -or $Setup) {
    Write-Step "Running automated tests..."
    $oldQtPlatform = $env:QT_QPA_PLATFORM
    try {
        $env:QT_QPA_PLATFORM = "offscreen"
        & $venvPy -B -m unittest discover -s tests -v
        if ($LASTEXITCODE -ne 0) { throw "automated tests failed." }
    }
    finally {
        if ($null -eq $oldQtPlatform) {
            Remove-Item Env:QT_QPA_PLATFORM -ErrorAction SilentlyContinue
        }
        else {
            $env:QT_QPA_PLATFORM = $oldQtPlatform
        }
    }

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

Write-Step "Starting MoonShell Spirit (tray icon present; quit via right-click menu)."
if (Test-Path $venvPyw) {
    try {
        $process = Start-Process -FilePath $venvPyw -ArgumentList "main.py" `
            -WorkingDirectory $PSScriptRoot -RedirectStandardError $log -WindowStyle Hidden -PassThru
    }
    catch {
        # A running first instance may still own last-run.log. The new process
        # only needs to signal it, so launch without redirection in that case.
        $process = Start-Process -FilePath $venvPyw -ArgumentList "main.py" `
            -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru
    }
    if ($process.WaitForExit(800) -and $process.ExitCode -ne 0) {
        throw "MoonShell exited during startup. See .venv\last-run.log for details."
    }
}
else {
    & $venvPy ".\main.py"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Pet exited with code $LASTEXITCODE" -ForegroundColor Yellow
        exit $LASTEXITCODE
    }
}
