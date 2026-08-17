[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[0-9a-f]{64}$')]
    [string]$PreviousImage,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-f]{40}$')]
    [string]$PreviousSourceRevision,

    [string]$ComposeFile
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$callerWhatIfPreference = $WhatIfPreference
$WhatIfPreference = $false
try {
    $canonicalCompose = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot 'compose.local.yml')).Path
    $ComposeFile = if ([string]::IsNullOrWhiteSpace($ComposeFile)) { $canonicalCompose } else { $ComposeFile }
    $reviewedComposeSha256 = 'c97a4b9abf17c295d4d0613dcef1cfdadea847637975a2bc88a5d0d1b08f9cb2'
    $resolvedCompose = (Resolve-Path -LiteralPath $ComposeFile).Path
    if (-not [StringComparer]::OrdinalIgnoreCase.Equals($resolvedCompose, $canonicalCompose)) {
        throw 'compose file must be the canonical reviewed template'
    }
    $actualComposeSha256 = (Get-FileHash -LiteralPath $resolvedCompose -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualComposeSha256 -cne $reviewedComposeSha256) {
        throw 'canonical compose file does not match its reviewed hash'
    }
}
finally {
    $WhatIfPreference = $callerWhatIfPreference
}

if ([Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT) {
    $commonData = [Environment]::GetFolderPath([Environment+SpecialFolder]::CommonApplicationData)
    $operationLockRoot = Join-Path $commonData 'tapd-capability\locks'
}
else {
    $operationLockRoot = '/var/lock/tapd-capability'
}
try {
    [IO.Directory]::CreateDirectory($operationLockRoot) | Out-Null
}
catch {
    throw 'deployment lock unavailable'
}

$operationLockFile = Join-Path $operationLockRoot 'staging.lock'
$operationLockToken = [Guid]::NewGuid().ToString('N')
$operationLockStream = $null
$operationLockOwned = $false
try {
    try {
        $operationLockStream = [IO.File]::Open(
            $operationLockFile,
            [IO.FileMode]::CreateNew,
            [IO.FileAccess]::Write,
            [IO.FileShare]::None
        )
        $tokenBytes = [Text.Encoding]::ASCII.GetBytes($operationLockToken)
        $operationLockStream.Write($tokenBytes, 0, $tokenBytes.Length)
        $operationLockStream.Flush($true)
        $operationLockOwned = $true
    }
    catch [IO.IOException] {
        if ($null -ne $operationLockStream) {
            $operationLockStream.Dispose()
            $operationLockStream = $null
            try { [IO.File]::Delete($operationLockFile) } catch { throw 'deployment lock unavailable' }
        }
        if ([IO.File]::Exists($operationLockFile)) {
            throw 'deployment busy: another tapd-capability operation is active'
        }
        throw 'deployment lock unavailable'
    }
    catch {
        if ($null -ne $operationLockStream) {
            $operationLockStream.Dispose()
            $operationLockStream = $null
            try { [IO.File]::Delete($operationLockFile) } catch { }
        }
        throw 'deployment lock unavailable'
    }

    $imageRevision = docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' $PreviousImage
    if ($LASTEXITCODE -ne 0) {
        throw "Previous immutable image is not available locally: $PreviousImage"
    }
    if ($imageRevision.Trim() -ne $PreviousSourceRevision) {
        throw 'Previous image revision label does not match PreviousSourceRevision.'
    }

    if (-not $PSCmdlet.ShouldProcess('tapd-capability local staging', "roll back to $PreviousImage")) {
        return
    }

    $previousOverride = $env:TAPD_CAPABILITY_IMAGE
    $previousRevisionOverride = $env:TAPD_CAPABILITY_SOURCE_REVISION
    try {
        $env:TAPD_CAPABILITY_IMAGE = $PreviousImage
        $env:TAPD_CAPABILITY_SOURCE_REVISION = $PreviousSourceRevision
        docker compose -f $resolvedCompose up -d --no-build tapd-capability
        if ($LASTEXITCODE -ne 0) {
            throw 'Compose rollback failed.'
        }

        $health = Invoke-RestMethod -Uri 'http://127.0.0.1:3796/healthz' -TimeoutSec 5
        if ($health.status -ne 'ok') {
            throw 'Previous image did not pass /healthz after rollback.'
        }
        Write-Host "Rollback health check passed for $PreviousImage"
    }
    finally {
        if ($null -eq $previousOverride) {
            Remove-Item Env:TAPD_CAPABILITY_IMAGE -ErrorAction SilentlyContinue
        }
        else {
            $env:TAPD_CAPABILITY_IMAGE = $previousOverride
        }
        if ($null -eq $previousRevisionOverride) {
            Remove-Item Env:TAPD_CAPABILITY_SOURCE_REVISION -ErrorAction SilentlyContinue
        }
        else {
            $env:TAPD_CAPABILITY_SOURCE_REVISION = $previousRevisionOverride
        }
    }
}
finally {
    if ($null -ne $operationLockStream) {
        $operationLockStream.Dispose()
        $operationLockStream = $null
    }
    if ($operationLockOwned) {
        try {
            $currentLockToken = [IO.File]::ReadAllText($operationLockFile).Trim()
            if ($currentLockToken -cne $operationLockToken) {
                throw 'lock ownership changed'
            }
            [IO.File]::Delete($operationLockFile)
        }
        catch {
            throw 'deployment lock release failed'
        }
    }
}
