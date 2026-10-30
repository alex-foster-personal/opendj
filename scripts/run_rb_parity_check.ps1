<#
.SYNOPSIS
Windows-native Rekordbox parity stabilization gate.

.DESCRIPTION
Requirements:
  [DONE] Sync the Python 3.11 .venv exactly from the committed uv.lock, never a fresh resolve.
  [DONE] Reject Node runtimes below pnpm 11.9's minimum supported version.
  [DONE] Install the frozen pnpm workspace before frontend verification.
  [DONE] Run the focused Python, frontend unit, Svelte, and optional build gates.

Acceptance:
  [if] uv.lock is missing or stale against pyproject.toml [then STOP] the script fails before any test runs.
  [if] any child command exits nonzero [then STOP] this script exits nonzero immediately.
  [if] Node is older than 22.14.0 [then STOP] this script fails before dependency installation.
  [if] pytest runs on Windows [then PASS] temp files stay inside the workspace and the tracked coverage matrix is not rewritten.
  [if] the Windows parity gate selects Python tests [then PASS] the real interprocess progress-lock suite runs while unrelated progress-route, Spotify, and USB suites remain in general CI only.
  [if] -Final is supplied [then PASS] the production frontend build completes after all faster gates.
#>
[CmdletBinding()]
param(
    [switch]$Final
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$FrontendRoot = Join-Path $RepoRoot "apps\webui\frontend"
$PythonExe = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$TmpRoot = [IO.Path]::GetFullPath((Join-Path $RepoRoot ".tmp"))
$PytestBaseTemp = [IO.Path]::GetFullPath((Join-Path $TmpRoot ".tmp_pytest_rb_parity"))

function Invoke-Checked {
    param(
        [Parameter(Mandatory)] [string]$Label,
        [Parameter(Mandatory)] [string]$Executable,
        [Parameter(Mandatory)] [string[]]$CommandArgs,
        [Parameter(Mandatory)] [string]$WorkingDirectory
    )

    Write-Output "[RUN] $Label"
    Push-Location $WorkingDirectory
    try {
        & $Executable @CommandArgs
        if ($LASTEXITCODE -ne 0) {
            throw "$Label failed with exit code $LASTEXITCODE"
        }
    }
    finally {
        Pop-Location
    }
    Write-Output "[OK] $Label"
}

foreach ($Tool in @("uv", "node", "pnpm")) {
    if (-not (Get-Command $Tool -ErrorAction SilentlyContinue)) {
        throw "Required tool is unavailable on PATH: $Tool"
    }
}

$MinimumNodeVersion = [version]"22.14.0"
$NodeVersionText = (& node --version).Trim().TrimStart("v")
$NodeVersion = [version]$NodeVersionText
if ($NodeVersion -lt $MinimumNodeVersion) {
    throw "Node $MinimumNodeVersion or newer is required by pnpm 11.9; found $NodeVersion."
}

# Exact install from the committed uv.lock, never a fresh resolve. The gate used
# `uv pip install -e .[dev]`, which ignores the lock: since Thu 15 Oct 2026 (run
# 38719125594) it tested pyrekordbox 0.5.0, which has no pyrekordbox.db6, while
# every lock pins 0.4.4. analysis and observability match the Linux lane's
# pylock.ci.toml. --locked fails on a stale lock, and sync trims a reused .venv.
$LockFile = Join-Path $RepoRoot "uv.lock"
if (-not (Test-Path -LiteralPath $LockFile -PathType Leaf)) {
    throw "Committed lock is missing: $LockFile (regenerate with: uv lock)"
}
Invoke-Checked -Label "sync Python dependencies exactly from uv.lock" -Executable "uv" `
    -CommandArgs @(
        "sync", "--locked", "--python", "3.11",
        "--extra", "dev", "--extra", "analysis", "--extra", "observability"
    ) -WorkingDirectory $RepoRoot
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw "uv sync did not produce the project interpreter: $PythonExe"
}

New-Item -ItemType Directory -Force -Path $TmpRoot | Out-Null
$ExpectedPrefix = $TmpRoot.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
if (-not $PytestBaseTemp.StartsWith($ExpectedPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to clear pytest temp outside workspace .tmp: $PytestBaseTemp"
}
if (Test-Path -LiteralPath $PytestBaseTemp) {
    Remove-Item -LiteralPath $PytestBaseTemp -Recurse -Force
}

# Half the logical CPUs, not auto: at 14 workers on bifrost1 the warm-index wall-clock
# ratchet in tests/webui/test_track_index.py took 3.4 s against its 1.5 s bound.
$XdistWorkers = [Math]::Max(2, [int][Math]::Floor([Environment]::ProcessorCount / 2))
Write-Output "[INFO] pytest-xdist workers: $XdistWorkers of $([Environment]::ProcessorCount) logical CPUs"
$PytestArgs = @(
    "-m", "pytest", "-q", "-m", "rb_parity", "--no-coverage-matrix",
    "-n", "$XdistWorkers", "--dist", "loadgroup",
    "--basetemp", $PytestBaseTemp
)

Invoke-Checked -Label "focused Python parity tests" -Executable $PythonExe `
    -CommandArgs $PytestArgs -WorkingDirectory $RepoRoot
Invoke-Checked -Label "frozen frontend install" -Executable "pnpm" `
    -CommandArgs @("install", "--frozen-lockfile") -WorkingDirectory $FrontendRoot
Invoke-Checked -Label "frontend unit tests" -Executable "pnpm" `
    -CommandArgs @("test:unit") -WorkingDirectory $FrontendRoot
Invoke-Checked -Label "Svelte type and accessibility check" -Executable "pnpm" `
    -CommandArgs @("check") -WorkingDirectory $FrontendRoot

if ($Final) {
    Invoke-Checked -Label "production frontend build" -Executable "pnpm" `
        -CommandArgs @("build") -WorkingDirectory $FrontendRoot
}

Write-Output "[OK] Rekordbox parity stabilization gate passed"
