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

    from bot_common import PROFILE_KEYS

    keys = {f.key for f in bot_gui.ALL_FIELDS}
    assert keys <= set(DEFAULT_CONFIG)
    assert not keys & set(PROFILE_KEYS)  # lo propio de cada símbolo no está en la general
    assert {"risk_percent", "evolve_sl_tp", "history_bars", "trading_hours", "exit_management",
            "validation_windows", "auto_disable_pf", "telegram_token"} <= keys
    # todo lo de la configuración general es editable desde la ventana
    shared = {k for k in set(DEFAULT_CONFIG) - set(PROFILE_KEYS) if not k.startswith("scout_")}
    assert shared - keys <= {"pf_cap"}   # las claves scout_* se editan en la pestaña Explorador


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


def test_symbol_search_with_spanish_synonyms() -> None:
    gold = {"name": "XAUUSD", "description": "Gold vs US Dollar", "path": "Metals\\Spot"}
    nas = {"name": "NAS100", "description": "US Tech 100 Index", "path": "Indices\\US"}
    eur = {"name": "EURUSD", "description": "Euro vs US Dollar", "path": "Forex\\Majors"}
    assert bot_gui.symbol_matches(gold, "oro")
    assert bot_gui.symbol_matches(gold, "gold")
    assert bot_gui.symbol_matches(nas, "nasdaq")
    assert bot_gui.symbol_matches(eur, "eur usd")
    assert not bot_gui.symbol_matches(eur, "oro")
    assert bot_gui.symbol_matches(eur, "majors")
