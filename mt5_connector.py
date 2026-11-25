"""
mt5_connector.py
================
Capa de acceso a MetaTrader 5 usando la librería oficial ``MetaTrader5``.

* Conexión con reintentos y verificación de cuenta DEMO.
* Cierre idempotente (``safe_shutdown``).
* Lectura de velas, spread, especificación de símbolo y posiciones.
* Envío de órdenes de mercado y cierre de posiciones del bot (por magic).
* ``MT5DataProvider``: adaptador que cumple el protocolo de ``risk_manager``.

La importación de ``MetaTrader5`` es tolerante: si la librería no está
instalada (p. ej. en Linux/CI), el módulo se importa igualmente y las
funciones fallan con un mensaje claro sólo cuando se llaman.
"""

from __future__ import annotations

import difflib
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from bot_common import CONFIG_PATH, load_config
from risk_manager import AccountSnapshot, SymbolSpec

try:  # La importación no tiene efectos secundarios: no conecta a nada.
    import MetaTrader5 as mt5  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - depende del entorno
    mt5 = None  # type: ignore[assignment]

logger = logging.getLogger("mt5_connector")

# Estado interno de la conexión (para que safe_shutdown sea idempotente).
_connected: bool = False

# Retcodes relevantes (valores oficiales de MQL5, por si la constante no existe).
RETCODE_PLACED = 10008
RETCODE_DONE = 10009
RETCODE_DONE_PARTIAL = 10010
RETCODE_INVALID_FILL = 10030
SUCCESS_RETCODES: frozenset[int] = frozenset({RETCODE_PLACED, RETCODE_DONE, RETCODE_DONE_PARTIAL})


# ---------------------------------------------------------------------------
# Utilidades internas
# ---------------------------------------------------------------------------
def _require_mt5() -> Any:
    """Devuelve el módulo MetaTrader5 o lanza un error claro si no está instalado."""
    if mt5 is None:
        raise RuntimeError(
            "La librería 'MetaTrader5' no está instalada o no es compatible con este "
            "sistema. Instálela en Windows con: pip install MetaTrader5"
        )
    return mt5


def timeframe_from_str(timeframe: str | int) -> int:
    """Convierte 'M1', 'M5', 'M15', 'H1', 'H4' en la constante TIMEFRAME_* de MT5."""
    m = _require_mt5()
    if isinstance(timeframe, int):
        return timeframe
    mapping = {
        "M1": m.TIMEFRAME_M1,
        "M5": m.TIMEFRAME_M5,
        "M15": m.TIMEFRAME_M15,
        "H1": m.TIMEFRAME_H1,
        "H4": m.TIMEFRAME_H4,
    }
    key = str(timeframe).upper().strip()
    if key not in mapping:
        raise ValueError(f"Timeframe no soportado: {timeframe!r}. Use uno de {list(mapping)}")
    return mapping[key]


def timeframe_seconds(timeframe: str) -> int:
    """Duración en segundos de una vela del timeframe dado."""
    return {"M1": 60, "M5": 300, "M15": 900, "H1": 3600, "H4": 14400}[timeframe.upper()]


# ---------------------------------------------------------------------------
# Conexión
# ---------------------------------------------------------------------------
def connect_mt5_with_retry(
    max_retries: int = 10,
    wait_seconds: float = 3,
    require_demo: bool | None = None,
    terminal_path: str | None = None,
    config_path: Path | None = None,
) -> bool:
    """
    Conecta con el terminal MT5 reintentando ``mt5.initialize()`` si falla.

    Tras conectar obtiene ``account_info()``, muestra login, servidor, balance
    y equity, y verifica que la cuenta sea DEMO si así se exige.

    :param max_retries: número máximo de intentos.
    :param wait_seconds: espera entre intentos.
    :param require_demo: exige cuenta DEMO. Si es None se lee de risk_config.json
                         (y si no existe el archivo, se asume True por seguridad).
    :param terminal_path: ruta opcional a terminal64.exe.
    :param config_path: ruta alternativa a risk_config.json.
    :returns: True si la conexión fue correcta, False si se agotaron los intentos.
    :raises SystemExit: si la cuenta no es DEMO y se exige DEMO.
    """
    global _connected
    m = _require_mt5()

    if require_demo is None or terminal_path is None:
        try:
            cfg = load_config(config_path or CONFIG_PATH, must_exist=False)
        except ValueError as exc:
            logger.warning("No se pudo leer la configuración (%s); se exige DEMO.", exc)
            cfg = {"require_demo": True, "terminal_path": ""}
        if require_demo is None:
            require_demo = bool(cfg.get("require_demo", True))
        if terminal_path is None:
            terminal_path = cfg.get("terminal_path") or None

    for attempt in range(1, max_retries + 1):
        ok = m.initialize(path=terminal_path) if terminal_path else m.initialize()
        if ok:
            break
        logger.warning(
            "Intento %d/%d: mt5.initialize() falló: %s", attempt, max_retries, m.last_error()
        )
        m.shutdown()
        if attempt < max_retries:
            time.sleep(wait_seconds)
    else:
        logger.error("No se pudo conectar a MT5 tras %d intentos.", max_retries)
        return False

    _connected = True
    account = m.account_info()
    if account is None:
        logger.error("Conectado pero account_info() es None: %s", m.last_error())
        safe_shutdown()
        return False

    is_demo = account.trade_mode == m.ACCOUNT_TRADE_MODE_DEMO
    mode_name = {
        m.ACCOUNT_TRADE_MODE_DEMO: "DEMO",
        m.ACCOUNT_TRADE_MODE_CONTEST: "CONTEST",
        m.ACCOUNT_TRADE_MODE_REAL: "REAL",
    }.get(account.trade_mode, str(account.trade_mode))

    logger.info(
        "Conectado a MT5 | login=%s | servidor=%s | modo=%s | balance=%.2f %s | equity=%.2f",
        account.login, account.server, mode_name, account.balance,
        account.currency, account.equity,
    )

    if require_demo and not is_demo:
        safe_shutdown()
        raise SystemExit(
            f"ABORTADO: la cuenta {account.login} ({account.server}) es {mode_name}, "
            "pero la configuración exige 'Solo cuenta DEMO' (require_demo=true). "
            "Inicie sesión en una cuenta demo en MT5 o desmarque la opción bajo su "
            "propia responsabilidad."
        )

    terminal = m.terminal_info()
    if terminal is not None and not terminal.trade_allowed:
        logger.warning(
            "El botón 'Algo Trading' del terminal está DESACTIVADO: las órdenes serán rechazadas."
        )
    return True


def is_connected() -> bool:
    """Indica si la conexión con el terminal sigue viva."""
    if mt5 is None or not _connected:
        return False
    return mt5.terminal_info() is not None


def safe_shutdown() -> None:
    """Cierra la conexión con MT5. Es idempotente: puede llamarse varias veces."""
    global _connected
    if mt5 is None or not _connected:
        return
    try:
        mt5.shutdown()
        logger.info("Conexión MT5 cerrada.")
    except Exception:  # pragma: no cover - defensivo
        logger.exception("Error al cerrar MT5 (ignorado).")
    finally:
        _connected = False


# ---------------------------------------------------------------------------
# Datos de mercado
# ---------------------------------------------------------------------------
def _normalize_symbol(name: str) -> str:
    """Nombre en minúsculas y sin signos (us500, US500.a y US-500 -> us500 / us500a)."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def find_similar_symbols(symbol: str, limit: int = 8) -> list[str]:
    """
    Busca en el bróker símbolos con nombre parecido (para sugerir correcciones).

    Orden: misma palabra ignorando mayúsculas, mismo nombre sin signos,
    nombres que lo contienen y, por último, coincidencias aproximadas.
    """
    m = _require_mt5()
    symbols = m.symbols_get()
    if not symbols:
        return []
    names = [s.name for s in symbols]
    query = _normalize_symbol(symbol)
    ranked: list[str] = []

    def _add(candidates: list[str]) -> None:
        for name in candidates:
            if name not in ranked:
                ranked.append(name)

    _add([n for n in names if n.lower() == symbol.lower()])
    _add([n for n in names if _normalize_symbol(n) == query])
    if query:
        _add(sorted((n for n in names if query in _normalize_symbol(n)), key=len))
    upper_map = {n.upper(): n for n in names}
    _add([upper_map[c] for c in difflib.get_close_matches(symbol.upper(), list(upper_map), n=limit, cutoff=0.6)])
    return ranked[:limit]


def list_symbols() -> list[dict[str, Any]]:
    """
    Todos los símbolos del bróker para el buscador del Centro de control.

    :returns: lista de dicts con name, description, category (primer nivel de la
              ruta, p. ej. 'Forex' o 'Indices'), path, spread (precio actual, None si
              no hay cotización), digits, volume_min, visible y tradable.
    """
    m = _require_mt5()
    symbols = m.symbols_get() or ()
    disabled = getattr(m, "SYMBOL_TRADE_MODE_DISABLED", 0)
    out: list[dict[str, Any]] = []
    for s in symbols:
        path = str(getattr(s, "path", "") or "")
        point = float(getattr(s, "point", 0.0) or 0.0)
        spread_pts = float(getattr(s, "spread", 0) or 0)
        out.append({
            "name": s.name,
            "description": str(getattr(s, "description", "") or ""),
            "path": path,
            "category": path.replace("/", "\\").split("\\")[0] if path else "Otros",
            "spread": spread_pts * point if spread_pts > 0 and point > 0 else None,
            "digits": int(getattr(s, "digits", 5) or 5),
            "volume_min": float(getattr(s, "volume_min", 0.0) or 0.0),
            "visible": bool(getattr(s, "visible", False)),
            "tradable": getattr(s, "trade_mode", 4) != disabled,
        })
    out.sort(key=lambda d: (d["category"].lower(), d["name"].lower()))
    return out


def ensure_symbol(symbol: str) -> bool:
    """Asegura que el símbolo exista y esté visible en Market Watch."""
    m = _require_mt5()
    info = m.symbol_info(symbol)
    if info is None:
        suggestions = find_similar_symbols(symbol)
        hint = (f" ¿Quiso decir: {', '.join(suggestions)}?" if suggestions
                else " Revise el nombre exacto en 'Observación del mercado' de MT5.")
        logger.error(
            "Símbolo '%s' no encontrado en el bróker (los nombres distinguen mayúsculas).%s",
            symbol, hint,
        )
        return False
    if not info.visible and not m.symbol_select(symbol, True):
        logger.error("No se pudo activar %s en Market Watch: %s", symbol, m.last_error())
        return False
    return True


def get_rates(symbol: str, timeframe: str | int, n_bars: int) -> pd.DataFrame:
    """
    Descarga las últimas ``n_bars`` velas.

    :returns: DataFrame con columnas time, open, high, low, close, tick_volume,
              spread, real_volume y spread_price (``time`` en hora del servidor
              del bróker, sin zona horaria). Vacío si falla.
    """
    m = _require_mt5()
    if not ensure_symbol(symbol):
        return pd.DataFrame()
    rates = m.copy_rates_from_pos(symbol, timeframe_from_str(timeframe), 0, int(n_bars))
    if rates is None or len(rates) == 0:
        logger.error("copy_rates_from_pos(%s) sin datos: %s", symbol, m.last_error())
        return pd.DataFrame()
    df = pd.DataFrame(rates)
    # MT5 entrega las horas en la zona horaria del SERVIDOR del bróker (no UTC).
    df["time"] = pd.to_datetime(df["time"], unit="s")
    info = m.symbol_info(symbol)
    if "spread" in df.columns and info is not None:
        # Spread histórico de cada vela en unidades de precio (lo usa el backtest).
        df["spread_price"] = df["spread"].astype(float) * float(info.point)
    return df.reset_index(drop=True)


def get_spread(symbol: str) -> float:
    """Spread actual (ask - bid) en unidades de precio. Devuelve NaN si no hay tick."""
    m = _require_mt5()
    tick = m.symbol_info_tick(symbol)
    if tick is None:
        logger.warning("Sin tick para %s: %s", symbol, m.last_error())
        return float("nan")
    return float(tick.ask - tick.bid)


def get_tick(symbol: str) -> Any | None:
    """Último tick (bid/ask) del símbolo, o None si no está disponible."""
    m = _require_mt5()
    return m.symbol_info_tick(symbol)


def get_symbol_spec(symbol: str) -> SymbolSpec | None:
    """Especificación del contrato para el cálculo de lotes."""
    m = _require_mt5()
    info = m.symbol_info(symbol)
    if info is None:
        return None
    return SymbolSpec(
        tick_size=float(info.trade_tick_size),
        tick_value=float(info.trade_tick_value),
        volume_min=float(info.volume_min),
        volume_max=float(info.volume_max),
        volume_step=float(info.volume_step),
    )


def get_min_stop_distance(symbol: str) -> float:
    """Distancia mínima SL/TP exigida por el bróker (trade_stops_level * point)."""
    m = _require_mt5()
    info = m.symbol_info(symbol)
    if info is None:
        return 0.0
    return float(info.trade_stops_level) * float(info.point)


def get_account_login() -> int | None:
    """Número de cuenta (login) conectada, o None."""
    m = _require_mt5()
    info = m.account_info()
    return int(info.login) if info is not None else None


def get_server_time(symbol: str) -> int | None:
    """Hora actual del servidor (epoch, en la zona del servidor) según el último tick."""
    tick = get_tick(symbol)
    return int(tick.time) if tick is not None and getattr(tick, "time", 0) else None


def get_bots_pnl_since(magics: set[int], since_server_time: int) -> tuple[float, float]:
    """
    Resultado de las operaciones de los bots (identificadas por ``magics``).

    :param since_server_time: hora del servidor (epoch) desde la que se suman los cierres.
    :returns: (resultado cerrado desde ``since_server_time`` incluyendo comisiones y
              swap, resultado flotante de las posiciones abiertas de los bots).
    """
    m = _require_mt5()
    positions = m.positions_get() or ()
    floating = sum(float(p.profit) + float(getattr(p, "swap", 0.0))
                   for p in positions if p.magic in magics)

    # Las posiciones se identifican por el deal de entrada (que lleva el magic del bot);
    # los cierres por SL/TP o manuales se asocian por position_id.
    date_to = datetime.now() + timedelta(days=2)
    date_from = datetime.now() - timedelta(days=30)
    deals = m.history_deals_get(date_from, date_to) or ()
    entry_in = getattr(m, "DEAL_ENTRY_IN", 0)
    bot_positions = {d.position_id for d in deals if d.magic in magics and d.entry == entry_in}
    realized = sum(
        float(d.profit) + float(d.commission) + float(d.swap)
        for d in deals
        if int(d.time) >= since_server_time and (d.position_id in bot_positions or d.magic in magics)
    )
    return realized, floating


def get_account_snapshot() -> AccountSnapshot | None:
    """Balance y equity actuales."""
    m = _require_mt5()
    info = m.account_info()
    if info is None:
        return None
    return AccountSnapshot(balance=float(info.balance), equity=float(info.equity))


def count_positions_by_magics(magics: set[int]) -> int:
    """Cuántas posiciones abiertas (de cualquier símbolo) tienen alguno de esos magic."""
    m = _require_mt5()
    positions = m.positions_get()
    if positions is None:
        return 0
    return sum(1 for p in positions if p.magic in magics)


def get_positions(symbol: str, magic: int) -> list[Any]:
    """Posiciones abiertas del símbolo que pertenecen al bot (mismo magic)."""
    m = _require_mt5()
    positions = m.positions_get(symbol=symbol)
    if positions is None:
        return []
    return [p for p in positions if p.magic == magic]


def get_bot_deals(magic: int | set[int], days: int = 7, limit: int | None = 50) -> list[dict[str, Any]]:
    """
    Operaciones CERRADAS del bot (identificadas por su magic o un conjunto de magics)
    en los últimos ``days`` días.

    Agrupa los deals por posición: la entrada debe llevar el magic del bot y la
    salida puede ser manual, por SL o por TP. El resultado incluye comisión y swap.

    :returns: lista de dicts (más reciente primero) con ticket, magic, symbol, side,
              volume, open_time, open_price, close_time, close_price, profit y
              open_ts / close_ts (epoch en hora del servidor).
    """
    magics = {magic} if isinstance(magic, int) else set(magic)
    m = _require_mt5()
    date_to = datetime.now() + timedelta(days=2)  # margen por la zona horaria del servidor
    date_from = datetime.now() - timedelta(days=days)
    deals = m.history_deals_get(date_from, date_to)
    if not deals:
        return []

    entry_in = getattr(m, "DEAL_ENTRY_IN", 0)
    exits = {getattr(m, "DEAL_ENTRY_OUT", 1), getattr(m, "DEAL_ENTRY_OUT_BY", 3)}
    entries = {d.position_id: d for d in deals if d.magic in magics and d.entry == entry_in}
    by_position: dict[int, list[Any]] = {}
    for d in deals:
        if d.position_id in entries:
            by_position.setdefault(d.position_id, []).append(d)

    def _fmt_time(ts: int) -> str:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M")

    out: list[dict[str, Any]] = []
    for pos_id, pos_deals in by_position.items():
        closing = [d for d in pos_deals if d.entry in exits]
        if not closing:
            continue  # posición todavía abierta
        entry = entries[pos_id]
        last = max(closing, key=lambda d: d.time)
        total = sum(float(d.profit) + float(d.commission) + float(d.swap) for d in pos_deals)
        out.append({
            "ticket": int(pos_id),
            "magic": int(entry.magic),
            "open_ts": int(entry.time),
            "close_ts": int(last.time),
            "symbol": entry.symbol,
            "side": "BUY" if entry.type == getattr(m, "DEAL_TYPE_BUY", 0) else "SELL",
            "volume": float(entry.volume),
            "open_time": _fmt_time(entry.time),
            "open_price": float(entry.price),
            "close_time": _fmt_time(last.time),
            "close_price": float(last.price),
            "profit": round(total, 2),
            "_sort": int(last.time),
        })
    out.sort(key=lambda r: r["_sort"], reverse=True)
    for row in out:
        row.pop("_sort")
    return out if limit is None else out[:limit]


def get_position_result(ticket: int) -> float | None:
    """Resultado total (beneficio + comisión + swap) de una posición ya cerrada."""
    m = _require_mt5()
    try:
        deals = m.history_deals_get(position=int(ticket))
    except TypeError:  # versiones antiguas sin el argumento 'position'
        deals = None
    if not deals:
        return None
    exits = {getattr(m, "DEAL_ENTRY_OUT", 1), getattr(m, "DEAL_ENTRY_OUT_BY", 3)}
    if not any(d.entry in exits for d in deals):
        return None
    return round(sum(float(d.profit) + float(d.commission) + float(d.swap) for d in deals), 2)


def modify_position_sl(position: Any, new_sl: float) -> bool:
    """Mueve el stop loss de una posición (mantiene su take profit)."""
    m = _require_mt5()
    request: dict[str, Any] = {
        "action": m.TRADE_ACTION_SLTP,
        "symbol": position.symbol,
        "position": int(position.ticket),
        "sl": _round_price(position.symbol, new_sl),
        "tp": float(getattr(position, "tp", 0.0) or 0.0),
        "magic": int(position.magic),
    }
    result = m.order_send(request)
    if result is not None and result.retcode in SUCCESS_RETCODES:
        logger.info("SL de #%s movido a %.6g", position.ticket, request["sl"])
        return True
    logger.warning("No se pudo mover el SL de #%s: %s", position.ticket,
                   result.comment if result is not None else m.last_error())
    return False


def close_positions_by_magics(magics: set[int], deviation: int = 20) -> tuple[int, int]:
    """
    Cierre de emergencia: cierra TODAS las posiciones abiertas con esos magics
    (en cualquier símbolo). Devuelve (cerradas, fallidas).
    """
    m = _require_mt5()
    closed = failed = 0
    for pos in m.positions_get() or ():
        if pos.magic in magics:
            if close_position(pos, deviation):
                closed += 1
            else:
                failed += 1
    return closed, failed


class MT5DataProvider:
    """Adaptador de MT5 que cumple el protocolo ``MarketDataProvider`` de risk_manager."""

    def get_account(self) -> AccountSnapshot | None:
        """Balance/equity desde MT5."""
        return get_account_snapshot()

    def get_spread(self, symbol: str) -> float | None:
        """Spread desde MT5 (None si no hay tick)."""
        spread = get_spread(symbol)
        return None if spread != spread else spread  # NaN -> None

    def get_symbol_spec(self, symbol: str) -> SymbolSpec | None:
        """Especificación del símbolo desde MT5."""
        return get_symbol_spec(symbol)


# ---------------------------------------------------------------------------
# Órdenes
# ---------------------------------------------------------------------------
def _round_price(symbol: str, price: float) -> float:
    """Redondea un precio a los dígitos del símbolo."""
    m = _require_mt5()
    info = m.symbol_info(symbol)
    digits = int(info.digits) if info is not None else 5
    return round(price, digits)


def send_market_order(
    symbol: str,
    side: str,
    volume: float,
    sl: float,
    tp: float,
    magic: int,
    deviation: int = 20,
    comment: str = "MT5Bot",
) -> Any | None:
    """
    Envía una orden de mercado con SL/TP y verifica el retcode.

    Usa filling IOC; si el bróker lo rechaza (INVALID_FILL) reintenta con FOK
    y luego RETURN.

    :param side: "buy" o "sell".
    :returns: el ``OrderSendResult`` si se ejecutó, None si falló.
    """
    m = _require_mt5()
    tick = m.symbol_info_tick(symbol)
    if tick is None:
        logger.error("order_send cancelado: sin tick para %s", symbol)
        return None

    is_buy = side.lower() == "buy"
    price = tick.ask if is_buy else tick.bid
    request: dict[str, Any] = {
        "action": m.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": float(volume),
        "type": m.ORDER_TYPE_BUY if is_buy else m.ORDER_TYPE_SELL,
        "price": price,
        "sl": _round_price(symbol, sl),
        "tp": _round_price(symbol, tp),
        "deviation": int(deviation),
        "magic": int(magic),
        "comment": comment[:31],
        "type_time": m.ORDER_TIME_GTC,
        "type_filling": m.ORDER_FILLING_IOC,
    }

    for filling in (m.ORDER_FILLING_IOC, m.ORDER_FILLING_FOK, m.ORDER_FILLING_RETURN):
        request["type_filling"] = filling
        result = m.order_send(request)
        if result is None:
            logger.error("order_send devolvió None: %s | request=%s", m.last_error(), request)
            return None
        if result.retcode in SUCCESS_RETCODES:
            logger.info(
                "ORDEN EJECUTADA %s %.2f %s @ %.5f | SL %.5f TP %.5f | ticket=%s deal=%s",
                side.upper(), volume, symbol, result.price or price,
                request["sl"], request["tp"], result.order, result.deal,
            )
            return result
        if result.retcode != RETCODE_INVALID_FILL:
            break
        logger.warning("Filling %s no soportado por el bróker, probando otro modo...", filling)

    logger.error(
        "ORDEN RECHAZADA retcode=%s (%s) | request=%s",
        result.retcode, result.comment, request,
    )
    return None


def close_position(position: Any, deviation: int = 20) -> bool:
    """Cierra una posición concreta con una orden opuesta."""
    m = _require_mt5()
    tick = m.symbol_info_tick(position.symbol)
    if tick is None:
        logger.error("No se puede cerrar #%s: sin tick", position.ticket)
        return False
    is_buy = position.type == m.POSITION_TYPE_BUY
    request: dict[str, Any] = {
        "action": m.TRADE_ACTION_DEAL,
        "symbol": position.symbol,
        "volume": float(position.volume),
        "type": m.ORDER_TYPE_SELL if is_buy else m.ORDER_TYPE_BUY,
        "position": position.ticket,
        "price": tick.bid if is_buy else tick.ask,
        "deviation": int(deviation),
        "magic": int(position.magic),
        "comment": "MT5Bot close",
        "type_time": m.ORDER_TIME_GTC,
    }
    for filling in (m.ORDER_FILLING_IOC, m.ORDER_FILLING_FOK, m.ORDER_FILLING_RETURN):
        request["type_filling"] = filling
        result = m.order_send(request)
        if result is not None and result.retcode in SUCCESS_RETCODES:
            logger.info("Posición #%s cerrada.", position.ticket)
            return True
        if result is None or result.retcode != RETCODE_INVALID_FILL:
            break
    logger.error(
        "No se pudo cerrar #%s: %s",
        position.ticket, result.comment if result is not None else m.last_error(),
    )
    return False


def close_all_positions(symbol: str, magic: int, deviation: int = 20) -> int:
    """Cierra todas las posiciones del bot en el símbolo. Devuelve cuántas se cerraron."""
    closed = 0
    for pos in get_positions(symbol, magic):
        if close_position(pos, deviation):
            closed += 1
    return closed
