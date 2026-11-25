"""Tests de la lógica del Centro de control (se omiten si no hay tkinter)."""

from __future__ import annotations

import sys

import pytest

pytest.importorskip("tkinter")

import bot_gui  # noqa: E402


def test_format_helpers() -> None:
    assert bot_gui.fmt(None) == "—"
    assert bot_gui.fmt(1.23456) == "1.23"
    assert bot_gui.fmt_price(1.098561234) == "1.09856"
    assert bot_gui.fmt("x", "{:.2f}") == "x"


def test_describe_params() -> None:
    text = bot_gui.describe_params({"fast": 6, "slow": 48, "rsi_buy": 70.0, "rsi_sell": 43.37,
                                    "sl_mult": 1.8, "tp_mult": 3.3})
    assert "6/48" in text and "SL 1.80×" in text and "TP 3.30×" in text
    assert bot_gui.describe_params(None) == "—"


def test_fields_cover_known_config_keys() -> None:
    from bot_common import DEFAULT_CONFIG

    keys = {f.key for f in bot_gui.ALL_FIELDS}
    assert keys <= set(DEFAULT_CONFIG)
    assert {"symbol", "timeframe", "risk_percent", "evolve_sl_tp", "history_bars"} <= keys


def test_line_tags() -> None:
    tag = bot_gui.ControlCenter._line_tag
    assert tag("... ORDEN EJECUTADA BUY ...") == "order"
    assert tag("x [ERROR] y") == "error"
    assert tag("x [WARNING] y") == "warning"
    assert tag("x En espera | y") == "wait"
    assert tag("### Iniciando") == "gui"


def test_bot_process_runs_and_stops(tmp_path, monkeypatch) -> None:
    """El proceso hijo se lanza, su salida llega a la cola y termina."""
    script = tmp_path / "trading_bot.py"
    script.write_text("print('hola desde el bot', flush=True)\n", encoding="utf-8")
    monkeypatch.setattr(bot_gui, "BASE_DIR", tmp_path)
    bp = bot_gui.BotProcess("principal", tmp_path / "risk_config.json", dry_run=True)
    bp.start()
    assert bp.proc is not None
    bp.proc.wait(timeout=30)
    import time
    deadline = time.time() + 5
    lines = []
    while time.time() < deadline and not lines:
        try:
            lines.append(bp.lines.get(timeout=0.2))
        except Exception:  # noqa: BLE001
            pass
    assert lines == ["hola desde el bot"]
    assert bp.exit_code == 0 and not bp.running
    bp.request_stop()
    assert (tmp_path / "logs" / "stop_principal.flag").exists()
    assert sys.executable


def test_describe_params_per_strategy() -> None:
    assert "Bollinger 20" in bot_gui.describe_params({"bb_period": 20, "bb_std": 2.0,
                                                       "rsi_low": 30, "rsi_high": 70})
    assert "Ruptura de 24 velas" in bot_gui.describe_params({"lookback": 24, "trend": 100})


def test_profile_name_for_symbol() -> None:
    assert bot_gui.profile_name_for_symbol("US500", "M5", set()) == "us500"
    assert bot_gui.profile_name_for_symbol("US500", "M5", {"us500"}) == "us500_m5"
    assert bot_gui.profile_name_for_symbol("EURUSD.m", "H1", set()) == "eurusd_m"
    assert bot_gui.round_sig(0.000237) == pytest.approx(0.00024)
