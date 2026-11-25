"""Pruebas de integración de mt5_connector + trading_bot con un MT5 falso."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import bot_common
import mt5_connector
import trading_bot
from strategy_evolver import IndicatorCache, signal_array
from tests.fake_mt5 import ACCOUNT_TRADE_MODE_REAL, ORDER_FILLING_FOK, FakeMT5

PARAMS = {"fast": 10, "slow": 40, "rsi_buy": 50.0, "rsi_sell": 50.0}


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch, synthetic_ohlc: pd.DataFrame) -> FakeMT5:
    fake = FakeMT5(synthetic_ohlc)
    monkeypatch.setattr(mt5_connector, "mt5", fake)
    monkeypatch.setattr(mt5_connector, "_connected", False)
    monkeypatch.setattr(mt5_connector.time, "sleep", lambda s: None)
    return fake


@pytest.fixture
def config_file(tmp_path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "risk_config.json"
    bot_common.save_config({"max_spread": 0.0005}, path)
    monkeypatch.setattr(trading_bot, "EVOLVED_PARAMS_PATH", tmp_path / "evolved.json")
    return path


def test_connect_with_retry(fake: FakeMT5) -> None:
    fake.fail_initialize = 2
    assert mt5_connector.connect_mt5_with_retry(max_retries=3, wait_seconds=0, require_demo=True)
    assert mt5_connector.is_connected()
    mt5_connector.safe_shutdown()
    mt5_connector.safe_shutdown()
    assert not mt5_connector.is_connected()


def test_connect_gives_up(fake: FakeMT5) -> None:
    fake.fail_initialize = 99
    assert mt5_connector.connect_mt5_with_retry(max_retries=3, wait_seconds=0) is False


def test_real_account_rejected_when_demo_required(fake: FakeMT5) -> None:
    fake.trade_mode = ACCOUNT_TRADE_MODE_REAL
    with pytest.raises(SystemExit):
        mt5_connector.connect_mt5_with_retry(max_retries=1, require_demo=True)
    assert not mt5_connector.is_connected()


def test_get_rates_and_spread(fake: FakeMT5) -> None:
    mt5_connector.connect_mt5_with_retry(max_retries=1, require_demo=True)
    df = mt5_connector.get_rates("EURUSD", "H1", 500)
    assert len(df) == 500 and df["time"].dt.tz is None  # hora del servidor, sin zona
    assert pd.api.types.is_datetime64_any_dtype(df["time"])
    assert mt5_connector.get_spread("EURUSD") == pytest.approx(0.0001)


def test_order_falls_back_to_other_filling(fake: FakeMT5) -> None:
    fake.supported_filling = ORDER_FILLING_FOK
    mt5_connector.connect_mt5_with_retry(max_retries=1, require_demo=True)
    res = mt5_connector.send_market_order("EURUSD", "buy", 0.1, 1.0, 2.0, 20250101)
    assert res is not None and res.retcode == 10009
    assert fake.orders[-1]["type_filling"] == ORDER_FILLING_FOK
    assert mt5_connector.close_all_positions("EURUSD", 20250101) == 1


def _bot_at_signal(fake: FakeMT5, config_file, dry_run: bool) -> trading_bot.TradingBot:
    """Crea un bot con PARAMS y recorta los datos para que la última vela cerrada tenga señal."""
    sig = signal_array(IndicatorCache(fake.df), PARAMS)
    idx = int(np.flatnonzero(sig)[-3])
    fake.df = fake.df.iloc[: idx + 2]  # +1 vela en formación
    mt5_connector.connect_mt5_with_retry(max_retries=1, require_demo=True)
    bot = trading_bot.TradingBot(bot_common.load_config(config_file), config_file, dry_run=dry_run)
    bot.risk.reset_daily()
    bot.params = dict(PARAMS)
    return bot


def test_step_opens_trade_once(fake: FakeMT5, config_file) -> None:
    bot = _bot_at_signal(fake, config_file, dry_run=False)
    bot.step()
    assert len(fake.orders) == 1
    order = fake.orders[0]
    assert order["magic"] == 20250101 and 0.01 <= order["volume"] <= 1.0
    if order["type"] == fake.ORDER_TYPE_BUY:
        assert order["sl"] < order["price"] < order["tp"]
    else:
        assert order["tp"] < order["price"] < order["sl"]
    bot.step()  # misma vela -> no repite
    bot.last_bar_time = None
    bot.step()  # ya hay posición con el mismo magic -> no abre otra
    assert len(fake.orders) == 1


def test_dry_run_sends_nothing(fake: FakeMT5, config_file) -> None:
    bot = _bot_at_signal(fake, config_file, dry_run=True)
    bot.step()
    assert fake.orders == []


def test_daily_loss_blocks_trading(fake: FakeMT5, config_file) -> None:
    bot = _bot_at_signal(fake, config_file, dry_run=False)
    fake.equity = 9_600  # -4 %
    bot.step()
    assert fake.orders == [] and bot.risk.trading_halted


def test_hot_reload(fake: FakeMT5, config_file) -> None:
    import os
    bot = _bot_at_signal(fake, config_file, dry_run=True)
    bot_common.save_config({**bot.config, "risk_percent": 0.5}, config_file)
    os.utime(config_file, (bot.config_mtime + 5, bot.config_mtime + 5))
    bot.reload_config_if_changed()
    assert bot.risk.risk_percent == 0.5


def test_main_full_flow_aborts_or_runs(fake: FakeMT5, config_file, monkeypatch) -> None:
    # El bucle infinito se corta con KeyboardInterrupt simulado.
    monkeypatch.setattr(trading_bot.TradingBot, "run",
                        lambda self: (_ for _ in ()).throw(KeyboardInterrupt))
    code = trading_bot.main(["--config", str(config_file), "--dry-run"])
    assert code in (trading_bot.EXIT_OK, trading_bot.EXIT_VALIDATION)
    assert not mt5_connector.is_connected()  # safe_shutdown en finally


def test_unhandled_error_closes_positions_when_configured(fake: FakeMT5, config_file, monkeypatch) -> None:
    bot_common.save_config({"max_spread": 0.0005, "close_positions_on_error": True,
                            "min_pf_in_sample": 0.0, "oos_pf_ratio_min": 0.0001,
                            "min_trades": 1, "min_trades_oos": 1}, config_file)
    fake.positions.append(__import__("types").SimpleNamespace(
        ticket=99, symbol="EURUSD", volume=0.1, magic=20250101, type=0))

    def boom(self):
        raise RuntimeError("fallo simulado")

    monkeypatch.setattr(trading_bot.TradingBot, "run", boom)
    assert trading_bot.main(["--config", str(config_file)]) == trading_bot.EXIT_ERROR
    assert fake.positions == []
    assert not mt5_connector.is_connected()


def test_unknown_symbol_suggests_correct_name(fake: FakeMT5, caplog) -> None:
    mt5_connector.connect_mt5_with_retry(max_retries=1, require_demo=True)
    assert mt5_connector.find_similar_symbols("us500")[0] == "US500"
    assert mt5_connector.find_similar_symbols("EUR/USD")[0] == "EURUSD"
    assert mt5_connector.ensure_symbol("us500") is False
    assert "US500" in caplog.text
    assert mt5_connector.ensure_symbol("US500") is True


def test_main_exits_with_config_code_on_unknown_symbol(fake: FakeMT5, config_file) -> None:
    bot_common.save_config({"symbol": "us500"}, config_file)
    assert trading_bot.main(["--config", str(config_file)]) == trading_bot.EXIT_CONFIG
    assert not mt5_connector.is_connected()


def test_get_rates_adds_spread_in_price(fake: FakeMT5) -> None:
    mt5_connector.connect_mt5_with_retry(max_retries=1, require_demo=True)
    df = mt5_connector.get_rates("EURUSD", "H1", 50)
    assert df["spread_price"].iloc[0] == pytest.approx(10 * 0.00001)


def test_candle_line_and_heartbeat_after_signal(fake: FakeMT5, config_file, caplog) -> None:
    caplog.set_level("INFO")
    bot = _bot_at_signal(fake, config_file, dry_run=True)
    bot.step()                       # vela con señal -> línea de vela + [DRY-RUN]
    assert "cerrada (hora servidor)" in caplog.text and "[DRY-RUN]" in caplog.text
    assert "1970" not in caplog.text
    caplog.clear()
    bot.last_heartbeat = 0.0
    bot.step()                       # misma vela -> mensaje de espera
    assert "En espera" in caplog.text and "Vela" not in caplog.text


def test_heartbeat_explains_what_bot_waits_for(fake: FakeMT5, config_file, caplog) -> None:
    caplog.set_level("INFO")
    sig = signal_array(IndicatorCache(fake.df), PARAMS)
    idx = int(np.flatnonzero(sig == 0)[-1])
    fake.df = fake.df.iloc[: idx + 2]  # última vela cerrada sin señal
    mt5_connector.connect_mt5_with_retry(max_retries=1, require_demo=True)
    bot = trading_bot.TradingBot(bot_common.load_config(config_file), config_file, dry_run=True)
    bot.risk.reset_daily()
    bot.params = dict(PARAMS)
    bot.step()                         # la línea de vela cuenta como latido
    assert "En espera" not in caplog.text
    bot.last_heartbeat = 0.0
    caplog.clear()
    bot.step()
    text = caplog.text
    assert "En espera" in text and "esperando cruce" in text
    # H1: el tick simulado está 1 min después de abrir la vela en curso -> faltan 59 min
    assert "en 59 min" in text and "día +0.00%" in text
    caplog.clear()
    bot.step()                         # antes de heartbeat_seconds -> no repite
    assert "En espera" not in caplog.text


def test_heartbeat_can_be_disabled(fake: FakeMT5, config_file, caplog) -> None:
    caplog.set_level("INFO")
    bot = _bot_at_signal(fake, config_file, dry_run=True)
    bot.config["heartbeat_seconds"] = 0
    bot.step()
    caplog.clear()
    bot.step()
    assert "En espera" not in caplog.text


def test_live_order_uses_evolved_sl_tp(fake: FakeMT5, config_file) -> None:
    bot = _bot_at_signal(fake, config_file, dry_run=False)
    bot.params = {**PARAMS, "sl_mult": 3.0, "tp_mult": 1.0}
    bot.step()
    order = fake.orders[0]
    sl_dist, tp_dist = abs(order["price"] - order["sl"]), abs(order["tp"] - order["price"])
    assert sl_dist == pytest.approx(3 * tp_dist, abs=3e-5)  # precios redondeados a 5 decimales
