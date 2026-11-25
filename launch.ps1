<#
.SYNOPSIS
    Lanzador PowerShell del MT5 Bot con log dual (consola + archivo).

.DESCRIPTION
    Equivalente a start_bot.bat: verifica MetaTrader 5 y Python, instala
    dependencias si faltan, abre el panel de configuracion y ejecuta
    trading_bot.py duplicando la salida en logs\bot_YYYYMMDD_HHMMSS.log.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\launch.ps1
    powershell -ExecutionPolicy Bypass -File .\launch.ps1 -DryRun -SkipPanel
#>
[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$SkipPanel,
    [string]$Mt5Path = $(if ($env:MT5_PATH) { $env:MT5_PATH } else { 'C:\Program Files\MetaTrader 5\terminal64.exe' }),
    [int]$Mt5StartupSeconds = 20
)

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUNBUFFERED = '1'

function Write-Info([string]$Message) { Write-Host "[INFO]  $Message" -ForegroundColor Cyan }
function Write-Ok([string]$Message)   { Write-Host "[OK]    $Message" -ForegroundColor Green }
function Write-Warn([string]$Message) { Write-Host "[WARN]  $Message" -ForegroundColor Yellow }
function Write-Err([string]$Message)  { Write-Host "[ERROR] $Message" -ForegroundColor Red }

# Ejecuta un comando nativo sin que PowerShell 5.1 convierta stderr en excepcion.
function Invoke-Native([scriptblock]$Block) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $Block } finally { $ErrorActionPreference = $previous }
}

$exitCode = 1
try {
    Write-Host '==========================================================' -ForegroundColor DarkCyan
    Write-Host '  MT5 Bot - launch.ps1  (framework educativo, usar en DEMO)' -ForegroundColor DarkCyan
    Write-Host '==========================================================' -ForegroundColor DarkCyan

    $logDir = Join-Path $PSScriptRoot 'logs'
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    $logFile = Join-Path $logDir ("bot_{0}.log" -f (Get-Date -Format 'yyyyMMdd_HHmmss'))

    # 1) MetaTrader 5
    if (Get-Process -Name 'terminal64' -ErrorAction SilentlyContinue) {
        Write-Ok 'MetaTrader 5 en ejecucion.'
    } else {
        Write-Warn 'MetaTrader 5 no esta en ejecucion.'
        if (-not (Test-Path -LiteralPath $Mt5Path)) {
            throw "No se encontro terminal64.exe en '$Mt5Path'. Use -Mt5Path o la variable MT5_PATH."
        }
        Write-Info "Abriendo $Mt5Path y esperando $Mt5StartupSeconds s..."
        Start-Process -FilePath $Mt5Path | Out-Null
        Start-Sleep -Seconds $Mt5StartupSeconds
        if (-not (Get-Process -Name 'terminal64' -ErrorAction SilentlyContinue)) {
            throw 'MetaTrader 5 no se pudo iniciar.'
        }
        Write-Ok 'MetaTrader 5 iniciado.'
    }

    # 2) Python
    $python = $null
    $pyPrefix = @()
    if (Get-Command 'python' -ErrorAction SilentlyContinue) {
        $version = Invoke-Native { & python --version 2>&1 }
        if ($LASTEXITCODE -eq 0) { $python = 'python' }
    }
    if (-not $python -and (Get-Command 'py' -ErrorAction SilentlyContinue)) {
        $version = Invoke-Native { & py -3 --version 2>&1 }
        if ($LASTEXITCODE -eq 0) { $python = 'py'; $pyPrefix = @('-3') }
    }
    if (-not $python) { throw 'Python 3.10+ no esta en el PATH. Instalelo desde python.org.' }
    Write-Ok "$version detectado."

    # 3) Dependencias
    Invoke-Native { & $python @pyPrefix -c 'import MetaTrader5, pandas, numpy' 2>&1 | Out-Null }
    $depsOk = ($LASTEXITCODE -eq 0)
    if ($depsOk) {
        Invoke-Native { & $python @pyPrefix -c 'import strategy_genome' 2>&1 | Out-Null }
        if ($LASTEXITCODE -ne 0) {
            Invoke-Native { & $python @pyPrefix -c 'import deap' 2>&1 | Out-Null }
            $depsOk = ($LASTEXITCODE -eq 0)
        }
    }
    if (-not $depsOk) {
        Write-Info 'Instalando dependencias de requirements.txt...'
        Invoke-Native { & $python @pyPrefix -m pip install -r requirements.txt }
        if ($LASTEXITCODE -ne 0) { throw 'Fallo pip install -r requirements.txt' }
    }
    Write-Ok 'Dependencias disponibles.'

    # 4) Panel de configuracion
    if (-not $SkipPanel) {
        Write-Info 'Abriendo panel de configuracion (guarde y cierre la ventana para continuar)...'
        Invoke-Native { & $python @pyPrefix config_panel.py }
    }
    if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'risk_config.json'))) {
        throw 'No existe risk_config.json. Pulse "Guardar configuracion" en el panel.'
    }

    # 5) Bot con log dual (consola + archivo)
    $botArgs = @('trading_bot.py')
    if ($DryRun) { $botArgs += '--dry-run' }
    Write-Info "Ejecutando: $python $($pyPrefix + $botArgs -join ' ')"
    Write-Info "Log: $logFile  (Ctrl+C para detener)"
    Invoke-Native {
        & $python @pyPrefix @botArgs 2>&1 | ForEach-Object { "$_" } | Tee-Object -FilePath $logFile
    }
    $exitCode = $LASTEXITCODE

    switch ($exitCode) {
        0       { Write-Ok    'Bot finalizado correctamente.' }
        2       { Write-Err   'Configuracion invalida o cuenta no DEMO (codigo 2).' }
        3       { Write-Err   'No se pudo conectar con MetaTrader 5 (codigo 3).' }
        4       { Write-Warn  'Parametros descartados por la validacion walk-forward (codigo 4).' }
        default { Write-Err   "El bot termino con codigo $exitCode. Revise $logFile" }
    }
}
catch {
    Write-Err $_.Exception.Message
    $exitCode = 1
}
finally {
    Write-Host ''
    Write-Host 'Pulse Enter para cerrar...' -ForegroundColor DarkGray
    [void](Read-Host)
}
exit $exitCode
