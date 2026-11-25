"""Todos los módulos deben importarse sin efectos secundarios (incluso sin MT5)."""

from __future__ import annotations

import importlib
import json

import pytest

import bot_common


@pytest.mark.parametrize("name", ["bot_common", "risk_manager", "strategy_evolver",
                                  "mt5_connector", "trading_bot"])
def test_import_without_side_effects(name: str) -> None:
    importlib.import_module(name)


def test_safe_shutdown_is_idempotent() -> None:
    import mt5_connector
    mt5_connector.safe_shutdown()
    mt5_connector.safe_shutdown()


def test_trading_bot_aborts_without_config(tmp_path) -> None:
    import trading_bot
    assert trading_bot.main(["--config", str(tmp_path / "no_existe.json")]) == trading_bot.EXIT_CONFIG


def test_config_roundtrip(tmp_path) -> None:
    path = tmp_path / "risk_config.json"
    bot_common.save_config({"risk_percent": 2.0, "symbol": "GBPUSD"}, path)
    cfg = bot_common.load_config(path)
    assert cfg["risk_percent"] == 2.0 and cfg["symbol"] == "GBPUSD"
    assert cfg["max_spread"] == bot_common.DEFAULT_CONFIG["max_spread"]


def test_invalid_config_rejected(tmp_path) -> None:
    path = tmp_path / "risk_config.json"
    path.write_text(json.dumps({"min_lot": 2.0, "max_lot": 1.0, "timeframe": "D1"}), encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        bot_common.load_config(path)
    assert "min_lot" in str(exc.value) and "timeframe" in str(exc.value)
    with pytest.raises(ValueError):
        bot_common.save_config({"risk_percent": -1}, path)
