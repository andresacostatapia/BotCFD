"""
strategy_evolver.py
===================
Evolución genética de parámetros de una estrategia de cruce de medias + RSI,
con backtest simple y validación Walk-Forward obligatoria.

Backend de optimización
-----------------------
* Principal: ``strategy-genome`` (PyPI, MIT) -> ``strategy_genome.evolve``.
* Alternativo: ``deap`` si ``strategy-genome`` no está instalado o falla.
  Se puede forzar con ``config["evolver_backend"] = "deap"``.

Estrategias (educativas), elegidas con ``config["strategy"]``
-------------------------------------------------------------
* ``cruce_medias`` (tendencia): COMPRA si la SMA rápida cruza al alza la lenta y
  RSI(14) > ``rsi_buy``; VENTA si cruza a la baja y RSI(14) < ``rsi_sell``.
* ``reversion_media`` (corto plazo): COMPRA si el cierre cae bajo la banda inferior
  de Bollinger (``bb_period``, ``bb_std``) con RSI < ``rsi_low``; VENTA si supera la
  banda superior con RSI > ``rsi_high``.
* ``ruptura`` (corto/medio plazo): COMPRA si el cierre supera el máximo de las
  ``lookback`` velas anteriores estando sobre la SMA ``trend``; VENTA si cae bajo
  el mínimo estando bajo la SMA.
* Salida: SL = sl_mult x ATR, TP = tp_mult x ATR. Con ``evolve_sl_tp`` (por
  defecto) ambos multiplicadores se evolucionan (SL 1.0-3.0, TP 1.0-4.0); si no,
  se usan los fijos de la configuración (1.5 / 2.0). El bot en vivo usa los mismos.

Este módulo NO importa MetaTrader5: recibe un DataFrame OHLC por parámetro.
"""

from __future__ import annotations

import logging
import math
import random
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

import numpy as np
import pandas as pd

from bot_common import (
    STOCK_DEFAULT_AVOID_OPEN_MINUTES, STOCK_DEFAULT_FLAT_BEFORE_CLOSE_MINUTES, hour_allowed,
)

logger = logging.getLogger("strategy_evolver")

#: Espacio de búsqueda: nombre -> (tipo, mínimo, máximo)
SEARCH_SPACE: dict[str, tuple[type, float, float]] = {
    "fast": (int, 5, 30),
    "slow": (int, 30, 100),
    "rsi_buy": (float, 40.0, 70.0),
    "rsi_sell": (float, 30.0, 60.0),
}

#: Espacio adicional para las salidas (multiplicadores de ATR), si evolve_sl_tp = true
SLTP_SPACE: dict[str, tuple[type, float, float]] = {
    "sl_mult": (float, 1.0, 3.0),
    "tp_mult": (float, 1.0, 4.0),
}

#: Estrategias disponibles: nombre -> espacio de búsqueda de sus parámetros de entrada.
STRATEGY_SPACES: dict[str, dict[str, tuple[type, float, float]]] = {
    # Tendencia: cruce de medias simples filtrado por RSI (la estrategia original).
    "cruce_medias": SEARCH_SPACE,
    # Reversión a la media: cierre fuera de las bandas de Bollinger con RSI extremo.
    "reversion_media": {
        "bb_period": (int, 10, 50),
        "bb_std": (float, 1.5, 3.0),
        "rsi_low": (float, 15.0, 40.0),
        "rsi_high": (float, 60.0, 85.0),
    },
    # Ruptura: cierre por encima del máximo (o debajo del mínimo) de las N velas
    # anteriores, a favor de la tendencia (SMA larga).
    "ruptura": {
        "lookback": (int, 10, 100),
        "trend": (int, 20, 200),
    },
}
STRATEGY_LABELS: dict[str, str] = {
    "cruce_medias": "Cruce de medias + RSI (tendencia)",
    "reversion_media": "Reversión a la media (Bandas de Bollinger + RSI)",
    "ruptura": "Ruptura de rango (máximo/mínimo de N velas + tendencia)",
}
DEFAULT_STRATEGY: str = "cruce_medias"

#: Filtro de régimen: nivel de ADX (tendencia exige ADX > nivel; reversión, ADX < nivel).
REGIME_SPACE: dict[str, tuple[type, float, float]] = {"adx": (float, 15.0, 40.0)}
#: Gestión de la salida (si se evoluciona): break-even y trailing en múltiplos de ATR.
EXIT_SPACES: dict[str, dict[str, tuple[type, float, float]]] = {
    "ninguna": {},
    "breakeven": {"be_mult": (float, 0.5, 2.5)},
    "trailing": {"trail_mult": (float, 1.0, 3.5)},
}
#: Todas las claves opcionales que pueden aparecer en unos parámetros.
EXTRA_SPACE: dict[str, tuple[type, float, float]] = {
    **SLTP_SPACE, **REGIME_SPACE, **EXIT_SPACES["breakeven"], **EXIT_SPACES["trailing"],
}

RSI_PERIOD: int = 14
#: Tope del PF de entrenamiento usado en la regla "PF validación >= ratio x PF entrenamiento":
#: con entrenamientos espectaculares (PF 5-10) la regla exigiría demasiado; con el tope,
#: un PF de validación >= 0.8 x 2.5 = 2.0 nunca se rechaza por esta regla.
IS_PF_REFERENCE_CAP: float = 2.5
MIN_BARS: int = 300

_DEFAULTS: dict[str, Any] = {
    "commission_per_trade": 0.00007,
    "min_trades": 20,
    "min_trades_oos": 5,
    "fitness_folds": 3,
    "train_ratio": 0.70,
    "oos_pf_ratio_min": 0.80,
    "min_pf_in_sample": 1.0,
    "pf_cap": 10.0,
    "atr_period": 14,
    "sl_atr_mult": 1.5,
    "tp_atr_mult": 2.0,
    "evolve_sl_tp": True,
    "strategy": DEFAULT_STRATEGY,
    "evolver_backend": "auto",
    "evolver_seed": None,
    # Las mejoras están desactivadas por defecto en el evolucionador "suelto";
    # el bot y el laboratorio las activan desde la configuración general.
    "validation_windows": 1,
    "min_pf_oos": 1.0,
    "regime_filter": False,
    "trading_hours": "",
    "exit_management": "ninguna",
    "be_atr_mult": 1.0,
    "trail_atr_mult": 2.0,
    "max_bars_in_trade": 0,
    # Modo acciones (desactivado por defecto; ver asset_config()).
    "session_hours": "",
    "avoid_open_minutes": 0,
    "flat_before_close_minutes": 0,
    "long_only": False,
    "commission_pct": 0.0,
}


# ---------------------------------------------------------------------------
# Indicadores
# ---------------------------------------------------------------------------
def sma(series: pd.Series, period: int) -> pd.Series:
    """Media móvil simple."""
    return series.rolling(window=int(period), min_periods=int(period)).mean()


def rsi(close: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    """RSI de Wilder."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - 100.0 / (1.0 + rs)
    # Sin pérdidas en la ventana -> RSI = 100
    return out.where(avg_loss != 0.0, 100.0).where(avg_gain.notna())


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range de Wilder."""
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average Directional Index de Wilder (fuerza de la tendencia, 0-100)."""
    high, low, close = df["high"].astype(float), df["low"].astype(float), df["close"].astype(float)
    up, down = high.diff(), -low.diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    alpha = 1.0 / period
    atr_s = tr.ewm(alpha=alpha, adjust=False, min_periods=period).mean()
    plus_di = 100 * plus_dm.ewm(alpha=alpha, adjust=False, min_periods=period).mean() / atr_s
    minus_di = 100 * minus_dm.ewm(alpha=alpha, adjust=False, min_periods=period).mean() / atr_s
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    return dx.ewm(alpha=alpha, adjust=False, min_periods=period).mean()


class IndicatorCache:
    """Precalcula y cachea indicadores sobre un DataFrame para acelerar el fitness."""

    def __init__(self, df: pd.DataFrame, atr_period: int = 14) -> None:
        """Valida el DataFrame y precalcula RSI y ATR."""
        missing = {"open", "high", "low", "close"} - set(df.columns)
        if missing:
            raise ValueError(f"Faltan columnas OHLC en el DataFrame: {sorted(missing)}")
        self.df = df.reset_index(drop=True)
        self.close = self.df["close"].astype(float)
        self.open_ = self.df["open"].to_numpy(dtype=float)
        self.high = self.df["high"].to_numpy(dtype=float)
        self.low = self.df["low"].to_numpy(dtype=float)
        self.close_np = self.close.to_numpy()
        self.rsi = rsi(self.close).to_numpy()
        self.atr = atr(self.df, atr_period).to_numpy()
        # Spread histórico por vela (precio). Si no viene en los datos, coste 0.
        if "spread_price" in self.df.columns:
            self.spread = self.df["spread_price"].fillna(0.0).to_numpy(dtype=float)
        else:
            self.spread = np.zeros(len(self.df), dtype=float)
        self._sma: dict[int, np.ndarray] = {}
        self._adx: np.ndarray | None = None
        self._allowed: dict[str, np.ndarray] = {}
        n = len(self.df)
        if "time" in self.df.columns:
            times = pd.to_datetime(self.df["time"])
            self.hours = times.dt.hour.to_numpy()
            secs = ((times - pd.Timestamp("1970-01-01", tz=times.dt.tz)) // pd.Timedelta(seconds=1)).to_numpy()
            diffs = np.diff(secs)
            self.bar_seconds = float(np.median(diffs)) if len(diffs) else 0.0
            # Hueco después de la vela j (cierre de sesión / fin de semana)
            gap = np.zeros(n, dtype=bool)
            if self.bar_seconds > 0 and n > 1:
                gap[:-1] = diffs > 1.5 * self.bar_seconds
        else:
            self.hours = np.full(n, -1)
            self.bar_seconds = 0.0
            gap = np.zeros(n, dtype=bool)
        self.gap_after = gap
        # Velas desde la apertura de la sesión (0 = primera vela tras un hueco) y hasta el cierre
        since = np.zeros(n, dtype=int)
        to_close = np.full(n, 10**9, dtype=int)
        for j in range(1, n):
            since[j] = 0 if gap[j - 1] else since[j - 1] + 1
        last_gap = None
        for j in range(n - 1, -1, -1):
            if gap[j]:
                last_gap = j
            if last_gap is not None:
                to_close[j] = last_gap - j
        self.bars_since_open = since
        self.bars_to_close = to_close
        self._std: dict[int, np.ndarray] = {}
        self._highest: dict[int, np.ndarray] = {}
        self._lowest: dict[int, np.ndarray] = {}

    @property
    def adx(self) -> np.ndarray:
        """ADX(14), calculado bajo demanda."""
        if self._adx is None:
            self._adx = adx(self.df, 14).to_numpy()
        return self._adx

    def session_allowed(self, spec: str) -> np.ndarray:
        """Vector booleano: True si la vela abre dentro del horario permitido."""
        spec = (spec or "").strip()
        if spec not in self._allowed:
            if not spec or (self.hours < 0).all():
                self._allowed[spec] = np.ones(len(self.df), dtype=bool)
            else:
                lookup = np.array([hour_allowed(h, spec) for h in range(24)])
                self._allowed[spec] = lookup[np.clip(self.hours, 0, 23)]
        return self._allowed[spec]

    def rolling_std(self, period: int) -> np.ndarray:
        """Desviación típica móvil del cierre (para Bollinger), cacheada."""
        period = int(period)
        if period not in self._std:
            self._std[period] = self.close.rolling(period, min_periods=period).std(ddof=0).to_numpy()
        return self._std[period]

    def highest(self, lookback: int) -> np.ndarray:
        """Máximo de los ``lookback`` máximos ANTERIORES a cada vela (sin incluirla)."""
        lookback = int(lookback)
        if lookback not in self._highest:
            self._highest[lookback] = (self.df["high"].astype(float)
                                       .rolling(lookback, min_periods=lookback).max()
                                       .shift(1).to_numpy())
        return self._highest[lookback]

    def lowest(self, lookback: int) -> np.ndarray:
        """Mínimo de los ``lookback`` mínimos ANTERIORES a cada vela (sin incluirla)."""
        lookback = int(lookback)
        if lookback not in self._lowest:
            self._lowest[lookback] = (self.df["low"].astype(float)
                                      .rolling(lookback, min_periods=lookback).min()
                                      .shift(1).to_numpy())
        return self._lowest[lookback]

    def sma(self, period: int) -> np.ndarray:
        """SMA cacheada por periodo."""
        period = int(period)
        if period not in self._sma:
            self._sma[period] = sma(self.close, period).to_numpy()
        return self._sma[period]

    def __len__(self) -> int:
        return len(self.df)


# ---------------------------------------------------------------------------
# Señales
# ---------------------------------------------------------------------------
def strategy_of(params: dict[str, Any]) -> str:
    """Deduce la estrategia a partir de las claves de los parámetros."""
    if "bb_period" in params:
        return "reversion_media"
    if "lookback" in params:
        return "ruptura"
    return "cruce_medias"


def warmup_bars(params: dict[str, Any]) -> int:
    """Velas mínimas para que los indicadores de la estrategia estén calculados."""
    periods = [int(params[k]) for k in ("fast", "slow", "bb_period", "lookback", "trend") if k in params]
    return max(periods + [RSI_PERIOD]) + 1


def build_search_space(config: dict[str, Any] | None = None) -> dict[str, tuple[type, float, float]]:
    """Espacio de búsqueda de la estrategia elegida; SL/TP (x ATR) si ``evolve_sl_tp``."""
    cfg = {**_DEFAULTS, **(config or {})}
    name = cfg.get("strategy") or DEFAULT_STRATEGY
    if name not in STRATEGY_SPACES:
        raise ValueError(f"Estrategia desconocida: {name!r}. Use una de {list(STRATEGY_SPACES)}")
    space = dict(STRATEGY_SPACES[name])
    if cfg.get("regime_filter"):
        space.update(REGIME_SPACE)
    if cfg.get("evolve_sl_tp"):
        space.update(SLTP_SPACE)
        space.update(EXIT_SPACES.get(str(cfg.get("exit_management", "ninguna")), {}))
    return space


def normalize_params(
    params: dict[str, Any], space: dict[str, tuple[type, float, float]] | None = None
) -> dict[str, Any]:
    """Convierte y limita los parámetros al espacio de búsqueda."""
    if space is None:
        space = {**STRATEGY_SPACES[strategy_of(params)],
                 **{k: v for k, v in EXTRA_SPACE.items() if k in params}}
    out: dict[str, Any] = {}
    for name, (kind, lo, hi) in space.items():
        value = float(params[name])
        value = min(max(value, lo), hi)
        out[name] = int(round(value)) if kind is int else round(value, 4)
    return out


def params_are_valid(params: dict[str, Any]) -> bool:
    """Reglas de coherencia (cruce de medias: la media lenta debe ser mayor que la rápida)."""
    if strategy_of(params) == "cruce_medias":
        return int(params["slow"]) > int(params["fast"])
    return True


def _fresh(cond: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """True en la vela donde ``cond`` pasa de falso a verdadero (y los datos son válidos)."""
    prev = np.roll(cond, 1)
    prev[0] = True  # nunca señal en la primera vela
    valid_prev = np.roll(valid, 1)
    valid_prev[0] = False
    return cond & ~prev & valid & valid_prev


def signal_array(cache: IndicatorCache, params: dict[str, Any]) -> np.ndarray:
    """
    Vector de señales por vela (1 = compra, -1 = venta, 0 = nada).

    La señal de la vela ``i`` sólo usa información disponible al cierre de ``i``.
    La estrategia se deduce de los parámetros (ver ``strategy_of``).
    """
    name = strategy_of(params)
    r = cache.rsi
    c = cache.close_np
    sig = np.zeros(len(cache), dtype=np.int8)

    if name == "cruce_medias":
        fast = cache.sma(int(params["fast"]))
        slow = cache.sma(int(params["slow"]))
        valid = ~(np.isnan(fast) | np.isnan(slow) | np.isnan(r))
        above = fast > slow
        cross_up = _fresh(above, valid)
        cross_dn = _fresh(~above, valid)
        sig[cross_up & (r > float(params["rsi_buy"]))] = 1
        sig[cross_dn & (r < float(params["rsi_sell"]))] = -1

    elif name == "reversion_media":
        period, k = int(params["bb_period"]), float(params["bb_std"])
        mid, sd = cache.sma(period), cache.rolling_std(period)
        lower, upper = mid - k * sd, mid + k * sd
        valid = ~(np.isnan(lower) | np.isnan(r))
        sig[_fresh(c < lower, valid) & (r < float(params["rsi_low"]))] = 1
        sig[_fresh(c > upper, valid) & (r > float(params["rsi_high"]))] = -1

    elif name == "ruptura":
        hh, ll = cache.highest(int(params["lookback"])), cache.lowest(int(params["lookback"]))
        trend = cache.sma(int(params["trend"]))
        valid = ~(np.isnan(hh) | np.isnan(ll) | np.isnan(trend))
        sig[_fresh(c > hh, valid) & (c > trend)] = 1
        sig[_fresh(c < ll, valid) & (c < trend)] = -1

    if "adx" in params:  # filtro de régimen
        level = float(params["adx"])
        with np.errstate(invalid="ignore"):
            ok = cache.adx < level if name == "reversion_media" else cache.adx > level
        sig[~np.nan_to_num(ok, nan=0).astype(bool)] = 0

    return sig


def describe_state(cache: IndicatorCache, params: dict[str, Any], i: int) -> tuple[str, str]:
    """
    Texto de los indicadores y de la condición que falta para la próxima señal
    en la vela ``i`` (incluye el filtro ADX si está activo). Devuelve (indicadores, espera).
    """
    ind, wait = _describe_entry(cache, params, i)
    if "adx" in params:
        value, level = float(cache.adx[i]), float(params["adx"])
        need = "<" if strategy_of(params) == "reversion_media" else ">"
        ok = value < level if need == "<" else value > level
        ind += f" ADX {value:.1f}"
        wait += f" · ADX {value:.1f} (necesita {need} {level:.1f}{': OK' if ok else ': aún no'})"
    return ind, wait


def _describe_entry(cache: IndicatorCache, params: dict[str, Any], i: int) -> tuple[str, str]:
    """Textos de la condición de entrada propia de cada estrategia."""
    name = strategy_of(params)
    c, r = float(cache.close_np[i]), float(cache.rsi[i])
    if name == "cruce_medias":
        fast = float(cache.sma(int(params["fast"]))[i])
        slow = float(cache.sma(int(params["slow"]))[i])
        ind = f"rápida {fast:.6g} lenta {slow:.6g} RSI {r:.1f}"
        if fast > slow:
            wait = (f"rápida > lenta: esperando cruce a la BAJA con RSI < "
                    f"{float(params['rsi_sell']):.1f} (RSI {r:.1f})")
        else:
            wait = (f"rápida < lenta: esperando cruce al ALZA con RSI > "
                    f"{float(params['rsi_buy']):.1f} (RSI {r:.1f})")
        return ind, wait
    if name == "reversion_media":
        period, k = int(params["bb_period"]), float(params["bb_std"])
        mid, sd = float(cache.sma(period)[i]), float(cache.rolling_std(period)[i])
        lower, upper = mid - k * sd, mid + k * sd
        ind = f"banda inf {lower:.6g} media {mid:.6g} banda sup {upper:.6g} RSI {r:.1f}"
        wait = (f"esperando cierre bajo {lower:.6g} con RSI < {float(params['rsi_low']):.1f} "
                f"(compra) o sobre {upper:.6g} con RSI > {float(params['rsi_high']):.1f} "
                f"(venta) · cierre {c:.6g} RSI {r:.1f}")
        return ind, wait
    hh = float(cache.highest(int(params["lookback"]))[i])
    ll = float(cache.lowest(int(params["lookback"]))[i])
    trend = float(cache.sma(int(params["trend"]))[i])
    ind = f"máx {params['lookback']} velas {hh:.6g} mín {ll:.6g} tendencia {trend:.6g}"
    if c > trend:
        wait = f"tendencia alcista: esperando cierre por ENCIMA de {hh:.6g} (cierre {c:.6g})"
    else:
        wait = f"tendencia bajista: esperando cierre por DEBAJO de {ll:.6g} (cierre {c:.6g})"
    return ind, wait


def compute_signal(df: pd.DataFrame, params: dict[str, Any], atr_period: int = 14) -> dict[str, Any]:
    """
    Señal en la ÚLTIMA vela del DataFrame (debe ser una vela cerrada).

    :returns: dict con ``signal`` (1/-1/0), ``atr``, ``close``, ``rsi``, ``strategy``,
              ``indicators`` y ``waiting`` (textos), ``fast_ma``/``slow_ma`` (solo en
              cruce de medias, si no None) y ``bar_time``.
    """
    cache = IndicatorCache(df, atr_period)
    sig = signal_array(cache, params)
    last = len(cache) - 1
    indicators, waiting = describe_state(cache, params, last)
    cross = strategy_of(params) == "cruce_medias"
    return {
        "signal": int(sig[last]),
        "strategy": strategy_of(params),
        "atr": float(cache.atr[last]),
        "close": float(cache.close_np[last]),
        "rsi": float(cache.rsi[last]),
        "fast_ma": float(cache.sma(int(params["fast"]))[last]) if cross else None,
        "slow_ma": float(cache.sma(int(params["slow"]))[last]) if cross else None,
        "indicators": indicators,
        "waiting": waiting,
        "bar_time": cache.df["time"].iloc[last] if "time" in cache.df.columns else last,
    }


# ---------------------------------------------------------------------------
# Modo acciones
# ---------------------------------------------------------------------------
def detect_session_hours(df: pd.DataFrame, min_share: float = 0.6) -> str:
    """
    Deduce el horario de negociación (hora del servidor) a partir de las velas.

    Devuelve '' si el mercado cotiza (casi) 24 h; si no, 'HH-HH' con las horas en las
    que hay velas en al menos ``min_share`` de los días con datos.
    """
    if df is None or "time" not in df.columns or len(df) < 50:
        return ""
    times = pd.to_datetime(df["time"])
    days = times.dt.normalize()
    n_days = days.nunique()
    if n_days < 3:
        return ""
    per_hour = times.dt.hour.groupby(times.dt.hour).apply(lambda h: days[h.index].nunique())
    active = sorted(int(h) for h, d in per_hour.items() if d >= min_share * n_days)
    if not active or len(active) >= 22:
        return ""
    # Ventana contigua más larga (puede cruzar medianoche)
    hours = set(active)
    best_start, best_len = active[0], 0
    for h in active:
        if (h - 1) % 24 in hours:
            continue
        length = 0
        while (h + length) % 24 in hours and length < 24:
            length += 1
        if length > best_len:
            best_start, best_len = h, length
    return f"{best_start}-{(best_start + best_len) % 24 or 24}"


def asset_config(config: dict[str, Any], asset_class: str, long_only: bool,
                 df: pd.DataFrame | None = None) -> dict[str, Any]:
    """
    Configuración efectiva según el tipo de activo (bot, laboratorio y explorador).

    Para acciones: horario de sesión (el del perfil o el detectado en ``df``),
    evitar la apertura y cerrar antes del cierre (-1 = 15 min), solo compras si el
    bróker no permite vender y comisión en % del precio (``stock_commission_pct``),
    salvo que el perfil fije ``commission_override``. Para el resto, -1 = desactivado.
    """
    cfg = dict(config)
    is_stock = asset_class == "accion"
    for key, default in (("avoid_open_minutes", STOCK_DEFAULT_AVOID_OPEN_MINUTES),
                         ("flat_before_close_minutes", STOCK_DEFAULT_FLAT_BEFORE_CLOSE_MINUTES)):
        value = int(cfg.get(key, -1) if cfg.get(key) is not None else -1)
        cfg[key] = (default if is_stock else 0) if value < 0 else value
    cfg["long_only"] = bool(cfg.get("long_only")) or bool(long_only)
    if is_stock and not str(cfg.get("session_hours") or "").strip() and df is not None:
        cfg["session_hours"] = detect_session_hours(df)
    cfg["commission_pct"] = float(cfg.get("stock_commission_pct", 0.001)) if is_stock else 0.0
    if cfg.get("commission_override") is not None:
        cfg["commission_per_trade"] = float(cfg["commission_override"])
        cfg["commission_pct"] = 0.0
    return cfg


# ---------------------------------------------------------------------------
# Backtest
# ---------------------------------------------------------------------------
@dataclass
class BacktestMetrics:
    """Métricas de un backtest (unidades de precio por 1 unidad de volumen)."""

    trades: int = 0
    wins: int = 0
    losses: int = 0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    net_profit: float = 0.0
    profit_factor: float = 0.0
    win_rate: float = 0.0
    max_drawdown: float = 0.0
    pnls: list[float] = field(default_factory=list, repr=False)
    #: (vela de entrada, vela de salida, lado) de cada operación
    log: list[tuple[int, int, int]] = field(default_factory=list, repr=False)

    def to_dict(self) -> dict[str, Any]:
        """Representación serializable (redondeada, sin la lista de operaciones)."""
        return {k: (round(v, 6) if isinstance(v, float) else v)
                for k, v in asdict(self).items() if k not in ("pnls", "log")}


def metrics_from_pnls(pnls: list[float], pf_cap: float = 10.0) -> BacktestMetrics:
    """Calcula las métricas a partir de la lista de resultados de cada operación."""
    m = BacktestMetrics(trades=len(pnls), pnls=list(pnls))
    if not pnls:
        return m
    arr = np.asarray(pnls, dtype=float)
    m.wins = int((arr > 0).sum())
    m.losses = int((arr <= 0).sum())
    m.gross_profit = float(arr[arr > 0].sum())
    m.gross_loss = float(-arr[arr <= 0].sum())
    m.net_profit = float(arr.sum())
    m.win_rate = m.wins / m.trades
    if m.gross_loss > 0:
        m.profit_factor = min(m.gross_profit / m.gross_loss, pf_cap)
    else:
        m.profit_factor = pf_cap if m.gross_profit > 0 else 0.0
    equity = np.cumsum(arr)
    m.max_drawdown = float((np.maximum.accumulate(np.maximum(equity, 0.0)) - equity).max())
    return m


def backtest(
    cache: IndicatorCache,
    params: dict[str, Any],
    start: int,
    end: int,
    config: dict[str, Any] | None = None,
) -> BacktestMetrics:
    """
    Backtest de una posición a la vez en el rango de velas [start, end).

    * Entrada a la apertura de la vela siguiente a la señal, si esa vela está
      dentro del horario permitido (``trading_hours``).
    * SL/TP con el ATR de la vela de la señal (multiplicadores evolucionados o fijos).
    * Si SL y TP se tocan en la misma vela se asume SL (conservador).
    * Gestión de la salida (``exit_management``), aplicada al cierre de cada vela:
      ``breakeven`` mueve el SL a la entrada cuando el precio avanza ``be_mult`` x ATR;
      ``trailing`` arrastra el SL a ``trail_mult`` x ATR del mejor precio.
    * ``max_bars_in_trade`` > 0: cierre al cierre de la vela N.
    * Posición abierta al final del rango -> se cierra al último cierre.
    * Coste: ``commission_per_trade`` + spread histórico de la vela de entrada
      + ``commission_pct`` x precio de entrada (acciones).
    * Modo acciones: solo entra dentro de ``session_hours``, no en las primeras
      velas de la sesión (``avoid_open_minutes``) ni justo antes del cierre, cierra al
      final de la sesión (``flat_before_close_minutes``) y, con ``long_only``, solo compra.
    """
    cfg = {**_DEFAULTS, **(config or {})}
    commission = float(cfg["commission_per_trade"])
    sl_mult = float(params.get("sl_mult", cfg["sl_atr_mult"]))
    tp_mult = float(params.get("tp_mult", cfg["tp_atr_mult"]))
    mode = str(cfg.get("exit_management", "ninguna"))
    be_mult = float(params.get("be_mult", cfg["be_atr_mult"]))
    trail_mult = float(params.get("trail_mult", cfg["trail_atr_mult"]))
    max_bars = int(cfg.get("max_bars_in_trade", 0) or 0)

    sig = signal_array(cache, params)
    allowed = (cache.session_allowed(str(cfg.get("trading_hours", "")))
               & cache.session_allowed(str(cfg.get("session_hours", ""))))
    bar_min = cache.bar_seconds / 60.0 if cache.bar_seconds else 0.0
    avoid = int(cfg.get("avoid_open_minutes", 0) or 0)
    if avoid > 0 and bar_min > 0:
        allowed = allowed & (cache.bars_since_open >= math.ceil(avoid / bar_min))
    flat = int(cfg.get("flat_before_close_minutes", 0) or 0)
    flat_bars = max(1, math.ceil(flat / bar_min)) if flat > 0 and bar_min > 0 else 0
    if flat_bars:
        allowed = allowed & (cache.bars_to_close >= flat_bars)  # no entrar justo antes del cierre
    long_only = bool(cfg.get("long_only", False))
    pct = float(cfg.get("commission_pct", 0.0) or 0.0)
    o, hi, lo, c, a = cache.open_, cache.high, cache.low, cache.close_np, cache.atr
    end = min(end, len(cache))

    pnls: list[float] = []
    log: list[tuple[int, int, int]] = []
    i = max(start, 1)
    while i < end - 1:
        side = sig[i]
        atr_i = a[i]
        if side == 0 or not allowed[i + 1] or not math.isfinite(atr_i) or atr_i <= 0 \
                or (long_only and side == -1):
            i += 1
            continue
        entry = o[i + 1]
        if side == 1:
            sl, tp = entry - sl_mult * atr_i, entry + tp_mult * atr_i
        else:
            sl, tp = entry + sl_mult * atr_i, entry - tp_mult * atr_i
        best = entry

        exit_price = c[end - 1]
        exit_idx = end - 1
        for j in range(i + 1, end):
            if side == 1:
                if lo[j] <= sl:
                    exit_price, exit_idx = sl, j
                    break
                if hi[j] >= tp:
                    exit_price, exit_idx = tp, j
                    break
            else:
                if hi[j] >= sl:
                    exit_price, exit_idx = sl, j
                    break
                if lo[j] <= tp:
                    exit_price, exit_idx = tp, j
                    break
            if max_bars and (j - i) >= max_bars:
                exit_price, exit_idx = c[j], j
                break
            if flat_bars and cache.bars_to_close[j] < flat_bars:  # cierre antes del fin de sesión
                exit_price, exit_idx = c[j], j
                break
            # Gestión de la salida con la información de la vela j (vale desde la j+1).
            if side == 1:
                best = max(best, hi[j])
                if mode == "breakeven" and best - entry >= be_mult * atr_i:
                    sl = max(sl, entry)
                elif mode == "trailing":
                    sl = max(sl, best - trail_mult * atr_i)
            else:
                best = min(best, lo[j])
                if mode == "breakeven" and entry - best >= be_mult * atr_i:
                    sl = min(sl, entry)
                elif mode == "trailing":
                    sl = min(sl, best + trail_mult * atr_i)

        pnl = (exit_price - entry) * side - commission - cache.spread[i + 1] - pct * entry
        pnls.append(float(pnl))
        log.append((i + 1, exit_idx, int(side)))
        i = max(exit_idx, i + 1)  # tras salir se pueden buscar nuevas señales

    metrics = metrics_from_pnls(pnls, float(cfg["pf_cap"]))
    metrics.log = log
    return metrics


def fitness_from_metrics(metrics: BacktestMetrics, min_trades: int) -> float:
    """
    Fitness = Profit Factor penalizado por pocas operaciones.

    Si hay menos de ``min_trades`` se multiplica por (trades / min_trades)^2,
    lo que hunde a las "curvas de 1 trade" sin quitar gradiente a la búsqueda.
    """
    if metrics.trades == 0:
        return 0.0
    penalty = min(1.0, metrics.trades / max(1, min_trades)) ** 2
    return float(metrics.profit_factor) * penalty


def fold_bounds(start: int, end: int, folds: int) -> list[tuple[int, int]]:
    """Divide [start, end) en ``folds`` tramos consecutivos de tamaño similar."""
    folds = max(1, int(folds))
    edges = np.linspace(start, end, folds + 1).astype(int)
    return [(int(a), int(b)) for a, b in zip(edges[:-1], edges[1:]) if b > a]


def make_fitness(
    cache: IndicatorCache, start: int, end: int, config: dict[str, Any]
) -> Callable[[dict[str, Any]], float]:
    """
    Crea la función de fitness (params -> float) para el rango indicado.

    Con ``fitness_folds`` = 1 el fitness es el PF penalizado de todo el rango.
    Con ``fitness_folds`` = k > 1 el rango de ENTRENAMIENTO se divide en k tramos
    consecutivos y el fitness es el PEOR de los k (cada tramo exige
    ``ceil(min_trades / k)`` operaciones). Así se premian combinaciones estables en
    todo el periodo, no las que tienen un pico de suerte en un solo tramo.
    Nunca se usan datos de validación (out-of-sample).
    """
    min_trades = int(config.get("min_trades", _DEFAULTS["min_trades"]))
    space = build_search_space(config)
    folds = fold_bounds(start, end, int(config.get("fitness_folds", _DEFAULTS["fitness_folds"])))
    # ceil: la suma de mínimos por tramo nunca queda por debajo de min_trades
    min_trades_fold = max(1, math.ceil(min_trades / len(folds)))

    def _fitness(params: dict[str, Any]) -> float:
        # Sólo las claves presentes: SL/TP ausentes -> se usan los fijos de la configuración.
        p = normalize_params(params, {k: v for k, v in space.items() if k in params})
        if not params_are_valid(p):
            return 0.0  # slow <= fast: inválido
        if len(folds) == 1:
            return fitness_from_metrics(backtest(cache, p, start, end, config), min_trades)
        return min(
            fitness_from_metrics(backtest(cache, p, a, b, config), min_trades_fold)
            for a, b in folds
        )

    return _fitness


# ---------------------------------------------------------------------------
# Backends de optimización
# ---------------------------------------------------------------------------
def _evolve_strategy_genome(
    fitness: Callable[[dict[str, Any]], float], pop: int, gens: int, seed: int | None,
    search_space: dict[str, tuple[type, float, float]],
) -> tuple[dict[str, Any], float]:
    """Optimiza con strategy-genome. Devuelve (mejores parámetros, fitness)."""
    from strategy_genome import evolve, space as sp

    space = {
        name: (sp.Int(int(lo), int(hi)) if kind is int else sp.Float(float(lo), float(hi)))
        for name, (kind, lo, hi) in search_space.items()
    }
    result = evolve(
        lambda genome: fitness(dict(genome.params)),
        space,
        population=pop,
        generations=gens,
        seed=seed,
        direction="maximize",
    )
    if result.best_params is None:
        raise RuntimeError("strategy-genome no devolvió ningún genoma válido")
    best = normalize_params(result.best_params, search_space)
    return best, fitness(best)


def _evolve_deap(
    fitness: Callable[[dict[str, Any]], float], pop: int, gens: int, seed: int | None,
    search_space: dict[str, tuple[type, float, float]],
) -> tuple[dict[str, Any], float]:
    """Optimiza con DEAP (algoritmo genético simple con elitismo)."""
    from deap import algorithms, base, creator, tools

    names = list(search_space)
    lows = [float(search_space[n][1]) for n in names]
    highs = [float(search_space[n][2]) for n in names]

    # creator.create registra clases globales: sólo una vez por proceso.
    if not hasattr(creator, "MT5BotFitnessMax"):
        creator.create("MT5BotFitnessMax", base.Fitness, weights=(1.0,))
    if not hasattr(creator, "MT5BotIndividual"):
        creator.create("MT5BotIndividual", list, fitness=creator.MT5BotFitnessMax)

    if seed is not None:
        random.seed(seed)  # DEAP usa el módulo random global
    rng = random.Random(seed)

    def _decode(ind: list[float]) -> dict[str, Any]:
        return normalize_params(dict(zip(names, ind)), search_space)

    def _clamp(ind: list[float]) -> list[float]:
        for k, (lo, hi) in enumerate(zip(lows, highs)):
            ind[k] = min(max(ind[k], lo), hi)
        return ind

    def _init() -> Any:
        return creator.MT5BotIndividual(rng.uniform(lo, hi) for lo, hi in zip(lows, highs))

    def _mate(a: Any, b: Any) -> tuple[Any, Any]:
        tools.cxBlend(a, b, alpha=0.3)
        return _clamp(a), _clamp(b)

    def _mutate(ind: Any) -> tuple[Any]:
        sigmas = [(hi - lo) * 0.1 for lo, hi in zip(lows, highs)]
        tools.mutGaussian(ind, mu=0.0, sigma=sigmas, indpb=0.3)
        return (_clamp(ind),)

    toolbox = base.Toolbox()
    toolbox.register("individual", _init)
    toolbox.register("population", tools.initRepeat, list, toolbox.individual)
    toolbox.register("evaluate", lambda ind: (fitness(_decode(ind)),))
    toolbox.register("mate", _mate)
    toolbox.register("mutate", _mutate)
    toolbox.register("select", tools.selTournament, tournsize=3)

    population = toolbox.population(n=pop)
    hof = tools.HallOfFame(1)
    algorithms.eaSimple(
        population, toolbox, cxpb=0.6, mutpb=0.3, ngen=gens, halloffame=hof, verbose=False
    )
    best = _decode(hof[0])
    return best, fitness(best)


def _run_backend(
    backend: str,
    fitness: Callable[[dict[str, Any]], float],
    pop: int,
    gens: int,
    seed: int | None,
    search_space: dict[str, tuple[type, float, float]] | None = None,
) -> tuple[dict[str, Any], float, str]:
    """Ejecuta el backend pedido (con fallback automático en modo 'auto')."""
    backend = (backend or "auto").lower()
    search_space = search_space or dict(STRATEGY_SPACES[DEFAULT_STRATEGY])
    if backend in ("auto", "strategy-genome", "strategy_genome"):
        try:
            best, fit = _evolve_strategy_genome(fitness, pop, gens, seed, search_space)
            return best, fit, "strategy-genome"
        except ImportError:
            if backend != "auto":
                raise
            logger.warning("strategy-genome no está instalado; usando DEAP como alternativa.")
        except Exception:
            if backend != "auto":
                raise
            logger.exception("strategy-genome falló; usando DEAP como alternativa.")
    if backend in ("auto", "deap"):
        best, fit = _evolve_deap(fitness, pop, gens, seed, search_space)
        return best, fit, "deap"
    raise ValueError(f"evolver_backend desconocido: {backend!r}")


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------
def walk_forward_windows(n: int, train_ratio: float, k: int) -> list[tuple[int, int, int]]:
    """
    Ventanas walk-forward ancladas: [(fin_entreno, inicio_test, fin_test), ...].

    El tramo de validación (el ``1 - train_ratio`` final) se divide en ``k`` partes
    consecutivas; cada ventana entrena con todo lo anterior a su parte.
    """
    split = int(n * train_ratio)
    k = max(1, int(k))
    size = (n - split) // k
    out = []
    for i in range(k):
        t0 = split + i * size
        t1 = n if i == k - 1 else split + (i + 1) * size
        out.append((t0, t0, t1))
    return out


def evolve_parameters(
    df: pd.DataFrame,
    pop: int = 20,
    gens: int = 10,
    config: dict[str, Any] | None = None,
    seed: int | None = None,
) -> dict[str, Any]:
    """
    Evoluciona los parámetros y los valida con walk-forward.

    * ``validation_windows`` = 1: un solo corte (70% entrena / 30% valida); los
      parámetros elegidos son los del entrenamiento.
    * ``validation_windows`` = K > 1: walk-forward anclado con K ventanas. Cada una
      evoluciona con los datos anteriores y se valida con su tramo; se juntan todas
      las operaciones de validación. Si el proceso supera la validación, los
      parámetros para operar se evolucionan con TODO el historial (los más recientes).

    Criterios de aceptación: parámetros coherentes, suficientes operaciones,
    PF de entrenamiento >= ``min_pf_in_sample``, PF de validación >= ``min_pf_oos``
    y >= ``oos_pf_ratio_min`` x PF de entrenamiento (con tope ``IS_PF_REFERENCE_CAP``),
    y (con K > 1) al menos el 60%
    de las ventanas con PF >= 1.

    :returns: dict con ``accepted``, ``reason``, ``params``, ``in_sample``,
        ``out_of_sample``, ``in_sample_folds``, ``windows``, ``backend``,
        ``strategy``, ``train_bars``, ``test_bars`` y ``validation``.
    """
    cfg = {**_DEFAULTS, **(config or {})}
    if seed is None:
        seed = cfg.get("evolver_seed")

    if df is None or len(df) < MIN_BARS:
        n = 0 if df is None else len(df)
        reason = f"datos insuficientes: {n} velas (mínimo {MIN_BARS})"
        logger.error("Evolución abortada: %s", reason)
        return {"accepted": False, "reason": reason, "params": None,
                "in_sample": None, "out_of_sample": None, "backend": None,
                "strategy": cfg.get("strategy")}

    cache = IndicatorCache(df, int(cfg["atr_period"]))
    n = len(cache)
    split = int(n * float(cfg["train_ratio"]))
    k = int(cfg.get("validation_windows", 1) or 1)
    space = build_search_space(cfg)
    backend_name = str(cfg["evolver_backend"])

    logger.info(
        "Evolucionando parámetros: estrategia %s | %d velas (train %d / test %d) | validación %s | "
        "pop=%d gens=%d | tramos de fitness=%d | SL/TP %s | ADX %s | horario %s | salida %s | backend=%s",
        cfg.get("strategy") or DEFAULT_STRATEGY, n, split, n - split,
        "1 corte" if k == 1 else f"walk-forward {k} ventanas", pop, gens, int(cfg["fitness_folds"]),
        "evolucionados" if cfg.get("evolve_sl_tp") else
        f"fijos ({cfg['sl_atr_mult']}x / {cfg['tp_atr_mult']}x ATR)",
        "sí" if cfg.get("regime_filter") else "no", cfg.get("trading_hours") or "24 h",
        cfg.get("exit_management", "ninguna"), backend_name,
    )

    def _evolve(train_end: int, run_seed: int | None) -> tuple[dict[str, Any], float, str]:
        fitness = make_fitness(cache, 0, train_end, cfg)
        return _run_backend(backend_name, fitness, pop, gens, run_seed, space)

    windows_info: list[dict[str, Any]] = []
    if k <= 1:
        best, best_fit, backend = _evolve(split, seed)
        is_m = backtest(cache, best, 0, split, cfg)
        oos_m = backtest(cache, best, split, n, cfg)
        fold_range = (0, split)
        mean_is_pf = is_m.profit_factor
        positive_ok = True
    else:
        oos_pnls: list[float] = []
        is_pfs: list[float] = []
        for w, (train_end, t0, t1) in enumerate(walk_forward_windows(n, float(cfg["train_ratio"]), k)):
            wp, _wf, backend = _evolve(train_end, None if seed is None else int(seed) + w + 1)
            w_is = backtest(cache, wp, 0, train_end, cfg)
            w_oos = backtest(cache, wp, t0, t1, cfg)
            oos_pnls += w_oos.pnls
            is_pfs.append(w_is.profit_factor)
            windows_info.append({
                "train_end": train_end, "test_start": t0, "test_end": t1, "params": wp,
                "pf_is": round(w_is.profit_factor, 4), "trades_is": w_is.trades,
                "pf_oos": round(w_oos.profit_factor, 4), "trades_oos": w_oos.trades,
            })
        oos_m = metrics_from_pnls(oos_pnls, float(cfg["pf_cap"]))
        mean_is_pf = float(np.mean(is_pfs)) if is_pfs else 0.0
        positives = sum(1 for wi in windows_info if wi["trades_oos"] > 0 and wi["pf_oos"] >= 1.0)
        need = math.ceil(0.6 * k)
        positive_ok = positives >= need
        # Parámetros para operar: evolucionados con todo el historial.
        best, best_fit, backend = _evolve(n, seed)
        is_m = backtest(cache, best, 0, n, cfg)
        fold_range = (0, n)

    is_folds = [backtest(cache, best, a, b, cfg).to_dict()
                for a, b in fold_bounds(*fold_range, int(cfg["fitness_folds"]))]

    reasons: list[str] = []
    if not params_are_valid(best):
        reasons.append(f"parámetros inválidos: {best}")
    if is_m.trades < int(cfg["min_trades"]):
        reasons.append(f"pocas operaciones in-sample ({is_m.trades} < {cfg['min_trades']})")
    if is_m.profit_factor < float(cfg["min_pf_in_sample"]):
        reasons.append(
            f"PF in-sample {is_m.profit_factor:.3f} < mínimo {float(cfg['min_pf_in_sample']):.3f}"
        )
    if oos_m.trades < int(cfg["min_trades_oos"]):
        reasons.append(f"pocas operaciones out-of-sample ({oos_m.trades} < {cfg['min_trades_oos']})")
    if oos_m.profit_factor < float(cfg.get("min_pf_oos", 0.0)):
        reasons.append(f"PF out-of-sample {oos_m.profit_factor:.3f} < mínimo {float(cfg['min_pf_oos']):.2f}")
    threshold = float(cfg["oos_pf_ratio_min"]) * min(mean_is_pf, IS_PF_REFERENCE_CAP)
    if oos_m.profit_factor < threshold:
        reasons.append(
            f"PF out-of-sample {oos_m.profit_factor:.3f} < {float(cfg['oos_pf_ratio_min']):.2f} x "
            f"PF in-sample ({threshold:.3f})"
        )
    if not positive_ok:
        positives = sum(1 for wi in windows_info if wi["trades_oos"] > 0 and wi["pf_oos"] >= 1.0)
        reasons.append(f"solo {positives} de {k} ventanas de validación con PF >= 1")

    accepted = not reasons
    reason = "validación walk-forward superada" if accepted else "; ".join(reasons)
    result = {
        "accepted": accepted,
        "reason": reason,
        "params": best,
        "fitness_in_sample": round(best_fit, 6),
        "in_sample": is_m.to_dict(),
        "in_sample_folds": [
            {"profit_factor": f["profit_factor"], "trades": f["trades"]} for f in is_folds
        ],
        "out_of_sample": oos_m.to_dict(),
        "windows": windows_info,
        "validation": "1 corte" if k <= 1 else f"walk-forward {k} ventanas",
        "backend": backend,
        "strategy": strategy_of(best),
        "train_bars": split if k <= 1 else n,
        "test_bars": n - split,
    }
    log = logger.info if accepted else logger.warning
    log(
        "Resultado evolución [%s]: %s | params=%s | IS: PF=%.3f trades=%d (por tramo: %s) | "
        "OOS: PF=%.3f trades=%d%s",
        "ACEPTADO" if accepted else "DESCARTADO", reason, best,
        is_m.profit_factor, is_m.trades,
        ", ".join(f"{f['profit_factor']:.2f}/{f['trades']}" for f in is_folds),
        oos_m.profit_factor, oos_m.trades,
        (" | ventanas: " + ", ".join(f"{w['pf_oos']:.2f}/{w['trades_oos']}" for w in windows_info))
        if windows_info else "",
    )
    return result


__all__ = [
    "SEARCH_SPACE", "SLTP_SPACE", "STRATEGY_SPACES", "STRATEGY_LABELS", "DEFAULT_STRATEGY",
    "strategy_of", "warmup_bars", "describe_state", "walk_forward_windows", "metrics_from_pnls",
    "REGIME_SPACE", "EXIT_SPACES", "EXTRA_SPACE", "adx", "detect_session_hours", "asset_config", "build_search_space", "evolve_parameters", "backtest", "compute_signal", "signal_array",
    "IndicatorCache", "BacktestMetrics", "fitness_from_metrics", "make_fitness",
    "normalize_params", "params_are_valid", "fold_bounds", "sma", "rsi", "atr",
]
