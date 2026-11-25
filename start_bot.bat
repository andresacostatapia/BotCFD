@echo off
rem ==========================================================================
rem  start_bot.bat - Lanzador del MT5 Bot (Windows)
rem  1) Verifica/abre MetaTrader 5   2) Verifica Python
rem  3) Instala dependencias si faltan   4) Abre el panel de configuracion
rem  5) Lanza trading_bot.py con salida a logs\bot_YYYYMMDD_HHMMSS.log
rem  Argumentos extra se pasan al bot, p. ej.:  start_bot.bat --dry-run
rem  Ruta de MT5 personalizada:  set MT5_PATH=D:\MT5\terminal64.exe
rem ==========================================================================
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUNBUFFERED=1"
if not defined MT5_PATH set "MT5_PATH=C:\Program Files\MetaTrader 5\terminal64.exe"
if not exist "logs" mkdir "logs"

echo ==========================================================
echo   MT5 Bot - lanzador  (framework educativo, usar en DEMO)
echo ==========================================================

rem ---------------------------------------------------------------- 1) MT5
tasklist /FI "IMAGENAME eq terminal64.exe" 2>nul | find /I "terminal64.exe" >nul
if not errorlevel 1 goto :mt5_ok
echo [INFO] MetaTrader 5 no esta en ejecucion. Intentando abrirlo...
if not exist "%MT5_PATH%" goto :mt5_missing
start "" "%MT5_PATH%"
echo [INFO] Esperando 20 segundos a que MT5 inicie y conecte...
timeout /t 20 /nobreak >nul
tasklist /FI "IMAGENAME eq terminal64.exe" 2>nul | find /I "terminal64.exe" >nul
if errorlevel 1 goto :mt5_missing
:mt5_ok
echo [OK] MetaTrader 5 en ejecucion.

rem ------------------------------------------------------------- 2) Python
set "PY=python"
%PY% --version >nul 2>&1
if not errorlevel 1 goto :py_ok
set "PY=py -3"
%PY% --version >nul 2>&1
if errorlevel 1 goto :no_python
:py_ok
for /f "delims=" %%v in ('%PY% --version 2^>^&1') do echo [OK] %%v detectado.

rem ------------------------------------------------------- 3) Dependencias
%PY% -c "import MetaTrader5, pandas, numpy" >nul 2>&1
if not errorlevel 1 goto :deps_ok
echo [INFO] Faltan dependencias. Instalando requirements.txt...
%PY% -m pip install -r requirements.txt
if errorlevel 1 goto :deps_fail
:deps_ok
%PY% -c "import strategy_genome" >nul 2>&1 || %PY% -c "import deap" >nul 2>&1 || %PY% -m pip install -r requirements.txt
echo [OK] Dependencias instaladas.

rem ------------------------------------------------------ 4) Panel config
echo [INFO] Abriendo panel de configuracion. Guarde y cierre la ventana para continuar.
%PY% config_panel.py
if not exist "risk_config.json" goto :no_config

rem ------------------------------------------------------------ 5) Bot
set "TS="
for /f "usebackq delims=" %%i in (`powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"`) do set "TS=%%i"
if not defined TS set "TS=%RANDOM%"
set "LOGFILE=logs\bot_%TS%.log"
echo [INFO] Iniciando trading_bot.py %*
echo [INFO] Salida redirigida a "%LOGFILE%"  (Ctrl+C para detener)
%PY% trading_bot.py %* > "%LOGFILE%" 2>&1
set "RC=%ERRORLEVEL%"
echo [INFO] trading_bot.py finalizo con codigo %RC%. Revise "%LOGFILE%".
if "%RC%"=="0" goto :end
if "%RC%"=="2" echo [ERROR] Problema de configuracion o cuenta no DEMO.
if "%RC%"=="3" echo [ERROR] No se pudo conectar con MetaTrader 5.
if "%RC%"=="4" echo [ERROR] Los parametros evolucionados no superaron la validacion walk-forward.
goto :end

:mt5_missing
echo [ERROR] No se encontro/abrio MetaTrader 5 en "%MT5_PATH%".
echo         Abra MT5 manualmente o defina la variable MT5_PATH.
goto :end

:no_python
echo [ERROR] Python no esta en el PATH. Instale Python 3.10+ desde python.org
echo         marcando "Add python.exe to PATH".
goto :end

:deps_fail
echo [ERROR] Fallo la instalacion de dependencias. Revise la conexion o pip.
goto :end

:no_config
echo [ERROR] No existe risk_config.json. Pulse "Guardar configuracion" en el panel.
goto :end

:end
echo.
pause
endlocal
