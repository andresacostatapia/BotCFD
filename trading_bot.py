"""
trading_bot.py
==============
Orquestador principal: carga configuración -> conecta MT5 -> evoluciona y
valida parámetros (walk-forward) -> bucle de trading con gestión de riesgo.

Uso:
    python trading_bot.py              # opera (en DEMO, si require_demo=true)
    python trading_bot.py --dry-run    # calcula todo pero NO envía órdenes

Códigos de salida:
    0 = parada limpia (Ctrl+C)   1 = error inesperado   2 = configuración
    3 = conexión MT5             4 = validación de parámetros fallida

AVISO: framework educativo. No garantiza rentabilidad. Pruebe siempre en DEMO.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import mt5_connector as mt5c
from bot_common import (
    CONFIG_PATH, ProfilePaths, config_path_for, list_profiles, load_config, now_iso,
    profile_paths, read_json, setup_logging, write_json_atomic,
)
from risk_manager import RiskManager
from strategy_evolver import compute_signal, evolve_parameters, warmup_bars

logger = logging.getLogger("trading_bot")

EXIT_OK, EXIT_ERROR, EXIT_CONFIG, EXIT_CONNECTION, EXIT_VALIDATION = 0, 1, 2, 3, 4
DEALS_REFRESH_SECONDS = 300


class StatusFile:
    """Estado en vivo del bot (JSON) que lee el Centro de control (bot_gui.py)."""

    def __init__(self, paths: ProfilePaths, dry_run: bool) -> None:
        """Prepara el archivo de estado del perfil."""
        self.paths = paths
        self.dry_run = dry_run
        self.persistent: dict[str, Any] = {}

    def write(self, phase: str, message: str = "", **extra: Any) -> None:
        """
        Escribe el estado. ``phase``: iniciando, conectando, evolucionando, operando,
        detenido_perdida, sin_parametros, descartado, error o finalizado.
        """
        data = {
            "profile": self.paths.name,
            "pid": os.getpid(),
            "dry_run": self.dry_run,
            "updated": now_iso(),
            **self.persistent,
            "phase": phase,
            "message": message,
            **extra,
        }
        write_json_atomic(self.paths.status, data)




class DayState:
    """
    Inicio del día de trading guardado en disco (``logs/day_state.json``), por cuenta.

    Lo comparten todos los perfiles de la carpeta: el primer bot del día fija el
    balance inicial y los demás (y los reinicios) lo reutilizan, así reiniciar no
    pone el contador de pérdida diaria a cero. Si un bot se detiene por pérdida
    diaria, queda anotado y los demás bots con el mismo criterio lo respetan.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def _load(self) -> dict[str, Any]:
        data = read_json(self.path)
        return data if isinstance(data, dict) else {}

    def get_or_create(self, login: int | str, today: str, balance: float,
                      server_time: int | None) -> tuple[dict[str, Any], bool]:
        """Devuelve (registro del día, True si se restauró uno existente)."""
        data = self._load()
        rec = data.get(str(login))
        if isinstance(rec, dict) and rec.get("date") == today and rec.get("start_balance"):
            return rec, True
        rec = {
            "date": today,
            "start_balance": float(balance),
            "start_server_time": int(server_time) if server_time else int(time.time()),
            "created": now_iso(),
            "halted": {},
        }
        data[str(login)] = rec
        write_json_atomic(self.path, data)
        return rec, False

    def halted_reason(self, login: int | str, today: str, scope: str) -> str:
        """Motivo si algún bot ya detuvo el trading hoy con este criterio ('' si no)."""
        rec = self._load().get(str(login)) or {}
        if rec.get("date") != today:
            return ""
        return str((rec.get("halted") or {}).get(scope, ""))

    def mark_halted(self, login: int | str, today: str, scope: str, reason: str) -> None:
        """Anota que el trading quedó detenido hoy con este criterio."""
        data = self._load()
        rec = data.get(str(login))
        if not isinstance(rec, dict) or rec.get("date") != today:
            return
        rec.setdefault("halted", {})[scope] = reason
        write_json_atomic(self.path, data)


class TradingBot:
    """Bot de trading: mantiene estado, parámetros evolucionados y el bucle principal."""

    def __init__(
        self,
        config: dict[str, Any],
        config_path: Path,
        dry_run: bool = False,
        status: StatusFile | None = None,
    ) -> None:
        """
        :param config: configuración ya validada.
        :param config_path: ruta de risk_config*.json (para recarga en caliente).
        :param dry_run: si True, no se envían órdenes reales.
        :param status: archivo de estado para el Centro de control (opcional).
        """
        self.config = config
        self.config_path = config_path
        self.paths = profile_paths(config_path)
        self.dry_run = dry_run
        self.status = status or StatusFile(self.paths, dry_run)
        self.config_mtime = config_path.stat().st_mtime
        self.risk = RiskManager(
            config["symbol"], int(config["magic"]), config, provider=mt5c.MT5DataProvider()
        )
        self.params: dict[str, Any] | None = None
        self.evolution: dict[str, Any] | None = None
        self.last_bar_time: Any = None      # última vela cuya señal ya se procesó
        self.logged_bar_time: Any = None    # última vela ya mostrada en el log
        self.last_heartbeat: float = 0.0    # time.monotonic() del último mensaje de espera
        self.halt_logged = False
        self.last_message = "iniciando"
        self.last_sig: dict[str, Any] | None = None
        self.last_positions: list[Any] = []
        self.last_tick: Any = None
        self._deals: list[dict[str, Any]] = []
        self._deals_time = 0.0
        self.day_state = DayState(config_path.parent / "logs" / "day_state.json")
        self.login: int | None = None
        self.day_start_server_time: int | None = None
        self.bots_pnl: tuple[float, float] | None = None  # (cerrado hoy, flotante) si scope=bots

    # ---------------------------------------------------------------- evolución
    def evolve(self) -> bool:
        """Descarga histórico, evoluciona parámetros y valida walk-forward."""
        cfg = self.config
        df = mt5c.get_rates(cfg["symbol"], cfg["timeframe"], int(cfg["history_bars"]) + 1)
        if df.empty:
            logger.error("Sin datos históricos para %s %s.", cfg["symbol"], cfg["timeframe"])
            self.params = None
            return False
        df = df.iloc[:-1]  # descartar la vela en formación

        logger.info("Datos para evolución: %s %s | %d velas cerradas desde %s hasta %s (hora servidor)",
                    cfg["symbol"], cfg["timeframe"], len(df), df["time"].iloc[0], df["time"].iloc[-1])
        result = evolve_parameters(
            df, pop=int(cfg["population"]), gens=int(cfg["generations"]), config=cfg
        )
        self.evolution = result
        self._save_evolution(result)
        if not result["accepted"]:
            logger.error("Parámetros DESCARTADOS por la validación: %s", result["reason"])
            self.params = None
            return False

        self.params = result["params"]
        logger.info(
            "Parámetros ACEPTADOS (%s): %s | PF IS=%.3f | PF OOS=%.3f",
            result["backend"], self.params,
            result["in_sample"]["profit_factor"], result["out_of_sample"]["profit_factor"],
        )
        return True

    def _save_evolution(self, result: dict[str, Any]) -> None:
        """Guarda la evolución en evolved_params*.json y la añade al historial del perfil."""
        payload = {
            "timestamp": now_iso(),
            "profile": self.paths.name,
            "symbol": self.config["symbol"],
            "timeframe": self.config["timeframe"],
            "history_bars": self.config["history_bars"],
            **result,
        }
        if not write_json_atomic(self.paths.evolved, payload):
            logger.error("No se pudo guardar %s", self.paths.evolved)

        summary = {
            "timestamp": payload["timestamp"],
            "symbol": payload["symbol"],
            "timeframe": payload["timeframe"],
            "strategy": result.get("strategy") or self.config.get("strategy"),
            "accepted": result.get("accepted"),
            "reason": result.get("reason"),
            "params": result.get("params"),
            "pf_is": (result.get("in_sample") or {}).get("profit_factor"),
            "trades_is": (result.get("in_sample") or {}).get("trades"),
            "pf_oos": (result.get("out_of_sample") or {}).get("profit_factor"),
            "trades_oos": (result.get("out_of_sample") or {}).get("trades"),
        }
        self.status.persistent["evolution"] = summary
        try:
            self.paths.history.parent.mkdir(parents=True, exist_ok=True)
            with self.paths.history.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(summary, ensure_ascii=False, default=str) + "\n")
        except OSError:
            logger.exception("No se pudo escribir el historial %s", self.paths.history)

    # --------------------------------------------------------- recarga en caliente
    def reload_config_if_changed(self) -> None:
        """Recarga risk_config.json si cambió en disco (sin reiniciar el bot)."""
        try:
            mtime = self.config_path.stat().st_mtime
        except FileNotFoundError:
            logger.warning("risk_config.json desapareció; se mantiene la configuración actual.")
            return
        if mtime == self.config_mtime:
            return
        self.config_mtime = mtime

        try:
            new_cfg = load_config(self.config_path)
        except (ValueError, FileNotFoundError) as exc:
            logger.error("Recarga ignorada, configuración inválida: %s", exc)
            return

        old = self.config
        self.config = new_cfg
        self.risk.update_config(new_cfg)
        logger.info("Configuración recargada en caliente desde %s", self.config_path.name)

        if new_cfg["require_demo"] and not old["require_demo"]:
            logger.info("require_demo activado: se verificará la cuenta al reconectar.")

        structural = ("symbol", "timeframe", "magic", "strategy")
        if any(old[k] != new_cfg[k] for k in structural):
            logger.warning(
                "Cambio de símbolo/timeframe/magic (%s %s %s -> %s %s %s): re-evolucionando...",
                old["symbol"], old["timeframe"], old["magic"],
                new_cfg["symbol"], new_cfg["timeframe"], new_cfg["magic"],
            )
            self.risk.symbol = new_cfg["symbol"]
            self.risk.magic = int(new_cfg["magic"])
            self.last_bar_time = None
            if not self.evolve():
                logger.error("La nueva configuración no superó la validación: trading en pausa.")

    # ------------------------------------------------------------------- bucle
    @property
    def loss_scope(self) -> str:
        """'cuenta' (equity de toda la cuenta) o 'bots' (solo operaciones de los bots)."""
        return "bots" if self.config.get("daily_loss_scope") == "bots" else "cuenta"

    def start_day(self) -> bool:
        """
        Fija el inicio del día: restaura el guardado en disco (si es de hoy) o lo crea
        con el balance actual. Devuelve False si no se pudo leer la cuenta.
        """
        account = self.risk.provider.get_account() if self.risk.provider else None
        if account is None:
            logger.error("No se pudo leer el balance de la cuenta para iniciar el día.")
            return False
        today = datetime.now().date()
        if self.login is None:
            self.login = mt5c.get_account_login() or 0
        rec, restored = self.day_state.get_or_create(
            self.login, today.isoformat(), account.balance, mt5c.get_server_time(self.config["symbol"])
        )
        self.risk.reset_daily(balance=float(rec["start_balance"]), today=today)
        self.day_start_server_time = int(rec["start_server_time"])
        self.halt_logged = False
        if restored:
            logger.info("Inicio del día restaurado (guardado %s): balance inicial %.2f.",
                        str(rec.get("created", "")).replace("T", " "), rec["start_balance"])
        logger.info("Límite de pérdida diaria: %.2f%% medido sobre %s.",
                    self.risk.max_daily_loss_percent,
                    "SOLO las operaciones de los bots" if self.loss_scope == "bots"
                    else "toda la cuenta (incluye operaciones manuales)")
        return True

    def check_new_day(self) -> None:
        """Inicia un nuevo día de trading al cambiar la fecha (00:00 hora local)."""
        today = datetime.now().date()
        if self.risk.day_start_date != today:
            logger.info("Nuevo día de trading: %s", today.isoformat())
            self.start_day()

    def check_daily_loss(self) -> bool:
        """
        Aplica el límite de pérdida diaria según ``daily_loss_scope`` y lo comparte con
        los demás bots de la cuenta. Devuelve True si se puede seguir operando.
        """
        scope = self.loss_scope
        today = self.risk.day_start_date.isoformat() if self.risk.day_start_date else ""
        if self.login is not None and not self.risk.trading_halted:
            other = self.day_state.halted_reason(self.login, today, scope)
            if other:
                self.risk.trading_halted = True
                self.risk.halt_reason = f"detenido por otro bot de la cuenta: {other}"

        if scope == "bots":
            try:
                realized, floating = mt5c.get_bots_pnl_since(
                    self.portfolio_magics(), int(self.day_start_server_time or 0))
            except Exception:  # noqa: BLE001
                logger.exception("No se pudo calcular el resultado de los bots")
                return not self.risk.trading_halted
            self.bots_pnl = (realized, floating)
            equity = float(self.risk.day_start_balance or 0.0) + realized + floating
            ok = self.risk.check_daily_loss(equity=equity)
        else:
            self.bots_pnl = None
            ok = self.risk.check_daily_loss()

        if not ok and self.login is not None and self.risk.halt_reason and \
                not self.risk.halt_reason.startswith("detenido por otro bot"):
            self.day_state.mark_halted(self.login, today, scope,
                                       f"[{self.config['symbol']}] {self.risk.halt_reason}")
        return ok

    def ensure_connection(self) -> bool:
        """Reconecta si el terminal se cerró o perdió conexión."""
        if mt5c.is_connected():
            return True
        logger.warning("Conexión MT5 perdida; reintentando...")
        return mt5c.connect_mt5_with_retry(
            max_retries=5, wait_seconds=5,
            require_demo=bool(self.config["require_demo"]),
            terminal_path=self.config.get("terminal_path") or None,
        )

    # ----------------------------------------------------------- mensajes
    def heartbeat(self, message: str) -> None:
        """Mensaje de espera periódico (cada ``heartbeat_seconds``; 0 = desactivado)."""
        self.last_message = message
        every = float(self.config.get("heartbeat_seconds", 60))
        if every <= 0:
            return
        now = time.monotonic()
        if now - self.last_heartbeat >= every - 1:
            self.last_heartbeat = now
            logger.info("En espera | %s", message)

    def _day_text(self) -> str:
        """Resultado del día en % frente al límite de pérdida diaria."""
        st = self.risk.status()
        pnl = -(st["daily_loss_percent"] or 0.0) + 0.0  # + 0.0 evita mostrar "-0.00"
        scope = "bots" if self.loss_scope == "bots" else "cuenta"
        return f"día ({scope}) {pnl:+.2f}% (límite -{st['max_daily_loss_percent']:g}%)"

    def _next_bar_text(self, bar_time: Any, tick: Any) -> str:
        """Hora de cierre de la vela en curso y minutos que faltan (hora del servidor)."""
        try:
            tf = timedelta(seconds=mt5c.timeframe_seconds(self.config["timeframe"]))
            next_close = bar_time.to_pydatetime() + 2 * tf  # bar_time = apertura de la última cerrada
            if tick is None:
                return f"próxima vela {next_close:%H:%M}"
            # tick.time viene en hora del servidor codificada como epoch
            server_now = datetime.fromtimestamp(int(tick.time), tz=timezone.utc).replace(tzinfo=None)
            remaining = (next_close - server_now).total_seconds()
            if remaining < -tf.total_seconds():
                return "sin cotizaciones recientes (¿mercado cerrado?)"
            minutes = max(0, int(remaining // 60))
            return f"próxima vela {next_close:%H:%M} (en {minutes} min)"
        except Exception:  # noqa: BLE001 - es solo informativo
            return "próxima vela: desconocida"

    @staticmethod
    def _waiting_text(sig: dict[str, Any]) -> str:
        """Explica qué condición falta para la próxima señal (depende de la estrategia)."""
        return str(sig.get("waiting") or "esperando señal")

    @staticmethod
    def _positions_text(positions: list[Any]) -> str:
        """Resumen de las posiciones abiertas del bot."""
        parts = []
        for pos in positions:
            side = "BUY" if getattr(pos, "type", 0) == 0 else "SELL"
            parts.append(
                f"{side} {pos.volume:g} @ {getattr(pos, 'price_open', 0.0):.6g} "
                f"SL {getattr(pos, 'sl', 0.0):.6g} TP {getattr(pos, 'tp', 0.0):.6g} "
                f"P&L {getattr(pos, 'profit', 0.0):+.2f}"
            )
        return "posición abierta: " + "; ".join(parts)

    # ------------------------------------------------------------- iteración
    def step(self) -> None:
        """Una iteración del bucle principal."""
        self.reload_config_if_changed()
        if not self.ensure_connection():
            self.heartbeat("sin conexión con MT5; se reintentará en el próximo ciclo")
            return
        self.check_new_day()

        cfg = self.config
        symbol, magic = cfg["symbol"], int(cfg["magic"])

        if not self.check_daily_loss():
            if not self.halt_logged:
                logger.error("Trading detenido hasta el próximo día: %s",
                             self.risk.halt_reason or self.risk.last_rejection)
                self.halt_logged = True
            self.heartbeat(f"trading detenido por pérdida diaria | {self._day_text()}")
            return

        if self.params is None:
            self.heartbeat("sin parámetros válidos: el bot no abre operaciones")
            return

        spread_ok = self.risk.check_spread()

        bars_needed = warmup_bars(self.params) + int(cfg["atr_period"]) + 50
        df = mt5c.get_rates(symbol, cfg["timeframe"], bars_needed + 1)
        if df.empty or len(df) < bars_needed:
            logger.warning("Velas insuficientes para calcular señal (%d).", len(df))
            return
        df = df.iloc[:-1]  # sólo velas cerradas

        sig = compute_signal(df, self.params, int(cfg["atr_period"]))
        bar_time = sig["bar_time"]
        positions = mt5c.get_positions(symbol, magic)
        self.last_sig = sig
        self.last_positions = positions
        signal_txt = {1: "COMPRA", -1: "VENTA"}.get(sig["signal"], "ninguna")

        if bar_time != self.logged_bar_time:
            self.logged_bar_time = bar_time
            logger.info(
                "Vela %s cerrada (hora servidor) | close %.6g | %s | ATR %.6g | señal: %s | "
                "posiciones: %d | %s",
                f"{bar_time:%Y-%m-%d %H:%M}" if hasattr(bar_time, "strftime") else bar_time,
                sig["close"], sig["indicators"], sig["atr"], signal_txt, len(positions),
                self._day_text(),
            )
            self.last_heartbeat = time.monotonic()  # el mensaje de vela cuenta como latido

        if bar_time != self.last_bar_time:
            if sig["signal"] == 0:
                self.last_bar_time = bar_time
            elif positions:
                self.last_bar_time = bar_time
                logger.info("Señal de %s ignorada: ya hay %d posición(es) con magic %d.",
                            signal_txt, len(positions), magic)
            elif not spread_ok:
                # No se marca la vela: se reintenta en el próximo ciclo si el spread baja.
                self.heartbeat(f"señal de {signal_txt} pendiente: spread "
                               f"{self.risk.last_spread:.6g} > máximo {self.risk.max_spread:.6g}")
                return
            else:
                self.last_bar_time = bar_time
                self.open_trade(sig)
                self.last_heartbeat = time.monotonic()
                return

        tick = mt5c.get_tick(symbol)
        self.last_tick = tick
        price_txt = (f"{symbol} {tick.bid:.6g}/{tick.ask:.6g} spread {tick.ask - tick.bid:.6g}"
                     if tick is not None else f"{symbol} sin cotización")
        if not spread_ok:
            state = f"spread alto (máx {self.risk.max_spread:.6g}): no se abrirán operaciones"
        elif positions:
            state = self._positions_text(positions)
        else:
            state = self._waiting_text(sig)
        self.heartbeat(f"{price_txt} | {self._next_bar_text(bar_time, tick)} | {state} | "
                       f"{self._day_text()}")

    def open_trade(self, sig: dict[str, Any]) -> None:
        """Calcula SL/TP con ATR, dimensiona el lote y envía la orden."""
        cfg = self.config
        symbol = cfg["symbol"]
        side = "buy" if sig["signal"] == 1 else "sell"
        atr_value = sig["atr"]
        if not atr_value or atr_value != atr_value or atr_value <= 0:
            logger.warning("SKIP: ATR inválido (%s).", atr_value)
            return

        params = self.params or {}
        sl_dist = float(params.get("sl_mult", cfg["sl_atr_mult"])) * atr_value
        tp_dist = float(params.get("tp_mult", cfg["tp_atr_mult"])) * atr_value
        min_stop = mt5c.get_min_stop_distance(symbol)
        if sl_dist <= min_stop or tp_dist <= min_stop:
            logger.warning(
                "SKIP: SL/TP (%.5f/%.5f) por debajo del mínimo del bróker (%.5f).",
                sl_dist, tp_dist, min_stop,
            )
            return

        if not self.portfolio_allows_new_trade():
            return

        lot = self.risk.calculate_lot(sl_dist)
        if lot is None:
            logger.warning("SKIP: el RiskManager rechazó la operación: %s", self.risk.last_rejection)
            return

        tick = mt5c.get_tick(symbol)
        if tick is None:
            logger.warning("SKIP: sin tick para %s.", symbol)
            return
        price = tick.ask if side == "buy" else tick.bid
        sl = price - sl_dist if side == "buy" else price + sl_dist
        tp = price + tp_dist if side == "buy" else price - tp_dist

        if self.dry_run:
            logger.info(
                "[DRY-RUN] %s %.2f %s @ %.5f | SL %.5f | TP %.5f (no se envía)",
                side.upper(), lot, symbol, price, sl, tp,
            )
            return

        mt5c.send_market_order(
            symbol, side, lot, sl, tp, int(cfg["magic"]),
            deviation=int(cfg["deviation"]), comment="MT5Bot evo",
        )

    def portfolio_magics(self) -> set[int]:
        """Magic numbers de TODOS los perfiles de la carpeta (para el límite global)."""
        magics = {int(self.config["magic"])}
        base = self.config_path.parent
        for name in list_profiles(base):
            cfg = read_json(config_path_for(name, base)) or {}
            if isinstance(cfg.get("magic"), int):
                magics.add(cfg["magic"])
        return magics

    def portfolio_allows_new_trade(self) -> bool:
        """Respeta 'max_open_positions_total' sumando las posiciones de todos los perfiles."""
        limit = int(self.config.get("max_open_positions_total", 0))
        if limit <= 0:
            return True
        open_total = mt5c.count_positions_by_magics(self.portfolio_magics())
        if open_total >= limit:
            logger.warning(
                "SKIP: límite global de posiciones alcanzado (%d abiertas entre todos los "
                "perfiles, máximo %d).", open_total, limit,
            )
            return False
        return True

    def run(self) -> None:
        """Bucle principal: una iteración cada ``loop_seconds``."""
        logger.info(
            "Bucle principal iniciado (%s %s, revisión cada %ss)%s. Ctrl+C para detener.",
            self.config["symbol"], self.config["timeframe"], self.config["loop_seconds"],
            " [DRY-RUN]" if self.dry_run else "",
        )
        while True:
            started = time.monotonic()
            self.step()
            self.publish_status()
            # Espera en tramos de 1 s para reaccionar rápido a una parada solicitada.
            deadline = started + max(1.0, float(self.config["loop_seconds"]))
            while time.monotonic() < deadline:
                if self.stop_requested():
                    logger.info("Parada solicitada desde el Centro de control.")
                    return
                time.sleep(min(1.0, max(0.05, deadline - time.monotonic())))

    def stop_requested(self) -> bool:
        """True si existe el archivo de parada del perfil (lo crea el Centro de control)."""
        return self.paths.stop_flag.exists()

    def publish_status(self) -> None:
        """Escribe el estado en vivo para el Centro de control (nunca lanza excepciones)."""
        try:
            if self.risk.trading_halted:
                phase = "detenido_perdida"
            elif self.params is None:
                phase = "sin_parametros"
            else:
                phase = "operando"
            account = self.risk.provider.get_account() if self.risk.provider else None
            now = time.monotonic()
            if now - self._deals_time >= DEALS_REFRESH_SECONDS or not self._deals_time:
                self._deals_time = now
                try:
                    self._deals = mt5c.get_bot_deals(int(self.config["magic"]))
                except Exception:  # noqa: BLE001 - informativo
                    logger.debug("No se pudo leer el historial de operaciones", exc_info=True)
            sig = self.last_sig or {}
            tick = self.last_tick
            st = self.risk.status()
            # Se conservan en el estado final (al detenerse) para que la ventana los siga mostrando.
            if account is not None:
                self.status.persistent["account"] = {"balance": account.balance, "equity": account.equity}
            self.status.persistent["deals"] = self._deals
            self.status.write(
                phase,
                self.last_message,
                symbol=self.config["symbol"],
                timeframe=self.config["timeframe"],
                magic=int(self.config["magic"]),
                params=self.params,
                day_pnl_percent=-(st["daily_loss_percent"] or 0.0) + 0.0,
                max_daily_loss_percent=st["max_daily_loss_percent"],
                daily_loss_scope=self.loss_scope,
                bots_pnl=({"realized": self.bots_pnl[0], "floating": self.bots_pnl[1]}
                          if self.bots_pnl else None),
                halt_reason=st["halt_reason"],
                price={"bid": tick.bid, "ask": tick.ask} if tick is not None else None,
                spread=st["last_spread"],
                max_spread=st["max_spread"],
                last_bar={
                    "time": str(sig.get("bar_time", "")),
                    "close": sig.get("close"), "fast_ma": sig.get("fast_ma"),
                    "indicators": sig.get("indicators"), "strategy": sig.get("strategy"),
                    "slow_ma": sig.get("slow_ma"), "rsi": sig.get("rsi"),
                    "atr": sig.get("atr"), "signal": sig.get("signal"),
                } if sig else None,
                positions=[{
                    "ticket": getattr(p, "ticket", None),
                    "side": "BUY" if getattr(p, "type", 0) == 0 else "SELL",
                    "volume": getattr(p, "volume", None),
                    "price_open": getattr(p, "price_open", None),
                    "sl": getattr(p, "sl", None), "tp": getattr(p, "tp", None),
                    "profit": getattr(p, "profit", None),
                } for p in self.last_positions],
            )
        except Exception:  # noqa: BLE001 - el estado nunca debe tumbar el bot
            logger.debug("No se pudo publicar el estado", exc_info=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Argumentos de línea de comandos."""
    parser = argparse.ArgumentParser(description="Bot de trading CFD para MT5 (educativo)")
    parser.add_argument("--dry-run", action="store_true", help="no envía órdenes reales")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH, help="ruta a risk_config.json")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Punto de entrada. Devuelve el código de salida."""
    args = parse_args(argv)
    paths = profile_paths(args.config)
    setup_logging(paths.log_file)
    status = StatusFile(paths, args.dry_run)
    try:
        paths.stop_flag.unlink(missing_ok=True)  # descartar una parada pendiente antigua
    except OSError:
        pass
    logger.info("=" * 70)
    logger.info("Iniciando MT5 Bot (perfil '%s')%s", paths.name, " [DRY-RUN]" if args.dry_run else "")
    status.write("iniciando", "cargando configuración")

    # 1) Configuración
    try:
        config = load_config(args.config)
    except (FileNotFoundError, ValueError) as exc:
        logger.error("%s", exc)
        status.write("error", str(exc), exit_code=EXIT_CONFIG)
        return EXIT_CONFIG
    logger.info(
        "Configuración: %s %s | estrategia %s | riesgo %g%% | historial %d velas | "
        "min_trades %d | tramos %d | %s",
        config["symbol"], config["timeframe"], config["strategy"], config["risk_percent"],
        config["history_bars"],
        config["min_trades"], config["fitness_folds"], args.config.name,
    )

    bot: TradingBot | None = None
    exit_code = EXIT_ERROR
    final_message = ""
    try:
        status.write("conectando", "conectando con MetaTrader 5",
                     symbol=config["symbol"], timeframe=config["timeframe"])
        # 2) Conexión (SystemExit si la cuenta no es demo y se exige demo)
        if not mt5c.connect_mt5_with_retry(
            require_demo=bool(config["require_demo"]),
            terminal_path=config.get("terminal_path") or None,
        ):
            exit_code, final_message = EXIT_CONNECTION, "no se pudo conectar con MetaTrader 5"
            return exit_code
        if not mt5c.ensure_symbol(config["symbol"]):
            exit_code, final_message = EXIT_CONFIG, f"símbolo '{config['symbol']}' no encontrado"
            return exit_code

        # 3) RiskManager + reset diario
        bot = TradingBot(config, args.config, dry_run=args.dry_run, status=status)
        if not bot.start_day():
            exit_code, final_message = EXIT_CONNECTION, "no se pudo leer el balance de la cuenta"
            return exit_code

        # 4-5) Evolución + validación walk-forward
        status.write("evolucionando", "buscando y validando parámetros")
        if not bot.evolve():
            reason = (bot.evolution or {}).get("reason", "sin datos")
            logger.error("Abortando: la validación walk-forward no fue superada. Ver %s",
                         paths.evolved.name)
            exit_code, final_message = EXIT_VALIDATION, f"parámetros descartados: {reason}"
            return exit_code

        # 6) Bucle principal (termina con Ctrl+C o con la parada del Centro de control)
        bot.run()
        exit_code, final_message = EXIT_OK, "detenido por el usuario"
        return exit_code

    except KeyboardInterrupt:
        logger.info("Ctrl+C recibido: deteniendo el bot de forma limpia.")
        exit_code, final_message = EXIT_OK, "detenido con Ctrl+C"
        return exit_code
    except SystemExit as exc:
        logger.error("%s", exc)
        exit_code, final_message = EXIT_CONFIG, str(exc)
        return exit_code
    except Exception as exc:
        logger.exception("Error no controlado en el bot (traceback completo arriba).")
        cfg = bot.config if bot is not None else config
        if cfg.get("close_positions_on_error"):
            try:
                closed = mt5c.close_all_positions(cfg["symbol"], int(cfg["magic"]),
                                                  int(cfg["deviation"]))
                logger.warning("Fail-safe: %d posición(es) cerrada(s).", closed)
            except Exception:
                logger.exception("Fail-safe: error cerrando posiciones.")
        else:
            logger.warning("Fail-safe: las posiciones abiertas se mantienen (con su SL/TP).")
        exit_code, final_message = EXIT_ERROR, f"error inesperado: {exc}"
        return exit_code
    finally:
        mt5c.safe_shutdown()
        try:
            paths.stop_flag.unlink(missing_ok=True)
        except OSError:
            pass
        phase = {EXIT_OK: "finalizado", EXIT_VALIDATION: "descartado"}.get(exit_code, "error")
        status.write(phase, final_message, exit_code=exit_code)
        logger.info("Bot finalizado.")


if __name__ == "__main__":
    sys.exit(main())
