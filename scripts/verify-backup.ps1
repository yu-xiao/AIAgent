param(
    [Parameter(Mandatory = $true)]
    [string]$BackupFile
)

$ErrorActionPreference = "Stop"

$resolvedBackup = (Resolve-Path -LiteralPath $BackupFile -ErrorAction Stop).Path
$checksumFile = "$resolvedBackup.sha256"
if (-not (Test-Path -LiteralPath $checksumFile -PathType Leaf)) {
    throw "Backup checksum file is required: $checksumFile"
}

$expected = (Get-Content -Raw -LiteralPath $checksumFile).Trim()
if ($expected -notmatch "^[0-9a-fA-F]{64}$") {
    throw "Backup checksum file must contain a SHA-256 digest."
}

$actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $resolvedBackup).Hash
if (-not [string]::Equals($expected, $actual, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Backup checksum validation failed."
}

if (-not (Get-Command pg_restore -ErrorAction SilentlyContinue)) {
    throw "pg_restore is required to validate the backup archive."
}

& pg_restore --list --exit-on-error $resolvedBackup
if ($LASTEXITCODE -ne 0) {
    throw "pg_restore archive validation failed with exit code $LASTEXITCODE."
}

Write-Output "Backup verification completed: $resolvedBackup"
