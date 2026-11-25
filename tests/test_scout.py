"""Tests del explorador (scout.py) con MT5 simulado."""

from __future__ import annotations

import json

import pandas as pd
import pytest

import bot_common
import mt5_connector
import scout
from tests.fake_mt5 import FakeMT5


def _r(symbol: str, accepted: bool, tf: str = "H1", strategy: str = "ruptura") -> dict:
    return {"symbol": symbol, "timeframe": tf, "strategy": strategy, "accepted": accepted,
            "pf_oos": 1.5, "trades_oos": 20}


def test_update_state_needs_consecutive_confirmations() -> None:
    st, new = scout.update_state({}, [_r("EURUSD", True), _r("GBPUSD", True)], 2, set(), "t1")
    assert new == [] and st["candidates"]["EURUSD|H1|ruptura"]["streak"] == 1
    st, new = scout.update_state(st, [_r("EURUSD", True), _r("GBPUSD", False)], 2, set(), "t2")
    assert new == ["EURUSD|H1|ruptura"]                       # 2 seguidas -> propuesta
    assert "GBPUSD|H1|ruptura" not in st["candidates"]        # rechazada -> racha a cero
    st, new = scout.update_state(st, [_r("EURUSD", True)], 2, set(), "t3")
    assert new == [] and st["candidates"]["EURUSD|H1|ruptura"]["proposed"]   # no se repite
    st, new = scout.update_state(st, [_r("EURUSD", False)], 2, set(), "t4")
    assert "EURUSD|H1|ruptura" not in st["candidates"]        # caduca si deja de pasar


def test_update_state_excludes_existing_and_dismissed() -> None:
    st, _ = scout.update_state({"candidates": {"US500|H1|ruptura": {"streak": 1}}},
                               [_r("EURUSD", True)], 1, {"EURUSD|H1|ruptura", "US500|H1|ruptura"}, "t")
    assert st["candidates"] == {}


def test_select_universe_by_cost() -> None:
    symbols = [
        {"name": "EURUSD", "category": "Forex", "tradable": True},
        {"name": "EXOTIC", "category": "Forex", "tradable": True},
        {"name": "US500", "category": "Indices", "tradable": True},
        {"name": "OFF", "category": "Forex", "tradable": False},
        {"name": "NODATA", "category": "Forex", "tradable": True},
    ]
    ratios = {"EURUSD": 0.05, "EXOTIC": 0.40, "US500": 0.02, "NODATA": None}
    chosen, skipped = scout.select_universe(symbols, ["forex"], 5, 0.15, ratios.get)
    assert chosen == ["EURUSD"]
    assert any("EXOTIC" in s for s in skipped) and any("NODATA" in s for s in skipped)
    chosen, _ = scout.select_universe(symbols, [], 1, 0.15, ratios.get)
    assert chosen == ["US500"]                                # el más barato primero


def test_scan_once_with_fake_mt5(monkeypatch, tmp_path, synthetic_ohlc: pd.DataFrame, capsys) -> None:
    fake = FakeMT5(synthetic_ohlc.assign(spread_price=0.00001))
    monkeypatch.setattr(mt5_connector, "mt5", fake)
    monkeypatch.setattr(mt5_connector, "_connected", False)
    (tmp_path / "logs").mkdir()
    # Ya existe un perfil EURUSD H1 ruptura -> no se vuelve a explorar esa combinación
    bot_common.save_config({"symbol": "EURUSD", "timeframe": "H1", "strategy": "ruptura", "magic": 1},
                           bot_common.config_path_for("principal", tmp_path))
    (tmp_path / "logs" / "scout_decisions.json").write_text(
        json.dumps({"dismissed": ["GBPUSD|H1|ruptura"]}), encoding="utf-8")
    cfg = {**bot_common.DEFAULT_CONFIG, "scout_categories": ["Forex"], "scout_timeframes": ["H1"],
           "scout_strategies": ["ruptura"], "scout_max_symbols": 3, "scout_confirmations": 1,
           "scout_min_pf_oos": 1.0, "scout_min_trades_oos": 3, "history_bars": 1500,
           "population": 6, "generations": 2, "validation_windows": 2, "min_trades": 5,
           "lab_workers": 1}
    summary = scout.scan_once(cfg, tmp_path, stop=lambda: False)
    state = json.loads((tmp_path / "logs" / "scout_state.json").read_text(encoding="utf-8"))
    evaluated = summary["combinations"]
    assert summary["symbols"] == 3 and evaluated == 1        # 3 de Forex - EURUSD y GBPUSD excluidos
    assert all(k.startswith("USDJPY") for k in state["candidates"])
    out = capsys.readouterr().out
    assert "SCOUT_PROGRESS" in out and '"stage": "fin"' in out
    if state["candidates"]:
        assert "SCOUT_PROPOSAL" in out
    assert not mt5_connector.is_connected()


def test_main_once_requires_categories(monkeypatch, capsys) -> None:
    monkeypatch.setattr(scout, "load_global_config", lambda base: dict(bot_common.DEFAULT_CONFIG))
    assert scout.main(["--once"]) == 2
    assert "categoría" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        scout.main([])


def test_config_validation_for_scout_keys() -> None:
    bad = {**bot_common.DEFAULT_CONFIG, "scout_timeframes": ["D1"], "scout_categories": "Forex",
           "scout_confirmations": 0}
    errors = " ".join(bot_common.validate_config(bad))
    assert "scout_timeframes" in errors and "scout_categories" in errors and "scout_confirmations" in errors
