[CmdletBinding()]
param([ValidateSet('Run', 'Stop')][string]$Action = 'Run')

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:COMPOSE_PROJECT_NAME = 'datamind-demo'
$env:DATAMIND_DEMO_ARCH = 'amd64'
$dockerDesktop = Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'
$dockerBin = Join-Path $env:ProgramFiles 'Docker\Docker\resources\bin'
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    $env:Path = "$dockerBin;$env:Path"
}

function Test-DockerReady {
    try {
        & docker info *> $null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

function Invoke-Docker {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    & docker @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Docker operation failed (exit $LASTEXITCODE)."
    }
}

try {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw 'Docker Desktop is required. Install it and finish its first-time setup.'
    }
    if ($Action -eq 'Stop') {
        if (-not (Test-DockerReady)) {
            Write-Host 'Docker is not running. Demo data is preserved.'
            exit 0
        }
        Invoke-Docker -Arguments @('compose', '-f', 'compose.yaml', 'stop')
        Write-Host 'Demo stopped. Databases, uploads and model configuration are preserved.'
        exit 0
    }
    $hostArchitecture = $env:PROCESSOR_ARCHITECTURE
    if ($env:PROCESSOR_ARCHITEW6432) { $hostArchitecture = $env:PROCESSOR_ARCHITEW6432 }
    if ($hostArchitecture -ne 'AMD64') {
        throw 'This Windows package requires an x86-64 (Intel/AMD) computer.'
    }
    if (-not (Test-DockerReady)) {
        if (-not (Test-Path -LiteralPath $dockerDesktop)) {
            throw 'Cannot find Docker Desktop. Install it and finish its first-time setup.'
        }
        Write-Host 'Starting Docker Desktop...'
        Start-Process -FilePath $dockerDesktop
        $daemonReady = $false
        for ($attempt = 0; $attempt -lt 120; $attempt++) {
            if (Test-DockerReady) { $daemonReady = $true; break }
            Start-Sleep -Seconds 2
        }
        if (-not $daemonReady) { throw 'Docker Desktop is not ready. Check its window.' }
    }
    $containerType = & docker info --format '{{.OSType}}'
    if ($LASTEXITCODE -ne 0 -or $containerType -ne 'linux') {
        throw 'Switch Docker Desktop to Linux containers.'
    }
    Invoke-Docker -Arguments @('compose', 'version')
    New-Item -ItemType Directory -Path 'data' -Force | Out-Null
    if (-not (Test-Path -LiteralPath 'data/model_config.env')) {
        Copy-Item -LiteralPath 'data/model_config.env.example' -Destination 'data/model_config.env'
    }
    $archive = 'images/amd64.tar.gz'
    $marker = 'data/.image-import-amd64.sha256'
    $imagesReady = $true
    foreach ($name in @('app', 'postgres', 'qdrant')) {
        try {
            & docker image inspect "datamind-demo-${name}:20261019-amd64" *> $null
            if ($LASTEXITCODE -ne 0) { $imagesReady = $false }
        } catch {
            $imagesReady = $false
        }
    }
    if (Test-Path -LiteralPath $archive) {
        $digest = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
        $previous = ''
        if (Test-Path -LiteralPath $marker) { $previous = (Get-Content -LiteralPath $marker -Raw).Trim() }
        if (-not $imagesReady -or $digest -ne $previous) {
            Write-Host 'Importing offline images. The first launch may take several minutes...'
            Invoke-Docker -Arguments @('load', '--input', $archive)
            Set-Content -LiteralPath $marker -Value $digest -Encoding Ascii
        }
    } elseif (-not $imagesReady) {
        throw 'Missing images/amd64.tar.gz. Extract the complete demo package.'
    } else {
        Write-Host 'Using demo images already imported on this computer.'
    }
    foreach ($name in @('app', 'postgres', 'qdrant')) {
        $actual = & docker image inspect --format '{{.Architecture}}' "datamind-demo-${name}:20261019-amd64"
        if ($LASTEXITCODE -ne 0 -or $actual -ne 'amd64') { throw "Wrong image architecture: $name" }
    }
    if (-not (Test-Path -LiteralPath 'seed/postgres/roles.sql')) {
        throw 'Missing seed/postgres/roles.sql. Extract the complete demo package.'
    }
    Write-Host 'Starting databases, knowledge base and demo page...'
    Invoke-Docker -Arguments @('compose', '-f', 'compose.yaml', 'up', '-d')
    $ready = $false
    $directClient = New-Object System.Net.WebClient
    $directClient.Proxy = $null
    for ($attempt = 0; $attempt -lt 150; $attempt++) {
        $containerId = & docker compose -f compose.yaml ps -q app 2>$null
        if ($containerId) {
            $health = & docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' $containerId 2>$null
            if ($health -eq 'healthy') {
                try {
                    $null = $directClient.DownloadString('http://127.0.0.1:8000/')
                    $ready = $true
                    break
                } catch { }
            }
        }
        Start-Sleep -Seconds 2
    }
    $directClient.Dispose()
    if (-not $ready) { throw 'Demo did not become ready within 5 minutes. See the logs below.' }
    Write-Host 'Demo ready: http://127.0.0.1:8000/'
    Write-Host 'Use stop.bat to stop it. The next launch reuses your data.'
    Start-Process 'http://127.0.0.1:8000/'
    exit 0
} catch {
    Write-Host ''
    Write-Host "Demo operation failed: $($_.Exception.Message)" -ForegroundColor Red
    if ((Get-Command docker -ErrorAction SilentlyContinue) -and (Test-DockerReady)) {
        & docker compose -f compose.yaml ps -a
        & docker compose -f compose.yaml logs --no-color --tail 80
    }
    Write-Host 'Data is preserved. Fix the error and launch again.'
    exit 1
}
