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

Estrategia (educativa)
----------------------
* COMPRA: la SMA rápida cruza por encima de la lenta y RSI(14) > ``rsi_buy``.
* VENTA:  la SMA rápida cruza por debajo de la lenta y RSI(14) < ``rsi_sell``.
* Salida: SL = 1.5 x ATR, TP = 2.0 x ATR (igual que en el bot en vivo).

Este módulo NO importa MetaTrader5: recibe un DataFrame OHLC por parámetro.
"""

from __future__ import annotations

import logging
import math
import random
from dataclasses import asdict, dataclass
from typing import Any, Callable

import numpy as np
import pandas as pd

logger = logging.getLogger("strategy_evolver")

#: Espacio de búsqueda: nombre -> (tipo, mínimo, máximo)
SEARCH_SPACE: dict[str, tuple[type, float, float]] = {
    "fast": (int, 5, 30),
    "slow": (int, 30, 100),
    "rsi_buy": (float, 40.0, 70.0),
    "rsi_sell": (float, 30.0, 60.0),
}

RSI_PERIOD: int = 14
MIN_BARS: int = 300

_DEFAULTS: dict[str, Any] = {
    "commission_per_trade": 0.00007,
    "min_trades": 20,
    "min_trades_oos": 5,
    "train_ratio": 0.70,
    "oos_pf_ratio_min": 0.80,
    "min_pf_in_sample": 1.0,
    "pf_cap": 10.0,
    "atr_period": 14,
    "sl_atr_mult": 1.5,
    "tp_atr_mult": 2.0,
    "evolver_backend": "auto",
    "evolver_seed": None,
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
def normalize_params(params: dict[str, Any]) -> dict[str, Any]:
    """Convierte y limita los parámetros al espacio de búsqueda."""
    out: dict[str, Any] = {}
    for name, (kind, lo, hi) in SEARCH_SPACE.items():
        value = float(params[name])
        value = min(max(value, lo), hi)
        out[name] = int(round(value)) if kind is int else round(value, 4)
    return out


def params_are_valid(params: dict[str, Any]) -> bool:
    """Regla de coherencia: la media lenta debe ser mayor que la rápida."""
    return int(params["slow"]) > int(params["fast"])


def signal_array(cache: IndicatorCache, params: dict[str, Any]) -> np.ndarray:
    """
    Vector de señales por vela (1 = compra, -1 = venta, 0 = nada).

    La señal de la vela ``i`` sólo usa información disponible al cierre de ``i``.
    """
    fast = cache.sma(int(params["fast"]))
    slow = cache.sma(int(params["slow"]))
    r = cache.rsi
    above = fast > slow
    prev_above = np.roll(above, 1)
    valid = ~(np.isnan(fast) | np.isnan(slow) | np.isnan(r))
    valid_prev = np.roll(valid, 1)
    valid_prev[0] = False
    cross_up = above & ~prev_above & valid & valid_prev
    cross_dn = ~above & prev_above & valid & valid_prev
    sig = np.zeros(len(cache), dtype=np.int8)
    sig[cross_up & (r > float(params["rsi_buy"]))] = 1
    sig[cross_dn & (r < float(params["rsi_sell"]))] = -1
    return sig


def compute_signal(df: pd.DataFrame, params: dict[str, Any], atr_period: int = 14) -> dict[str, Any]:
    """
    Señal en la ÚLTIMA vela del DataFrame (debe ser una vela cerrada).

    :returns: dict con ``signal`` (1/-1/0), ``atr``, ``close``, ``rsi``,
              ``fast_ma``, ``slow_ma`` y ``bar_time`` (si existe la columna time).
    """
    cache = IndicatorCache(df, atr_period)
    sig = signal_array(cache, params)
    last = len(cache) - 1
    return {
        "signal": int(sig[last]),
        "atr": float(cache.atr[last]),
        "close": float(cache.close_np[last]),
        "rsi": float(cache.rsi[last]),
        "fast_ma": float(cache.sma(int(params["fast"]))[last]),
        "slow_ma": float(cache.sma(int(params["slow"]))[last]),
        "bar_time": cache.df["time"].iloc[last] if "time" in cache.df.columns else last,
    }


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

    def to_dict(self) -> dict[str, Any]:
        """Representación serializable (redondeada)."""
        return {k: (round(v, 6) if isinstance(v, float) else v) for k, v in asdict(self).items()}


def backtest(
    cache: IndicatorCache,
    params: dict[str, Any],
    start: int,
    end: int,
    config: dict[str, Any] | None = None,
) -> BacktestMetrics:
    """
    Backtest simple de una posición a la vez en el rango de velas [start, end).

    * Entrada a la apertura de la vela siguiente a la señal.
    * SL/TP con ATR de la vela de la señal.
    * Si SL y TP se tocan en la misma vela se asume SL (conservador).
    * Posición abierta al final del rango -> se cierra al último cierre.
    * Se resta ``commission_per_trade`` (precio) a cada operación y, si los
      datos traen la columna ``spread_price``, también el spread de la vela de entrada.
    """
    cfg = {**_DEFAULTS, **(config or {})}
    commission = float(cfg["commission_per_trade"])
    sl_mult, tp_mult = float(cfg["sl_atr_mult"]), float(cfg["tp_atr_mult"])
    pf_cap = float(cfg["pf_cap"])

    sig = signal_array(cache, params)
    o, hi, lo, c, a = cache.open_, cache.high, cache.low, cache.close_np, cache.atr
    end = min(end, len(cache))

    pnls: list[float] = []
    i = max(start, 1)
    while i < end - 1:
        side = sig[i]
        atr_i = a[i]
        if side == 0 or not math.isfinite(atr_i) or atr_i <= 0:
            i += 1
            continue
        entry = o[i + 1]
        if side == 1:
            sl, tp = entry - sl_mult * atr_i, entry + tp_mult * atr_i
        else:
            sl, tp = entry + sl_mult * atr_i, entry - tp_mult * atr_i

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

        # Coste: comisión fija + spread histórico de la vela de entrada.
        pnl = (exit_price - entry) * side - commission - cache.spread[i + 1]
        pnls.append(pnl)
        i = max(exit_idx, i + 1)  # tras salir se pueden buscar nuevas señales

    m = BacktestMetrics(trades=len(pnls))
    if not pnls:
        return m
    arr = np.asarray(pnls)
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


def make_fitness(
    cache: IndicatorCache, start: int, end: int, config: dict[str, Any]
) -> Callable[[dict[str, Any]], float]:
    """Crea la función de fitness (params -> float) para el rango indicado."""
    min_trades = int(config.get("min_trades", _DEFAULTS["min_trades"]))

    def _fitness(params: dict[str, Any]) -> float:
        p = normalize_params(params)
        if not params_are_valid(p):
            return 0.0  # slow <= fast: inválido
        return fitness_from_metrics(backtest(cache, p, start, end, config), min_trades)

    return _fitness


# ---------------------------------------------------------------------------
# Backends de optimización
# ---------------------------------------------------------------------------
def _evolve_strategy_genome(
    fitness: Callable[[dict[str, Any]], float], pop: int, gens: int, seed: int | None
) -> tuple[dict[str, Any], float]:
    """Optimiza con strategy-genome. Devuelve (mejores parámetros, fitness)."""
    from strategy_genome import evolve, space as sp

    space = {
        name: (sp.Int(int(lo), int(hi)) if kind is int else sp.Float(float(lo), float(hi)))
        for name, (kind, lo, hi) in SEARCH_SPACE.items()
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
    best = normalize_params(result.best_params)
    return best, fitness(best)


def _evolve_deap(
    fitness: Callable[[dict[str, Any]], float], pop: int, gens: int, seed: int | None
) -> tuple[dict[str, Any], float]:
    """Optimiza con DEAP (algoritmo genético simple con elitismo)."""
    from deap import algorithms, base, creator, tools

    names = list(SEARCH_SPACE)
    lows = [float(SEARCH_SPACE[n][1]) for n in names]
    highs = [float(SEARCH_SPACE[n][2]) for n in names]

    # creator.create registra clases globales: sólo una vez por proceso.
    if not hasattr(creator, "MT5BotFitnessMax"):
        creator.create("MT5BotFitnessMax", base.Fitness, weights=(1.0,))
    if not hasattr(creator, "MT5BotIndividual"):
        creator.create("MT5BotIndividual", list, fitness=creator.MT5BotFitnessMax)

    if seed is not None:
        random.seed(seed)  # DEAP usa el módulo random global
    rng = random.Random(seed)

    def _decode(ind: list[float]) -> dict[str, Any]:
        return normalize_params(dict(zip(names, ind)))

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
) -> tuple[dict[str, Any], float, str]:
    """Ejecuta el backend pedido (con fallback automático en modo 'auto')."""
    backend = (backend or "auto").lower()
    if backend in ("auto", "strategy-genome", "strategy_genome"):
        try:
            best, fit = _evolve_strategy_genome(fitness, pop, gens, seed)
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
        best, fit = _evolve_deap(fitness, pop, gens, seed)
        return best, fit, "deap"
    raise ValueError(f"evolver_backend desconocido: {backend!r}")


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------
def evolve_parameters(
    df: pd.DataFrame,
    pop: int = 20,
    gens: int = 10,
    config: dict[str, Any] | None = None,
    seed: int | None = None,
) -> dict[str, Any]:
    """
    Evoluciona los parámetros y los valida con Walk-Forward (70% / 30%).

    :param df: velas OHLC ordenadas cronológicamente (sólo velas cerradas).
    :param pop: tamaño de población.
    :param gens: número de generaciones.
    :param config: configuración (comisión, min_trades, ratios, ATR...).
    :param seed: semilla para reproducibilidad (si None, se usa config["evolver_seed"]).
    :returns: dict con las claves
        ``accepted`` (bool), ``reason`` (str), ``params`` (dict),
        ``in_sample`` / ``out_of_sample`` (métricas), ``fitness_in_sample``,
        ``backend``, ``train_bars``, ``test_bars``.
    """
    cfg = {**_DEFAULTS, **(config or {})}
    if seed is None:
        seed = cfg.get("evolver_seed")

    if df is None or len(df) < MIN_BARS:
        n = 0 if df is None else len(df)
        reason = f"datos insuficientes: {n} velas (mínimo {MIN_BARS})"
        logger.error("Evolución abortada: %s", reason)
        return {"accepted": False, "reason": reason, "params": None,
                "in_sample": None, "out_of_sample": None, "backend": None}

    cache = IndicatorCache(df, int(cfg["atr_period"]))
    n = len(cache)
    split = int(n * float(cfg["train_ratio"]))

    logger.info(
        "Evolucionando parámetros: %d velas (train %d / test %d) | pop=%d gens=%d | backend=%s",
        n, split, n - split, pop, gens, cfg["evolver_backend"],
    )
    fitness = make_fitness(cache, 0, split, cfg)
    best, best_fit, backend = _run_backend(str(cfg["evolver_backend"]), fitness, pop, gens, seed)

    is_m = backtest(cache, best, 0, split, cfg)
    oos_m = backtest(cache, best, split, n, cfg)

    reasons: list[str] = []
    if not params_are_valid(best):
        reasons.append(f"parámetros inválidos (slow={best['slow']} <= fast={best['fast']})")
    if is_m.trades < int(cfg["min_trades"]):
        reasons.append(f"pocas operaciones in-sample ({is_m.trades} < {cfg['min_trades']})")
    if is_m.profit_factor < float(cfg["min_pf_in_sample"]):
        reasons.append(
            f"PF in-sample {is_m.profit_factor:.3f} < mínimo {float(cfg['min_pf_in_sample']):.3f}"
        )
    if oos_m.trades < int(cfg["min_trades_oos"]):
        reasons.append(f"pocas operaciones out-of-sample ({oos_m.trades} < {cfg['min_trades_oos']})")
    threshold = float(cfg["oos_pf_ratio_min"]) * is_m.profit_factor
    if oos_m.profit_factor < threshold:
        reasons.append(
            f"PF out-of-sample {oos_m.profit_factor:.3f} < {float(cfg['oos_pf_ratio_min']):.2f} x "
            f"PF in-sample ({threshold:.3f})"
        )

    accepted = not reasons
    reason = "validación walk-forward superada" if accepted else "; ".join(reasons)
    result = {
        "accepted": accepted,
        "reason": reason,
        "params": best,
        "fitness_in_sample": round(best_fit, 6),
        "in_sample": is_m.to_dict(),
        "out_of_sample": oos_m.to_dict(),
        "backend": backend,
        "train_bars": split,
        "test_bars": n - split,
    }
    log = logger.info if accepted else logger.warning
    log(
        "Resultado evolución [%s]: %s | params=%s | IS: PF=%.3f trades=%d | OOS: PF=%.3f trades=%d",
        "ACEPTADO" if accepted else "DESCARTADO", reason, best,
        is_m.profit_factor, is_m.trades, oos_m.profit_factor, oos_m.trades,
    )
    return result


__all__ = [
    "SEARCH_SPACE", "evolve_parameters", "backtest", "compute_signal", "signal_array",
    "IndicatorCache", "BacktestMetrics", "fitness_from_metrics", "make_fitness",
    "normalize_params", "params_are_valid", "sma", "rsi", "atr",
]
