<#
.SYNOPSIS
    Update an existing claude-apiary install on Windows (repo + GUI).

.DESCRIPTION
    Pulls the latest code, then re-runs the (idempotent) install chain to bring
    everything into sync: `poetry install`, `self-bootstrap` (refreshes hooks
    and slash commands), repo git hooks, and `doctor`. Pass -Gui to also update
    the desktop GUI's dependency group.

    This is the counterpart to scripts/install.ps1 and forwards the same flags.
    Run it as often as you like -- every step is safe to repeat.

.PARAMETER Gui
    Also update the desktop GUI (the `gui` Poetry group). Use this if you
    installed with -Gui.

.PARAMETER SkipBootstrap
    Stop after `poetry install`. Skips self-bootstrap, repo hooks, and doctor.

.PARAMETER Yes
    Assume "yes" to every prompt (non-interactive).

.PARAMETER DryRun
    Print every step without changing anything (including the git pull).

.EXAMPLE
    .\scripts\update.ps1            # update CLI
    .\scripts\update.ps1 -Gui       # update CLI + desktop GUI
#>
[CmdletBinding()]
param(
    [switch]$Gui,
    [switch]$SkipBootstrap,
    [switch]$Yes,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot

Write-Host ""
Write-Host "claude-apiary updater (Windows)" -ForegroundColor Magenta
Write-Host "repo: $RepoRoot"
if ($DryRun) { Write-Host "    !   DRY RUN -- no changes will be made" -ForegroundColor Yellow }
Write-Host ""

# --- Pull latest ---------------------------------------------------------- #
Write-Host "==> Pulling latest (git pull --ff-only)" -ForegroundColor Cyan
if ($DryRun) {
    Write-Host "    OK  [dry-run] git -C `"$RepoRoot`" pull --ff-only" -ForegroundColor Green
} else {
    git -C $RepoRoot pull --ff-only
    if ($LASTEXITCODE -ne 0) {
        Write-Host "    X   git pull was not a fast-forward. Resolve manually (e.g. git status, git stash), then re-run." -ForegroundColor Red
        exit 1
    }
}

# --- Re-run the idempotent install chain ---------------------------------- #
# install.ps1 handles dependency install, self-bootstrap, repo hooks, and
# doctor -- all safe to repeat -- so updates reuse exactly one implementation.
$installer = Join-Path $PSScriptRoot 'install.ps1'
& $installer -Gui:$Gui -SkipBootstrap:$SkipBootstrap -Yes:$Yes -DryRun:$DryRun
