<#
.SYNOPSIS
    Run a UniFi Protect archive sync into a local destination.

.DESCRIPTION
    Wraps `protect-archiver sync` with the things a repeatable backup needs and the CLI
    does not provide on its own: a single-instance lock, a timestamped log, and a check
    that the destination volume is actually mounted before anything is written to it.

    The account on this system is backed by Ubiquiti SSO with an email second factor, so
    there is no unattended mode: when the cached session token has expired the run stops
    and asks for a code. The token is cached between runs, so that is occasional rather
    than every time. Run this from an interactive terminal.

    Credentials come from the environment, never from arguments -- an argument is visible
    to every other process on the machine via the command line.

.PARAMETER Destination
    Archive root. Must already exist: the CLI declares it as an existing path and fails
    argument parsing otherwise.

.PARAMETER Verify
    How thoroughly to check files the archive already holds before skipping them.
    none  - trust the manifest, touch no files
    quick - confirm each file exists at its recorded size (default)
    hash  - re-compute SHA-256; reads the whole archive
    deep  - additionally ask ffprobe to decode each file

.PARAMETER Reconcile
    Adopt footage already on disk that the manifest does not know about, before syncing.
    Use once after losing the manifest, or when adopting an archive built by an older
    version, to avoid re-downloading what is already held.

.EXAMPLE
    .\scripts\protect-sync.ps1
    Normal incremental run against the defaults below.

.EXAMPLE
    .\scripts\protect-sync.ps1 -Verify hash
    Re-hash everything already archived and re-download anything that has rotted.
#>

[CmdletBinding()]
param(
    [string]$Destination = 'D:\Unifi',
    [string]$Address = $(if ($env:PROTECT_ADDRESS) { $env:PROTECT_ADDRESS } else { 'protect.invalid' }),
    [ValidateSet('none', 'quick', 'hash', 'deep')]
    [string]$Verify = 'quick',
    [switch]$Reconcile,
    [string]$LogDirectory = $(Join-Path $Destination 'logs'),
    [int]$WaitBetweenDownloads = 0
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Write-Log {
    param([string]$Message, [string]$Level = 'INFO')
    $line = '{0} [{1}] {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Message
    Write-Host $line
    if ($script:LogFile) { Add-Content -LiteralPath $script:LogFile -Value $line }
}

# -- preconditions ------------------------------------------------------------

foreach ($name in @('PROTECT_USERNAME', 'PROTECT_PASSWORD')) {
    if (-not (Get-Item "Env:$name" -ErrorAction SilentlyContinue)) {
        throw "$name is not set. Set it for your user account before running this script."
    }
}

# A missing destination is the failure mode worth catching early: if D: is an external or
# network volume that did not mount, the CLI would otherwise refuse at argument parsing
# with a message that does not say why.
if (-not (Test-Path -LiteralPath $Destination -PathType Container)) {
    throw "Destination '$Destination' does not exist or is not a directory. If it is on a removable or network volume, check that the volume is mounted."
}

if (-not (Test-Path -LiteralPath $LogDirectory)) {
    New-Item -ItemType Directory -Path $LogDirectory -Force | Out-Null
}
$script:LogFile = Join-Path $LogDirectory ('sync-{0}.log' -f (Get-Date -Format 'yyyyMMdd'))

# -- single instance ----------------------------------------------------------

# A sync can run for hours. Two of them against one archive would fight over the manifest
# and re-request the same segments, so hold an exclusive lock for the duration.
$lockPath = Join-Path $Destination '.protect-archive\sync.lock'
New-Item -ItemType Directory -Path (Split-Path $lockPath) -Force | Out-Null

$lockStream = $null
try {
    $lockStream = [System.IO.File]::Open(
        $lockPath,
        [System.IO.FileMode]::OpenOrCreate,
        [System.IO.FileAccess]::ReadWrite,
        [System.IO.FileShare]::None
    )
} catch [System.IO.IOException] {
    Write-Log "Another sync is already running against $Destination (lock held at $lockPath). Nothing to do." 'WARN'
    exit 0
}

try {
    $freeBytes = (Get-PSDrive -Name (Split-Path -Qualifier $Destination).TrimEnd(':')).Free
    Write-Log "Archive:     $Destination ($([math]::Round($freeBytes / 1GB, 1)) GB free)"
    Write-Log "Protect:     $Address as $env:PROTECT_USERNAME"
    Write-Log "Verify:      $Verify"
    Write-Log "Log:         $script:LogFile"

    $archiverArguments = @(
        'sync', $Destination,
        '--address', $Address,
        '--verify', $Verify,
        '--wait-between-downloads', $WaitBetweenDownloads,
        # Without this a single unrecoverable hour aborts the whole run; the manifest
        # records the failure so the next run retries exactly that hour.
        '--ignore-failed-downloads'
    )
    if ($Reconcile) { $archiverArguments += '--reconcile' }

    Write-Log "Running: protect-archiver $($archiverArguments -join ' ')"
    Write-Log 'If the cached session has expired you will be asked for a multi-factor code.'

    # Not redirected to a file: the multi-factor prompt has to reach the terminal.
    & protect-archiver @archiverArguments
    $exitCode = $LASTEXITCODE

    if ($exitCode -eq 0) {
        Write-Log 'Sync completed.'
    } else {
        Write-Log "Sync exited with code $exitCode. Segments recorded as failed will be retried on the next run; 'protect-archiver verify $Destination' shows the archive's current state." 'WARN'
    }
    exit $exitCode
} finally {
    if ($lockStream) { $lockStream.Dispose() }
}
