"""Tests de RiskManager con datos inyectados (sin MetaTrader5)."""

from __future__ import annotations

from datetime import date

import pytest

from risk_manager import AccountSnapshot, MarketDataProvider, RiskManager, SymbolSpec

EURUSD = SymbolSpec(tick_size=0.00001, tick_value=1.0, volume_min=0.01,
                    volume_max=100.0, volume_step=0.01)
CONFIG = {"risk_percent": 1.0, "max_daily_loss_percent": 3.0, "max_lot": 1.0,
          "min_lot": 0.01, "max_spread": 0.0002}


class FakeProvider:
    """Proveedor falso que cumple el protocolo MarketDataProvider."""

    def __init__(self, balance: float = 10_000, equity: float = 10_000,
                 spread: float | None = 0.0001, spec: SymbolSpec | None = EURUSD) -> None:
        self.balance, self.equity, self.spread, self.spec = balance, equity, spread, spec

    def get_account(self) -> AccountSnapshot | None:
        return AccountSnapshot(self.balance, self.equity)

    def get_spread(self, symbol: str) -> float | None:
        return self.spread

    def get_symbol_spec(self, symbol: str) -> SymbolSpec | None:
        return self.spec


def make(provider: FakeProvider | None = None, **overrides: float) -> RiskManager:
    return RiskManager("EURUSD", 20250101, {**CONFIG, **overrides}, provider=provider)


def test_module_does_not_import_mt5() -> None:
    import risk_manager
    assert "mt5" not in vars(risk_manager)


def test_fake_provider_matches_protocol() -> None:
    assert isinstance(FakeProvider(), MarketDataProvider)


def test_lot_formula() -> None:
    # equity 10 000, 1% = 100 $. SL 0.00200 = 200 ticks * 1 $ = 200 $/lote -> 0.50 lotes
    rm = make()
    assert rm.calculate_lot(0.0020, equity=10_000, symbol_spec=EURUSD) == pytest.approx(0.50)


def test_lot_rounds_down_to_step() -> None:
    rm = make()
    # 100 / (0.0033/0.00001 * 1) = 0.30303 -> 0.30
    assert rm.calculate_lot(0.0033, equity=10_000, symbol_spec=EURUSD) == pytest.approx(0.30)


def test_lot_clamped_to_max() -> None:
    rm = make(max_lot=0.2)
    assert rm.calculate_lot(0.0010, equity=10_000, symbol_spec=EURUSD) == pytest.approx(0.20)


def test_lot_clamped_to_min_within_tolerance() -> None:
    # equity 1000 -> riesgo 10 $; SL 0.0008 = 80 $/lote -> bruto 0.125 lotes.
    # Con min_lot 0.15 el riesgo real es 12 $ (1.2x <= 1.5x) -> se acepta 0.15
    rm = make(min_lot=0.15)
    assert rm.calculate_lot(0.0008, equity=1_000, symbol_spec=EURUSD) == pytest.approx(0.15)


def test_min_lot_exceeding_risk_returns_none() -> None:
    rm = make()
    # equity 100 -> riesgo 1 $. SL 0.0050 = 500 $/lote. 0.01 lote = 5 $ > 1.5 $ -> None
    assert rm.calculate_lot(0.0050, equity=100, symbol_spec=EURUSD) is None
    assert "lote mínimo" in rm.last_rejection


def test_invalid_sl_distance_returns_none() -> None:
    rm = make()
    assert rm.calculate_lot(0.0, equity=10_000, symbol_spec=EURUSD) is None
    assert rm.calculate_lot(-1.0, equity=10_000, symbol_spec=EURUSD) is None


def test_lot_uses_provider() -> None:
    rm = make(FakeProvider(equity=20_000))
    assert rm.calculate_lot(0.0020) == pytest.approx(1.0)  # 200 $ / 200 $ = 1.0 (= max_lot)


def test_broker_volume_limits_respected() -> None:
    spec = SymbolSpec(0.00001, 1.0, volume_min=0.1, volume_max=0.5, volume_step=0.1)
    rm = make(max_lot=5.0)
    assert rm.calculate_lot(0.0005, equity=100_000, symbol_spec=spec) == pytest.approx(0.5)


def test_daily_loss_halts_trading() -> None:
    provider = FakeProvider(balance=10_000, equity=10_000)
    rm = make(provider)
    assert rm.reset_daily(today=date(2025, 1, 1))
    assert rm.check_daily_loss() is True
    provider.equity = 9_750  # -2.5 %
    assert rm.check_daily_loss() is True
    provider.equity = 9_690  # -3.1 %
    assert rm.check_daily_loss() is False
    assert rm.trading_halted
    provider.equity = 10_500  # aunque se recupere, sigue detenido hasta reset
    assert rm.check_daily_loss() is False
    rm.reset_daily(today=date(2025, 1, 2))
    assert rm.check_daily_loss() is True


def test_daily_loss_requires_reset() -> None:
    rm = make(FakeProvider())
    assert rm.check_daily_loss() is False
    assert "reset_daily" in rm.last_rejection


def test_spread_check() -> None:
    rm = make()
    assert rm.check_spread(0.00015) is True
    assert rm.check_spread(0.00025) is False
    assert "spread" in rm.last_rejection
    assert make(FakeProvider(spread=None)).check_spread() is False


def test_status_and_hot_reload() -> None:
    rm = make(FakeProvider())
    rm.reset_daily()
    rm.check_daily_loss()
    status = rm.status()
    assert status["trading_halted"] is False
    assert status["day_start_balance"] == 10_000
    rm.update_config({**CONFIG, "risk_percent": 2.0})
    assert rm.status()["risk_percent"] == 2.0
