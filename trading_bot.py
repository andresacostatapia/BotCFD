"""
trading_bot.py
==============
Orquestador principal: carga configuración -> conecta MT5 -> evoluciona y
valida parámetros (walk-forward) -> bucle de trading con gestión de riesgo.

Uso:
    python trading_bot.py              # opera (en DEMO, si require_demo=true)
    python trading_bot.py --dry-run    # calcula todo pero NO envía órdenes
    python trading_bot.py --force-evolve  # no reutiliza los parámetros guardados

Códigos de salida:
    0 = parada limpia (Ctrl+C)   1 = error inesperado   2 = configuración
    3 = conexión MT5             4 = validación fallida (solo con retry_evolve_hours = 0;
                                     si no, el bot sigue en marcha y reintenta)

AVISO: framework educativo. No garantiza rentabilidad. Pruebe siempre en DEMO.
"""

from __future__ import annotations

import argparse
import hashlib
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
import news_filter as nf
from bot_common import (
    AUTO_STRATEGY, CONFIG_PATH, PROFILE_KEYS, VALID_STRATEGIES, ProfilePaths, config_path_for, global_config_path, hour_allowed,
    list_profiles, load_config, now_iso, profile_paths, read_json, save_profile, session_clock,
    setup_logging, write_json_atomic,
)
from risk_manager import RiskManager
from strategy_evolver import (
    IndicatorCache, asset_config, backtest, compute_signal, evolve_parameters, warmup_bars,
)

logger = logging.getLogger("trading_bot")

EXIT_OK, EXIT_ERROR, EXIT_CONFIG, EXIT_CONNECTION, EXIT_VALIDATION = 0, 1, 2, 3, 4
DEALS_REFRESH_SECONDS = 300
#: Correlaciones entre símbolos: velas H1 usadas y validez de la caché (segundos).
CORRELATION_TIMEFRAME = "H1"
CORRELATION_BARS = 500
CORRELATION_CACHE_SECONDS = 6 * 3600
#: Una señal solo se opera si la vela en curso (la siguiente a la de la señal) lleva
#: abierta como mucho esta fracción de su duración (mín. ``SIGNAL_MIN_AGE_SECONDS``).
#: El backtest entra a la APERTURA de esa vela: entrar mucho más tarde (p. ej. al
#: arrancar el bot a mitad de vela) ya no es la operación que se validó.
SIGNAL_MAX_AGE_FRACTION = 0.5
SIGNAL_MIN_AGE_SECONDS = 90
#: Segundos tras el cierre de vela a los que se despierta el bucle (margen para MT5).
BAR_CLOSE_MARGIN_SECONDS = 3
#: Los parámetros actuales se conservan (si la nueva búsqueda falla) como mucho
#: durante este múltiplo de ``reevolve_hours``.
CHAMPION_MAX_AGE_FACTOR = 4
#: Claves de configuración que influyen en la evolución: si cambia alguna, no se
#: reutilizan los parámetros guardados al arrancar.
EVOLUTION_KEYS: tuple[str, ...] = (
    "symbol", "timeframe", "strategy", "history_bars", "population", "generations",
    "commission_per_trade", "min_trades", "min_trades_oos", "fitness_folds", "train_ratio",
    "oos_pf_ratio_min", "min_pf_in_sample", "pf_cap", "validation_windows", "min_pf_oos",
    "regime_filter", "trading_hours", "exit_management", "be_atr_mult", "trail_atr_mult",
    "max_bars_in_trade", "atr_period", "sl_atr_mult", "tp_atr_mult", "evolve_sl_tp",
    "stability_check", "min_trades_per_week", "asset_class", "session_hours",
    "avoid_open_minutes", "flat_before_close_minutes", "long_only", "commission_override",
    "stock_commission_pct",
)


def correlated_positions(symbol: str, magic: int, side: int, positions: list[Any],
                         corr_fn: Any, threshold: float) -> list[str]:
    """
    Posiciones (de otros bots/perfiles) "ligadas" a abrir ``side`` (1 compra, -1 venta)
    en ``symbol``: |correlación| >= ``threshold`` y misma dirección efectiva (misma
    dirección con correlación positiva, o contraria con correlación negativa).
    ``corr_fn(otro_símbolo)`` devuelve la correlación o None.
    """
    linked = []
    for pos in positions:
        if pos.symbol == symbol and int(pos.magic) == int(magic):
            continue
        corr = corr_fn(pos.symbol)
        if corr is None or abs(corr) < threshold:
            continue
        pos_side = 1 if getattr(pos, "type", 0) == 0 else -1
        if side * pos_side * (1 if corr > 0 else -1) > 0:
            linked.append(f"{pos.symbol} {'BUY' if pos_side == 1 else 'SELL'} (corr {corr:+.2f})")
    return linked


def config_fingerprint(config: dict[str, Any]) -> str:
    """Huella corta de las claves de ``EVOLUTION_KEYS`` (para saber si se puede reutilizar)."""
    data = {k: config.get(k) for k in EVOLUTION_KEYS}
    return hashlib.sha1(json.dumps(data, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:12]


def result_score(result: dict[str, Any]) -> float:
    """Puntuación de una evolución: PF de validación (tope 5) x raíz de sus operaciones (tope 60)."""
    oos = result.get("out_of_sample") or {}
    return min(float(oos.get("profit_factor") or 0.0), 5.0) * min(int(oos.get("trades") or 0), 60) ** 0.5


def evolve_auto(df: Any, pop: int, gens: int, config: dict[str, Any],
                strategies: tuple[str, ...] = VALID_STRATEGIES) -> dict[str, Any]:
    """
    Estrategia automática: evoluciona y valida CADA estrategia y devuelve el resultado
    de la mejor ACEPTADA (por ``result_score``). Si ninguna se acepta, devuelve la de
    mejor puntuación marcada como descartada. Añade ``candidates`` con el resumen de todas.

    Elegir la mejor de varias aumenta el riesgo de quedarse con una "afortunada"; por eso
    solo compiten las que superan la validación completa (walk-forward y estabilidad).
    """
    results = []
    for name in strategies:
        res = evolve_parameters(df, pop=pop, gens=gens, config={**config, "strategy": name})
        res["strategy"] = res.get("strategy") or name
        results.append(res)
    candidates = [{
        "strategy": r["strategy"], "accepted": bool(r.get("accepted")),
        "pf_oos": (r.get("out_of_sample") or {}).get("profit_factor"),
        "trades_oos": (r.get("out_of_sample") or {}).get("trades"),
        "trades_per_week": r.get("trades_per_week"), "score": round(result_score(r), 3),
        "reason": r.get("reason", ""),
    } for r in results]
    accepted = [r for r in results if r.get("accepted")]
    pool = accepted or results
    best = dict(max(pool, key=result_score))
    best["candidates"] = candidates
    best["auto"] = True
    if accepted:
        best["reason"] = (f"estrategia automática: elegida {best['strategy']} entre "
                          f"{len(accepted)} aceptada(s) de {len(results)}")
    else:
        best["accepted"] = False
        best["reason"] = "estrategia automática: ninguna superó la validación (" + "; ".join(
            f"{c['strategy']}: {c['reason']}" for c in candidates) + ")"
    logger.info("Estrategia automática: %s", " | ".join(
        f"{c['strategy']} {'OK' if c['accepted'] else 'no'} PF {float(c['pf_oos'] or 0):.2f}/"
        f"{c['trades_oos'] or 0} op" for c in candidates))
    return best


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
        self.last_heartbeat: float | None = None  # time.monotonic() del último mensaje (None = nunca)
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
        self.params_adopted_at: float = time.time()  # reloj del PC al adoptar los parámetros
        self.last_evolution_ts: float = time.time()  # última evolución (o conservación) válida
        self.last_attempt_ts: float = time.time()    # último intento de evolución (aceptado o no)
        self.last_evolution_iso: str = ""
        self.last_health_ts: float = time.time()
        self.health: dict[str, Any] | None = None
        self.params_source = ""  # cómo se obtuvieron: búsqueda nueva / reutilizados / conservados
        self.halt_notified = False
        # Modo acciones: tipo de activo, sesión detectada y reglas efectivas
        self.asset_class = "otro"
        self.broker_long_only = False
        self.detected_session = ""
        self.rules: dict[str, Any] = asset_config(config, "otro", False)
        self.flat_done_day: str = ""
        # Noticias y correlación
        self.currencies: list[str] = []
        self._news: nf.NewsCalendar | None = None
        self.news_info: dict[str, Any] | None = None
        self._news_closed_for: float | None = None
        self._corr_cache: dict[tuple[str, str], tuple[float | None, float]] = {}
        # Diagnóstico: señales desde que arrancó y por qué no se operaron
        self.signal_stats: dict[str, Any] = {"signals": 0, "opened": 0, "blocked": {}, "last": ""}

    def _notify_prefix(self) -> str:
        sim = " SIM" if self.dry_run else ""
        return f"[MT5Bot {self.paths.name} {self.config['symbol']} {self.config['timeframe']}{sim}]"

    @property
    def dry_run(self) -> bool:
        """Simulación: forzada por --dry-run o por ``simulation`` del perfil (en caliente)."""
        return self.forced_dry_run or bool(self.config.get("simulation", False))

    # ---------------------------------------------------------------- evolución
    def resolve_asset(self) -> None:
        """Tipo de activo (del perfil o detectado en el bróker) y si solo admite compras."""
        cfg = self.config
        try:
            detected, long_only = mt5c.symbol_asset_info(cfg["symbol"])
        except Exception:  # noqa: BLE001 - informativo
            detected, long_only = "otro", False
        self.asset_class = detected if cfg.get("asset_class", "auto") == "auto" else cfg["asset_class"]
        self.broker_long_only = long_only
        try:
            self.currencies = mt5c.symbol_currencies(cfg["symbol"])
        except Exception:  # noqa: BLE001 - informativo
            self.currencies = []

    def update_rules(self, df: Any = None) -> None:
        """Recalcula las reglas efectivas (horario, comisión, solo compras...) del activo."""
        cfg = dict(self.config)
        if not str(cfg.get("session_hours") or "").strip() and self.detected_session:
            cfg["session_hours"] = self.detected_session
        self.rules = asset_config(cfg, self.asset_class, self.broker_long_only, df)
        if df is not None and self.asset_class == "accion" and not str(self.config.get("session_hours") or "").strip():
            self.detected_session = str(self.rules.get("session_hours") or "")
        if df is not None:
            self.check_max_spread(df)
        self.risk.update_config(self.rules)  # p. ej. lote máximo de acciones

    def check_max_spread(self, df: Any) -> None:
        """
        Avisa (y corrige si se dejó el valor por defecto) cuando el spread máximo es
        menor que el spread habitual del símbolo: el bot nunca abriría operaciones.
        """
        if "spread_price" not in getattr(df, "columns", []):
            return
        typical = float(df["spread_price"].tail(500).median())
        current = float(self.config.get("max_spread", 0.0))
        if typical <= 0 or current >= 1.2 * typical:
            return
        suggested = float(f"{typical * 2.5:.2g}")
        default = 0.0002
        if abs(current - default) < 1e-12:  # se quedó el valor por defecto de EURUSD: se corrige solo
            self.config["max_spread"] = suggested
            self.rules["max_spread"] = suggested
            try:
                own = {k: self.config[k] for k in PROFILE_KEYS}
                save_profile(own, self.config_path)
                self.config_mtime = self._config_stamp()
            except (ValueError, OSError):
                logger.exception("No se pudo guardar el spread máximo ajustado")
            logger.warning("Spread máximo ajustado automáticamente: %.6g -> %.6g (spread habitual de %s: %.6g).",
                           current, suggested, self.config["symbol"], typical)
        else:
            logger.warning("El spread máximo (%.6g) es menor que el spread habitual de %s (%.6g): el bot "
                           "casi nunca abrirá operaciones. Súbalo a ~%.6g en Editar….",
                           current, self.config["symbol"], typical, suggested)

    def _history(self, bars: int) -> Any:
        """Velas CERRADAS del símbolo (DataFrame vacío si no hay datos)."""
        df = mt5c.get_rates(self.config["symbol"], self.config["timeframe"], int(bars) + 1)
        return df if df.empty else df.iloc[:-1]  # descartar la vela en formación

    def _log_asset_rules(self) -> None:
        cfg = self.rules
        if self.asset_class == "accion":
            logger.info(
                "Modo ACCIONES: sesión %s (servidor) | sin entrar los primeros %d min | cerrar %d min antes "
                "del cierre | %s | comisión %s",
                cfg.get("session_hours") or "24 h", cfg["avoid_open_minutes"], cfg["flat_before_close_minutes"],
                "solo compras" if cfg["long_only"] else "compras y ventas",
                f"{cfg['commission_per_trade']:g} por operación" if self.config.get("commission_override") is not None
                else f"{cfg['commission_pct']:.2%} del precio")
        elif cfg.get("long_only"):
            logger.info("Este símbolo solo admite compras: se ignorarán las señales de venta.")

    def _run_evolution(self, df: Any) -> dict[str, Any]:
        """Evoluciona con la estrategia del perfil, o con todas si es ``auto``."""
        cfg = self.rules
        pop, gens = int(cfg["population"]), int(cfg["generations"])
        if self.config.get("strategy") == AUTO_STRATEGY:
            return evolve_auto(df, pop, gens, cfg)
        return evolve_parameters(df, pop=pop, gens=gens, config=cfg)

    def evolve(self, reason: str = "inicio") -> bool:
        """
        Descarga histórico, evoluciona parámetros y valida walk-forward.

        Campeón / retador: si ya hay parámetros en uso y la nueva búsqueda NO supera la
        validación, se conservan los actuales mientras sigan sanos con las velas
        posteriores a su adopción (ver ``champion_status``). Devuelve True si el bot
        queda con parámetros válidos.
        """
        self.last_attempt_ts = time.time()
        try:
            self.status.write("evolucionando", f"buscando y validando parámetros ({reason})",
                              symbol=self.config["symbol"], timeframe=self.config["timeframe"])
        except Exception:  # noqa: BLE001 - el estado es informativo
            logger.debug("No se pudo escribir el estado", exc_info=True)
        df = self._history(int(self.config["history_bars"]))
        if df.empty:
            logger.error("Sin datos históricos para %s %s.", self.config["symbol"], self.config["timeframe"])
            if self.params is not None:
                self.last_evolution_ts += 3600  # se conservan los actuales; nuevo intento en 1 h
            return self.params is not None and not self.paused_reason
        self.resolve_asset()
        self.update_rules(df)
        self._log_asset_rules()
        cfg = self.rules
        logger.info("Datos para evolución (%s): %s %s | %d velas cerradas desde %s hasta %s (hora servidor)",
                    reason, cfg["symbol"], cfg["timeframe"], len(df), df["time"].iloc[0], df["time"].iloc[-1])
        result = self._run_evolution(df)

        if not result["accepted"] and self.params is not None:
            keep, why = self.champion_status(df)
            if keep:
                previous = dict(self.evolution or {})
                kept = {**previous, "accepted": True, "kept_previous": True,
                        "reason": f"se mantienen los parámetros actuales ({why}); la nueva búsqueda "
                                  f"se descartó: {result['reason']}",
                        "challenger": {"strategy": result.get("strategy"), "reason": result.get("reason"),
                                       "out_of_sample": result.get("out_of_sample")}}
                self.evolution = kept
                self.params_source = f"conservados ({reason}): la nueva búsqueda se descartó"
                self._save_evolution(kept)
                self.last_evolution_ts = time.time()
                self.last_evolution_iso = now_iso()
                logger.warning("Nueva búsqueda DESCARTADA (%s); se MANTIENEN los parámetros actuales: %s",
                               result["reason"], why)
                return True
            logger.warning("Los parámetros actuales ya no se conservan: %s", why)

        self.evolution = result
        self.last_evolution_ts = time.time()
        self.last_evolution_iso = now_iso()
        self.params_since_ts = mt5c.get_server_time(cfg["symbol"]) or int(time.time())
        self.params_adopted_at = time.time()
        self.paused_reason = ""  # parámetros nuevos: se vuelve a evaluar el rendimiento desde cero
        self.health = None
        self.last_health_ts = time.time()
        self._save_evolution(result)
        if not result["accepted"]:
            logger.error("Parámetros DESCARTADOS por la validación: %s", result["reason"])
            self.params = None
            return False

        self.params = result["params"]
        self.params_source = f"búsqueda nueva ({reason})"
        logger.info(
            "Parámetros ACEPTADOS (%s%s): %s | PF IS=%.3f | PF OOS=%.3f",
            result.get("backend"), f", estrategia {result['strategy']}" if result.get("auto") else "",
            self.params, result["in_sample"]["profit_factor"], result["out_of_sample"]["profit_factor"],
        )
        return True

    def champion_status(self, df: Any = None) -> tuple[bool, str]:
        """
        ¿Se pueden conservar los parámetros actuales? No si el símbolo está pausado por
        rendimiento, si son demasiado antiguos (``CHAMPION_MAX_AGE_FACTOR`` x
        ``reevolve_hours``) o si con las velas posteriores a su adopción ya tienen
        suficientes operaciones simuladas y pierden (PF < 1).
        """
        if self.params is None:
            return False, "no hay parámetros"
        if self.paused_reason:
            return False, f"pausado ({self.paused_reason})"
        hours = int(self.config.get("reevolve_hours", 0) or 0)
        age_h = (time.time() - self.params_adopted_at) / 3600
        if hours > 0 and age_h > CHAMPION_MAX_AGE_FACTOR * hours:
            return False, f"tienen {age_h:.0f} h (máximo {CHAMPION_MAX_AGE_FACTOR * hours} h)"
        health = self.compute_health(df)
        if health is None:
            return True, f"adoptados hace {age_h:.0f} h, sin velas nuevas suficientes para evaluarlos"
        needed = max(5, int(self.config.get("min_trades_oos", 5)))
        if health["trades"] >= needed and health["pf"] < 1.0:
            return False, (f"desde su adopción pierden: PF {health['pf']:.2f} en {health['trades']} "
                           f"operaciones simuladas")
        return True, (f"desde su adopción: PF {health['pf']:.2f} en {health['trades']} operaciones "
                      f"simuladas")

    def compute_health(self, df: Any = None) -> dict[str, Any] | None:
        """
        Backtest de los parámetros ACTUALES solo con las velas posteriores a su adopción
        (datos que la evolución no vio): es la mejor medida de si siguen funcionando,
        con más operaciones que las reales (cuenta también las señales bloqueadas).
        """
        if self.params is None or not self.params_since_ts:
            return None
        try:
            import pandas as pd

            tf_sec = mt5c.timeframe_seconds(self.config["timeframe"])
            warm = warmup_bars(self.params) + int(self.config["atr_period"]) + 60
            if df is None:
                server_now = mt5c.get_server_time(self.config["symbol"]) or int(time.time())
                new_bars = max(0, int((server_now - self.params_since_ts) // tf_sec)) + 1
                df = self._history(min(int(self.config["history_bars"]), new_bars + warm))
            if df is None or df.empty:
                return None
            since = pd.Timestamp(int(self.params_since_ts), unit="s")
            times = pd.to_datetime(df["time"])
            if getattr(times.dt, "tz", None) is not None:
                times = times.dt.tz_localize(None)
            start = int((times < since).sum())
            if start >= len(df) - 1:
                return None  # aún no hay velas cerradas posteriores a la adopción
            return self._health_from(df, start)
        except Exception:  # noqa: BLE001 - diagnóstico: nunca debe tumbar el bot
            logger.debug("No se pudo calcular la salud de los parámetros", exc_info=True)
            return None

    def _health_from(self, df: Any, start: int) -> dict[str, Any]:
        cache = IndicatorCache(df, int(self.config["atr_period"]))
        m = backtest(cache, self.params or {}, start, len(cache), self.rules)
        return {"since": str(df["time"].iloc[start]), "bars": len(cache) - start, "trades": m.trades,
                "pf": round(float(m.profit_factor), 3), "win_rate": round(float(m.win_rate), 3),
                "checked": now_iso()}

    def maybe_check_health(self, positions: list[Any]) -> None:
        """
        Vigilancia periódica (``health_check_hours``): si con las velas nuevas los
        parámetros actuales tienen ``auto_disable_min_trades`` operaciones simuladas o
        más y su PF cae por debajo de ``auto_disable_pf``, re-evoluciona YA en vez de
        esperar a la re-evolución periódica.
        """
        hours = int(self.config.get("health_check_hours", 0) or 0)
        if hours <= 0 or self.params is None or positions:
            return
        if time.time() - self.last_health_ts < hours * 3600:
            return
        self.last_health_ts = time.time()
        health = self.compute_health()
        if health is None:
            return
        self.health = health
        logger.info("Revisión de los parámetros con las velas nuevas (desde %s): PF %.2f en %d operaciones "
                    "simuladas, %.0f%% de acierto", health["since"], health["pf"], health["trades"],
                    100 * health["win_rate"])
        threshold = float(self.config.get("auto_disable_pf", 0) or 0)
        needed = int(self.config.get("auto_disable_min_trades", 0) or 0)
        if threshold > 0 and needed > 0 and health["trades"] >= needed and health["pf"] < threshold:
            logger.warning("Los parámetros se han degradado (PF %.2f < %g con %d operaciones): "
                           "re-evolución anticipada.", health["pf"], threshold, health["trades"])
            ok = self.evolve("degradación")
            self.notifier.send(f"Parámetros degradados (PF {health['pf']:.2f}); re-evolución: "
                               + ("ACEPTADOS" if ok else "sin parámetros válidos"))

    def try_reuse_evolution(self) -> bool:
        """
        Al arrancar, reutiliza los parámetros ACEPTADOS guardados si son de la misma
        configuración (``config_fingerprint``) y más recientes que ``reevolve_hours``.
        Evita repetir la búsqueda en cada reinicio (repetirla hasta que "salga" aceptada
        sería otra forma de sobreajuste) y arranca al instante.
        """
        if not self.config.get("reuse_evolution", True):
            return False
        saved = read_json(self.paths.evolved) or {}
        if not saved.get("accepted") or not saved.get("params"):
            return False
        if saved.get("fingerprint") != config_fingerprint(self.config):
            logger.info("La configuración cambió desde la última evolución: se busca de nuevo.")
            return False
        evolved_at = float(saved.get("evolved_at_epoch") or 0.0)
        hours = int(self.config.get("reevolve_hours", 0) or 0) or 168
        age_h = (time.time() - evolved_at) / 3600
        if evolved_at <= 0 or age_h < 0 or age_h >= hours:
            return False
        df = self._history(min(int(self.config["history_bars"]), 2000))
        if df.empty:
            return False
        self.resolve_asset()
        self.update_rules(df)
        self._log_asset_rules()
        self.evolution = saved
        self.params = saved["params"]
        self.last_evolution_ts = evolved_at
        self.last_attempt_ts = evolved_at
        self.last_evolution_iso = str(saved.get("timestamp", ""))
        self.params_since_ts = int(saved.get("params_since_ts") or 0)
        self.params_adopted_at = float(saved.get("adopted_at_epoch") or evolved_at)
        self.last_health_ts = 0.0  # revisar la salud en el primer ciclo
        self.params_source = f"reutilizados (aceptados el {saved.get('timestamp', '?')})"
        logger.info("Se REUTILIZAN los parámetros aceptados el %s (hace %.1f h): %s. Próxima re-evolución "
                    "en %.0f h.", saved.get("timestamp", "?"), age_h, self.params, hours - age_h)
        return True

    def next_evolution_ts(self) -> float | None:
        """Hora (reloj del PC) de la próxima evolución prevista, o None si no hay."""
        if self.params is None or self.paused_reason:
            retry = int(self.config.get("retry_evolve_hours", 0) or 0) \
                or int(self.config.get("reevolve_hours", 0) or 0)
            return self.last_attempt_ts + retry * 3600 if retry else None
        hours = int(self.config.get("reevolve_hours", 0) or 0)
        return self.last_evolution_ts + hours * 3600 if hours else None

    def _save_evolution(self, result: dict[str, Any]) -> None:
        """Guarda la evolución en evolved_params*.json y la añade al historial del perfil."""
        payload = {
            "timestamp": now_iso(),
            "profile": self.paths.name,
            "symbol": self.config["symbol"],
            "timeframe": self.config["timeframe"],
            "history_bars": self.config["history_bars"],
            **result,
            "configured_strategy": self.config.get("strategy"),
            "fingerprint": config_fingerprint(self.config),
            "evolved_at_epoch": time.time(),
            "params_since_ts": self.params_since_ts,
            "adopted_at_epoch": self.params_adopted_at,
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
        if new_cfg.get("asset_class") != old.get("asset_class"):
            self.resolve_asset()
        self.update_rules()
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
            self.params = None  # otra estrategia/símbolo: los parámetros actuales no sirven
            if not self.evolve("cambio de configuración"):
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
        if self.last_heartbeat is None or now - self.last_heartbeat >= every - 1:
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
        self.maybe_check_health(positions)
        if self.params is None:
            due = self.next_evolution_ts()
            retry = (f"; nueva búsqueda a las {datetime.fromtimestamp(due):%H:%M} del "
                     f"{datetime.fromtimestamp(due):%d/%m}" if due else "")
            self.heartbeat(f"sin parámetros válidos: el bot no abre operaciones{retry}")
            return

        spread_ok = self.risk.check_spread()

        bars_needed = warmup_bars(self.params) + int(cfg["atr_period"]) + 50
        df = mt5c.get_rates(symbol, cfg["timeframe"], bars_needed + 1)
        if df.empty or len(df) < bars_needed:
            logger.warning("Velas insuficientes para calcular señal (%d).", len(df))
            return
        forming_open = df["time"].iloc[-1]  # apertura de la vela en curso
        df = df.iloc[:-1]  # sólo velas cerradas

        sig = compute_signal(df, self.params, int(cfg["atr_period"]))
        bar_time = sig["bar_time"]
        self.last_sig = sig
        tick = mt5c.get_tick(symbol)
        self.last_tick = tick
        if positions:
            self.manage_positions(positions, sig, tick)
            self.close_before_news(positions)
        news = self.news_check()
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
        stock_block = self.stock_session_block(tick, positions)
        if stock_block:
            session_open = False

        if bar_time != self.last_bar_time:
            if sig["signal"] == 0:
                self.last_bar_time = bar_time
            elif positions:
                self.last_bar_time = bar_time
                self._count_signal(sig, "ya había una posición abierta")
                logger.info("Señal de %s ignorada: ya hay %d posición(es) con magic %d.",
                            signal_txt, len(positions), magic)
            elif self.signal_is_stale(forming_open, tick):
                self.last_bar_time = bar_time
                self._count_signal(sig, "señal tardía (vela ya avanzada)")
                logger.info("Señal de %s ignorada: llegó con la vela siguiente ya avanzada (p. ej. al "
                            "arrancar el bot); el backtest habría entrado en su apertura.", signal_txt)
            elif news is not None:
                self.last_bar_time = bar_time
                self._count_signal(sig, f"noticia {news.currency}")
                logger.info("Señal de %s ignorada por noticia: %s (pausa de %s min antes y %s después).",
                            signal_txt, news.local_text(), cfg.get("news_minutes_before"),
                            cfg.get("news_minutes_after"))
            elif not session_open:
                self.last_bar_time = bar_time
                self._count_signal(sig, "fuera de horario / sesión")
                logger.info("Señal de %s ignorada: %s.", signal_txt,
                            stock_block or f"fuera del horario de operación ({cfg.get('trading_hours')}, hora servidor)")
            elif sig["signal"] == -1 and self.rules.get("long_only"):
                self.last_bar_time = bar_time
                self._count_signal(sig, "solo compras")
                logger.info("Señal de VENTA ignorada: este símbolo solo admite compras.")
            elif not spread_ok:
                # No se marca la vela: se reintenta en el próximo ciclo si el spread baja.
                if self.signal_stats.get("_spread_bar") != bar_time:
                    self.signal_stats["_spread_bar"] = bar_time
                    self._count_signal(sig, "spread alto (pendiente)")
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
            state = stock_block or f"fuera del horario de operación ({cfg.get('trading_hours')} h servidor)"
        elif news is not None:
            state = f"pausa por noticia: {news.local_text()}"
        else:
            state = self._waiting_text(sig)
        self.heartbeat(f"{price_txt} | {self._next_bar_text(bar_time, tick)} | {state} | "
                       f"{self._day_text()}")

    # ------------------------------------------------------------ noticias
    def news_calendar(self) -> nf.NewsCalendar:
        """Calendario compartido (logs/news_calendar.json) con la URL y el refresco actuales."""
        url = str(self.config.get("news_url") or nf.DEFAULT_NEWS_URL)
        refresh = float(self.config.get("news_refresh_hours", 6) or 6)
        if self._news is None or self._news.url != url or self._news.refresh_hours != refresh:
            self._news = nf.NewsCalendar(self.config_path.parent / "logs" / "news_calendar.json",
                                         url=url, refresh_hours=refresh)
        return self._news

    def news_check(self, now: float | None = None) -> nf.NewsEvent | None:
        """
        Noticia que bloquea ahora las entradas (None si no hay o el filtro está apagado).
        Actualiza ``news_info`` (próxima noticia o error) para el estado del bot.
        """
        cfg = self.config
        if not cfg.get("news_filter") or not self.currencies:
            self.news_info = None
            return None
        now = time.time() if now is None else now
        calendar = self.news_calendar()
        events = calendar.events(now)
        impacts = nf.impacts_for(str(cfg.get("news_impact", "alto")))
        before, after = int(cfg.get("news_minutes_before", 30)), int(cfg.get("news_minutes_after", 30))
        blocking = nf.find_blocking(events, self.currencies, now, before, after, impacts)
        upcoming = blocking or nf.next_event(events, self.currencies, now, after, impacts)
        self.news_info = {"currencies": self.currencies, "error": calendar.last_error,
                          "event": upcoming.to_dict(now, blocking is not None) if upcoming else None}
        return blocking

    def close_before_news(self, positions: list[Any], now: float | None = None) -> None:
        """Con ``news_close_positions``, cierra las posiciones del bot antes de una noticia."""
        if not positions or not self.config.get("news_close_positions") or not self.config.get("news_filter"):
            return
        event = self.news_check(now)
        now = time.time() if now is None else now
        if event is None or event.ts < now or self._news_closed_for == event.ts:
            return  # solo ANTES de la noticia, y una vez por noticia
        self._news_closed_for = event.ts
        for pos in positions:
            if self.dry_run:
                logger.info("[DRY-RUN] cerraría #%s antes de la noticia %s", pos.ticket, event.local_text())
            else:
                logger.warning("Cerrando #%s antes de la noticia %s", pos.ticket, event.local_text())
                mt5c.close_position(pos, int(self.config["deviation"]))
        self.notifier.send(f"Posiciones cerradas antes de la noticia {event.local_text()}")

    # ---------------------------------------------------------- correlación
    def correlation(self, other: str) -> float | None:
        """Correlación (cacheada unas horas) entre el símbolo del bot y ``other``."""
        symbol = self.config["symbol"]
        key = tuple(sorted((symbol, other)))
        cached = self._corr_cache.get(key)  # type: ignore[arg-type]
        if cached is not None and time.time() - cached[1] < CORRELATION_CACHE_SECONDS:
            return cached[0]
        try:
            value = mt5c.returns_correlation(symbol, other, CORRELATION_TIMEFRAME, CORRELATION_BARS)
        except Exception:  # noqa: BLE001 - sin datos: no se bloquea
            logger.debug("No se pudo calcular la correlación %s/%s", symbol, other, exc_info=True)
            value = None
        self._corr_cache[key] = (value, time.time())  # type: ignore[index]
        return value

    def correlation_block(self, side: int) -> str | None:
        """
        Motivo para NO abrir si ya hay ``max_correlated_positions`` posiciones de los bots
        en símbolos correlacionados (|corr| >= ``correlation_threshold``) que van en la
        MISMA dirección efectiva (misma dirección con correlación positiva, o contraria
        con correlación negativa: comprar EURUSD y vender USDCHF es casi la misma apuesta).
        """
        cfg = self.config
        if not cfg.get("correlation_filter"):
            return None
        threshold = float(cfg.get("correlation_threshold", 0.8))
        limit = max(1, int(cfg.get("max_correlated_positions", 1)))
        try:
            positions = mt5c.get_positions_by_magics(self.portfolio_magics())
        except Exception:  # noqa: BLE001
            return None
        linked = correlated_positions(cfg["symbol"], int(cfg["magic"]), side, positions,
                                      self.correlation, threshold)
        if len(linked) >= limit:
            return ", ".join(linked)
        return None

    def signal_is_stale(self, forming_open: Any, tick: Any) -> bool:
        """
        True si la vela en curso (la siguiente a la de la señal) lleva abierta más de
        ``SIGNAL_MAX_AGE_FRACTION`` de su duración: ya no es la entrada que se validó.
        """
        if tick is None or not getattr(tick, "time", 0) or not hasattr(forming_open, "to_pydatetime"):
            return False
        tf_sec = mt5c.timeframe_seconds(self.config["timeframe"])
        server_now = datetime.fromtimestamp(int(tick.time), tz=timezone.utc).replace(tzinfo=None)
        opened = forming_open.to_pydatetime()
        if opened.tzinfo is not None:
            opened = opened.replace(tzinfo=None)
        elapsed = (server_now - opened).total_seconds()
        return elapsed > max(SIGNAL_MIN_AGE_SECONDS, SIGNAL_MAX_AGE_FRACTION * tf_sec)

    def seconds_to_bar_close(self) -> float | None:
        """Segundos hasta el cierre de la vela en curso (hora del servidor), o None."""
        sig, tick = self.last_sig or {}, self.last_tick
        bar_time = sig.get("bar_time")
        if tick is None or not getattr(tick, "time", 0) or not hasattr(bar_time, "to_pydatetime"):
            return None
        tf = mt5c.timeframe_seconds(self.config["timeframe"])
        next_close = bar_time.to_pydatetime().replace(tzinfo=None) + timedelta(seconds=2 * tf)
        server_now = datetime.fromtimestamp(int(tick.time), tz=timezone.utc).replace(tzinfo=None)
        remaining = (next_close - server_now).total_seconds()
        return remaining if 0 <= remaining <= 2 * tf else None

    def _count_signal(self, sig: dict[str, Any], outcome: str | None) -> None:
        """Registra una señal y su resultado (None = operación abierta / simulada)."""
        stats = self.signal_stats
        pending = "spread alto (pendiente)"
        if outcome != pending and stats.get("_spread_bar") is not None \
                and stats.get("_spread_bar") == sig.get("bar_time"):
            # la misma señal ya se contó como "pendiente por spread": se sustituye
            stats["signals"] -= 1
            stats["blocked"][pending] = stats["blocked"].get(pending, 1) - 1
            if stats["blocked"][pending] <= 0:
                stats["blocked"].pop(pending, None)
            stats["_spread_bar"] = None
        stats["signals"] += 1
        side = "COMPRA" if sig.get("signal") == 1 else "VENTA"
        when = sig.get("bar_time")
        when_txt = f"{when:%d/%m %H:%M}" if hasattr(when, "strftime") else str(when)
        if outcome is None:
            stats["opened"] += 1
            stats["last"] = f"{side} {when_txt}: {'simulada' if self.dry_run else 'abierta'}"
        else:
            stats["blocked"][outcome] = stats["blocked"].get(outcome, 0) + 1
            stats["last"] = f"{side} {when_txt}: bloqueada ({outcome})"

    def open_trade(self, sig: dict[str, Any]) -> str | None:
        """
        Calcula SL/TP con ATR, dimensiona el lote y envía la orden.

        :returns: None si la operación se abrió (o se simuló); si no, el motivo corto.
        """
        outcome = self._open_trade(sig)
        self._count_signal(sig, outcome)
        return outcome

    def _open_trade(self, sig: dict[str, Any]) -> str | None:
        cfg = self.config
        symbol = cfg["symbol"]
        side = "buy" if sig["signal"] == 1 else "sell"
        atr_value = sig["atr"]
        if not atr_value or atr_value != atr_value or atr_value <= 0:
            logger.warning("SKIP: ATR inválido (%s).", atr_value)
            return "ATR inválido"

        params = self.params or {}
        sl_dist = float(params.get("sl_mult", cfg["sl_atr_mult"])) * atr_value
        tp_dist = float(params.get("tp_mult", cfg["tp_atr_mult"])) * atr_value
        min_stop = mt5c.get_min_stop_distance(symbol)
        if sl_dist <= min_stop or tp_dist <= min_stop:
            logger.warning(
                "SKIP: SL/TP (%.5f/%.5f) por debajo del mínimo del bróker (%.5f).",
                sl_dist, tp_dist, min_stop,
            )
            return "SL menor que el mínimo del bróker"

        if not self.portfolio_allows_new_trade():
            return "límite global de posiciones"
        linked = self.correlation_block(int(sig["signal"]))
        if linked:
            logger.warning("SKIP: ya hay posiciones correlacionadas en la misma dirección: %s "
                           "(máximo %s).", linked, cfg.get("max_correlated_positions"))
            return "correlación con otra posición"

        lot = self.risk.calculate_lot(sl_dist)
        if lot is None:
            logger.warning("SKIP: el RiskManager rechazó la operación: %s", self.risk.last_rejection)
            return "lote / riesgo"

        tick = mt5c.get_tick(symbol)
        if tick is None:
            logger.warning("SKIP: sin tick para %s.", symbol)
            return "sin cotización"
        price = tick.ask if side == "buy" else tick.bid
        sl = price - sl_dist if side == "buy" else price + sl_dist
        tp = price + tp_dist if side == "buy" else price - tp_dist

        if self.dry_run:
            logger.info(
                "[DRY-RUN] %s %.2f %s @ %.5f | SL %.5f | TP %.5f (no se envía)",
                side.upper(), lot, symbol, price, sl, tp,
            )
            self.notifier.send(f"Señal {side.upper()} {lot:.2f} @ {price:.6g} (simulación, no se envía)")
            return None

        result = mt5c.send_market_order(
            symbol, side, lot, sl, tp, int(cfg["magic"]),
            deviation=int(cfg["deviation"]), comment="MT5Bot evo",
        )
        if result is None:
            return "orden rechazada por el bróker"
        ticket = int(getattr(result, "order", 0) or 0)
        if ticket:
            self.registry.set(ticket, {"atr": atr_value, "entry": price, "best": price,
                                       "side": 1 if side == "buy" else -1, "opened": now_iso()})
        self.notifier.send(f"ORDEN {side.upper()} {lot:.2f} @ {price:.6g} | SL {sl:.6g} | TP {tp:.6g}")
        return None

    # ------------------------------------------------------------ horario
    def stock_session_block(self, tick: Any, positions: list[Any]) -> str:
        """
        Reglas de sesión del símbolo (acciones): devuelve el motivo por el que NO se
        puede abrir ahora ('' si se puede) y cierra las posiciones antes del cierre.
        """
        spec = str(self.rules.get("session_hours") or "")
        if not spec.strip() or tick is None or not getattr(tick, "time", 0):
            return ""
        now = datetime.fromtimestamp(int(tick.time), tz=timezone.utc)  # hora del servidor
        inside, since_open, to_close = session_clock(now.hour, now.minute, spec)
        if not inside:
            return f"mercado cerrado (sesión {spec} h servidor)"
        flat = int(self.rules.get("flat_before_close_minutes", 0) or 0)
        if flat > 0 and to_close <= flat:
            day = now.strftime("%Y-%m-%d")
            if positions and self.flat_done_day != day:
                self.flat_done_day = day
                if self.dry_run:
                    logger.info("[DRY-RUN] cerraría %d posición(es): faltan %d min para el cierre de la sesión.",
                                len(positions), to_close)
                else:
                    logger.info("Cierre antes del fin de sesión (faltan %d min): cerrando %d posición(es).",
                                to_close, len(positions))
                    for pos in positions:
                        mt5c.close_position(pos, int(self.config["deviation"]))
            return f"faltan {to_close} min para el cierre de la sesión ({spec} h servidor)"
        avoid = int(self.rules.get("avoid_open_minutes", 0) or 0)
        if avoid > 0 and since_open < avoid:
            return f"primeros {avoid} min de la sesión ({since_open} min desde la apertura)"
        return ""

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
        """
        Re-evolución sin posición abierta: periódica cada ``reevolve_hours`` y, si no hay
        parámetros válidos o el símbolo está pausado por rendimiento, reintento cada
        ``retry_evolve_hours`` (o ``reevolve_hours`` si es 0).
        """
        if positions:
            return
        due = self.next_evolution_ts()
        if due is None or time.time() < due:
            return
        if self.params is None or self.paused_reason:
            reason = "reintento" if self.params is None else "pausado por rendimiento"
            if self.paused_reason:
                self.params = None  # el campeón pausado no se conserva
            logger.info("Nueva búsqueda de parámetros (%s)…", reason)
        else:
            reason = "periódica"
            logger.info("Re-evolución periódica (cada %s h)…", self.config.get("reevolve_hours"))
        ok = self.evolve(reason)
        self.notifier.send(f"Re-evolución ({reason}): parámetros " + ("ACEPTADOS" if ok else
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
            # Si la vela cierra antes, se despierta justo después del cierre (menos retraso).
            wait = max(1.0, float(self.config["loop_seconds"]))
            to_close = self.seconds_to_bar_close()
            if to_close is not None:
                wait = max(1.0, min(wait, to_close + BAR_CLOSE_MARGIN_SECONDS))
            deadline = started + wait
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
                signal_stats={k: v for k, v in self.signal_stats.items() if not k.startswith("_")},
                trades_per_week=(self.evolution or {}).get("trades_per_week"),
                asset_class=self.asset_class,
                session_hours=self.rules.get("session_hours") or "",
                long_only=bool(self.rules.get("long_only")),
                last_evolution=self.last_evolution_iso,
                next_evolution=(datetime.fromtimestamp(nxt).isoformat(timespec="minutes")
                                if (nxt := self.next_evolution_ts()) else None),
                health=self.health,
                news=self.news_info,
                params_source=self.params_source,
                filters={"news": bool(self.config.get("news_filter")),
                         "correlation": bool(self.config.get("correlation_filter"))},
                strategy=(self.evolution or {}).get("strategy") if self.params else None,
                auto_strategy=self.config.get("strategy") == AUTO_STRATEGY,
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
    parser.add_argument("--force-evolve", action="store_true",
                        help="buscar parámetros nuevos aunque haya unos aceptados recientes")
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
        if not ((not args.force_evolve and bot.try_reuse_evolution()) or bot.evolve()):
            reason = (bot.evolution or {}).get("reason", "sin datos")
            retry = int(bot.config.get("retry_evolve_hours", 0) or 0)
            if retry <= 0:
                bot.notifier.send(f"Parámetros DESCARTADOS, el bot no opera: {reason}")
                logger.error("Abortando: la validación walk-forward no fue superada. Ver %s",
                             paths.evolved.name)
                exit_code, final_message = EXIT_VALIDATION, f"parámetros descartados: {reason}"
                return exit_code
            bot.notifier.send(f"Parámetros DESCARTADOS ({reason}); reintentará cada {retry} h")
            logger.warning("Validación no superada: el bot sigue en marcha SIN operar y buscará "
                           "parámetros de nuevo cada %d h (retry_evolve_hours).", retry)
        else:
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
