param(
    [Parameter(Mandatory = $true)]
    [string]$OutputDirectory,
    [Parameter(Mandatory = $true)]
    [string]$DatabaseHost,
    [int]$DatabasePort = 5432,
    [string]$DatabaseName = "ai_agent",
    [string]$DatabaseUser = "ai_agent",
    [Parameter(Mandatory = $true)]
    [string]$DatabasePasswordFile,
    [switch]$IncludeVaultSnapshot,
    [string]$VaultAddress = "",
    [string]$VaultTokenFile = ""
)

$ErrorActionPreference = "Stop"

$passwordPath = (Resolve-Path -LiteralPath $DatabasePasswordFile).Path
if (-not (Test-Path -LiteralPath $OutputDirectory -PathType Container)) {
    New-Item -ItemType Directory -Path $OutputDirectory | Out-Null
}
$outputPath = (Resolve-Path -LiteralPath $OutputDirectory).Path
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$databaseBackup = Join-Path $outputPath "ai-agent-postgres-$timestamp.dump"
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
    & pg_dump --host $DatabaseHost --port $DatabasePort --username $DatabaseUser --dbname $DatabaseName --format custom --file $databaseBackup
    if ($LASTEXITCODE -ne 0) {
        throw "pg_dump failed with exit code $LASTEXITCODE."
    }
    $databaseHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $databaseBackup).Hash
    Set-Content -LiteralPath "$databaseBackup.sha256" -Value $databaseHash

    if ($IncludeVaultSnapshot) {
        if (-not $VaultAddress -or -not $VaultTokenFile) {
            throw "VaultAddress and VaultTokenFile are required for a Vault snapshot."
        }
        $resolvedVaultToken = (Resolve-Path -LiteralPath $VaultTokenFile).Path
        $previousVaultAddress = $env:VAULT_ADDR
        $previousVaultToken = $env:VAULT_TOKEN
        try {
            $env:VAULT_ADDR = $VaultAddress
            $env:VAULT_TOKEN = (Get-Content -Raw -LiteralPath $resolvedVaultToken).Trim()
            $vaultBackup = Join-Path $outputPath "ai-agent-vault-$timestamp.snap"
            & vault operator raft snapshot save $vaultBackup
            if ($LASTEXITCODE -ne 0) {
                throw "Vault snapshot failed with exit code $LASTEXITCODE."
            }
            $vaultHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $vaultBackup).Hash
            Set-Content -LiteralPath "$vaultBackup.sha256" -Value $vaultHash
        }
        finally {
            $env:VAULT_ADDR = $previousVaultAddress
            $env:VAULT_TOKEN = $previousVaultToken
        }
    }

    Write-Output "Backup completed: $databaseBackup"
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
