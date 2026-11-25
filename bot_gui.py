"""
bot_gui.py
==========
Centro de control del MT5 Bot (tkinter, sin dependencias extra).

Pestañas:
* **Operación** (principal): todos los símbolos en una tabla, con interruptores
  *Activo* y *Simulación/REAL*, resumen de la cuenta y, a la derecha, el detalle
  del símbolo seleccionado (situación, posición, log y evolución).
* **Laboratorio**: escáner de combinaciones símbolo x timeframe x estrategia
  (``lab.py``) para decidir qué operar; "Activar seleccionados" crea los perfiles.
* **Estadísticas**: operaciones reales de los bots, curva de resultados y
  comparación con lo esperado en la validación.
* **Configuración general**: lo común a todos los símbolos (riesgo, horario,
  salidas, avisos...). Lo propio de cada símbolo se edita con "Editar…".

Uso:
    python bot_gui.py               (o start_bot.bat, que instala dependencias y lo abre)
    python bot_gui.py --autostart   (inicia los símbolos activos al abrir)

Cada bot es un proceso ``trading_bot.py --config risk_config_<perfil>.json``. La
ventana lee su salida y ``logs/status_<perfil>.json`` y lo detiene creando
``logs/stop_<perfil>.flag``.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk
from typing import Any, Callable

from bot_common import (
    BASE_DIR, DEFAULT_CONFIG, LOG_DIR, MAIN_PROFILE, PROFILE_KEYS, PROFILE_NAME_RE,
    VALID_EXIT_MODES, VALID_STRATEGIES, VALID_TIMEFRAMES, config_path_for, list_profiles,
    load_config, load_global_config, migrate_to_global, profile_paths, read_json,
    save_global_config, save_profile, setup_logging, validate_config,
)

logger = logging.getLogger("bot_gui")

# ---------------------------------------------------------------------------
# Estilo y textos
# ---------------------------------------------------------------------------
C_BG = "#eef1f5"
C_CARD = "#ffffff"
C_HEADER = "#1f2a37"
C_TEXT = "#1f2933"
C_MUTED = "#6b7785"
C_GREEN = "#1b7f3b"
C_RED = "#c62828"
C_ORANGE = "#b26a00"
C_BLUE = "#1565c0"
C_GREY = "#7a8591"

FONT = "Segoe UI"
F_BASE = (FONT, 10)
F_SMALL = (FONT, 9)
F_BOLD = (FONT, 10, "bold")
F_TITLE = (FONT, 15, "bold")
F_SUB = (FONT, 12, "bold")
F_CARD_TITLE = (FONT, 9, "bold")
F_BIG = (FONT, 14, "bold")
F_MONO = ("Consolas", 9)

EXIT_MEANING = {
    0: "detenido correctamente",
    1: "error inesperado (ver log)",
    2: "configuración inválida, símbolo inexistente o cuenta no DEMO",
    3: "no se pudo conectar con MetaTrader 5",
    4: "parámetros descartados por la validación (no opera)",
}

PHASES: dict[str, tuple[str, str]] = {
    "iniciando": ("Iniciando…", C_BLUE),
    "conectando": ("Conectando…", C_BLUE),
    "evolucionando": ("Evolucionando…", C_BLUE),
    "operando": ("Operando", C_GREEN),
    "sin_parametros": ("Sin parámetros válidos", C_ORANGE),
    "pausado_rendimiento": ("Pausado por rendimiento", C_ORANGE),
    "detenido_perdida": ("Detenido por pérdida diaria", C_RED),
    "descartado": ("Descartado por validación", C_ORANGE),
    "error": ("Error", C_RED),
    "finalizado": ("Detenido", C_GREY),
}

STRATEGY_NAMES = {
    "cruce_medias": "Cruce de medias + RSI",
    "reversion_media": "Reversión a la media (Bollinger)",
    "ruptura": "Ruptura de rango",
}
STRATEGY_SHORT = {"cruce_medias": "Cruce medias", "reversion_media": "Reversión", "ruptura": "Ruptura"}

PARAM_LABELS = {
    "fast": "Media rápida (velas)", "slow": "Media lenta (velas)",
    "rsi_buy": "RSI mínimo para comprar", "rsi_sell": "RSI máximo para vender",
    "bb_period": "Periodo Bollinger (velas)", "bb_std": "Ancho bandas (desv. típicas)",
    "rsi_low": "RSI máximo para comprar", "rsi_high": "RSI mínimo para vender",
    "lookback": "Rango de ruptura (velas)", "trend": "Media de tendencia (velas)",
    "adx": "Nivel ADX (filtro de régimen)", "sl_mult": "Stop loss (× ATR)",
    "tp_mult": "Take profit (× ATR)", "be_mult": "Break-even al avanzar (× ATR)",
    "trail_mult": "Trailing a (× ATR)",
}

HOURS_PRESETS = ("24 h", "9-21", "10-19", "8-17", "15-23")
STOP_TIMEOUT_SECONDS = 20
MAX_LOG_LINES = 3000
LAB_RESULTS_PATH = LOG_DIR / "lab_results.json"
LAB_STOP_FLAG = LOG_DIR / "stop_lab.flag"


# ---------------------------------------------------------------------------
# Campos de la configuración general
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Field:
    """Un campo editable de la configuración general."""

    key: str
    label: str
    kind: str          # float | int | str | secret | bool | choice | combo | optint
    help: str = ""
    choices: tuple[str, ...] = ()


BASIC_GROUPS: list[tuple[str, list[Field]]] = [
    ("Riesgo", [
        Field("risk_percent", "Riesgo por operación (%)", "float", "% del equity que se arriesga en cada operación de cada símbolo"),
        Field("max_daily_loss_percent", "Pérdida diaria máxima (%)", "float", "Al alcanzarla, TODOS los bots dejan de abrir operaciones hasta el día siguiente"),
        Field("daily_loss_scope", "Pérdida diaria cuenta", "choice",
              "bots: solo el resultado de hoy de las operaciones de los bots (sus operaciones manuales no cuentan). "
              "cuenta: equity de toda la cuenta", ("bots", "cuenta")),
        Field("max_open_positions_total", "Máx. posiciones a la vez", "int", "Entre todos los símbolos. 0 = sin límite"),
        Field("min_lot", "Lote mínimo", "float", "Límite inferior de lote (además del del bróker)"),
        Field("max_lot", "Lote máximo", "float", "Límite superior de lote"),
    ]),
    ("Horario y salidas", [
        Field("trading_hours", "Horario (hora servidor)", "combo",
              "Solo abre operaciones en ese horario del servidor del bróker (Pepperstone ≈ GMT+2/+3). "
              "'24 h' = siempre. '10-19' ≈ Londres + Nueva York", HOURS_PRESETS),
        Field("exit_management", "Gestión de la salida", "choice",
              "ninguna: solo SL/TP. breakeven: lleva el SL a la entrada al avanzar. trailing: el SL sigue al precio",
              VALID_EXIT_MODES),
        Field("max_bars_in_trade", "Cerrar tras N velas", "int", "Cierre por tiempo. 0 = desactivado"),
        Field("regime_filter", "Filtro de régimen (ADX)", "bool", "Tendencia solo con ADX alto; reversión solo con ADX bajo (nivel evolucionado)"),
        Field("evolve_sl_tp", "Evolucionar SL, TP y salida", "bool", "El algoritmo elige los multiplicadores de stop loss, take profit, break-even y trailing"),
    ]),
    ("Cuenta, inicio y avisos", [
        Field("require_demo", "Solo cuenta DEMO", "bool", "Los bots no arrancan si la cuenta conectada no es demo"),
        Field("autostart_bots", "Iniciar símbolos activos al abrir", "bool", "Al abrir el Centro de control arranca los símbolos marcados como Activo"),
        Field("telegram_token", "Telegram: token del bot", "secret", "Opcional. Créelo con @BotFather en Telegram"),
        Field("telegram_chat_id", "Telegram: chat id", "str", "Opcional. Su id de chat (p. ej. con @userinfobot)"),
        Field("terminal_path", "Ruta terminal64.exe", "str", "Opcional, si hay varias instalaciones de MT5"),
    ]),
]
ADVANCED_GROUPS: list[tuple[str, list[Field]]] = [
    ("Evolución", [
        Field("history_bars", "Velas de historial", "int", "Velas para evolucionar y validar"),
        Field("population", "Población", "int", "Combinaciones por generación"),
        Field("generations", "Generaciones", "int", "Iteraciones del algoritmo genético"),
        Field("evolver_seed", "Semilla", "optint", "Vacío = aleatoria. Con un número, el resultado es reproducible"),
        Field("evolver_backend", "Motor genético", "choice", "auto usa strategy-genome y, si falla, deap",
              ("auto", "strategy-genome", "deap")),
        Field("lab_workers", "Procesos del laboratorio", "int", "0 = automático (núcleos del PC − 1)"),
    ]),
    ("Validación", [
        Field("validation_windows", "Ventanas walk-forward", "int", "1 = un solo corte 70/30. 3 = más estricto y fiable"),
        Field("train_ratio", "Proporción entrenamiento", "float", "0.7 = 70% entrena / 30% valida"),
        Field("min_pf_oos", "PF validación mínimo", "float", "Profit Factor mínimo en validación (1.0 = no perder)"),
        Field("oos_pf_ratio_min", "PF validación / entreno mín.", "float", "Descarta si PF validación < ratio × PF entrenamiento"),
        Field("min_pf_in_sample", "PF entrenamiento mínimo", "float", "Profit Factor mínimo en el entrenamiento"),
        Field("min_trades", "Mín. operaciones (entreno)", "int", "Menos operaciones penaliza y descarta"),
        Field("min_trades_oos", "Mín. operaciones (validación)", "int", "Mínimo de operaciones de validación"),
        Field("fitness_folds", "Tramos de fitness", "int", "Puntúa por el peor tramo del entrenamiento (1 = desactivado)"),
        Field("commission_per_trade", "Comisión por operación (precio)", "float", "Se suma al spread histórico en el backtest"),
    ]),
    ("Ejecución", [
        Field("sl_atr_mult", "Stop loss fijo (× ATR)", "float", "Solo si no se evolucionan SL/TP"),
        Field("tp_atr_mult", "Take profit fijo (× ATR)", "float", "Solo si no se evolucionan SL/TP"),
        Field("be_atr_mult", "Break-even fijo (× ATR)", "float", "Solo si no se evoluciona"),
        Field("trail_atr_mult", "Trailing fijo (× ATR)", "float", "Solo si no se evoluciona"),
        Field("atr_period", "Periodo ATR", "int", "Velas del ATR"),
        Field("loop_seconds", "Revisión cada (s)", "int", "Frecuencia del bucle del bot"),
        Field("heartbeat_seconds", "Mensaje de espera cada (s)", "int", "0 = sin mensajes de espera"),
        Field("deviation", "Deslizamiento máx. (puntos)", "int", "Desviación permitida al enviar órdenes"),
        Field("close_positions_on_error", "Cerrar posiciones si hay error", "bool", "Ante un error inesperado cierra las posiciones del símbolo"),
    ]),
    ("Seguimiento", [
        Field("auto_disable_pf", "Pausar si PF real <", "float", "Pausa un símbolo cuyo PF real con los parámetros actuales cae por debajo. 0 = nunca"),
        Field("auto_disable_min_trades", "…tras N operaciones", "int", "Operaciones reales mínimas antes de evaluar"),
        Field("reevolve_hours", "Re-evolucionar cada (h)", "int", "Busca parámetros nuevos periódicamente (sin posición abierta). 0 = nunca"),
    ]),
]
ALL_FIELDS: list[Field] = [f for _, g in BASIC_GROUPS + ADVANCED_GROUPS for f in g]

_FLOAT_RE = re.compile(r"^\d*\.?\d*$")
_INT_RE = re.compile(r"^\d*$")

#: Términos en español -> cómo suelen aparecer en los nombres/descripciones del bróker.
SEARCH_SYNONYMS: dict[str, tuple[str, ...]] = {
    "oro": ("gold", "xau"), "plata": ("silver", "xag"), "petroleo": ("oil", "wti", "brent", "crude"),
    "petróleo": ("oil", "wti", "brent", "crude"), "crudo": ("oil", "wti", "brent", "crude"),
    "gas": ("natgas", "ngas"), "sp500": ("us500", "spx"), "s&p": ("us500", "spx"),
    "nasdaq": ("nas", "ustec", "us100"), "dow": ("us30", "wall"), "dax": ("ger", "de40", "de30"),
    "bitcoin": ("btc",), "ethereum": ("eth",), "euro": ("eur",), "dolar": ("usd",), "dólar": ("usd",),
    "libra": ("gbp",), "yen": ("jpy",), "franco": ("chf",), "cobre": ("copper",),
}


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def fmt(value: Any, spec: str = "{:.2f}", empty: str = "—") -> str:
    """Formatea un número o devuelve un guion si no hay dato."""
    if value is None or value == "":
        return empty
    try:
        return spec.format(value)
    except (ValueError, TypeError):
        return str(value)


def fmt_price(value: Any) -> str:
    """Precio con hasta 6 cifras significativas."""
    return fmt(value, "{:.6g}")


def round_sig(value: float, digits: int = 2) -> float:
    """Redondea a ``digits`` cifras significativas (0.000237 -> 0.00024)."""
    if value <= 0:
        return value
    import math

    return round(value, -int(math.floor(math.log10(value))) + digits - 1)


def python_for_children() -> str:
    """Intérprete para lanzar procesos (python.exe aunque la GUI corra con pythonw.exe)."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        candidate = exe.with_name("python.exe")
        if candidate.exists():
            return str(candidate)
    return str(exe)


def pythonw_executable() -> str:
    """pythonw.exe (sin consola) si existe; si no, el intérprete actual."""
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    return str(candidate) if candidate.exists() else str(exe)


def describe_params(params: dict[str, Any] | None) -> str:
    """Resumen legible de los parámetros evolucionados (según la estrategia)."""
    if not params:
        return "—"
    if "bb_period" in params:
        text = (f"Bollinger {params.get('bb_period')} × {fmt(params.get('bb_std'), '{:.2f}')} · "
                f"RSI < {fmt(params.get('rsi_low'), '{:.1f}')} compra / "
                f"> {fmt(params.get('rsi_high'), '{:.1f}')} venta")
    elif "lookback" in params:
        text = f"Ruptura de {params.get('lookback')} velas · tendencia SMA {params.get('trend')}"
    else:
        text = (f"Medias {params.get('fast')}/{params.get('slow')} · "
                f"RSI > {fmt(params.get('rsi_buy'), '{:.1f}')} compra / "
                f"< {fmt(params.get('rsi_sell'), '{:.1f}')} venta")
    if "adx" in params:
        text += f" · ADX {fmt(params.get('adx'), '{:.0f}')}"
    if "sl_mult" in params:
        text += (f" · SL {fmt(params.get('sl_mult'), '{:.2f}')}× / "
                 f"TP {fmt(params.get('tp_mult'), '{:.2f}')}× ATR")
    if "be_mult" in params:
        text += f" · BE {fmt(params.get('be_mult'), '{:.2f}')}×"
    if "trail_mult" in params:
        text += f" · trailing {fmt(params.get('trail_mult'), '{:.2f}')}×"
    return text


def profile_name_for_symbol(symbol: str, timeframe: str, existing: set[str],
                            strategy: str | None = None) -> str | None:
    """Nombre de perfil libre: 'gbpusd', 'gbpusd_m5' o 'gbpusd_m5_ruptura'."""
    base = re.sub(r"[^a-z0-9_-]+", "_", symbol.lower()).strip("_-")[:20] or "simbolo"
    options = [base, f"{base}_{timeframe.lower()}"]
    if strategy:
        options.append(f"{base}_{timeframe.lower()}_{STRATEGY_SHORT.get(strategy, strategy)[:8].lower()}")
    for name in options:
        name = re.sub(r"[^a-z0-9_-]+", "_", name)[:31]
        if name != MAIN_PROFILE and PROFILE_NAME_RE.match(name) and name not in existing:
            return name
    return None


def symbol_matches(item: dict[str, Any], query: str) -> bool:
    """True si TODAS las palabras de la búsqueda aparecen en el nombre o la descripción."""
    text = f"{item.get('name', '')} {item.get('description', '')} {item.get('path', '')}".lower()
    for word in query.lower().split():
        options = (word,) + SEARCH_SYNONYMS.get(word, ())
        if not any(opt in text for opt in options):
            return False
    return True


def with_mt5(require_demo: bool, terminal_path: str | None,
             task: Callable[[Any], dict[str, Any]]) -> dict[str, Any]:
    """
    Conecta a MT5 (en un hilo aparte), ejecuta ``task(mt5_connector)`` y desconecta.
    Devuelve el dict de la tarea con ``ok`` o ``{"ok": False, "error": ...}``.
    """
    try:
        import mt5_connector as mt5c  # import diferido: la ventana abre aunque falte MT5

        if not mt5c.connect_mt5_with_retry(max_retries=2, wait_seconds=1, require_demo=require_demo,
                                           terminal_path=terminal_path or None):
            return {"ok": False, "error": "No se pudo conectar. ¿Está MT5 abierto y con sesión iniciada?"}
        out = task(mt5c)
        out.setdefault("ok", True)
        return out
    except SystemExit as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - se muestra en la ventana
        logger.exception("Error con MT5")
        return {"ok": False, "error": f"Error: {exc}"}
    finally:
        try:
            import mt5_connector as mt5c

            mt5c.safe_shutdown()
        except Exception:  # noqa: BLE001
            pass


def startup_shortcut_path() -> Path | None:
    """Acceso del Centro de control en la carpeta Inicio de Windows (None fuera de Windows)."""
    appdata = os.environ.get("APPDATA")
    if os.name != "nt" or not appdata:
        return None
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "MT5Bot.cmd"


def _mtime(path: Path) -> float:
    """Fecha de modificación de un archivo (0 si no existe)."""
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def read_json_line(line: str) -> dict[str, Any] | None:
    """Parsea una línea JSON (None si está corrupta)."""
    try:
        data = json.loads(line)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def profile_config(name: str) -> dict[str, Any] | None:
    """Configuración efectiva de un perfil (general + propia), o None si es inválida."""
    try:
        return load_config(config_path_for(name))
    except (ValueError, FileNotFoundError):
        return None


def next_magic() -> int:
    """Magic number libre (el mayor existente + 1)."""
    magics = [(read_json(config_path_for(n)) or {}).get("magic") for n in list_profiles()]
    return max([m for m in magics if isinstance(m, int)] + [20250100]) + 1


def status_is_fresh(st: dict[str, Any], seconds: int = 180) -> bool:
    """True si el estado se actualizó hace menos de ``seconds`` segundos."""
    try:
        return (datetime.now() - datetime.fromisoformat(st["updated"])).total_seconds() < seconds
    except (KeyError, TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Widgets auxiliares
# ---------------------------------------------------------------------------
class Tooltip:
    """Ayuda emergente al pasar el ratón por un widget."""

    def __init__(self, widget: tk.Widget, text: str) -> None:
        self.widget, self.text = widget, text
        self.tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")

    def _show(self, _event: Any = None) -> None:
        if self.tip or not self.text:
            return
        x = self.widget.winfo_rootx() + 10
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        tk.Label(self.tip, text=self.text, bg="#fffbe6", fg=C_TEXT, font=F_SMALL, relief="solid",
                 borderwidth=1, padx=6, pady=3, wraplength=340, justify="left").pack()

    def _hide(self, _event: Any = None) -> None:
        if self.tip:
            self.tip.destroy()
            self.tip = None


class Card(tk.Frame):
    """Tarjeta blanca con título, valor principal y líneas secundarias."""

    def __init__(self, master: tk.Widget, title: str, lines: int = 2) -> None:
        super().__init__(master, bg=C_CARD, highlightbackground="#d5dbe3", highlightthickness=1,
                         padx=12, pady=8)
        tk.Label(self, text=title.upper(), bg=C_CARD, fg=C_MUTED, font=F_CARD_TITLE,
                 anchor="w").pack(fill="x")
        self.main = tk.Label(self, text="—", bg=C_CARD, fg=C_TEXT, font=F_BIG, anchor="w", justify="left")
        self.main.pack(fill="x", pady=(2, 2))
        self.lines: list[tk.Label] = []
        for _ in range(lines):
            lbl = tk.Label(self, text="", bg=C_CARD, fg=C_MUTED, font=F_SMALL, anchor="w",
                           justify="left", wraplength=260)
            lbl.pack(fill="x")
            self.lines.append(lbl)

    def set(self, main: str, color: str = C_TEXT, *lines: tuple[str, str] | str) -> None:
        """Actualiza el texto principal y las líneas secundarias (texto o (texto, color))."""
        self.main.configure(text=main, fg=color)
        for i, lbl in enumerate(self.lines):
            item = lines[i] if i < len(lines) else ""
            text, col = (item, C_MUTED) if isinstance(item, str) else item
            lbl.configure(text=text, fg=col)


def make_tree(master: tk.Widget, columns: list[tuple[str, str, int, str]], height: int = 8) -> ttk.Treeview:
    """Crea un Treeview con scroll. columns = [(id, título, ancho, alineación)]."""
    frame = ttk.Frame(master)
    tree = ttk.Treeview(frame, columns=[c[0] for c in columns], show="headings", height=height)
    for cid, title, width, anchor in columns:
        tree.heading(cid, text=title)
        tree.column(cid, width=width, anchor=anchor, stretch=True)
    scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=scroll.set)
    tree.pack(side="left", fill="both", expand=True)
    scroll.pack(side="right", fill="y")
    tree.frame = frame  # type: ignore[attr-defined]
    for tag, color in (("ok", C_GREEN), ("bad", C_RED), ("warn", C_ORANGE), ("idle", C_GREY),
                       ("info", C_BLUE)):
        tree.tag_configure(tag, foreground=color)
    return tree


class Toast(tk.Toplevel):
    """Aviso emergente en la esquina inferior derecha (se cierra solo o con un clic)."""

    active: list[Toast] = []

    def __init__(self, master: tk.Tk, title: str, text: str, color: str = C_BLUE,
                 seconds: int = 8) -> None:
        super().__init__(master)
        self.wm_overrideredirect(True)
        try:
            self.attributes("-topmost", True)
        except tk.TclError:
            pass
        frame = tk.Frame(self, bg=C_CARD, highlightbackground="#b8c1cc", highlightthickness=1)
        frame.pack(fill="both", expand=True)
        tk.Frame(frame, bg=color, width=6).pack(side="left", fill="y")
        body = tk.Frame(frame, bg=C_CARD, padx=10, pady=8)
        body.pack(side="left", fill="both", expand=True)
        tk.Label(body, text=title, bg=C_CARD, fg=color, font=F_BOLD, anchor="w").pack(fill="x")
        tk.Label(body, text=text, bg=C_CARD, fg=C_TEXT, font=F_SMALL, anchor="w", justify="left",
                 wraplength=300).pack(fill="x")
        for w in (self, frame, body):
            w.bind("<Button-1>", lambda _e: self.close())
        Toast.active.append(self)
        self.update_idletasks()
        width, height = 340, max(64, self.winfo_reqheight())
        index = len(Toast.active) - 1
        x = self.winfo_screenwidth() - width - 16
        y = self.winfo_screenheight() - (height + 10) * (index + 1) - 50
        self.geometry(f"{width}x{height}+{x}+{max(0, y)}")
        self.after(seconds * 1000, self.close)

    def close(self) -> None:
        if self in Toast.active:
            Toast.active.remove(self)
        try:
            self.destroy()
        except tk.TclError:
            pass


class EquityChart(tk.Canvas):
    """Curva de resultado acumulado de las operaciones (dibujada a mano, sin librerías)."""

    def __init__(self, master: tk.Widget, height: int = 220) -> None:
        super().__init__(master, height=height, bg=C_CARD, highlightthickness=1,
                         highlightbackground="#d5dbe3")
        self.values: list[float] = []
        self.labels: list[str] = []
        self.bind("<Configure>", lambda _e: self.redraw())

    def set_data(self, labels: list[str], profits: list[float]) -> None:
        """labels/profits en orden cronológico; se dibuja el acumulado."""
        total, acc = 0.0, []
        for p in profits:
            total += p
            acc.append(total)
        self.values, self.labels = acc, labels
        self.redraw()

    def redraw(self) -> None:
        self.delete("all")
        w, h = max(self.winfo_width(), 200), max(self.winfo_height(), 120)
        pad_l, pad_r, pad_t, pad_b = 60, 16, 16, 26
        if not self.values:
            self.create_text(w / 2, h / 2, text="Sin operaciones cerradas en el periodo",
                             fill=C_MUTED, font=F_BASE)
            return
        vals = [0.0] + self.values
        lo, hi = min(vals), max(vals)
        if hi - lo < 1e-9:
            hi, lo = hi + 1, lo - 1
        sx = (w - pad_l - pad_r) / max(1, len(vals) - 1)

        def y(v: float) -> float:
            return pad_t + (hi - v) / (hi - lo) * (h - pad_t - pad_b)

        for v in (lo, (lo + hi) / 2, hi):
            self.create_line(pad_l, y(v), w - pad_r, y(v), fill="#eef1f5")
            self.create_text(pad_l - 6, y(v), text=f"{v:+.0f}", anchor="e", fill=C_MUTED, font=F_SMALL)
        self.create_line(pad_l, y(0), w - pad_r, y(0), fill="#b8c1cc", dash=(3, 3))
        pts = [(pad_l + i * sx, y(v)) for i, v in enumerate(vals)]
        color = C_GREEN if vals[-1] >= 0 else C_RED
        self.create_line(*[c for p in pts for c in p], fill=color, width=2, smooth=False)
        for px, py in pts[1:]:
            self.create_oval(px - 2, py - 2, px + 2, py + 2, fill=color, outline=color)
        self.create_text(w - pad_r, pad_t, text=f"Acumulado {vals[-1]:+.2f}", anchor="ne",
                         fill=color, font=F_BOLD)
        if self.labels:
            self.create_text(pad_l, h - 6, text=self.labels[0], anchor="sw", fill=C_MUTED, font=F_SMALL)
            self.create_text(w - pad_r, h - 6, text=self.labels[-1], anchor="se", fill=C_MUTED, font=F_SMALL)


class SymbolBrowser(tk.Toplevel):
    """
    Buscador de símbolos del bróker.

    * ``mode="create"``: selección múltiple + timeframe, estrategia y simulación
      -> crear un perfil por símbolo.
    * ``mode="pick"``: selección múltiple, devuelve solo los símbolos (laboratorio).
    * ``mode="single"``: elegir un único símbolo.
    """

    def __init__(self, master: tk.Tk, symbols: list[dict[str, Any]], mode: str,
                 existing: set[str], timeframe: str = "M15", strategy: str = "ruptura") -> None:
        super().__init__(master)
        self.mode = mode
        multi = mode in ("create", "pick")
        self.title("Buscador de símbolos" + (" — crear símbolos" if mode == "create" else ""))
        self.configure(bg=C_BG, padx=14, pady=12)
        self.geometry("920x600")
        self.minsize(760, 480)
        self.transient(master)
        self.symbols = symbols
        self.by_name = {d["name"]: d for d in symbols}
        self.existing = existing
        self.result: dict[str, Any] | None = None
        self._after_id: str | None = None
        self._shown = 0

        top = ttk.Frame(self)
        top.pack(fill="x")
        ttk.Label(top, text="Buscar:", font=F_BOLD).pack(side="left")
        self.query = tk.StringVar()
        entry = ttk.Entry(top, textvariable=self.query, width=30)
        entry.pack(side="left", padx=(6, 14))
        entry.focus_set()
        ttk.Label(top, text="Categoría:").pack(side="left")
        cats = sorted({d["category"] for d in symbols}, key=str.lower)
        self.category = tk.StringVar(value="Todas")
        ttk.Combobox(top, textvariable=self.category, values=["Todas"] + cats, state="readonly",
                     width=18).pack(side="left", padx=6)
        self.only_tradable = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="Solo operables", variable=self.only_tradable).pack(side="left", padx=8)
        ttk.Label(self, text="Escriba parte del nombre o la descripción, en inglés o español: eur, jpy, "
                             "oro, nasdaq, dax, bitcoin…"
                  + ("  ·  Ctrl/Mayús + clic para elegir varios." if multi else "  ·  Doble clic para elegir."),
                  style="Muted.TLabel").pack(fill="x", pady=(6, 6))

        self.tree = make_tree(self, [
            ("name", "Símbolo", 120, "w"), ("desc", "Descripción", 330, "w"),
            ("cat", "Categoría", 120, "w"), ("spread", "Spread actual", 100, "e"),
            ("lot", "Lote mín.", 70, "e"), ("note", "", 120, "w")], height=14)
        self.tree.configure(selectmode="extended" if multi else "browse")
        self.tree.frame.pack(fill="both", expand=True)
        self.tree.tag_configure("existing", foreground=C_GREY)
        self.tree.tag_configure("visible", foreground=C_BLUE)
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._update_count())
        self.tree.bind("<Double-1>", lambda _e: self._ok())
        self.count = ttk.Label(self, text="", style="Muted.TLabel")
        self.count.pack(fill="x", pady=(6, 0))

        bottom = ttk.Frame(self)
        bottom.pack(fill="x", pady=(10, 0))
        if mode == "create":
            ttk.Label(bottom, text="Timeframe:").pack(side="left")
            self.tf = tk.StringVar(value=timeframe)
            ttk.Combobox(bottom, textvariable=self.tf, values=list(VALID_TIMEFRAMES), state="readonly",
                         width=6).pack(side="left", padx=(4, 10))
            ttk.Label(bottom, text="Estrategia:").pack(side="left")
            self.strategy = tk.StringVar(value=strategy)
            ttk.Combobox(bottom, textvariable=self.strategy, values=list(VALID_STRATEGIES),
                         state="readonly", width=16).pack(side="left", padx=(4, 10))
            self.simulation = tk.BooleanVar(value=True)
            ttk.Checkbutton(bottom, text="Simulación", variable=self.simulation).pack(side="left")
        ttk.Button(bottom, text="Cancelar", command=self.destroy).pack(side="right")
        label = {"create": "Crear", "pick": "Añadir", "single": "Usar este símbolo"}[mode]
        self.ok_button = ttk.Button(bottom, style="Accent.TButton", command=self._ok, text=label)
        self.ok_button.pack(side="right", padx=8)
        self._label = label

        for var in (self.query, self.category, self.only_tradable):
            var.trace_add("write", lambda *_a: self._schedule_filter())
        self.bind("<Escape>", lambda _e: self.destroy())
        self.bind("<Return>", lambda _e: self._ok())
        self._apply_filter()
        self.update_idletasks()
        x = master.winfo_rootx() + max(0, (master.winfo_width() - self.winfo_width()) // 2)
        y = master.winfo_rooty() + 60
        self.geometry(f"+{x}+{y}")
        self.grab_set()

    def _schedule_filter(self) -> None:
        if self._after_id:
            self.after_cancel(self._after_id)
        self._after_id = self.after(150, self._apply_filter)

    def _apply_filter(self) -> None:
        self._after_id = None
        query, cat = self.query.get().strip(), self.category.get()
        selected = set(self.tree.selection())
        self.tree.delete(*self.tree.get_children())
        shown = 0
        for d in self.symbols:
            if cat != "Todas" and d["category"] != cat:
                continue
            if self.only_tradable.get() and not d.get("tradable", True):
                continue
            if query and not symbol_matches(d, query):
                continue
            has = d["name"] in self.existing
            note = "ya está en la lista" if has else ("en Observación" if d.get("visible") else "")
            tag = "existing" if has else ("visible" if d.get("visible") else "")
            self.tree.insert("", "end", iid=d["name"], tags=(tag,) if tag else (), values=(
                d["name"], d["description"], d["category"],
                fmt(d.get("spread"), "{:.6g}", "sin cotización"), fmt(d.get("volume_min"), "{:g}"), note))
            shown += 1
        keep = [i for i in selected if self.tree.exists(i)]
        if keep:
            self.tree.selection_set(keep)
        self._shown = shown
        self._update_count()

    def _update_count(self) -> None:
        n_sel = len(self.tree.selection())
        self.count.configure(text=f"{self._shown} de {len(self.symbols)} símbolos"
                                  + (f" · {n_sel} seleccionado(s)" if n_sel else ""))
        if self.mode != "single":
            self.ok_button.configure(text=f"{self._label} ({n_sel})" if n_sel else self._label)

    def _ok(self) -> None:
        chosen = [self.by_name[i] for i in self.tree.selection() if i in self.by_name]
        if not chosen:
            messagebox.showinfo("Buscador de símbolos", "Seleccione al menos un símbolo.", parent=self)
            return
        self.result = {"symbols": chosen if self.mode != "single" else chosen[:1]}
        if self.mode == "create":
            self.result.update(timeframe=self.tf.get(), strategy=self.strategy.get(),
                               simulation=bool(self.simulation.get()))
        self.destroy()


class ProfileDialog(tk.Toplevel):
    """Edita lo propio de un símbolo: símbolo, timeframe, estrategia, spread, activo, simulación."""

    def __init__(self, master: ControlCenter, name: str) -> None:
        super().__init__(master)
        self.app, self.name = master, name
        self.path = config_path_for(name)
        cfg = profile_config(name) or {**load_global_config(BASE_DIR), **(read_json(self.path) or {})}
        self.title(f"Editar símbolo — perfil '{name}'")
        self.configure(bg=C_BG, padx=16, pady=14)
        self.resizable(False, False)
        self.transient(master)
        self.saved = False
        self.vars: dict[str, tk.Variable] = {
            "symbol": tk.StringVar(value=cfg.get("symbol", "")),
            "timeframe": tk.StringVar(value=cfg.get("timeframe", "M15")),
            "strategy": tk.StringVar(value=cfg.get("strategy", "ruptura")),
            "max_spread": tk.StringVar(value=f"{float(cfg.get('max_spread', 0.0002)):g}"),
            "magic": tk.StringVar(value=str(cfg.get("magic", ""))),
            "enabled": tk.BooleanVar(value=bool(cfg.get("enabled", True))),
            "simulation": tk.BooleanVar(value=bool(cfg.get("simulation", True))),
        }
        grid = ttk.Frame(self)
        grid.pack(fill="x")
        rows = [("Símbolo", "symbol"), ("Timeframe", "timeframe"), ("Estrategia", "strategy"),
                ("Spread máximo (precio)", "max_spread"), ("Magic number", "magic")]
        for r, (label, key) in enumerate(rows):
            ttk.Label(grid, text=label).grid(row=r, column=0, sticky="w", pady=4)
            if key == "timeframe":
                w: tk.Widget = ttk.Combobox(grid, textvariable=self.vars[key], values=list(VALID_TIMEFRAMES),
                                            state="readonly", width=18)
            elif key == "strategy":
                w = ttk.Combobox(grid, textvariable=self.vars[key], values=list(VALID_STRATEGIES),
                                 state="readonly", width=18)
            else:
                w = ttk.Entry(grid, textvariable=self.vars[key], width=20)
            w.grid(row=r, column=1, sticky="ew", padx=(10, 0), pady=4)
            if key == "symbol":
                ttk.Button(grid, text="Buscar…", command=self._pick_symbol).grid(row=r, column=2, padx=(6, 0))
            if key == "max_spread":
                ttk.Button(grid, text="Ajustar con MT5", command=self._auto_spread).grid(
                    row=r, column=2, padx=(6, 0))
        ttk.Checkbutton(self, text="Activo (se inicia con 'Iniciar activos')",
                        variable=self.vars["enabled"]).pack(anchor="w", pady=(10, 2))
        ttk.Checkbutton(self, text="Simulación (calcula todo pero NO envía órdenes)",
                        variable=self.vars["simulation"]).pack(anchor="w")
        self.status = tk.Label(self, text="", bg=C_BG, fg=C_MUTED, font=F_SMALL, anchor="w",
                               justify="left", wraplength=420)
        self.status.pack(fill="x", pady=(6, 0))
        buttons = ttk.Frame(self)
        buttons.pack(anchor="e", pady=(12, 0))
        ttk.Button(buttons, text="Cancelar", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="Guardar", style="Accent.TButton", command=self._save).pack(
            side="right", padx=8)
        self.bind("<Escape>", lambda _e: self.destroy())
        self.update_idletasks()
        x = master.winfo_rootx() + (master.winfo_width() - self.winfo_reqwidth()) // 2
        y = master.winfo_rooty() + 120
        self.geometry(f"+{max(0, x)}+{max(0, y)}")
        self.grab_set()

    def _pick_symbol(self) -> None:
        self.app.with_symbol_list(self._open_single)

    def _open_single(self, symbols: list[dict[str, Any]]) -> None:
        dialog = SymbolBrowser(self.app, symbols, "single", set())
        self.wait_window(dialog)
        if dialog.result and self.winfo_exists():
            item = dialog.result["symbols"][0]
            self.vars["symbol"].set(item["name"])
            if item.get("spread"):
                self.vars["max_spread"].set(f"{round_sig(item['spread'] * 2.5):g}")
                self.status.configure(text=f"{item['name']}: spread actual {item['spread']:g} -> "
                                           "spread máximo ajustado a 2,5×.", fg=C_GREEN)

    def _auto_spread(self) -> None:
        symbol = str(self.vars["symbol"].get()).strip()
        self.status.configure(text=f"Consultando el spread de {symbol} en MT5…", fg=C_BLUE)
        gcfg = load_global_config(BASE_DIR)

        def task(mt5c: Any) -> dict[str, Any]:
            if not mt5c.ensure_symbol(symbol):
                return {"ok": False, "error": f"'{symbol}' no existe. Sugerencias: "
                                              f"{', '.join(mt5c.find_similar_symbols(symbol)[:5])}"}
            return {"spread": mt5c.get_spread(symbol)}

        def done(res: dict[str, Any]) -> None:
            if not self.winfo_exists():
                return
            sp = res.get("spread")
            if not res.get("ok"):
                self.status.configure(text=res.get("error", "Error"), fg=C_RED)
            elif sp is None or sp != sp:
                self.status.configure(text="Sin cotización ahora (¿mercado cerrado?).", fg=C_ORANGE)
            else:
                self.vars["max_spread"].set(f"{round_sig(sp * 2.5):g}")
                self.status.configure(text=f"Spread actual {sp:g} -> máximo {round_sig(sp * 2.5):g}.", fg=C_GREEN)

        self.app.run_mt5(bool(gcfg["require_demo"]), gcfg.get("terminal_path") or None, task, done)

    def _save(self) -> None:
        try:
            data = {
                "symbol": str(self.vars["symbol"].get()).strip(),
                "timeframe": self.vars["timeframe"].get(),
                "strategy": self.vars["strategy"].get(),
                "max_spread": float(self.vars["max_spread"].get()),
                "magic": int(self.vars["magic"].get()),
                "enabled": bool(self.vars["enabled"].get()),
                "simulation": bool(self.vars["simulation"].get()),
            }
        except ValueError:
            self.status.configure(text="Spread máximo y magic deben ser números.", fg=C_RED)
            return
        clash = [n for n in list_profiles() if n != self.name
                 and (read_json(config_path_for(n)) or {}).get("magic") == data["magic"]]
        if clash:
            self.status.configure(text=f"El magic {data['magic']} ya lo usa '{clash[0]}'.", fg=C_RED)
            return
        if not data["simulation"] and not messagebox.askyesno(
                "Envío de órdenes", f"'{self.name}' enviará órdenes REALES a la cuenta.\n¿Continuar?",
                parent=self, icon="warning"):
            return
        try:
            save_profile(data, self.path)
        except (ValueError, OSError) as exc:
            self.status.configure(text=str(exc), fg=C_RED)
            return
        self.saved = True
        self.destroy()


# ---------------------------------------------------------------------------
# Procesos hijos (bots y laboratorio)
# ---------------------------------------------------------------------------
@dataclass
class BotProcess:
    """Un proceso lanzado desde la ventana (un bot o el laboratorio)."""

    profile: str
    config_path: Path
    dry_run: bool = False
    cmd: list[str] | None = None
    stop_flag_path: Path | None = None
    proc: subprocess.Popen[str] | None = None
    lines: queue.Queue[str] = field(default_factory=queue.Queue)
    buffer: list[str] = field(default_factory=list)
    stop_requested_at: float | None = None
    reported: bool = False
    restart: bool = False

    def start(self) -> None:
        """Lanza el proceso sin ventana de consola y empieza a leer su salida."""
        cmd = self.cmd or [python_for_children(), "-u", str(BASE_DIR / "trading_bot.py"),
                           "--config", str(self.config_path)]
        if self.dry_run and self.cmd is None:
            cmd = cmd + ["--dry-run"]
        env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0  # type: ignore[attr-defined]
        self.proc = subprocess.Popen(
            cmd, cwd=str(BASE_DIR), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=flags,
        )
        self.stop_requested_at = None
        self.reported = False
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        for line in self.proc.stdout:
            self.lines.put(line.rstrip("\r\n"))

    @property
    def running(self) -> bool:
        """True mientras el proceso siga vivo."""
        return self.proc is not None and self.proc.poll() is None

    @property
    def exit_code(self) -> int | None:
        """Código de salida (None si sigue en ejecución)."""
        return None if self.proc is None else self.proc.poll()

    def request_stop(self) -> None:
        """Pide al proceso que se detenga creando su archivo de parada."""
        flag = self.stop_flag_path or profile_paths(self.config_path).stop_flag
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text(datetime.now().isoformat(), encoding="utf-8")
        self.stop_requested_at = time.monotonic()

    def kill(self) -> None:
        """Termina el proceso a la fuerza (si no respondió a la parada ordenada)."""
        if self.running and self.proc is not None:
            self.proc.terminate()


# ---------------------------------------------------------------------------
# Ventana principal
# ---------------------------------------------------------------------------
class ControlCenter(tk.Tk):
    """Centro de control: operación, laboratorio, estadísticas y configuración general."""

    def __init__(self, autostart: bool = False) -> None:
        super().__init__()
        self.title("MT5 Bot — Centro de control")
        self.geometry("1280x800")
        self.minsize(1080, 700)
        self.configure(bg=C_BG)
        self._setup_style()

        self.migrated = False
        try:
            self.migrated = migrate_to_global(BASE_DIR)
        except OSError:
            logger.exception("No se pudo migrar la configuración")

        self.procs: dict[str, BotProcess] = {}
        self.lab_proc: BotProcess | None = None
        self.selected = tk.StringVar(value="")
        self.gvars: dict[str, tk.Variable] = {}
        self.gdirty = False
        self._loading = False
        self._closing = False
        self._symbol_list: list[dict[str, Any]] | None = None
        self._results_stamp: tuple[str, float, float] | None = None
        self.last_account: dict[str, Any] | None = None
        self.lab_results: list[dict[str, Any]] = []

        self._build_header()
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=12, pady=(8, 12))
        self._build_operation_tab()
        self._build_lab_tab()
        self._build_stats_tab()
        self._build_config_tab()

        self._load_global_form()
        self._refresh_table()
        names = list_profiles()
        if names:
            self.select_profile(names[0])
        self._load_lab_results()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(250, self._poll_processes)
        self.after(800, self._refresh_live)
        if self.migrated:
            self.after(600, lambda: Toast(self, "Configuración actualizada",
                                          "Ahora hay una configuración general común y cada símbolo solo "
                                          "guarda lo suyo. Copia de seguridad en logs/backup_config_*.",
                                          C_BLUE, 12))
        if autostart or bool(load_global_config(BASE_DIR).get("autostart_bots")):
            self.after(2500, lambda: self.start_active(confirm=False))

    # ------------------------------------------------------------- estilo
    def _setup_style(self) -> None:
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        elif "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure(".", font=F_BASE)
        style.configure("TNotebook", background=C_BG)
        style.configure("TNotebook.Tab", padding=(18, 7), font=F_BOLD)
        style.configure("TFrame", background=C_BG)
        style.configure("TLabelframe", background=C_BG)
        style.configure("TLabelframe.Label", font=F_BOLD, foreground=C_TEXT, background=C_BG)
        style.configure("TLabel", background=C_BG, foreground=C_TEXT)
        style.configure("Muted.TLabel", background=C_BG, foreground=C_MUTED, font=F_SMALL)
        style.configure("TCheckbutton", background=C_BG)
        style.configure("Treeview", rowheight=26, font=F_BASE)
        style.configure("Treeview.Heading", font=F_BOLD)
        style.configure("Accent.TButton", font=F_BOLD, padding=(14, 6))
        style.configure("Danger.TButton", foreground=C_RED, padding=(10, 6))
        style.configure("TButton", padding=(10, 5))

    def _build_header(self) -> None:
        bar = tk.Frame(self, bg=C_HEADER, padx=16, pady=10)
        bar.pack(fill="x")
        tk.Label(bar, text="MT5 Bot", bg=C_HEADER, fg="white", font=F_TITLE).pack(side="left")
        tk.Label(bar, text="  Centro de control", bg=C_HEADER, fg="#b8c4d2", font=(FONT, 12)).pack(
            side="left", pady=(3, 0))
        self.header_status = tk.Label(bar, text="", bg=C_HEADER, fg="#a5d6a7", font=F_BOLD)
        self.header_status.pack(side="left", padx=24)
        tk.Label(bar, text="Framework educativo · use cuenta DEMO · no garantiza rentabilidad",
                 bg=C_HEADER, fg="#ffcc80", font=F_SMALL).pack(side="right")

    # ======================================================= pestaña Operación
    def _build_operation_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=10)
        self.tab_operation = tab
        self.notebook.add(tab, text="  Operación  ")

        cards = ttk.Frame(tab)
        cards.pack(fill="x")
        for i in range(4):
            cards.columnconfigure(i, weight=1, uniform="card")
        self.card_account = Card(cards, "Cuenta")
        self.card_day = Card(cards, "Resultado de hoy")
        self.card_positions = Card(cards, "Posiciones abiertas")
        self.card_bots = Card(cards, "Bots")
        for i, card in enumerate((self.card_account, self.card_day, self.card_positions, self.card_bots)):
            card.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 8, 0))

        actions = ttk.Frame(tab, padding=(0, 10, 0, 6))
        actions.pack(fill="x")
        ttk.Button(actions, text="+ Añadir símbolos…", style="Accent.TButton",
                   command=self.add_symbols).pack(side="left")
        ttk.Button(actions, text="Iniciar activos", command=self.start_active).pack(side="left", padx=(8, 2))
        ttk.Button(actions, text="Detener todos", command=self.stop_all).pack(side="left", padx=2)
        ttk.Button(actions, text="Cerrar posiciones de los bots…", style="Danger.TButton",
                   command=self.emergency_close).pack(side="right")

        paned = ttk.PanedWindow(tab, orient="horizontal")
        paned.pack(fill="both", expand=True)

        left = ttk.Frame(paned)
        paned.add(left, weight=3)
        self.table = make_tree(left, [
            ("on", "Activo", 50, "center"), ("symbol", "Símbolo", 80, "w"), ("tf", "TF", 40, "center"),
            ("strategy", "Estrategia", 90, "w"), ("mode", "Modo", 85, "center"),
            ("state", "Estado", 145, "w"), ("position", "Posición", 90, "w"),
            ("pnl", "Resultado", 70, "e"), ("validation", "Validación", 90, "e")], height=16)
        self.table.configure(selectmode="browse")
        self.table.frame.pack(fill="both", expand=True)
        self.table.bind("<<TreeviewSelect>>", self._on_table_select)
        self.table.bind("<Button-1>", self._on_table_click, add="+")
        self.table.bind("<Double-1>", lambda _e: self.edit_profile())
        ttk.Label(left, text="Clic en 'Activo' o en 'Modo' para cambiarlos · doble clic para editar · "
                             "Modo REAL = envía órdenes a la cuenta.", style="Muted.TLabel").pack(
            fill="x", pady=(4, 2))
        row = ttk.Frame(left)
        row.pack(fill="x", pady=(2, 0))
        self.btn_start = ttk.Button(row, text="Iniciar", command=self.start_selected)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(row, text="Detener", command=self.stop_selected)
        self.btn_stop.pack(side="left", padx=4)
        ttk.Button(row, text="Editar…", command=self.edit_profile).pack(side="left", padx=4)
        ttk.Button(row, text="Reiniciar (re-evoluciona)", command=self.restart_selected).pack(side="left", padx=4)
        ttk.Button(row, text="Eliminar", command=self.delete_profile).pack(side="right")

        right = ttk.Frame(paned, padding=(10, 0, 0, 0))
        paned.add(right, weight=2)
        self.detail_title = ttk.Label(right, text="Seleccione un símbolo", font=F_SUB)
        self.detail_title.pack(fill="x")
        box = tk.Frame(right, bg=C_CARD, highlightbackground="#d5dbe3", highlightthickness=1, padx=10, pady=8)
        box.pack(fill="x", pady=(6, 6))
        self.d_state = tk.Label(box, text="", bg=C_CARD, fg=C_TEXT, font=F_BOLD, anchor="w", justify="left")
        self.d_state.pack(fill="x")
        self.d_situation = tk.Label(box, text="", bg=C_CARD, fg=C_TEXT, font=F_SMALL, anchor="w",
                                    justify="left", wraplength=440)
        self.d_situation.pack(fill="x", pady=(2, 0))
        self.d_position = tk.Label(box, text="", bg=C_CARD, fg=C_BLUE, font=F_BOLD, anchor="w",
                                   justify="left", wraplength=440)
        self.d_position.pack(fill="x")
        self.d_bar = tk.Label(box, text="", bg=C_CARD, fg=C_MUTED, font=F_SMALL, anchor="w",
                              justify="left", wraplength=440)
        self.d_bar.pack(fill="x")
        self.d_strategy = tk.Label(box, text="", bg=C_CARD, fg=C_MUTED, font=F_SMALL, anchor="w",
                                   justify="left", wraplength=440)
        self.d_strategy.pack(fill="x")
        box.bind("<Configure>", lambda e: [lbl.configure(wraplength=max(200, e.width - 24)) for lbl in (
            self.d_situation, self.d_position, self.d_bar, self.d_strategy)])

        sub = ttk.Notebook(right)
        sub.pack(fill="both", expand=True)
        log_tab = ttk.Frame(sub, padding=4)
        sub.add(log_tab, text="Log")
        self.log_text = scrolledtext.ScrolledText(log_tab, height=12, font=F_MONO, wrap="word",
                                                  bg="#0f1720", fg="#d7dde5", insertbackground="white",
                                                  relief="flat")
        self.log_text.pack(fill="both", expand=True)
        self.log_text.configure(state="disabled")
        for tag, color in (("error", "#ff6b6b"), ("warning", "#ffb74d"), ("order", "#69f0ae"),
                           ("dry", "#82b1ff"), ("wait", "#8796a8"), ("gui", "#ffd54f"),
                           ("accepted", "#69f0ae")):
            self.log_text.tag_configure(tag, foreground=color)
        self.log_text.tag_configure("order", font=(F_MONO[0], F_MONO[1], "bold"))

        evo_tab = ttk.Frame(sub, padding=4)
        sub.add(evo_tab, text="Evolución")
        self.verdict = tk.Label(evo_tab, text="Sin resultados todavía.", bg="#e3e8ee", fg=C_TEXT, font=F_BOLD,
                                anchor="w", justify="left", padx=8, pady=6, wraplength=440)
        self.verdict.pack(fill="x")
        evo_tab.bind("<Configure>", lambda e: self.verdict.configure(wraplength=max(200, e.width - 24)))
        self.params_tree = make_tree(evo_tab, [("param", "Parámetro", 200, "w"), ("value", "Valor", 80, "e")],
                                     height=5)
        self.params_tree.frame.pack(fill="x", pady=(6, 0))
        self.metrics_tree = make_tree(evo_tab, [("metric", "Métrica", 150, "w"),
                                                ("is", "Entrenamiento", 100, "e"),
                                                ("oos", "Validación", 100, "e")], height=5)
        self.metrics_tree.frame.pack(fill="x", pady=(6, 0))
        self.windows_label = ttk.Label(evo_tab, text="", style="Muted.TLabel", wraplength=440)
        self.windows_label.pack(fill="x", pady=(4, 0))
        self.history_tree = make_tree(evo_tab, [
            ("date", "Fecha", 120, "w"), ("strategy", "Estrategia", 90, "w"),
            ("result", "Resultado", 80, "w"), ("pf", "PF E/V", 90, "e")], height=4)
        self.history_tree.frame.pack(fill="both", expand=True, pady=(6, 0))

    # ===================================================== pestaña Laboratorio
    def _build_lab_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=10)
        self.tab_lab = tab
        self.notebook.add(tab, text="  Laboratorio  ")
        ttk.Label(tab, text="Pruebe muchas combinaciones a la vez con la misma evolución y validación que "
                            "usa el bot, y active solo las que demuestren algo. Usa la Configuración general.",
                  style="Muted.TLabel").pack(fill="x", pady=(0, 8))
        body = ttk.Frame(tab)
        body.pack(fill="both", expand=True)

        left = ttk.LabelFrame(body, text=" Qué analizar ", padding=10)
        left.pack(side="left", fill="y")
        ttk.Label(left, text="Símbolos:", font=F_BOLD).pack(anchor="w")
        self.lab_symbols = tk.Listbox(left, height=9, selectmode="extended", font=F_BASE, width=24,
                                      activestyle="none")
        self.lab_symbols.pack(fill="x", pady=(2, 4))
        srow = ttk.Frame(left)
        srow.pack(fill="x")
        ttk.Button(srow, text="Añadir…", command=self.lab_add_symbols).pack(side="left")
        ttk.Button(srow, text="Quitar", command=self.lab_remove_symbols).pack(side="left", padx=4)
        ttk.Button(srow, text="Mis símbolos", command=self.lab_use_profiles).pack(side="left")
        ttk.Label(left, text="Timeframes:", font=F_BOLD).pack(anchor="w", pady=(10, 0))
        tfrow = ttk.Frame(left)
        tfrow.pack(fill="x")
        self.lab_tfs = {tf: tk.BooleanVar(value=tf in ("M15", "H1")) for tf in ("M5", "M15", "H1", "H4")}
        for tf, var in self.lab_tfs.items():
            ttk.Checkbutton(tfrow, text=tf, variable=var).pack(side="left", padx=(0, 6))
        ttk.Label(left, text="Estrategias:", font=F_BOLD).pack(anchor="w", pady=(10, 0))
        self.lab_strats = {s: tk.BooleanVar(value=True) for s in VALID_STRATEGIES}
        for s, var in self.lab_strats.items():
            ttk.Checkbutton(left, text=STRATEGY_NAMES[s], variable=var).pack(anchor="w")
        brow = ttk.Frame(left)
        brow.pack(fill="x", pady=(10, 0))
        ttk.Label(brow, text="Velas de historial:").pack(side="left")
        self.lab_bars = tk.StringVar(value=str(load_global_config(BASE_DIR).get("history_bars", 8000)))
        ttk.Entry(brow, textvariable=self.lab_bars, width=8).pack(side="left", padx=6)
        self.btn_lab_run = ttk.Button(left, text="Analizar", style="Accent.TButton", command=self.lab_run)
        self.btn_lab_run.pack(fill="x", pady=(14, 4))
        self.btn_lab_stop = ttk.Button(left, text="Detener", command=self.lab_stop, state="disabled")
        self.btn_lab_stop.pack(fill="x")
        self.lab_progress = ttk.Progressbar(left, mode="determinate", length=200)
        self.lab_progress.pack(fill="x", pady=(14, 2))
        self.lab_status = ttk.Label(left, text="", style="Muted.TLabel", wraplength=230)
        self.lab_status.pack(fill="x")

        right = ttk.Frame(body, padding=(10, 0, 0, 0))
        right.pack(side="left", fill="both", expand=True)
        self.lab_tree = make_tree(right, [
            ("ok", "Válida", 50, "center"), ("symbol", "Símbolo", 90, "w"), ("tf", "TF", 45, "center"),
            ("strategy", "Estrategia", 100, "w"), ("pf_oos", "PF valid.", 70, "e"),
            ("trades_oos", "Op. valid.", 70, "e"), ("windows", "Ventanas +", 75, "center"),
            ("pf_is", "PF entreno", 75, "e"), ("win", "% acierto", 70, "e"),
            ("reason", "Motivo", 260, "w")], height=18)
        self.lab_tree.configure(selectmode="extended")
        self.lab_tree.frame.pack(fill="both", expand=True)
        rrow = ttk.Frame(right)
        rrow.pack(fill="x", pady=(6, 0))
        ttk.Button(rrow, text="Activar seleccionados (en simulación)", style="Accent.TButton",
                   command=self.lab_activate).pack(side="left")
        self.lab_summary = ttk.Label(rrow, text="", style="Muted.TLabel")
        self.lab_summary.pack(side="left", padx=12)
        ttk.Label(right, text="Válida = superó la validación walk-forward. Ventanas + = ventanas de validación "
                              "con PF ≥ 1. PF = Profit Factor (ganancias / pérdidas). Ninguna combinación "
                              "garantiza resultados futuros.", style="Muted.TLabel", wraplength=780).pack(
            fill="x", pady=(4, 0))

    # =================================================== pestaña Estadísticas
    def _build_stats_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=10)
        self.tab_stats = tab
        self.notebook.add(tab, text="  Estadísticas  ")
        top = ttk.Frame(tab)
        top.pack(fill="x")
        ttk.Label(top, text="Periodo:").pack(side="left")
        self.stats_days = tk.StringVar(value="30 días")
        ttk.Combobox(top, textvariable=self.stats_days, values=["7 días", "30 días", "90 días", "365 días"],
                     state="readonly", width=10).pack(side="left", padx=6)
        ttk.Button(top, text="Actualizar desde MT5", command=self.refresh_stats).pack(side="left", padx=6)
        self.stats_status = ttk.Label(top, text="Pulse 'Actualizar' para leer las operaciones cerradas de los bots.",
                                      style="Muted.TLabel")
        self.stats_status.pack(side="left", padx=10)
        self.chart = EquityChart(tab, height=230)
        self.chart.pack(fill="x", pady=(10, 10))
        self.stats_tree = make_tree(tab, [
            ("profile", "Perfil", 110, "w"), ("symbol", "Símbolo", 90, "w"),
            ("trades", "Operaciones", 85, "e"), ("win", "% acierto", 75, "e"),
            ("pf", "PF real", 70, "e"), ("pf_val", "PF validación", 95, "e"),
            ("net", "Resultado", 90, "e"), ("dd", "Drawdown máx.", 95, "e"),
            ("verdict", "Valoración", 260, "w")], height=8)
        self.stats_tree.frame.pack(fill="both", expand=True)
        ttk.Label(tab, text="PF real por debajo de ~70% del PF de validación con 10+ operaciones sugiere que "
                            "la estrategia funciona peor en vivo que en el backtest (el bot la pausa solo si "
                            "'Pausar si PF real <' está activo).", style="Muted.TLabel", wraplength=1100).pack(
            fill="x", pady=(6, 0))

    # ======================================== pestaña Configuración general
    def _build_config_tab(self) -> None:
        tab = ttk.Frame(self.notebook)
        self.tab_config = tab
        self.notebook.add(tab, text="  Configuración general  ")
        canvas = tk.Canvas(tab, bg=C_BG, highlightthickness=0)
        scroll = ttk.Scrollbar(tab, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas, padding=12)
        inner.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(int(-e.delta / 120), "units")
                        if self.notebook.select() == str(self.tab_config) else None)

        ttk.Label(inner, text="Esta configuración es común a todos los símbolos. Lo propio de cada símbolo "
                              "(timeframe, estrategia, spread, simulación) se cambia en Operación > Editar. "
                              "Los bots en marcha aplican los cambios al guardar.",
                  style="Muted.TLabel", wraplength=1150).pack(fill="x", pady=(0, 8))
        float_vcmd = (self.register(lambda v: bool(_FLOAT_RE.match(v))), "%P")
        int_vcmd = (self.register(lambda v: bool(_INT_RE.match(v))), "%P")

        basic = ttk.Frame(inner)
        basic.pack(fill="x")
        for i in range(3):
            basic.columnconfigure(i, weight=1, uniform="cfg")
        for col, (name, fields) in enumerate(BASIC_GROUPS):
            box = ttk.LabelFrame(basic, text=f" {name} ", padding=(10, 6))
            box.grid(row=0, column=col, sticky="nsew", padx=(0 if col == 0 else 10, 0))
            box.columnconfigure(1, weight=1)
            for r, fld in enumerate(fields):
                self._build_field(box, r, fld, float_vcmd, int_vcmd)
            if name.startswith("Cuenta"):
                srow = ttk.Frame(box)
                srow.grid(row=len(fields), column=0, columnspan=2, sticky="w", pady=(6, 0))
                ttk.Button(srow, text="Iniciar con Windows", command=self.install_startup).pack(side="left")
                ttk.Button(srow, text="Quitar", command=self.remove_startup).pack(side="left", padx=4)
                ttk.Button(srow, text="Probar Telegram", command=self.test_telegram).pack(side="left", padx=4)

        self.adv_button = ttk.Button(inner, text="+ Mostrar opciones avanzadas", command=self._toggle_advanced)
        self.adv_button.pack(anchor="w", pady=(12, 6))
        self.adv_frame = ttk.Frame(inner)
        for i in range(4):
            self.adv_frame.columnconfigure(i, weight=1, uniform="adv")
        for col, (name, fields) in enumerate(ADVANCED_GROUPS):
            box = ttk.LabelFrame(self.adv_frame, text=f" {name} ", padding=(10, 6))
            box.grid(row=0, column=col, sticky="nsew", padx=(0 if col == 0 else 10, 0))
            box.columnconfigure(1, weight=1)
            for r, fld in enumerate(fields):
                self._build_field(box, r, fld, float_vcmd, int_vcmd)
        self.adv_visible = False

        bottom = ttk.Frame(inner, padding=(0, 12, 0, 0))
        bottom.pack(fill="x", side="bottom")
        ttk.Button(bottom, text="Guardar configuración", style="Accent.TButton",
                   command=self.save_global).pack(side="left")
        ttk.Button(bottom, text="Probar conexión MT5", command=self.test_connection).pack(side="left", padx=8)
        ttk.Button(bottom, text="Valores por defecto", command=self.reset_defaults).pack(side="left")
        self.cfg_status = tk.Label(bottom, text="", bg=C_BG, fg=C_MUTED, font=F_BASE, anchor="w",
                                   justify="left", wraplength=640)
        self.cfg_status.pack(side="left", padx=14, fill="x", expand=True)
        self.cfg_bottom = bottom

    def _toggle_advanced(self) -> None:
        self.adv_visible = not self.adv_visible
        if self.adv_visible:
            self.adv_frame.pack(fill="x", before=self.cfg_bottom)
            self.adv_button.configure(text="− Ocultar opciones avanzadas")
        else:
            self.adv_frame.pack_forget()
            self.adv_button.configure(text="+ Mostrar opciones avanzadas")

    def _build_field(self, box: ttk.LabelFrame, row: int, fld: Field,
                     float_vcmd: tuple[str, str], int_vcmd: tuple[str, str]) -> None:
        if fld.kind == "bool":
            var: tk.Variable = tk.BooleanVar()
            widget: tk.Widget = ttk.Checkbutton(box, text=fld.label, variable=var)
            widget.grid(row=row, column=0, columnspan=2, sticky="w", pady=3)
            Tooltip(widget, fld.help)
        else:
            var = tk.StringVar()
            label = ttk.Label(box, text=fld.label)
            label.grid(row=row, column=0, sticky="w", pady=3)
            Tooltip(label, fld.help)
            if fld.kind == "choice":
                widget = ttk.Combobox(box, textvariable=var, values=list(fld.choices), state="readonly", width=14)
            elif fld.kind == "combo":
                widget = ttk.Combobox(box, textvariable=var, values=list(fld.choices), width=14)
            elif fld.kind == "float":
                widget = ttk.Entry(box, textvariable=var, width=14, validate="key", validatecommand=float_vcmd)
            elif fld.kind in ("int", "optint"):
                widget = ttk.Entry(box, textvariable=var, width=14, validate="key", validatecommand=int_vcmd)
            elif fld.kind == "secret":
                widget = ttk.Entry(box, textvariable=var, width=16, show="•")
            else:
                widget = ttk.Entry(box, textvariable=var, width=16)
            widget.grid(row=row, column=1, sticky="ew", padx=(10, 0), pady=3)
            Tooltip(widget, fld.help)
        var.trace_add("write", self._mark_gdirty)
        self.gvars[fld.key] = var

    # ================================================================ MT5
    def run_mt5(self, require_demo: bool, terminal_path: str | None,
                task: Callable[[Any], dict[str, Any]], done: Callable[[dict[str, Any]], None]) -> None:
        """Ejecuta una tarea con MT5 en segundo plano y llama a ``done`` en la ventana."""
        q: queue.Queue[dict[str, Any]] = queue.Queue()
        self.configure(cursor="watch")
        threading.Thread(target=lambda: q.put(with_mt5(require_demo, terminal_path, task)), daemon=True).start()

        def check() -> None:
            try:
                res = q.get_nowait()
            except queue.Empty:
                self.after(200, check)
                return
            self.configure(cursor="")
            done(res)

        self.after(200, check)

    def _gcfg(self) -> dict[str, Any]:
        try:
            return load_global_config(BASE_DIR)
        except ValueError:
            return dict(DEFAULT_CONFIG)

    def with_symbol_list(self, callback: Callable[[list[dict[str, Any]]], None]) -> None:
        """Llama a ``callback`` con la lista de símbolos del bróker (la carga la primera vez)."""
        if self._symbol_list is not None:
            callback(self._symbol_list)
            return
        g = self._gcfg()
        self.header_status.configure(text="Cargando símbolos del bróker…", fg="#90caf9")

        def done(res: dict[str, Any]) -> None:
            self.header_status.configure(text="")
            if not res.get("ok") or not res.get("symbols"):
                messagebox.showerror("Buscador no disponible", res.get("error", "El bróker no devolvió símbolos."),
                                     parent=self)
                return
            self._symbol_list = res["symbols"]
            callback(self._symbol_list)

        self.run_mt5(bool(g["require_demo"]), g.get("terminal_path") or None,
                     lambda m: {"symbols": m.list_symbols()}, done)

    # ====================================================== tabla de símbolos
    def _refresh_table(self) -> None:
        names = list_profiles()
        tree = self.table
        for iid in tree.get_children():
            if iid not in names:
                tree.delete(iid)
        for name in names:
            cfg = profile_config(name) or {**DEFAULT_CONFIG, **(read_json(config_path_for(name)) or {})}
            st = read_json(profile_paths(config_path_for(name)).status) or {}
            label, color, live = self._state_of(name, st)
            positions = (st.get("positions") or []) if live else []
            pos_txt = " | ".join(f"{p['side']} {fmt(p.get('volume'), '{:g}')}" for p in positions) or "—"
            pnl = sum((p.get("profit") or 0) for p in positions)
            evo = st.get("evolution") or {}
            if not evo:
                ev = read_json(profile_paths(config_path_for(name)).evolved) or {}
                if ev:
                    evo = {"accepted": ev.get("accepted"),
                           "pf_oos": (ev.get("out_of_sample") or {}).get("profit_factor")}
            val = "—" if not evo else f"{'Acept.' if evo.get('accepted') else 'Desc.'} {fmt(evo.get('pf_oos'))}"
            values = ("Sí" if cfg.get("enabled", True) else "No", cfg.get("symbol", "?"), cfg.get("timeframe", "?"),
                      STRATEGY_SHORT.get(cfg.get("strategy", ""), "?"),
                      "Simulación" if cfg.get("simulation", True) else "REAL", label, pos_txt,
                      fmt(pnl, "{:+.2f}") if positions else "—", val)
            tag = {C_GREEN: "ok", C_RED: "bad", C_ORANGE: "warn", C_BLUE: "info"}.get(color, "idle")
            if tree.exists(name):
                tree.item(name, values=values, tags=(tag,))
            else:
                tree.insert("", "end", iid=name, values=values, tags=(tag,))
        if not names:
            self.detail_title.configure(text="Pulse '+ Añadir símbolos…' para empezar")

    def _state_of(self, name: str, st: dict[str, Any]) -> tuple[str, str, bool]:
        """(texto, color, en ejecución) de un perfil."""
        bp = self.procs.get(name)
        phase = st.get("phase", "")
        if bp and bp.running:
            if bp.stop_requested_at:
                return ("Reiniciando…" if bp.restart else "Deteniendo…"), C_ORANGE, True
            label, color = PHASES.get(phase, ("Iniciando…", C_BLUE))
            return label, color, True
        if status_is_fresh(st) and phase in ("operando", "sin_parametros", "detenido_perdida",
                                             "evolucionando", "pausado_rendimiento"):
            return "En marcha (fuera de la ventana)", C_GREEN, True
        if phase in ("descartado", "error"):
            label, color = PHASES[phase]
            return label, color, False
        return "Detenido", C_GREY, False

    def _on_table_select(self, _event: Any = None) -> None:
        sel = self.table.selection()
        if sel and sel[0] != self.selected.get():
            self.select_profile(sel[0])

    def _on_table_click(self, event: Any) -> str | None:
        if self.table.identify("region", event.x, event.y) != "cell":
            return None
        row = self.table.identify_row(event.y)
        col = self.table.identify_column(event.x)
        if not row:
            return None
        if col == "#1":
            self.toggle_profile_key(row, "enabled")
            return "break"
        if col == "#5":
            self.toggle_profile_key(row, "simulation")
            return "break"
        return None

    def toggle_profile_key(self, name: str, key: str, confirm: bool = True) -> None:
        """Cambia 'enabled' o 'simulation' de un perfil (pide confirmación para operar en REAL)."""
        cfg = profile_config(name)
        if cfg is None:
            messagebox.showerror("Perfil inválido", f"Revise la configuración de '{name}' (Editar…).", parent=self)
            return
        new = not bool(cfg.get(key, True))
        if key == "simulation" and not new and confirm:
            running = name in self.procs and self.procs[name].running
            if not messagebox.askyesno(
                    "Activar envío de órdenes",
                    f"{cfg['symbol']} {cfg['timeframe']} pasará a modo REAL: enviará órdenes a la cuenta"
                    f"{' (el bot en marcha lo aplica al instante)' if running else ''}.\n\n¿Continuar?",
                    parent=self, icon="warning"):
                return
        own = {k: cfg[k] for k in PROFILE_KEYS}
        own[key] = new
        try:
            save_profile(own, config_path_for(name))
        except (ValueError, OSError) as exc:
            messagebox.showerror("No se pudo guardar", str(exc), parent=self)
            return
        self._refresh_table()

    def select_profile(self, name: str) -> None:
        """Muestra el detalle del perfil en el panel derecho."""
        self.selected.set(name)
        if self.table.exists(name) and self.table.selection() != (name,):
            self.table.selection_set(name)
        self._results_stamp = None
        self._render_log_buffer()
        self._render_detail()

    # ------------------------------------------------------------ detalle
    def _render_detail(self) -> None:
        name = self.selected.get()
        if not name:
            return
        cfg = profile_config(name) or {}
        paths = profile_paths(config_path_for(name))
        st = read_json(paths.status) or {}
        label, color, live = self._state_of(name, st)
        mode = "Simulación" if cfg.get("simulation", True) else "REAL (envía órdenes)"
        self.detail_title.configure(text=f"{cfg.get('symbol', name)} · {cfg.get('timeframe', '')} · "
                                         f"{STRATEGY_NAMES.get(cfg.get('strategy', ''), '')}")
        code = st.get("exit_code")
        extra = f" · último cierre: {EXIT_MEANING.get(code, '')}" if (code is not None and not live) else ""
        self.d_state.configure(text=f"{label} · {mode} · perfil '{name}'{extra}", fg=color)
        if live:
            message = " | ".join(p for p in str(st.get("message") or "").split(" | ")
                                 if not p.startswith("posición abierta"))
            if st.get("phase") == "evolucionando":
                message = "Buscando y validando parámetros (walk-forward)…"
            if st.get("paused_reason"):
                message = f"PAUSADO: {st['paused_reason']}. {message}"
            self.d_situation.configure(text=message or "Esperando datos del bot…")
        elif st.get("phase") == "descartado":
            self.d_situation.configure(text="La validación descartó los parámetros: este símbolo no opera "
                                            "(protección contra el sobreajuste). Vea la pestaña Evolución.")
        elif st.get("phase") == "error":
            self.d_situation.configure(text=f"Se detuvo: {st.get('message', '')}")
        else:
            self.d_situation.configure(text="Detenido. Pulse 'Iniciar' para arrancarlo.")
        positions = (st.get("positions") or []) if live else []
        self.d_position.configure(text=("Posición: " + " | ".join(
            f"{p['side']} {fmt(p.get('volume'), '{:g}')} @ {fmt_price(p.get('price_open'))} · SL "
            f"{fmt_price(p.get('sl'))} · TP {fmt_price(p.get('tp'))} · {fmt(p.get('profit'), '{:+.2f}')}"
            for p in positions)) if positions else "")
        bar = (st.get("last_bar") or {}) if live else {}
        if bar:
            sig = {1: "COMPRA", -1: "VENTA"}.get(bar.get("signal"), "ninguna")
            self.d_bar.configure(text=f"Última vela {bar.get('time', '')} (servidor): cierre "
                                      f"{fmt_price(bar.get('close'))} · {bar.get('indicators') or ''} · "
                                      f"ATR {fmt_price(bar.get('atr'))} · señal: {sig}")
        else:
            self.d_bar.configure(text="")
        evo = read_json(paths.evolved) or {}
        self.d_strategy.configure(text=("Parámetros: " + describe_params(evo.get("params"))) if evo else "")
        self._render_evolution(paths)

    def _render_evolution(self, paths: Any) -> None:
        stamp = (paths.name, _mtime(paths.evolved), _mtime(paths.history))
        if stamp == self._results_stamp:
            return
        self._results_stamp = stamp
        for tree in (self.params_tree, self.metrics_tree, self.history_tree):
            tree.delete(*tree.get_children())
        evo = read_json(paths.evolved)
        if evo:
            ok = evo.get("accepted")
            self.verdict.configure(
                text=(f"{'ACEPTADO' if ok else 'DESCARTADO'} · "
                      f"{str(evo.get('timestamp', '')).replace('T', ' ')} · "
                      f"{evo.get('validation', '1 corte')} · motor {evo.get('backend', '?')}\n{evo.get('reason', '')}"),
                bg="#e3f4e8" if ok else "#fdebd3", fg=C_GREEN if ok else C_ORANGE)
            for key, value in (evo.get("params") or {}).items():
                self.params_tree.insert("", "end", values=(PARAM_LABELS.get(key, key), fmt(value, "{:g}")))
            is_m, oos_m = evo.get("in_sample") or {}, evo.get("out_of_sample") or {}
            for title, key, spec in (("Profit Factor", "profit_factor", "{:.3f}"), ("Operaciones", "trades", "{}"),
                                     ("% de acierto", "win_rate", "{:.1%}"),
                                     ("Beneficio neto (precio)", "net_profit", "{:.5f}"),
                                     ("Drawdown máx. (precio)", "max_drawdown", "{:.5f}")):
                tag = ()
                if key == "profit_factor" and oos_m.get(key) is not None:
                    tag = ("ok",) if oos_m[key] >= 1 else ("bad",)
                self.metrics_tree.insert("", "end", tags=tag, values=(title, fmt(is_m.get(key), spec),
                                                                      fmt(oos_m.get(key), spec)))
            wins = evo.get("windows") or []
            self.windows_label.configure(text=("Ventanas de validación (PF / operaciones): " + " · ".join(
                f"{fmt(w.get('pf_oos'))} / {w.get('trades_oos')}" for w in wins)) if wins else "")
        else:
            self.verdict.configure(text="Sin resultados todavía: se generan al iniciar el bot.",
                                   bg="#e3e8ee", fg=C_TEXT)
            self.windows_label.configure(text="")
        try:
            lines = paths.history.read_text(encoding="utf-8").splitlines()[-100:]
        except OSError:
            lines = []
        for line in reversed(lines):
            rec = read_json_line(line)
            if rec:
                ok = rec.get("accepted")
                self.history_tree.insert("", "end", tags=("ok",) if ok else ("warn",), values=(
                    str(rec.get("timestamp", "")).replace("T", " ")[:16],
                    STRATEGY_SHORT.get(rec.get("strategy") or "", "Cruce medias"),
                    "Aceptado" if ok else "Descartado", f"{fmt(rec.get('pf_is'))} / {fmt(rec.get('pf_oos'))}"))

    # ------------------------------------------------------------- tarjetas
    def _render_cards(self) -> None:
        statuses = []
        for name in list_profiles():
            st = read_json(profile_paths(config_path_for(name)).status) or {}
            _label, _color, live = self._state_of(name, st)
            statuses.append((name, st, live))
        live_st = [st for _n, st, live in statuses if live]
        fresh = sorted((st for st in live_st if st.get("account")), key=lambda s: s.get("updated", ""))
        acc = (fresh[-1]["account"] if fresh else None) or self.last_account
        g = self._gcfg()
        if acc:
            self.card_account.set(f"{acc.get('equity', 0):,.2f}", C_TEXT,
                                  f"Equity · balance {acc.get('balance', 0):,.2f}",
                                  "Solo cuenta DEMO" if g.get("require_demo") else ("Permite cuenta REAL", C_RED))
        else:
            self.card_account.set("—", C_MUTED, "Se muestra con un bot en marcha",
                                  "o con 'Probar conexión MT5'")
        if fresh:
            st = fresh[-1]
            pnl = st.get("day_pnl_percent") or 0.0
            scope = "solo bots" if st.get("daily_loss_scope") == "bots" else "cuenta"
            bots = st.get("bots_pnl") or {}
            halted = any(s.get("phase") == "detenido_perdida" for s in live_st)
            self.card_day.set(f"{pnl:+.2f}%", C_RED if halted or pnl < 0 else (C_GREEN if pnl > 0 else C_TEXT),
                              f"{scope} · límite −{fmt(st.get('max_daily_loss_percent'), '{:g}')}%",
                              ("DETENIDO por pérdida diaria", C_RED) if halted else
                              (f"cerrado {fmt(bots.get('realized'), '{:+.2f}')} · abierto "
                               f"{fmt(bots.get('floating'), '{:+.2f}')}" if bots else ""))
        else:
            self.card_day.set("—", C_MUTED, f"límite −{g.get('max_daily_loss_percent')}% "
                                            f"({g.get('daily_loss_scope')})")
        positions = [p for st in live_st for p in (st.get("positions") or [])]
        open_pnl = sum((p.get("profit") or 0) for p in positions)
        limit = int(g.get("max_open_positions_total") or 0)
        self.card_positions.set(f"{len(positions)}" + (f" / {limit}" if limit else ""), C_TEXT,
                                (f"resultado abierto {open_pnl:+.2f}",
                                 C_GREEN if open_pnl > 0 else (C_RED if open_pnl < 0 else C_MUTED)),
                                "límite entre todos los símbolos" if limit else "sin límite global")
        running = sum(1 for p in self.procs.values() if p.running)
        cfgs = [profile_config(n) or {} for n in list_profiles()]
        enabled = sum(1 for c in cfgs if c.get("enabled", True))
        real = sum(1 for c in cfgs if not c.get("simulation", True))
        self.card_bots.set(f"{running} en marcha", C_GREEN if running else C_MUTED,
                           f"{len(statuses)} símbolos · {enabled} activos",
                           (f"{real} en modo REAL", C_RED) if real else "todos en simulación")
        lab = "laboratorio analizando…" if self.lab_proc and self.lab_proc.running else ""
        text = f"{running} bot(s) en marcha" if running else ""
        self.header_status.configure(text=" · ".join(t for t in (text, lab) if t), fg="#a5d6a7")

    # ================================================================ acciones
    def add_symbols(self) -> None:
        """Buscador -> crea un perfil por símbolo elegido."""
        existing = {str((read_json(config_path_for(n)) or {}).get("symbol", "")) for n in list_profiles()}

        def open_browser(symbols: list[dict[str, Any]]) -> None:
            dialog = SymbolBrowser(self, symbols, "create", existing)
            self.wait_window(dialog)
            if dialog.result:
                created = self.create_profiles([
                    {"symbol": d["name"], "timeframe": dialog.result["timeframe"],
                     "strategy": dialog.result["strategy"], "spread": d.get("spread"),
                     "simulation": dialog.result["simulation"]} for d in dialog.result["symbols"]])
                self._report_created(created)

        self.with_symbol_list(open_browser)

    def create_profiles(self, items: list[dict[str, Any]]) -> list[str]:
        """Crea perfiles (símbolo, timeframe, estrategia, spread) y devuelve líneas de resumen."""
        existing = set(list_profiles())
        combos = {(c.get("symbol"), c.get("timeframe"), c.get("strategy"))
                  for c in (profile_config(n) or {} for n in existing)}
        magic = next_magic()
        out = []
        for it in items:
            key = (it["symbol"], it["timeframe"], it["strategy"])
            if key in combos:
                out.append(f"• {it['symbol']} {it['timeframe']} {STRATEGY_SHORT.get(it['strategy'], '')}: ya existe")
                continue
            name = profile_name_for_symbol(it["symbol"], it["timeframe"], existing, it["strategy"])
            if name is None:
                out.append(f"• {it['symbol']}: no se encontró un nombre de perfil libre")
                continue
            spread = it.get("spread")
            data = {"symbol": it["symbol"], "timeframe": it["timeframe"], "strategy": it["strategy"],
                    "max_spread": round_sig(spread * 2.5) if spread else DEFAULT_CONFIG["max_spread"],
                    "magic": magic, "enabled": True, "simulation": bool(it.get("simulation", True))}
            try:
                save_profile(data, config_path_for(name))
            except (ValueError, OSError) as exc:
                out.append(f"• {it['symbol']}: {exc}")
                continue
            existing.add(name)
            combos.add(key)
            note = (f"spread máx {data['max_spread']:g}" if spread
                    else "sin cotización: revise el spread máximo con Editar")
            out.append(f"+ {it['symbol']} {it['timeframe']} · {STRATEGY_SHORT[it['strategy']]} · "
                       f"{'simulación' if data['simulation'] else 'REAL'} · {note}")
            magic += 1
        self._refresh_table()
        return out

    def _report_created(self, lines: list[str]) -> None:
        if not lines:
            return
        total = len(list_profiles())
        g = self._gcfg()
        messagebox.showinfo(
            "Símbolos", "\n".join(lines) + f"\n\nAhora hay {total} símbolos. Cada uno arriesga "
            f"{g['risk_percent']:g}% por operación y como máximo habrá {g['max_open_positions_total']} "
            "posiciones a la vez.\nPulse 'Iniciar activos' para arrancarlos.", parent=self)

    def edit_profile(self) -> None:
        name = self.selected.get()
        if not name:
            return
        dialog = ProfileDialog(self, name)
        self.wait_window(dialog)
        if dialog.saved:
            self._results_stamp = None
            self._refresh_table()
            self._render_detail()

    def delete_profile(self) -> None:
        name = self.selected.get()
        if not name:
            return
        if name in self.procs and self.procs[name].running:
            messagebox.showwarning("Bot en marcha", "Detenga primero el bot de este símbolo.", parent=self)
            return
        cfg = profile_config(name) or {}
        if not messagebox.askyesno("Eliminar símbolo",
                                   f"¿Eliminar '{name}' ({cfg.get('symbol', '')} {cfg.get('timeframe', '')})?\n"
                                   "Se borra su configuración; logs e historial se conservan.", parent=self):
            return
        paths = profile_paths(config_path_for(name))
        for path in (paths.config, paths.evolved):
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                messagebox.showerror("Error", str(exc), parent=self)
                return
        self.procs.pop(name, None)
        if self.table.exists(name):
            self.table.delete(name)
        names = list_profiles()
        self.selected.set("")
        if names:
            self.select_profile(names[0])

    # ------------------------------------------------------------ arrancar
    def _launch(self, name: str) -> bool:
        cfg = profile_config(name)
        if cfg is None:
            messagebox.showerror("Configuración inválida", f"Revise '{name}' con Editar…", parent=self)
            return False
        bp = BotProcess(name, config_path_for(name))
        mode = "SIMULACIÓN" if cfg.get("simulation", True) else "ÓRDENES REALES"
        bp.buffer.append(f"### {datetime.now():%H:%M:%S} Iniciando · {cfg['symbol']} {cfg['timeframe']} · "
                         f"{STRATEGY_NAMES.get(cfg['strategy'], '')} · {mode}")
        try:
            bp.start()
        except OSError as exc:
            messagebox.showerror("No se pudo iniciar", f"{name}: {exc}", parent=self)
            return False
        self.procs[name] = bp
        return True

    def _confirm_real(self, names: list[str]) -> bool:
        real = [n for n in names if not (profile_config(n) or {}).get("simulation", True)]
        if not real:
            return True
        return messagebox.askyesno(
            "Envío de órdenes", "Estos símbolos enviarán órdenes REALES a la cuenta:\n"
            + "\n".join(f"• {n}" for n in real) + "\n\n¿Continuar?", parent=self, icon="warning")

    def start_selected(self) -> None:
        name = self.selected.get()
        if not name or (name in self.procs and self.procs[name].running):
            return
        if self._confirm_real([name]) and self._launch(name):
            self._render_log_buffer()

    def start_active(self, confirm: bool = True) -> None:
        """Inicia todos los perfiles marcados como Activo que no estén en marcha."""
        names = [n for n in list_profiles()
                 if (profile_config(n) or {}).get("enabled", True)
                 and not (n in self.procs and self.procs[n].running)]
        if not names:
            if confirm:
                messagebox.showinfo("Iniciar activos", "No hay símbolos activos detenidos.", parent=self)
            return
        if confirm and not self._confirm_real(names):
            return
        started = sum(self._launch(n) for n in names)
        self._render_log_buffer()
        Toast(self, "Bots iniciados", f"{started} símbolo(s) arrancando: evolucionan y validan sus "
                                      "parámetros antes de operar.", C_GREEN)

    def stop_selected(self) -> None:
        bp = self.procs.get(self.selected.get())
        if bp and bp.running:
            bp.request_stop()
            self._append(bp, f"### {datetime.now():%H:%M:%S} Deteniendo… (las posiciones abiertas se "
                             "mantienen con su SL/TP)")

    def restart_selected(self) -> None:
        name = self.selected.get()
        bp = self.procs.get(name)
        if bp and bp.running:
            bp.restart = True
            bp.request_stop()
            self._append(bp, f"### {datetime.now():%H:%M:%S} Reiniciando para re-evolucionar…")
        else:
            self.start_selected()

    def stop_all(self) -> None:
        for bp in self.procs.values():
            if bp.running and not bp.stop_requested_at:
                bp.request_stop()
                self._append(bp, f"### {datetime.now():%H:%M:%S} Deteniendo…")

    def emergency_close(self) -> None:
        """Detiene todos los bots y cierra TODAS las posiciones abiertas con magic de los bots."""
        magics = {c["magic"] for c in (profile_config(n) for n in list_profiles()) if c}
        if not magics:
            return
        if not messagebox.askyesno(
                "Cerrar posiciones de los bots",
                "Se detendrán TODOS los bots y se cerrarán a mercado todas sus posiciones abiertas "
                f"(magics {', '.join(map(str, sorted(magics)))}).\nSus operaciones manuales no se tocan."
                "\n\n¿Continuar?", parent=self, icon="warning"):
            return
        self.stop_all()
        g = self._gcfg()

        def done(res: dict[str, Any]) -> None:
            if res.get("ok"):
                Toast(self, "Cierre de emergencia",
                      f"{res['closed']} posición(es) cerrada(s)"
                      + (f", {res['failed']} con error (revise MT5)" if res.get("failed") else ""),
                      C_ORANGE if res.get("failed") else C_GREEN, 12)
            else:
                messagebox.showerror("Cierre de emergencia", res.get("error", "Error"), parent=self)

        def task(mt5c: Any) -> dict[str, Any]:
            closed, failed = mt5c.close_positions_by_magics(magics, int(g.get("deviation", 20)))
            return {"closed": closed, "failed": failed}

        self.run_mt5(False, g.get("terminal_path") or None, task, done)

    # ------------------------------------------------------------------ log
    @staticmethod
    def _line_tag(line: str) -> str | None:
        if line.startswith("###"):
            return "gui"
        if "ORDEN EJECUTADA" in line or "Posición cerrada" in line:
            return "order"
        if "[ERROR]" in line or "Traceback" in line:
            return "error"
        if "[DRY-RUN]" in line:
            return "dry"
        if "[ACEPTADO]" in line or "Parámetros ACEPTADOS" in line:
            return "accepted"
        if "[WARNING]" in line or "[DESCARTADO]" in line:
            return "warning"
        if "En espera |" in line:
            return "wait"
        return None

    def _append(self, bp: BotProcess, line: str) -> None:
        bp.buffer.append(line)
        if len(bp.buffer) > MAX_LOG_LINES:
            del bp.buffer[: len(bp.buffer) - MAX_LOG_LINES]
        if bp.profile == self.selected.get():
            self._insert_log([line])

    def _insert_log(self, lines: list[str], clear: bool = False) -> None:
        text = self.log_text
        at_end = text.yview()[1] > 0.98
        text.configure(state="normal")
        if clear:
            text.delete("1.0", "end")
        for line in lines:
            text.insert("end", line + "\n", self._line_tag(line) or ())
        excess = int(text.index("end-1c").split(".")[0]) - MAX_LOG_LINES
        if excess > 0:
            text.delete("1.0", f"{excess + 1}.0")
        text.configure(state="disabled")
        if at_end or clear:
            text.see("end")

    def _render_log_buffer(self) -> None:
        name = self.selected.get()
        bp = self.procs.get(name)
        if bp and bp.buffer:
            self._insert_log(bp.buffer, clear=True)
            return
        if not name:
            self._insert_log([], clear=True)
            return
        log_path = LOG_DIR / profile_paths(config_path_for(name)).log_file
        lines = ["### Este símbolo no se ha iniciado desde esta ventana."]
        try:
            tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-150:]
            if tail:
                lines += [f"### Últimas líneas de {log_path.name}:"] + tail
        except OSError:
            pass
        self._insert_log(lines, clear=True)

    def _toast_for(self, bp: BotProcess, line: str) -> None:
        """Avisos emergentes para los eventos importantes del log de un bot."""
        cfg = profile_config(bp.profile) or {}
        who = f"{cfg.get('symbol', bp.profile)} {cfg.get('timeframe', '')}"
        msg = line.split(": ", 2)[-1] if ": " in line else line
        if "ORDEN EJECUTADA" in line:
            Toast(self, f"Orden ejecutada · {who}", msg, C_GREEN, 12)
        elif "Posición cerrada" in line:
            positive = "resultado +" in line
            Toast(self, f"Posición cerrada · {who}", msg, C_GREEN if positive else C_RED, 12)
        elif "[DRY-RUN]" in line and ("BUY" in line or "SELL" in line):
            Toast(self, f"Señal (simulación) · {who}", msg, C_BLUE)
        elif "TRADING DETENIDO" in line:
            Toast(self, "Detenido por pérdida diaria", msg, C_RED, 15)
        elif "PAUSADO por rendimiento" in line:
            Toast(self, f"Pausado · {who}", msg, C_ORANGE, 15)

    # ------------------------------------------------------------- bucles
    def _poll_processes(self) -> None:
        """Lee la salida de bots y laboratorio y detecta cuándo terminan (cada 250 ms)."""
        changed = False
        for bp in list(self.procs.values()):
            batch: list[str] = []
            try:
                while len(batch) < 500:
                    batch.append(bp.lines.get_nowait())
            except queue.Empty:
                pass
            for line in batch:
                self._append(bp, line)
                if len(batch) < 50:
                    self._toast_for(bp, line)
            if bp.running and bp.stop_requested_at and \
                    time.monotonic() - bp.stop_requested_at > STOP_TIMEOUT_SECONDS:
                bp.kill()
                self._append(bp, "### No respondió a tiempo: proceso terminado a la fuerza.")
            if not bp.running and not bp.reported and bp.lines.empty():
                bp.reported = True
                code = bp.exit_code
                self._append(bp, f"### {datetime.now():%H:%M:%S} Terminó · código {code}: "
                                 f"{EXIT_MEANING.get(code if code is not None else -1, 'terminado')}")
                if code not in (0, None) and not self._closing:
                    cfg = profile_config(bp.profile) or {}
                    Toast(self, f"{cfg.get('symbol', bp.profile)} se detuvo",
                          EXIT_MEANING.get(code, "terminado"), C_ORANGE if code == 4 else C_RED, 10)
                if bp.restart and not self._closing:
                    bp.restart = False
                    self._launch(bp.profile)
                    if bp.profile == self.selected.get():
                        self._render_log_buffer()
                changed = True
        if self.lab_proc is not None:
            self._poll_lab()
        if changed:
            self._refresh_table()
        if self._closing and not any(p.running for p in self.procs.values()) and \
                not (self.lab_proc and self.lab_proc.running):
            self.destroy()
            return
        self.after(250, self._poll_processes)

    def _refresh_live(self) -> None:
        try:
            self._refresh_table()
            self._render_cards()
            self._render_detail()
            name = self.selected.get()
            bp = self.procs.get(name)
            running = bool(bp and bp.running)
            self.btn_start.configure(state="disabled" if running or not name else "normal")
            self.btn_stop.configure(state="normal" if running and bp and not bp.stop_requested_at else "disabled")
        except Exception:  # noqa: BLE001 - nunca romper el bucle de la ventana
            logger.exception("Error actualizando la ventana")
        self.after(1500, self._refresh_live)

    # ============================================================ laboratorio
    def lab_add_symbols(self) -> None:
        current = set(self.lab_symbols.get(0, "end"))

        def open_browser(symbols: list[dict[str, Any]]) -> None:
            dialog = SymbolBrowser(self, symbols, "pick", current)
            self.wait_window(dialog)
            if dialog.result:
                for d in dialog.result["symbols"]:
                    if d["name"] not in current:
                        self.lab_symbols.insert("end", d["name"])
                        current.add(d["name"])

        self.with_symbol_list(open_browser)

    def lab_remove_symbols(self) -> None:
        for idx in reversed(self.lab_symbols.curselection()):
            self.lab_symbols.delete(idx)

    def lab_use_profiles(self) -> None:
        current = set(self.lab_symbols.get(0, "end"))
        for n in list_profiles():
            sym = (profile_config(n) or {}).get("symbol")
            if sym and sym not in current:
                self.lab_symbols.insert("end", sym)
                current.add(sym)

    def lab_run(self) -> None:
        symbols = list(self.lab_symbols.get(0, "end"))
        tfs = [tf for tf, v in self.lab_tfs.items() if v.get()]
        strats = [s for s, v in self.lab_strats.items() if v.get()]
        if not symbols or not tfs or not strats:
            messagebox.showinfo("Laboratorio", "Elija al menos un símbolo, un timeframe y una estrategia.",
                                parent=self)
            return
        try:
            bars = int(self.lab_bars.get())
        except ValueError:
            bars = 0
        total = len(symbols) * len(tfs) * len(strats)
        if total > 60 and not messagebox.askyesno(
                "Laboratorio", f"Son {total} combinaciones; puede tardar varios minutos. ¿Continuar?",
                parent=self):
            return
        cmd = [python_for_children(), "-u", str(BASE_DIR / "lab.py"), "--symbols", ",".join(symbols),
               "--timeframes", ",".join(tfs), "--strategies", ",".join(strats)]
        if bars > 0:
            cmd += ["--bars", str(bars)]
        self.lab_results = []
        self.lab_tree.delete(*self.lab_tree.get_children())
        self.lab_progress.configure(value=0, maximum=max(1, total))
        self.lab_status.configure(text="Iniciando…")
        bp = BotProcess("laboratorio", BASE_DIR / "lab.py", cmd=cmd, stop_flag_path=LAB_STOP_FLAG)
        try:
            bp.start()
        except OSError as exc:
            messagebox.showerror("Laboratorio", str(exc), parent=self)
            return
        self.lab_proc = bp
        self.btn_lab_run.configure(state="disabled")
        self.btn_lab_stop.configure(state="normal")

    def lab_stop(self) -> None:
        if self.lab_proc and self.lab_proc.running:
            self.lab_proc.request_stop()
            self.lab_status.configure(text="Deteniendo después de las combinaciones en curso…")

    def _poll_lab(self) -> None:
        bp = self.lab_proc
        if bp is None:
            return
        try:
            while True:
                line = bp.lines.get_nowait()
                if line.startswith("LAB_PROGRESS "):
                    info = read_json_line(line[len("LAB_PROGRESS "):]) or {}
                    if info.get("total"):
                        self.lab_progress.configure(maximum=info["total"], value=info.get("done", 0))
                    self.lab_status.configure(text=info.get("message", ""))
                elif line.startswith("LAB_RESULT "):
                    res = read_json_line(line[len("LAB_RESULT "):])
                    if res:
                        self.lab_results.append(res)
                        self._render_lab_results()
                elif "[ERROR]" in line or "Traceback" in line:
                    self.lab_status.configure(text=line[-200:])
        except queue.Empty:
            pass
        if not bp.running and bp.lines.empty() and not bp.reported:
            bp.reported = True
            self.btn_lab_run.configure(state="normal")
            self.btn_lab_stop.configure(state="disabled")
            if bp.exit_code not in (0, None):
                self.lab_status.configure(text=f"El laboratorio terminó con error (código {bp.exit_code}). "
                                               "Vea logs/lab.log.")
            else:
                accepted = sum(1 for r in self.lab_results if r.get("accepted"))
                Toast(self, "Laboratorio terminado",
                      f"{accepted} de {len(self.lab_results)} combinaciones superaron la validación.",
                      C_GREEN if accepted else C_ORANGE, 12)

    def _render_lab_results(self) -> None:
        from lab import score

        self.lab_results.sort(key=score, reverse=True)
        tree = self.lab_tree
        selected = set(tree.selection())
        tree.delete(*tree.get_children())
        for r in self.lab_results:
            iid = f"{r['symbol']}|{r['timeframe']}|{r['strategy']}"
            if tree.exists(iid):
                continue
            ok = r.get("accepted")
            tree.insert("", "end", iid=iid, tags=("ok",) if ok else ("idle",), values=(
                "Sí" if ok else "No", r["symbol"], r["timeframe"], STRATEGY_SHORT.get(r["strategy"], r["strategy"]),
                fmt(r.get("pf_oos")), fmt(r.get("trades_oos"), "{}"), r.get("windows") or "—",
                fmt(r.get("pf_is")), fmt(r.get("win_rate_oos"), "{:.0%}"),
                "" if ok else str(r.get("reason", ""))[:120]))
        keep = [i for i in selected if tree.exists(i)]
        if keep:
            tree.selection_set(keep)
        accepted = sum(1 for r in self.lab_results if r.get("accepted"))
        self.lab_summary.configure(text=f"{len(self.lab_results)} combinaciones · {accepted} aceptadas")

    def _load_lab_results(self) -> None:
        data = read_json(LAB_RESULTS_PATH) or {}
        self.lab_results = list(data.get("results") or [])
        if self.lab_results:
            self._render_lab_results()
            self.lab_status.configure(text=f"Últimos resultados: {str(data.get('started', '')).replace('T', ' ')}")
        for s in data.get("symbols") or []:
            self.lab_symbols.insert("end", s)

    def lab_activate(self) -> None:
        selected = set(self.lab_tree.selection())
        chosen = [r for r in self.lab_results if f"{r['symbol']}|{r['timeframe']}|{r['strategy']}" in selected]
        if not chosen:
            messagebox.showinfo("Laboratorio", "Seleccione una o varias filas (mejor las válidas).",
                                parent=self)
            return
        rejected = [r for r in chosen if not r.get("accepted")]
        if rejected and not messagebox.askyesno(
                "Combinaciones descartadas", f"{len(rejected)} de las elegidas NO superaron la validación. "
                "¿Activarlas igualmente (en simulación)?", parent=self):
            return
        lines = self.create_profiles([{"symbol": r["symbol"], "timeframe": r["timeframe"],
                                       "strategy": r["strategy"], "spread": r.get("spread"),
                                       "simulation": True} for r in chosen])
        self.notebook.select(self.tab_operation)
        self._report_created(lines)

    # ========================================================== estadísticas
    def refresh_stats(self) -> None:
        by_magic: dict[int, tuple[str, dict[str, Any]]] = {}
        for n in list_profiles():
            cfg = profile_config(n)
            if cfg:
                by_magic[cfg["magic"]] = (n, cfg)
        if not by_magic:
            self.stats_status.configure(text="No hay símbolos configurados.")
            return
        days = int(self.stats_days.get().split()[0])
        self.stats_status.configure(text="Leyendo operaciones de MT5…")
        g = self._gcfg()

        def done(res: dict[str, Any]) -> None:
            if not res.get("ok"):
                self.stats_status.configure(text=res.get("error", "Error"))
                return
            self._render_stats(res["deals"], by_magic, days)

        self.run_mt5(bool(g["require_demo"]), g.get("terminal_path") or None,
                     lambda m: {"deals": m.get_bot_deals(set(by_magic), days=days, limit=None)}, done)

    def _render_stats(self, deals: list[dict[str, Any]], by_magic: dict[int, tuple[str, dict[str, Any]]],
                      days: int) -> None:
        deals = sorted(deals, key=lambda d: d.get("close_ts", 0))
        self.chart.set_data([d["close_time"][:10] for d in deals], [d["profit"] for d in deals])
        tree = self.stats_tree
        tree.delete(*tree.get_children())
        groups: dict[int, list[dict[str, Any]]] = {}
        for d in deals:
            groups.setdefault(d.get("magic", 0), []).append(d)

        def stats(rows: list[dict[str, Any]]) -> tuple[int, float, float, float, float]:
            profits = [r["profit"] for r in rows]
            gains = sum(p for p in profits if p > 0)
            losses = -sum(p for p in profits if p <= 0)
            pf = gains / losses if losses > 0 else (float("inf") if gains > 0 else 0.0)
            acc, peak, dd = 0.0, 0.0, 0.0
            for p in profits:
                acc += p
                peak = max(peak, acc)
                dd = max(dd, peak - acc)
            wins = sum(1 for p in profits if p > 0)
            return len(profits), (wins / len(profits) if profits else 0.0), pf, sum(profits), dd

        for magic, (name, cfg) in by_magic.items():
            rows = groups.get(magic, [])
            n, win, pf, net, dd = stats(rows)
            evo = read_json(profile_paths(config_path_for(name)).evolved) or {}
            pf_val = (evo.get("out_of_sample") or {}).get("profit_factor")
            if n == 0:
                verdict, tag = "sin operaciones en el periodo", "idle"
            elif n < 10:
                verdict, tag = "muestra pequeña: aún no es concluyente", "info"
            elif pf_val and pf < 0.7 * min(pf_val, 2.5):
                verdict, tag = "rinde por debajo de lo esperado en la validación", "bad"
            elif pf >= 1:
                verdict, tag = "en línea o mejor de lo esperado", "ok"
            else:
                verdict, tag = "en pérdidas", "warn"
            tree.insert("", "end", tags=(tag,), values=(
                name, cfg["symbol"], n, fmt(win if n else None, "{:.0%}"),
                "∞" if pf == float("inf") else fmt(pf if n else None), fmt(pf_val),
                fmt(net if n else None, "{:+.2f}"), fmt(dd if n else None, "{:.2f}"), verdict))
        n, win, pf, net, dd = stats(deals)
        pf_txt = "∞" if pf == float("inf") else f"{pf:.2f}"
        self.stats_status.configure(text=f"Últimos {days} días: {n} operaciones · {win:.0%} acierto · "
                                         f"PF {pf_txt} · resultado {net:+.2f} · drawdown {dd:.2f}")

    # =================================================== configuración general
    def _load_global_form(self) -> None:
        try:
            cfg = load_global_config(BASE_DIR)
        except ValueError as exc:
            cfg = dict(DEFAULT_CONFIG)
            self._set_cfg_status(f"global_config.json inválido, se muestran valores por defecto: {exc}", C_RED)
        self._fill_global(cfg)

    def _fill_global(self, cfg: dict[str, Any]) -> None:
        self._loading = True
        for fld in ALL_FIELDS:
            value = cfg.get(fld.key, DEFAULT_CONFIG.get(fld.key))
            var = self.gvars[fld.key]
            if fld.kind == "bool":
                var.set(bool(value))
            elif fld.kind == "optint":
                var.set("" if value is None else str(value))
            elif fld.kind == "float":
                var.set(f"{float(value):.6f}".rstrip("0").rstrip("."))
            elif fld.key == "trading_hours":
                var.set(value or "24 h")
            else:
                var.set(str(value))
        self._loading = False
        self.gdirty = False

    def _mark_gdirty(self, *_args: Any) -> None:
        if not self._loading:
            self.gdirty = True
            self._set_cfg_status("Hay cambios sin guardar.", C_ORANGE)

    def _set_cfg_status(self, text: str, color: str = C_MUTED) -> None:
        self.cfg_status.configure(text=text, fg=color)

    def collect_global(self) -> dict[str, Any]:
        """Lee el formulario de la configuración general; ValueError si algo es inválido."""
        data = dict(self._gcfg())
        for fld in ALL_FIELDS:
            raw = self.gvars[fld.key].get()
            if fld.kind == "bool":
                data[fld.key] = bool(raw)
                continue
            raw = str(raw).strip()
            if fld.kind == "optint":
                data[fld.key] = int(raw) if raw else None
            elif fld.key == "trading_hours":
                data[fld.key] = "" if raw.lower().replace(" ", "") in ("", "24h", "24") else raw
            elif fld.kind in ("str", "secret", "choice", "combo"):
                data[fld.key] = raw
            else:
                if not raw:
                    raise ValueError(f"El campo '{fld.label}' está vacío")
                try:
                    data[fld.key] = float(raw) if fld.kind == "float" else int(raw)
                except ValueError as exc:
                    raise ValueError(f"Valor inválido en '{fld.label}': {raw!r}") from exc
        errors = validate_config(data)
        if errors:
            raise ValueError("\n".join(f"• {e}" for e in errors))
        return data

    def save_global(self, quiet: bool = False) -> bool:
        try:
            data = self.collect_global()
            save_global_config(data, BASE_DIR)
        except (ValueError, OSError) as exc:
            self._set_cfg_status("No se guardó: revise los campos.", C_RED)
            messagebox.showerror("Configuración inválida", str(exc), parent=self)
            return False
        self.gdirty = False
        running = sum(1 for p in self.procs.values() if p.running)
        extra = f" Los {running} bots en marcha aplican los cambios." if running else ""
        self._set_cfg_status(f"Configuración general guardada.{extra}", C_GREEN)
        if not quiet:
            Toast(self, "Configuración guardada", f"Se aplica a todos los símbolos.{extra}", C_GREEN)
        return True

    def reset_defaults(self) -> None:
        if messagebox.askyesno("Valores por defecto", "¿Reemplazar el formulario por los valores por defecto?\n"
                               "(No se guarda hasta pulsar 'Guardar configuración'.)", parent=self):
            self._fill_global(DEFAULT_CONFIG)
            self.gdirty = True
            self._set_cfg_status("Valores por defecto cargados (sin guardar).", C_ORANGE)

    def test_connection(self) -> None:
        self._set_cfg_status("Conectando con MetaTrader 5…", C_BLUE)

        def task(mt5c: Any) -> dict[str, Any]:
            acc = mt5c.mt5.account_info()
            return {"login": acc.login, "server": acc.server, "balance": acc.balance,
                    "equity": acc.equity, "currency": acc.currency}

        def done(res: dict[str, Any]) -> None:
            if not res.get("ok"):
                self._set_cfg_status(res.get("error", "Error"), C_RED)
                return
            self.last_account = {"balance": res["balance"], "equity": res["equity"]}
            self._set_cfg_status(f"Conexión OK · {res['login']} ({res['server']}) · balance {res['balance']:.2f} "
                                 f"{res['currency']} · equity {res['equity']:.2f}", C_GREEN)

        self.run_mt5(bool(self.gvars["require_demo"].get()),
                     str(self.gvars["terminal_path"].get()).strip() or None, task, done)

    def test_telegram(self) -> None:
        if self.gdirty and not self.save_global(quiet=True):
            return
        from trading_bot import Notifier

        g = self._gcfg()
        if not g.get("telegram_token") or not g.get("telegram_chat_id"):
            messagebox.showinfo("Telegram", "Escriba el token y el chat id, y guarde.", parent=self)
            return
        Notifier(lambda: g, lambda: "[MT5Bot]").send("Prueba de avisos del Centro de control: OK")
        self._set_cfg_status("Mensaje de prueba enviado a Telegram (si no llega, revise token y chat id).", C_BLUE)

    def install_startup(self) -> None:
        path = startup_shortcut_path()
        if path is None:
            messagebox.showinfo("Iniciar con Windows", "Solo disponible en Windows.", parent=self)
            return
        content = (f'@echo off\r\ncd /d "{BASE_DIR}"\r\n'
                   f'start "" "{pythonw_executable()}" bot_gui.py --autostart\r\n')
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except OSError as exc:
            messagebox.showerror("Iniciar con Windows", str(exc), parent=self)
            return
        messagebox.showinfo("Iniciar con Windows",
                            "Listo: al iniciar sesión en Windows se abrirá el Centro de control e iniciará "
                            "los símbolos activos.\nMetaTrader 5 también debe abrirse e iniciar sesión "
                            "(puede añadirlo a Inicio de Windows).", parent=self)

    def remove_startup(self) -> None:
        path = startup_shortcut_path()
        if path is None or not path.exists():
            messagebox.showinfo("Iniciar con Windows", "No hay acceso de inicio instalado.", parent=self)
            return
        path.unlink(missing_ok=True)
        messagebox.showinfo("Iniciar con Windows", "Acceso de inicio eliminado.", parent=self)

    # ================================================================ cierre
    def on_close(self) -> None:
        if self.gdirty:
            answer = messagebox.askyesnocancel("Cambios sin guardar",
                                               "¿Guardar la configuración general antes de salir?", parent=self)
            if answer is None:
                return
            if answer and not self.save_global(quiet=True):
                return
        active = [p for p in self.procs.values() if p.running]
        lab_running = bool(self.lab_proc and self.lab_proc.running)
        if not active and not lab_running:
            self.destroy()
            return
        if not messagebox.askokcancel(
                "Bots en ejecución",
                f"Hay {len(active)} bot(s) en marcha{' y el laboratorio' if lab_running else ''}. Se detendrán "
                "antes de cerrar.\nLas posiciones abiertas se mantienen en MT5 con su SL/TP.\n\n¿Salir?",
                parent=self):
            return
        self._closing = True
        for p in active:
            p.restart = False
            p.request_stop()
        if lab_running and self.lab_proc:
            self.lab_proc.request_stop()
        self.title("MT5 Bot — cerrando…")


def _enable_dpi_awareness() -> None:
    """Texto nítido en pantallas con escalado en Windows."""
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass


def main(on_ready: Callable[[ControlCenter], None] | None = None) -> None:
    """Abre el Centro de control (bloqueante hasta cerrar la ventana)."""
    setup_logging("bot_gui.log")
    _enable_dpi_awareness()
    app = ControlCenter(autostart="--autostart" in sys.argv)
    if on_ready:
        on_ready(app)
    app.mainloop()


if __name__ == "__main__":
    main()
