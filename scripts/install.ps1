<#
.SYNOPSIS
    Fresh-machine installer for claude-apiary on Windows.

.DESCRIPTION
    One user-run script that takes a freshly cloned claude-apiary repo all the
    way to a working install, handling the Windows-specific snags that derail an
    agent doing it step by step:

      * The WindowsApps stub      -> the `python` / `python3` app-execution alias
                                     under WindowsApps exits 9009 and hijacks
                                     Poetry's interpreter discovery. This script
                                     finds a REAL interpreter and runs every child
                                     process with WindowsApps stripped from PATH.
      * Python installed but lost  -> discovers it via the `py` launcher and the
                                     registry, not directory guessing, so an
                                     install in a non-standard location is found.
      * Poetry missing            -> installs it via `pip install --user poetry`.
      * git-bash launcher shebang -> never invokes the `apiary` console script;
                                     drives the CLI as `python -m core.cli`, which
                                     is shell-agnostic.

    A Python 3.11+ interpreter must already be installed; if none is found the
    script stops with instructions rather than installing one itself.

    Because YOU run this script, the whole chain (poetry install ->
    self-bootstrap -> repo hooks -> doctor) is a single user action, so the
    Claude Code safety classifier never blocks the bootstrap mid-way.

.PARAMETER Gui
    Also install the desktop GUI: adds the `gui` Poetry group and prefers a
    GUI-compatible interpreter (Python 3.11/3.12, since pythonnet has no 3.13+
    wheel). If only a newer Python exists it is used anyway, with a loud warning.

.PARAMETER SkipBootstrap
    Stop after `poetry install`. Skips self-bootstrap, repo hooks, and doctor.

.PARAMETER Yes
    Assume "yes" to every prompt (non-interactive).

.PARAMETER DryRun
    Print every step that would run without changing the system. Safe to inspect.

.EXAMPLE
    # CLI only, from inside the cloned repo:
    .\scripts\install.ps1

.EXAMPLE
    # CLI + desktop GUI:
    .\scripts\install.ps1 -Gui
#>
[CmdletBinding()]
param(
    [switch]$Gui,
    [switch]$SkipBootstrap,
    [switch]$Yes,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

# scripts/ -> repo root
$RepoRoot = Split-Path -Parent $PSScriptRoot
$MinPython = [version]'3.11'

# --------------------------------------------------------------------------- #
# Output helpers
# --------------------------------------------------------------------------- #
function Write-Step($msg)  { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)    { Write-Host "    OK  $msg" -ForegroundColor Green }
function Write-Warn2($msg) { Write-Host "    !   $msg" -ForegroundColor Yellow }
function Write-Die($msg)   { Write-Host "    X   $msg" -ForegroundColor Red; exit 1 }

function Confirm-Or-Exit($question) {
    if ($Yes) { return $true }
    $ans = Read-Host "$question [Y/n]"
    if ([string]::IsNullOrWhiteSpace($ans)) { return $true }
    return $ans.Trim().ToLower() -eq 'y'
}

# --------------------------------------------------------------------------- #
# PATH hygiene -- the WindowsApps alias stubs are the root cause of the
# "returned non-zero exit status 9009" failures during Poetry discovery.
# Every child process this script spawns gets a PATH with them removed.
# --------------------------------------------------------------------------- #
function Get-CleanPath {
    ($env:PATH -split ';' | Where-Object { $_ -and ($_ -notlike '*WindowsApps*') }) -join ';'
}

# True when an exe path is the Windows Store app-execution alias (a 0-byte
# reparse point that opens the Store instead of running Python).
function Test-IsStub($exePath) {
    if (-not $exePath) { return $true }
    return $exePath -like '*WindowsApps*'
}

# Run a candidate interpreter and return its [version], or $null if it isn't
# runnable. Does NOT apply the WindowsApps stub fast-path -- callers that have
# already decided to trust the path (an explicit APIARY_PYTHON override) use
# this directly so a real Store interpreter living under WindowsApps is
# honored, matching core/hooks_lib.resolve_python()'s no-questions trust of the
# override. NB: when Store Python is installed and its app-execution alias is
# enabled, even the 0-byte alias runs (it forwards to the real runtime), so a
# user who points the override straight at the alias is taken at their word --
# that is deliberate intent, the same as resolve_python() would honor. The
# substring veto is kept only for blind PATH discovery (Get-PythonVersion).
# Never throws.
function Get-PythonVersionByRun($exePath) {
    if (-not $exePath -or -not (Test-Path $exePath)) { return $null }
    try {
        $out = & $exePath -c "import sys; print('%d.%d.%d' % sys.version_info[:3])" 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $out) { return $null }
        return [version]($out.Trim())
    } catch {
        return $null
    }
}

# Probe a candidate interpreter: returns its [version] if it's a real,
# runnable Python >= MinPython, else $null. Applies the substring stub
# fast-path so plain-PATH discovery never spawns the Store-opening alias.
# Never throws.
function Get-PythonVersion($exePath) {
    if (Test-IsStub $exePath) { return $null }
    return Get-PythonVersionByRun $exePath
}

# --------------------------------------------------------------------------- #
# Step 1: find a real Python >= 3.11, skipping the WindowsApps stub.
#
# The failure this guards against is "Python IS installed but the tools can't
# find it" -- a naive glob of one or two directories misses Store installs,
# custom install dirs, and conda. The two authoritative sources on Windows are
# the `py` launcher and the registry; this leads with both, then falls back to
# globs and PATH.
# --------------------------------------------------------------------------- #

# Ask the `py` launcher to enumerate every registered interpreter and its path.
# `py -0p` output rows look like:  -V:3.12 *        C:\...\python.exe
function Get-PyLauncherPaths {
    $launcher = (Get-Command py -ErrorAction SilentlyContinue).Source
    if (-not $launcher) { return @() }
    $found = @()
    foreach ($flag in @('-0p', '--list-paths')) {
        try {
            $lines = & $launcher $flag 2>$null
        } catch { continue }
        if ($LASTEXITCODE -ne 0 -or -not $lines) { continue }
        foreach ($line in $lines) {
            $m = [regex]::Match($line, '([A-Za-z]:\\[^\r\n]*?python(?:w)?\.exe)')
            if ($m.Success) { $found += $m.Groups[1].Value }
        }
        if ($found) { break }
    }
    return $found
}

# Read install dirs straight from the registry -- the record every official
# installer writes. Covers per-user, all-users, 32-bit, and Store installs.
function Get-RegistryPythonPaths {
    $roots = @(
        'HKLM:\SOFTWARE\Python\PythonCore',
        'HKLM:\SOFTWARE\WOW6432Node\Python\PythonCore',
        'HKCU:\SOFTWARE\Python\PythonCore'
    )
    $found = @()
    foreach ($root in $roots) {
        if (-not (Test-Path $root)) { continue }
        foreach ($verKey in Get-ChildItem $root -ErrorAction SilentlyContinue) {
            $ipPath = Join-Path $verKey.PSPath 'InstallPath'
            if (-not (Test-Path $ipPath)) { continue }
            try {
                $key = Get-Item $ipPath
                $exe = $key.GetValue('ExecutablePath')   # set by modern installers
                if (-not $exe) {
                    $dir = $key.GetValue('')              # default value = install dir
                    if ($dir) { $exe = Join-Path $dir 'python.exe' }
                }
                if ($exe) { $found += $exe }
            } catch { }
        }
    }
    return $found
}

$GuiPyMaxExclusive = [version]'3.13'   # pythonnet has no wheel for 3.13+

function Find-RealPython([bool]$preferGuiCompat = $false) {
    # The APIARY_PYTHON override wins outright -- explicit user intent over
    # discovery, matching core/hooks_lib.resolve_python() so the pre-install
    # layer resolves the same interpreter as every other apiary layer. Accepts
    # an absolute path or a bare command name on PATH; bypasses the GUI-compat
    # preference because an explicit override is a deliberate choice.
    if ($env:APIARY_PYTHON) {
        $ov = $env:APIARY_PYTHON
        if (-not (Test-Path $ov)) {
            $resolved = (Get-Command $ov -ErrorAction SilentlyContinue).Source
            if ($resolved) { $ov = $resolved }
        }
        # Validate by running, not via the stub fast-path: an explicit override
        # is deliberate user intent, so a real Store interpreter under
        # WindowsApps must be honored rather than vetoed by Test-IsStub.
        $ovVer = Get-PythonVersionByRun $ov
        if ($ovVer -and $ovVer -ge $MinPython) {
            return [pscustomobject]@{ Exe = $ov; Version = $ovVer }
        }
        Write-Warn2 "APIARY_PYTHON=$($env:APIARY_PYTHON) is not a usable Python >= $MinPython; falling back to discovery."
    }

    $candidates = New-Object System.Collections.Generic.List[string]
    $add = { param($p) if ($p) { $candidates.Add($p) } }

    # 1. py launcher -- authoritative, never the bare stub
    foreach ($p in (Get-PyLauncherPaths)) { & $add $p }

    # 2. registry -- the install record every official installer writes
    foreach ($p in (Get-RegistryPythonPaths)) { & $add $p }

    # 3. python / python3 on PATH (may be the stub -- Get-PythonVersion rejects it)
    foreach ($name in @('python', 'python3')) {
        $src = (Get-Command $name -ErrorAction SilentlyContinue).Source
        if ($src) { & $add $src }
    }

    # 4. common install locations (conda + standard installers). Microsoft
    # Store installs are NOT globbed here: their only real interpreter lives in
    # the ACL-locked C:\Program Files\WindowsApps\... and is reached via the
    # registry's ExecutablePath (step 2). A glob under %LOCALAPPDATA%\...\
    # WindowsApps would only ever match alias reparse points, which Test-IsStub
    # correctly rejects -- so it was dead weight (T-2026-244).
    $globs = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python3*\python.exe'),
        'C:\Python3*\python.exe',
        (Join-Path $env:PROGRAMFILES 'Python3*\python.exe'),
        (Join-Path $env:USERPROFILE 'anaconda3\python.exe'),
        (Join-Path $env:USERPROFILE 'miniconda3\python.exe')
    )
    foreach ($g in $globs) {
        Get-ChildItem -Path $g -ErrorAction SilentlyContinue |
            ForEach-Object { & $add $_.FullName }
    }

    # Probe candidates in priority order, collecting every real one >= MinPython.
    $seen = @{}
    $valid = New-Object System.Collections.Generic.List[object]
    foreach ($c in $candidates) {
        $norm = $c.ToLower()
        if ($seen.ContainsKey($norm)) { continue }
        $seen[$norm] = $true
        $ver = Get-PythonVersion $c
        if ($ver -and $ver -ge $MinPython) {
            $valid.Add([pscustomobject]@{ Exe = $c; Version = $ver })
        }
    }
    if ($valid.Count -eq 0) { return $null }

    # For the GUI, prefer a 3.11/3.12 interpreter when one exists; otherwise fall
    # through to the highest-priority valid interpreter (caller warns loudly).
    if ($preferGuiCompat) {
        foreach ($v in $valid) {
            if ($v.Version -lt $GuiPyMaxExclusive) { return $v }
        }
    }
    return $valid[0]
}

# --------------------------------------------------------------------------- #
# Step 2: ensure Poetry, return the path to poetry.exe.
# --------------------------------------------------------------------------- #
function Ensure-Poetry($pythonExe) {
    $existing = (Get-Command poetry -ErrorAction SilentlyContinue).Source
    if ($existing -and -not (Test-IsStub $existing)) {
        Write-Ok "Poetry already installed: $existing"
        return $existing
    }

    Write-Step "Installing Poetry via pip (user scope)"
    if ($DryRun) {
        Write-Ok "[dry-run] $pythonExe -m pip install --user poetry"
        return 'poetry'  # placeholder for dry-run flow
    }
    & $pythonExe -m pip install --user poetry
    if ($LASTEXITCODE -ne 0) { Write-Die "Poetry install failed (exit $LASTEXITCODE)." }

    # poetry.exe lands in the user base Scripts dir
    $userBase = (& $pythonExe -m site --user-base).Trim()
    $poetryExe = Join-Path $userBase 'Scripts\poetry.exe'
    if (-not (Test-Path $poetryExe)) {
        Write-Die "Poetry installed but poetry.exe not found at $poetryExe."
    }
    Write-Ok "Poetry installed: $poetryExe"
    return $poetryExe
}

# --------------------------------------------------------------------------- #
# Run a command with a stub-free PATH and a Python-first PATH so Poetry's
# interpreter discovery can never wander back to the WindowsApps alias.
# --------------------------------------------------------------------------- #
function Invoke-Clean($exe, [string[]]$cmdArgs, $pythonDir) {
    $saved = $env:PATH
    try {
        $clean = Get-CleanPath
        $env:PATH = "$pythonDir;$pythonDir\Scripts;$clean"
        if ($DryRun) {
            Write-Ok "[dry-run] $exe $($cmdArgs -join ' ')"
            return
        }
        & $exe @cmdArgs
        if ($LASTEXITCODE -ne 0) {
            throw "command failed (exit ${LASTEXITCODE}): $exe $($cmdArgs -join ' ')"
        }
    } finally {
        $env:PATH = $saved
    }
}

# =========================================================================== #
# Main
# =========================================================================== #
Write-Host ""
Write-Host "claude-apiary installer (Windows)" -ForegroundColor Magenta
Write-Host "repo: $RepoRoot"
if ($DryRun) { Write-Warn2 "DRY RUN -- no changes will be made" }
Write-Host ""

# --- Python --------------------------------------------------------------- #
Write-Step "Locating a real Python >= $MinPython (ignoring the WindowsApps stub)"
$py = Find-RealPython $Gui.IsPresent
if (-not $py) {
    Write-Host ""
    Write-Die @"
No Python 3.11+ found on this machine.

Install one, then re-run this script:
  * Download: https://www.python.org/downloads/windows/
  * During install, tick 'Add python.exe to PATH'.
  * Already installed? It may be hidden behind the Microsoft Store
    'python' alias. Turn it off in Settings > Apps > Advanced app
    settings > App execution aliases, then open a NEW terminal.
"@
}
Write-Ok "Python $($py.Version): $($py.Exe)"
$pythonExe = $py.Exe
$pythonDir = Split-Path -Parent $pythonExe
if ($Gui -and $py.Version -ge $GuiPyMaxExclusive) {
    Write-Warn2 "Python $($py.Version) is newer than the GUI supports (needs < 3.13)."
    Write-Warn2 "Installing the GUI anyway, but its window may fail to open."
    Write-Warn2 "For a working GUI, install Python 3.12 and re-run with -Gui."
}

# --- Preflight ------------------------------------------------------------- #
Write-Step "Running pre-install environment check"
$preflight = Join-Path $RepoRoot 'scripts\preflight.py'
$pfArgs = @($preflight)
if ($Gui) { $pfArgs += '--gui' }
& $pythonExe @pfArgs
if ($LASTEXITCODE -ne 0 -and -not $DryRun) {
    Write-Die "Preflight found blockers (above). Resolve them and re-run."
}

# --- Poetry --------------------------------------------------------------- #
Write-Step "Ensuring Poetry is installed"
$poetry = Ensure-Poetry $pythonExe

# --- poetry install ------------------------------------------------------- #
Write-Step "Pointing Poetry at the real interpreter and installing dependencies"
Push-Location $RepoRoot
try {
    Invoke-Clean $poetry @('env', 'use', $pythonExe) $pythonDir
    $installArgs = if ($Gui) { @('install', '--with', 'gui') } else { @('install') }
    Invoke-Clean $poetry $installArgs $pythonDir
    Write-Ok "poetry install complete$(if ($Gui) { ' (with gui)' })"

    if ($SkipBootstrap) {
        Write-Warn2 "-SkipBootstrap set; stopping after dependency install."
    } else {
        # Drive the CLI as `python -m core.cli` -- shell-agnostic, no console-script
        # shebang for git-bash to choke on.
        Write-Step "Bootstrapping main-apiary against itself"
        Invoke-Clean $poetry @('run', 'python', '-m', 'core.cli', 'self-bootstrap') $pythonDir
        Write-Ok "self-bootstrap complete"

        Write-Step "Installing repo-local git hooks"
        Invoke-Clean $poetry @('run', 'python', 'scripts/install_repo_hooks.py') $pythonDir
        Write-Ok "git hooks installed"

        Write-Step "Validating the install"
        Invoke-Clean $poetry @('run', 'python', '-m', 'core.cli', 'doctor') $pythonDir
    }
} finally {
    Pop-Location
}

Write-Host ""
Write-Host "Done." -ForegroundColor Green
if (-not $SkipBootstrap) {
    Write-Host "Next: restart Claude Code in a session rooted at this repo so its hooks" -ForegroundColor Green
    Write-Host "and slash commands load. Then run /apiary-context if anything is missing." -ForegroundColor Green
}
if ($Gui) {
    Write-Host "Launch the desktop GUI with:  poetry run python -m gui.app" -ForegroundColor Green
}
