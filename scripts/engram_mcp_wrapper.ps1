# Fail-closed stdio wrapper for registered Engram projects on Windows.
$ErrorActionPreference = 'Stop'
# Declared before the first guard: Write-Error is terminating under
# $ErrorActionPreference = 'Stop', which made the explicit exit codes below
# unreachable and reported 1 instead of the documented 64/75/127.
function Write-WrapperLog([string]$Message) { [Console]::Error.WriteLine("[$((Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'))] $Message") }
function Stop-Wrapper([string]$Message, [int]$Code) { Write-WrapperLog $Message; exit $Code }
if ($env:ENGRAM_CLOUD_AUTOSYNC -or $env:ENGRAM_CLOUD_SERVER -or $env:ENGRAM_CLOUD_TOKEN -or $env:ENGRAM_REMOTE_URL -or $env:ENGRAM_TOKEN -or $env:ENGRAM_DATABASE_URL -or $env:ENGRAM_JWT_SECRET) {
    Stop-Wrapper 'ERROR cloud/autosync environment is unsupported by this locked local-only wrapper' 64
}
$defaultConfigDir = if ($env:APPDATA) { Join-Path $env:APPDATA 'naos-engram-memory' } else { Join-Path $HOME '.config\naos-engram-memory' }
$legacyConfigDir = if ($env:APPDATA) { Join-Path $env:APPDATA 'engram-memory' } else { Join-Path $HOME '.config\engram-memory' }
$legacyNames = @('ENGRAM_MEMORY_CONFIG_DIR', 'ENGRAM_MEMORY_TOOL', 'ENGRAM_MEMORY_REGISTRY', 'ENGRAM_MEMORY_LOG', 'ENGRAM_MEMORY_PYTHON', 'NAOS_ENGRAM_MEMORY_CONFIG_DIR', 'NAOS_ENGRAM_MEMORY_TOOL', 'NAOS_ENGRAM_MEMORY_REGISTRY', 'NAOS_ENGRAM_MEMORY_LOG', 'NAOS_ENGRAM_MEMORY_PYTHON')
if ($legacyNames | Where-Object { [Environment]::GetEnvironmentVariable($_) }) {
    Stop-Wrapper 'ERROR toolkit path overrides are not accepted by the installed MCP wrapper' 64
}
# NAOS governance declares data_dir: ~/.engram and documents ENGRAM_DATA_DIR as a
# runtime override, so a caller that merely restates the managed store used to be
# refused for agreeing with us. Accept that, refuse only a different store: the
# effective store still cannot vary, so maintenance can never back up or probe a
# database Engram does not open.
if ($env:ENGRAM_DATA_DIR) {
    $managedDataDir = Join-Path $HOME '.engram'
    $declaredDataDir = $env:ENGRAM_DATA_DIR
    if ($declaredDataDir -eq '~') { $declaredDataDir = [string]$HOME }
    elseif ($declaredDataDir.StartsWith('~/') -or $declaredDataDir.StartsWith('~\')) {
        $declaredDataDir = Join-Path $HOME $declaredDataDir.Substring(2)
    }
    $trim = [char[]]@([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar)
    $declaredNormalized = $declaredDataDir.TrimEnd($trim)
    $managedNormalized = $managedDataDir.TrimEnd($trim)
    if (-not [string]::Equals($declaredNormalized, $managedNormalized, [System.StringComparison]::OrdinalIgnoreCase)) {
        Stop-Wrapper "ERROR ENGRAM_DATA_DIR names a different store than this release manages; unset it or set it to $managedNormalized" 64
    }
}
$ConfigDir = __NAOS_ENGRAM_MEMORY_CONFIG_DIR__
if ($ConfigDir -eq ('__NAOS' + '_ENGRAM_MEMORY_CONFIG_DIR__')) { $ConfigDir = $defaultConfigDir }
$Tool = Join-Path $ConfigDir 'engram_memory.py'
$Registry = Join-Path $ConfigDir 'projects.json'
$MaintenanceLock = Join-Path $ConfigDir '.provider-maintenance.lock'
$ClientLeaseRoot = Join-Path $ConfigDir '.provider-client-leases'

function Assert-NoReparsePoint([string]$Path) {
    if (-not [System.IO.Path]::IsPathRooted($Path)) { Stop-Wrapper 'ERROR managed wrapper path is not absolute' 64 }
    $current = [System.IO.Path]::GetPathRoot($Path)
    foreach ($part in $Path.Substring($current.Length).Split([System.IO.Path]::DirectorySeparatorChar, [System.StringSplitOptions]::RemoveEmptyEntries)) {
        $current = Join-Path $current $part
        if (Test-Path -LiteralPath $current) {
            $item = Get-Item -LiteralPath $current -Force
            if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { Stop-Wrapper 'ERROR managed wrapper path crosses a reparse point' 64 }
        }
    }
}

$PythonExecutable = __PYTHON_EXECUTABLE__
if ($PythonExecutable -eq ('__PYTHON' + '_EXECUTABLE__')) { Stop-Wrapper 'ERROR configured Python 3.10+ interpreter is unavailable' 64 }
foreach ($managedPath in @($ConfigDir, $legacyConfigDir, $Tool, $Registry, $MaintenanceLock, $ClientLeaseRoot, $PythonExecutable)) { Assert-NoReparsePoint $managedPath }
if (-not (Test-Path -LiteralPath $ConfigDir) -and (Test-Path -LiteralPath $legacyConfigDir)) {
    Stop-Wrapper "ERROR legacy toolkit config detected at $legacyConfigDir; no files were read or merged" 64
}
if (-not (Test-Path -LiteralPath $Tool) -or -not (Test-Path -LiteralPath $Registry)) { Stop-Wrapper 'ERROR managed registry tool or project registry is not installed' 64 }
if (-not (Test-Path -LiteralPath $PythonExecutable)) { Stop-Wrapper 'ERROR configured Python 3.10+ interpreter is unavailable' 64 }
$requested = if ($env:ENGRAM_PROJECT) { $env:ENGRAM_PROJECT } elseif ($args.Count -gt 0) { $args[0] } else { '' }
$incomingProjectSignal = if ($requested) { 'present' } else { 'absent' }
$workspaceSignal = if ($env:CLAUDE_PROJECT_DIR) { 'claude_project_dir' } else { 'process_cwd' }
$projectCwd = if ($workspaceSignal -eq 'claude_project_dir') { $env:CLAUDE_PROJECT_DIR } else { (Get-Location).Path }
$toolArgs = @('--registry', $Registry, 'resolve', '--cwd', $projectCwd)
if ($requested) { $toolArgs += @('--project', $requested) }
try {
    $resolution = & $PythonExecutable $Tool @toolArgs
    if ($LASTEXITCODE -ne 0) { Stop-Wrapper 'ERROR canonical-project-resolution-failed; inspect a redacted inventory' 64 }
    $resolutionObject = $resolution | ConvertFrom-Json
    $canonical = $resolutionObject.project
    $resolutionSource = $resolutionObject.source
    if ($resolutionSource -notin @('approved_git_remote', 'registered_explicit_project')) {
        Stop-Wrapper 'ERROR canonical-project-resolution-returned-unsupported-source' 64
    }
} catch { Stop-Wrapper 'ERROR canonical-project-resolution-failed; inspect a redacted inventory' 64 }

if ($env:ENGRAM_BIN) { $EngramBin = $env:ENGRAM_BIN } elseif (Test-Path -LiteralPath __NAOS_ENGRAM_PROVIDER_BIN__) {
    $EngramBin = __NAOS_ENGRAM_PROVIDER_BIN__
} else {
    $engram = Get-Command engram -ErrorAction SilentlyContinue
    $EngramBin = if ($engram) { $engram.Source } else { Join-Path $HOME 'bin\engram.exe' }
}
Assert-NoReparsePoint $EngramBin
if (-not (Test-Path -LiteralPath $EngramBin)) { Stop-Wrapper 'ERROR Engram binary is not installed or executable' 127 }
$maintenancePresent = (Test-Path -LiteralPath $MaintenanceLock) -or ([System.IO.File]::Exists($MaintenanceLock))
if ($maintenancePresent) { Stop-Wrapper 'ERROR provider maintenance is active or requires manual lock review' 75 }
if (-not (Test-Path -LiteralPath $ClientLeaseRoot)) {
    New-Item -ItemType Directory -Path $ClientLeaseRoot -ErrorAction Stop | Out-Null
}
Assert-NoReparsePoint $ClientLeaseRoot
$ClientLease = Join-Path $ClientLeaseRoot ("client.{0}.{1}" -f $PID, [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $ClientLease -ErrorAction Stop | Out-Null
Assert-NoReparsePoint $ClientLease
$maintenancePresent = (Test-Path -LiteralPath $MaintenanceLock) -or ([System.IO.File]::Exists($MaintenanceLock))
if ($maintenancePresent) {
    Remove-Item -LiteralPath $ClientLease -ErrorAction SilentlyContinue
    Stop-Wrapper 'ERROR provider maintenance started during MCP client startup' 75
}
& $PythonExecutable $Tool provider verify-bound --home $HOME --config-dir $ConfigDir --path $EngramBin | Out-Null
if ($LASTEXITCODE -ne 0) { Stop-Wrapper 'ERROR Engram provider integrity verification failed' 64 }
Write-WrapperLog "spawn project=$canonical workspace_signal=$workspaceSignal resolution_source=$resolutionSource incoming_project_signal=$incomingProjectSignal provider_probe=not_run client_lease=active"
$allowedChildEnvironment = @{
    'HOME' = [string]$HOME
    'USERPROFILE' = [string]$env:USERPROFILE
    'SystemRoot' = [string]$env:SystemRoot
    'WINDIR' = [string]$env:WINDIR
    'ComSpec' = [string]$env:ComSpec
    'PATH' = if ($env:SystemRoot) { "$($env:SystemRoot)\System32;$($env:SystemRoot)" } else { '' }
    'TEMP' = [string]$env:TEMP
    'TMP' = [string]$env:TMP
    'LANG' = 'C'
    'NO_COLOR' = '1'
    'ENGRAM_PROJECT' = [string]$canonical
}
foreach ($name in @([Environment]::GetEnvironmentVariables('Process').Keys)) {
    [Environment]::SetEnvironmentVariable([string]$name, $null, 'Process')
}
foreach ($entry in $allowedChildEnvironment.GetEnumerator()) {
    if ($entry.Value) {
        [Environment]::SetEnvironmentVariable([string]$entry.Key, [string]$entry.Value, 'Process')
    }
}
$providerExitCode = 1
try {
    & $EngramBin mcp '--tools=mem_current_project,mem_context,mem_search,mem_get_observation,mem_save,mem_session_summary' "--project=$canonical"
    $providerExitCode = $LASTEXITCODE
} finally {
    # Remove only the lease directory created by this wrapper. If another
    # process inserted content, Remove-Item without -Recurse fails closed and
    # leaves the ambiguous lease for manual review.
    Remove-Item -LiteralPath $ClientLease -ErrorAction SilentlyContinue
}
exit $providerExitCode
