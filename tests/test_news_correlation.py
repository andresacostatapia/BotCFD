"""Pruebas del filtro de noticias y del control de correlación."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

import bot_common
import mt5_connector
import news_filter as nf
import trading_bot
from tests.fake_mt5 import FakeMT5

NOW = datetime(2026, 12, 4, 13, 0, tzinfo=timezone.utc).timestamp()  # viernes 13:00 UTC

RAW = [
    {"title": "Non-Farm Employment Change", "country": "USD", "date": "2026-12-04T08:30:00-05:00",
     "impact": "High", "forecast": "", "previous": ""},          # 13:30 UTC
    {"title": "German Factory Orders", "country": "EUR", "date": "2026-12-04T02:00:00-05:00",
     "impact": "Medium", "forecast": "", "previous": ""},        # 07:00 UTC
    {"title": "BOJ Gov Speaks", "country": "JPY", "date": "2026-12-04T09:00:00-05:00",
     "impact": "High", "forecast": "", "previous": ""},          # 14:00 UTC
    {"title": "Bank Holiday", "country": "CAD", "date": "2026-12-04T08:00:00-05:00",
     "impact": "Holiday", "forecast": "", "previous": ""},
    {"title": "sin fecha", "country": "USD", "impact": "High"},  # mal formada: se ignora
]


# ----------------------------------------------------------------- noticias
def test_parse_events_converts_to_utc() -> None:
    events = nf.parse_events(RAW)
    assert len(events) == 4 and events == sorted(events, key=lambda e: e.ts)
    nfp = next(e for e in events if e.title.startswith("Non-Farm"))
    assert nfp.ts == datetime(2026, 12, 4, 13, 30, tzinfo=timezone.utc).timestamp()
    assert nfp.currency == "USD" and nfp.impact == "High"


def test_blocking_window_and_impacts() -> None:
    events = nf.parse_events(RAW)
    high = nf.impacts_for("alto")
    # 13:00, NFP a las 13:30: dentro de los 30 min previos
    assert nf.find_blocking(events, ["EUR", "USD"], NOW, 30, 30, high).title.startswith("Non-Farm")
    assert nf.find_blocking(events, ["EUR", "USD"], NOW, 20, 30, high) is None
    # después de la noticia: 13:30 + 25 min sigue en pausa con 30 min "después"
    assert nf.find_blocking(events, ["USD"], NOW + 55 * 60, 30, 30, high) is not None
    assert nf.find_blocking(events, ["USD"], NOW + 65 * 60, 30, 30, high) is None
    # otras divisas no afectan; el impacto medio solo cuenta con nivel "medio"
    assert nf.find_blocking(events, ["GBP"], NOW, 30, 30, high) is None
    morning = datetime(2026, 12, 4, 7, 10, tzinfo=timezone.utc).timestamp()
    assert nf.find_blocking(events, ["EUR"], morning, 30, 30, high) is None
    assert nf.find_blocking(events, ["EUR"], morning, 30, 30, nf.impacts_for("medio")) is not None
    nxt = nf.next_event(events, ["USD", "JPY"], NOW, 30, high)
    assert nxt.title.startswith("Non-Farm")
    assert nf.next_event(events, ["GBP"], NOW, 30, high) is None
    info = nxt.to_dict(NOW, True)
    assert info["minutes"] == 30 and info["impact"] == "alto" and info["blocking"]


def test_calendar_cache_refresh_and_fallback(tmp_path) -> None:
    calls = {"n": 0, "fail": False}

    def fetch(url: str) -> list:
        calls["n"] += 1
        if calls["fail"]:
            raise OSError("sin internet")
        return RAW

    path = tmp_path / "logs" / "news_calendar.json"
    cal = nf.NewsCalendar(path, refresh_hours=6, fetcher=fetch)
    assert len(cal.events(NOW)) == 4 and calls["n"] == 1 and cal.last_error == ""
    assert len(cal.events(NOW + 3600)) == 4 and calls["n"] == 1           # caché vigente
    # otro proceso (otro bot) usa la misma caché sin descargar
    other = nf.NewsCalendar(path, refresh_hours=6, fetcher=fetch)
    assert len(other.events(NOW + 7200)) == 4 and calls["n"] == 1
    # caducada y sin internet: se usa la copia guardada y se informa del error
    calls["fail"] = True
    assert len(cal.events(NOW + 7 * 3600)) == 4 and calls["n"] == 2
    assert "no se pudo descargar" in cal.last_error
    assert len(cal.events(NOW + 7 * 3600 + 600)) == 4 and calls["n"] == 2  # espera antes de reintentar
    # demasiado antigua: no bloquea nada
    assert cal.events(NOW + 9 * 86400) == [] and cal.last_error


def test_calendar_without_any_copy_does_not_block(tmp_path) -> None:
    cal = nf.NewsCalendar(tmp_path / "c.json", fetcher=lambda url: (_ for _ in ()).throw(OSError("x")))
    assert cal.events(NOW) == [] and "no se pudo descargar" in cal.last_error


# ------------------------------------------------------------- bot (fake MT5)
@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch, synthetic_ohlc: pd.DataFrame) -> FakeMT5:
    fake = FakeMT5(synthetic_ohlc)
    monkeypatch.setattr(mt5_connector, "mt5", fake)
    monkeypatch.setattr(mt5_connector, "_connected", False)
    monkeypatch.setattr(mt5_connector.time, "sleep", lambda s: None)
    return fake


@pytest.fixture
def bot(fake: FakeMT5, tmp_path) -> trading_bot.TradingBot:
    path = tmp_path / "risk_config.json"
    bot_common.save_config({"max_spread": 0.0005, "simulation": False}, path)
    mt5_connector.connect_mt5_with_retry(max_retries=1, require_demo=True)
    b = trading_bot.TradingBot(bot_common.load_config(path), path, dry_run=False)
    b.risk.reset_daily()
    b.resolve_asset()
    return b


def _with_calendar(bot: trading_bot.TradingBot, raw: list) -> None:
    bot._news = nf.NewsCalendar(bot.config_path.parent / "logs" / "news_calendar.json",
                                url=bot.config["news_url"], refresh_hours=6, fetcher=lambda url: raw)


def test_symbol_currencies_and_correlation(fake: FakeMT5) -> None:
    mt5_connector.connect_mt5_with_retry(max_retries=1, require_demo=True)
    assert mt5_connector.symbol_currencies("EURUSD") == ["EUR", "USD"]
    assert mt5_connector.returns_correlation("EURUSD", "EURUSD") == 1.0
    assert mt5_connector.returns_correlation("EURUSD", "GBPUSD") == pytest.approx(1.0)  # mismos datos


def test_bot_news_check(bot: trading_bot.TradingBot) -> None:
    assert bot.currencies == ["EUR", "USD"]
    _with_calendar(bot, RAW)
    event = bot.news_check(NOW)
    assert event is not None and event.currency == "USD"
    assert bot.news_info["event"]["blocking"] and bot.news_info["currencies"] == ["EUR", "USD"]
    assert bot.news_check(NOW - 3 * 3600) is None
    assert bot.news_info["event"]["title"].startswith("Non-Farm") and not bot.news_info["event"]["blocking"]
    bot.config["news_filter"] = False
    assert bot.news_check(NOW) is None and bot.news_info is None


def test_news_blocks_signal_in_step(fake: FakeMT5, bot: trading_bot.TradingBot) -> None:
    import numpy as np

    from strategy_evolver import IndicatorCache, signal_array

    params = {"fast": 10, "slow": 40, "rsi_buy": 50.0, "rsi_sell": 50.0}
    idx = int(np.flatnonzero(signal_array(IndicatorCache(fake.df), params))[-3])
    fake.df = fake.df.iloc[: idx + 2]
    bot.params = params
    now = __import__("time").time()
    when = datetime.fromtimestamp(now + 600, tz=timezone.utc).isoformat()
    _with_calendar(bot, [{"title": "CPI m/m", "country": "USD", "date": when, "impact": "High"}])
    bot.step()
    assert fake.orders == []
    assert "noticia USD" in bot.signal_stats["blocked"]


def test_close_positions_before_news(fake: FakeMT5, bot: trading_bot.TradingBot) -> None:
    pos = SimpleNamespace(ticket=7, symbol="EURUSD", volume=0.1, magic=bot.config["magic"], type=0,
                          price_open=1.1, sl=1.0, tp=1.2, profit=0.0, time=0)
    fake.positions = [pos]
    _with_calendar(bot, RAW)
    bot.close_before_news([pos], NOW)                    # opción desactivada por defecto
    assert fake.positions == [pos]
    bot.config["news_close_positions"] = True
    bot.close_before_news([pos], NOW + 45 * 60)          # la noticia ya pasó: no se cierra
    assert fake.positions == [pos]
    bot.close_before_news([pos], NOW)
    assert fake.positions == []                          # cerrada antes de la noticia


def test_correlation_block(fake: FakeMT5, bot: trading_bot.TradingBot, monkeypatch) -> None:
    monkeypatch.setattr(bot, "portfolio_magics", lambda: {bot.config["magic"], 999})
    other = SimpleNamespace(ticket=5, symbol="GBPUSD", volume=0.1, magic=999, type=0,
                            price_open=1.3, sl=1.2, tp=1.4, profit=0.0, time=0)
    fake.positions = [other]
    assert "GBPUSD BUY" in bot.correlation_block(1)      # misma dirección y correlación 1
    assert bot.correlation_block(-1) is None             # dirección contraria: cubre, no suma
    bot.config["max_correlated_positions"] = 2
    assert bot.correlation_block(1) is None              # se permiten 2 en el grupo
    bot.config["max_correlated_positions"] = 1
    # correlación negativa: comprar aquí equivale a la misma apuesta que vender allí
    bot._corr_cache.clear()
    monkeypatch.setattr(mt5_connector, "returns_correlation", lambda *a, **k: -0.9)
    other.type = 1
    assert bot.correlation_block(1) is not None
    assert bot.correlation_block(-1) is None
    # poco correlacionados: no bloquea
    bot._corr_cache.clear()
    monkeypatch.setattr(mt5_connector, "returns_correlation", lambda *a, **k: 0.3)
    assert bot.correlation_block(-1) is None and bot.correlation_block(1) is None
    bot.config["correlation_filter"] = False
    assert bot.correlation_block(1) is None


def test_correlation_blocks_order(fake: FakeMT5, bot: trading_bot.TradingBot, monkeypatch) -> None:
    import numpy as np

    from strategy_evolver import IndicatorCache, signal_array

    params = {"fast": 10, "slow": 40, "rsi_buy": 50.0, "rsi_sell": 50.0}
    sig = signal_array(IndicatorCache(fake.df), params)
    idx = int(np.flatnonzero(sig)[-3])
    fake.df = fake.df.iloc[: idx + 2]
    bot.params = params
    monkeypatch.setattr(bot, "portfolio_magics", lambda: {bot.config["magic"], 999})
    side = int(sig[idx])
    fake.positions = [SimpleNamespace(ticket=5, symbol="GBPUSD", volume=0.1, magic=999,
                                      type=0 if side == 1 else 1, price_open=1.3, sl=0.0, tp=0.0,
                                      profit=0.0, time=0)]
    bot.step()
    assert fake.orders == []
    assert bot.signal_stats["blocked"].get("correlación con otra posición") == 1


def test_config_validation_for_new_keys() -> None:
    base = dict(bot_common.DEFAULT_CONFIG)
    assert bot_common.validate_config(base) == []
    assert bot_common.validate_config({**base, "news_impact": "bajo"})
    assert bot_common.validate_config({**base, "correlation_threshold": 1.5})
    assert bot_common.validate_config({**base, "max_correlated_positions": 0})
    assert bot_common.validate_config({**base, "news_minutes_before": -5})
