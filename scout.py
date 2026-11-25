"""
scout.py
========
Explorador: revisa periódicamente los símbolos del bróker (de las categorías
elegidas) y **propone** combinaciones símbolo x timeframe x estrategia que superan
la validación walk-forward con criterios más exigentes que el bot. No opera ni
crea perfiles: las propuestas se aprueban en el Centro de control.

Cómo decide:
1. Universo: símbolos operables de ``scout_categories``, ordenados por coste
   relativo (spread / ATR de H1) y limitados a ``scout_max_symbols``; se descartan
   los que superan ``scout_max_spread_atr``.
2. Evalúa cada combinación con el mismo motor del laboratorio, exigiendo
   PF de validación >= ``scout_min_pf_oos`` y >= ``scout_min_trades_oos`` operaciones.
3. Solo propone una combinación aceptada en ``scout_confirmations`` exploraciones
   SEGUIDAS (si una exploración la rechaza, vuelve a cero). Más símbolos revisados
   = más probabilidad de encontrar algo por azar; las confirmaciones lo reducen.

Uso:
    python scout.py --once     # una exploración
    python scout.py --loop     # cada scout_interval_minutes (lo lanza el Centro de control)

Archivos (en logs/):
* ``scout_state.json``      candidatos, propuestas y estado (lo escribe este script)
* ``scout_decisions.json``  combinaciones descartadas por el usuario (lo escribe la ventana)
* ``scout_now.flag``        "explorar ahora" (lo crea la ventana)
* ``stop_scout.flag``       detener el bucle
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from bot_common import (
    BASE_DIR, LOG_DIR, config_path_for, list_profiles, load_config, load_global_config, now_iso,
    read_json, setup_logging, write_json_atomic,
)

logger = logging.getLogger("scout")

STATE_PATH: Path = LOG_DIR / "scout_state.json"
DECISIONS_PATH: Path = LOG_DIR / "scout_decisions.json"
NOW_FLAG: Path = LOG_DIR / "scout_now.flag"
STOP_FLAG: Path = LOG_DIR / "stop_scout.flag"


def combo_key(symbol: str, timeframe: str, strategy: str) -> str:
    """Clave única de una combinación."""
    return f"{symbol}|{timeframe}|{strategy}"


def existing_combos(base_dir: Path | None = None) -> set[str]:
    """Combinaciones que ya tienen perfil (no se proponen)."""
    out = set()
    for name in list_profiles(base_dir):
        try:
            cfg = load_config(config_path_for(name, base_dir))
        except (ValueError, FileNotFoundError):
            continue
        out.add(combo_key(cfg["symbol"], cfg["timeframe"], cfg["strategy"]))
    return out


def dismissed_combos(path: Path = DECISIONS_PATH) -> set[str]:
    """Combinaciones descartadas por el usuario."""
    data = read_json(path) or {}
    return set(data.get("dismissed") or [])


def select_universe(symbols: list[dict[str, Any]], categories: list[str], max_symbols: int,
                    max_spread_atr: float, cost_ratio: Callable[[str], float | None],
                    ) -> tuple[list[str], list[str]]:
    """
    Elige los símbolos a explorar: de las categorías pedidas, operables, con
    spread/ATR <= ``max_spread_atr``, ordenados por ese coste y como máximo ``max_symbols``.

    :returns: (símbolos elegidos, motivos de los omitidos)
    """
    wanted = {c.lower() for c in categories}
    pool = [s for s in symbols if s.get("tradable", True)
            and (not wanted or str(s.get("category", "")).lower() in wanted)]
    ranked: list[tuple[float, str]] = []
    skipped: list[str] = []
    for s in pool:
        ratio = cost_ratio(s["name"])
        if ratio is None:
            skipped.append(f"{s['name']}: sin datos")
        elif ratio > max_spread_atr:
            skipped.append(f"{s['name']}: spread demasiado alto ({ratio:.0%} del ATR)")
        else:
            ranked.append((ratio, s["name"]))
    ranked.sort()
    return [name for _r, name in ranked[:max_symbols]], skipped


def update_state(state: dict[str, Any], results: list[dict[str, Any]], confirmations: int,
                 exclude: set[str], now: str) -> tuple[dict[str, Any], list[str]]:
    """
    Actualiza candidatos y propuestas con los resultados de una exploración.

    Aceptada -> racha + 1; rechazada -> se elimina (la racha vuelve a cero).
    Es propuesta si su racha >= ``confirmations``. Devuelve (estado, claves nuevas propuestas).
    """
    candidates: dict[str, Any] = dict(state.get("candidates") or {})
    for key in list(candidates):
        if key in exclude:
            candidates.pop(key)
    for r in results:
        key = combo_key(r["symbol"], r["timeframe"], r["strategy"])
        if key in exclude:
            continue
        if r.get("accepted"):
            c = candidates.get(key) or {"first_seen": now, "streak": 0, "proposed": False}
            c.update(symbol=r["symbol"], timeframe=r["timeframe"], strategy=r["strategy"],
                     streak=int(c.get("streak", 0)) + 1, last_seen=now,
                     last={k: r.get(k) for k in ("pf_oos", "trades_oos", "pf_is", "windows",
                                                 "win_rate_oos", "spread", "params", "reason")})
            candidates[key] = c
        else:
            candidates.pop(key, None)
    new: list[str] = []
    for key, c in candidates.items():
        if c["streak"] >= confirmations and not c.get("proposed"):
            c["proposed"] = True
            c["proposed_at"] = now
            new.append(key)
    state = {**state, "candidates": candidates, "last_scan": now}
    return state, new


def _emit(kind: str, payload: dict[str, Any]) -> None:
    """Línea legible por el Centro de control."""
    print(f"{kind} {json.dumps(payload, ensure_ascii=False, default=str)}", flush=True)


def scan_once(config: dict[str, Any], base_dir: Path = BASE_DIR,
              stop: Callable[[], bool] = STOP_FLAG.exists) -> dict[str, Any]:
    """Una exploración completa. Devuelve un resumen (también lo guarda en el estado)."""
    state_path = base_dir / "logs" / STATE_PATH.name
    import lab
    import mt5_connector as mt5c
    from strategy_evolver import atr

    started = time.monotonic()
    timeframes = list(config["scout_timeframes"])
    strategies = list(config["scout_strategies"])
    exclude = existing_combos(base_dir) | dismissed_combos(base_dir / "logs" / DECISIONS_PATH.name)

    _emit("SCOUT_PROGRESS", {"stage": "datos", "message": "Conectando y eligiendo símbolos…"})
    try:
        if not mt5c.connect_mt5_with_retry(max_retries=3, wait_seconds=2,
                                           require_demo=bool(config["require_demo"]),
                                           terminal_path=config.get("terminal_path") or None):
            raise RuntimeError("no se pudo conectar con MetaTrader 5")
        symbols = mt5c.list_symbols()

        def cost_ratio(name: str) -> float | None:
            df = mt5c.get_rates(name, "H1", 220)
            if df.empty or len(df) < 50:
                return None
            atr_now = float(atr(df, 14).iloc[-1])
            if not atr_now > 0:
                return None
            if "spread_price" in df.columns and float(df["spread_price"].median()) > 0:
                spread = float(df["spread_price"].median())
            else:
                spread = mt5c.get_spread(name)
                if spread != spread:
                    return None
            return spread / atr_now

        universe, skipped = select_universe(symbols, list(config["scout_categories"]),
                                            int(config["scout_max_symbols"]),
                                            float(config["scout_max_spread_atr"]), cost_ratio)
        _emit("SCOUT_PROGRESS", {"stage": "datos", "message": f"Descargando historial de {len(universe)} "
                                                              f"símbolos ({len(skipped)} omitidos)…"})
        data, spreads, problems = lab.load_data(universe, timeframes, int(config["history_bars"]))
    finally:
        mt5c.safe_shutdown()

    jobs = [(s, tf, st) for s in universe for tf in timeframes for st in strategies
            if combo_key(s, tf, st) not in exclude]
    strict = {**config,
              "min_pf_oos": max(float(config["min_pf_oos"]), float(config["scout_min_pf_oos"])),
              "min_trades_oos": max(int(config["min_trades_oos"]), int(config["scout_min_trades_oos"]))}
    total = sum(1 for j in jobs if (j[0], j[1]) in data)
    workers = int(config.get("lab_workers") or 0) or max(1, (os.cpu_count() or 2) - 1)

    def on_result(result: dict[str, Any], done: int, n: int) -> None:
        _emit("SCOUT_PROGRESS", {"stage": "evaluando", "done": done, "total": n,
                                 "message": f"Explorando {done}/{n}: {result['symbol']} {result['timeframe']} "
                                            f"{result['strategy']}"})

    _emit("SCOUT_PROGRESS", {"stage": "evaluando", "done": 0, "total": total,
                             "message": f"Evaluando {total} combinaciones…"})
    results = lab.run_lab(jobs, data, strict, spreads, workers, on_result, stop)

    now = now_iso()
    state = read_json(state_path) or {}
    state, new = update_state(state, results, int(config["scout_confirmations"]), exclude, now)
    accepted = sum(1 for r in results if r.get("accepted"))
    summary = {"time": now, "symbols": len(universe), "combinations": len(results), "accepted": accepted,
               "new_proposals": len(new), "skipped": skipped[:50] + problems[:50],
               "seconds": round(time.monotonic() - started, 1)}
    state["last_summary"] = summary
    state["history"] = ([summary] + list(state.get("history") or []))[:50]
    write_json_atomic(state_path, state)
    for key in new:
        c = state["candidates"][key]
        _emit("SCOUT_PROPOSAL", {"key": key, **{k: c[k] for k in ("symbol", "timeframe", "strategy")},
                                 "pf_oos": c["last"].get("pf_oos"), "trades_oos": c["last"].get("trades_oos")})
    logger.info("Exploración: %d símbolos, %d combinaciones, %d aceptadas, %d propuestas nuevas (%.0f s).",
                len(universe), len(results), accepted, len(new), summary["seconds"])
    _emit("SCOUT_PROGRESS", {"stage": "fin", "done": total, "total": total,
                             "message": f"Exploración terminada: {accepted} aceptadas, {len(new)} propuestas nuevas"})
    return summary


def _set_state(state_path: Path, **fields: Any) -> None:
    state = read_json(state_path) or {}
    state.update(fields)
    write_json_atomic(state_path, state)


def run_loop(state_path: Path = STATE_PATH) -> int:
    """Explora cada ``scout_interval_minutes`` (o al pedir 'explorar ahora') hasta la parada."""
    STOP_FLAG.unlink(missing_ok=True)
    next_run = datetime.now()
    _set_state(state_path, running=True, pid=os.getpid())
    try:
        while not STOP_FLAG.exists():
            config = load_global_config(BASE_DIR)
            if NOW_FLAG.exists() or datetime.now() >= next_run:
                NOW_FLAG.unlink(missing_ok=True)
                if not config.get("scout_categories"):
                    _emit("SCOUT_PROGRESS", {"stage": "error",
                                             "message": "Elija al menos una categoría en el Explorador."})
                else:
                    try:
                        scan_once(config, BASE_DIR)
                    except Exception as exc:  # noqa: BLE001 - el bucle sigue
                        logger.exception("Error en la exploración")
                        _emit("SCOUT_PROGRESS", {"stage": "error", "message": f"Error: {exc}"})
                next_run = datetime.now() + timedelta(minutes=int(config["scout_interval_minutes"]))
                _set_state(state_path, next_scan=next_run.isoformat(timespec="seconds"), running=True)
            time.sleep(1.0)
    finally:
        STOP_FLAG.unlink(missing_ok=True)
        _set_state(state_path, running=False, next_scan=None)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Punto de entrada."""
    p = argparse.ArgumentParser(description="Explorador de símbolos (educativo)")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="una sola exploración")
    mode.add_argument("--loop", action="store_true", help="explorar periódicamente")
    args = p.parse_args(argv)
    setup_logging("scout.log")
    if args.loop:
        return run_loop()
    config = load_global_config(BASE_DIR)
    if not config.get("scout_categories"):
        _emit("SCOUT_PROGRESS", {"stage": "error", "message": "Elija al menos una categoría en el Explorador."})
        return 2
    try:
        scan_once(config)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Error en la exploración")
        _emit("SCOUT_PROGRESS", {"stage": "error", "message": f"Error: {exc}"})
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
