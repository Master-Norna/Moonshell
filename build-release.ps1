[CmdletBinding()]
param(
    [switch]$SkipInstall,
    [switch]$SkipTests,
    [switch]$SkipSmoke,
    [switch]$RecreateEnvironment,
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = $PSScriptRoot
$BuildVenv = Join-Path $ProjectRoot ".build-venv"
$BuildPython = Join-Path $BuildVenv "Scripts\python.exe"
$RequiredPythonVersion = (
    Get-Content -LiteralPath (Join-Path $ProjectRoot ".python-version") -Raw
).Trim()

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Executable,
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $Executable $Arguments"
    }
}

Set-Location $ProjectRoot

if ($RecreateEnvironment -and (Test-Path -LiteralPath $BuildVenv)) {
    $ResolvedRoot = [IO.Path]::GetFullPath($ProjectRoot).TrimEnd("\")
    $ResolvedBuildVenv = [IO.Path]::GetFullPath($BuildVenv).TrimEnd("\")
    if (
        [IO.Path]::GetDirectoryName($ResolvedBuildVenv) -ne $ResolvedRoot -or
        [IO.Path]::GetFileName($ResolvedBuildVenv) -ne ".build-venv"
    ) {
        throw "Refusing to remove unexpected build environment: $ResolvedBuildVenv"
    }
    Remove-Item -LiteralPath $ResolvedBuildVenv -Recurse -Force
}

if (-not (Test-Path -LiteralPath $BuildPython)) {
    if ($SkipInstall) {
        throw "Build environment is missing. Run without -SkipInstall once."
    }

    $BasePython = ""
    $BaseArguments = @()
    if ($Python) {
        $Candidate = $Python
        if (-not [IO.Path]::IsPathRooted($Candidate)) {
            $Candidate = Join-Path $ProjectRoot $Candidate
        }
        $BasePython = (Resolve-Path -LiteralPath $Candidate).Path
    } else {
        $PyLauncher = Get-Command py -ErrorAction SilentlyContinue
        if ($null -ne $PyLauncher) {
            $VersionParts = $RequiredPythonVersion.Split(".")
            $Selector = "-$($VersionParts[0]).$($VersionParts[1])"
            & $PyLauncher.Source $Selector -c (
                "import platform; raise SystemExit(0 if " +
                "platform.python_version() == '$RequiredPythonVersion' else 2)"
            ) *> $null
            if ($LASTEXITCODE -eq 0) {
                $BasePython = $PyLauncher.Source
                $BaseArguments = @($Selector)
            }
        }
        if (-not $BasePython) {
            $PythonCommand = Get-Command python -ErrorAction SilentlyContinue
            if ($null -ne $PythonCommand) {
                $BasePython = $PythonCommand.Source
            }
        }
        if (-not $BasePython) {
            throw (
                "Python $RequiredPythonVersion x64 was not found. Install that " +
                "version or pass -Python C:\path\to\python.exe."
            )
        }
    }
    Invoke-Checked -Executable $BasePython -Arguments (
        $BaseArguments + @(
            "-c",
            (
                "import platform,sys; raise SystemExit(0 if " +
                "platform.python_version() == '$RequiredPythonVersion' " +
                "and sys.maxsize > 2**32 else 2)"
            )
        )
    )
    Invoke-Checked -Executable $BasePython -Arguments (
        $BaseArguments + @("-m", "venv", $BuildVenv)
    )
}

Invoke-Checked -Executable $BuildPython -Arguments @(
    "-c",
    (
        "import platform,sys; raise SystemExit(0 if " +
        "platform.python_version() == '$RequiredPythonVersion' " +
        "and sys.maxsize > 2**32 else 2)"
    )
)

if (-not $SkipInstall) {
    Invoke-Checked -Executable $BuildPython -Arguments @(
        "-m",
        "pip",
        "install",
        "--require-hashes",
        "--only-binary=:all:",
        "-r",
        "requirements-build.txt"
    )
}
Invoke-Checked -Executable $BuildPython -Arguments @("-m", "pip", "check")
Invoke-Checked -Executable $BuildPython -Arguments @("-c", "import importlib.metadata as m; raise SystemExit(3 if any(d.metadata['Name'].lower().replace('_','-') == 'pyside6-addons' for d in m.distributions()) else 0)")
Invoke-Checked -Executable $BuildPython -Arguments @(
    "tools\check_build_environment.py"
)

Invoke-Checked -Executable $BuildPython -Arguments @("tools\build_icon.py")
Invoke-Checked -Executable $BuildPython -Arguments @("tools\check_release.py")

$PreviousQtPlatform = $env:QT_QPA_PLATFORM
try {
    $env:QT_QPA_PLATFORM = "offscreen"
    if (-not $SkipTests) {
        Invoke-Checked -Executable $BuildPython -Arguments @("-m", "unittest", "discover", "-s", "tests", "-v")
        Invoke-Checked -Executable $BuildPython -Arguments @("tools\check_sprites.py")
        Invoke-Checked -Executable $BuildPython -Arguments @("tools\check_layout.py")
    }
} finally {
    if ($null -eq $PreviousQtPlatform) {
        Remove-Item Env:QT_QPA_PLATFORM -ErrorAction SilentlyContinue
    } else {
        $env:QT_QPA_PLATFORM = $PreviousQtPlatform
    }
}

$OriginalPath = $env:PATH
$BasePrefix = (& $BuildPython -c "import sys; print(sys.base_prefix)").Trim()
if ($LASTEXITCODE -ne 0) {
    throw "Could not resolve the build interpreter base prefix."
}
$SafePathParts = @(
    (Join-Path $BuildVenv "Scripts"),
    $BasePrefix,
    (Join-Path $env:WINDIR "System32"),
    $env:WINDIR
) | Select-Object -Unique
try {
    $env:PATH = [string]::Join(";", $SafePathParts)
    Invoke-Checked -Executable $BuildPython -Arguments @(
        "-m", "PyInstaller", "--clean", "--noconfirm", "MoonShell.spec"
    )
} finally {
    $env:PATH = $OriginalPath
}
Invoke-Checked -Executable $BuildPython -Arguments @(
    "tools\check_build_provenance.py"
)

$ReleaseExe = Join-Path $ProjectRoot "dist\MoonShell\MoonShell.exe"
if (-not (Test-Path -LiteralPath $ReleaseExe -PathType Leaf)) {
    throw "PyInstaller did not produce dist\MoonShell\MoonShell.exe."
}
Invoke-Checked -Executable $BuildPython -Arguments @("tools\write_build_info.py", "--output", (Join-Path $ProjectRoot "dist\MoonShell\BUILD_INFO.json"))
Invoke-Checked -Executable $BuildPython -Arguments @("tools\check_release.py", "--exe", $ReleaseExe)

$ExpectedVersion = (& $BuildPython -c "from pet.version import APP_VERSION; print(APP_VERSION)").Trim()
if ($LASTEXITCODE -ne 0) {
    throw "Could not read application version."
}
$ExpectedNumericVersion = $ExpectedVersion + ".0"
$VersionInfo = (Get-Item -LiteralPath $ReleaseExe).VersionInfo
if ($VersionInfo.ProductName -ne "MoonShell Spirit") {
    throw "ProductName metadata mismatch: $($VersionInfo.ProductName)"
}
if ($VersionInfo.OriginalFilename -ne "MoonShell.exe") {
    throw "OriginalFilename metadata mismatch: $($VersionInfo.OriginalFilename)"
}
if ($VersionInfo.ProductVersion -notin @($ExpectedVersion, $ExpectedNumericVersion)) {
    throw "ProductVersion metadata mismatch: $($VersionInfo.ProductVersion)"
}
if ($VersionInfo.FileVersion -notin @($ExpectedVersion, $ExpectedNumericVersion)) {
    throw "FileVersion metadata mismatch: $($VersionInfo.FileVersion)"
}

if (-not $SkipSmoke) {
    Invoke-Checked -Executable $BuildPython -Arguments @("tools\smoke_release.py", "--exe", $ReleaseExe)
}

Invoke-Checked -Executable $BuildPython -Arguments @(
    "tools\package_release.py",
    "--exe",
    $ReleaseExe,
    "--output-dir",
    (Join-Path $ProjectRoot "dist"),
    "--qa"
)

Write-Host ""
Write-Warning (
    "MoonShell unsigned QA package is ready in: " +
    (Join-Path $ProjectRoot "dist") +
    ". Public maintenance packages are created only by the clean-tag " +
    "GitHub workflow and remain explicitly unsigned."
)
