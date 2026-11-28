"""Pruebas del diagnóstico (con un MetaTrader5 simulado y un calendario de ejemplo)."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

import bot_common
import diagnostics
import mt5_connector
import news_filter as nf
from tests.fake_mt5 import FakeMT5


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch, synthetic_ohlc: pd.DataFrame) -> FakeMT5:
    fake = FakeMT5(synthetic_ohlc)
    monkeypatch.setattr(mt5_connector, "mt5", fake)
    monkeypatch.setattr(mt5_connector, "_connected", False)
    monkeypatch.setattr(mt5_connector.time, "sleep", lambda s: None)
    mt5_connector.connect_mt5_with_retry(max_retries=1, require_demo=True)
    return fake


def _profile(name: str, symbol: str, magic: int, live: bool = True, status: dict | None = None) -> dict:
    cfg = {**bot_common.DEFAULT_CONFIG, "symbol": symbol, "magic": magic}
    return {"name": name, "cfg": cfg, "status": status or {}, "evolved": {}, "live": live}


def _calendar(tmp_path, minutes_ahead: float) -> nf.NewsCalendar:
    when = datetime.fromtimestamp(time.time() + minutes_ahead * 60, tz=timezone.utc).isoformat()
    raw = [{"title": "Non-Farm Employment Change", "country": "USD", "date": when, "impact": "High"}]
    return nf.NewsCalendar(tmp_path / "cal.json", fetcher=lambda url: raw)


def _lines(report: list[dict], title: str) -> list[tuple[str, str]]:
    return next(s for s in report if s["title"] == title)["lines"]


def test_report_with_news_pause_and_correlation(fake: FakeMT5, tmp_path) -> None:
    status = {"phase": "operando", "dry_run": True, "params_source": "reutilizados (aceptados el X)",
              "next_evolution": "2026-12-05T10:00", "filters": {"news": True, "correlation": True},
              "news": {"currencies": ["EUR", "USD"], "error": "",
                       "event": {"time": "2026-12-04T14:30", "currency": "USD", "title": "NFP",
                                 "blocking": True}},
              "signal_stats": {"signals": 3, "opened": 1, "blocked": {"noticia USD": 2}}}
    profiles = [_profile("principal", "EURUSD", 1, status=status), _profile("gbpusd", "GBPUSD", 2)]
    fake.positions = [SimpleNamespace(ticket=9, symbol="GBPUSD", magic=2, type=0, volume=0.1,
                                      price_open=1.3, sl=0.0, tp=0.0, profit=-3.0, time=0)]
    report = diagnostics.build_report(mt5_connector, dict(bot_common.DEFAULT_CONFIG), profiles,
                                      tmp_path / "cal.json", calendar=_calendar(tmp_path, 10))
    assert [s["title"] for s in report] == ["Conexión y cuenta", "Calendario de noticias",
                                            "Correlación entre sus símbolos", "Estado interno de cada bot"]
    assert ("ok", "Cuenta DEMO") in _lines(report, "Conexión y cuenta")
    news = _lines(report, "Calendario de noticias")
    assert any(level == "ok" and "Calendario descargado" in t for level, t in news)
    assert any(level == "warn" and t.startswith("EURUSD (EUR, USD): AHORA EN PAUSA") for level, t in news)
    corr = _lines(report, "Correlación entre sus símbolos")
    assert any("Ligados: EURUSD y GBPUSD" in t for _l, t in corr)  # mismos datos simulados: corr 1
    assert any(t.startswith("principal (EURUSD): compra BLOQUEADA por GBPUSD BUY") and "venta permitida" in t
               for _l, t in corr)
    bots = _lines(report, "Estado interno de cada bot")
    assert any("reutilizados" in t for _l, t in bots)
    assert any("bloqueadas: noticia USD: 2" in t for _l, t in bots)
    assert any(t.startswith("gbpusd") for _l, t in bots)
    text = diagnostics.report_text(report)
    assert "== Calendario de noticias ==" in text and "[!] EURUSD (EUR, USD): AHORA EN PAUSA" in text


def test_report_without_calendar(fake: FakeMT5, tmp_path) -> None:
    calendar = nf.NewsCalendar(tmp_path / "c.json", fetcher=lambda url: (_ for _ in ()).throw(OSError("sin red")))
    report = diagnostics.build_report(mt5_connector, dict(bot_common.DEFAULT_CONFIG),
                                      [_profile("principal", "EURUSD", 1, live=False)],
                                      tmp_path / "c.json", calendar=calendar)
    news = _lines(report, "Calendario de noticias")
    assert news[0][0] == "bad" and "NO bloquea" in news[0][1]
    bots = _lines(report, "Estado interno de cada bot")
    assert "detenido" in bots[0][1]


def test_free_symbol_and_no_positions(fake: FakeMT5, tmp_path) -> None:
    report = diagnostics.build_report(mt5_connector, dict(bot_common.DEFAULT_CONFIG),
                                      [_profile("principal", "EURUSD", 1)], tmp_path / "cal.json",
                                      calendar=_calendar(tmp_path, 600))
    news = _lines(report, "Calendario de noticias")
    assert any(level == "ok" and "puede operar ahora; próxima pausa" in t for level, t in news)
    corr = _lines(report, "Correlación entre sus símbolos")
    assert any("no hay posiciones abiertas" in t for _l, t in corr)
