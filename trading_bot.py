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
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import mt5_connector as mt5c
from bot_common import CONFIG_PATH, EVOLVED_PARAMS_PATH, load_config, setup_logging
from risk_manager import RiskManager
from strategy_evolver import compute_signal, evolve_parameters

logger = logging.getLogger("trading_bot")

EXIT_OK, EXIT_ERROR, EXIT_CONFIG, EXIT_CONNECTION, EXIT_VALIDATION = 0, 1, 2, 3, 4


class TradingBot:
    """Bot de trading: mantiene estado, parámetros evolucionados y el bucle principal."""

    def __init__(self, config: dict[str, Any], config_path: Path, dry_run: bool = False) -> None:
        """
        :param config: configuración ya validada.
        :param config_path: ruta de risk_config.json (para recarga en caliente).
        :param dry_run: si True, no se envían órdenes reales.
        """
        self.config = config
        self.config_path = config_path
        self.dry_run = dry_run
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
        """Guarda el resultado de la evolución en evolved_params.json (auditoría)."""
        payload = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "symbol": self.config["symbol"],
            "timeframe": self.config["timeframe"],
            **result,
        }
        try:
            EVOLVED_PARAMS_PATH.write_text(
                json.dumps(payload, indent=4, ensure_ascii=False, default=str), encoding="utf-8"
            )
        except OSError:
            logger.exception("No se pudo guardar %s", EVOLVED_PARAMS_PATH)

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

        structural = ("symbol", "timeframe", "magic")
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
    def check_new_day(self) -> None:
        """Llama a reset_daily() al cambiar de día (00:00 hora local)."""
        today = datetime.now().date()
        if self.risk.day_start_date != today:
            logger.info("Nuevo día de trading: %s", today.isoformat())
            self.risk.reset_daily(today=today)
            self.halt_logged = False

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
        return f"día {pnl:+.2f}% (límite -{st['max_daily_loss_percent']:g}%)"

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

    def _waiting_text(self, sig: dict[str, Any]) -> str:
        """Explica qué condición falta para la próxima señal."""
        p = self.params or {}
        fast, slow, rsi_now = sig["fast_ma"], sig["slow_ma"], sig["rsi"]
        if fast > slow:
            return (f"rápida {fast:.6g} > lenta {slow:.6g}: esperando cruce a la BAJA "
                    f"con RSI < {float(p['rsi_sell']):.1f} (RSI {rsi_now:.1f})")
        return (f"rápida {fast:.6g} < lenta {slow:.6g}: esperando cruce al ALZA "
                f"con RSI > {float(p['rsi_buy']):.1f} (RSI {rsi_now:.1f})")

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

        if not self.risk.check_daily_loss():
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

        bars_needed = int(self.params["slow"]) + int(cfg["atr_period"]) + 50
        df = mt5c.get_rates(symbol, cfg["timeframe"], bars_needed + 1)
        if df.empty or len(df) < bars_needed:
            logger.warning("Velas insuficientes para calcular señal (%d).", len(df))
            return
        df = df.iloc[:-1]  # sólo velas cerradas

        sig = compute_signal(df, self.params, int(cfg["atr_period"]))
        bar_time = sig["bar_time"]
        positions = mt5c.get_positions(symbol, magic)
        signal_txt = {1: "COMPRA", -1: "VENTA"}.get(sig["signal"], "ninguna")

        if bar_time != self.logged_bar_time:
            self.logged_bar_time = bar_time
            logger.info(
                "Vela %s cerrada (hora servidor) | close %.6g | rápida %.6g lenta %.6g | "
                "RSI %.1f | ATR %.6g | señal: %s | posiciones: %d | %s",
                f"{bar_time:%Y-%m-%d %H:%M}" if hasattr(bar_time, "strftime") else bar_time,
                sig["close"], sig["fast_ma"], sig["slow_ma"], sig["rsi"], sig["atr"],
                signal_txt, len(positions), self._day_text(),
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

        sl_dist = float(cfg["sl_atr_mult"]) * atr_value
        tp_dist = float(cfg["tp_atr_mult"]) * atr_value
        min_stop = mt5c.get_min_stop_distance(symbol)
        if sl_dist <= min_stop or tp_dist <= min_stop:
            logger.warning(
                "SKIP: SL/TP (%.5f/%.5f) por debajo del mínimo del bróker (%.5f).",
                sl_dist, tp_dist, min_stop,
            )
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
            elapsed = time.monotonic() - started
            time.sleep(max(1.0, float(self.config["loop_seconds"]) - elapsed))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Argumentos de línea de comandos."""
    parser = argparse.ArgumentParser(description="Bot de trading CFD para MT5 (educativo)")
    parser.add_argument("--dry-run", action="store_true", help="no envía órdenes reales")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH, help="ruta a risk_config.json")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Punto de entrada. Devuelve el código de salida."""
    args = parse_args(argv)
    setup_logging("trading_bot.log")
    logger.info("=" * 70)
    logger.info("Iniciando MT5 Bot%s", " [DRY-RUN]" if args.dry_run else "")

    # 1) Configuración
    try:
        config = load_config(args.config)
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        return EXIT_CONFIG
    except ValueError as exc:
        logger.error("%s", exc)
        return EXIT_CONFIG
    logger.info(
        "Configuración: %s %s | riesgo %g%% | historial %d velas | min_trades %d | tramos %d | %s",
        config["symbol"], config["timeframe"], config["risk_percent"], config["history_bars"],
        config["min_trades"], config["fitness_folds"], args.config.name,
    )

    bot: TradingBot | None = None
    try:
        # 2) Conexión (SystemExit si la cuenta no es demo y se exige demo)
        if not mt5c.connect_mt5_with_retry(
            require_demo=bool(config["require_demo"]),
            terminal_path=config.get("terminal_path") or None,
        ):
            return EXIT_CONNECTION
        if not mt5c.ensure_symbol(config["symbol"]):
            return EXIT_CONFIG

        # 3) RiskManager + reset diario
        bot = TradingBot(config, args.config, dry_run=args.dry_run)
        if not bot.risk.reset_daily():
            return EXIT_CONNECTION

        # 4-5) Evolución + validación walk-forward
        if not bot.evolve():
            logger.error("Abortando: la validación walk-forward no fue superada. Ver %s",
                         EVOLVED_PARAMS_PATH.name)
            return EXIT_VALIDATION

        # 6) Bucle principal
        bot.run()
        return EXIT_OK  # pragma: no cover - run() es infinito

    except KeyboardInterrupt:
        logger.info("Ctrl+C recibido: deteniendo el bot de forma limpia.")
        return EXIT_OK
    except SystemExit as exc:
        logger.error("%s", exc)
        return EXIT_CONFIG
    except Exception:
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
        return EXIT_ERROR
    finally:
        mt5c.safe_shutdown()
        logger.info("Bot finalizado.")


if __name__ == "__main__":
    sys.exit(main())
