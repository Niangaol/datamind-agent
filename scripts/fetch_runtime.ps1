<#
.SYNOPSIS
    下载便携发行包所需的运行时压缩包，缓存在 delivery_cache/ 里，重复执行不会重下。

.DESCRIPTION
    供便携运行时装配使用。本脚本只负责「把 4 个文件放到缓存目录并校验」，
    不负责解压、不负责打包。

    缓存命中判据（两者都满足才跳过）：
      1. 文件大小与 delivery_cache/runtime_manifest.json 里记录的一致；
      2. 文件 sha256 与 manifest 里记录的一致。
    记录不一致（下载被截断、上游换了产物）时重下，而不是默默用坏文件。

    下载优先用 Windows 自带的 curl.exe（支持断点续传与重试）；没有 curl 时退回
    Invoke-WebRequest。走代理是默认行为，代理地址可用 -Proxy 覆盖，-Direct 表示直连。

.PARAMETER CacheDir
    缓存目录。默认 <仓库上级>\delivery_cache。

.PARAMETER Proxy
    HTTP 代理，默认 http://127.0.0.1:7890。

.PARAMETER Direct
    不走代理（直连）。

.PARAMETER Force
    忽略缓存，全部重下。

.EXAMPLE
    pwsh -File scripts/fetch_runtime.ps1
    pwsh -File scripts/fetch_runtime.ps1 -Direct
    pwsh -File scripts/fetch_runtime.ps1 -Force
#>
[CmdletBinding()]
param(
    [string]$CacheDir,
    [string]$Proxy = 'http://127.0.0.1:7890',
    [switch]$Direct,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$RepoRoot = Split-Path -Parent $PSScriptRoot
$BaseRoot = Split-Path -Parent $RepoRoot
if (-not $CacheDir) { $CacheDir = Join-Path $BaseRoot 'delivery_cache' }
$CacheDir = [System.IO.Path]::GetFullPath($CacheDir)
$ManifestPath = Join-Path $CacheDir 'runtime_manifest.json'

if (-not (Test-Path $CacheDir)) { New-Item -ItemType Directory -Path $CacheDir -Force | Out-Null }

function Write-Step([string]$Text) { Write-Host "==> $Text" -ForegroundColor Cyan }

# ── 下载清单 ────────────────────────────────────────────────────────────────
# 版本是刻意固定的：便携运行时一旦变更，包内 PostgreSQL 数据目录的 pg_control
# 版本与 qdrant 存储格式都可能不再兼容，所以升级必须走一次完整重打包。
$PythonVersion = '3.14.5'
$Items = @(
    [pscustomobject]@{
        Name = "python-$PythonVersion-embed-amd64.zip"
        Url  = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip"
        Note = "CPython $PythonVersion 嵌入式发行版（与本仓库 .venv 同一个小版本）"
    }
    [pscustomobject]@{
        Name = 'qdrant-x86_64-pc-windows-msvc.zip'
        Url  = 'https://github.com/qdrant/qdrant/releases/download/v1.12.4/qdrant-x86_64-pc-windows-msvc.zip'
        Note = 'Qdrant v1.12.4 单文件二进制（与 deploy/compose.yaml 同版本）'
    }
    [pscustomobject]@{
        Name = 'postgresql-16.4-1-windows-x64-binaries.zip'
        Url  = 'https://get.enterprisedb.com/postgresql/postgresql-16.4-1-windows-x64-binaries.zip'
        Note = 'PostgreSQL 16.4 免安装二进制（与 deploy/compose.yaml 的 postgres:16 同大版本）'
    }
    [pscustomobject]@{
        Name = 'get-pip.py'
        Url  = 'https://bootstrap.pypa.io/get-pip.py'
        Note = '给嵌入式 Python 装 pip 的引导脚本'
    }
)

function Get-Sha256([string]$Path) {
    return (Get-FileHash -Path $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Read-Manifest {
    if (-not (Test-Path $ManifestPath)) { return @{} }
    try {
        $raw = Get-Content $ManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        Write-Host "缓存清单无法解析，按空的处理：$($_.Exception.Message)" -ForegroundColor Yellow
        return @{}
    }
    $table = @{}
    foreach ($entry in $raw) { $table[$entry.name] = $entry }
    return $table
}

function Save-Manifest($Table) {
    $list = @()
    foreach ($key in ($Table.Keys | Sort-Object)) { $list += $Table[$key] }
    $json = ConvertTo-Json -InputObject @($list) -Depth 4
    Set-Content -Path $ManifestPath -Value $json -Encoding UTF8
}

function Invoke-Download([string]$Url, [string]$Target, [string]$ProxyValue) {
    $part = "$Target.part"
    if (Test-Path $part) { Remove-Item $part -Force }
    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    if ($curl) {
        $curlArgs = @('-L', '--fail', '--retry', '3', '--retry-delay', '3', '--connect-timeout', '30',
                      '--speed-time', '120', '--speed-limit', '1024', '-o', $part)
        if ($ProxyValue) { $curlArgs += @('--proxy', $ProxyValue) }
        $curlArgs += $Url
        Write-Host ("curl " + ($curlArgs -join ' '))
        & $curl.Source @curlArgs
        if ($LASTEXITCODE -ne 0) { throw "curl 退出码 $LASTEXITCODE：$Url" }
    } else {
        $params = @{ Uri = $Url; OutFile = $part; UseBasicParsing = $true; TimeoutSec = 3600 }
        if ($ProxyValue) { $params['Proxy'] = $ProxyValue }
        Invoke-WebRequest @params
    }
    if (-not (Test-Path $part)) { throw "下载后没有产生文件：$Target" }
    $length = (Get-Item $part).Length
    if ($length -le 0) { throw "下载到 0 字节：$Url" }
    Move-Item -Path $part -Destination $Target -Force
    return $length
}

$proxyValue = if ($Direct) { '' } else { $Proxy }
$manifest = Read-Manifest
$changed = $false

foreach ($item in $Items) {
    $target = Join-Path $CacheDir $item.Name
    if ($Force -and (Test-Path $target)) { Remove-Item $target -Force }
    if (Test-Path $target) {
        $record = $null
        if ($manifest.ContainsKey($item.Name)) { $record = $manifest[$item.Name] }
        if ($record -and $record.sha256) {
            $actual = Get-Sha256 $target
            if ($actual -eq $record.sha256 -and (Get-Item $target).Length -eq [int64]$record.size) {
                Write-Host ("缓存命中  {0}  ({1:N1} MB)" -f $item.Name, ((Get-Item $target).Length / 1MB)) -ForegroundColor Green
                continue
            }
            Write-Host ("缓存校验失败，重下：{0}" -f $item.Name) -ForegroundColor Yellow
        } else {
            Write-Host ("缓存无校验记录，重下：{0}" -f $item.Name) -ForegroundColor Yellow
        }
    }
    Write-Step ("下载 {0}  ({1})" -f $item.Name, $item.Note)
    $size = Invoke-Download -Url $item.Url -Target $target -ProxyValue $proxyValue
    $hash = Get-Sha256 $target
    $manifest[$item.Name] = [pscustomobject]@{
        name = $item.Name; url = $item.Url; size = $size; sha256 = $hash
        note = $item.Note; fetched_at = (Get-Date).ToUniversalTime().ToString('o')
    }
    $changed = $true
    Write-Host ("  完成 {0:N1} MB  sha256={1}" -f ($size / 1MB), $hash) -ForegroundColor Green
}

if ($changed) { Save-Manifest $manifest }

Write-Host ''
Write-Host "缓存目录：$CacheDir" -ForegroundColor Cyan
Get-ChildItem $CacheDir -File | Where-Object { $_.Name -ne 'runtime_manifest.json' } |
    Sort-Object Name | ForEach-Object { Write-Host ("  {0,-52} {1,9:N1} MB" -f $_.Name, ($_.Length / 1MB)) }
$total = (Get-ChildItem $CacheDir -File | Measure-Object -Sum Length).Sum
Write-Host ("  合计 {0:N1} MB" -f ($total / 1MB)) -ForegroundColor Cyan
exit 0
