"""
bot_gui.py
==========
Centro de control gráfico del MT5 Bot (tkinter, sin dependencias extra).

* **Perfiles:** varias configuraciones (``risk_config.json`` y
  ``risk_config_<nombre>.json``), cada una con su magic number, log, resultados
  y su propio proceso del bot. Se pueden ejecutar varios perfiles a la vez.
* **Panel:** iniciar/detener el bot, estado, cuenta, mercado, posición abierta
  y log en vivo con colores.
* **Configuración:** todos los parámetros agrupados, con validación.
* **Resultados:** última evolución (parámetros y métricas), historial de
  evoluciones y operaciones cerradas por el bot.

Uso:
    python bot_gui.py          (o start_bot.bat, que instala dependencias y lo abre)

El bot se ejecuta como proceso independiente (``trading_bot.py --config ...``).
La ventana lee su salida y el archivo de estado ``logs/status_<perfil>.json``, y
lo detiene de forma ordenada creando ``logs/stop_<perfil>.flag``.
"""

from __future__ import annotations

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
from tkinter import messagebox, scrolledtext, simpledialog, ttk
from typing import Any, Callable

from bot_common import (
    BASE_DIR, DEFAULT_CONFIG, LOG_DIR, MAIN_PROFILE, PROFILE_NAME_RE, VALID_STRATEGIES,
    VALID_TIMEFRAMES,
    config_path_for, list_profiles, load_config, profile_paths, read_json, save_config,
    setup_logging, validate_config,
)

logger = logging.getLogger("bot_gui")

# ---------------------------------------------------------------------------
# Estilo
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
F_CARD_TITLE = (FONT, 9, "bold")
F_BIG = (FONT, 14, "bold")
F_MONO = ("Consolas", 9)

EXIT_MEANING = {
    0: "detenido correctamente",
    1: "error inesperado (ver log)",
    2: "configuración inválida, símbolo inexistente o cuenta no DEMO",
    3: "no se pudo conectar con MetaTrader 5",
    4: "parámetros descartados por la validación walk-forward",
}

PHASES: dict[str, tuple[str, str]] = {
    "iniciando": ("Iniciando…", C_BLUE),
    "conectando": ("Conectando con MT5…", C_BLUE),
    "evolucionando": ("Evolucionando parámetros…", C_BLUE),
    "operando": ("Operando", C_GREEN),
    "sin_parametros": ("Sin parámetros válidos", C_ORANGE),
    "detenido_perdida": ("Detenido por pérdida diaria", C_RED),
    "descartado": ("Parámetros descartados", C_ORANGE),
    "error": ("Error", C_RED),
    "finalizado": ("Detenido", C_GREY),
}

STRATEGY_NAMES = {
    "cruce_medias": "Cruce de medias + RSI",
    "reversion_media": "Reversión a la media (Bollinger)",
    "ruptura": "Ruptura de rango",
}

PARAM_LABELS = {
    "bb_period": "Periodo Bollinger (velas)",
    "bb_std": "Ancho bandas (desv. típicas)",
    "rsi_low": "RSI máximo para comprar",
    "rsi_high": "RSI mínimo para vender",
    "lookback": "Rango de ruptura (velas)",
    "trend": "Media de tendencia (velas)",
    "fast": "Media rápida (velas)",
    "slow": "Media lenta (velas)",
    "rsi_buy": "RSI mínimo para comprar",
    "rsi_sell": "RSI máximo para vender",
    "sl_mult": "Stop loss (× ATR)",
    "tp_mult": "Take profit (× ATR)",
}

STOP_TIMEOUT_SECONDS = 20
MAX_LOG_LINES = 3000


# ---------------------------------------------------------------------------
# Campos de configuración
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Field:
    """Un campo editable de risk_config.json."""

    key: str
    label: str
    kind: str                     # float | int | str | bool | choice | optint
    help: str = ""
    choices: tuple[str, ...] = ()


FIELD_GROUPS: list[tuple[str, list[Field]]] = [
    ("Mercado", [
        Field("symbol", "Símbolo", "str", "Nombre exacto en MT5 (distingue mayúsculas: US500, EURUSD.m...)"),
        Field("timeframe", "Timeframe", "choice", "Duración de cada vela", VALID_TIMEFRAMES),
        Field("magic", "Magic number", "int", "Identifica las órdenes de este perfil. Debe ser distinto en cada perfil"),
        Field("require_demo", "Solo cuenta DEMO", "bool", "Aborta si la cuenta conectada no es demo"),
    ]),
    ("Riesgo", [
        Field("risk_percent", "Riesgo por operación (%)", "float", "% del equity que se arriesga en cada operación"),
        Field("max_daily_loss_percent", "Pérdida diaria máxima (%)", "float", "Al alcanzarla no abre más operaciones hasta el día siguiente"),
        Field("daily_loss_scope", "Pérdida diaria cuenta", "choice",
              "bots (por defecto): mide solo el resultado de hoy de las operaciones de los bots "
              "(cerradas + abiertas); sus operaciones manuales no los bloquean. "
              "cuenta: mide el equity de TODA la cuenta, incluidas las operaciones manuales",
              ("bots", "cuenta")),
        Field("min_lot", "Lote mínimo", "float", "Límite inferior de lote (además del del bróker)"),
        Field("max_lot", "Lote máximo", "float", "Límite superior de lote"),
        Field("max_spread", "Spread máximo (precio)", "float", "ask − bid máximo. ~0.0002 en EURUSD, ~1.0 en US500"),
        Field("max_open_positions_total", "Máx. posiciones (todos)", "int",
              "Máximo de posiciones abiertas sumando TODOS los perfiles/símbolos. 0 = sin límite"),
    ]),
    ("Evolución", [
        Field("strategy", "Estrategia", "choice",
              "cruce_medias: tendencia (M15/H1). reversion_media: vuelve a la media tras salir de "
              "las bandas de Bollinger (corto plazo). ruptura: rompe el máximo/mínimo de N velas "
              "a favor de la tendencia", VALID_STRATEGIES),
        Field("history_bars", "Velas de historial", "int", "Velas usadas para evolucionar y validar"),
        Field("population", "Población", "int", "Combinaciones por generación"),
        Field("generations", "Generaciones", "int", "Iteraciones del algoritmo genético"),
        Field("min_trades", "Mín. operaciones (entreno)", "int", "Menos operaciones penaliza el fitness y descarta"),
        Field("min_trades_oos", "Mín. operaciones (validación)", "int", "Mínimo de operaciones en el 30% final"),
        Field("fitness_folds", "Tramos de fitness", "int", "Puntúa por el peor tramo del entrenamiento (1 = desactivado)"),
        Field("evolve_sl_tp", "Evolucionar SL y TP", "bool", "El algoritmo elige también los multiplicadores de stop loss y take profit"),
        Field("evolver_seed", "Semilla", "optint", "Vacío = aleatoria. Con un número, el resultado es reproducible"),
        Field("evolver_backend", "Motor genético", "choice", "auto usa strategy-genome y, si falla, deap",
              ("auto", "strategy-genome", "deap")),
    ]),
    ("Validación y costes", [
        Field("train_ratio", "Proporción entrenamiento", "float", "0.7 = 70% entrena / 30% valida"),
        Field("oos_pf_ratio_min", "PF validación / entreno mín.", "float", "Descarta si PF validación < este ratio × PF entrenamiento"),
        Field("min_pf_in_sample", "PF entrenamiento mín.", "float", "Profit Factor mínimo en el entrenamiento"),
        Field("commission_per_trade", "Comisión por operación (precio)", "float", "Se suma al spread histórico en el backtest"),
    ]),
    ("Ejecución", [
        Field("sl_atr_mult", "Stop loss fijo (× ATR)", "float", "Solo si 'Evolucionar SL y TP' está desactivado"),
        Field("tp_atr_mult", "Take profit fijo (× ATR)", "float", "Solo si 'Evolucionar SL y TP' está desactivado"),
        Field("atr_period", "Periodo ATR", "int", "Velas del ATR"),
        Field("loop_seconds", "Revisión cada (s)", "int", "Frecuencia del bucle del bot"),
        Field("heartbeat_seconds", "Mensaje de espera cada (s)", "int", "0 = sin mensajes de espera"),
        Field("deviation", "Deslizamiento máx. (puntos)", "int", "Desviación permitida al enviar órdenes"),
        Field("close_positions_on_error", "Cerrar posiciones si hay error", "bool", "Ante un error inesperado cierra las posiciones del perfil"),
        Field("terminal_path", "Ruta terminal64.exe", "str", "Opcional, si hay varias instalaciones de MT5"),
    ]),
]
ALL_FIELDS: list[Field] = [f for _, group in FIELD_GROUPS for f in group]

_FLOAT_RE = re.compile(r"^\d*\.?\d*$")
_INT_RE = re.compile(r"^\d*$")


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


def python_for_children() -> str:
    """Intérprete para lanzar el bot (python.exe aunque la GUI corra con pythonw.exe)."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        candidate = exe.with_name("python.exe")
        if candidate.exists():
            return str(candidate)
    return str(exe)


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
    if "sl_mult" in params:
        text += (f" · SL {fmt(params.get('sl_mult'), '{:.2f}')}× / "
                 f"TP {fmt(params.get('tp_mult'), '{:.2f}')}× ATR")
    return text


def test_connection(symbol: str, require_demo: bool, terminal_path: str | None) -> dict[str, Any]:
    """
    Conecta a MT5 (en un hilo aparte), lee la cuenta y el símbolo, y desconecta.

    :returns: dict con ok, error, login, server, balance, equity, currency,
              symbol_found, suggestions, spread, volume_min.
    """
    out: dict[str, Any] = {"ok": False}
    try:
        import mt5_connector as mt5c  # import diferido: la GUI abre aunque falte MT5

        if not mt5c.connect_mt5_with_retry(
            max_retries=2, wait_seconds=1, require_demo=require_demo,
            terminal_path=terminal_path or None,
        ):
            out["error"] = "No se pudo conectar. ¿Está MT5 abierto y con sesión iniciada?"
            return out
        acc = mt5c.mt5.account_info()
        out.update(ok=True, login=acc.login, server=acc.server, balance=acc.balance,
                   equity=acc.equity, currency=acc.currency)
        if symbol:
            if mt5c.ensure_symbol(symbol):
                spec = mt5c.get_symbol_spec(symbol)
                out.update(symbol_found=True, spread=mt5c.get_spread(symbol),
                           volume_min=spec.volume_min if spec else None)
            else:
                out.update(symbol_found=False, suggestions=mt5c.find_similar_symbols(symbol))
    except SystemExit as exc:      # cuenta real con "Solo cuenta DEMO"
        out["error"] = str(exc)
    except Exception as exc:       # noqa: BLE001 - se muestra en la GUI
        logger.exception("Error probando conexión")
        out["error"] = f"Error: {exc}"
    finally:
        try:
            import mt5_connector as mt5c

            mt5c.safe_shutdown()
        except Exception:  # noqa: BLE001
            pass
    return out


def round_sig(value: float, digits: int = 2) -> float:
    """Redondea a ``digits`` cifras significativas (0.000237 -> 0.00024)."""
    if value <= 0:
        return value
    import math

    return round(value, -int(math.floor(math.log10(value))) + digits - 1)


def profile_name_for_symbol(symbol: str, timeframe: str, existing: set[str]) -> str | None:
    """Nombre de perfil libre para un símbolo: 'gbpusd', o 'gbpusd_m5' si ya existe."""
    base = re.sub(r"[^a-z0-9_-]+", "_", symbol.lower()).strip("_-")[:24] or "simbolo"
    for name in (base, f"{base}_{timeframe.lower()}"):
        if name != MAIN_PROFILE and PROFILE_NAME_RE.match(name) and name not in existing:
            return name
    return None


def inspect_symbols(symbols: list[str], require_demo: bool, terminal_path: str | None) -> dict[str, Any]:
    """
    Comprueba en MT5 (en un hilo aparte) qué símbolos existen y su spread actual.
    Corrige mayúsculas/sufijos evidentes (us500 -> US500).

    :returns: {"ok": bool, "error": str, "symbols": {entrada: {"name", "spread", "suggestions"}}}
    """
    out: dict[str, Any] = {"ok": False, "symbols": {}}
    try:
        import mt5_connector as mt5c

        if not mt5c.connect_mt5_with_retry(max_retries=2, wait_seconds=1,
                                           require_demo=require_demo,
                                           terminal_path=terminal_path or None):
            out["error"] = "No se pudo conectar con MetaTrader 5."
            return out
        out["ok"] = True
        for sym in symbols:
            name: str | None = sym if mt5c.ensure_symbol(sym) else None
            suggestions: list[str] = []
            if name is None:
                suggestions = mt5c.find_similar_symbols(sym)
                same = [x for x in suggestions
                        if mt5c._normalize_symbol(x) == mt5c._normalize_symbol(sym)]
                if same and mt5c.ensure_symbol(same[0]):
                    name = same[0]
            spread = mt5c.get_spread(name) if name else None
            out["symbols"][sym] = {"name": name, "suggestions": suggestions,
                                   "spread": spread if spread == spread else None}
    except SystemExit as exc:
        out.update(ok=False, error=str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception("Error comprobando símbolos")
        out.update(ok=False, error=f"Error: {exc}")
    finally:
        try:
            import mt5_connector as mt5c

            mt5c.safe_shutdown()
        except Exception:  # noqa: BLE001
            pass
    return out


def load_symbols(require_demo: bool, terminal_path: str | None) -> dict[str, Any]:
    """Carga (en un hilo aparte) la lista de símbolos del bróker para el buscador."""
    out: dict[str, Any] = {"ok": False, "symbols": []}
    try:
        import mt5_connector as mt5c

        if not mt5c.connect_mt5_with_retry(max_retries=2, wait_seconds=1,
                                           require_demo=require_demo,
                                           terminal_path=terminal_path or None):
            out["error"] = "No se pudo conectar con MetaTrader 5. ¿Está abierto y con sesión iniciada?"
            return out
        out.update(ok=True, symbols=mt5c.list_symbols())
    except SystemExit as exc:
        out["error"] = str(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Error cargando símbolos")
        out["error"] = f"Error: {exc}"
    finally:
        try:
            import mt5_connector as mt5c

            mt5c.safe_shutdown()
        except Exception:  # noqa: BLE001
            pass
    return out


#: Términos en español -> cómo suelen aparecer en los nombres/descripciones del bróker.
SEARCH_SYNONYMS: dict[str, tuple[str, ...]] = {
    "oro": ("gold", "xau"), "plata": ("silver", "xag"), "petroleo": ("oil", "wti", "brent"),
    "petróleo": ("oil", "wti", "brent"), "crudo": ("oil", "wti", "brent"), "gas": ("natgas", "ngas"),
    "sp500": ("us500", "spx"), "s&p": ("us500", "spx"), "nasdaq": ("nas", "ustec", "us100"),
    "dow": ("us30", "wall"), "dax": ("ger", "de40", "de30"), "bitcoin": ("btc",),
    "ethereum": ("eth",), "euro": ("eur",), "dolar": ("usd",), "dólar": ("usd",),
    "libra": ("gbp",), "yen": ("jpy",), "franco": ("chf",), "cobre": ("copper",),
}


def symbol_matches(item: dict[str, Any], query: str) -> bool:
    """True si TODAS las palabras de la búsqueda aparecen en el nombre o la descripción."""
    text = f"{item.get('name', '')} {item.get('description', '')} {item.get('path', '')}".lower()
    for word in query.lower().split():
        options = (word,) + SEARCH_SYNONYMS.get(word, ())
        if not any(opt in text for opt in options):
            return False
    return True


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
        tk.Label(self.tip, text=self.text, bg="#fffbe6", fg=C_TEXT, font=F_SMALL,
                 relief="solid", borderwidth=1, padx=6, pady=3, wraplength=320,
                 justify="left").pack()

    def _hide(self, _event: Any = None) -> None:
        if self.tip:
            self.tip.destroy()
            self.tip = None


class Card(tk.Frame):
    """Tarjeta blanca con título y líneas de texto actualizables."""

    def __init__(self, master: tk.Widget, title: str, lines: int = 3) -> None:
        super().__init__(master, bg=C_CARD, highlightbackground="#d5dbe3",
                         highlightthickness=1, padx=12, pady=8)
        tk.Label(self, text=title.upper(), bg=C_CARD, fg=C_MUTED, font=F_CARD_TITLE,
                 anchor="w").pack(fill="x")
        self.main = tk.Label(self, text="—", bg=C_CARD, fg=C_TEXT, font=F_BIG, anchor="w",
                             justify="left")
        self.main.pack(fill="x", pady=(2, 2))
        self.lines: list[tk.Label] = []
        for _ in range(lines):
            lbl = tk.Label(self, text="", bg=C_CARD, fg=C_MUTED, font=F_SMALL, anchor="w",
                           justify="left", wraplength=250)
            lbl.pack(fill="x")
            self.lines.append(lbl)

    def set(self, main: str, color: str = C_TEXT, *lines: tuple[str, str] | str) -> None:
        """Actualiza el texto principal y las líneas secundarias (texto o (texto, color))."""
        self.main.configure(text=main, fg=color)
        for i, lbl in enumerate(self.lines):
            item = lines[i] if i < len(lines) else ""
            text, col = (item, C_MUTED) if isinstance(item, str) else item
            lbl.configure(text=text, fg=col)


class MultiSymbolDialog(tk.Toplevel):
    """Pide una lista de símbolos y el timeframe para crear un perfil por símbolo."""

    def __init__(self, master: tk.Tk, timeframe: str, strategy: str = "cruce_medias") -> None:
        super().__init__(master)
        self.title("Operar varios símbolos")
        self.configure(bg=C_BG, padx=16, pady=14)
        self.resizable(False, False)
        self.transient(master)
        self.result: tuple[list[str], str, bool, str] | None = None
        ttk.Label(self, text="Se creará un perfil por símbolo, copiando la configuración del "
                             "perfil actual,\n"
                             "con su propio magic number. Después podrá iniciarlos "
                             "todos a la vez.", justify="left").pack(anchor="w")
        ttk.Label(self, text="Símbolos (separados por comas):", font=F_BOLD).pack(anchor="w", pady=(12, 2))
        self.symbols = tk.StringVar(value="GBPUSD, USDJPY, US500")
        entry = ttk.Entry(self, textvariable=self.symbols, width=52)
        entry.pack(anchor="w")
        entry.focus_set()
        row = ttk.Frame(self)
        row.pack(anchor="w", pady=(10, 0))
        ttk.Label(row, text="Timeframe:").pack(side="left")
        self.tf = tk.StringVar(value=timeframe)
        ttk.Combobox(row, textvariable=self.tf, values=list(VALID_TIMEFRAMES), state="readonly",
                     width=6).pack(side="left", padx=6)
        ttk.Label(row, text="   Estrategia:").pack(side="left")
        self.strategy = tk.StringVar(value=strategy)
        ttk.Combobox(row, textvariable=self.strategy, values=list(VALID_STRATEGIES),
                     state="readonly", width=16).pack(side="left", padx=6)
        self.auto_spread = tk.BooleanVar(value=True)
        ttk.Checkbutton(self, text="Comprobar los símbolos en MT5 y ajustar el spread máximo de "
                                   "cada uno (recomendado)", variable=self.auto_spread).pack(
            anchor="w", pady=(10, 0))
        buttons = ttk.Frame(self)
        buttons.pack(anchor="e", pady=(14, 0))
        ttk.Button(buttons, text="Cancelar", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="Crear perfiles", style="Accent.TButton",
                   command=self._ok).pack(side="right", padx=8)
        self.bind("<Return>", lambda _e: self._ok())
        self.bind("<Escape>", lambda _e: self.destroy())
        self.update_idletasks()  # centrar sobre la ventana principal
        x = master.winfo_rootx() + (master.winfo_width() - self.winfo_reqwidth()) // 2
        y = master.winfo_rooty() + (master.winfo_height() - self.winfo_reqheight()) // 3
        self.geometry(f"+{max(0, x)}+{max(0, y)}")
        self.grab_set()

    def _ok(self) -> None:
        items = [s.strip() for s in self.symbols.get().replace(";", ",").split(",") if s.strip()]
        unique = list(dict.fromkeys(items))
        if not unique:
            messagebox.showerror("Sin símbolos", "Escriba al menos un símbolo.", parent=self)
            return
        self.result = (unique, self.tf.get(), bool(self.auto_spread.get()), self.strategy.get())
        self.destroy()


class SymbolBrowser(tk.Toplevel):
    """
    Buscador de símbolos del bróker.

    * ``multi=True``: selección múltiple para crear un perfil por símbolo
      (con timeframe, estrategia y ajuste de spread).
    * ``multi=False``: elegir un único símbolo para el perfil actual.
    """

    def __init__(self, master: tk.Tk, symbols: list[dict[str, Any]], multi: bool,
                 existing: set[str], timeframe: str = "M15", strategy: str = "cruce_medias") -> None:
        super().__init__(master)
        self.title("Buscador de símbolos" + (" — crear perfiles" if multi else ""))
        self.configure(bg=C_BG, padx=14, pady=12)
        self.geometry("900x600")
        self.minsize(760, 480)
        self.transient(master)
        self.symbols = symbols
        self.by_name = {d["name"]: d for d in symbols}
        self.multi = multi
        self.existing = existing
        self.result: dict[str, Any] | None = None
        self._after_id: str | None = None

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
        ttk.Label(self, text="Escriba parte del nombre o la descripción, en inglés o español: "
                             "eur, jpy, oro, nasdaq, dax, bitcoin…"
                  + ("  ·  Ctrl/Mayús + clic para elegir varios." if multi else
                     "  ·  Doble clic para elegir."),
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
        if multi:
            ttk.Label(bottom, text="Timeframe:").pack(side="left")
            self.tf = tk.StringVar(value=timeframe)
            ttk.Combobox(bottom, textvariable=self.tf, values=list(VALID_TIMEFRAMES),
                         state="readonly", width=6).pack(side="left", padx=(4, 12))
            ttk.Label(bottom, text="Estrategia:").pack(side="left")
            self.strategy = tk.StringVar(value=strategy)
            ttk.Combobox(bottom, textvariable=self.strategy, values=list(VALID_STRATEGIES),
                         state="readonly", width=16).pack(side="left", padx=(4, 12))
            self.auto_spread = tk.BooleanVar(value=True)
            ttk.Checkbutton(bottom, text="Ajustar spread máximo de cada uno",
                            variable=self.auto_spread).pack(side="left")
        ttk.Button(bottom, text="Cancelar", command=self.destroy).pack(side="right")
        self.ok_button = ttk.Button(bottom, style="Accent.TButton", command=self._ok,
                                    text="Crear perfiles" if multi else "Usar este símbolo")
        self.ok_button.pack(side="right", padx=8)

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
        query = self.query.get().strip()
        cat = self.category.get()
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
            has_profile = d["name"] in self.existing
            note = "ya tiene perfil" if has_profile else ("en Observación" if d.get("visible") else "")
            tag = "existing" if has_profile else ("visible" if d.get("visible") else "")
            self.tree.insert("", "end", iid=d["name"], tags=(tag,) if tag else (), values=(
                d["name"], d["description"], d["category"],
                fmt(d.get("spread"), "{:.6g}", "sin cotización"),
                fmt(d.get("volume_min"), "{:g}"), note))
            shown += 1
        keep = [i for i in selected if self.tree.exists(i)]
        if keep:
            self.tree.selection_set(keep)
        self._shown = shown
        self._update_count()

    def _update_count(self) -> None:
        n_sel = len(self.tree.selection())
        self.count.configure(text=f"{getattr(self, '_shown', 0)} de {len(self.symbols)} símbolos"
                                  + (f" · {n_sel} seleccionado(s)" if n_sel else ""))
        if self.multi:
            self.ok_button.configure(text=f"Crear perfiles ({n_sel})" if n_sel else "Crear perfiles")

    def _ok(self) -> None:
        chosen = [self.by_name[i] for i in self.tree.selection() if i in self.by_name]
        if not chosen:
            messagebox.showinfo("Buscador de símbolos", "Seleccione al menos un símbolo.", parent=self)
            return
        if self.multi:
            self.result = {"symbols": chosen, "timeframe": self.tf.get(),
                           "strategy": self.strategy.get(), "auto_spread": bool(self.auto_spread.get())}
        else:
            self.result = {"symbols": chosen[:1]}
        self.destroy()


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
    tree.tag_configure("ok", foreground=C_GREEN)
    tree.tag_configure("bad", foreground=C_RED)
    tree.tag_configure("warn", foreground=C_ORANGE)
    return tree


# ---------------------------------------------------------------------------
# Proceso del bot
# ---------------------------------------------------------------------------
@dataclass
class BotProcess:
    """Un proceso de trading_bot.py lanzado desde la ventana."""

    profile: str
    config_path: Path
    dry_run: bool
    proc: subprocess.Popen[str] | None = None
    lines: queue.Queue[str] = field(default_factory=queue.Queue)
    buffer: list[str] = field(default_factory=list)
    stop_requested_at: float | None = None
    reported: bool = False

    def start(self) -> None:
        """Lanza el bot como proceso hijo, sin ventana de consola."""
        cmd = [python_for_children(), "-u", str(BASE_DIR / "trading_bot.py"),
               "--config", str(self.config_path)]
        if self.dry_run:
            cmd.append("--dry-run")
        env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0  # type: ignore[attr-defined]
        self.proc = subprocess.Popen(
            cmd, cwd=str(BASE_DIR), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=flags,
        )
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self) -> None:
        """Hilo que lee la salida del bot línea a línea."""
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
        """Pide al bot que se detenga creando el archivo de parada."""
        flag = profile_paths(self.config_path).stop_flag
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
    """Centro de control: perfiles, panel, configuración y resultados."""

    def __init__(self) -> None:
        """Construye la ventana y carga el primer perfil."""
        super().__init__()
        self.title("MT5 Bot — Centro de control")
        self.geometry("1180x780")
        self.minsize(1020, 700)
        self.configure(bg=C_BG)
        self._setup_style()

        self.procs: dict[str, BotProcess] = {}
        self.profile = tk.StringVar()
        self.dry_run = tk.BooleanVar(value=False)
        self.vars: dict[str, tk.Variable] = {}
        self.dirty = False
        self._loading = False
        self._conn_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self._closing = False
        self._results_stamp: tuple[float, float] | None = None
        self._symbols_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self._symbol_list: list[dict[str, Any]] | None = None   # caché del buscador
        self._load_queue: queue.Queue[dict[str, Any]] = queue.Queue()

        self._build_header()
        self._build_profile_bar()
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self._build_dashboard_tab()
        self._build_overview_tab()
        self._build_config_tab()
        self._build_results_tab()

        self._refresh_profiles(select=MAIN_PROFILE)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(250, self._poll_processes)
        self.after(1000, self._refresh_live)

    # ------------------------------------------------------------- estilo
    def _setup_style(self) -> None:
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        elif "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure(".", font=F_BASE)
        style.configure("TNotebook", background=C_BG)
        style.configure("TNotebook.Tab", padding=(16, 6), font=F_BOLD)
        style.configure("TFrame", background=C_BG)
        style.configure("Card.TFrame", background=C_CARD)
        style.configure("TLabelframe", background=C_BG)
        style.configure("TLabelframe.Label", font=F_BOLD, foreground=C_TEXT, background=C_BG)
        style.configure("TLabel", background=C_BG, foreground=C_TEXT)
        style.configure("Muted.TLabel", background=C_BG, foreground=C_MUTED, font=F_SMALL)
        style.configure("TCheckbutton", background=C_BG)
        style.configure("Treeview", rowheight=24, font=F_BASE)
        style.configure("Treeview.Heading", font=F_BOLD)
        style.configure("Accent.TButton", font=F_BOLD, padding=(14, 6))
        style.configure("TButton", padding=(10, 5))

    def _build_header(self) -> None:
        bar = tk.Frame(self, bg=C_HEADER, padx=16, pady=10)
        bar.pack(fill="x")
        tk.Label(bar, text="MT5 Bot", bg=C_HEADER, fg="white", font=F_TITLE).pack(side="left")
        tk.Label(bar, text="  Centro de control", bg=C_HEADER, fg="#b8c4d2",
                 font=(FONT, 12)).pack(side="left", pady=(3, 0))
        tk.Label(bar, text="Framework educativo · úselo en cuenta DEMO · no garantiza rentabilidad",
                 bg=C_HEADER, fg="#ffcc80", font=F_SMALL).pack(side="right")

    def _build_profile_bar(self) -> None:
        bar = ttk.Frame(self, padding=(12, 10, 12, 8))
        bar.pack(fill="x")
        ttk.Label(bar, text="Perfil:", font=F_BOLD).pack(side="left")
        self.profile_box = ttk.Combobox(bar, textvariable=self.profile, state="readonly", width=22)
        self.profile_box.pack(side="left", padx=(6, 6))
        self.profile_box.bind("<<ComboboxSelected>>", self._on_profile_selected)
        ttk.Button(bar, text="Nuevo perfil…", command=self.new_profile).pack(side="left", padx=2)
        ttk.Button(bar, text="Eliminar", command=self.delete_profile).pack(side="left", padx=2)
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=10)
        ttk.Button(bar, text="Buscar símbolos…", command=lambda: self.browse_symbols(multi=True)).pack(
            side="left", padx=2)
        ttk.Button(bar, text="Iniciar todos", command=self.start_all).pack(side="left", padx=2)
        ttk.Button(bar, text="Detener todos", command=self.stop_all).pack(side="left", padx=2)
        self.running_label = ttk.Label(bar, text="", style="Muted.TLabel")
        self.running_label.pack(side="right")
        self._prev_profile = ""

    # ------------------------------------------------------------ pestaña Panel
    def _build_dashboard_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(tab, text="  Panel  ")

        cards = ttk.Frame(tab)
        cards.pack(fill="x")
        for i in range(4):
            cards.columnconfigure(i, weight=1, uniform="card")
        self.card_state = Card(cards, "Estado del bot")
        self.card_account = Card(cards, "Cuenta")
        self.card_market = Card(cards, "Mercado")
        self.card_strategy = Card(cards, "Estrategia evolucionada")
        for i, card in enumerate((self.card_state, self.card_account, self.card_market,
                                  self.card_strategy)):
            card.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 8, 0))

        controls = ttk.Frame(tab, padding=(0, 12, 0, 6))
        controls.pack(fill="x")
        self.btn_start = ttk.Button(controls, text="Iniciar bot", style="Accent.TButton",
                                    command=self.start_bot)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(controls, text="Detener", command=self.stop_bot)
        self.btn_stop.pack(side="left", padx=8)
        ttk.Checkbutton(controls, text="Modo simulación (calcula todo pero no envía órdenes)",
                        variable=self.dry_run).pack(side="left", padx=8)
        ttk.Button(controls, text="Abrir carpeta de logs", command=self.open_logs).pack(side="right")

        situation = tk.Frame(tab, bg=C_CARD, highlightbackground="#d5dbe3", highlightthickness=1,
                             padx=12, pady=8)
        situation.pack(fill="x", pady=(0, 8))
        tk.Label(situation, text="SITUACIÓN", bg=C_CARD, fg=C_MUTED, font=F_CARD_TITLE,
                 anchor="w").pack(fill="x")
        self.situation_label = tk.Label(situation, text="El bot está detenido.", bg=C_CARD,
                                        fg=C_TEXT, font=F_BASE, anchor="w", justify="left",
                                        wraplength=1080)
        self.situation_label.pack(fill="x")
        self.position_label = tk.Label(situation, text="", bg=C_CARD, fg=C_BLUE, font=F_BOLD,
                                       anchor="w", justify="left", wraplength=1080)
        self.position_label.pack(fill="x")
        self.bar_label = tk.Label(situation, text="", bg=C_CARD, fg=C_MUTED, font=F_SMALL,
                                  anchor="w", justify="left", wraplength=1080)
        self.bar_label.pack(fill="x")

        log_frame = ttk.LabelFrame(tab, text=" Log en vivo ", padding=6)
        log_frame.pack(fill="both", expand=True)
        self.log_text = scrolledtext.ScrolledText(log_frame, height=12, font=F_MONO, wrap="word",
                                                  bg="#0f1720", fg="#d7dde5",
                                                  insertbackground="white", relief="flat")
        self.log_text.pack(fill="both", expand=True)
        self.log_text.configure(state="disabled")
        for tag, color in (("error", "#ff6b6b"), ("warning", "#ffb74d"), ("order", "#69f0ae"),
                           ("dry", "#82b1ff"), ("wait", "#8796a8"), ("gui", "#ffd54f"),
                           ("accepted", "#69f0ae"), ("rejected", "#ffb74d")):
            self.log_text.tag_configure(tag, foreground=color)
        self.log_text.tag_configure("order", font=(F_MONO[0], F_MONO[1], "bold"))

    # ------------------------------------------------- pestaña Todos los símbolos
    def _build_overview_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=12)
        self.tab_overview = tab
        self.notebook.add(tab, text="  Todos los símbolos  ")
        top = ttk.Frame(tab)
        top.pack(fill="x", pady=(0, 8))
        ttk.Label(top, text="Cada fila es un perfil (símbolo + timeframe) con su propio bot. "
                            "Doble clic para ver su panel.", style="Muted.TLabel").pack(side="left")
        ttk.Button(top, text="Detener todos", command=self.stop_all).pack(side="right")
        ttk.Button(top, text="Iniciar todos", style="Accent.TButton",
                   command=self.start_all).pack(side="right", padx=8)
        ttk.Button(top, text="Escribir símbolos…", command=self.add_symbols).pack(side="right")
        ttk.Button(top, text="Buscar símbolos…", command=lambda: self.browse_symbols(multi=True)).pack(
            side="right", padx=8)
        self.overview_tree = make_tree(tab, [
            ("profile", "Perfil", 110, "w"), ("symbol", "Símbolo", 90, "w"), ("tf", "TF", 50, "w"),
            ("state", "Estado", 190, "w"), ("day", "Hoy", 70, "e"),
            ("position", "Posición abierta", 230, "w"), ("pnl", "Resultado", 80, "e"),
            ("evo", "Estrategia (PF valid.)", 190, "w"), ("magic", "Magic", 80, "e")], height=14)
        self.overview_tree.frame.pack(fill="both", expand=True)
        self.overview_tree.tag_configure("idle", foreground=C_GREY)
        self.overview_tree.bind("<Double-1>", self._on_overview_open)
        self.overview_summary = ttk.Label(tab, text="", font=F_BOLD)
        self.overview_summary.pack(fill="x", pady=(8, 0))
        ttk.Label(tab, text="Riesgo combinado: cada símbolo arriesga su '% por operación'. El límite "
                            "'Máx. posiciones (todos)' y la 'Pérdida diaria máxima' se aplican a toda "
                            "la cuenta, para todos los perfiles.", style="Muted.TLabel",
                  wraplength=1100).pack(fill="x", pady=(4, 0))

    def _on_overview_open(self, _event: Any = None) -> None:
        sel = self.overview_tree.selection()
        if not sel:
            return
        self.profile.set(sel[0])
        self._on_profile_selected()
        self.notebook.select(0)

    def _profile_state(self, name: str, st: dict[str, Any]) -> tuple[str, str, bool]:
        """(texto de estado, color, en ejecución o reciente) de un perfil."""
        bp = self.procs.get(name)
        phase = st.get("phase", "")
        if bp and bp.running:
            if bp.stop_requested_at:
                return "Deteniendo…", C_ORANGE, True
            label, color = PHASES.get(phase, ("Iniciando…", C_BLUE))
            return (label + (" (simulación)" if bp.dry_run else "")), color, True
        fresh = False
        if st.get("updated"):
            try:
                fresh = (datetime.now() - datetime.fromisoformat(st["updated"])).total_seconds() < 180
            except ValueError:
                pass
        if fresh and phase in ("operando", "sin_parametros", "detenido_perdida", "evolucionando"):
            return "En ejecución (fuera de la ventana)", C_GREEN, True
        if phase in ("descartado", "error"):
            label, color = PHASES[phase]
            return label, color, False
        return "Detenido", C_GREY, False

    def _refresh_overview(self) -> None:
        names = list_profiles() or [MAIN_PROFILE]
        tree = self.overview_tree
        for iid in tree.get_children():
            if iid not in names:
                tree.delete(iid)
        running_count, total_pnl, open_count = 0, 0.0, 0
        for name in names:
            path = config_path_for(name)
            cfg = read_json(path) or {}
            st = read_json(profile_paths(path).status) or {}
            label, color, live = self._profile_state(name, st)
            running_count += int(live)
            positions = (st.get("positions") or []) if live else []
            pos_txt = " | ".join(f"{p['side']} {fmt(p.get('volume'), '{:g}')} @ "
                                 f"{fmt_price(p.get('price_open'))}" for p in positions) or "—"
            pnl = sum((p.get("profit") or 0) for p in positions)
            open_count += len(positions)
            total_pnl += pnl
            evo = st.get("evolution") or {}
            if not evo:
                ev = read_json(profile_paths(path).evolved) or {}
                evo = {"accepted": ev.get("accepted"),
                       "pf_oos": (ev.get("out_of_sample") or {}).get("profit_factor")} if ev else {}
            short = {"reversion_media": "Reversión", "ruptura": "Ruptura"}.get(
                cfg.get("strategy", ""), "Cruce medias")
            evo_txt = (f"{short}: —" if not evo else
                       f"{short}: {'aceptada' if evo.get('accepted') else 'descartada'} "
                       f"({fmt(evo.get('pf_oos'))})")
            day = st.get("day_pnl_percent") if live else None
            values = (name, cfg.get("symbol", "—"), cfg.get("timeframe", "—"), label,
                      fmt(day, "{:+.2f}%"), pos_txt, fmt(pnl, "{:+.2f}") if positions else "—",
                      evo_txt, cfg.get("magic", "—"))
            tag = {C_GREEN: "ok", C_RED: "bad", C_ORANGE: "warn"}.get(color, "idle")
            if tree.exists(name):
                tree.item(name, values=values, tags=(tag,))
            else:
                tree.insert("", "end", iid=name, values=values, tags=(tag,))
        self.overview_summary.configure(
            text=f"{len(names)} perfiles · {running_count} en ejecución · {open_count} posiciones "
                 f"abiertas · resultado abierto {total_pnl:+.2f}",
            foreground=C_GREEN if total_pnl > 0 else (C_RED if total_pnl < 0 else C_TEXT))

    # ----------------------------------------------------- pestaña Configuración
    def _build_config_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=12)
        self.tab_config = tab
        self.notebook.add(tab, text="  Configuración  ")

        grid = ttk.Frame(tab)
        grid.pack(fill="both", expand=True)
        for i in range(3):
            grid.columnconfigure(i, weight=1, uniform="cfg")
        layout = {0: ["Mercado", "Riesgo"], 1: ["Evolución"], 2: ["Validación y costes", "Ejecución"]}
        groups = dict(FIELD_GROUPS)
        float_vcmd = (self.register(lambda v: bool(_FLOAT_RE.match(v))), "%P")
        int_vcmd = (self.register(lambda v: bool(_INT_RE.match(v))), "%P")

        for col, names in layout.items():
            column = ttk.Frame(grid)
            column.grid(row=0, column=col, sticky="nsew", padx=(0 if col == 0 else 10, 0))
            for name in names:
                box = ttk.LabelFrame(column, text=f" {name} ", padding=(10, 6))
                box.pack(fill="x", pady=(0, 10))
                box.columnconfigure(1, weight=1)
                for row, fld in enumerate(groups[name]):
                    self._build_field(box, row, fld, float_vcmd, int_vcmd)

        bottom = ttk.Frame(tab, padding=(0, 6, 0, 0))
        bottom.pack(fill="x")
        ttk.Button(bottom, text="Guardar configuración", style="Accent.TButton",
                   command=self.save_profile).pack(side="left")
        ttk.Button(bottom, text="Probar conexión MT5", command=self.test_connection).pack(
            side="left", padx=8)
        ttk.Button(bottom, text="Valores por defecto", command=self.reset_defaults).pack(side="left")
        self.cfg_status = tk.Label(bottom, text="", bg=C_BG, fg=C_MUTED, font=F_BASE, anchor="w",
                                   justify="left", wraplength=620)
        self.cfg_status.pack(side="left", padx=14, fill="x", expand=True)
        ttk.Label(tab, text="Pase el ratón sobre cada campo para ver su explicación. Si el bot de "
                            "este perfil está en marcha, aplica los cambios al guardar (si cambia "
                            "símbolo, timeframe o magic, vuelve a evolucionar).",
                  style="Muted.TLabel").pack(fill="x", pady=(6, 0))

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
                widget = ttk.Combobox(box, textvariable=var, values=list(fld.choices),
                                      state="readonly", width=16)
            elif fld.kind == "float":
                widget = ttk.Entry(box, textvariable=var, width=16, validate="key",
                                   validatecommand=float_vcmd)
            elif fld.kind in ("int", "optint"):
                widget = ttk.Entry(box, textvariable=var, width=16, validate="key",
                                   validatecommand=int_vcmd)
            else:
                widget = ttk.Entry(box, textvariable=var, width=18)
            widget.grid(row=row, column=1, sticky="ew", padx=(10, 0), pady=3)
            Tooltip(widget, fld.help)
            if fld.key == "symbol":
                btn = ttk.Button(box, text="Buscar…", width=8,
                                 command=lambda: self.browse_symbols(multi=False))
                btn.grid(row=row, column=2, padx=(6, 0), pady=3)
                Tooltip(btn, "Elegir el símbolo de una lista con todos los del bróker")
        var.trace_add("write", self._mark_dirty)
        self.vars[fld.key] = var

    # -------------------------------------------------------- pestaña Resultados
    def _build_results_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(tab, text="  Resultados  ")

        self.verdict = tk.Label(tab, text="Todavía no hay resultados para este perfil.",
                                bg="#e3e8ee", fg=C_TEXT, font=F_BOLD, anchor="w", justify="left",
                                padx=12, pady=10, wraplength=1100)
        self.verdict.pack(fill="x")

        top = ttk.Frame(tab, padding=(0, 10, 0, 0))
        top.pack(fill="x")
        top.columnconfigure(0, weight=2)
        top.columnconfigure(1, weight=3)
        pbox = ttk.LabelFrame(top, text=" Parámetros elegidos ", padding=6)
        pbox.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        self.params_tree = make_tree(pbox, [("param", "Parámetro", 200, "w"),
                                            ("value", "Valor", 90, "e")], height=6)
        self.params_tree.frame.pack(fill="both", expand=True)

        mbox = ttk.LabelFrame(top, text=" Métricas del backtest ", padding=6)
        mbox.grid(row=0, column=1, sticky="nsew")
        self.metrics_tree = make_tree(mbox, [("metric", "Métrica", 190, "w"),
                                             ("is", "Entrenamiento (70%)", 140, "e"),
                                             ("oos", "Validación (30%)", 140, "e")], height=6)
        self.metrics_tree.frame.pack(fill="both", expand=True)
        self.folds_label = ttk.Label(mbox, text="", style="Muted.TLabel")
        self.folds_label.pack(fill="x", pady=(4, 0))

        bottom = ttk.Frame(tab, padding=(0, 10, 0, 0))
        bottom.pack(fill="both", expand=True)
        bottom.columnconfigure(0, weight=1)
        bottom.columnconfigure(1, weight=1)
        bottom.rowconfigure(0, weight=1)
        hbox = ttk.LabelFrame(bottom, text=" Historial de evoluciones ", padding=6)
        hbox.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        self.history_tree = make_tree(hbox, [
            ("date", "Fecha", 125, "w"), ("market", "Mercado", 80, "w"),
            ("strategy", "Estrategia", 105, "w"),
            ("result", "Resultado", 90, "w"), ("pf_is", "PF entreno", 75, "e"),
            ("pf_oos", "PF valid.", 70, "e"), ("trades", "Op. E/V", 65, "e")], height=9)
        self.history_tree.frame.pack(fill="both", expand=True)

        dbox = ttk.LabelFrame(bottom, text=" Operaciones cerradas del bot (últimos 7 días) ", padding=6)
        dbox.grid(row=0, column=1, sticky="nsew")
        self.deals_tree = make_tree(dbox, [
            ("close", "Cierre (servidor)", 125, "w"), ("side", "Lado", 50, "w"),
            ("vol", "Lote", 50, "e"), ("open", "Entrada", 80, "e"),
            ("exit", "Salida", 80, "e"), ("profit", "Resultado", 80, "e")], height=9)
        self.deals_tree.frame.pack(fill="both", expand=True)
        self.deals_total = ttk.Label(dbox, text="", font=F_BOLD)
        self.deals_total.pack(fill="x", pady=(4, 0))
        ttk.Label(tab, text="Las operaciones se actualizan mientras el bot de este perfil está en "
                            "marcha. PF = Profit Factor (ganancias / pérdidas; > 1 es positivo).",
                  style="Muted.TLabel").pack(fill="x", pady=(6, 0))

    # ================================================================ perfiles
    @property
    def config_path(self) -> Path:
        """Archivo de configuración del perfil seleccionado."""
        return config_path_for(self.profile.get() or MAIN_PROFILE)

    def _refresh_profiles(self, select: str | None = None) -> None:
        names = list_profiles() or [MAIN_PROFILE]
        if MAIN_PROFILE not in names:
            names.insert(0, MAIN_PROFILE)
        self.profile_box.configure(values=names)
        target = select if select in names else (self.profile.get() if self.profile.get() in names else names[0])
        self.profile.set(target)
        self._prev_profile = target
        self._load_profile()

    def _on_profile_selected(self, _event: Any = None) -> None:
        new = self.profile.get()
        if new == self._prev_profile:
            return
        if self.dirty:
            answer = messagebox.askyesnocancel(
                "Cambios sin guardar",
                f"El perfil '{self._prev_profile}' tiene cambios sin guardar.\n¿Guardarlos antes de cambiar?",
                parent=self)
            if answer is None:
                self.profile.set(self._prev_profile)
                return
            if answer:
                self.profile.set(self._prev_profile)
                if not self.save_profile(quiet=True):
                    return
                self.profile.set(new)
        self._prev_profile = new
        self._load_profile()

    def _load_profile(self) -> None:
        """Carga la configuración del perfil en el formulario y refresca todo."""
        path = self.config_path
        try:
            cfg = load_config(path, must_exist=False)
            msg = (f"Perfil '{self.profile.get()}' cargado desde {path.name}." if path.exists()
                   else f"Perfil nuevo: valores por defecto (se creará {path.name} al guardar).")
            self._set_cfg_status(msg, C_MUTED)
        except ValueError as exc:
            cfg = dict(DEFAULT_CONFIG)
            self._set_cfg_status(f"{path.name} es inválido, se muestran valores por defecto: {exc}", C_RED)
        self._fill_form(cfg)
        self._render_log_buffer()
        self._refresh_results()
        self._refresh_live(reschedule=False)
        self._update_buttons()

    def _fill_form(self, cfg: dict[str, Any]) -> None:
        self._loading = True
        for fld in ALL_FIELDS:
            value = cfg.get(fld.key, DEFAULT_CONFIG.get(fld.key))
            var = self.vars[fld.key]
            if fld.kind == "bool":
                var.set(bool(value))
            elif fld.kind == "optint":
                var.set("" if value is None else str(value))
            elif fld.kind == "float":
                var.set(f"{float(value):.6f}".rstrip("0").rstrip("."))
            else:
                var.set(str(value))
        self._loading = False
        self.dirty = False

    def _mark_dirty(self, *_args: Any) -> None:
        if not self._loading:
            self.dirty = True
            self._set_cfg_status("Hay cambios sin guardar.", C_ORANGE)

    def _set_cfg_status(self, text: str, color: str = C_MUTED) -> None:
        self.cfg_status.configure(text=text, fg=color)

    def collect_form(self) -> dict[str, Any]:
        """Lee el formulario y lo convierte a tipos; lanza ValueError si algo no es válido."""
        base = load_config(self.config_path, must_exist=False) if self.config_path.exists() else dict(DEFAULT_CONFIG)
        data = dict(base)
        for fld in ALL_FIELDS:
            raw = self.vars[fld.key].get()
            if fld.kind == "bool":
                data[fld.key] = bool(raw)
                continue
            raw = str(raw).strip()
            if fld.kind == "optint":
                data[fld.key] = int(raw) if raw else None
            elif fld.kind == "str" or fld.kind == "choice":
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

    def _magic_conflict(self, data: dict[str, Any]) -> str | None:
        """Devuelve el nombre de otro perfil con el mismo magic, si existe."""
        for name in list_profiles():
            if name == self.profile.get():
                continue
            other = read_json(config_path_for(name)) or {}
            if other.get("magic") == data["magic"]:
                return name
        return None

    def save_profile(self, quiet: bool = False) -> bool:
        """Valida y guarda el perfil. Devuelve True si se guardó."""
        try:
            data = self.collect_form()
        except ValueError as exc:
            self._set_cfg_status("No se guardó: revise los campos marcados.", C_RED)
            messagebox.showerror("Configuración inválida", str(exc), parent=self)
            return False
        clash = self._magic_conflict(data)
        if clash and not messagebox.askyesno(
                "Magic number repetido",
                f"El perfil '{clash}' usa el mismo magic number ({data['magic']}).\n"
                "Dos perfiles con el mismo magic verían las operaciones del otro.\n\n¿Guardar igualmente?",
                parent=self):
            return False
        try:
            save_config(data, self.config_path)
        except (OSError, ValueError) as exc:
            messagebox.showerror("Error al guardar", str(exc), parent=self)
            return False
        self.dirty = False
        running = self.profile.get() in self.procs and self.procs[self.profile.get()].running
        extra = " El bot en marcha aplicará los cambios." if running else ""
        self._set_cfg_status(f"Guardado en {self.config_path.name}.{extra}", C_GREEN)
        if not quiet:
            messagebox.showinfo("Configuración guardada",
                                f"Perfil '{self.profile.get()}' guardado.{extra}", parent=self)
        if self.profile.get() not in (self.profile_box.cget("values") or ()):
            self._refresh_profiles(select=self.profile.get())
        return True

    def reset_defaults(self) -> None:
        """Rellena el formulario con los valores por defecto (sin guardar)."""
        if messagebox.askyesno("Valores por defecto",
                               "¿Reemplazar el formulario por los valores por defecto?\n"
                               "(No se guarda hasta pulsar 'Guardar configuración'.)", parent=self):
            magic = self.vars["magic"].get()
            self._fill_form(DEFAULT_CONFIG)
            self.vars["magic"].set(magic)
            self.dirty = True
            self._set_cfg_status("Valores por defecto cargados (sin guardar).", C_ORANGE)

    def new_profile(self) -> None:
        """Crea un perfil nuevo a partir del formulario actual."""
        name = simpledialog.askstring(
            "Nuevo perfil",
            "Nombre del perfil (minúsculas, números, - o _), por ejemplo: m5, h1, us500",
            parent=self)
        if not name:
            return
        name = name.strip().lower()
        if not PROFILE_NAME_RE.match(name) or name == MAIN_PROFILE:
            messagebox.showerror("Nombre inválido",
                                 "Use solo minúsculas, números, '-' o '_' (máx. 31 caracteres).",
                                 parent=self)
            return
        path = config_path_for(name)
        if path.exists():
            messagebox.showerror("Ya existe", f"El perfil '{name}' ya existe.", parent=self)
            return
        try:
            data = self.collect_form()
        except ValueError:
            data = dict(DEFAULT_CONFIG)
        magics = [(read_json(config_path_for(n)) or {}).get("magic", 0) for n in list_profiles()]
        data["magic"] = max([int(m) for m in magics if isinstance(m, int)] + [int(data["magic"])]) + 1
        save_config(data, path)
        self.dirty = False
        self._refresh_profiles(select=name)
        self.notebook.select(self.tab_config)
        self._set_cfg_status(
            f"Perfil '{name}' creado (copia del anterior, magic {data['magic']}). "
            "Ajuste timeframe/símbolo y guarde.", C_GREEN)

    def delete_profile(self) -> None:
        """Elimina el perfil seleccionado (no el principal)."""
        name = self.profile.get()
        if name == MAIN_PROFILE:
            messagebox.showinfo("No permitido", "El perfil principal no se puede eliminar.", parent=self)
            return
        if name in self.procs and self.procs[name].running:
            messagebox.showwarning("Bot en marcha", "Detenga el bot de este perfil antes de eliminarlo.",
                                   parent=self)
            return
        if not messagebox.askyesno("Eliminar perfil",
                                   f"¿Eliminar el perfil '{name}'?\n(Se borra su configuración; "
                                   "los logs y el historial se conservan.)", parent=self):
            return
        paths = profile_paths(self.config_path)
        for path in (paths.config, paths.evolved):
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                messagebox.showerror("Error", str(exc), parent=self)
                return
        self.procs.pop(name, None)
        self.dirty = False
        self._refresh_profiles(select=MAIN_PROFILE)

    # ================================================================ conexión
    def test_connection(self) -> None:
        """Prueba la conexión con MT5 en segundo plano."""
        symbol = str(self.vars["symbol"].get()).strip()
        self._set_cfg_status("Conectando con MetaTrader 5…", C_BLUE)
        self.configure(cursor="watch")
        args = (symbol, bool(self.vars["require_demo"].get()),
                str(self.vars["terminal_path"].get()).strip() or None)
        threading.Thread(target=lambda: self._conn_queue.put(test_connection(*args)),
                         daemon=True).start()
        self.after(200, self._check_connection_result)

    def _check_connection_result(self) -> None:
        try:
            res = self._conn_queue.get_nowait()
        except queue.Empty:
            self.after(200, self._check_connection_result)
            return
        self.configure(cursor="")
        if not res.get("ok"):
            self._set_cfg_status(res.get("error", "Error desconocido"), C_RED)
            return
        text = (f"Conexión OK · {res['login']} ({res['server']}) · balance "
                f"{res['balance']:.2f} {res['currency']} · equity {res['equity']:.2f}")
        symbol = str(self.vars["symbol"].get()).strip()
        if res.get("symbol_found") is False:
            sugg = res.get("suggestions") or []
            if sugg and messagebox.askyesno(
                    "Símbolo no encontrado",
                    f"'{symbol}' no existe en este bróker (los nombres distinguen mayúsculas).\n\n"
                    f"¿Usar '{sugg[0]}'?", parent=self):
                self.vars["symbol"].set(sugg[0])
                self._set_cfg_status(f"{text}\nSímbolo cambiado a '{sugg[0]}'. Pulse 'Probar conexión' "
                                     "de nuevo para ver su spread y guarde.", C_ORANGE)
            else:
                extra = f" Sugerencias: {', '.join(sugg)}" if sugg else ""
                self._set_cfg_status(f"{text}\nEl símbolo '{symbol}' no existe.{extra}", C_RED)
            return
        if res.get("symbol_found"):
            spread = res.get("spread")
            try:
                max_spread = float(self.vars["max_spread"].get())
            except ValueError:
                max_spread = float("nan")
            lot = f" · lote mín. bróker {res['volume_min']:g}" if res.get("volume_min") else ""
            if spread is None or spread != spread:
                self._set_cfg_status(f"{text}\n{symbol}: sin cotización ahora (¿mercado cerrado?){lot}",
                                     C_ORANGE)
            elif spread > max_spread:
                self._set_cfg_status(
                    f"{text}\n{symbol}: spread {spread:g} > máximo {max_spread:g}. El bot NO operará: "
                    f"suba 'Spread máximo' (p. ej. {spread * 2:g}) y guarde.{lot}", C_RED)
            else:
                self._set_cfg_status(f"{text}\n{symbol}: spread {spread:g} ≤ máximo {max_spread:g}{lot}",
                                     C_GREEN)
            return
        self._set_cfg_status(text, C_GREEN)

    # ================================================================== bot
    def start_bot(self) -> None:
        """Guarda si hace falta y lanza el bot del perfil seleccionado."""
        name = self.profile.get()
        if name in self.procs and self.procs[name].running:
            return
        if self.dirty or not self.config_path.exists():
            if not messagebox.askyesno("Guardar antes de iniciar",
                                       "Hay cambios sin guardar (o el perfil aún no existe).\n"
                                       "¿Guardar la configuración e iniciar el bot?", parent=self):
                return
            if not self.save_profile(quiet=True):
                return
        try:
            cfg = load_config(self.config_path)
        except (ValueError, FileNotFoundError) as exc:
            messagebox.showerror("Configuración inválida", str(exc), parent=self)
            return
        if not self.dry_run.get() and not cfg.get("require_demo", True):
            if not messagebox.askyesno(
                    "Atención: 'Solo cuenta DEMO' desactivado",
                    "Este perfil puede operar en una cuenta REAL.\n¿Seguro que quiere iniciar el bot?",
                    parent=self, icon="warning"):
                return
        if self._launch(name, self.config_path, cfg):
            self.notebook.select(0)
            self._render_log_buffer()
            self._update_buttons()

    def _launch(self, name: str, path: Path, cfg: dict[str, Any]) -> bool:
        """Lanza el proceso del bot de un perfil. Devuelve True si arrancó."""
        bp = BotProcess(name, path, bool(self.dry_run.get()))
        mode = "SIMULACIÓN (no envía órdenes)" if bp.dry_run else "envío de órdenes ACTIVADO"
        bp.buffer.append(f"### {datetime.now():%H:%M:%S} Iniciando bot · perfil '{name}' · "
                         f"{cfg['symbol']} {cfg['timeframe']} · {mode}")
        try:
            bp.start()
        except OSError as exc:
            messagebox.showerror("No se pudo iniciar", f"Perfil '{name}': {exc}", parent=self)
            return False
        self.procs[name] = bp
        return True

    def start_all(self) -> None:
        """Inicia los bots de todos los perfiles que no estén en marcha."""
        if self.dirty:
            answer = messagebox.askyesnocancel(
                "Cambios sin guardar", f"El perfil '{self.profile.get()}' tiene cambios sin guardar.\n"
                "¿Guardarlos antes de iniciar todos?", parent=self)
            if answer is None or (answer and not self.save_profile(quiet=True)):
                return
        pending, errors = [], []
        for name in list_profiles():
            if name in self.procs and self.procs[name].running:
                continue
            path = config_path_for(name)
            try:
                pending.append((name, path, load_config(path)))
            except (ValueError, FileNotFoundError) as exc:
                errors.append(f"• {name}: {exc}")
        if not pending:
            messagebox.showinfo("Iniciar todos", "No hay perfiles detenidos para iniciar."
                                + ("\n\n" + "\n".join(errors) if errors else ""), parent=self)
            return
        magics = [cfg["magic"] for _, _, cfg in pending] + [
            (read_json(config_path_for(n)) or {}).get("magic") for n, p in self.procs.items() if p.running]
        if len(set(magics)) != len(magics):
            messagebox.showerror("Magic numbers repetidos",
                                 "Hay perfiles con el mismo magic number. Corríjalo en Configuración "
                                 "antes de iniciar todos.", parent=self)
            return
        real = [n for n, _, cfg in pending if not cfg.get("require_demo", True)]
        if real and not self.dry_run.get() and not messagebox.askyesno(
                "Atención: 'Solo cuenta DEMO' desactivado",
                f"Estos perfiles pueden operar en cuenta REAL: {', '.join(real)}.\n¿Iniciar igualmente?",
                parent=self, icon="warning"):
            return
        mode = "en SIMULACIÓN" if self.dry_run.get() else "con envío de órdenes"
        if not messagebox.askyesno(
                "Iniciar todos",
                f"Se iniciarán {len(pending)} bot(s) {mode}:\n"
                + "\n".join(f"• {n}: {c['symbol']} {c['timeframe']}" for n, _, c in pending)
                + "\n\n¿Continuar?", parent=self):
            return
        started = sum(self._launch(n, p, c) for n, p, c in pending)
        self.notebook.select(self.tab_overview)
        self._render_log_buffer()
        self._update_buttons()
        if errors:
            messagebox.showwarning("Algunos perfiles no se iniciaron",
                                   f"Iniciados: {started}\n\n" + "\n".join(errors), parent=self)

    def stop_all(self) -> None:
        """Pide a todos los bots en marcha que se detengan."""
        active = [p for p in self.procs.values() if p.running and not p.stop_requested_at]
        if not active:
            return
        for bp in active:
            bp.request_stop()
            self._append_line(bp, f"### {datetime.now():%H:%M:%S} Deteniendo el bot… "
                                  "(las posiciones abiertas se mantienen con su SL/TP)")
        self._update_buttons()

    def browse_symbols(self, multi: bool, reload: bool = False) -> None:
        """Abre el buscador de símbolos (carga la lista del bróker la primera vez)."""
        if self._symbol_list is not None and not reload:
            self._open_browser(multi)
            return
        self.configure(cursor="watch")
        self._set_cfg_status("Cargando la lista de símbolos del bróker…", C_BLUE)
        args = (bool(self.vars["require_demo"].get()),
                str(self.vars["terminal_path"].get()).strip() or None)
        threading.Thread(target=lambda: self._load_queue.put(load_symbols(*args)), daemon=True).start()
        self.after(200, lambda: self._wait_symbol_list(multi))

    def _wait_symbol_list(self, multi: bool) -> None:
        try:
            res = self._load_queue.get_nowait()
        except queue.Empty:
            self.after(200, lambda: self._wait_symbol_list(multi))
            return
        self.configure(cursor="")
        if not res.get("ok") or not res.get("symbols"):
            self._set_cfg_status(res.get("error", "El bróker no devolvió símbolos."), C_RED)
            if multi and messagebox.askyesno(
                    "Buscador no disponible",
                    f"{res.get('error', 'No se pudo cargar la lista de símbolos.')}\n\n"
                    "¿Escribir los símbolos a mano?", parent=self):
                self.add_symbols()
            elif not multi:
                messagebox.showerror("Buscador no disponible",
                                     res.get("error", "No se pudo cargar la lista de símbolos."),
                                     parent=self)
            return
        self._symbol_list = res["symbols"]
        self._set_cfg_status(f"{len(self._symbol_list)} símbolos cargados del bróker.", C_MUTED)
        self._open_browser(multi)

    def _open_browser(self, multi: bool) -> None:
        existing = {str((read_json(config_path_for(n)) or {}).get("symbol", "")) for n in list_profiles()}
        try:
            base = self.collect_form()
        except ValueError:
            base = dict(DEFAULT_CONFIG)
        dialog = SymbolBrowser(self, self._symbol_list or [], multi, existing,
                               str(base.get("timeframe", "M15")), str(base.get("strategy", "cruce_medias")))
        self.wait_window(dialog)
        if not dialog.result:
            return
        chosen = dialog.result["symbols"]
        if not multi:
            item = chosen[0]
            self.vars["symbol"].set(item["name"])
            spread = item.get("spread")
            msg = f"Símbolo '{item['name']}' elegido ({item['description']})."
            try:
                current_max = float(self.vars["max_spread"].get())
            except ValueError:
                current_max = 0.0
            if spread and spread > current_max:
                new_max = round_sig(spread * 2.5, 2)
                if messagebox.askyesno(
                        "Ajustar spread máximo",
                        f"El spread actual de {item['name']} es {spread:g}, mayor que el máximo "
                        f"configurado ({current_max:g}). Con ese valor el bot nunca operaría.\n\n"
                        f"¿Cambiar el spread máximo a {new_max:g}?", parent=self):
                    self.vars["max_spread"].set(f"{new_max:g}")
                    msg += f" Spread máximo ajustado a {new_max:g}."
            self.notebook.select(self.tab_config)
            self._set_cfg_status(msg + " Pulse 'Guardar configuración'.", C_ORANGE)
            return
        try:
            base = self.collect_form()
        except ValueError as exc:
            messagebox.showerror("Configuración inválida",
                                 f"Corrija primero el perfil actual (se usa como plantilla):\n{exc}",
                                 parent=self)
            return
        base["timeframe"] = dialog.result["timeframe"]
        base["strategy"] = dialog.result["strategy"]
        names = [d["name"] for d in chosen]
        info = ({d["name"]: {"name": d["name"], "spread": d.get("spread"), "suggestions": []}
                 for d in chosen} if dialog.result["auto_spread"] else None)
        self._create_symbol_profiles(base, names, info)

    def add_symbols(self) -> None:
        """Asistente: crea un perfil por símbolo a partir del perfil actual."""
        try:
            base = self.collect_form()
        except ValueError as exc:
            messagebox.showerror("Configuración inválida",
                                 f"Corrija primero el perfil actual (se usa como plantilla):\n{exc}",
                                 parent=self)
            return
        dialog = MultiSymbolDialog(self, str(base["timeframe"]), str(base.get("strategy", "cruce_medias")))
        self.wait_window(dialog)
        if not dialog.result:
            return
        symbols, timeframe, check, strategy = dialog.result
        base["timeframe"] = timeframe
        base["strategy"] = strategy
        if not check:
            self._create_symbol_profiles(base, symbols, None)
            return
        self.configure(cursor="watch")
        self._set_cfg_status("Comprobando símbolos en MetaTrader 5…", C_BLUE)
        args = (symbols, bool(base["require_demo"]), base.get("terminal_path") or None)
        threading.Thread(target=lambda: self._symbols_queue.put(inspect_symbols(*args)),
                         daemon=True).start()
        self.after(200, lambda: self._wait_symbols(base, symbols))

    def _wait_symbols(self, base: dict[str, Any], symbols: list[str]) -> None:
        try:
            res = self._symbols_queue.get_nowait()
        except queue.Empty:
            self.after(200, lambda: self._wait_symbols(base, symbols))
            return
        self.configure(cursor="")
        self._set_cfg_status("", C_MUTED)
        if not res.get("ok"):
            if not messagebox.askyesno(
                    "Sin conexión con MT5",
                    f"{res.get('error', 'No se pudo conectar')}\n\n¿Crear los perfiles igualmente? "
                    "(No se comprobará si los símbolos existen y el spread máximo se copiará del "
                    "perfil actual.)", parent=self):
                return
            self._create_symbol_profiles(base, symbols, None)
            return
        self._create_symbol_profiles(base, symbols, res["symbols"])

    def _create_symbol_profiles(self, base: dict[str, Any], symbols: list[str],
                                info: dict[str, Any] | None) -> None:
        existing = set(list_profiles())
        magics = [(read_json(config_path_for(n)) or {}).get("magic") for n in existing]
        next_magic = max([m for m in magics if isinstance(m, int)] + [int(base["magic"])]) + 1
        created, skipped = [], []
        for sym in symbols:
            name_in_broker = sym
            spread = None
            if info is not None:
                data = info.get(sym) or {}
                if not data.get("name"):
                    sugg = data.get("suggestions") or []
                    skipped.append(f"• {sym}: no existe en el bróker"
                                   + (f" (¿{', '.join(sugg[:4])}?)" if sugg else ""))
                    continue
                name_in_broker, spread = data["name"], data.get("spread")
            profile = profile_name_for_symbol(name_in_broker, base["timeframe"], existing)
            if profile is None:
                skipped.append(f"• {name_in_broker}: ya existe un perfil para este símbolo")
                continue
            cfg = dict(base)
            cfg.update(symbol=name_in_broker, magic=next_magic)
            note = ""
            if spread:
                cfg["max_spread"] = round_sig(spread * 2.5, 2)
                note = f" · spread actual {spread:g} → máx {cfg['max_spread']:g}"
            elif info is not None:
                note = " · sin cotización ahora: revise el spread máximo"
            try:
                save_config(cfg, config_path_for(profile))
            except (OSError, ValueError) as exc:
                skipped.append(f"• {name_in_broker}: {exc}")
                continue
            existing.add(profile)
            created.append(f"• {profile}: {name_in_broker} {cfg['timeframe']} · {cfg['strategy']} · "
                           f"magic {next_magic}{note}")
            next_magic += 1
        current = self.profile.get()
        self._refresh_profiles(select=current)
        self.notebook.select(self.tab_overview)
        risk_note = ""
        total = len(list_profiles())
        if created:
            risk_note = (f"\n\nCon {total} perfiles al {base['risk_percent']:g}% cada uno, el límite "
                         f"'Máx. posiciones (todos)' = {base['max_open_positions_total']} evita arriesgar "
                         "demasiado a la vez (ajústelo en Configuración).")
        messagebox.showinfo(
            "Varios símbolos",
            (("Perfiles creados:\n" + "\n".join(created)) if created else "No se creó ningún perfil.")
            + (("\n\nOmitidos:\n" + "\n".join(skipped)) if skipped else "") + risk_note
            + ("\n\nPulse 'Iniciar todos' para arrancarlos." if created else ""), parent=self)

    def stop_bot(self) -> None:
        """Pide al bot del perfil seleccionado que se detenga."""
        bp = self.procs.get(self.profile.get())
        if not bp or not bp.running:
            return
        bp.request_stop()
        self._append_line(bp, f"### {datetime.now():%H:%M:%S} Deteniendo el bot… "
                              "(las posiciones abiertas se mantienen con su SL/TP)")
        self._update_buttons()

    def open_logs(self) -> None:
        """Abre la carpeta de logs en el explorador."""
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        try:
            if os.name == "nt":
                os.startfile(str(LOG_DIR))  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", str(LOG_DIR)])
        except OSError as exc:
            messagebox.showinfo("Carpeta de logs", f"{LOG_DIR}\n\n({exc})", parent=self)

    def _update_buttons(self) -> None:
        bp = self.procs.get(self.profile.get())
        running = bool(bp and bp.running)
        stopping = bool(bp and bp.running and bp.stop_requested_at)
        self.btn_start.configure(state="disabled" if running else "normal")
        self.btn_stop.configure(state="normal" if running and not stopping else "disabled")
        active = [n for n, p in self.procs.items() if p.running]
        if active:
            text = (f"● 1 bot en ejecución ({active[0]})" if len(active) == 1
                    else f"● {len(active)} bots en ejecución")
            self.running_label.configure(text=text, foreground=C_GREEN)
        else:
            self.running_label.configure(text="Ningún bot en ejecución", foreground=C_MUTED)

    # ---------------------------------------------------------------- log
    @staticmethod
    def _line_tag(line: str) -> str | None:
        if line.startswith("###"):
            return "gui"
        if "ORDEN EJECUTADA" in line:
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

    def _append_line(self, bp: BotProcess, line: str) -> None:
        bp.buffer.append(line)
        if len(bp.buffer) > MAX_LOG_LINES:
            del bp.buffer[: len(bp.buffer) - MAX_LOG_LINES]
        if bp.profile == self.profile.get():
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
        """Muestra el log del perfil seleccionado (o el final del último log en disco)."""
        bp = self.procs.get(self.profile.get())
        if bp and bp.buffer:
            self._insert_log(bp.buffer, clear=True)
            return
        log_path = LOG_DIR / profile_paths(self.config_path).log_file
        lines = ["### El bot de este perfil no se ha iniciado desde esta ventana."]
        try:
            tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]
            if tail:
                lines += [f"### Últimas líneas de {log_path.name}:"] + tail
        except OSError:
            pass
        self._insert_log(lines, clear=True)

    # ------------------------------------------------------------ bucles
    def _poll_processes(self) -> None:
        """Lee la salida de los bots y detecta cuándo terminan (cada 250 ms)."""
        changed = False
        for bp in list(self.procs.values()):
            batch: list[str] = []
            try:
                while len(batch) < 500:
                    batch.append(bp.lines.get_nowait())
            except queue.Empty:
                pass
            for line in batch:
                self._append_line(bp, line)
            if bp.running and bp.stop_requested_at and \
                    time.monotonic() - bp.stop_requested_at > STOP_TIMEOUT_SECONDS:
                bp.kill()
                self._append_line(bp, "### El bot no respondió a tiempo: proceso terminado a la fuerza.")
            if not bp.running and not bp.reported and bp.lines.empty():
                bp.reported = True
                code = bp.exit_code
                meaning = EXIT_MEANING.get(code if code is not None else -1, "terminado")
                self._append_line(bp, f"### {datetime.now():%H:%M:%S} El bot terminó · código {code}: {meaning}")
                changed = True
        if changed:
            self._update_buttons()
            self._refresh_results()
            self._refresh_live(reschedule=False)
        if self._closing and not any(p.running for p in self.procs.values()):
            self.destroy()
            return
        self.after(250, self._poll_processes)

    def _refresh_live(self, reschedule: bool = True) -> None:
        """Actualiza tarjetas y situación desde el archivo de estado (cada 1,5 s)."""
        try:
            self._render_status()
            self._update_buttons()
            self._refresh_overview()
            paths = profile_paths(self.config_path)
            stamp = (_mtime(paths.evolved), _mtime(paths.history))
            if stamp != self._results_stamp:  # nueva evolución -> refrescar Resultados
                self._refresh_results()
        except Exception:  # noqa: BLE001 - nunca romper el bucle de la GUI
            logger.exception("Error actualizando el panel")
        if reschedule:
            self.after(1500, self._refresh_live)

    def _render_status(self) -> None:
        name = self.profile.get()
        bp = self.procs.get(name)
        running = bool(bp and bp.running)
        st = read_json(profile_paths(self.config_path).status) or {}
        cfg_symbol = str(self.vars["symbol"].get())
        cfg_tf = str(self.vars["timeframe"].get())

        # --- estado
        phase = st.get("phase", "finalizado")
        fresh = False
        if st.get("updated"):
            try:
                fresh = (datetime.now() - datetime.fromisoformat(st["updated"])).total_seconds() < 180
            except ValueError:
                fresh = False
        if running:
            label, color = PHASES.get(phase, (phase, C_BLUE))
            if bp and bp.stop_requested_at:
                label, color = "Deteniendo…", C_ORANGE
            sub1 = "Modo simulación (no envía órdenes)" if (bp and bp.dry_run) else "Envío de órdenes activado"
        elif fresh and phase in ("operando", "sin_parametros", "detenido_perdida", "evolucionando"):
            label, color = "En ejecución (fuera de esta ventana)", C_GREEN
            sub1 = "Iniciado desde consola o desde otra ventana"
        else:
            label, color = PHASES.get(phase, ("Detenido", C_GREY)) if phase in ("descartado", "error") else ("Detenido", C_GREY)
            sub1 = "Pulse 'Iniciar bot' para comenzar"
        code = st.get("exit_code")
        sub2 = f"Último cierre: {EXIT_MEANING.get(code, '')}" if (code is not None and not running) else ""
        self.card_state.set(label, color, sub1, sub2, f"Perfil '{name}'")

        # --- cuenta
        acc = st.get("account") or {}
        if acc:
            pnl = st.get("day_pnl_percent") or 0.0
            pnl_color = C_GREEN if pnl > 0 else (C_RED if pnl < 0 else C_MUTED)
            scope = "solo bots" if st.get("daily_loss_scope") == "bots" else "cuenta"
            bots = st.get("bots_pnl") or {}
            detail = (f"Bots hoy: cerrado {fmt(bots.get('realized'), '{:+.2f}')} · abierto "
                      f"{fmt(bots.get('floating'), '{:+.2f}')}") if bots else ""
            self.card_account.set(
                f"{acc.get('equity', 0):,.2f}", C_TEXT,
                f"Equity · balance {acc.get('balance', 0):,.2f}",
                (f"Hoy ({scope}) {pnl:+.2f}% (límite −{fmt(st.get('max_daily_loss_percent'), '{:g}')}%)",
                 pnl_color), detail)
        else:
            self.card_account.set("—", C_MUTED, "Sin datos: inicie el bot o pulse", "'Probar conexión' en Configuración")

        # --- mercado
        price = st.get("price") or {}
        spread, max_spread = st.get("spread"), st.get("max_spread")
        symbol = st.get("symbol") or cfg_symbol
        tf = st.get("timeframe") or cfg_tf
        if price and running:
            sp_color = C_RED if (spread and max_spread and spread > max_spread) else C_MUTED
            self.card_market.set(
                f"{symbol} · {tf}", C_TEXT,
                f"Bid {fmt_price(price.get('bid'))} · Ask {fmt_price(price.get('ask'))}",
                (f"Spread {fmt_price(spread)} (máx {fmt_price(max_spread)})", sp_color))
        else:
            self.card_market.set(f"{cfg_symbol} · {cfg_tf}", C_TEXT, "Precio visible con el bot en marcha")

        # --- estrategia
        evo = st.get("evolution") or {}
        evolved = read_json(profile_paths(self.config_path).evolved) or {}
        if not evo and evolved:
            evo = {"accepted": evolved.get("accepted"), "params": evolved.get("params"),
                   "pf_is": (evolved.get("in_sample") or {}).get("profit_factor"),
                   "pf_oos": (evolved.get("out_of_sample") or {}).get("profit_factor")}
        strat = STRATEGY_NAMES.get(str(self.vars["strategy"].get()), "")
        if evo:
            ok = evo.get("accepted")
            self.card_strategy.set(
                ("Aceptada" if ok else "Descartada"), C_GREEN if ok else C_ORANGE,
                describe_params(evo.get("params")),
                f"PF entrenamiento {fmt(evo.get('pf_is'))} · validación {fmt(evo.get('pf_oos'))}")
        else:
            self.card_strategy.set("—", C_MUTED, strat, "Se evoluciona al iniciar el bot")

        # --- situación
        if running or fresh:
            # La posición se muestra aparte (en azul): se quita del mensaje para no repetirla.
            message = " | ".join(part for part in str(st.get("message") or "").split(" | ")
                                 if not part.startswith("posición abierta"))
            if phase == "evolucionando":
                message = "Buscando parámetros con el algoritmo genético y validándolos (walk-forward)…"
            self.situation_label.configure(text=message or "Esperando datos del bot…", fg=C_TEXT)
        elif phase == "descartado":
            self.situation_label.configure(
                text="La validación descartó los parámetros, así que el bot no opera (es una protección "
                     "contra el sobreajuste). Revise la pestaña Resultados.", fg=C_ORANGE)
        elif phase == "error":
            self.situation_label.configure(text=f"El bot se detuvo: {st.get('message', '')}", fg=C_RED)
        else:
            self.situation_label.configure(text="El bot está detenido.", fg=C_TEXT)

        positions = st.get("positions") or []
        if positions and (running or fresh):
            self.position_label.configure(text="Posición abierta: " + " | ".join(
                f"{p['side']} {fmt(p.get('volume'), '{:g}')} @ {fmt_price(p.get('price_open'))} · "
                f"SL {fmt_price(p.get('sl'))} · TP {fmt_price(p.get('tp'))} · "
                f"resultado {fmt(p.get('profit'), '{:+.2f}')}" for p in positions))
        else:
            self.position_label.configure(text="")
        bar = st.get("last_bar") or {}
        if bar and (running or fresh):
            sig = {1: "COMPRA", -1: "VENTA"}.get(bar.get("signal"), "ninguna")
            indicators = bar.get("indicators") or (
                f"rápida {fmt_price(bar.get('fast_ma'))} lenta {fmt_price(bar.get('slow_ma'))} "
                f"RSI {fmt(bar.get('rsi'), '{:.1f}')}")
            self.bar_label.configure(
                text=f"Última vela cerrada {bar.get('time', '')} (hora servidor): cierre "
                     f"{fmt_price(bar.get('close'))} · {indicators} · "
                     f"ATR {fmt_price(bar.get('atr'))} · señal: {sig}")
        else:
            self.bar_label.configure(text="")

        # --- operaciones (pestaña Resultados)
        self._render_deals(st.get("deals") or [])

    # ------------------------------------------------------------ resultados
    def _refresh_results(self) -> None:
        paths = profile_paths(self.config_path)
        self._results_stamp = (_mtime(paths.evolved), _mtime(paths.history))
        evo = read_json(paths.evolved)
        for tree in (self.params_tree, self.metrics_tree, self.history_tree):
            tree.delete(*tree.get_children())
        if evo:
            ok = evo.get("accepted")
            self.verdict.configure(
                text=(f"{'✔ ACEPTADO' if ok else '✖ DESCARTADO'} · {evo.get('symbol', '')} "
                      f"{evo.get('timeframe', '')} · "
                      f"{STRATEGY_NAMES.get(evo.get('strategy') or '', 'Cruce de medias + RSI')} · "
                      f"{evo.get('timestamp', '')} · "
                      f"{evo.get('train_bars', '?')} velas de entrenamiento / {evo.get('test_bars', '?')} "
                      f"de validación · motor {evo.get('backend', '?')}\n{evo.get('reason', '')}"),
                bg="#e3f4e8" if ok else "#fdebd3", fg=C_GREEN if ok else C_ORANGE)
            for key, value in (evo.get("params") or {}).items():
                self.params_tree.insert("", "end", values=(PARAM_LABELS.get(key, key),
                                                           fmt(value, "{:g}")))
            is_m, oos_m = evo.get("in_sample") or {}, evo.get("out_of_sample") or {}
            rows = [("Profit Factor", "profit_factor", "{:.3f}"), ("Operaciones", "trades", "{}"),
                    ("% de acierto", "win_rate", "{:.1%}"),
                    ("Beneficio neto (precio, 1 lote)", "net_profit", "{:.5f}"),
                    ("Drawdown máx. (precio, 1 lote)", "max_drawdown", "{:.5f}")]
            for title, key, spec in rows:
                tag = ()
                if key == "profit_factor" and oos_m.get(key) is not None:
                    tag = ("ok",) if oos_m[key] >= 1 else ("bad",)
                self.metrics_tree.insert("", "end", values=(title, fmt(is_m.get(key), spec),
                                                            fmt(oos_m.get(key), spec)), tags=tag)
            folds = evo.get("in_sample_folds") or []
            self.folds_label.configure(text=("Tramos del entrenamiento (PF / operaciones): " + " · ".join(
                f"{fmt(f.get('profit_factor'))} / {f.get('trades')}" for f in folds)) if folds else "")
        else:
            self.verdict.configure(text="Todavía no hay resultados para este perfil. Se generan al "
                                        "iniciar el bot.", bg="#e3e8ee", fg=C_TEXT)
            self.folds_label.configure(text="")

        try:
            lines = paths.history.read_text(encoding="utf-8").splitlines()[-200:]
        except OSError:
            lines = []
        for line in reversed(lines):
            rec = read_json_line(line)
            if not rec:
                continue
            ok = rec.get("accepted")
            self.history_tree.insert("", "end", tags=("ok",) if ok else ("warn",), values=(
                str(rec.get("timestamp", "")).replace("T", " "),
                f"{rec.get('symbol', '')} {rec.get('timeframe', '')}",
                {"reversion_media": "Reversión", "ruptura": "Ruptura"}.get(
                    rec.get("strategy") or "", "Cruce medias"),
                "Aceptado" if ok else "Descartado",
                fmt(rec.get("pf_is")), fmt(rec.get("pf_oos")),
                f"{rec.get('trades_is', '?')}/{rec.get('trades_oos', '?')}"))

    def _render_deals(self, deals: list[dict[str, Any]]) -> None:
        current = [self.deals_tree.item(i, "values") for i in self.deals_tree.get_children()]
        rows = [(d.get("close_time", ""), d.get("side", ""), fmt(d.get("volume"), "{:g}"),
                 fmt_price(d.get("open_price")), fmt_price(d.get("close_price")),
                 fmt(d.get("profit"), "{:+.2f}")) for d in deals]
        if [tuple(map(str, r)) for r in rows] == [tuple(map(str, c)) for c in current]:
            return
        self.deals_tree.delete(*self.deals_tree.get_children())
        for row, d in zip(rows, deals):
            profit = d.get("profit") or 0
            self.deals_tree.insert("", "end", values=row, tags=("ok",) if profit > 0 else ("bad",))
        if deals:
            total = sum(d.get("profit") or 0 for d in deals)
            wins = sum(1 for d in deals if (d.get("profit") or 0) > 0)
            self.deals_total.configure(
                text=f"Total: {total:+.2f} · {len(deals)} operaciones · {wins} ganadoras",
                foreground=C_GREEN if total >= 0 else C_RED)
        else:
            self.deals_total.configure(text="Sin operaciones cerradas todavía.", foreground=C_MUTED)

    # ------------------------------------------------------------- cierre
    def on_close(self) -> None:
        """Al cerrar: si hay bots en marcha, los detiene de forma ordenada."""
        active = [p for p in self.procs.values() if p.running]
        if self.dirty and not active:
            answer = messagebox.askyesnocancel("Cambios sin guardar", "¿Guardar los cambios antes de salir?",
                                               parent=self)
            if answer is None:
                return
            if answer and not self.save_profile(quiet=True):
                return
        if not active:
            self.destroy()
            return
        if not messagebox.askokcancel(
                "Bots en ejecución",
                f"Hay {len(active)} bot(s) en ejecución. Se detendrán antes de cerrar la ventana.\n"
                "Las posiciones abiertas se mantienen en MT5 con su stop loss y take profit.\n\n¿Salir?",
                parent=self):
            return
        self._closing = True
        for p in active:
            p.request_stop()
            self._append_line(p, "### Cerrando la ventana: deteniendo el bot…")
        self.title("MT5 Bot — cerrando…")


def _mtime(path: Path) -> float:
    """Fecha de modificación de un archivo (0 si no existe)."""
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def read_json_line(line: str) -> dict[str, Any] | None:
    """Parsea una línea JSON del historial (None si está corrupta)."""
    import json

    try:
        data = json.loads(line)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


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
    app = ControlCenter()
    if on_ready:
        on_ready(app)
    app.mainloop()


if __name__ == "__main__":
    main()
