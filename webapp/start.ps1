param([string]$QgisPython = '', [int]$Port = 8000)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
if (-not $QgisPython) {
    $installations = Get-ChildItem -LiteralPath $env:ProgramFiles -Directory -Filter '*QGIS*' | Sort-Object Name -Descending
    foreach ($installation in $installations) {
        $candidate = Join-Path $installation.FullName 'bin\python-qgis.bat'
        if (Test-Path -LiteralPath $candidate) { $QgisPython = $candidate; break }
    }
}
if (-not $QgisPython -or -not (Test-Path -LiteralPath $QgisPython)) {
    throw 'Не найдена среда Python с GDAL. Укажите -QgisPython путь\к\python-qgis.bat или используйте Docker Compose из корня проекта.'
}
Push-Location $projectRoot
try {
    if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot '.deps\fastapi\__init__.py'))) {
        & $QgisPython -m pip install --target (Join-Path $PSScriptRoot '.deps') -r (Join-Path $PSScriptRoot 'requirements.txt') --no-input --keyring-provider disabled --no-cache-dir
        if ($LASTEXITCODE -ne 0) { throw 'Не удалось установить зависимости сервера.' }
    }
    if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'frontend\dist\index.html'))) {
        Push-Location (Join-Path $PSScriptRoot 'frontend')
        try {
            & npm.cmd ci --cache (Join-Path $projectRoot '.npm-cache') --no-audit --no-fund
            if ($LASTEXITCODE -ne 0) { throw 'Не удалось установить зависимости интерфейса.' }
            & npm.cmd run build
            if ($LASTEXITCODE -ne 0) { throw 'Не удалось собрать интерфейс.' }
        } finally { Pop-Location }
    }
    $env:PORT = [string]$Port
    Write-Host "ГеоРиск: http://127.0.0.1:$Port  |  Остановка: Ctrl+C"
    & $QgisPython -X utf8 (Join-Path $PSScriptRoot 'run.py')
} finally { Pop-Location }
