# BotCFD — Bot de trading CFD para MetaTrader 5 (Python)

Framework **educativo** para operar CFD en MetaTrader 5 desde Python, con evolución
genética de parámetros, validación walk-forward y gestión de riesgo.

> ⚠️ **Aviso:** este código no promete ni garantiza rentabilidad. Los resultados de
> un backtest no predicen resultados futuros. Úselo **solo en cuenta DEMO** hasta
> entenderlo por completo. Operar CFD con apalancamiento puede causar pérdidas
> superiores al capital invertido.

---

## Arquitectura

```
C:\MT5Bot\
├── bot_gui.py               # CENTRO DE CONTROL: perfiles, configurar, lanzar, ver resultados
├── config_panel.py          # GUI tkinter simple para configurar riesgos -> risk_config.json
├── risk_manager.py          # RiskManager (sin dependencia de MT5, testeable)
├── mt5_connector.py         # Conexión MT5 con reintentos + verificación DEMO + órdenes
├── strategy_evolver.py      # Evolución genética + backtest + walk-forward (sin MT5)
├── trading_bot.py           # Orquestador: evoluciona -> valida -> opera
├── bot_common.py            # Rutas, configuración centralizada y logging rotativo
├── requirements.txt
├── requirements-dev.txt     # Para ejecutar los tests (sin MetaTrader5)
├── risk_config.example.json # Todas las claves con sus valores por defecto
├── start_bot.bat            # Lanzador: instala dependencias y abre el Centro de control
├── launch.ps1               # Lanzador PowerShell con log dual (instala dependencias)
├── tests\                   # pytest (incluye un MT5 falso para integración)
└── logs\                    # Logs rotativos (se crea automáticamente)
```

`bot_common.py` es un módulo de apoyo añadido a la arquitectura pedida para que
**todos** los módulos lean un único `risk_config.json` y compartan el mismo
formato de logging (`%(asctime)s [%(levelname)s] %(name)s: %(message)s`).

### Flujo

```
risk_config.json ─► connect_mt5_with_retry() ─► RiskManager.reset_daily()
      │                     (aborta si no es DEMO y require_demo=true)
      ▼
get_rates(history_bars) ─► evolve_parameters()  [70% train / 30% validación]
      │                     PF_oos < 0.8 × PF_is  ─► ABORTA (código 4)
      ▼
Bucle cada 60 s:
  recarga en caliente · reconexión · reset a las 00:00
  check_daily_loss() ─► check_spread() ─► señal en vela cerrada
  sin posición (mismo magic) ─► SL/TP ATR (evolucionados o 1.5× / 2.0×) ─► calculate_lot()
  ─► order_send (IOC, con fallback FOK/RETURN) ─► verifica retcode
```

### Motor genético: `strategy-genome` (con fallback a `deap`)

Se usa **`strategy-genome`** (PyPI, licencia MIT) como motor principal
(`strategy_genome.evolve`). Si no está instalado o falla, el evolucionador cambia
automáticamente a **`deap`**. Se puede forzar con `"evolver_backend": "deap"` en
`risk_config.json`. `strategy-genome` es una librería joven (v0.1.x); `deap` es la
alternativa madura.

### Estrategias (elegibles por perfil con `strategy`)

| Estrategia | Para qué plazo | Señal | Parámetros que evoluciona |
|---|---|---|---|
| `cruce_medias` (por defecto) | M15 / H1 (tendencia) | COMPRA si la SMA rápida cruza al alza la lenta y RSI(14) > `rsi_buy`; VENTA si cruza a la baja y RSI < `rsi_sell` | `fast` Int(5,30), `slow` Int(30,100) (`slow > fast`), `rsi_buy` Float(40,70), `rsi_sell` Float(30,60) |
| `reversion_media` | M5 / M15 (corto plazo) | COMPRA si el cierre cae **bajo la banda inferior** de Bollinger con RSI < `rsi_low`; VENTA si supera la **banda superior** con RSI > `rsi_high` | `bb_period` Int(10,50), `bb_std` Float(1.5,3), `rsi_low` Float(15,40), `rsi_high` Float(60,85) |
| `ruptura` | M5 / M15 / H1 | COMPRA si el cierre **supera el máximo** de las `lookback` velas anteriores estando sobre la SMA `trend`; VENTA si **cae bajo el mínimo** estando bajo la SMA | `lookback` Int(10,100), `trend` Int(20,200) |

Además, con `evolve_sl_tp` (por defecto) se evolucionan las salidas:

| Parámetro | Rango | Uso |
|---|---|---|
| `sl_mult` | Float(1, 3) | Stop loss = valor × ATR |
| `tp_mult` | Float(1, 4) | Take profit = valor × ATR |

Todas las estrategias pasan por el mismo backtest con costes y la misma validación
walk-forward. Ninguna tiene ventaja garantizada: pruébelas en demo y en varios
símbolos/timeframes, y deje que la validación descarte las que no funcionen.

* **Fitness:** Profit Factor del backtest (restando `commission_per_trade` y el
  spread histórico a cada operación), multiplicado por `(trades / min_trades)²` si
  hay menos de `min_trades` operaciones (penaliza las "curvas de 1 trade").
  Con `fitness_folds = 3` (por defecto) el entrenamiento se divide en 3 tramos y
  el fitness es el **peor** de ellos: una combinación solo puntúa alto si funciona
  en todo el periodo. Los datos de validación nunca se usan para elegir.
* **Backtest:** entrada a la apertura de la vela siguiente a la señal, SL/TP con ATR,
  si SL y TP se tocan en la misma vela se asume SL (conservador). El mismo
  modelo de SL/TP que usa el bot en vivo.
* **Walk-forward:** se evoluciona con el 70 % inicial y se valida el mejor
  individuo en el 30 % final. Se descarta si `PF_oos < 0.8 × PF_is`, si
  `PF_is < min_pf_in_sample` o si hay pocas operaciones. El resultado completo
  (parámetros + métricas IS/OOS) se guarda en `evolved_params.json`.

---

## Instalación

1. Instale **Python 3.10+** (64 bits) desde python.org marcando *Add python.exe to PATH*.
   Compruebe que la versión de Python está soportada por el paquete `MetaTrader5`
   que instale (las versiones muy nuevas de Python pueden tardar en estar soportadas).
2. Instale **MetaTrader 5**, inicie sesión en una **cuenta DEMO** y active el botón
   **Algo Trading** (y en *Herramientas → Opciones → Asesores Expertos*, permita el
   trading algorítmico).
3. Copie el proyecto, por ejemplo en `C:\MT5Bot\`. Las dependencias se instalan
   **automáticamente** la primera vez que ejecuta `start_bot.bat`. Para solo
   instalar/verificar, sin abrir el panel ni el bot:
   ```bat
   cd C:\MT5Bot
   start_bot.bat --install-only
   ```
   O manualmente: `python -m pip install -r requirements.txt`.

### Instalación automática (`start_bot.bat` y `launch.ps1`)

| Paso | Qué comprueba | Si falta |
|---|---|---|
| Python | `python` o `py -3` en el PATH, versión ≥ 3.10 | Ofrece instalar Python 3.12 con `winget` (pregunta S/N); si no hay winget, indica cómo instalarlo |
| pip | `python -m pip --version` | Lo instala con `python -m ensurepip --upgrade` |
| `MetaTrader5`, `pandas`, `numpy` | Cada una por separado (`import ...`) | Instala solo la que falta con `pip install`; si falla, reintenta con `--user` |
| `strategy-genome` / `deap` | Cada una por separado | Las instala; basta con que quede **una** disponible |
| `tkinter` | `import tkinter` | No se instala con pip: indica reinstalar Python con la opción *tcl/tk and IDLE* |

Ambos lanzadores hacen las mismas comprobaciones. `launch.ps1` además recarga el
PATH tras instalar Python con winget y continúa sin tener que reabrir la consola.

Al final vuelve a importar cada librería para confirmar que la instalación
funcionó; si algo sigue faltando, se detiene con un mensaje y el comando manual.
Las versiones mínimas del `.bat` y del `.ps1` coinciden con `requirements.txt`
(si cambia una, cambie las otras).

---

## Configuración (`risk_config.json`)

La GUI edita las claves principales; las avanzadas se editan a mano (la GUI las
conserva al guardar). Valores por defecto en `risk_config.example.json`.

| Clave | Defecto | Descripción |
|---|---|---|
| `risk_percent` | 1.0 | % del equity arriesgado por operación |
| `max_daily_loss_percent` | 3.0 | % de pérdida diaria (sobre el balance de inicio del día) que detiene el trading hasta el día siguiente |
| `daily_loss_scope` | bots | Qué mide la pérdida diaria. **`bots`**: solo el resultado de hoy de las operaciones de los bots (cerradas + abiertas, por magic number); sus operaciones manuales no los bloquean. **`cuenta`**: equity de toda la cuenta, incluidas las operaciones manuales (más conservador) |
| `max_lot` / `min_lot` | 1.0 / 0.01 | Límites de lote (además de los del bróker) |
| `max_spread` | 0.00020 | Spread máximo en unidades de precio (ask − bid). **Depende del activo:** ~0.0002 en EURUSD, ~1.0 en US500. "Probar conexión MT5" muestra el spread actual |
| `magic` | 20250101 | Identifica las órdenes del bot |
| `symbol` / `timeframe` | EURUSD / H1 | Símbolo con el nombre **exacto** del bróker (distingue mayúsculas: `US500`, no `us500`; respete sufijos como `EURUSD.m`). Si no existe, el panel y el log sugieren nombres parecidos |
| `require_demo` | true | Aborta si la cuenta no es DEMO |
| `history_bars` | 2000 | Velas para evolución + validación |
| `population` / `generations` | 20 / 10 | Tamaño de la evolución |
| `evolver_backend` | auto | `auto`, `strategy-genome` o `deap` |
| `evolver_seed` | null | Semilla para reproducibilidad |
| `commission_per_trade` | 0.00007 | Comisión fija estimada por operación (precio). El backtest suma además el spread histórico de cada vela |
| `min_trades` / `min_trades_oos` | 20 / 5 | Mínimo de operaciones in-sample / out-of-sample |
| `fitness_folds` | 3 | Divide el entrenamiento en N tramos y puntúa cada combinación por su **peor** tramo (premia la estabilidad, reduce el sobreajuste). `1` = PF de todo el entrenamiento |
| `train_ratio` | 0.70 | Proporción de entrenamiento walk-forward |
| `oos_pf_ratio_min` | 0.80 | PF_oos mínimo relativo a PF_is |
| `min_pf_in_sample` | 1.0 | PF in-sample mínimo |
| `atr_period`, `sl_atr_mult`, `tp_atr_mult` | 14, 1.5, 2.0 | SL/TP por ATR (fijos, se usan solo si `evolve_sl_tp` = false) |
| `strategy` | cruce_medias | `cruce_medias`, `reversion_media` o `ruptura` (ver tabla de estrategias) |
| `evolve_sl_tp` | true | El algoritmo genético elige también los multiplicadores de SL y TP. Más libertad = más riesgo de sobreajuste; la validación walk-forward lo controla |
| `loop_seconds` | 60 | Frecuencia del bucle |
| `heartbeat_seconds` | 60 | Cada cuánto se muestra el mensaje "En espera" (precio, próxima vela, qué condición falta, P&L del día). `0` = desactivado |
| `deviation` | 20 | Deslizamiento máximo en puntos |
| `close_positions_on_error` | false | Fail-safe: cerrar posiciones del bot ante un error no controlado |
| `max_open_positions_total` | 3 | Máximo de posiciones abiertas sumando **todos los perfiles** (0 = sin límite) |
| `terminal_path` | "" | Ruta a `terminal64.exe` si hay varias instalaciones |

---

## Centro de control (recomendado)

Doble clic en **`start_bot.bat`**: instala lo que falte, abre MetaTrader 5 si no
está abierto y muestra la ventana **MT5 Bot — Centro de control**
(`python bot_gui.py` hace lo mismo sin el instalador).

| Pestaña | Qué permite |
|---|---|
| **Panel** | Iniciar / detener el bot del perfil seleccionado, modo simulación, estado, cuenta (equity y resultado del día), mercado (bid/ask/spread), estrategia evolucionada, posición abierta, última vela y **log en vivo** con colores |
| **Todos los símbolos** | Tabla con todos los perfiles a la vez: estado, posición, resultado y estrategia de cada uno. **Iniciar todos / Detener todos**. Doble clic en una fila abre su panel |
| **Configuración** | Todos los parámetros (incluidos los avanzados) agrupados, con explicación al pasar el ratón, validación, **Probar conexión MT5** (comprueba símbolo y spread) y valores por defecto |
| **Resultados** | Última evolución (aceptada/descartada y por qué), parámetros elegidos, métricas de entrenamiento vs. validación, tramos, **historial de evoluciones** y **operaciones cerradas** del bot con su resultado |

### Perfiles y varios símbolos a la vez

Cada **perfil** es un archivo `risk_config_<nombre>.json` (el principal es
`risk_config.json`) con su propio símbolo, timeframe y **magic number**, su log
(`logs/trading_bot_<nombre>.log`), su resultado de evolución
(`evolved_params_<nombre>.json`) y su propio proceso del bot. Varios perfiles
pueden funcionar **simultáneamente**.

* **Buscar símbolos…**: abre un buscador con **todos los símbolos del bróker**
  (nombre, descripción, categoría, spread actual y lote mínimo). Búsqueda en vivo
  en inglés o español (`eur`, `jpy`, `oro`, `nasdaq`, `dax`, `bitcoin`…) y filtro
  por categoría. Seleccione varios (Ctrl/Mayús + clic), elija timeframe y
  estrategia, y se crea un perfil por símbolo copiando la configuración actual,
  con magic único y **spread máximo** ajustado a ~2,5× el spread actual de cada uno.
* **Buscar…** (junto al campo Símbolo, en Configuración): elige el símbolo del
  perfil actual desde la misma lista y ofrece ajustar su spread máximo.
* **Escribir símbolos…** (pestaña Todos los símbolos): alternativa escribiendo
  `GBPUSD, USDJPY, US500`; comprueba en MT5 que existan (corrige `us500` → `US500`).
* **Nuevo perfil…**: copia del perfil actual, por ejemplo para otro timeframe.
* **Riesgo combinado:** cada perfil arriesga su `% por operación`. Dos límites se
  aplican a **todos los perfiles juntos**: `Pérdida diaria máxima` (por defecto
  sobre el resultado de todos los bots; si uno la alcanza, todos se detienen hasta
  el día siguiente) y `Máx. posiciones (todos)` = `max_open_positions_total` (por
  defecto 3): un bot no abre si ya hay ese número de posiciones abiertas entre
  todos los perfiles. Recuerde que pares como EURUSD y GBPUSD suelen moverse juntos.
* **Inicio del día guardado:** el balance de inicio del día se guarda en
  `logs/day_state.json` (por cuenta). Reiniciar un bot o la ventana no pone a
  cero el contador de pérdida diaria.
* **Detener:** la ventana crea `logs/stop_<perfil>.flag` y el bot termina de forma
  ordenada en ~1 s (las posiciones abiertas se mantienen con su SL/TP). Al cerrar
  la ventana se detienen todos los bots que lanzó.

## Instrucciones de uso

### Orden de ejecución

**Con el Centro de control:** `start_bot.bat` → pestaña **Configuración** →
**Probar conexión MT5** → **Guardar** → pestaña **Panel** → marque **Modo
simulación** → **Iniciar bot**. Cuando todo se vea bien, desmarque la simulación.

**Por consola** (modo anterior):

1. Abrir MT5, iniciar sesión en **DEMO**, activar **Algo Trading**.
2. `python config_panel.py` → ajustar valores → **Probar conexión MT5** →
   **Guardar configuración** → cerrar la ventana.
3. Primero en modo simulación: `python trading_bot.py --dry-run`.
4. Después, en DEMO: `python trading_bot.py`.

O todo en uno:

```bat
start_bot.bat                          :: Centro de control (recomendado)
start_bot.bat --install-only           :: solo verifica/instala dependencias
start_bot.bat --console                :: panel simple -> bot, log en logs\bot_*.log
start_bot.bat --console --dry-run      :: igual, sin enviar órdenes
python trading_bot.py --config risk_config_m5.json   :: un perfil concreto
```
```powershell
powershell -ExecutionPolicy Bypass -File .\launch.ps1            # consola + archivo
powershell -ExecutionPolicy Bypass -File .\launch.ps1 -DryRun -SkipPanel
powershell -ExecutionPolicy Bypass -File .\launch.ps1 -InstallOnly   # solo dependencias
```
Si MT5 no está en `C:\Program Files\MetaTrader 5\terminal64.exe`, defina la
variable `MT5_PATH` o use `-Mt5Path`.

### Qué verificar antes de operar

- [ ] La cuenta es **DEMO** (el bot lo comprueba con `require_demo=true`).
- [ ] **Algo Trading** activado (el bot avisa si está desactivado).
- [ ] El símbolo existe con el nombre exacto del bróker y es visible en *Observación del mercado*.
- [ ] El spread típico del símbolo es menor que `max_spread` (si no, nunca operará).
- [ ] El lote mínimo del bróker y su valor de tick son compatibles con su riesgo
      (si el lote mínimo arriesga > 1,5× el presupuesto, la operación se rechaza).
- [ ] `evolved_params.json` muestra métricas IS/OOS razonables y suficientes operaciones.
- [ ] Los logs en `logs\` no muestran errores ni rechazos inesperados.

### Cómo probar en demo

1. Ejecute `python trading_bot.py --dry-run` durante varias velas: verá en el log
   la señal, el lote calculado y la orden que **se habría** enviado.
2. Ejecute sin `--dry-run` en la cuenta DEMO y compare las órdenes de la pestaña
   *Operaciones* de MT5 (comentario `MT5Bot evo`, magic configurado) con el log.
3. Pruebe los límites: baje `max_daily_loss_percent` o `max_spread` en el panel
   y guarde con el bot en marcha: la **recarga en caliente** los aplica sin reiniciar.
4. Ejecute los tests (no requieren MT5):
   ```bash
   python -m pip install -r requirements-dev.txt
   python -m pytest -q
   ```

### Cómo detener el bot limpiamente

- Pulse **Ctrl+C** en la consola: el bot captura `KeyboardInterrupt`, registra la
  parada y llama a `safe_shutdown()`. Las posiciones abiertas **se mantienen**
  con su SL/TP en el servidor del bróker.
- Para cerrarlas, hágalo manualmente en MT5 (filtre por el magic number).
- Ante un error no controlado, el bot registra el traceback completo, cierra
  las posiciones del bot si `close_positions_on_error=true` y desconecta.

Códigos de salida: `0` parada limpia · `1` error inesperado · `2` configuración/cuenta
no DEMO · `3` conexión MT5 · `4` validación walk-forward no superada.

---

## Limitaciones conocidas y próximos pasos

1. **Backtest simplificado:** usa velas OHLC (no ticks), comisión fija y no modela
   spread variable, swaps, deslizamiento ni gaps de fin de semana. Si SL y TP caen en
   la misma vela se asume SL. → Usar el spread histórico de `copy_rates` y swaps
   del símbolo; validar en el Probador de Estrategias de MT5.
2. **Un único split walk-forward (70/30):** sigue habiendo riesgo de
   sobreajuste y el resultado depende del periodo elegido. → Walk-forward
   *anclado/rodante* con varias ventanas y re-evolución periódica (p. ej. semanal).
3. **Métrica única (Profit Factor):** ignora drawdown, duración y estabilidad.
   → Optimización multiobjetivo (PF, drawdown, nº de operaciones) con NSGA-II,
   que `strategy-genome` ya soporta.
4. **Una sola estrategia de ejemplo** (cruce de SMA + RSI) y una posición por perfil.
   Varios símbolos funcionan en paralelo (un perfil cada uno) con límite global de
   posiciones, pero sin control de correlación entre ellos. → Estrategias de corto plazo
   (reversión a la media, ruptura) y control de exposición por divisa.
5. **El día de trading usa la hora local del PC** (reset a las 00:00 locales), no la
   hora del servidor del bróker. → Opción para usar la hora del servidor.
6. **La pérdida diaria sólo detiene nuevas entradas**; no cierra las posiciones
   abiertas al alcanzar el límite. → Opción `close_on_daily_limit`.
7. **Sin trailing stop ni break-even**, y sin gestión de noticias/horario de sesión.
8. **Con `daily_loss_scope = bots` el riesgo de sus operaciones manuales no lo
   controla el bot**: es responsabilidad suya (use `cuenta` si quiere que también
   cuenten). El inicio del día se guarda en disco, pero el "día" empieza a las 00:00
   de su PC, no del servidor del bróker.
9. **El Centro de control se ha probado en Linux con un MT5 simulado** (arranque,
   log en vivo, parada, varios perfiles a la vez); en Windows con MT5 real falta
   la prueba del usuario. Los lanzadores `.bat`/`.ps1` sólo se han revisado de forma estática (no hay
   Windows/MT5 en el entorno de desarrollo); la lógica sí está cubierta por tests
   con un MT5 simulado. → Probarlos en su equipo antes de confiar en ellos.

---

## Licencia

Uso educativo. Sin garantía de ningún tipo.
