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
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import mt5_connector as mt5c
from bot_common import (
    CONFIG_PATH, ProfilePaths, config_path_for, global_config_path, hour_allowed, list_profiles,
    load_config, now_iso, profile_paths, read_json, setup_logging, write_json_atomic,
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


class Notifier:
    """Avisos opcionales por Telegram (si hay token y chat_id). Nunca bloquea ni falla."""

    def __init__(self, get_config: Any, prefix: Any) -> None:
        self._get_config = get_config
        self._prefix = prefix

    def send(self, text: str) -> None:
        """Envía el aviso en segundo plano."""
        cfg = self._get_config()
        token, chat = str(cfg.get("telegram_token") or ""), str(cfg.get("telegram_chat_id") or "")
        if not token or not chat:
            return
        message = f"{self._prefix()} {text}"

        def _post() -> None:
            try:
                data = urllib.parse.urlencode({"chat_id": chat, "text": message}).encode()
                req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
                with urllib.request.urlopen(req, timeout=10):  # noqa: S310 - URL fija de Telegram
                    pass
            except Exception as exc:  # noqa: BLE001
                logger.warning("No se pudo enviar el aviso de Telegram: %s", exc)

        threading.Thread(target=_post, daemon=True).start()


class TradeRegistry:
    """Datos de cada posición abierta por el bot (ATR de entrada y mejor precio) en disco."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _load(self) -> dict[str, Any]:
        data = read_json(self.path)
        return data if isinstance(data, dict) else {}

    def get(self, ticket: int) -> dict[str, Any]:
        return dict(self._load().get(str(ticket)) or {})

    def set(self, ticket: int, record: dict[str, Any]) -> None:
        data = self._load()
        data[str(ticket)] = record
        write_json_atomic(self.path, data)

    def remove(self, ticket: int) -> None:
        data = self._load()
        if data.pop(str(ticket), None) is not None:
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
        :param dry_run: si True, fuerza la simulación (no se envían órdenes) aunque
                        el perfil tenga ``simulation = false``.
        :param status: archivo de estado para el Centro de control (opcional).
        """
        self.config = config
        self.config_path = config_path
        self.paths = profile_paths(config_path)
        self.forced_dry_run = dry_run
        self.status = status or StatusFile(self.paths, self.dry_run)
        self.config_mtime = self._config_stamp()
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
        self.registry = TradeRegistry(config_path.parent / "logs" / f"trades_{self.paths.name}.json")
        self.notifier = Notifier(lambda: self.config, self._notify_prefix)
        self.known_tickets: set[int] | None = None
        self.paused_reason = ""
        self.params_since_ts: int = 0          # hora servidor desde la que rigen los parámetros
        self.last_evolution_mono: float = time.monotonic()
        self.last_evolution_iso: str = ""
        self.halt_notified = False

    def _notify_prefix(self) -> str:
        sim = " SIM" if self.dry_run else ""
        return f"[MT5Bot {self.paths.name} {self.config['symbol']} {self.config['timeframe']}{sim}]"

    @property
    def dry_run(self) -> bool:
        """Simulación: forzada por --dry-run o por ``simulation`` del perfil (en caliente)."""
        return self.forced_dry_run or bool(self.config.get("simulation", False))

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
        self.last_evolution_mono = time.monotonic()
        self.last_evolution_iso = now_iso()
        self.params_since_ts = mt5c.get_server_time(cfg["symbol"]) or int(time.time())
        self.paused_reason = ""  # parámetros nuevos: se vuelve a evaluar el rendimiento desde cero
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
    def _config_stamp(self) -> tuple[float, float]:
        """Fechas de modificación del perfil y de la configuración general."""
        stamps = []
        for path in (self.config_path, global_config_path(self.config_path)):
            try:
                stamps.append(path.stat().st_mtime)
            except OSError:
                stamps.append(0.0)
        return stamps[0], stamps[1]

    def reload_config_if_changed(self) -> None:
        """Recarga la configuración (perfil o general) si cambió en disco, sin reiniciar."""
        if not self.config_path.exists():
            logger.warning("%s desapareció; se mantiene la configuración actual.", self.config_path.name)
            return
        stamp = self._config_stamp()
        if stamp == self.config_mtime:
            return
        self.config_mtime = stamp

        try:
            new_cfg = load_config(self.config_path)
        except (ValueError, FileNotFoundError) as exc:
            logger.error("Recarga ignorada, configuración inválida: %s", exc)
            return

        old = self.config
        self.config = new_cfg
        self.risk.update_config(new_cfg)
        self.status.dry_run = self.dry_run
        if bool(old.get("simulation")) != bool(new_cfg.get("simulation")) and not self.forced_dry_run:
            logger.warning("Modo cambiado en caliente: %s",
                           "SIMULACIÓN (no envía órdenes)" if self.dry_run else "ENVÍO DE ÓRDENES ACTIVADO")
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

        positions = mt5c.get_positions(symbol, magic)
        self.last_positions = positions
        self.track_closed_positions(positions)

        halted = not self.check_daily_loss()
        if halted and not self.halt_logged:
            logger.error("Trading detenido hasta el próximo día: %s",
                         self.risk.halt_reason or self.risk.last_rejection)
            self.halt_logged = True
            self.notifier.send(f"TRADING DETENIDO por pérdida diaria: {self.risk.halt_reason}")

        self.maybe_reevolve(positions)
        if self.params is None:
            hours = int(cfg.get("reevolve_hours", 0) or 0)
            retry = f"; se reintentará en la re-evolución periódica ({hours} h)" if hours else ""
            self.heartbeat(f"sin parámetros válidos: el bot no abre operaciones{retry}")
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
        self.last_sig = sig
        tick = mt5c.get_tick(symbol)
        self.last_tick = tick
        if positions:
            self.manage_positions(positions, sig, tick)
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

        if halted:
            self.heartbeat(f"trading detenido por pérdida diaria | {self._day_text()}")
            return
        if self.paused_reason:
            self.heartbeat(f"PAUSADO por rendimiento ({self.paused_reason}) | {self._day_text()}")
            return
        session_open = self.session_open_for(bar_time)

        if bar_time != self.last_bar_time:
            if sig["signal"] == 0:
                self.last_bar_time = bar_time
            elif positions:
                self.last_bar_time = bar_time
                logger.info("Señal de %s ignorada: ya hay %d posición(es) con magic %d.",
                            signal_txt, len(positions), magic)
            elif not session_open:
                self.last_bar_time = bar_time
                logger.info("Señal de %s ignorada: fuera del horario de operación (%s, hora servidor).",
                            signal_txt, cfg.get("trading_hours"))
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

        price_txt = (f"{symbol} {tick.bid:.6g}/{tick.ask:.6g} spread {tick.ask - tick.bid:.6g}"
                     if tick is not None else f"{symbol} sin cotización")
        if not spread_ok:
            state = f"spread alto (máx {self.risk.max_spread:.6g}): no se abrirán operaciones"
        elif positions:
            state = self._positions_text(positions)
        elif not session_open:
            state = f"fuera del horario de operación ({cfg.get('trading_hours')} h servidor)"
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
            self.notifier.send(f"Señal {side.upper()} {lot:.2f} @ {price:.6g} (simulación, no se envía)")
            return

        result = mt5c.send_market_order(
            symbol, side, lot, sl, tp, int(cfg["magic"]),
            deviation=int(cfg["deviation"]), comment="MT5Bot evo",
        )
        if result is not None:
            ticket = int(getattr(result, "order", 0) or 0)
            if ticket:
                self.registry.set(ticket, {"atr": atr_value, "entry": price, "best": price,
                                           "side": 1 if side == "buy" else -1, "opened": now_iso()})
            self.notifier.send(f"ORDEN {side.upper()} {lot:.2f} @ {price:.6g} | SL {sl:.6g} | TP {tp:.6g}")

    # ------------------------------------------------------------ horario
    def session_open_for(self, bar_time: Any) -> bool:
        """¿La vela en curso (la siguiente a ``bar_time``) está dentro del horario?"""
        spec = str(self.config.get("trading_hours") or "")
        if not spec.strip() or not hasattr(bar_time, "hour"):
            return True
        nxt = bar_time + timedelta(seconds=mt5c.timeframe_seconds(self.config["timeframe"]))
        return hour_allowed(int(nxt.hour), spec)

    # ------------------------------------------------- gestión de posiciones
    def manage_positions(self, positions: list[Any], sig: dict[str, Any], tick: Any) -> None:
        """Break-even, trailing y cierre por tiempo sobre las posiciones abiertas del bot."""
        cfg = self.config
        mode = str(cfg.get("exit_management", "ninguna"))
        max_bars = int(cfg.get("max_bars_in_trade", 0) or 0)
        if mode == "ninguna" and not max_bars:
            return
        params = self.params or {}
        tf_sec = mt5c.timeframe_seconds(cfg["timeframe"])
        server_now = int(tick.time) if tick is not None and getattr(tick, "time", 0) else None
        spec = mt5c.get_symbol_spec(cfg["symbol"])
        step = spec.tick_size if spec else 0.0
        min_stop = mt5c.get_min_stop_distance(cfg["symbol"])
        for pos in positions:
            side = 1 if getattr(pos, "type", 0) == 0 else -1
            rec = self.registry.get(pos.ticket)
            atr_e = float(rec.get("atr") or sig.get("atr") or 0.0)
            opened = int(getattr(pos, "time", 0) or 0)
            if max_bars and server_now and opened and (server_now - opened) // tf_sec >= max_bars:
                if self.dry_run:
                    logger.info("[DRY-RUN] cerraría #%s por tiempo (%d velas)", pos.ticket, max_bars)
                else:
                    logger.info("Cierre por tiempo de #%s (%d velas abiertas).", pos.ticket, max_bars)
                    mt5c.close_position(pos, int(cfg["deviation"]))
                continue
            if tick is None or atr_e <= 0 or mode == "ninguna":
                continue
            price = tick.bid if side == 1 else tick.ask
            entry = float(pos.price_open)
            best = float(rec.get("best", entry))
            best = max(best, price) if side == 1 else min(best, price)
            if best != rec.get("best"):
                rec.setdefault("atr", atr_e)
                rec["best"] = best
                self.registry.set(pos.ticket, rec)
            target = None
            if mode == "breakeven":
                be = float(params.get("be_mult", cfg["be_atr_mult"]))
                if (best - entry) * side >= be * atr_e:
                    target = entry
            elif mode == "trailing":
                trail = float(params.get("trail_mult", cfg["trail_atr_mult"]))
                target = best - side * trail * atr_e
            if target is None:
                continue
            current = float(getattr(pos, "sl", 0.0) or 0.0)
            improves = current == 0.0 or (target - current) * side > max(step, 1e-12)
            room = (price - target) * side >= max(min_stop, step)
            if improves and room:
                if self.dry_run:
                    logger.info("[DRY-RUN] movería el SL de #%s a %.6g (%s)", pos.ticket, target, mode)
                else:
                    mt5c.modify_position_sl(pos, target)

    def track_closed_positions(self, positions: list[Any]) -> None:
        """Detecta posiciones cerradas desde la última revisión (por SL, TP o a mano)."""
        current = {int(p.ticket) for p in positions}
        if self.known_tickets is None:
            self.known_tickets = current
            return
        closed = self.known_tickets - current
        self.known_tickets = current
        for ticket in closed:
            result = mt5c.get_position_result(ticket)
            txt = f"{result:+.2f}" if result is not None else "desconocido"
            logger.info("Posición cerrada #%s (%s): resultado %s", ticket, self.config["symbol"], txt)
            self.notifier.send(f"Posición cerrada #{ticket}: resultado {txt}")
            self.registry.remove(ticket)
        if closed:
            self.evaluate_performance()

    def evaluate_performance(self) -> None:
        """Pausa el símbolo si su rendimiento REAL con los parámetros actuales es malo."""
        threshold = float(self.config.get("auto_disable_pf", 0) or 0)
        needed = int(self.config.get("auto_disable_min_trades", 0) or 0)
        if threshold <= 0 or needed <= 0:
            return
        try:
            trades = mt5c.get_bot_deals(int(self.config["magic"]), days=120, limit=None)
        except Exception:  # noqa: BLE001
            logger.exception("No se pudo evaluar el rendimiento real")
            return
        recent = [t for t in trades if int(t.get("open_ts", 0)) >= self.params_since_ts]
        if len(recent) < needed:
            return
        sample = recent[: max(needed, 20)]
        gains = sum(t["profit"] for t in sample if t["profit"] > 0)
        losses = -sum(t["profit"] for t in sample if t["profit"] <= 0)
        pf = gains / losses if losses > 0 else float("inf")
        if pf < threshold and not self.paused_reason:
            self.paused_reason = (f"rendimiento real bajo: PF {pf:.2f} en las últimas {len(sample)} "
                                  f"operaciones (mínimo {threshold:g})")
            logger.error("PAUSADO por rendimiento: %s. No abrirá nuevas operaciones hasta la "
                         "próxima re-evolución.", self.paused_reason)
            self.notifier.send(f"PAUSADO por rendimiento: {self.paused_reason}")

    def maybe_reevolve(self, positions: list[Any]) -> None:
        """Re-evolución periódica (cada ``reevolve_hours``) cuando no hay posición abierta."""
        hours = int(self.config.get("reevolve_hours", 0) or 0)
        if hours <= 0 or positions:
            return
        if time.monotonic() - self.last_evolution_mono < hours * 3600:
            return
        logger.info("Re-evolución periódica (cada %d h)…", hours)
        ok = self.evolve()
        self.notifier.send("Re-evolución: parámetros " + ("ACEPTADOS" if ok else
                           f"DESCARTADOS ({(self.evolution or {}).get('reason', '')})"))

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
            elif self.paused_reason:
                phase = "pausado_rendimiento"
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
                paused_reason=self.paused_reason,
                last_evolution=self.last_evolution_iso,
                dry_run=self.dry_run,
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
    logger.info("Iniciando MT5 Bot (perfil '%s')", paths.name)
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
    simulated = args.dry_run or bool(config.get("simulation"))
    status.dry_run = simulated
    logger.info("Modo: %s", "SIMULACIÓN (calcula todo pero NO envía órdenes) [DRY-RUN]" if simulated
                else "ENVÍO DE ÓRDENES ACTIVADO")

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
            bot.notifier.send(f"Parámetros DESCARTADOS, el bot no opera: {reason}")
            logger.error("Abortando: la validación walk-forward no fue superada. Ver %s",
                         paths.evolved.name)
            exit_code, final_message = EXIT_VALIDATION, f"parámetros descartados: {reason}"
            return exit_code

        ev = bot.evolution or {}
        bot.notifier.send(
            f"Bot iniciado ({'simulación' if bot.dry_run else 'ÓRDENES REALES'}). Parámetros aceptados: "
            f"PF validación {(ev.get('out_of_sample') or {}).get('profit_factor', 0):.2f}")
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
        if bot is not None:
            bot.notifier.send(f"ERROR: el bot se detuvo ({exc})")
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
