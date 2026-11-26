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
    bot_common.save_config({"max_spread": 0.0005, "simulation": False}, path)
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
    bot.config["daily_loss_scope"] = "cuenta"
    fake.equity = 9_600  # -4 % en la cuenta
    bot.step()
    assert fake.orders == [] and bot.risk.trading_halted


def test_manual_loss_does_not_block_bots_by_default(fake: FakeMT5, config_file) -> None:
    bot = _bot_at_signal(fake, config_file, dry_run=False)
    assert bot.loss_scope == "bots"  # valor por defecto
    fake.equity = 9_600  # -4 % por una operación manual
    bot.step()
    assert len(fake.orders) == 1 and not bot.risk.trading_halted


def test_hot_reload(fake: FakeMT5, config_file) -> None:
    import os
    bot = _bot_at_signal(fake, config_file, dry_run=True)
    bot_common.save_config({**bot.config, "risk_percent": 0.5}, config_file)
    os.utime(config_file, (bot.config_mtime[0] + 5, bot.config_mtime[0] + 5))
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
                            "min_trades": 1, "min_trades_oos": 1, "min_pf_oos": 0.0,
                            "validation_windows": 1, "regime_filter": False}, config_file)
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
    bot.last_heartbeat = None
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
    bot.last_heartbeat = None
    caplog.clear()
    bot.step()
    text = caplog.text
    assert "En espera" in text and "esperando cruce" in text
    # H1: el tick simulado está 1 min después de abrir la vela en curso -> faltan 59 min
    assert "en 59 min" in text and "día (bots) +0.00%" in text
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


def test_status_file_and_history(fake: FakeMT5, config_file, monkeypatch) -> None:
    import json

    from bot_common import profile_paths, read_json

    paths = profile_paths(config_file)
    calls = {"n": 0}

    def one_step_then_stop(self):
        self.step()
        self.publish_status()
        calls["n"] += 1
        status = read_json(paths.status)
        assert status["phase"] in ("operando", "sin_parametros")
        assert status["account"]["equity"] == 10_000.0
        assert status["profile"] == "principal" and status["symbol"] == "EURUSD"

    monkeypatch.setattr(trading_bot.TradingBot, "run", one_step_then_stop)
    code = trading_bot.main(["--config", str(config_file), "--dry-run"])
    final = read_json(paths.status)
    assert final["exit_code"] == code
    assert final["phase"] == ("finalizado" if code == 0 else "descartado")
    assert "evolution" in final
    lines = paths.history.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1 and "pf_oos" in json.loads(lines[0])
    assert paths.evolved.exists()


def test_stop_flag_ends_run_loop(fake: FakeMT5, config_file, monkeypatch) -> None:
    bot = _bot_at_signal(fake, config_file, dry_run=True)
    bot.config["loop_seconds"] = 2
    bot.paths.stop_flag.parent.mkdir(parents=True, exist_ok=True)
    bot.paths.stop_flag.write_text("stop")
    bot.run()  # debe volver en lugar de quedarse en bucle
    assert bot.status.paths.status.exists()


def test_bot_deals_grouped_by_position(fake: FakeMT5) -> None:
    mt5_connector.connect_mt5_with_retry(max_retries=1, require_demo=True)
    mt5_connector.send_market_order("EURUSD", "buy", 0.1, 1.0, 2.0, 20250101)
    mt5_connector.send_market_order("EURUSD", "sell", 0.2, 2.0, 1.0, 999)  # otro magic
    assert mt5_connector.get_bot_deals(20250101) == []  # aún abierta
    mt5_connector.close_all_positions("EURUSD", 20250101)
    deals = mt5_connector.get_bot_deals(20250101)
    assert len(deals) == 1
    assert deals[0]["side"] == "BUY" and deals[0]["profit"] == pytest.approx(4.0)


def test_global_position_limit_across_profiles(fake: FakeMT5, config_file) -> None:
    # Otro perfil (magic 777) ya tiene 1 posición abierta en otro símbolo.
    bot_common.save_config({"magic": 777, "symbol": "GBPUSD"},
                           bot_common.config_path_for("gbpusd", config_file.parent))
    fake.positions.append(__import__("types").SimpleNamespace(
        ticket=50, symbol="GBPUSD", volume=0.1, magic=777, type=0))
    bot_common.save_config({**bot_common.load_config(config_file), "max_open_positions_total": 1},
                           config_file)
    bot = _bot_at_signal(fake, config_file, dry_run=False)
    assert bot.portfolio_magics() == {20250101, 777}
    bot.step()
    assert fake.orders == []  # bloqueado por el límite global
    bot.config["max_open_positions_total"] = 2
    bot.last_bar_time = None
    bot.step()
    assert len(fake.orders) == 1


@pytest.mark.parametrize("params", [
    {"bb_period": 20, "bb_std": 2.0, "rsi_low": 35.0, "rsi_high": 65.0, "sl_mult": 1.5, "tp_mult": 1.0},
    {"lookback": 20, "trend": 50, "sl_mult": 2.0, "tp_mult": 3.0},
])
def test_bot_trades_with_new_strategies(fake: FakeMT5, config_file, params) -> None:
    sig = signal_array(IndicatorCache(fake.df), params)
    idx = int(np.flatnonzero(sig)[-2])
    fake.df = fake.df.iloc[: idx + 2]
    mt5_connector.connect_mt5_with_retry(max_retries=1, require_demo=True)
    bot = trading_bot.TradingBot(bot_common.load_config(config_file), config_file, dry_run=False)
    bot.risk.reset_daily()
    bot.params = dict(params)
    bot.step()
    assert len(fake.orders) == 1
    assert fake.orders[0]["type"] == (fake.ORDER_TYPE_BUY if sig[idx] == 1 else fake.ORDER_TYPE_SELL)


def _new_bot(config_file, **overrides) -> trading_bot.TradingBot:
    cfg = {**bot_common.load_config(config_file), **overrides}
    bot_common.save_config(cfg, config_file)
    bot = trading_bot.TradingBot(bot_common.load_config(config_file), config_file, dry_run=True)
    assert bot.start_day()
    return bot


def test_day_start_survives_restart(fake: FakeMT5, config_file) -> None:
    mt5_connector.connect_mt5_with_retry(max_retries=1, require_demo=True)
    first = _new_bot(config_file, daily_loss_scope="cuenta")
    assert first.risk.day_start_balance == 10_000
    fake.balance = fake.equity = 9_700  # pérdida realizada después del inicio del día
    second = _new_bot(config_file, daily_loss_scope="cuenta")  # "reinicio" el mismo día
    assert second.risk.day_start_balance == 10_000
    assert second.risk.daily_loss_percent(9_700) == pytest.approx(3.0)
    assert second.check_daily_loss() is False  # la pérdida previa al reinicio cuenta


def test_bots_scope_ignores_manual_trades(fake: FakeMT5, config_file) -> None:
    from types import SimpleNamespace

    mt5_connector.connect_mt5_with_retry(max_retries=1, require_demo=True)
    fake.positions.append(SimpleNamespace(ticket=900, symbol="EURUSD", volume=1.0, magic=0,
                                          type=0, profit=-916.0))
    fake.equity = 10_000 - 916
    account_bot = _new_bot(config_file, daily_loss_scope="cuenta")
    assert account_bot.check_daily_loss() is False      # la manual cuenta
    (config_file.parent / "logs" / "day_state.json").unlink()
    bots_bot = _new_bot(config_file, daily_loss_scope="bots")
    assert bots_bot.check_daily_loss() is True          # la manual NO cuenta
    assert bots_bot.bots_pnl == (0.0, 0.0)


def test_bots_scope_counts_bot_losses_and_shares_halt(fake: FakeMT5, config_file) -> None:
    from types import SimpleNamespace

    mt5_connector.connect_mt5_with_retry(max_retries=1, require_demo=True)
    bot = _new_bot(config_file, daily_loss_scope="bots")
    t = bot.day_start_server_time
    # Posición del bot cerrada hoy con -350 y otra abierta con -10 (= -3.6 %)
    fake.deals += [
        SimpleNamespace(position_id=70, magic=20250101, entry=0, type=0, symbol="EURUSD",
                        volume=0.5, price=1.1, time=t + 5, profit=0.0, commission=-2.0, swap=0.0),
        SimpleNamespace(position_id=70, magic=0, entry=1, type=1, symbol="EURUSD",
                        volume=0.5, price=1.09, time=t + 50, profit=-348.0, commission=0.0, swap=0.0),
        # cierre de AYER de otra posición del bot: no cuenta
        SimpleNamespace(position_id=71, magic=20250101, entry=1, type=1, symbol="EURUSD",
                        volume=0.5, price=1.09, time=t - 3600, profit=-500.0, commission=0.0, swap=0.0),
    ]
    fake.positions.append(SimpleNamespace(ticket=72, symbol="EURUSD", volume=0.1, magic=20250101,
                                          type=0, profit=-10.0, swap=0.0))
    assert bot.check_daily_loss() is False
    assert bot.bots_pnl == (pytest.approx(-350.0), pytest.approx(-10.0))
    # Otro perfil de la misma cuenta y mismo criterio queda detenido también
    other_cfg = bot_common.config_path_for("gbpusd", config_file.parent)
    bot_common.save_config({**bot_common.load_config(config_file), "magic": 555}, other_cfg)
    other = trading_bot.TradingBot(bot_common.load_config(other_cfg), other_cfg, dry_run=True)
    assert other.start_day()
    fake.positions.clear()
    fake.deals.clear()
    assert other.check_daily_loss() is False
    assert "otro bot" in other.risk.halt_reason


def test_list_symbols_for_browser(fake: FakeMT5) -> None:
    mt5_connector.connect_mt5_with_retry(max_retries=1, require_demo=True)
    symbols = mt5_connector.list_symbols()
    names = [s["name"] for s in symbols]
    assert set(names) == set(fake.symbols)
    us500 = next(s for s in symbols if s["name"] == "US500")
    assert us500["category"] == "Indices" and us500["description"] == "US 500 Index"
    assert us500["spread"] == pytest.approx(10 * 0.00001) and us500["tradable"]
    assert names.index("EURUSD") < names.index("US500")  # ordenados por categoría


# ------------------------------------------------------------------ Fase 3
from types import SimpleNamespace  # noqa: E402


def _position(ticket: int = 900, side: int = 0, entry: float = 1.1000, sl: float = 1.0980,
              tp: float = 1.1050, opened: int = 0) -> SimpleNamespace:
    return SimpleNamespace(ticket=ticket, symbol="EURUSD", volume=0.1, magic=20250101, type=side,
                           price_open=entry, sl=sl, tp=tp, profit=0.0, time=opened)


def test_profile_simulation_blocks_real_orders(fake: FakeMT5, config_file) -> None:
    bot = _bot_at_signal(fake, config_file, dry_run=False)
    bot.config["simulation"] = True
    assert bot.dry_run
    bot.step()
    assert fake.orders == []


def test_session_filter_ignores_signal_outside_hours(fake: FakeMT5, config_file) -> None:
    bot = _bot_at_signal(fake, config_file, dry_run=False)
    closed_bar = fake.df["time"].iloc[-2]
    forming_hour = (closed_bar.hour + 1) % 24
    start = (forming_hour + 1) % 24
    bot.config["trading_hours"] = f"{start}-{(start + 2) % 24 or 24}"
    bot.step()
    assert fake.orders == []
    bot.config["trading_hours"] = ""
    bot.last_bar_time = None
    bot.step()
    assert len(fake.orders) == 1


def test_breakeven_moves_stop_to_entry(fake: FakeMT5, config_file) -> None:
    mt5_connector.connect_mt5_with_retry(max_retries=1, require_demo=True)
    bot = trading_bot.TradingBot(bot_common.load_config(config_file), config_file, dry_run=False)
    bot.config.update(exit_management="breakeven", be_atr_mult=1.0)
    bot.params = dict(PARAMS)
    pos = _position()
    fake.positions.append(pos)
    bot.registry.set(900, {"atr": 0.0010, "best": 1.1000})
    bot.manage_positions([pos], {"atr": 0.0010}, SimpleNamespace(bid=1.1005, ask=1.1006, time=0))
    assert pos.sl == 1.0980  # aún no avanzó 1 ATR
    bot.manage_positions([pos], {"atr": 0.0010}, SimpleNamespace(bid=1.1012, ask=1.1013, time=0))
    assert pos.sl == pytest.approx(1.1000)
    assert bot.registry.get(900)["best"] == pytest.approx(1.1012)


def test_trailing_follows_best_price_for_sell(fake: FakeMT5, config_file) -> None:
    mt5_connector.connect_mt5_with_retry(max_retries=1, require_demo=True)
    bot = trading_bot.TradingBot(bot_common.load_config(config_file), config_file, dry_run=False)
    bot.config.update(exit_management="trailing")
    bot.params = {**PARAMS, "trail_mult": 1.5}
    pos = _position(side=1, entry=1.1000, sl=1.1030, tp=1.0950)
    fake.positions.append(pos)
    bot.registry.set(900, {"atr": 0.0010, "best": 1.1000})
    bot.manage_positions([pos], {"atr": 0.0010}, SimpleNamespace(bid=1.0979, ask=1.0980, time=0))
    assert pos.sl == pytest.approx(1.0980 + 0.0015)
    previous = pos.sl
    bot.manage_positions([pos], {"atr": 0.0010}, SimpleNamespace(bid=1.0989, ask=1.0990, time=0))
    assert pos.sl == pytest.approx(previous)  # nunca retrocede


def test_time_stop_closes_old_position(fake: FakeMT5, config_file) -> None:
    mt5_connector.connect_mt5_with_retry(max_retries=1, require_demo=True)
    bot = trading_bot.TradingBot(bot_common.load_config(config_file), config_file, dry_run=False)
    bot.config.update(max_bars_in_trade=3, timeframe="H1")
    bot.params = dict(PARAMS)
    now = 1_700_000_000
    old, fresh = _position(901, opened=now - 4 * 3600), _position(902, opened=now - 3600)
    fake.positions += [old, fresh]
    bot.manage_positions([old, fresh], {"atr": 0.001}, SimpleNamespace(bid=1.1, ask=1.1001, time=now))
    assert [p.ticket for p in fake.positions] == [902]


def test_closed_position_detection_and_auto_pause(fake: FakeMT5, config_file) -> None:
    mt5_connector.connect_mt5_with_retry(max_retries=1, require_demo=True)
    bot = trading_bot.TradingBot(bot_common.load_config(config_file), config_file, dry_run=False)
    bot.config.update(auto_disable_min_trades=5, auto_disable_pf=0.7)
    bot.params_since_ts = 0
    bot.known_tickets = {1000}
    for k in range(6):  # 1 ganadora y 5 perdedoras -> PF bajo
        pid = 1000 + k
        fake.deals.append(SimpleNamespace(position_id=pid, magic=20250101, entry=0, type=0,
                                          symbol="EURUSD", volume=0.1, price=1.1, time=10 + k,
                                          profit=0.0, commission=0.0, swap=0.0))
        fake.deals.append(SimpleNamespace(position_id=pid, magic=0, entry=1, type=1, symbol="EURUSD",
                                          volume=0.1, price=1.1, time=20 + k,
                                          profit=10.0 if k == 0 else -20.0, commission=0.0, swap=0.0))
    bot.track_closed_positions([])
    assert "rendimiento real bajo" in bot.paused_reason
    assert mt5_connector.get_position_result(1000) == pytest.approx(10.0)


def test_auto_pause_only_counts_trades_with_current_params(fake: FakeMT5, config_file) -> None:
    mt5_connector.connect_mt5_with_retry(max_retries=1, require_demo=True)
    bot = trading_bot.TradingBot(bot_common.load_config(config_file), config_file, dry_run=False)
    bot.config.update(auto_disable_min_trades=3, auto_disable_pf=0.7)
    for k in range(5):
        fake.deals += [SimpleNamespace(position_id=k, magic=20250101, entry=0, type=0, symbol="EURUSD",
                                       volume=0.1, price=1.1, time=100 + k, profit=0.0, commission=0.0, swap=0.0),
                       SimpleNamespace(position_id=k, magic=0, entry=1, type=1, symbol="EURUSD", volume=0.1,
                                       price=1.1, time=200 + k, profit=-5.0, commission=0.0, swap=0.0)]
    bot.params_since_ts = 10_000  # parámetros nuevos, posteriores a esas pérdidas
    bot.evaluate_performance()
    assert bot.paused_reason == ""


def test_periodic_reevolution(fake: FakeMT5, config_file, monkeypatch) -> None:
    mt5_connector.connect_mt5_with_retry(max_retries=1, require_demo=True)
    bot = trading_bot.TradingBot(bot_common.load_config(config_file), config_file, dry_run=True)
    calls = []
    monkeypatch.setattr(bot, "evolve", lambda: calls.append(1) or True)
    bot.config["reevolve_hours"] = 1
    bot.last_evolution_mono -= 3000
    bot.maybe_reevolve([])
    assert calls == []                      # aún no pasó 1 hora
    bot.last_evolution_mono -= 1000
    bot.maybe_reevolve([_position()])
    assert calls == []                      # espera a no tener posición
    bot.maybe_reevolve([])
    assert calls == [1]


def test_telegram_notifier(monkeypatch) -> None:
    import threading as th

    sent = []
    done = th.Event()

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=10):
        sent.append((req.full_url, req.data))
        done.set()
        return _Resp()

    monkeypatch.setattr(trading_bot.urllib.request, "urlopen", fake_urlopen)
    cfg = {"telegram_token": "", "telegram_chat_id": "42"}
    notifier = trading_bot.Notifier(lambda: cfg, lambda: "[X]")
    notifier.send("sin token")
    cfg["telegram_token"] = "ABC"
    notifier.send("hola")
    assert done.wait(5)
    assert sent[0][0] == "https://api.telegram.org/botABC/sendMessage"
    assert b"hola" in sent[0][1] and b"chat_id=42" in sent[0][1] and len(sent) == 1


def test_emergency_close_by_magics(fake: FakeMT5) -> None:
    mt5_connector.connect_mt5_with_retry(max_retries=1, require_demo=True)
    fake.positions += [_position(1, ), SimpleNamespace(**{**vars(_position(2)), "magic": 555}),
                       SimpleNamespace(**{**vars(_position(3)), "magic": 0})]
    closed, failed = mt5_connector.close_positions_by_magics({20250101, 555})
    assert (closed, failed) == (2, 0)
    assert [p.magic for p in fake.positions] == [0]   # la manual queda intacta


def test_order_registered_for_management(fake: FakeMT5, config_file) -> None:
    bot = _bot_at_signal(fake, config_file, dry_run=False)
    bot.step()
    ticket = fake.positions[-1].ticket
    rec = bot.registry.get(ticket)
    assert rec["atr"] > 0 and rec["side"] in (1, -1)
