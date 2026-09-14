$ErrorActionPreference = "Stop"

$verifyScript = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..\scripts\verify-backup.ps1")).Path
$tempDirectory = Join-Path ([IO.Path]::GetTempPath()) ([IO.Path]::GetRandomFileName())
$backupFile = Join-Path $tempDirectory "database.dump"
$checksumFile = "$backupFile.sha256"

function global:pg_restore {
    $global:LASTEXITCODE = $global:MockPgRestoreExitCode
}

function Assert-FailsWith {
    param(
        [Parameter(Mandatory = $true)]
        [scriptblock]$Action,
        [Parameter(Mandatory = $true)]
        [string]$Pattern
    )

    try {
        & $Action
    }
    catch {
        if ($_.Exception.Message -notmatch $Pattern) {
            throw "Expected failure matching '$Pattern', got: $($_.Exception.Message)"
        }
        return
    }

    throw "Expected failure matching '$Pattern', but the command succeeded."
}

try {
    New-Item -ItemType Directory -Path $tempDirectory | Out-Null
    Set-Content -LiteralPath $backupFile -NoNewline -Value "test-backup"

    Assert-FailsWith -Pattern "checksum file is required" -Action {
        & $verifyScript -BackupFile $backupFile
    }

    Set-Content -LiteralPath $checksumFile -Value "invalid"
    Assert-FailsWith -Pattern "must contain a SHA-256 digest" -Action {
        & $verifyScript -BackupFile $backupFile
    }

    Set-Content -LiteralPath $checksumFile -Value ("0" * 64)
    Assert-FailsWith -Pattern "checksum validation failed" -Action {
        & $verifyScript -BackupFile $backupFile
    }

    $actualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $backupFile).Hash
    Set-Content -LiteralPath $checksumFile -Value $actualHash
    $global:MockPgRestoreExitCode = 9
    Assert-FailsWith -Pattern "archive validation failed with exit code 9" -Action {
        & $verifyScript -BackupFile $backupFile
    }

    $global:MockPgRestoreExitCode = 0
    $result = & $verifyScript -BackupFile $backupFile
    if ($result -notmatch "Backup verification completed") {
        throw "Expected successful backup verification output."
    }

    Write-Output "verify-backup.ps1 tests passed"
}
finally {
    Remove-Item -Path Function:\pg_restore -ErrorAction SilentlyContinue
    Remove-Variable -Name MockPgRestoreExitCode -Scope Global -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $tempDirectory) {
        Remove-Item -LiteralPath $tempDirectory -Recurse -Force
    }
}
