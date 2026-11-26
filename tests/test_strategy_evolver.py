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


def test_fold_bounds_cover_range() -> None:
    folds = se.fold_bounds(0, 1400, 3)
    assert folds[0][0] == 0 and folds[-1][1] == 1400 and len(folds) == 3
    assert all(a < b for a, b in folds)
    assert all(folds[i][1] == folds[i + 1][0] for i in range(2))


def test_fold_fitness_is_worst_fold(synthetic_ohlc: pd.DataFrame) -> None:
    cache = se.IndicatorCache(synthetic_ohlc)
    p = {"fast": 10, "slow": 40, "rsi_buy": 50.0, "rsi_sell": 50.0}
    cfg = {"fitness_folds": 3, "min_trades": 9}
    expected = min(
        se.fitness_from_metrics(se.backtest(cache, p, a, b, cfg), 3)
        for a, b in se.fold_bounds(0, 1400, 3)
    )
    assert se.make_fitness(cache, 0, 1400, cfg)(p) == pytest.approx(expected)
    single = se.make_fitness(cache, 0, 1400, {"fitness_folds": 1, "min_trades": 9})(p)
    assert single == pytest.approx(
        se.fitness_from_metrics(se.backtest(cache, p, 0, 1400, cfg), 9))


def test_evolve_reports_fold_metrics(synthetic_ohlc: pd.DataFrame) -> None:
    out = se.evolve_parameters(synthetic_ohlc, pop=8, gens=2, config={"fitness_folds": 3}, seed=2)
    assert len(out["in_sample_folds"]) == 3


def test_search_space_includes_sl_tp_when_enabled() -> None:
    assert {"sl_mult", "tp_mult"} <= set(se.build_search_space({"evolve_sl_tp": True}))
    assert "sl_mult" not in se.build_search_space({"evolve_sl_tp": False})


def test_backtest_uses_evolved_sl_tp(synthetic_ohlc: pd.DataFrame) -> None:
    cache = se.IndicatorCache(synthetic_ohlc)
    base = {"fast": 10, "slow": 40, "rsi_buy": 50.0, "rsi_sell": 50.0}
    fixed = se.backtest(cache, base, 0, len(cache), {"sl_atr_mult": 1.5, "tp_atr_mult": 2.0})
    same = se.backtest(cache, {**base, "sl_mult": 1.5, "tp_mult": 2.0}, 0, len(cache), {})
    wide = se.backtest(cache, {**base, "sl_mult": 3.0, "tp_mult": 4.0}, 0, len(cache), {})
    assert fixed.to_dict() == same.to_dict()
    assert wide.trades < fixed.trades  # operaciones más largas -> menos operaciones


@pytest.mark.parametrize("backend", BACKENDS)
def test_evolve_with_sl_tp(synthetic_ohlc: pd.DataFrame, backend: str) -> None:
    out = se.evolve_parameters(synthetic_ohlc, pop=10, gens=3,
                               config={"evolver_backend": backend, "evolve_sl_tp": True}, seed=4)
    p = out["params"]
    assert 1.0 <= p["sl_mult"] <= 3.0 and 1.0 <= p["tp_mult"] <= 4.0
    off = se.evolve_parameters(synthetic_ohlc, pop=10, gens=3,
                               config={"evolver_backend": backend, "evolve_sl_tp": False}, seed=4)
    assert "sl_mult" not in off["params"]


STRATEGY_PARAMS = {
    "cruce_medias": {"fast": 10, "slow": 40, "rsi_buy": 50.0, "rsi_sell": 50.0},
    "reversion_media": {"bb_period": 20, "bb_std": 2.0, "rsi_low": 35.0, "rsi_high": 65.0},
    "ruptura": {"lookback": 20, "trend": 50},
    "retroceso": {"trend": 100, "pb_rsi": 20.0, "pb_window": 3},
    "compresion": {"lookback": 20, "squeeze_pct": 0.3, "trend": 100, "pen": 0.1},
}


@pytest.mark.parametrize("name", list(STRATEGY_PARAMS))
def test_each_strategy_signals_without_lookahead(synthetic_ohlc: pd.DataFrame, name: str) -> None:
    params = STRATEGY_PARAMS[name]
    assert se.strategy_of(params) == name
    full = se.signal_array(se.IndicatorCache(synthetic_ohlc), params)
    assert (full == 1).sum() > 0 and (full == -1).sum() > 0
    cut = 1300
    part = se.signal_array(se.IndicatorCache(synthetic_ohlc.iloc[:cut]), params)
    np.testing.assert_array_equal(full[:cut], part)


@pytest.mark.parametrize("name", list(STRATEGY_PARAMS))
def test_compute_signal_texts(synthetic_ohlc: pd.DataFrame, name: str) -> None:
    out = se.compute_signal(synthetic_ohlc, STRATEGY_PARAMS[name])
    assert out["strategy"] == name
    assert out["indicators"] and out["waiting"]
    assert (out["fast_ma"] is None) == (name != "cruce_medias")


def test_breakout_rules(synthetic_ohlc: pd.DataFrame) -> None:
    cache = se.IndicatorCache(synthetic_ohlc)
    p = STRATEGY_PARAMS["ruptura"]
    sig = se.signal_array(cache, p)
    hh, ll, trend = cache.highest(20), cache.lowest(20), cache.sma(50)
    c = cache.close_np
    buys, sells = np.flatnonzero(sig == 1), np.flatnonzero(sig == -1)
    assert np.all(c[buys] > hh[buys]) and np.all(c[buys] > trend[buys])
    assert np.all(c[sells] < ll[sells]) and np.all(c[sells] < trend[sells])
    # el máximo de referencia excluye la vela actual
    assert hh[30] == pytest.approx(synthetic_ohlc["high"].iloc[10:30].max())


def test_bollinger_rules(synthetic_ohlc: pd.DataFrame) -> None:
    cache = se.IndicatorCache(synthetic_ohlc)
    p = STRATEGY_PARAMS["reversion_media"]
    sig = se.signal_array(cache, p)
    lower = cache.sma(20) - 2.0 * cache.rolling_std(20)
    buys = np.flatnonzero(sig == 1)
    assert np.all(cache.close_np[buys] < lower[buys]) and np.all(cache.rsi[buys] < 35.0)


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("name", ["reversion_media", "ruptura", "retroceso", "compresion"])
def test_evolve_new_strategies(synthetic_ohlc: pd.DataFrame, backend: str, name: str) -> None:
    out = se.evolve_parameters(synthetic_ohlc, pop=10, gens=3, seed=3,
                               config={"strategy": name, "evolver_backend": backend})
    assert out["strategy"] == name
    assert set(se.STRATEGY_SPACES[name]) <= set(out["params"])
    assert {"sl_mult", "tp_mult"} <= set(out["params"])
    for key, (_kind, lo, hi) in se.STRATEGY_SPACES[name].items():
        assert lo <= out["params"][key] <= hi


def test_bollinger_confirmation_rules(synthetic_ohlc: pd.DataFrame) -> None:
    cache = se.IndicatorCache(synthetic_ohlc)
    p = {**STRATEGY_PARAMS["reversion_media"], "confirm": 1}
    sig = se.signal_array(cache, p)
    lower = cache.sma(20) - 2.0 * cache.rolling_std(20)
    upper = cache.sma(20) + 2.0 * cache.rolling_std(20)
    c, r = cache.close_np, cache.rsi
    buys, sells = np.flatnonzero(sig == 1), np.flatnonzero(sig == -1)
    assert len(buys) and len(sells)
    # la vela anterior cerró fuera con RSI extremo y esta vuelve dentro
    assert np.all(c[buys - 1] < lower[buys - 1]) and np.all(r[buys - 1] < 35.0)
    assert np.all(c[buys] >= lower[buys])
    assert np.all(c[sells - 1] > upper[sells - 1]) and np.all(c[sells] <= upper[sells])
    # sin el gen (parámetros guardados por versiones anteriores) se mantiene la regla antigua
    old = se.signal_array(cache, STRATEGY_PARAMS["reversion_media"])
    assert not np.array_equal(old, sig)
    assert se.normalize_params(STRATEGY_PARAMS["reversion_media"]) == STRATEGY_PARAMS["reversion_media"]


def test_pullback_rules(synthetic_ohlc: pd.DataFrame) -> None:
    cache = se.IndicatorCache(synthetic_ohlc)
    p = STRATEGY_PARAMS["retroceso"]
    sig = se.signal_array(cache, p)
    trend, rs = cache.sma(100), cache.rsi_short
    c, hi, lo = cache.close_np, cache.high, cache.low
    for i in np.flatnonzero(sig == 1):
        assert c[i] > trend[i] and trend[i] > trend[i - 5] and c[i] > hi[i - 1]
        assert (rs[i - 3:i] <= 20.0).any()
    for i in np.flatnonzero(sig == -1):
        assert c[i] < trend[i] and trend[i] < trend[i - 5] and c[i] < lo[i - 1]
        assert (rs[i - 3:i] >= 80.0).any()


def test_squeeze_rules(synthetic_ohlc: pd.DataFrame) -> None:
    cache = se.IndicatorCache(synthetic_ohlc)
    p = STRATEGY_PARAMS["compresion"]
    sig = se.signal_array(cache, p)
    hh, ll, trend, rank, a = cache.highest(20), cache.lowest(20), cache.sma(100), cache.width_rank, cache.atr
    c = cache.close_np
    for i in np.flatnonzero(sig == 1):
        assert c[i] > hh[i] + 0.1 * a[i] and c[i] > trend[i]
        assert (rank[i - se.SQUEEZE_RECENT_BARS:i] <= 0.3).any()
    for i in np.flatnonzero(sig == -1):
        assert c[i] < ll[i] - 0.1 * a[i] and c[i] < trend[i]
    assert np.nanmin(rank) > 0 and np.nanmax(rank) <= 1


def test_adx_direction_for_new_strategies(synthetic_ohlc: pd.DataFrame) -> None:
    cache = se.IndicatorCache(synthetic_ohlc)
    for name in ("retroceso", "compresion"):
        sig = se.signal_array(cache, {**STRATEGY_PARAMS[name], "adx": 25.0})
        assert np.all(cache.adx[sig != 0] > 25.0)


def test_parameter_stability_and_monte_carlo(synthetic_ohlc: pd.DataFrame) -> None:
    cache = se.IndicatorCache(synthetic_ohlc)
    p = {**STRATEGY_PARAMS["ruptura"], "sl_mult": 1.5, "tp_mult": 2.0}
    stab = se.parameter_stability(cache, p, 0, len(cache), {}, samples=6, seed=1)
    assert stab["samples"] == 6 and stab["min_pf"] <= stab["median_pf"]
    assert 0.0 <= stab["profitable_share"] <= 1.0
    assert se.parameter_stability(cache, p, 0, len(cache), {}, samples=6, seed=1) == stab
    assert se.monte_carlo([1.0, -1.0]) is None
    mc = se.monte_carlo([2.0, -1.0, 1.5, -1.0, -1.0, 3.0, -1.0, 1.0], runs=500, seed=3)
    assert mc["trades"] == 8 and 0.0 <= mc["p_loss"] <= 1.0
    assert 0.0 <= mc["dd50_losses"] <= mc["dd95_losses"]
    losers = se.monte_carlo([-1.0] * 6, runs=200)
    assert losers["p_loss"] == 1.0 and losers["dd50_losses"] == 6.0


def test_stability_check_rejects_fragile(synthetic_ohlc: pd.DataFrame, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(se, "parameter_stability",
                        lambda *a, **k: {"samples": 8, "median_pf": 0.7, "min_pf": 0.5, "profitable_share": 0.1})
    out = se.evolve_parameters(synthetic_ohlc, pop=8, gens=2, seed=2,
                               config={"strategy": "ruptura", "stability_check": True})
    assert not out["accepted"] and "frágiles" in out["reason"]
    assert out["stability"]["median_pf"] == 0.7 and out["monte_carlo"] is not None
    off = se.evolve_parameters(synthetic_ohlc, pop=8, gens=2, seed=2, config={"strategy": "ruptura"})
    assert off["stability"] is None and "frágiles" not in off["reason"]


def test_warmup_bars() -> None:
    assert se.warmup_bars({"fast": 10, "slow": 80}) == 81
    assert se.warmup_bars({"lookback": 20, "trend": 150}) == 151
    assert se.warmup_bars({"bb_period": 20}) == 21
    assert se.warmup_bars(STRATEGY_PARAMS["compresion"]) == 127


def test_unknown_strategy_rejected() -> None:
    with pytest.raises(ValueError):
        se.build_search_space({"strategy": "magia"})


# ------------------------------------------------------------------ Fase 2
def test_adx_range(synthetic_ohlc: pd.DataFrame) -> None:
    values = se.adx(synthetic_ohlc).dropna()
    assert len(values) > 1000 and values.between(0, 100).all()


def test_regime_filter_blocks_signals(synthetic_ohlc: pd.DataFrame) -> None:
    cache = se.IndicatorCache(synthetic_ohlc)
    base = STRATEGY_PARAMS["ruptura"]
    plain = se.signal_array(cache, base)
    strict = se.signal_array(cache, {**base, "adx": 40.0})
    loose = se.signal_array(cache, {**base, "adx": 1.0})
    assert np.count_nonzero(strict) < np.count_nonzero(plain)
    idx = np.flatnonzero(strict)
    assert np.all(cache.adx[idx] > 40.0)
    assert np.count_nonzero(loose) <= np.count_nonzero(plain)
    rev = se.signal_array(cache, {**STRATEGY_PARAMS["reversion_media"], "adx": 20.0})
    assert np.all(cache.adx[np.flatnonzero(rev)] < 20.0)


def test_session_filter_restricts_entries(synthetic_ohlc: pd.DataFrame) -> None:
    cache = se.IndicatorCache(synthetic_ohlc)
    p = STRATEGY_PARAMS["cruce_medias"]
    all_day = se.backtest(cache, p, 0, len(cache), {})
    london = se.backtest(cache, p, 0, len(cache), {"trading_hours": "8-16"})
    assert 0 < london.trades < all_day.trades
    allowed = cache.session_allowed("8-16")
    assert allowed.sum() == pytest.approx(len(cache) * 8 / 24, rel=0.02)


def test_exit_management_changes_results(synthetic_ohlc: pd.DataFrame) -> None:
    cache = se.IndicatorCache(synthetic_ohlc)
    p = {**STRATEGY_PARAMS["ruptura"], "sl_mult": 2.0, "tp_mult": 4.0}
    plain = se.backtest(cache, p, 0, len(cache), {})
    be = se.backtest(cache, {**p, "be_mult": 0.8}, 0, len(cache), {"exit_management": "breakeven"})
    trail = se.backtest(cache, {**p, "trail_mult": 1.0}, 0, len(cache), {"exit_management": "trailing"})
    timed = se.backtest(cache, p, 0, len(cache), {"max_bars_in_trade": 3})
    assert plain.to_dict() != be.to_dict() != trail.to_dict()
    # el break-even convierte pérdidas en operaciones ~0 (menos de coste): más "empates"
    assert np.sum(np.abs(np.asarray(be.pnls)) < 0.0002) > np.sum(np.abs(np.asarray(plain.pnls)) < 0.0002)
    assert timed.trades >= plain.trades  # al cerrar antes caben más operaciones


def test_search_space_with_new_options() -> None:
    space = se.build_search_space({"regime_filter": True, "evolve_sl_tp": True,
                                   "exit_management": "trailing", "strategy": "ruptura"})
    assert {"lookback", "trend", "adx", "sl_mult", "tp_mult", "trail_mult"} == set(space)
    space = se.build_search_space({"exit_management": "breakeven", "evolve_sl_tp": False})
    assert "be_mult" not in space and "adx" not in space


def test_walk_forward_windows_cover_validation() -> None:
    wins = se.walk_forward_windows(1000, 0.7, 3)
    assert wins[0][0] == 700 and wins[-1][2] == 1000
    assert [w[2] for w in wins[:-1]] == [w[1] for w in wins[1:]]


@pytest.mark.parametrize("backend", BACKENDS)
def test_multi_window_walk_forward(synthetic_ohlc: pd.DataFrame, backend: str) -> None:
    cfg = {"validation_windows": 3, "evolver_backend": backend, "regime_filter": True,
           "exit_management": "breakeven", "strategy": "ruptura"}
    out = se.evolve_parameters(synthetic_ohlc, pop=8, gens=2, config=cfg, seed=5)
    assert len(out["windows"]) == 3 and out["validation"] == "walk-forward 3 ventanas"
    assert out["out_of_sample"]["trades"] == sum(w["trades_oos"] for w in out["windows"])
    assert {"adx", "be_mult"} <= set(out["params"])
    assert out["train_bars"] == len(synthetic_ohlc)  # parámetros finales con todo el historial


def test_min_pf_oos_rejects(synthetic_ohlc: pd.DataFrame) -> None:
    out = se.evolve_parameters(synthetic_ohlc, pop=8, gens=2, seed=1,
                               config={"min_pf_oos": 50.0, "pf_cap": 100.0})
    assert out["accepted"] is False and "mínimo 50.00" in out["reason"]


def test_ratio_rule_uses_capped_in_sample_pf(synthetic_ohlc: pd.DataFrame, monkeypatch) -> None:
    # Con PF de entrenamiento muy alto, un PF de validación >= 2.0 no se rechaza por la regla de ratio.
    out = se.evolve_parameters(synthetic_ohlc, pop=8, gens=2, seed=2,
                               config={"strategy": "ruptura", "min_trades": 1, "min_trades_oos": 1})
    if out["in_sample"]["profit_factor"] > 2.5 and out["out_of_sample"]["profit_factor"] >= 2.0:
        assert "x PF in-sample" not in out["reason"]
    assert se.IS_PF_REFERENCE_CAP == 2.5


# ------------------------------------------------------------ frecuencia
def test_span_weeks_and_frequency_factor(synthetic_ohlc: pd.DataFrame) -> None:
    cache = se.IndicatorCache(synthetic_ohlc)
    assert se.span_weeks(cache, 0, 169) == pytest.approx(1.0)  # 168 h = 1 semana
    assert se.span_weeks(se.IndicatorCache(synthetic_ohlc.drop(columns="time")), 0, 100) is None
    assert se.frequency_factor(10, 2.0, 0.0) == 1.0
    assert se.frequency_factor(10, 2.0, 5.0) == 1.0
    assert se.frequency_factor(2, 2.0, 2.0) == pytest.approx(0.25)
    assert se.frequency_factor(5, None, 3.0) == 1.0


def test_fitness_prefers_frequent_combinations(synthetic_ohlc: pd.DataFrame) -> None:
    cache = se.IndicatorCache(synthetic_ohlc)
    params = {"lookback": 20, "trend": 50, "sl_mult": 1.5, "tp_mult": 2.0}
    base = {"strategy": "ruptura", "fitness_folds": 1}
    plain = se.make_fitness(cache, 0, len(cache), base)(params)
    m = se.backtest(cache, params, 0, len(cache), base)
    tpw = m.trades / se.span_weeks(cache, 0, len(cache))
    easy = se.make_fitness(cache, 0, len(cache), {**base, "min_trades_per_week": tpw / 2})(params)
    hard = se.make_fitness(cache, 0, len(cache), {**base, "min_trades_per_week": tpw * 2})(params)
    assert easy == pytest.approx(plain)
    assert hard == pytest.approx(plain * 0.25)
