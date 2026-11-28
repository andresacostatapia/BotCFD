# BotCFD — Bot de trading CFD para MetaTrader 5 (Python)

Framework **educativo** para operar CFD en MetaTrader 5 desde Python: varias
estrategias, evolución genética de parámetros, validación walk-forward, varios
símbolos a la vez, gestión de riesgo y un **Centro de control** gráfico.

> ⚠️ **Aviso:** este código no promete ni garantiza rentabilidad. Los resultados de
> un backtest no predicen resultados futuros. Úselo **solo en cuenta DEMO** hasta
> entenderlo por completo. Operar CFD con apalancamiento puede causar pérdidas
> superiores al capital invertido.

---

## Inicio rápido

1. Instale **Python 3.10+** (marcando *Add python.exe to PATH*) y **MetaTrader 5**;
   inicie sesión en una **cuenta DEMO** y active **Algo Trading**.
2. Copie el proyecto (p. ej. `C:\MT5Bot\`) y haga doble clic en **`start_bot.bat`**:
   instala lo que falte, abre MT5 si está cerrado y abre el **Centro de control**.
3. Pestaña **Laboratorio** → añada símbolos → **Analizar** → seleccione las
   combinaciones *Válidas* → **Activar seleccionados** (se crean en simulación).
4. Pestaña **Operación** → **Iniciar activos**. Observe unos días en simulación.
5. Solo si los resultados reales (pestaña **Estadísticas**) acompañan, cambie el
   modo de un símbolo a **REAL** (clic en la columna *Modo*).

---

## Arquitectura

```
C:\MT5Bot\
├── bot_gui.py               # CENTRO DE CONTROL (Operación, Laboratorio, Estadísticas, Configuración)
├── trading_bot.py           # Bot de un símbolo: evoluciona -> valida -> opera -> gestiona
├── lab.py                   # Laboratorio: escáner símbolo x timeframe x estrategia (en paralelo)
├── scout.py                 # Explorador: revisa categorías periódicamente y PROPONE símbolos nuevos
├── strategy_evolver.py      # Estrategias, backtest, evolución genética y walk-forward (sin MT5)
├── risk_manager.py          # Pérdida diaria, spread y tamaño de lote (sin MT5, testeable)
├── news_filter.py           # Calendario económico (caché compartida) y ventanas de noticias (sin MT5)
├── diagnostics.py           # Informe de diagnóstico (noticias, correlación, estado de los bots)
├── mt5_connector.py         # Conexión MT5, datos, órdenes, gestión de posiciones, historial
├── bot_common.py            # Configuración por capas, perfiles, rutas y logging
├── config_panel.py          # Panel simple (modo consola)
├── start_bot.bat / launch.ps1   # Lanzadores (instalan dependencias automáticamente)
├── global_config.json       # Configuración GENERAL (común a todos los símbolos)
├── risk_config*.json        # Un archivo por símbolo/perfil (solo lo propio)
├── risk_config.example.json # Todas las claves con sus valores por defecto
├── tests\                   # pytest (con un MetaTrader5 simulado)
└── logs\                    # Logs, estados, historial, laboratorio (se crea solo)
```

### Configuración por capas

`valores por defecto` ← **`global_config.json`** (común: riesgo, horario, salidas,
evolución, validación, avisos…) ← **`risk_config_<perfil>.json`** (propio de cada
símbolo: `symbol`, `timeframe`, `strategy`, `max_spread`, `magic`, `enabled`,
`simulation`).

La primera vez que se abre el Centro de control, las configuraciones antiguas
(perfiles completos) se **migran solas**: la general se toma del perfil principal
y cada perfil se queda solo con lo suyo. Antes se copia todo a
`logs/backup_config_<fecha>/`.

---

## Centro de control

| Pestaña | Qué permite |
|---|---|
| **Operación** (principal) | Tarjetas de **Cuenta**, **Resultado de hoy**, **Posiciones abiertas** y **Bots**. Tabla con **todos los símbolos**: clic en *Activo* (Sí/No) o en *Modo* (Simulación/REAL) para cambiarlos; estado, posición, resultado y validación de cada uno. Panel derecho con el **detalle del símbolo seleccionado**: situación (qué espera), posición, última vela, parámetros, **Log** en vivo y **Evolución** (veredicto, parámetros, métricas, ventanas, historial). Botones: *+ Añadir símbolos…* (buscador), *Iniciar activos*, *Detener todos*, *Iniciar / Detener / Editar… / Reiniciar (re-evoluciona) / Eliminar* y **Cerrar posiciones de los bots…** (emergencia) |
| **Laboratorio** | Elija símbolos (buscador o *Mis símbolos*), timeframes y estrategias → **Analizar**. Evalúa cada combinación con la misma evolución y validación que el bot, en paralelo, y las ordena por robustez. **Activar seleccionados** crea los perfiles (en simulación) |
| **Explorador** | Elija **categorías del bróker** (Forex, Índices, Metales…), timeframes y estrategias. Revisa automáticamente cada N minutos (por defecto 60; botón **Explorar ahora**) y **propone** las combinaciones que superan una validación más exigente en varias exploraciones seguidas. **Activar seleccionadas** crea los perfiles en simulación; **Descartar** las oculta. Muestra también las que están *en observación* |
| **Estadísticas** | Operaciones **reales** cerradas por los bots (7/30/90/365 días): curva de resultado acumulado y, por símbolo, operaciones, % de acierto, PF real vs. PF de validación, resultado y drawdown, con una valoración |
| **Configuración general** | Lo común a todos los símbolos: *Riesgo*, *Horario y salidas*, *Cuenta, inicio y avisos*, *Noticias y correlación* y, plegadas, las opciones **avanzadas** (evolución, validación, ejecución, seguimiento). Probar conexión, **Iniciar con Windows**, **Probar Telegram** |

* **Buscador de símbolos:** todos los símbolos del bróker con descripción,
  categoría, spread actual y lote mínimo; búsqueda en inglés o español (`oro`,
  `nasdaq`, `dax`, `bitcoin`…). Al crear un perfil ajusta su **spread máximo** a
  ~2,5× el spread actual (el de EURUSD no sirve para US500).
* **Avisos emergentes** en el escritorio: orden ejecutada, posición cerrada (con
  resultado), señal en simulación, pausa por rendimiento, pérdida diaria, bot
  detenido.
* **Detener** crea `logs/stop_<perfil>.flag` y el bot termina en ~1 s; las
  posiciones abiertas se mantienen con su SL/TP. Al cerrar la ventana se detienen
  los bots que lanzó.
* **Iniciar con Windows** crea un acceso en la carpeta *Inicio* que abre el Centro
  de control y arranca los símbolos activos (MT5 también debe abrirse e iniciar sesión).
* **Diagnóstico…** (pestaña Operación): comprueba lo que no se ve en pantalla y
  muestra un informe coloreado que se puede **copiar**: conexión y tipo de cuenta;
  **calendario de noticias** (si se descargó, próximas noticias de sus divisas y qué
  símbolos están ahora en pausa); **correlación** entre sus símbolos activos (pares
  ligados y qué compras/ventas bloquearía ahora con las posiciones abiertas); y el
  **estado interno de cada bot en marcha** (origen de los parámetros, próxima
  búsqueda, revisión con velas nuevas, noticias y señales bloqueadas con su motivo).
* **Valores negativos en rojo:** resultado de hoy y de las posiciones, filas de la
  tabla y de *Estadísticas* con resultado negativo (las tablas colorean la fila
  entera), beneficio neto negativo en *Evolución*, tramos de la curva con acumulado
  negativo y, en el log, cada número negativo (y la línea entera si una operación
  se cierra con pérdida).

### Explorador (`scout.py`)

1. **Universo:** símbolos operables de las categorías elegidas, ordenados por coste
   relativo (spread / ATR de H1); se descartan los que superan `scout_max_spread_atr`
   (0.15) y se exploran como máximo `scout_max_symbols` (20).
2. **Evaluación:** el mismo motor que el Laboratorio y el bot, pero más exigente:
   PF de validación ≥ `scout_min_pf_oos` (1.2) y ≥ `scout_min_trades_oos` (15) operaciones.
3. **Confirmación:** solo se propone una combinación aceptada en
   `scout_confirmations` (2) exploraciones **seguidas**; si una exploración la
   rechaza, vuelve a cero. Las que ya tienen perfil o se descartaron no se proponen.
4. **Usted decide:** el explorador nunca crea perfiles ni opera por sí mismo.

> Revisar muchos símbolos aumenta la probabilidad de encontrar algo "bueno" por
> azar; por eso los criterios son más estrictos y se exigen confirmaciones.
> Aun así, active las propuestas en **simulación** y compruebe sus resultados reales.

El modo automático (`scout_enabled`) funciona mientras el Centro de control está
abierto. Estado en `logs/scout_state.json`; descartes en `logs/scout_decisions.json`.

### Operación de prueba (solo cuenta demo)

Para comprobar que las órdenes se ejecutan sin esperar a una señal: seleccione un
símbolo con el mercado abierto y pulse **Operación de prueba…** (pestaña Operación).
Elija *Compra/Venta*, el SL y el TP en múltiplos de ATR y los minutos tras los que se
cerrará sola (0 = no se cierra; ciérrela usted o con *Cerrar posiciones de los bots…*).

- Se niega si la cuenta **no es demo**, sin importar la configuración.
- Usa el **lote mínimo** del bróker, con SL y TP en el servidor.
- Lleva el magic `20259999` y el comentario `MT5Bot prueba`: **no cuenta** como operación
  de ningún bot ni en sus estadísticas, pero sí la cierra *Cerrar posiciones de los bots…*.
- El cierre automático lo hace el Centro de control: si lo cierra antes, la posición
  sigue abierta con su SL/TP.

### Acciones (modo acciones)

Las acciones CFD (p. ej. `AAPL.US`, `SAN.ES`) se detectan solas por la categoría
o el nombre del bróker (columna/filtro **Tipo** en el buscador) y se operan con
reglas propias, iguales en el backtest, el bot, el Laboratorio y el Explorador:

| Regla | Por defecto en acciones | Clave del símbolo (Editar…) |
|---|---|---|
| Solo operar dentro de la **sesión de bolsa** (hora del servidor), deducida del historial (p. ej. EE. UU. ≈ `16-23` en Pepperstone) | automático | `session_hours` (vacío = automático) |
| No entrar en los **primeros minutos** de la sesión (los más volátiles) | 15 min | `avoid_open_minutes` (-1 = automático) |
| **Cerrar antes del cierre** de la sesión: no se queda abierto de un día a otro (sin hueco nocturno ni swap) | 15 min | `flat_before_close_minutes` (0 = mantener) |
| **Solo compras** si el bróker no permite vender esa acción | según el bróker | `long_only` |
| **Comisión** en % del precio (ida y vuelta) | 0,1 % (`stock_commission_pct`, general) | `commission_override` (precio fijo por operación) |
| **Lote máximo** en acciones (1 lote = 1 acción) | 1000 (`stock_max_lot`, general) | — |
| **Spread máximo**: si quedó el valor por defecto (0.0002, de EURUSD), el bot lo ajusta a ~2,5× el spread habitual del historial y lo guarda; si es un valor propio demasiado bajo, solo avisa | automático | `max_spread` |

El tipo de activo se puede fijar a mano (`asset_class`: `accion`, `indice`,
`forex`, `materia`, `cripto`, `otro`); con `auto` lo decide el bróker. Para el
resto de activos las reglas de sesión están desactivadas salvo que se configuren.
Tenga en cuenta que el historial H1 de una acción tiene solo ~7 velas por día:
use 4000–8000 velas o M15.

---

## Más operaciones: cómo acortar la espera

Un bot abre **una posición a la vez** y solo cuando su estrategia da señal al cierre
de una vela; con H1 y estrategias selectivas eso puede ser 1 operación cada varios
días. La frecuencia total sale de **sumar** la de todos los símbolos activos. Sin
inventar señales, se puede acelerar así:

1. **Más combinaciones válidas a la vez.** En el *Laboratorio* marque M5 y M15,
   todas las estrategias y muchos símbolos; ordene por **Frecuencia** y active (en
   simulación) varias combinaciones aceptadas. El resumen muestra las op./semana
   que suman. La tarjeta **Bots** de *Operación* muestra la frecuencia esperada de
   los símbolos activos (según su validación).
2. **Preferencia de frecuencia** (`min_trades_per_week`, por defecto 2): la
   evolución penaliza las combinaciones que operan menos de N veces por semana, así
   que elige parámetros más activos. No es un requisito para aceptar: si nada opera
   tan a menudo con ventaja, se aceptará lo que haya o se descartará igual que antes.
3. **Explorador con frecuencia mínima** (`scout_min_trades_week`, por defecto 2):
   solo propone combinaciones que operaron al menos N veces por semana en la
   validación. La columna *Op./sem.* lo muestra.
4. **Límite global de posiciones** (`max_open_positions_total`, 3 por defecto): con
   muchos símbolos, las señales se bloquean cuando ya hay 3 abiertas (lo verá como
   "bloqueadas: límite de posiciones" en el detalle). Súbalo, y si lo hace **baje el
   riesgo por operación** (p. ej. 6 posiciones con 0,5 % en vez de 3 con 1 %).
5. **Horario:** forex y oro tienen más movimiento en Londres y Nueva York; acciones e
   índices de EE. UU. solo operan en su sesión (y no en festivos).

Lo que **no** conviene: bajar el PF mínimo, las operaciones mínimas o la validación
para que se acepte más. Más operaciones sin ventaja solo producen más costes (spread
y comisión) y se pierde más rápido. Para comprobar la ejecución sin esperar está
*Operación de prueba…*.

## Estrategias

| Estrategia | Para qué plazo | Señal | Parámetros que evoluciona |
|---|---|---|---|
| `cruce_medias` | M15 / H1 (tendencia) | COMPRA si la SMA rápida cruza al alza la lenta y RSI(14) > `rsi_buy`; VENTA si cruza a la baja y RSI < `rsi_sell` | `fast` Int(5,30), `slow` Int(30,100) (`slow > fast`), `rsi_buy` Float(40,70), `rsi_sell` Float(30,60) |
| `reversion_media` | M5 / M15 (corto plazo) | COMPRA si el cierre cae **bajo la banda inferior** de Bollinger con RSI < `rsi_low`; VENTA si supera la **banda superior** con RSI > `rsi_high`. Con `confirm` = 1 espera a que la vela siguiente **vuelva a cerrar dentro** de la banda (no compra mientras el precio sigue cayendo) | `bb_period` Int(10,50), `bb_std` Float(1.5,3), `rsi_low` Float(15,40), `rsi_high` Float(60,85), `confirm` Int(0,1) |
| `ruptura` | M5 / M15 / H1 | COMPRA si el cierre **supera el máximo** de las `lookback` velas anteriores estando sobre la SMA `trend`; VENTA si **cae bajo el mínimo** estando bajo la SMA | `lookback` Int(10,100), `trend` Int(20,200) |
| `retroceso` | M15 / H1 (tendencia) | **Entrar a favor de la tendencia tras un retroceso.** Tendencia: cierre sobre la SMA `trend` y la SMA subiendo. Retroceso: RSI(3) ≤ `pb_rsi` en alguna de las `pb_window` velas anteriores. Entrada: cierre por encima del máximo de la vela anterior (el precio reanuda la subida). Venta: lo simétrico (RSI(3) ≥ 100 − `pb_rsi`) | `trend` Int(50,250), `pb_rsi` Float(5,40), `pb_window` Int(1,6) |
| `compresion` | M15 / H1 | **Volatilidad comprimida y luego ruptura.** Compresión: el ancho de Bollinger(20) está en el percentil ≤ `squeeze_pct` de las últimas 100 velas (en alguna de las 6 anteriores). Entrada: cierre por encima del máximo de `lookback` velas + `pen` × ATR (filtra rupturas falsas "por un pelo"), a favor de la SMA `trend` | `lookback` Int(10,60), `squeeze_pct` Float(0.05,0.5), `trend` Int(50,300), `pen` Float(0,0.5) |

Opciones comunes (configuración general):

| Opción | Qué añade a la búsqueda / al bot |
|---|---|
| `regime_filter` (sí) | **Filtro de régimen ADX**: nivel `adx` Float(15,40) evolucionado. Tendencia, ruptura, retroceso y compresión solo con ADX > nivel; reversión solo con ADX < nivel |
| `evolve_sl_tp` (sí) | Evoluciona `sl_mult` Float(1,3) y `tp_mult` Float(1,4) (× ATR) y, según la salida, `be_mult` Float(0.5,2.5) o `trail_mult` Float(1,3.5) |
| `exit_management` | `ninguna` (solo SL/TP), `breakeven` (SL a la entrada al avanzar `be_mult` × ATR) o `trailing` (SL a `trail_mult` × ATR del mejor precio). El bot lo aplica en vivo sobre las posiciones abiertas |
| `max_bars_in_trade` | Cierre por tiempo tras N velas (0 = desactivado) |
| `trading_hours` | Solo abre operaciones en ese horario **del servidor** (`""` = 24 h; `"10-19"` ≈ Londres + Nueva York en Pepperstone). Se aplica igual en el backtest |

Motor genético: **`strategy-genome`** (PyPI, MIT) con fallback automático a **`deap`**.

### Evaluación y validación (contra el sobreajuste)

* **Backtest:** entrada a la apertura de la vela siguiente a la señal; SL/TP con el
  ATR de la vela de la señal; si SL y TP se tocan en la misma vela se asume SL;
  coste = `commission_per_trade` + **spread histórico** de la vela de entrada.
* **Fitness:** Profit Factor, penalizado si hay menos de `min_trades`
  operaciones; con `fitness_folds` = 3 se puntúa por el **peor de 3 tramos** del
  entrenamiento (premia la estabilidad).
* **Walk-forward con varias ventanas** (`validation_windows` = 3): el 30 % final se
  divide en 3 tramos; para cada uno se evoluciona con todo lo anterior y se valida
  en el tramo. Se juntan todas las operaciones de validación. Se **acepta** solo si:
  * PF de validación ≥ `min_pf_oos` (1.0, no perder),
  * PF de validación ≥ `oos_pf_ratio_min` (0.8) × PF de entrenamiento (con tope 2.5),
  * al menos el 60 % de las ventanas tienen PF ≥ 1,
  * hay suficientes operaciones y el PF de entrenamiento ≥ `min_pf_in_sample`.

  Si se acepta, los parámetros para operar se evolucionan con **todo** el historial.
  Con `validation_windows` = 1 se usa un solo corte 70/30.
* **Parámetros frágiles** (`stability_check`, activado): se prueban 8 variantes de
  los parámetros elegidos, moviendo cada uno al azar hasta ±10 % de su rango. Si con
  esos vecinos el PF mediano es < 1, el resultado era un pico de suerte y se
  **descarta**. Una ventaja real suele formar una "meseta": funciona también con
  valores parecidos.
* **Monte Carlo** (informativo, no descarta): las operaciones de validación se
  remuestrean 1000 veces. La pestaña *Evolución* muestra la probabilidad de acabar en
  pérdidas y el drawdown típico y del peor 5 %, en "pérdidas medias" (≈ veces el
  riesgo por operación: con riesgo 1 %, "peor 5 %: 6 pérdidas medias" ≈ −6 %).
* El resultado completo se guarda en `evolved_params_<perfil>.json` y en
  `logs/evolution_history_<perfil>.jsonl`.

**Cuánto protege la validación.** Con series de **puro ruido** (sin ventaja posible;
8 series × 5 estrategias, configuración por defecto) se aceptaron 7 de 40
combinaciones; el control de parámetros frágiles frenó otras 2. Casi todos esos
falsos positivos tenían **pocas operaciones de validación (8-25)**. Por eso: dé más
crédito a los resultados con ≥ 20-30 operaciones de validación, una probabilidad de
pérdidas baja en el Monte Carlo y varias ventanas positivas. Si prefiere menos
falsos positivos (y menos símbolos aceptados), suba `min_trades_oos` (p. ej. a 15).

**Origen de las ideas.** Las estrategias `retroceso` y `compresion`, la confirmación
de la reversión y los controles de robustez se inspiran en proyectos públicos
(licencia MIT) que comparten sus resultados, incluidos los negativos:
[vrd07/Quant_Trading](https://github.com/vrd07/Quant_Trading) (compresión → ruptura
con margen mínimo y tendencia; retroceso con estocástico),
[Mohammed-AB/forex-strategy-lab](https://github.com/Mohammed-AB/forex-strategy-lab)
(Monte Carlo, ruptura fallida de Bollinger),
[bek01/forex-trading-bot](https://github.com/bek01/forex-trading-bot) y
[jjalww/algotrading-bot](https://github.com/jjalww/algotrading-bot) (qué no funcionó:
apilar indicadores, ajustar tras cada día malo, conclusiones con 30-100 operaciones).
No se ha copiado código: las reglas se han reescrito y se validan con el mismo
walk-forward que el resto.

---

## El bot en vivo (`trading_bot.py`)

Cada perfil es un proceso. Al arrancar: carga la configuración → conecta (aborta si
la cuenta no es DEMO con `require_demo`) → inicia el día → obtiene parámetros:

* **Reutiliza** los aceptados recientes (`reuse_evolution`) si son de la misma
  configuración (símbolo, timeframe, estrategia, validación…) y tienen menos de
  `reevolve_hours`. Arranca al instante y evita "reiniciar hasta que salga
  aceptado", que sería otra forma de sobreajuste. *Reiniciar (re-evoluciona)* en el
  Centro de control fuerza una búsqueda nueva (`--force-evolve`).
* Si no, **evoluciona y valida**. Si la validación descarta, el bot **sigue en
  marcha sin operar** y vuelve a buscar cada `retry_evolve_hours` (6 h). Con
  `retry_evolve_hours` = 0 termina con código 4, como antes.

Después, cada `loop_seconds` (y además justo **después de cada cierre de vela**, para
reaccionar sin esperar al siguiente ciclo):

1. Recarga en caliente la configuración (general o del perfil). Si cambian símbolo,
   timeframe, magic o estrategia, vuelve a evolucionar. Cambiar *Simulación/REAL*
   se aplica al instante.
2. Detecta **posiciones cerradas** (por SL, TP o a mano) y su resultado.
3. **Pérdida diaria** (`daily_loss_scope`): `bots` = resultado de hoy de las
   operaciones de los bots (sus operaciones manuales no cuentan), `cuenta` = equity de
   toda la cuenta. El inicio del día se guarda en `logs/day_state.json` (por cuenta):
   reiniciar no lo pone a cero, y si un bot llega al límite **todos** se detienen
   hasta el día siguiente.
4. **Adaptación de la estrategia** (siempre sin posición abierta):
   * **Re-evolución periódica** cada `reevolve_hours` (168 h). Funciona como
     *campeón / retador*: si la nueva búsqueda no supera la validación, se
     **conservan los parámetros actuales** mientras sigan sanos (no pierden con las
     velas posteriores a su adopción) y no tengan más de 4 × `reevolve_hours`.
   * **Vigilancia** cada `health_check_hours` (24 h): repite el backtest de los
     parámetros actuales **solo con las velas nuevas** (que la evolución no vio). Si
     con al menos `auto_disable_min_trades` operaciones simuladas el PF cae bajo
     `auto_disable_pf`, re-evoluciona **ya**, sin esperar a la periódica. El detalle
     del símbolo muestra el resultado y la hora de la próxima búsqueda.
   * Sin parámetros válidos o pausado por rendimiento: nueva búsqueda cada
     `retry_evolve_hours`.
5. **Gestiona las posiciones abiertas**: break-even, trailing y cierre por tiempo.
6. Señal en la última vela cerrada → filtros: horario, spread, una posición por
   símbolo, **señal tardía** (si la vela siguiente ya lleva abierta más de la mitad
   de su duración, p. ej. al arrancar el bot a media vela, no se opera: el backtest
   entra en su apertura), **noticias** y **correlación** (ver abajo), **límite
   global** `max_open_positions_total` (entre todos los perfiles) → lote por riesgo
   (`risk_percent`, SL por ATR) → orden IOC (con fallback FOK/RETURN).
7. **Pausa por rendimiento**: si el PF **real** de las últimas operaciones con los
   parámetros actuales (mínimo `auto_disable_min_trades`) cae bajo
   `auto_disable_pf`, deja de abrir operaciones y busca parámetros nuevos tras
   `retry_evolve_hours`.
8. Escribe `logs/status_<perfil>.json` para el Centro de control y, si está
   configurado, envía **avisos por Telegram** (`telegram_token`, `telegram_chat_id`).

En **simulación** (`simulation` = true, por defecto en los perfiles nuevos) calcula
todo y muestra la orden que *habría* enviado, sin enviarla.

### Noticias económicas

Con `news_filter` (activado) el bot **no abre operaciones** desde
`news_minutes_before` (30) minutos antes hasta `news_minutes_after` (30) minutos
después de una noticia de **alto impacto** (`news_impact` = `alto`; con `medio`
también las de impacto medio) de las **divisas del símbolo**, según el bróker:
EURUSD → EUR y USD; XAUUSD, US500 y acciones de EE. UU. → USD; GER40 → EUR. Con
`news_close_positions` además **cierra** sus posiciones al entrar en la ventana
previa (desactivado por defecto: el SL ya limita la pérdida).

* Calendario: el JSON semanal público de Forex Factory (`news_url`). La API de
  Python de MT5 no da acceso al calendario de MetaTrader. Se descarga como mucho
  cada `news_refresh_hours` (6 h) y se guarda en `logs/news_calendar.json`,
  **compartido** por todos los bots (una descarga para todos).
* Sin internet se usa la última copia (hasta 8 días). Si no hay ninguna, el filtro
  **no bloquea** y el detalle del símbolo lo indica ("no se bloquea nada").
* El detalle del símbolo muestra la próxima noticia relevante o "PAUSA POR NOTICIA";
  las señales bloqueadas aparecen como "noticia USD", etc.
* El backtest **no** conoce las noticias pasadas: la validación incluye operaciones
  que en vivo se habrían evitado.

### Correlación entre símbolos

Con `correlation_filter` (activado) el bot **no abre** si ya hay
`max_correlated_positions` (1) posiciones de los bots en símbolos cuyos rendimientos
H1 de las últimas 500 velas tienen |correlación| ≥ `correlation_threshold` (0.8) **y
van en la misma dirección efectiva**: comprar EURUSD y GBPUSD, o US500 y US30, cuenta
como la misma apuesta; comprar EURUSD y vender USDCHF (correlación negativa) también.
Posiciones en sentido contrario (que se cubren) no bloquean. Las correlaciones se
recalculan cada 6 h. Las señales bloqueadas aparecen como "correlación con otra
posición" y el log indica con qué símbolos.

**Estrategia automática** (`strategy` = `auto`, en *Editar…*): en cada evolución el
bot evoluciona y valida **las 5 estrategias** y opera la mejor **aceptada** (PF de
validación × raíz de sus operaciones, para no premiar 3 operaciones con suerte). La
tabla muestra "Auto: Ruptura", por ejemplo, y el detalle el resultado de cada una.
Tarda unas 5 veces más en evolucionar y, al elegir entre varias, sube algo el riesgo
de quedarse con una afortunada: solo compiten las que superan toda la validación.

---

## Configuración (claves principales)

Todas las claves y sus valores por defecto: `risk_config.example.json`.

| Clave | Defecto | Descripción |
|---|---|---|
| **Por símbolo** | | |
| `symbol`, `timeframe`, `strategy` | EURUSD, H1, cruce_medias | Nombre **exacto** del bróker (`US500`, `EURUSD.m`…), M1–H4, estrategia |
| `max_spread` | 0.0002 | Spread máximo (precio). Depende del activo |
| `magic` | — | Único por perfil (lo asigna la ventana) |
| `enabled` / `simulation` | true / true | Se inicia con "Iniciar activos" / no envía órdenes |
| `asset_class`, `session_hours` | auto, "" | Tipo de activo y horario propio (acciones) |
| `avoid_open_minutes`, `flat_before_close_minutes` | -1, -1 | Minutos sin entrar tras la apertura / cierre antes del fin (-1 = auto) |
| `long_only`, `commission_override` | false, null | Solo compras / comisión propia por operación |
| **Riesgo** | | |
| `risk_percent` | 1.0 | % del equity por operación (de cada símbolo) |
| `max_daily_loss_percent` / `daily_loss_scope` | 3.0 / bots | Límite diario y qué mide |
| `max_open_positions_total` | 3 | Posiciones abiertas a la vez entre todos los símbolos |
| `min_lot` / `max_lot` | 0.01 / 1.0 | Límites de lote |
| `require_demo` | true | Solo cuenta DEMO |
| **Horario y salidas** | | |
| `trading_hours`, `exit_management`, `max_bars_in_trade` | "", ninguna, 0 | Ver tabla de opciones |
| `regime_filter`, `evolve_sl_tp` | true, true | Filtro ADX y evolución de salidas |
| **Evolución y validación** | | |
| `history_bars`, `population`, `generations` | 2000, 20, 10 | Tamaño de la búsqueda (recomendado 8000 velas) |
| `validation_windows`, `train_ratio` | 3, 0.7 | Ventanas walk-forward |
| `min_pf_oos`, `oos_pf_ratio_min`, `min_pf_in_sample` | 1.0, 0.8, 1.0 | Criterios de aceptación |
| `min_trades`, `min_trades_oos`, `fitness_folds` | 20, 5, 3 | Mínimos de operaciones y tramos |
| `commission_per_trade`, `evolver_seed`, `evolver_backend` | 0.00007, null, auto | Coste, semilla (número = reproducible), motor |
| `stock_commission_pct` | 0.001 | Comisión de las acciones (fracción del precio, ida y vuelta) |
| **Seguimiento y avisos** | | |
| `auto_disable_pf`, `auto_disable_min_trades` | 0.7, 10 | Pausa por rendimiento real (0 = nunca) |
| `reevolve_hours` | 168 | Re-evolución periódica (0 = nunca); conserva los parámetros actuales si la nueva búsqueda falla y siguen sanos |
| `retry_evolve_hours` | 6 | Sin parámetros válidos: nueva búsqueda cada N h (0 = el bot se detiene al descartar) |
| `health_check_hours` | 24 | Revisión de los parámetros con las velas nuevas; re-evolución anticipada si se degradan (0 = nunca) |
| `reuse_evolution` | true | Reutilizar al arrancar los parámetros aceptados recientes de la misma configuración |
| **Noticias y correlación** | | |
| `news_filter`, `news_impact` | true, alto | Pausa por noticias de las divisas del símbolo; `medio` incluye impacto medio |
| `news_minutes_before`, `news_minutes_after` | 30, 30 | Ventana sin abrir operaciones alrededor de la noticia |
| `news_close_positions` | false | Cerrar las posiciones del bot antes de la noticia |
| `news_url`, `news_refresh_hours` | Forex Factory, 6 | Calendario (JSON semanal) y cada cuánto se descarga |
| `correlation_filter`, `correlation_threshold`, `max_correlated_positions` | true, 0.8, 1 | Control de posiciones correlacionadas en la misma dirección |
| `min_trades_per_week`, `stability_check` | 2, true | Preferencia de frecuencia; rechazo de parámetros frágiles |
| `telegram_token`, `telegram_chat_id` | "" | Avisos por Telegram (opcional) |
| `autostart_bots`, `lab_workers` | false, 0 | Autoarranque de activos; procesos del laboratorio (0 = auto) |
| **Explorador** | | |
| `scout_enabled`, `scout_interval_minutes` | false, 60 | Explorar automáticamente y cada cuánto |
| `scout_categories`, `scout_timeframes`, `scout_strategies` | [], [M15, H1], todas | Qué explorar |
| `scout_max_symbols`, `scout_max_spread_atr` | 20, 0.15 | Tamaño del universo y coste máximo |
| `scout_min_pf_oos`, `scout_min_trades_oos`, `scout_confirmations` | 1.2, 15, 2 | Exigencia para proponer |

---

## Instalación y lanzadores

| Comando | Qué hace |
|---|---|
| `start_bot.bat` | Instala/verifica dependencias, abre MT5 y el **Centro de control** |
| `start_bot.bat --install-only` | Solo verifica/instala dependencias |
| `start_bot.bat --console [--dry-run]` | Modo consola anterior (panel simple + un bot) |
| `python bot_gui.py [--autostart]` | Centro de control sin instalador |
| `python lab.py --symbols EURUSD,US500 --timeframes M15,H1 --strategies all` | Laboratorio por consola |
| `python scout.py --once` / `--loop` | Explorador por consola (usa la configuración general) |
| `python trading_bot.py --config risk_config_us500.json [--dry-run]` | Un perfil concreto por consola |
| `launch.ps1 [-DryRun] [-SkipPanel] [-InstallOnly]` | Equivalente en PowerShell |

La instalación automática comprueba Python ≥ 3.10 (ofrece instalarlo con
`winget`), pip, `MetaTrader5`, `pandas`, `numpy`, `strategy-genome`/`deap` y
`tkinter`, e instala solo lo que falte.

Tests (no requieren MT5): `python -m pip install -r requirements-dev.txt` y
`python -m pytest -q`.

### Qué verificar antes de operar en REAL (aunque sea demo)

- [ ] La cuenta es **DEMO** y **Algo Trading** está activado.
- [ ] El símbolo existe con el nombre exacto y su spread típico < `max_spread`.
- [ ] El laboratorio y la pestaña *Evolución* muestran la combinación como **aceptada**,
      con suficientes operaciones de validación.
- [ ] Varios días en **simulación** sin errores en el log.
- [ ] En *Estadísticas*, el PF real no está muy por debajo del de validación.

Códigos de salida del bot: `0` parada limpia · `1` error inesperado · `2`
configuración / símbolo / cuenta no DEMO · `3` conexión MT5 · `4` validación no superada.

---

## Limitaciones conocidas y próximos pasos

1. **Backtest simplificado:** velas OHLC (no ticks), sin swaps ni deslizamiento; si
   SL y TP caen en la misma vela se asume SL. El break-even/trailing se simula al
   cierre de cada vela, mientras que en vivo se aplica con el precio de cada revisión.
   → Validar también en el Probador de Estrategias de MT5.
2. **Ninguna estrategia tiene ventaja garantizada.** Más estrategias y opciones
   amplían la búsqueda y también el riesgo de encontrar algo por azar; la validación
   walk-forward lo reduce pero no lo elimina. No repita el laboratorio hasta que
   "salga" algo: fije una semilla y decida con los datos.
3. **Métrica única (Profit Factor).** → Optimización multiobjetivo (PF, drawdown,
   nº de operaciones) con NSGA-II, que `strategy-genome` ya soporta.
4. **Sin control de correlación entre símbolos** más allá del límite global de
   posiciones. → Límite de exposición por divisa / sector.
   Acciones: no se tienen en cuenta dividendos (ajustes del CFD), resultados
   trimestrales ni subastas de apertura/cierre; la sesión se deduce del historial
   (revísela en el detalle del símbolo, sobre todo con cambios de horario de verano).
5. **El "día" empieza a las 00:00 del PC**, no del servidor del bróker; el horario de
   operación (`trading_hours`) sí usa la hora del servidor.
6. **La pérdida diaria solo detiene nuevas entradas**; no cierra posiciones abiertas
   (para eso está *Cerrar posiciones de los bots…*).
7. **Con `daily_loss_scope = bots`, el riesgo de sus operaciones manuales no lo
   controla el bot** (use `cuenta` si quiere que cuenten).
8. **Probado en Linux con un MetaTrader5 simulado** (201 tests y la ventana
   recorrida en Xvfb). En Windows con MT5 real falta la prueba del usuario; los
   lanzadores `.bat`/`.ps1` solo se han revisado de forma estática.

---

## Licencia

Uso educativo. Sin garantía de ningún tipo.
