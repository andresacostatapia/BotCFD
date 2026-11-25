"""
risk_manager.py
===============
Gestión de riesgo del bot, independiente de MetaTrader 5.

La clase ``RiskManager`` NO importa ``MetaTrader5``. Los datos de mercado y de
cuenta los obtiene de dos formas (ambas testeables):

1. Pasándolos explícitamente como argumentos a cada método
   (``equity=...``, ``spread=...``, ``symbol_spec=...``).
2. A través de un *proveedor de datos* inyectado (``provider``) que cumpla el
   protocolo ``MarketDataProvider``. En producción se usa
   ``mt5_connector.MT5DataProvider``; en tests, un objeto falso.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger("risk_manager")


# ---------------------------------------------------------------------------
# Estructuras de datos
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SymbolSpec:
    """Especificación de contrato del símbolo necesaria para dimensionar lotes."""

    tick_size: float       # trade_tick_size
    tick_value: float      # trade_tick_value (valor de 1 tick para 1 lote, en divisa de la cuenta)
    volume_min: float      # volumen mínimo permitido por el bróker
    volume_max: float      # volumen máximo permitido por el bróker
    volume_step: float     # incremento de volumen


@dataclass(frozen=True)
class AccountSnapshot:
    """Foto de la cuenta en un instante."""

    balance: float
    equity: float


@runtime_checkable
class MarketDataProvider(Protocol):
    """Protocolo que debe cumplir cualquier proveedor de datos (MT5 real o falso)."""

    def get_account(self) -> AccountSnapshot | None:
        """Devuelve balance y equity actuales, o None si no hay datos."""
        ...

    def get_spread(self, symbol: str) -> float | None:
        """Devuelve el spread actual (ask - bid) en unidades de precio."""
        ...

    def get_symbol_spec(self, symbol: str) -> SymbolSpec | None:
        """Devuelve la especificación del símbolo."""
        ...


# ---------------------------------------------------------------------------
# RiskManager
# ---------------------------------------------------------------------------
class RiskManager:
    """Controla pérdida diaria, spread y tamaño de posición."""

    #: Si el lote mínimo supera este múltiplo del riesgo presupuestado -> rechazar.
    MIN_LOT_RISK_TOLERANCE: float = 1.5

    def __init__(
        self,
        symbol: str,
        magic: int,
        config: dict[str, Any],
        provider: MarketDataProvider | None = None,
    ) -> None:
        """
        Crea el gestor de riesgo.

        :param symbol: símbolo a operar (p. ej. "EURUSD").
        :param magic: magic number de las órdenes del bot.
        :param config: diccionario de configuración (ver risk_config.json).
        :param provider: proveedor de datos opcional (MT5 real o falso para tests).
        """
        self.symbol: str = symbol
        self.magic: int = magic
        self.provider: MarketDataProvider | None = provider
        self.config: dict[str, Any] = {}
        self.update_config(config)

        self.trading_halted: bool = False
        self.halt_reason: str = ""
        self.day_start_balance: float | None = None
        self.day_start_date: date | None = None
        self.last_equity: float | None = None
        self.last_spread: float | None = None
        self.last_rejection: str = ""

    # ------------------------------------------------------------------ config
    def update_config(self, config: dict[str, Any]) -> None:
        """Actualiza los parámetros de riesgo (permite recarga en caliente)."""
        self.config = dict(config)
        self.risk_percent: float = float(config.get("risk_percent", 1.0))
        self.max_daily_loss_percent: float = float(config.get("max_daily_loss_percent", 3.0))
        self.max_lot: float = float(config.get("max_lot", 1.0))
        self.min_lot: float = float(config.get("min_lot", 0.01))
        self.max_spread: float = float(config.get("max_spread", 0.0002))
        logger.info(
            "Parámetros de riesgo: riesgo=%.2f%% | pérdida diaria máx=%.2f%% | "
            "lote=[%.2f, %.2f] | spread máx=%.5f",
            self.risk_percent, self.max_daily_loss_percent,
            self.min_lot, self.max_lot, self.max_spread,
        )

    # --------------------------------------------------------------- helpers
    def _reject(self, reason: str) -> None:
        """Registra un rechazo con su razón explícita."""
        self.last_rejection = reason
        logger.warning("RECHAZO: %s", reason)

    def _account(self, equity: float | None = None, balance: float | None = None) -> AccountSnapshot | None:
        """Obtiene datos de cuenta de los argumentos o del proveedor."""
        if equity is not None:
            return AccountSnapshot(balance=balance if balance is not None else equity, equity=equity)
        if self.provider is None:
            return None
        return self.provider.get_account()

    # ------------------------------------------------------------ pérdida diaria
    def reset_daily(
        self, balance: float | None = None, today: date | None = None
    ) -> bool:
        """
        Guarda el balance inicial del día y reactiva el trading.

        :param balance: balance inicial; si es None se pide al proveedor.
        :param today: fecha del día (por defecto, hoy).
        :returns: True si se pudo fijar el balance inicial.
        """
        if balance is None:
            account = self._account()
            if account is None:
                self._reject("reset_daily: no se pudo obtener el balance de la cuenta")
                return False
            balance = account.balance

        self.day_start_balance = float(balance)
        self.day_start_date = today or datetime.now().date()
        self.trading_halted = False
        self.halt_reason = ""
        logger.info(
            "Reset diario (%s): balance inicial = %.2f",
            self.day_start_date.isoformat(), self.day_start_balance,
        )
        return True

    def daily_loss_percent(self, equity: float) -> float:
        """Pérdida del día en % respecto al balance inicial (positivo = pérdida)."""
        if not self.day_start_balance:
            return 0.0
        return (self.day_start_balance - equity) / self.day_start_balance * 100.0

    def check_daily_loss(self, equity: float | None = None) -> bool:
        """
        Comprueba el límite de pérdida diaria usando el equity (incluye flotante).

        :param equity: equity actual; si es None se pide al proveedor.
        :returns: True si se puede seguir operando; False si el trading está detenido.
        """
        if self.trading_halted:
            return False

        if self.day_start_balance is None:
            self._reject("check_daily_loss: reset_daily() no se ha ejecutado")
            return False

        account = self._account(equity=equity)
        if account is None:
            self._reject("check_daily_loss: no se pudo obtener el equity")
            return False

        self.last_equity = account.equity
        loss_pct = self.daily_loss_percent(account.equity)
        if loss_pct >= self.max_daily_loss_percent:
            self.trading_halted = True
            self.halt_reason = (
                f"pérdida diaria {loss_pct:.2f}% >= límite {self.max_daily_loss_percent:.2f}% "
                f"(balance inicial {self.day_start_balance:.2f}, equity {account.equity:.2f})"
            )
            logger.error("TRADING DETENIDO: %s", self.halt_reason)
            return False
        return True

    # ------------------------------------------------------------------ spread
    def check_spread(self, spread: float | None = None) -> bool:
        """
        Compara el spread actual con el máximo permitido.

        :param spread: spread en unidades de precio; si es None se pide al proveedor.
        :returns: True si el spread es aceptable.
        """
        if spread is None:
            if self.provider is None:
                self._reject("check_spread: sin proveedor de datos ni spread explícito")
                return False
            spread = self.provider.get_spread(self.symbol)
        if spread is None:
            self._reject(f"check_spread: no se pudo obtener el spread de {self.symbol}")
            return False

        self.last_spread = float(spread)
        if spread > self.max_spread:
            self._reject(
                f"spread {spread:.5f} > máximo permitido {self.max_spread:.5f} ({self.symbol})"
            )
            return False
        return True

    # ------------------------------------------------------------------ lotaje
    @staticmethod
    def _floor_to_step(value: float, step: float) -> float:
        """Redondea hacia abajo al múltiplo de ``step`` (evita errores de coma flotante)."""
        if step <= 0:
            return value
        decimals = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
        steps = math.floor(value / step + 1e-9)
        return round(steps * step, decimals + 2)

    @staticmethod
    def _ceil_to_step(value: float, step: float) -> float:
        """Redondea hacia arriba al múltiplo de ``step``."""
        if step <= 0:
            return value
        decimals = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
        steps = math.ceil(value / step - 1e-9)
        return round(steps * step, decimals + 2)

    def calculate_lot(
        self,
        stop_loss_distance: float,
        equity: float | None = None,
        symbol_spec: SymbolSpec | None = None,
    ) -> float | None:
        """
        Calcula el lote según el riesgo por operación.

        Fórmula:
            riesgo_$   = equity * risk_percent / 100
            risk_ticks = stop_loss_distance / tick_size
            lot        = riesgo_$ / (risk_ticks * tick_value)

        Después se ajusta al ``volume_step`` (hacia abajo) y se limita entre
        ``min_lot`` y ``max_lot`` (y los límites del bróker). Si el lote mínimo
        ya supera 1.5x el presupuesto de riesgo, devuelve None.

        :param stop_loss_distance: distancia al SL en unidades de precio.
        :param equity: equity actual (si es None se pide al proveedor).
        :param symbol_spec: especificación del símbolo (si es None se pide al proveedor).
        :returns: lote a operar, o None si la operación debe rechazarse.
        """
        if stop_loss_distance is None or not math.isfinite(stop_loss_distance) or stop_loss_distance <= 0:
            self._reject(f"calculate_lot: distancia de SL inválida ({stop_loss_distance})")
            return None

        account = self._account(equity=equity)
        if account is None or account.equity <= 0:
            self._reject("calculate_lot: equity no disponible o <= 0")
            return None

        spec = symbol_spec
        if spec is None and self.provider is not None:
            spec = self.provider.get_symbol_spec(self.symbol)
        if spec is None:
            self._reject(f"calculate_lot: sin especificación de símbolo para {self.symbol}")
            return None
        if spec.tick_size <= 0 or spec.tick_value <= 0:
            self._reject(
                f"calculate_lot: tick_size/tick_value inválidos "
                f"({spec.tick_size}, {spec.tick_value})"
            )
            return None

        risk_money = account.equity * self.risk_percent / 100.0
        risk_ticks = stop_loss_distance / spec.tick_size
        loss_per_lot = risk_ticks * spec.tick_value
        raw_lot = risk_money / loss_per_lot

        step = spec.volume_step if spec.volume_step > 0 else 0.01
        # Límites efectivos: los del usuario dentro de los del bróker.
        lower = self._ceil_to_step(max(self.min_lot, spec.volume_min), step)
        upper = self._floor_to_step(min(self.max_lot, spec.volume_max), step)
        if lower > upper:
            self._reject(
                f"calculate_lot: rango de lote vacío (mín {lower} > máx {upper}); "
                f"revise min_lot/max_lot frente a los límites del bróker "
                f"[{spec.volume_min}, {spec.volume_max}]"
            )
            return None

        # Si incluso el lote mínimo arriesga demasiado -> rechazar.
        min_lot_risk = lower * loss_per_lot
        if min_lot_risk > self.MIN_LOT_RISK_TOLERANCE * risk_money:
            self._reject(
                f"calculate_lot: el lote mínimo {lower} arriesga {min_lot_risk:.2f}, "
                f"más de {self.MIN_LOT_RISK_TOLERANCE}x el presupuesto ({risk_money:.2f}). "
                f"SL demasiado amplio o cuenta demasiado pequeña."
            )
            return None

        lot = self._floor_to_step(raw_lot, step)
        lot = min(max(lot, lower), upper)

        logger.info(
            "Lote calculado: %.2f (bruto %.4f) | riesgo %.2f (%.2f%% de %.2f) | "
            "SL %.5f = %.1f ticks | pérdida/lote %.2f | riesgo real %.2f",
            lot, raw_lot, risk_money, self.risk_percent, account.equity,
            stop_loss_distance, risk_ticks, loss_per_lot, lot * loss_per_lot,
        )
        return lot

    # ------------------------------------------------------------------ estado
    def status(self) -> dict[str, Any]:
        """Devuelve el estado actual del gestor para mostrar en GUI/consola."""
        loss_pct = (
            self.daily_loss_percent(self.last_equity)
            if self.last_equity is not None else None
        )
        return {
            "symbol": self.symbol,
            "magic": self.magic,
            "trading_halted": self.trading_halted,
            "halt_reason": self.halt_reason,
            "day_start_date": self.day_start_date.isoformat() if self.day_start_date else None,
            "day_start_balance": self.day_start_balance,
            "last_equity": self.last_equity,
            "daily_loss_percent": round(loss_pct, 4) if loss_pct is not None else None,
            "max_daily_loss_percent": self.max_daily_loss_percent,
            "last_spread": self.last_spread,
            "max_spread": self.max_spread,
            "risk_percent": self.risk_percent,
            "lot_range": [self.min_lot, self.max_lot],
            "last_rejection": self.last_rejection,
        }


__all__ = ["RiskManager", "SymbolSpec", "AccountSnapshot", "MarketDataProvider"]
