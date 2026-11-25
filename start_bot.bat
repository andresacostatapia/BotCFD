@echo off
rem ==========================================================================
rem  start_bot.bat - Lanzador del MT5 Bot (Windows)
rem  1) Verifica Python 3.10+ (ofrece instalarlo con winget si falta)
rem  2) Verifica pip y cada libreria; instala automaticamente las que falten
rem  3) Verifica/abre MetaTrader 5   4) Abre el panel de configuracion
rem  5) Lanza trading_bot.py con salida a logs\bot_YYYYMMDD_HHMMSS.log
rem
rem  Uso:
rem    start_bot.bat                  flujo completo
rem    start_bot.bat --dry-run        argumentos extra se pasan al bot
rem    start_bot.bat --install-only   solo verifica/instala dependencias
rem  Ruta de MT5 personalizada:  set MT5_PATH=D:\MT5\terminal64.exe
rem ==========================================================================
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUNBUFFERED=1"
set "PIP_DISABLE_PIP_VERSION_CHECK=1"
if not defined MT5_PATH set "MT5_PATH=C:\Program Files\MetaTrader 5\terminal64.exe"
if not exist "logs" mkdir "logs"
set "RC=0"
set "INSTALL_ONLY=0"
if /I "%~1"=="--install-only" set "INSTALL_ONLY=1"

echo ==========================================================
echo   MT5 Bot - lanzador  (framework educativo, usar en DEMO)
echo ==========================================================

rem ------------------------------------------------------------- 1) Python
set "PY=python"
%PY% --version >nul 2>&1
if not errorlevel 1 goto :py_found
set "PY=py -3"
%PY% --version >nul 2>&1
if errorlevel 1 goto :no_python
:py_found
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 goto :old_python
for /f "delims=" %%v in ('%PY% --version 2^>^&1') do echo [OK] %%v detectado.

rem ------------------------------------------------- 2) pip + dependencias
rem  Las versiones minimas deben coincidir con requirements.txt
echo [INFO] Verificando dependencias...
%PY% -m pip --version >nul 2>&1
if not errorlevel 1 goto :pip_ok
echo [INFO] pip no esta disponible. Instalandolo con ensurepip...
%PY% -m ensurepip --upgrade
%PY% -m pip --version >nul 2>&1
if errorlevel 1 goto :no_pip
:pip_ok
echo [OK] pip disponible.

set "DEPS_ERR=0"
call :ensure_module MetaTrader5 "MetaTrader5>=5.0.45" || set "DEPS_ERR=1"
call :ensure_module pandas "pandas>=2.0" || set "DEPS_ERR=1"
call :ensure_module numpy "numpy>=1.24" || set "DEPS_ERR=1"

rem  Motor genetico: basta con uno (strategy-genome principal, deap alternativo)
set "GA_OK=0"
call :ensure_module strategy_genome "strategy-genome>=0.1.0" && set "GA_OK=1"
call :ensure_module deap "deap>=1.4" && set "GA_OK=1"
if "%GA_OK%"=="0" echo [ERROR] No hay ningun motor genetico disponible (strategy-genome o deap).
if "%GA_OK%"=="0" set "DEPS_ERR=1"

rem  tkinter viene con Python; no se puede instalar con pip
%PY% -c "import tkinter" >nul 2>&1
if not errorlevel 1 goto :tk_ok
echo [ERROR] tkinter no esta disponible. Reinstale/modifique Python desde python.org
echo         marcando la opcion "tcl/tk and IDLE".
set "DEPS_ERR=1"
goto :tk_done
:tk_ok
echo [OK] tkinter ya instalado.
:tk_done

if "%DEPS_ERR%"=="1" goto :deps_fail
echo [OK] Todas las dependencias estan listas.
if "%INSTALL_ONLY%"=="1" goto :end

rem ---------------------------------------------------------------- 3) MT5
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
if "%RC%"=="2" echo [ERROR] Configuracion invalida, simbolo inexistente o cuenta no DEMO.
if "%RC%"=="2" echo         Busque la linea [ERROR] en "%LOGFILE%" (incluye sugerencias de simbolo).
if "%RC%"=="3" echo [ERROR] No se pudo conectar con MetaTrader 5.
if "%RC%"=="4" echo [ERROR] Los parametros evolucionados no superaron la validacion walk-forward.
goto :end

rem ------------------------------------------------------------- errores
:no_python
set "RC=1"
echo [ERROR] Python no esta instalado o no esta en el PATH.
where winget >nul 2>&1
if errorlevel 1 goto :no_python_manual
choice /C SN /M "Desea instalar Python 3.12 automaticamente con winget"
if errorlevel 2 goto :no_python_manual
winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
if errorlevel 1 goto :no_python_manual
echo [OK] Python instalado. CIERRE esta ventana y vuelva a ejecutar start_bot.bat
echo      (el PATH solo se actualiza en una consola nueva).
goto :end
:no_python_manual
echo         Instale Python 3.10+ desde https://www.python.org/downloads/
echo         marcando "Add python.exe to PATH" y vuelva a ejecutar este archivo.
goto :end

:old_python
set "RC=1"
for /f "delims=" %%v in ('%PY% --version 2^>^&1') do echo [ERROR] %%v es demasiado antiguo: se requiere Python 3.10 o superior.
goto :end

:no_pip
set "RC=1"
echo [ERROR] No se pudo instalar pip. Reinstale Python desde python.org con la opcion "pip".
goto :end

:deps_fail
set "RC=1"
echo [ERROR] Faltan dependencias que no se pudieron instalar automaticamente.
echo         Revise la conexion a internet / proxy y pruebe manualmente:
echo         %PY% -m pip install -r requirements.txt
echo         Nota: MetaTrader5 solo existe para Windows y versiones de Python soportadas.
goto :end

:mt5_missing
set "RC=1"
echo [ERROR] No se encontro/abrio MetaTrader 5 en "%MT5_PATH%".
echo         Abra MT5 manualmente o defina la variable MT5_PATH.
goto :end

:no_config
set "RC=2"
echo [ERROR] No existe risk_config.json. Pulse "Guardar configuracion" en el panel.
goto :end

:end
echo.
pause
endlocal & exit /b %RC%

rem ==========================================================================
rem  :ensure_module <modulo_import> "<especificacion_pip>"
rem  Comprueba si el modulo se puede importar; si no, lo instala con pip
rem  (si falla, reintenta con --user) y vuelve a comprobarlo.
rem  Devuelve errorlevel 0 si el modulo queda disponible, 1 si no.
rem ==========================================================================
:ensure_module
%PY% -c "import %~1" >nul 2>&1
if errorlevel 1 goto :ensure_install
echo [OK] %~1 ya instalado.
exit /b 0
:ensure_install
echo [INFO] %~1 no esta instalado. Instalando "%~2"...
%PY% -m pip install "%~2"
if not errorlevel 1 goto :ensure_verify
echo [WARN] La instalacion fallo. Reintentando con --user...
%PY% -m pip install --user "%~2"
:ensure_verify
%PY% -c "import %~1" >nul 2>&1
if errorlevel 1 goto :ensure_fail
echo [OK] %~1 instalado correctamente.
exit /b 0
:ensure_fail
echo [ERROR] No se pudo instalar %~1.
exit /b 1
