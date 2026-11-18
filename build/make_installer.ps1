<#
.SYNOPSIS
    构建 NetAnchor-Campus 安装包：PyInstaller onedir → Inno Setup → SHA256SUMS。

.DESCRIPTION
    版本号唯一来源是 src\netanchor\__init__.py，注入给 Inno，不在脚本或 .iss 里写第二份。
    简体中文是硬要求：编译器缺少 ChineseSimplified.isl 时直接失败，不退回英文。

.PARAMETER SkipBuild
    复用已有 dist\NetAnchor-Campus，只重打安装包。

.PARAMETER IsccPath
    显式指定 ISCC.exe；默认在 Inno Setup 6 安装目录里找。

.PARAMETER Python
    用来跑 PyInstaller 的解释器，默认 `python`。
    机器上装了多个 Python 时，指定那个装了 PyInstaller 与依赖的。
#>
[CmdletBinding()]
param(
    [switch]$SkipBuild,
    [string]$IsccPath,
    [string]$Python = 'python'
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

function Get-AppVersion {
    $init = Join-Path $repo 'src\netanchor\__init__.py'
    $match = Select-String -LiteralPath $init -Pattern '^__version__\s*=\s*"([^"]+)"' |
        Select-Object -First 1
    if (-not $match) { throw "无法从 $init 读取版本号" }
    return $match.Matches[0].Groups[1].Value
}

function Resolve-Iscc([string]$explicit) {
    $candidates = @()
    if ($explicit) { $candidates += $explicit }
    $candidates += @(
        (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'),
        (Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe')
    )
    $onPath = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($onPath) { $candidates += $onPath.Source }
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) { return $candidate }
    }
    throw '找不到 ISCC.exe。请先安装 Inno Setup 6（winget install JRSoftware.InnoSetup）。'
}

$version = Get-AppVersion
$iscc = Resolve-Iscc $IsccPath
Write-Host "版本: $version"
Write-Host "ISCC: $iscc"

# 简体中文必须存在：缺了就失败，不允许静默产出英文安装包。
# 文件随仓库分发（build\languages），不依赖编译器自带翻译，避免版本漂移。
$chinese = Join-Path $PSScriptRoot 'languages\ChineseSimplified.isl'
if (-not (Test-Path -LiteralPath $chinese)) {
    throw "缺少简体中文语言文件: $chinese。安装包必须带简体中文，构建中止。"
}
Write-Host "简体中文语言文件: $chinese"

if (-not $SkipBuild) {
    Write-Host '构建 onedir ...'
    & $Python -m PyInstaller 'build\netanchor.spec' --noconfirm --workpath 'build\work' --distpath 'dist'
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败（退出码 $LASTEXITCODE）" }
}

$payload = Join-Path $repo 'dist\NetAnchor-Campus'
if (-not (Test-Path -LiteralPath (Join-Path $payload 'NetAnchor.Cli.exe'))) {
    throw "打包产物不完整: $payload"
}

# 只重打安装器（-SkipBuild）时，dist 里可能还是上一版的可执行文件；
# 那样安装包版本号是新的、里面的程序却是旧的，装完才发现 GUI/行为不对。
$payloadVersion = (& (Join-Path $payload 'NetAnchor.Cli.exe') --version) -replace '^NetAnchor\.Cli\s+', ''
if ($payloadVersion.Trim() -ne $version) {
    throw "打包产物是 $payloadVersion，与源码版本 $version 不一致。请去掉 -SkipBuild 重新构建。"
}

# 版本号没变也可能改了代码（同版本重新构建），再比一次时间戳
$payloadTime = (Get-Item -LiteralPath (Join-Path $payload 'NetAnchor.Tray.exe')).LastWriteTime
$newest = Get-ChildItem -LiteralPath (Join-Path $repo 'src') -Recurse -File -Filter *.py |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($newest -and $newest.LastWriteTime -gt $payloadTime) {
    throw "打包产物（$payloadTime）比源码（$($newest.Name) @ $($newest.LastWriteTime)）旧，请去掉 -SkipBuild 重新构建。"
}
Write-Host "打包产物版本: $payloadVersion（$payloadTime）"

Write-Host '打包安装程序 ...'
& $iscc "/DAppVersion=$version" 'build\installer.iss'
if ($LASTEXITCODE -ne 0) { throw "ISCC 失败（退出码 $LASTEXITCODE）" }

$setup = Join-Path $repo "dist\installer\NetAnchor-Campus-$version-setup.exe"
if (-not (Test-Path -LiteralPath $setup)) { throw "未生成 $setup" }

$sums = Join-Path $repo 'dist\installer\SHA256SUMS.txt'
$hash = (Get-FileHash -LiteralPath $setup -Algorithm SHA256).Hash.ToLower()
"{0}  {1}" -f $hash, (Split-Path -Leaf $setup) | Set-Content -LiteralPath $sums -Encoding utf8

$sizeMb = [math]::Round((Get-Item -LiteralPath $setup).Length / 1MB, 1)
Write-Host "安装包: $setup ($sizeMb MB)"
Write-Host "校验和: $sums"
Get-Content -LiteralPath $sums