<#
.SYNOPSIS
    Lanzador PowerShell del MT5 Bot con log dual (consola + archivo).

.DESCRIPTION
    Equivalente a start_bot.bat:
      1) Verifica Python 3.10+ (ofrece instalarlo con winget si falta)
      2) Verifica pip y cada libreria; instala automaticamente las que falten
      3) Verifica/abre MetaTrader 5
      4) Abre el panel de configuracion
      5) Ejecuta trading_bot.py duplicando la salida en logs\bot_YYYYMMDD_HHMMSS.log

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\launch.ps1
    powershell -ExecutionPolicy Bypass -File .\launch.ps1 -DryRun -SkipPanel
    powershell -ExecutionPolicy Bypass -File .\launch.ps1 -InstallOnly
#>
[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$SkipPanel,
    [switch]$InstallOnly,
    [string]$Mt5Path = $(if ($env:MT5_PATH) { $env:MT5_PATH } else { 'C:\Program Files\MetaTrader 5\terminal64.exe' }),
    [int]$Mt5StartupSeconds = 20
)

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUNBUFFERED = '1'
$env:PIP_DISABLE_PIP_VERSION_CHECK = '1'

# Librerias requeridas: import -> especificacion pip (debe coincidir con requirements.txt)
$RequiredModules = [ordered]@{
    'MetaTrader5' = 'MetaTrader5>=5.0.45'
    'pandas'      = 'pandas>=2.0'
    'numpy'       = 'numpy>=1.24'
}
# Motores geneticos: basta con que uno quede disponible
$GeneticModules = [ordered]@{
    'strategy_genome' = 'strategy-genome>=0.1.0'
    'deap'            = 'deap>=1.4'
}

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

# Recarga el PATH desde el registro (tras instalar Python en esta misma sesion).
function Update-SessionPath {
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = "$machine;$user"
}

# Busca un Python 3.10+ y fija $script:Python / $script:PyPrefix. Devuelve $true si lo encuentra.
function Find-Python {
    $candidates = @(
        @{ Exe = 'python'; Prefix = @() },
        @{ Exe = 'py'; Prefix = @('-3') }
    )
    foreach ($dir in @("$env:LOCALAPPDATA\Programs\Python\Python312", "$env:ProgramFiles\Python312")) {
        $candidates += @{ Exe = (Join-Path $dir 'python.exe'); Prefix = @() }
    }
    foreach ($c in $candidates) {
        $exe = $c.Exe
        $prefix = $c.Prefix
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        Invoke-Native { & $exe @prefix -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 3)' 2>&1 | Out-Null }
        if ($LASTEXITCODE -eq 0) {
            $script:Python = $exe
            $script:PyPrefix = $prefix
            return $true
        }
        if ($LASTEXITCODE -eq 3) {
            $old = Invoke-Native { & $exe @prefix --version 2>&1 }
            Write-Warn "$old es demasiado antiguo (se requiere 3.10+); se ignora."
        }
    }
    return $false
}

# Comprueba si un modulo de Python se puede importar.
function Test-PyImport([string]$Module) {
    Invoke-Native { & $script:Python @script:PyPrefix -c "import $Module" 2>&1 | Out-Null }
    return ($LASTEXITCODE -eq 0)
}

# Instala un modulo si falta (reintenta con --user) y verifica. Devuelve $true si queda disponible.
function Install-PyModule([string]$Module, [string]$Spec) {
    if (Test-PyImport $Module) { Write-Ok "$Module ya instalado."; return $true }
    Write-Info "$Module no esta instalado. Instalando '$Spec'..."
    Invoke-Native { & $script:Python @script:PyPrefix -m pip install $Spec 2>&1 | Out-Host }
    if ($LASTEXITCODE -ne 0) {
        Write-Warn 'La instalacion fallo. Reintentando con --user...'
        Invoke-Native { & $script:Python @script:PyPrefix -m pip install --user $Spec 2>&1 | Out-Host }
    }
    if (Test-PyImport $Module) { Write-Ok "$Module instalado correctamente."; return $true }
    Write-Err "No se pudo instalar $Module."
    return $false
}

$exitCode = 1
try {
    Write-Host '==========================================================' -ForegroundColor DarkCyan
    Write-Host '  MT5 Bot - launch.ps1  (framework educativo, usar en DEMO)' -ForegroundColor DarkCyan
    Write-Host '==========================================================' -ForegroundColor DarkCyan

    $logDir = Join-Path $PSScriptRoot 'logs'
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    $logFile = Join-Path $logDir ("bot_{0}.log" -f (Get-Date -Format 'yyyyMMdd_HHmmss'))

    # 1) Python 3.10+
    $script:Python = $null
    $script:PyPrefix = @()
    if (-not (Find-Python)) {
        Write-Warn 'Python 3.10+ no esta instalado o no esta en el PATH.'
        if (-not (Get-Command 'winget' -ErrorAction SilentlyContinue)) {
            throw 'Instale Python 3.10+ desde https://www.python.org/downloads/ marcando "Add python.exe to PATH".'
        }
        $answer = Read-Host 'Desea instalar Python 3.12 automaticamente con winget? (S/N)'
        if ($answer -notmatch '^[sSyY]') {
            throw 'Instalacion de Python cancelada. Instalelo desde https://www.python.org/downloads/'
        }
        Invoke-Native { & winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements }
        if ($LASTEXITCODE -ne 0) { throw "winget no pudo instalar Python (codigo $LASTEXITCODE)." }
        Update-SessionPath
        if (-not (Find-Python)) {
            throw 'Python se instalo pero no se encuentra todavia. Cierre esta ventana y vuelva a ejecutar launch.ps1.'
        }
    }
    $version = Invoke-Native { & $script:Python @script:PyPrefix --version 2>&1 }
    Write-Ok "$version detectado ($script:Python)."

    # 2) pip + dependencias
    Write-Info 'Verificando dependencias...'
    Invoke-Native { & $script:Python @script:PyPrefix -m pip --version 2>&1 | Out-Null }
    if ($LASTEXITCODE -ne 0) {
        Write-Info 'pip no esta disponible. Instalandolo con ensurepip...'
        Invoke-Native { & $script:Python @script:PyPrefix -m ensurepip --upgrade }
        Invoke-Native { & $script:Python @script:PyPrefix -m pip --version 2>&1 | Out-Null }
        if ($LASTEXITCODE -ne 0) { throw 'No se pudo instalar pip. Reinstale Python con la opcion "pip".' }
    }
    Write-Ok 'pip disponible.'

    $missing = @()
    foreach ($entry in $RequiredModules.GetEnumerator()) {
        if (-not (Install-PyModule $entry.Key $entry.Value)) { $missing += $entry.Key }
    }
    $geneticOk = $false
    foreach ($entry in $GeneticModules.GetEnumerator()) {
        if (Install-PyModule $entry.Key $entry.Value) { $geneticOk = $true }
    }
    if (-not $geneticOk) {
        Write-Err 'No hay ningun motor genetico disponible (strategy-genome o deap).'
        $missing += 'strategy-genome/deap'
    }
    if (Test-PyImport 'tkinter') {
        Write-Ok 'tkinter ya instalado.'
    } else {
        Write-Err 'tkinter no esta disponible: modifique/reinstale Python marcando "tcl/tk and IDLE".'
        $missing += 'tkinter'
    }
    if ($missing.Count -gt 0) {
        $msg = 'Faltan dependencias: ' + ($missing -join ', ') +
               ". Pruebe manualmente: $script:Python -m pip install -r requirements.txt" +
               ' (MetaTrader5 solo existe para Windows y versiones de Python soportadas).'
        throw $msg
    }
    Write-Ok 'Todas las dependencias estan listas.'

    if ($InstallOnly) {
        Write-Ok 'Modo -InstallOnly: no se abre MT5, ni el panel, ni el bot.'
        $exitCode = 0
    } else {
        # 3) MetaTrader 5
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

        # 4) Panel de configuracion
        if (-not $SkipPanel) {
            Write-Info 'Abriendo panel de configuracion (guarde y cierre la ventana para continuar)...'
            Invoke-Native { & $script:Python @script:PyPrefix config_panel.py }
        }
        if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'risk_config.json'))) {
            $exitCode = 2
            throw 'No existe risk_config.json. Pulse "Guardar configuracion" en el panel.'
        }

        # 5) Bot con log dual (consola + archivo)
        $botArgs = @('trading_bot.py')
        if ($DryRun) { $botArgs += '--dry-run' }
        Write-Info "Ejecutando: $script:Python $(($script:PyPrefix + $botArgs) -join ' ')"
        Write-Info "Log: $logFile  (Ctrl+C para detener)"
        Invoke-Native {
            & $script:Python @script:PyPrefix @botArgs 2>&1 | ForEach-Object { "$_" } | Tee-Object -FilePath $logFile
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
}
catch {
    Write-Err $_.Exception.Message
    if ($exitCode -eq 0) { $exitCode = 1 }
}
finally {
    Write-Host ''
    Write-Host 'Pulse Enter para cerrar...' -ForegroundColor DarkGray
    [void](Read-Host)
}
exit $exitCode
