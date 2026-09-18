# Managed Engram setup for Windows PowerShell 5.1+.
# Default action is a read-only inventory. It never resolves or installs
# "latest" and never synchronizes memory through Git.
[CmdletBinding()]
param(
    [switch]$Inventory,
    [switch]$Configure,
    [switch]$RenderClient,
    [switch]$InstallClient,
    [switch]$InstallSupported,
    [switch]$Upgrade,
    [switch]$Rollback,
    [string]$Project,
    [ValidateSet('codex', 'vscode-generic', 'antigravity', 'kilo', 'opencode-v1', 'cursor', 'project-config')]
    [string]$Client,
    [string]$Workspace,
    [string]$Wrapper,
    [string]$Python,
    [switch]$MaintenanceWindow,
    [switch]$Yes,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$RepoDir = Split-Path -Parent $PSScriptRoot
$Tool = Join-Path $RepoDir 'tools\engram_memory.py'
$Registry = Join-Path $RepoDir 'config\projects.json'
$UserConfigRoot = if ($env:APPDATA) { $env:APPDATA } else { Join-Path $HOME 'AppData\Roaming' }
$UserRegistry = Join-Path $UserConfigRoot 'naos-engram-memory\projects.json'
if ($env:ENGRAM_MEMORY_CONFIG_DIR -or $env:ENGRAM_MEMORY_BIN_DIR -or $env:ENGRAM_MEMORY_BACKUP_ROOT -or $env:ENGRAM_MEMORY_DATABASE_PATH) { throw '[error] Legacy ENGRAM_MEMORY_* toolkit variable detected; use NAOS_ENGRAM_MEMORY_* after explicit migration.' }

function Write-Info([string]$Message) { Write-Host "[ok] $Message" -ForegroundColor Green }
function Write-Warn([string]$Message) { Write-Host "[warn] $Message" -ForegroundColor Yellow }
function Stop-Setup([string]$Message) { throw "[error] $Message" }
function Resolve-Python {
    $candidates = @()
    if ($Python) { $candidates += [pscustomobject]@{ Name = $Python; LauncherArgs = @() } }
    if ($env:NAOS_ENGRAM_MEMORY_PYTHON) { $candidates += [pscustomobject]@{ Name = $env:NAOS_ENGRAM_MEMORY_PYTHON; LauncherArgs = @() } }
    $candidates += [pscustomobject]@{ Name = 'python3'; LauncherArgs = @() }
    $candidates += [pscustomobject]@{ Name = 'python'; LauncherArgs = @() }
    $candidates += [pscustomobject]@{ Name = 'py'; LauncherArgs = @('-3') }
    foreach ($entry in $candidates) {
        $command = Get-Command $entry.Name -ErrorAction SilentlyContinue
        if (-not $command) { continue }
        & $command.Source @($entry.LauncherArgs) -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' *> $null
        if ($LASTEXITCODE -eq 0) {
            return [pscustomobject]@{ Command = $command.Source; LauncherArgs = @($entry.LauncherArgs) }
        }
    }
    Stop-Setup 'Python 3.10 or newer was not found. Exact remediation: install Python 3.10+, then rerun with -Python C:\absolute\python.exe.'
}
$ResolvedPython = Resolve-Python
function Invoke-PythonTool([string[]]$ToolArgs) {
    & $ResolvedPython.Command @($ResolvedPython.LauncherArgs) $Tool @ToolArgs
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

function Install-ManagedConfig {
    # Delegate all managed runtime writes to the hardened Python implementation.
    $runtimeArgs = @('runtime', 'install', '--home', $HOME, '--yes', '--non-interactive')
    if ($DryRun) { $runtimeArgs += '--dry-run' }
    Invoke-PythonTool $runtimeArgs
}

$chosen = @()
foreach ($selection in @($Inventory, $Configure, $RenderClient, $InstallClient, $InstallSupported, $Upgrade, $Rollback)) {
    if ($selection) { $chosen += $true }
}
if ($chosen.Count -gt 1) { Stop-Setup 'Choose exactly one action.' }
if ($Configure) { Install-ManagedConfig; exit 0 }
if ($RenderClient) {
    if (-not $Client) { Stop-Setup '-RenderClient requires -Client.' }
    $activeRegistry = if (Test-Path -LiteralPath $UserRegistry -PathType Leaf) { $UserRegistry } else { $Registry }
    $renderArgs = @('--registry', $activeRegistry, 'render-client', '--client', $Client)
    if ($Project) { $renderArgs += @('--project', $Project) }
    if ($Workspace) { $renderArgs += @('--workspace', $Workspace) }
    if ($Wrapper) { $renderArgs += @('--wrapper', $Wrapper) }
    Invoke-PythonTool $renderArgs
    exit $LASTEXITCODE
}
if ($InstallClient) {
    if (-not $Client) { Stop-Setup '-InstallClient requires -Client.' }
    if (-not (Test-Path -LiteralPath $UserRegistry -PathType Leaf)) { Stop-Setup 'Managed user registry is missing; run -Configure, then register the project explicitly.' }
    $installArgs = @('--registry', $UserRegistry, 'install-client', '--client', $Client)
    if (-not $Project) { Stop-Setup "-InstallClient requires -Project for $Client." }
    if (-not $Workspace) { Stop-Setup "-InstallClient requires -Workspace for $Client." }
    $installArgs += @('--project', $Project, '--workspace', $Workspace)
    if ($Wrapper) { $installArgs += @('--wrapper', $Wrapper) }
    if ($DryRun) { $installArgs += '--dry-run' }
    if (-not $DryRun) {
        if (-not $Yes) { Stop-Setup '-InstallClient requires -Yes; the action name alone is not consent.' }
        $installArgs += @('--yes', '--non-interactive')
    }
    Invoke-PythonTool $installArgs
    exit $LASTEXITCODE
}
if ($InstallSupported -or $Upgrade -or $Rollback) {
    Stop-Setup 'Windows binary/database maintenance is experimental and disabled; no download, backup, replacement, or rollback is performed by setup.ps1.'
}
$activeRegistry = if (Test-Path -LiteralPath $UserRegistry -PathType Leaf) { $UserRegistry } else { $Registry }
Invoke-PythonTool @('--registry', $activeRegistry, 'inventory', '--cwd', (Get-Location).Path)
