"""
lab.py
======
Laboratorio / escáner: evalúa muchas combinaciones **símbolo x timeframe x
estrategia** con el mismo proceso que usa el bot (evolución genética + validación
walk-forward) y las ordena por robustez. Sirve para decidir QUÉ operar: solo
merece la pena activar las combinaciones aceptadas.

Uso:
    python lab.py --symbols EURUSD,GBPUSD,US500 --timeframes M15,H1 --strategies all
    python lab.py --symbols EURUSD --timeframes M5 --strategies ruptura,reversion_media --bars 10000

Usa la configuración general (global_config.json): población, generaciones,
ventanas de validación, filtros y gestión de salida.

Salida:
* Líneas ``LAB_PROGRESS {json}`` y ``LAB_RESULT {json}`` por stdout (las lee el
  Centro de control).
* ``logs/lab_results.json`` con todos los resultados, ordenados.
* Se detiene si aparece ``logs/stop_lab.flag``.

Códigos de salida: 0 = terminado, 2 = argumentos inválidos, 3 = sin conexión MT5.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from bot_common import (
    BASE_DIR, LOG_DIR, VALID_STRATEGIES, VALID_TIMEFRAMES, load_global_config, now_iso,
    setup_logging, write_json_atomic,
)
from strategy_evolver import evolve_parameters

logger = logging.getLogger("lab")

RESULTS_PATH: Path = LOG_DIR / "lab_results.json"
STOP_FLAG: Path = LOG_DIR / "stop_lab.flag"

Job = tuple[str, str, str]  # (símbolo, timeframe, estrategia)


# ---------------------------------------------------------------------------
# Evaluación de una combinación (se ejecuta en procesos aparte)
# ---------------------------------------------------------------------------
def _worker_init() -> None:
    """Silencia el log de los procesos de trabajo (el progreso lo informa el principal)."""
    logging.getLogger().setLevel(logging.CRITICAL)


def evaluate_job(job: Job, df: pd.DataFrame, config: dict[str, Any], spread: float | None) -> dict[str, Any]:
    """Evoluciona y valida una combinación; devuelve un resumen serializable."""
    symbol, timeframe, strategy = job
    cfg = {**config, "symbol": symbol, "timeframe": timeframe, "strategy": strategy}
    started = time.monotonic()
    try:
        res = evolve_parameters(df, pop=int(cfg["population"]), gens=int(cfg["generations"]), config=cfg)
    except Exception as exc:  # noqa: BLE001 - un fallo no debe tumbar el laboratorio
        return {"symbol": symbol, "timeframe": timeframe, "strategy": strategy, "accepted": False,
                "reason": f"error: {exc}", "error": True}
    is_m = res.get("in_sample") or {}
    oos = res.get("out_of_sample") or {}
    windows = res.get("windows") or []
    positives = sum(1 for w in windows if w.get("trades_oos", 0) > 0 and w.get("pf_oos", 0) >= 1.0)
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "strategy": strategy,
        "accepted": bool(res.get("accepted")),
        "reason": res.get("reason", ""),
        "params": res.get("params"),
        "pf_is": is_m.get("profit_factor"),
        "trades_is": is_m.get("trades"),
        "pf_oos": oos.get("profit_factor"),
        "trades_oos": oos.get("trades"),
        "win_rate_oos": oos.get("win_rate"),
        "net_oos": oos.get("net_profit"),
        "dd_oos": oos.get("max_drawdown"),
        "windows": f"{positives}/{len(windows)}" if windows else "",
        "bars": len(df),
        "spread": spread,
        "seconds": round(time.monotonic() - started, 1),
    }


def score(result: dict[str, Any]) -> tuple[float, ...]:
    """Orden de los resultados: aceptadas primero, luego PF de validación y nº de operaciones."""
    pf = float(result.get("pf_oos") or 0.0)
    trades = int(result.get("trades_oos") or 0)
    return (1.0 if result.get("accepted") else 0.0, min(pf, 5.0) * min(trades, 60) ** 0.5, trades)


# ---------------------------------------------------------------------------
# Laboratorio
# ---------------------------------------------------------------------------
def run_lab(
    jobs: list[Job],
    data: dict[tuple[str, str], pd.DataFrame],
    config: dict[str, Any],
    spreads: dict[str, float | None] | None = None,
    workers: int = 1,
    on_result: Callable[[dict[str, Any], int, int], None] | None = None,
    should_stop: Callable[[], bool] = lambda: False,
) -> list[dict[str, Any]]:
    """
    Evalúa las combinaciones (en paralelo si ``workers`` > 1) y las devuelve ordenadas.

    :param data: velas por (símbolo, timeframe); las combinaciones sin datos se omiten.
    :param on_result: callback(resultado, hechas, total) tras cada combinación.
    :param should_stop: si devuelve True, se cancela lo pendiente.
    """
    spreads = spreads or {}
    runnable = [j for j in jobs if (j[0], j[1]) in data]
    total = len(runnable)
    results: list[dict[str, Any]] = []

    def _done(result: dict[str, Any]) -> None:
        results.append(result)
        if on_result:
            on_result(result, len(results), total)

    if workers <= 1:
        for job in runnable:
            if should_stop():
                break
            _done(evaluate_job(job, data[(job[0], job[1])], config, spreads.get(job[0])))
    else:
        with ProcessPoolExecutor(max_workers=workers, initializer=_worker_init) as pool:
            pending: dict[Future[dict[str, Any]], Job] = {
                pool.submit(evaluate_job, job, data[(job[0], job[1])], config, spreads.get(job[0])): job
                for job in runnable
            }
            while pending:
                finished, _ = wait(list(pending), timeout=1.0, return_when=FIRST_COMPLETED)
                for fut in finished:
                    pending.pop(fut)
                    _done(fut.result())
                if should_stop():
                    for fut in pending:
                        fut.cancel()
                    break
    results.sort(key=score, reverse=True)
    return results


def load_data(symbols: list[str], timeframes: list[str], bars: int,
              ) -> tuple[dict[tuple[str, str], pd.DataFrame], dict[str, float | None], list[str]]:
    """Descarga de MT5 las velas cerradas de cada símbolo y timeframe (y el spread actual)."""
    import mt5_connector as mt5c

    data: dict[tuple[str, str], pd.DataFrame] = {}
    spreads: dict[str, float | None] = {}
    problems: list[str] = []
    for symbol in symbols:
        if not mt5c.ensure_symbol(symbol):
            problems.append(f"{symbol}: no existe en el bróker")
            continue
        sp = mt5c.get_spread(symbol)
        spreads[symbol] = None if sp != sp else sp
        for tf in timeframes:
            df = mt5c.get_rates(symbol, tf, bars + 1)
            if df.empty or len(df) < 400:
                problems.append(f"{symbol} {tf}: datos insuficientes ({len(df)} velas)")
                continue
            data[(symbol, tf)] = df.iloc[:-1].reset_index(drop=True)
    return data, spreads, problems


def _emit(kind: str, payload: dict[str, Any]) -> None:
    """Línea legible por el Centro de control."""
    print(f"{kind} {json.dumps(payload, ensure_ascii=False, default=str)}", flush=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Argumentos de línea de comandos."""
    p = argparse.ArgumentParser(description="Laboratorio: escáner de combinaciones (educativo)")
    p.add_argument("--symbols", required=True, help="lista separada por comas, p. ej. EURUSD,US500")
    p.add_argument("--timeframes", default="M15,H1", help=f"de {','.join(VALID_TIMEFRAMES)}")
    p.add_argument("--strategies", default="all", help=f"'all' o de {','.join(VALID_STRATEGIES)}")
    p.add_argument("--bars", type=int, default=0, help="velas de historial (0 = las de la config. general)")
    p.add_argument("--workers", type=int, default=0, help="procesos en paralelo (0 = automático)")
    p.add_argument("--out", type=Path, default=RESULTS_PATH)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Punto de entrada del laboratorio."""
    args = parse_args(argv)
    setup_logging("lab.log")
    config = load_global_config(BASE_DIR)
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    timeframes = [t.strip().upper() for t in args.timeframes.split(",") if t.strip()]
    strategies = (list(VALID_STRATEGIES) if args.strategies.strip().lower() == "all"
                  else [s.strip() for s in args.strategies.split(",") if s.strip()])
    bad = ([t for t in timeframes if t not in VALID_TIMEFRAMES]
           + [s for s in strategies if s not in VALID_STRATEGIES])
    if not symbols or not timeframes or not strategies or bad:
        logger.error("Argumentos inválidos: %s", bad or "faltan símbolos/timeframes/estrategias")
        return 2
    bars = args.bars or int(config["history_bars"])
    workers = args.workers or int(config.get("lab_workers") or 0) or max(1, (os.cpu_count() or 2) - 1)
    STOP_FLAG.unlink(missing_ok=True)

    import mt5_connector as mt5c

    logger.info("Laboratorio: %d símbolos x %d timeframes x %d estrategias | %d velas | %d procesos",
                len(symbols), len(timeframes), len(strategies), bars, workers)
    _emit("LAB_PROGRESS", {"stage": "datos", "done": 0, "total": 0,
                           "message": "Descargando datos de MetaTrader 5…"})
    try:
        if not mt5c.connect_mt5_with_retry(max_retries=3, wait_seconds=2,
                                           require_demo=bool(config["require_demo"]),
                                           terminal_path=config.get("terminal_path") or None):
            _emit("LAB_PROGRESS", {"stage": "error", "message": "No se pudo conectar con MetaTrader 5"})
            return 3
        data, spreads, problems = load_data(symbols, timeframes, bars)
    except SystemExit as exc:
        _emit("LAB_PROGRESS", {"stage": "error", "message": str(exc)})
        return 3
    finally:
        mt5c.safe_shutdown()
    for p in problems:
        logger.warning("Omitido: %s", p)

    jobs = [(s, t, st) for s in symbols for t in timeframes for st in strategies]
    total = sum(1 for j in jobs if (j[0], j[1]) in data)
    payload: dict[str, Any] = {"started": now_iso(), "finished": None, "bars": bars,
                               "symbols": symbols, "timeframes": timeframes, "strategies": strategies,
                               "problems": problems, "results": []}
    _emit("LAB_PROGRESS", {"stage": "evolucion", "done": 0, "total": total,
                           "message": f"Evaluando {total} combinaciones con {workers} procesos…"})

    def on_result(result: dict[str, Any], done: int, n: int) -> None:
        payload["results"].append(result)
        payload["results"].sort(key=score, reverse=True)
        write_json_atomic(args.out, payload)
        _emit("LAB_RESULT", result)
        _emit("LAB_PROGRESS", {"stage": "evolucion", "done": done, "total": n,
                               "message": f"{done}/{n}: {result['symbol']} {result['timeframe']} "
                                          f"{result['strategy']} -> "
                                          f"{'ACEPTADA' if result['accepted'] else 'descartada'}"})

    results = run_lab(jobs, data, config, spreads, workers, on_result, STOP_FLAG.exists)
    payload["results"] = results
    payload["finished"] = now_iso()
    payload["stopped"] = STOP_FLAG.exists()
    write_json_atomic(args.out, payload)
    STOP_FLAG.unlink(missing_ok=True)
    accepted = sum(1 for r in results if r.get("accepted"))
    logger.info("Laboratorio terminado: %d combinaciones, %d aceptadas.", len(results), accepted)
    _emit("LAB_PROGRESS", {"stage": "fin", "done": len(results), "total": total,
                           "message": f"Terminado: {accepted} de {len(results)} combinaciones aceptadas"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
