param(
  [string]$Python,
  [switch]$List,
  [switch]$Yes,
  [switch]$NonInteractive,
  [switch]$InstallPython,
  [switch]$InstallPipx,
  [switch]$PipFallback,
  [string]$Package
)
$ErrorActionPreference = 'Stop'
if ($env:ENGRAM_MEMORY_PYTHON) { throw 'Legacy ENGRAM_MEMORY_PYTHON detected; use NAOS_ENGRAM_MEMORY_PYTHON after explicit migration.' }
$candidates = @()
if ($Python -and (Test-Path $Python)) { $candidates += [pscustomobject]@{ Command = (Get-Command $Python); LauncherArgs = @() } }
if ($env:NAOS_ENGRAM_MEMORY_PYTHON -and (Test-Path $env:NAOS_ENGRAM_MEMORY_PYTHON)) { $candidates += [pscustomobject]@{ Command = (Get-Command $env:NAOS_ENGRAM_MEMORY_PYTHON); LauncherArgs = @() } }
$python3 = Get-Command python3 -ErrorAction SilentlyContinue
if ($python3) { $candidates += [pscustomobject]@{ Command = $python3; LauncherArgs = @() } }
$pythonCommand = Get-Command python -ErrorAction SilentlyContinue
if ($pythonCommand) { $candidates += [pscustomobject]@{ Command = $pythonCommand; LauncherArgs = @() } }
$py = Get-Command py -ErrorAction SilentlyContinue
if ($py) { $candidates += [pscustomobject]@{ Command = $py; LauncherArgs = @('-3') } }
$candidate = $null
$launcherArgs = @()
foreach ($entry in $candidates) {
  & $entry.Command.Source @($entry.LauncherArgs) -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' *> $null
  if ($LASTEXITCODE -eq 0) { $candidate = $entry.Command; $launcherArgs = @($entry.LauncherArgs); break }
}
if (-not $candidate) {
  $manager = if (Get-Command winget -ErrorAction SilentlyContinue) { 'winget' } elseif (Get-Command choco -ErrorAction SilentlyContinue) { 'choco' } else { $null }
  if (-not $manager) { throw 'Python 3.10+ and a supported package manager were not found. Install Python 3.10+ manually and rerun with -Python C:\absolute\python.exe -List.' }
  $display = if ($manager -eq 'winget') { 'winget install Python.Python.3.12' } else { 'choco install python -y' }
  if ($NonInteractive) { throw "Python 3.10 or newer was not found. Exact remediation: $display. Non-interactive bootstrap never runs a package manager; execute it through your approved administration process." }
  if (-not ($InstallPython -and $Yes)) { throw "Python 3.10 or newer was not found. Offered command: $display. Review it, then rerun with -InstallPython -Yes." }
  if ($manager -eq 'winget') { & winget install Python.Python.3.12 } else { & choco install python -y }
  exit $LASTEXITCODE
}
$arguments = @((Join-Path $PSScriptRoot 'bootstrap.py'))
if ($Python) { $arguments += @('--python', $Python) }
if ($List) { $arguments += '--list' }
if ($Yes) { $arguments += '--yes' }
if ($NonInteractive) { $arguments += '--non-interactive' }
if ($InstallPython) { $arguments += '--install-python' }
if ($InstallPipx) { $arguments += '--install-pipx' }
if ($PipFallback) { $arguments += '--pip-fallback' }
if ($Package) { $arguments += @('--package', $Package) }
& $candidate.Source @launcherArgs @arguments
exit $LASTEXITCODE
