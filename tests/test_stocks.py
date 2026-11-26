"""Tests del modo acciones: sesión, huecos, solo compras, comisión y reglas en vivo."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

import bot_common
import mt5_connector
import strategy_evolver as se
import trading_bot
from tests.fake_mt5 import FakeMT5


@pytest.fixture
def stock_df() -> pd.DataFrame:
    """Acción sintética: velas M15 solo de 16:00 a 22:45 (hora servidor), lunes a viernes, con gaps."""
    rng = np.random.default_rng(11)
    days = pd.bdate_range("2025-01-06", periods=160)
    times = [d + pd.Timedelta(hours=16) + pd.Timedelta(minutes=15 * k) for d in days for k in range(28)]
    n = len(times)
    ret = rng.normal(0, 0.004, n) + 0.002 * np.sin(np.arange(n) / 25)
    gaps = np.r_[0, (np.diff(np.array(times, dtype="datetime64[m]")).astype(int) > 15).astype(float)]
    ret += gaps * rng.normal(0, 0.02, n)  # saltos de apertura
    close = 150 * np.exp(np.cumsum(ret))
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) * (1 + rng.uniform(0, 0.002, n))
    low = np.minimum(open_, close) * (1 - rng.uniform(0, 0.002, n))
    return pd.DataFrame({"time": times, "open": open_, "high": high, "low": low, "close": close,
                         "spread_price": 0.02})


def test_detect_session_hours(stock_df: pd.DataFrame, synthetic_ohlc: pd.DataFrame) -> None:
    assert se.detect_session_hours(stock_df) == "16-23"
    assert se.detect_session_hours(synthetic_ohlc) == ""   # 24 h (forex)


def test_cache_session_arrays(stock_df: pd.DataFrame) -> None:
    cache = se.IndicatorCache(stock_df)
    assert cache.bar_seconds == 900
    assert cache.gap_after[27] and not cache.gap_after[26]          # hueco tras la vela 22:45
    assert cache.bars_since_open[28] == 0 and cache.bars_since_open[29] == 1
    assert cache.bars_to_close[27] == 0 and cache.bars_to_close[26] == 1


def test_asset_config_rules() -> None:
    base = {**bot_common.DEFAULT_CONFIG}
    stock = se.asset_config(base, "accion", True)
    assert stock["avoid_open_minutes"] == 15 and stock["flat_before_close_minutes"] == 15
    assert stock["long_only"] and stock["commission_pct"] == pytest.approx(0.001)
    fx = se.asset_config(base, "forex", False)
    assert fx["avoid_open_minutes"] == 0 and fx["flat_before_close_minutes"] == 0 and fx["commission_pct"] == 0
    custom = se.asset_config({**base, "commission_override": 0.04, "flat_before_close_minutes": 0}, "accion", False)
    assert custom["commission_per_trade"] == 0.04 and custom["commission_pct"] == 0
    assert custom["flat_before_close_minutes"] == 0            # el usuario lo desactivó


def test_backtest_stock_rules(stock_df: pd.DataFrame) -> None:
    cache = se.IndicatorCache(stock_df)
    p = {"lookback": 20, "trend": 60, "sl_mult": 3.0, "tp_mult": 4.0}
    free = se.backtest(cache, p, 0, len(cache), {})
    rules = se.asset_config({**bot_common.DEFAULT_CONFIG, "session_hours": "16-23"}, "accion", True, stock_df)
    stock = se.backtest(cache, p, 0, len(cache), rules)
    assert stock.trades > 0 and free.trades > 0
    for entry, exit_, side in stock.log:
        assert side == 1                                                  # solo compras
        assert cache.bars_since_open[entry] >= 1                         # no en los primeros 15 min
        assert not cache.gap_after[entry:exit_].any()                    # nunca atraviesa la noche
    assert any(cache.gap_after[a:b].any() for a, b, _ in free.log)       # sin reglas sí lo hace


def test_commission_pct_cost(stock_df: pd.DataFrame) -> None:
    cache = se.IndicatorCache(stock_df)
    p = {"lookback": 20, "trend": 60}
    a = se.backtest(cache, p, 0, len(cache), {})
    b = se.backtest(cache, p, 0, len(cache), {"commission_pct": 0.001})
    entries = sum(cache.open_[e] for e, _x, _s in a.log)
    assert a.trades == b.trades
    assert a.net_profit - b.net_profit == pytest.approx(0.001 * entries)


@pytest.mark.parametrize("name,path,desc,expected", [
    ("AAPL.US", "Shares\\US", "Apple Inc", "accion"),
    ("VOD.UK", "", "", "accion"),
    ("US500", "Indices\\US", "US 500 Index", "indice"),
    ("XAUUSD", "Metals\\Spot", "Gold vs US Dollar", "materia"),
    ("BTCUSD", "Crypto\\Majors", "Bitcoin vs US Dollar", "cripto"),
    ("EURUSD", "Forex\\Majors", "Euro vs US Dollar", "forex"),
    ("GBPJPY", "", "", "forex"),
])
def test_classify_symbol(name, path, desc, expected) -> None:
    assert mt5_connector.classify_symbol(name, path, desc) == expected


def test_lab_applies_stock_rules(stock_df: pd.DataFrame) -> None:
    import lab
    cfg = {**bot_common.DEFAULT_CONFIG, "population": 6, "generations": 2, "validation_windows": 1,
           "min_trades": 3, "min_trades_oos": 1}
    r = lab.evaluate_job(("AAPL.US", "M15", "ruptura"), stock_df, cfg, 0.02,
                         {"asset_class": "accion", "long_only": True})
    assert r["asset_class"] == "accion" and r["session_hours"] == "16-23" and r["long_only"]


# ----------------------------------------------------------------- en vivo
@pytest.fixture
def stock_bot(monkeypatch, tmp_path, stock_df):
    fake = FakeMT5(stock_df)
    monkeypatch.setattr(mt5_connector, "mt5", fake)
    monkeypatch.setattr(mt5_connector, "_connected", False)
    path = tmp_path / "risk_config.json"
    bot_common.save_config({"symbol": "AAPL.US", "timeframe": "M15", "strategy": "ruptura",
                            "max_spread": 1.0, "simulation": False}, path)
    mt5_connector.connect_mt5_with_retry(max_retries=1, require_demo=True)
    bot = trading_bot.TradingBot(bot_common.load_config(path), path, dry_run=False)
    bot.resolve_asset()
    bot.update_rules(stock_df)
    return bot, fake


def _tick(hour: int, minute: int) -> SimpleNamespace:
    ts = int(pd.Timestamp(f"2025-06-02 {hour:02d}:{minute:02d}").timestamp())
    return SimpleNamespace(bid=150.0, ask=150.02, time=ts)


def test_live_stock_detection(stock_bot) -> None:
    bot, _fake = stock_bot
    assert bot.asset_class == "accion" and bot.broker_long_only
    assert bot.rules["session_hours"] == "16-23" and bot.rules["long_only"]


def test_live_session_rules(stock_bot) -> None:
    bot, fake = stock_bot
    pos = SimpleNamespace(ticket=7, symbol="AAPL.US", volume=1.0, magic=bot.config["magic"], type=0,
                          price_open=150.0, sl=149.0, tp=152.0, profit=3.0, time=0)
    fake.positions.append(pos)
    assert "mercado cerrado" in bot.stock_session_block(_tick(10, 0), [pos])
    assert "primeros 15 min" in bot.stock_session_block(_tick(16, 5), [pos])
    assert bot.stock_session_block(_tick(18, 0), [pos]) == ""
    assert fake.positions                                               # todavía abierta
    assert "cierre" in bot.stock_session_block(_tick(22, 50), [pos])
    assert fake.positions == []                                         # cerrada antes del cierre


def test_live_long_only_ignores_sell(stock_bot, caplog) -> None:
    bot, fake = stock_bot
    bot.params = {"lookback": 20, "trend": 60}
    bot.risk.reset_daily()
    caplog.set_level("INFO")
    sig = {"signal": -1, "atr": 1.0, "bar_time": pd.Timestamp("2025-06-02 18:00"), "close": 150.0,
           "indicators": "", "waiting": "", "rsi": 50.0, "strategy": "ruptura"}
    import strategy_evolver
    original = strategy_evolver.compute_signal
    trading_bot.compute_signal = lambda *a, **k: sig
    try:
        fake.df = fake.df.copy()
        bot.stock_session_block = lambda tick, positions: ""   # dentro de la sesión
        bot.step()
    finally:
        trading_bot.compute_signal = original
    assert fake.orders == [] and "solo admite compras" in caplog.text
