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


def test_profile_helpers(tmp_path) -> None:
    from bot_common import config_path_for, list_profiles, profile_name, profile_paths

    assert list_profiles(tmp_path) == []
    bot_common.save_config({}, config_path_for("principal", tmp_path))
    bot_common.save_config({"timeframe": "M5"}, config_path_for("m5", tmp_path))
    (tmp_path / "risk_config_MAL NOMBRE.json").write_text("{}")
    assert list_profiles(tmp_path) == ["principal", "m5"]
    assert profile_name(tmp_path / "risk_config_m5.json") == "m5"
    p = profile_paths(tmp_path / "risk_config_m5.json")
    assert p.evolved.name == "evolved_params_m5.json" and p.log_file == "trading_bot_m5.log"
    assert profile_paths(tmp_path / "risk_config.json").evolved.name == "evolved_params.json"


def test_layered_config_global_and_profile(tmp_path) -> None:
    from bot_common import (PROFILE_KEYS, config_path_for, load_config, read_json,
                            save_global_config, save_profile)

    save_global_config({"risk_percent": 0.5, "history_bars": 6000, "symbol": "IGNORADO"}, tmp_path)
    p = config_path_for("us500", tmp_path)
    save_profile({"symbol": "US500", "timeframe": "M5", "max_spread": 0.8, "magic": 7,
                  "risk_percent": 9.0}, p)
    stored = read_json(p)
    assert set(stored) == set(PROFILE_KEYS)          # solo claves propias
    assert "risk_percent" not in stored
    cfg = load_config(p)
    assert cfg["risk_percent"] == 0.5 and cfg["history_bars"] == 6000   # de la general
    assert cfg["symbol"] == "US500" and cfg["timeframe"] == "M5"        # del perfil
    assert "symbol" not in read_json(tmp_path / "global_config.json")
    save_global_config({**cfg, "risk_percent": 2.0}, tmp_path)            # cambio general
    assert load_config(p)["risk_percent"] == 2.0                          # afecta a todos


def test_migration_to_global(tmp_path) -> None:
    from bot_common import config_path_for, load_config, migrate_to_global, read_json

    bot_common.save_config({"risk_percent": 0.7, "symbol": "EURUSD", "magic": 1},
                           config_path_for("principal", tmp_path))
    bot_common.save_config({"risk_percent": 3.0, "symbol": "GBPUSD", "magic": 2, "timeframe": "M5"},
                           config_path_for("gbpusd", tmp_path))
    assert migrate_to_global(tmp_path) is True
    assert migrate_to_global(tmp_path) is False   # solo una vez
    assert read_json(tmp_path / "global_config.json")["risk_percent"] == 0.7
    g = load_config(config_path_for("gbpusd", tmp_path))
    assert g["symbol"] == "GBPUSD" and g["timeframe"] == "M5" and g["risk_percent"] == 0.7
    backups = list((tmp_path / "logs").glob("backup_config_*/risk_config*.json"))
    assert len(backups) == 2


def test_hours_helpers() -> None:
    from bot_common import hour_allowed, parse_hours

    assert parse_hours("10-19") == (10, 19) and parse_hours("") is None and parse_hours("x") is None
    assert hour_allowed(12, "10-19") and not hour_allowed(19, "10-19")
    assert hour_allowed(23, "22-6") and hour_allowed(3, "22-6") and not hour_allowed(12, "22-6")
    assert hour_allowed(5, "")
