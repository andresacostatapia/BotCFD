"""Módulo MetaTrader5 falso para probar mt5_connector y trading_bot sin terminal."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd

TIMEFRAME_M1, TIMEFRAME_M5, TIMEFRAME_M15, TIMEFRAME_H1, TIMEFRAME_H4 = 1, 5, 15, 16385, 16388
ACCOUNT_TRADE_MODE_DEMO, ACCOUNT_TRADE_MODE_CONTEST, ACCOUNT_TRADE_MODE_REAL = 0, 1, 2
TRADE_ACTION_DEAL = 1
ORDER_TYPE_BUY, ORDER_TYPE_SELL = 0, 1
POSITION_TYPE_BUY, POSITION_TYPE_SELL = 0, 1
ORDER_TIME_GTC = 0
ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN = 0, 1, 2


def _epoch_seconds(times: pd.Series) -> np.ndarray:
    """Segundos desde epoch, independiente de la resolución interna de pandas."""
    naive = times.dt.tz_localize(None) if times.dt.tz is not None else times
    return ((naive - pd.Timestamp("1970-01-01")) // pd.Timedelta(seconds=1)).to_numpy(dtype="int64")


class FakeMT5:
    """Estado de un terminal falso. Se usa como módulo: atributos = API de MT5."""

    def __init__(self, df: pd.DataFrame, trade_mode: int = ACCOUNT_TRADE_MODE_DEMO,
                 fail_initialize: int = 0, supported_filling: int = ORDER_FILLING_IOC) -> None:
        for name, value in globals().items():
            if name.isupper():
                setattr(self, name, value)
        self.df = df
        self.trade_mode = trade_mode
        self.fail_initialize = fail_initialize
        self.supported_filling = supported_filling
        self.initialized = False
        self.orders: list[dict[str, Any]] = []
        self.positions: list[SimpleNamespace] = []
        self.balance = self.equity = 10_000.0

    # --- conexión
    def initialize(self, path: str | None = None) -> bool:
        if self.fail_initialize > 0:
            self.fail_initialize -= 1
            return False
        self.initialized = True
        return True

    def shutdown(self) -> None:
        self.initialized = False

    def last_error(self) -> tuple[int, str]:
        return (0, "ok")

    def account_info(self) -> SimpleNamespace:
        return SimpleNamespace(login=123, server="Fake-Demo", balance=self.balance,
                               equity=self.equity, currency="USD", trade_mode=self.trade_mode)

    def terminal_info(self) -> SimpleNamespace | None:
        return SimpleNamespace(trade_allowed=True) if self.initialized else None

    # --- símbolos
    symbols: tuple[str, ...] = ("EURUSD", "GBPUSD", "US500", "US30", "USDJPY")

    def symbols_get(self) -> tuple[SimpleNamespace, ...]:
        return tuple(SimpleNamespace(name=n) for n in self.symbols)

    def symbol_info(self, symbol: str) -> SimpleNamespace | None:
        if symbol not in self.symbols:
            return None
        return SimpleNamespace(visible=True, trade_tick_size=0.00001, trade_tick_value=1.0,
                               volume_min=0.01, volume_max=100.0, volume_step=0.01,
                               digits=5, point=0.00001, trade_stops_level=10)

    def symbol_select(self, symbol: str, enable: bool) -> bool:
        return True

    def symbol_info_tick(self, symbol: str) -> SimpleNamespace:
        last = float(self.df["close"].iloc[-1])
        # Hora del servidor: 1 minuto después de abrir la vela en curso (última fila).
        now = int(_epoch_seconds(self.df["time"].iloc[-1:])[0]) + 60
        return SimpleNamespace(bid=last, ask=last + 0.0001, time=now)

    def copy_rates_from_pos(self, symbol: str, tf: int, start: int, count: int) -> np.ndarray:
        sub = self.df.iloc[-count:]
        out = np.zeros(len(sub), dtype=[("time", "i8"), ("open", "f8"), ("high", "f8"),
                                         ("low", "f8"), ("close", "f8"), ("tick_volume", "i8"),
                                         ("spread", "i4"), ("real_volume", "i8")])
        out["time"] = _epoch_seconds(sub["time"])
        out["spread"] = 10
        for col in ("open", "high", "low", "close"):
            out[col] = sub[col].to_numpy()
        return out

    # --- trading
    def positions_get(self, symbol: str | None = None) -> tuple[SimpleNamespace, ...]:
        return tuple(self.positions)

    def order_send(self, request: dict[str, Any]) -> SimpleNamespace:
        if request["type_filling"] != self.supported_filling:
            return SimpleNamespace(retcode=10030, comment="Unsupported filling mode",
                                   order=0, deal=0, price=0.0)
        self.orders.append(dict(request))
        if "position" in request:
            self.positions = [p for p in self.positions if p.ticket != request["position"]]
        else:
            self.positions.append(SimpleNamespace(
                ticket=len(self.orders), symbol=request["symbol"], volume=request["volume"],
                magic=request["magic"],
                type=POSITION_TYPE_BUY if request["type"] == ORDER_TYPE_BUY else POSITION_TYPE_SELL))
        return SimpleNamespace(retcode=10009, comment="done", order=len(self.orders),
                               deal=len(self.orders), price=request["price"])
