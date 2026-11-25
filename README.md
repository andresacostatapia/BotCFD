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
├── config_panel.py          # GUI tkinter para configurar riesgos -> risk_config.json
├── risk_manager.py          # RiskManager (sin dependencia de MT5, testeable)
├── mt5_connector.py         # Conexión MT5 con reintentos + verificación DEMO + órdenes
├── strategy_evolver.py      # Evolución genética + backtest + walk-forward (sin MT5)
├── trading_bot.py           # Orquestador: evoluciona -> valida -> opera
├── bot_common.py            # Rutas, configuración centralizada y logging rotativo
├── requirements.txt
├── requirements-dev.txt     # Para ejecutar los tests (sin MetaTrader5)
├── risk_config.example.json # Todas las claves con sus valores por defecto
├── start_bot.bat            # Lanzador CMD
├── launch.ps1               # Lanzador PowerShell con log dual
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
  sin posición (mismo magic) ─► SL/TP ATR (1.5× / 2.0×) ─► calculate_lot()
  ─► order_send (IOC, con fallback FOK/RETURN) ─► verifica retcode
```

### Motor genético: `strategy-genome` (con fallback a `deap`)

Se usa **`strategy-genome`** (PyPI, licencia MIT) como motor principal
(`strategy_genome.evolve`). Si no está instalado o falla, el evolucionador cambia
automáticamente a **`deap`**. Se puede forzar con `"evolver_backend": "deap"` en
`risk_config.json`. `strategy-genome` es una librería joven (v0.1.x); `deap` es la
alternativa madura.

### Estrategia (de ejemplo)

| Parámetro  | Rango       | Uso                                                    |
|------------|-------------|--------------------------------------------------------|
| `fast`     | Int(5, 30)  | SMA rápida                                             |
| `slow`     | Int(30, 100)| SMA lenta (debe ser `slow > fast`, si no fitness = 0)  |
| `rsi_buy`  | Float(40,70)| COMPRA si la SMA rápida cruza al alza y RSI(14) > valor |
| `rsi_sell` | Float(30,60)| VENTA si la SMA rápida cruza a la baja y RSI(14) < valor |

* **Fitness:** Profit Factor del backtest (restando `commission_per_trade` a cada
  operación), multiplicado por `(trades / min_trades)²` si hay menos de
  `min_trades` operaciones (penaliza las "curvas de 1 trade").
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
3. Copie el proyecto, por ejemplo en `C:\MT5Bot\`, y ejecute:
   ```bat
   cd C:\MT5Bot
   python -m pip install -r requirements.txt
   ```
   (`start_bot.bat` y `launch.ps1` instalan las dependencias si faltan).

---

## Configuración (`risk_config.json`)

La GUI edita las claves principales; las avanzadas se editan a mano (la GUI las
conserva al guardar). Valores por defecto en `risk_config.example.json`.

| Clave | Defecto | Descripción |
|---|---|---|
| `risk_percent` | 1.0 | % del equity arriesgado por operación |
| `max_daily_loss_percent` | 3.0 | % de pérdida diaria (sobre el balance de inicio del día, usando equity) que detiene el trading hasta el día siguiente |
| `max_lot` / `min_lot` | 1.0 / 0.01 | Límites de lote (además de los del bróker) |
| `max_spread` | 0.00020 | Spread máximo en unidades de precio (ask − bid) |
| `magic` | 20250101 | Identifica las órdenes del bot |
| `symbol` / `timeframe` | EURUSD / H1 | Símbolo (respete sufijos del bróker, p. ej. `EURUSD.m`) y M1/M5/M15/H1/H4 |
| `require_demo` | true | Aborta si la cuenta no es DEMO |
| `history_bars` | 2000 | Velas para evolución + validación |
| `population` / `generations` | 20 / 10 | Tamaño de la evolución |
| `evolver_backend` | auto | `auto`, `strategy-genome` o `deap` |
| `evolver_seed` | null | Semilla para reproducibilidad |
| `commission_per_trade` | 0.00007 | Coste fijo estimado por operación (precio) |
| `min_trades` / `min_trades_oos` | 20 / 5 | Mínimo de operaciones in-sample / out-of-sample |
| `train_ratio` | 0.70 | Proporción de entrenamiento walk-forward |
| `oos_pf_ratio_min` | 0.80 | PF_oos mínimo relativo a PF_is |
| `min_pf_in_sample` | 1.0 | PF in-sample mínimo |
| `atr_period`, `sl_atr_mult`, `tp_atr_mult` | 14, 1.5, 2.0 | SL/TP por ATR |
| `loop_seconds` | 60 | Frecuencia del bucle |
| `deviation` | 20 | Deslizamiento máximo en puntos |
| `close_positions_on_error` | false | Fail-safe: cerrar posiciones del bot ante un error no controlado |
| `terminal_path` | "" | Ruta a `terminal64.exe` si hay varias instalaciones |

---

## Instrucciones de uso

### Orden de ejecución

1. Abrir MT5, iniciar sesión en **DEMO**, activar **Algo Trading**.
2. `python config_panel.py` → ajustar valores → **Probar conexión MT5** →
   **Guardar configuración** → cerrar la ventana.
3. Primero en modo simulación: `python trading_bot.py --dry-run`.
4. Después, en DEMO: `python trading_bot.py`.

O todo en uno:

```bat
start_bot.bat                 :: panel -> bot, salida en logs\bot_YYYYMMDD_HHMMSS.log
start_bot.bat --dry-run       :: igual, sin enviar órdenes
```
```powershell
powershell -ExecutionPolicy Bypass -File .\launch.ps1            # consola + archivo
powershell -ExecutionPolicy Bypass -File .\launch.ps1 -DryRun -SkipPanel
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
4. **Una posición por símbolo y una sola estrategia de ejemplo** (cruce de SMA + RSI).
   → Cartera multisímbolo con control de correlación y riesgo agregado.
5. **El día de trading usa la hora local del PC** (reset a las 00:00 locales), no la
   hora del servidor del bróker. → Opción para usar la hora del servidor.
6. **La pérdida diaria sólo detiene nuevas entradas**; no cierra las posiciones
   abiertas al alcanzar el límite. → Opción `close_on_daily_limit`.
7. **Sin trailing stop ni break-even**, y sin gestión de noticias/horario de sesión.
8. **Persistencia mínima:** el estado (balance de inicio del día) se recalcula al
   reiniciar, por lo que reiniciar el bot a mitad del día reinicia el contador de
   pérdida diaria. → Guardar estado diario en disco.
9. **La GUI y los lanzadores sólo se han verificado de forma estática** (no hay
   Windows/MT5 en el entorno de desarrollo); la lógica sí está cubierta por tests
   con un MT5 simulado. → Probarlos en su equipo antes de confiar en ellos.

---

## Licencia

Uso educativo. Sin garantía de ningún tipo.
