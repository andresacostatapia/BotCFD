"""Tests del evolucionador (sin MetaTrader5)."""

from __future__ import annotations

import importlib.util

import numpy as np
import pandas as pd
import pytest

import strategy_evolver as se

BACKENDS = [b for b, mod in (("strategy-genome", "strategy_genome"), ("deap", "deap"))
            if importlib.util.find_spec(mod) is not None]


def test_module_does_not_import_mt5() -> None:
    assert "mt5" not in vars(se)


def test_indicators_shapes(synthetic_ohlc: pd.DataFrame) -> None:
    r = se.rsi(synthetic_ohlc["close"])
    a = se.atr(synthetic_ohlc)
    assert len(r) == len(a) == len(synthetic_ohlc)
    assert r.dropna().between(0, 100).all()
    assert (a.dropna() > 0).all()


def test_signals_have_no_lookahead(synthetic_ohlc: pd.DataFrame) -> None:
    params = {"fast": 10, "slow": 40, "rsi_buy": 50.0, "rsi_sell": 50.0}
    full = se.signal_array(se.IndicatorCache(synthetic_ohlc), params)
    cut = 1200
    partial = se.signal_array(se.IndicatorCache(synthetic_ohlc.iloc[:cut]), params)
    np.testing.assert_array_equal(full[:cut], partial)


def test_compute_signal_matches_array(synthetic_ohlc: pd.DataFrame) -> None:
    params = {"fast": 10, "slow": 40, "rsi_buy": 50.0, "rsi_sell": 50.0}
    sig = se.signal_array(se.IndicatorCache(synthetic_ohlc), params)
    idx = int(np.flatnonzero(sig)[5])
    out = se.compute_signal(synthetic_ohlc.iloc[: idx + 1], params)
    assert out["signal"] == sig[idx]
    assert out["atr"] > 0


def test_backtest_metrics_consistent(synthetic_ohlc: pd.DataFrame) -> None:
    cache = se.IndicatorCache(synthetic_ohlc)
    m = se.backtest(cache, {"fast": 10, "slow": 40, "rsi_buy": 50.0, "rsi_sell": 50.0},
                    0, len(cache))
    assert m.trades == m.wins + m.losses > 0
    assert m.net_profit == pytest.approx(m.gross_profit - m.gross_loss)
    assert 0 <= m.profit_factor <= 10.0


def test_commission_reduces_profit(synthetic_ohlc: pd.DataFrame) -> None:
    cache = se.IndicatorCache(synthetic_ohlc)
    p = {"fast": 10, "slow": 40, "rsi_buy": 50.0, "rsi_sell": 50.0}
    cheap = se.backtest(cache, p, 0, len(cache), {"commission_per_trade": 0.0})
    pricey = se.backtest(cache, p, 0, len(cache), {"commission_per_trade": 0.0005})
    assert pricey.net_profit < cheap.net_profit


def test_fitness_penalizes_few_trades() -> None:
    few = se.BacktestMetrics(trades=2, profit_factor=10.0)
    many = se.BacktestMetrics(trades=40, profit_factor=1.5)
    assert se.fitness_from_metrics(few, 20) < se.fitness_from_metrics(many, 20)


def test_invalid_params_get_zero_fitness(synthetic_ohlc: pd.DataFrame) -> None:
    fit = se.make_fitness(se.IndicatorCache(synthetic_ohlc), 0, 1400, {})
    assert fit({"fast": 30, "slow": 30, "rsi_buy": 50, "rsi_sell": 50}) == 0.0


def test_insufficient_data_rejected(synthetic_ohlc: pd.DataFrame) -> None:
    out = se.evolve_parameters(synthetic_ohlc.iloc[:100])
    assert out["accepted"] is False
    assert "insuficientes" in out["reason"]


@pytest.mark.parametrize("backend", BACKENDS)
def test_evolve_returns_walk_forward_report(synthetic_ohlc: pd.DataFrame, backend: str) -> None:
    out = se.evolve_parameters(synthetic_ohlc, pop=12, gens=4,
                               config={"evolver_backend": backend}, seed=1)
    assert out["backend"] == backend
    p = out["params"]
    assert 5 <= p["fast"] <= 30 and 30 <= p["slow"] <= 100
    assert 40 <= p["rsi_buy"] <= 70 and 30 <= p["rsi_sell"] <= 60
    assert out["train_bars"] == 1400 and out["test_bars"] == 600
    assert {"profit_factor", "trades"} <= set(out["in_sample"])
    assert {"profit_factor", "trades"} <= set(out["out_of_sample"])
    is_pf = out["in_sample"]["profit_factor"]
    oos_pf = out["out_of_sample"]["profit_factor"]
    if oos_pf < 0.8 * is_pf:
        assert out["accepted"] is False


@pytest.mark.parametrize("backend", BACKENDS)
def test_evolution_is_reproducible(synthetic_ohlc: pd.DataFrame, backend: str) -> None:
    cfg = {"evolver_backend": backend}
    a = se.evolve_parameters(synthetic_ohlc, pop=10, gens=3, config=cfg, seed=7)
    b = se.evolve_parameters(synthetic_ohlc, pop=10, gens=3, config=cfg, seed=7)
    assert a["params"] == b["params"]


def test_historical_spread_is_a_cost(synthetic_ohlc: pd.DataFrame) -> None:
    p = {"fast": 10, "slow": 40, "rsi_buy": 50.0, "rsi_sell": 50.0}
    base = se.backtest(se.IndicatorCache(synthetic_ohlc), p, 0, len(synthetic_ohlc))
    wide = synthetic_ohlc.assign(spread_price=0.0003)
    costly = se.backtest(se.IndicatorCache(wide), p, 0, len(wide))
    assert costly.trades == base.trades
    assert costly.net_profit == pytest.approx(base.net_profit - 0.0003 * base.trades)
