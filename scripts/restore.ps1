param(
    [Parameter(Mandatory = $true)]
    [string]$BackupFile,
    [Parameter(Mandatory = $true)]
    [string]$DatabaseHost,
    [int]$DatabasePort = 5432,
    [string]$DatabaseName = "ai_agent",
    [string]$DatabaseUser = "ai_agent",
    [Parameter(Mandatory = $true)]
    [string]$DatabasePasswordFile,
    [switch]$ConfirmRestore
)

$ErrorActionPreference = "Stop"

if (-not $ConfirmRestore) {
    throw "Restore replaces database objects. Re-run with -ConfirmRestore after verifying the target."
}

$resolvedBackup = (Resolve-Path -LiteralPath $BackupFile).Path
$passwordPath = (Resolve-Path -LiteralPath $DatabasePasswordFile).Path
& (Join-Path $PSScriptRoot "verify-backup.ps1") -BackupFile $resolvedBackup

$tempDirectory = Join-Path ([IO.Path]::GetTempPath()) ([IO.Path]::GetRandomFileName())
New-Item -ItemType Directory -Path $tempDirectory | Out-Null
$pgpass = Join-Path $tempDirectory "pgpass"
$previousPgpass = $env:PGPASSFILE

try {
    $password = (Get-Content -Raw -LiteralPath $passwordPath).Trim()
    if (-not $password) {
        throw "Database password file is empty."
    }
    $escapedPassword = $password.Replace("\", "\\").Replace(":", "\:")
    Set-Content -LiteralPath $pgpass -NoNewline -Value "$DatabaseHost`:$DatabasePort`:$DatabaseName`:$DatabaseUser`:$escapedPassword"
    $env:PGPASSFILE = $pgpass
    & pg_restore --host $DatabaseHost --port $DatabasePort --username $DatabaseUser --dbname $DatabaseName --clean --if-exists --exit-on-error $resolvedBackup
    if ($LASTEXITCODE -ne 0) {
        throw "pg_restore failed with exit code $LASTEXITCODE."
    }
    Write-Output "Restore completed: $resolvedBackup"
}
finally {
    $env:PGPASSFILE = $previousPgpass
    if (Test-Path -LiteralPath $pgpass) {
        Remove-Item -LiteralPath $pgpass -Force
    }
    if (Test-Path -LiteralPath $tempDirectory) {
        Remove-Item -LiteralPath $tempDirectory -Force
    }
}
