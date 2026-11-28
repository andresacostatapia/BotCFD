"""
diagnostics.py
==============
Diagnóstico de las funciones "invisibles" del bot, para comprobar que funcionan:

* conexión y tipo de cuenta;
* calendario de noticias (descarga, próximas noticias y qué símbolos estarían en pausa);
* correlación entre los símbolos configurados y qué entradas bloquearía ahora;
* estado interno de cada bot (origen de los parámetros, próxima búsqueda, revisión
  con velas nuevas, noticias, señales bloqueadas y por qué).

``build_report`` recibe el módulo ``mt5_connector`` YA conectado (así se puede probar
con un MetaTrader5 simulado) y devuelve secciones con líneas (nivel, texto).
``report_text`` lo convierte en texto plano para copiarlo.
"""

from __future__ import annotations

import time
from datetime import datetime
from itertools import combinations
from pathlib import Path
from typing import Any

import news_filter as nf

#: Niveles de cada línea: ok (verde), warn (naranja), bad (rojo), info (normal).
LEVEL_MARK = {"ok": "[OK]", "warn": "[!]", "bad": "[X]", "info": "   "}


def _section(title: str) -> dict[str, Any]:
    return {"title": title, "lines": []}


def _add(section: dict[str, Any], level: str, text: str) -> None:
    section["lines"].append((level, text))


def check_account(mt5c: Any) -> dict[str, Any]:
    sec = _section("Conexión y cuenta")
    try:
        acc = mt5c.mt5.account_info()
        demo = getattr(acc, "trade_mode", None) == getattr(mt5c.mt5, "ACCOUNT_TRADE_MODE_DEMO", 0)
        _add(sec, "ok", f"Conectado a MT5: cuenta {acc.login} ({getattr(acc, 'server', '?')})")
        _add(sec, "ok" if demo else "warn", "Cuenta DEMO" if demo else "Cuenta REAL")
        _add(sec, "info", f"Balance {acc.balance:,.2f} · equity {acc.equity:,.2f} {getattr(acc, 'currency', '')}")
    except Exception as exc:  # noqa: BLE001
        _add(sec, "bad", f"No se pudo leer la cuenta: {exc}")
    return sec


def check_news(mt5c: Any, gcfg: dict[str, Any], symbols: list[str], cache_path: Path,
               now: float, calendar: nf.NewsCalendar | None = None) -> dict[str, Any]:
    sec = _section("Calendario de noticias")
    if not gcfg.get("news_filter"):
        _add(sec, "warn", "El filtro de noticias está DESACTIVADO (Configuración general > Noticias y correlación)")
    calendar = calendar or nf.NewsCalendar(cache_path, url=str(gcfg.get("news_url") or nf.DEFAULT_NEWS_URL),
                                           refresh_hours=float(gcfg.get("news_refresh_hours") or 6))
    events = calendar.events(now)
    cache = calendar._read_cache()
    fetched = float(cache.get("fetched_at") or 0.0)
    if events:
        age_h = (now - fetched) / 3600
        _add(sec, "ok", f"Calendario descargado hace {age_h:.1f} h: {len(events)} noticias esta semana")
    else:
        _add(sec, "bad", "Sin calendario: el filtro NO bloquea nada"
             + (f" ({calendar.last_error})" if calendar.last_error else ""))
        _add(sec, "info", "Compruebe que el PC tiene internet y que el antivirus/firewall permite a Python "
                          "acceder a nfs.faireconomy.media")
        return sec
    if calendar.last_error:
        _add(sec, "warn", f"Último intento de actualización fallido: {calendar.last_error} (se usa la copia guardada)")

    impacts = nf.impacts_for(str(gcfg.get("news_impact", "alto")))
    before, after = int(gcfg.get("news_minutes_before", 30)), int(gcfg.get("news_minutes_after", 30))
    per_symbol: dict[str, list[str]] = {}
    for sym in symbols:
        try:
            per_symbol[sym] = mt5c.symbol_currencies(sym)
        except Exception:  # noqa: BLE001
            per_symbol[sym] = []
    all_cur = sorted({c for cur in per_symbol.values() for c in cur})
    upcoming = [e for e in events if e.impact in impacts and e.ts + after * 60 >= now
                and (e.currency in all_cur or e.currency == "ALL")][:8]
    _add(sec, "info", f"Divisas vigiladas ({', '.join(all_cur) or 'ninguna'}), impacto "
                      f"{gcfg.get('news_impact', 'alto')}, pausa {before} min antes y {after} min después")
    if upcoming:
        _add(sec, "info", "Próximas noticias que pausan sus símbolos:")
        for e in upcoming:
            _add(sec, "info", f"    {e.local_text()}")
    else:
        _add(sec, "info", "No quedan noticias relevantes para sus divisas esta semana")
    for sym, curs in per_symbol.items():
        if not curs:
            _add(sec, "warn", f"{sym}: no se reconocen sus divisas: el filtro no le afecta")
            continue
        ev = nf.find_blocking(events, curs, now, before, after, impacts)
        if ev is not None:
            _add(sec, "warn", f"{sym} ({', '.join(curs)}): AHORA EN PAUSA por {ev.local_text()}")
        else:
            nxt = nf.next_event(events, curs, now, after, impacts)
            when = f"próxima pausa {datetime.fromtimestamp(nxt.ts - before * 60):%d/%m %H:%M}" if nxt else \
                "sin pausas esta semana"
            _add(sec, "ok", f"{sym} ({', '.join(curs)}): puede operar ahora; {when}")
    return sec


def check_correlation(mt5c: Any, gcfg: dict[str, Any], profiles: list[dict[str, Any]],
                      magics: set[int]) -> dict[str, Any]:
    from trading_bot import correlated_positions

    sec = _section("Correlación entre sus símbolos")
    if not gcfg.get("correlation_filter"):
        _add(sec, "warn", "El control de correlación está DESACTIVADO")
    threshold = float(gcfg.get("correlation_threshold", 0.8))
    limit = max(1, int(gcfg.get("max_correlated_positions", 1)))
    symbols = sorted({p["cfg"]["symbol"] for p in profiles})
    cache: dict[tuple[str, str], float | None] = {}

    def corr(a: str, b: str) -> float | None:
        key = tuple(sorted((a, b)))
        if key not in cache:
            try:
                cache[key] = mt5c.returns_correlation(a, b, "H1", 500)  # type: ignore[index]
            except Exception:  # noqa: BLE001
                cache[key] = None  # type: ignore[index]
        return cache[key]  # type: ignore[index]

    _add(sec, "info", f"Rendimientos H1 de las últimas 500 velas · ligados si |correlación| >= {threshold:g} · "
                      f"máximo {limit} posición(es) por grupo en la misma dirección")
    pairs = [(a, b, corr(a, b)) for a, b in combinations(symbols, 2)]
    linked = [(a, b, c) for a, b, c in pairs if c is not None and abs(c) >= threshold]
    if linked:
        for a, b, c in sorted(linked, key=lambda t: -abs(t[2])):
            how = "se mueven igual" if c > 0 else "se mueven al revés"
            _add(sec, "info", f"Ligados: {a} y {b} (corr {c:+.2f}, {how})")
    elif len(symbols) > 1:
        _add(sec, "ok", "Ningún par de sus símbolos supera el umbral: el filtro no bloqueará entre ellos")
    missing = [f"{a}/{b}" for a, b, c in pairs if c is None]
    if missing:
        _add(sec, "warn", "Sin datos suficientes para: " + ", ".join(missing[:10]))
    try:
        positions = mt5c.get_positions_by_magics(magics)
    except Exception:  # noqa: BLE001
        positions = []
    if not positions:
        _add(sec, "info", "Ahora no hay posiciones abiertas de los bots: nada que bloquear")
        return sec
    _add(sec, "info", "Posiciones abiertas de los bots: " + ", ".join(
        f"{p.symbol} {'BUY' if getattr(p, 'type', 0) == 0 else 'SELL'}" for p in positions))
    for prof in profiles:
        cfg = prof["cfg"]
        verdicts = []
        for side, name in ((1, "compra"), (-1, "venta")):
            linked_now = correlated_positions(cfg["symbol"], int(cfg["magic"]), side, positions,
                                              lambda other, s=cfg["symbol"]: corr(s, other), threshold)
            blocked = gcfg.get("correlation_filter") and len(linked_now) >= limit
            verdicts.append(f"{name} {'BLOQUEADA por ' + ', '.join(linked_now) if blocked else 'permitida'}")
        level = "warn" if any("BLOQUEADA" in v for v in verdicts) else "ok"
        _add(sec, level, f"{prof['name']} ({cfg['symbol']}): " + "; ".join(verdicts))
    return sec


def check_bots(profiles: list[dict[str, Any]]) -> dict[str, Any]:
    sec = _section("Estado interno de cada bot")
    for prof in profiles:
        cfg, st, evo = prof["cfg"], prof.get("status") or {}, prof.get("evolved") or {}
        head = f"{prof['name']} ({cfg['symbol']} {cfg['timeframe']} · {cfg.get('strategy')})"
        if not prof.get("live"):
            accepted = "aceptados" if evo.get("accepted") else ("descartados" if evo else "sin evolución")
            _add(sec, "info", f"{head}: detenido · últimos parámetros {accepted}")
            continue
        phase = st.get("phase", "?")
        level = {"operando": "ok", "sin_parametros": "warn", "pausado_rendimiento": "warn",
                 "detenido_perdida": "bad"}.get(phase, "info")
        _add(sec, level, f"{head}: {phase.replace('_', ' ')}"
                         + (f" · {'SIMULACIÓN' if st.get('dry_run') else 'ÓRDENES REALES'}"))
        if st.get("params_source"):
            _add(sec, "info", f"    Parámetros: {st['params_source']}")
        if cfg.get("strategy") == "auto" and st.get("strategy"):
            _add(sec, "info", f"    Estrategia automática: opera {st['strategy']}")
        if st.get("next_evolution"):
            _add(sec, "info", f"    Próxima búsqueda de parámetros: {str(st['next_evolution']).replace('T', ' ')}")
        health = st.get("health") or {}
        if health:
            lvl = "ok" if (health.get("pf") or 0) >= 1 else "warn"
            _add(sec, lvl, f"    Con velas nuevas (desde {str(health.get('since', ''))[:16]}): PF "
                           f"{health.get('pf')} en {health.get('trades')} operaciones simuladas")
        news = st.get("news")
        filters = st.get("filters") or {}
        if filters.get("news") and not news:
            _add(sec, "warn", "    Noticias: el bot no reconoce las divisas del símbolo")
        elif news:
            ev = news.get("event") or {}
            if news.get("error") and not ev:
                _add(sec, "warn", f"    Noticias: {news['error']}")
            elif ev:
                _add(sec, "warn" if ev.get("blocking") else "ok",
                     f"    Noticias: {'EN PAUSA por' if ev.get('blocking') else 'próxima'} "
                     f"{str(ev.get('time', '')).replace('T', ' ')} {ev.get('currency')} {ev.get('title')}")
            else:
                _add(sec, "ok", "    Noticias: vigiladas, ninguna relevante esta semana")
        stats = st.get("signal_stats") or {}
        if stats:
            blocked = ", ".join(f"{k}: {v}" for k, v in (stats.get("blocked") or {}).items()) or "ninguna"
            _add(sec, "info", f"    Señales desde que arrancó: {stats.get('signals', 0)}, operadas "
                              f"{stats.get('opened', 0)}; bloqueadas: {blocked}")
    if not profiles:
        _add(sec, "info", "No hay símbolos configurados")
    return sec


def build_report(mt5c: Any, gcfg: dict[str, Any], profiles: list[dict[str, Any]], cache_path: Path,
                 now: float | None = None, calendar: nf.NewsCalendar | None = None) -> list[dict[str, Any]]:
    """
    :param profiles: [{"name", "cfg" (configuración completa), "status", "evolved", "live"}]
                     de los símbolos ACTIVOS.
    """
    now = time.time() if now is None else now
    magics = {int(p["cfg"]["magic"]) for p in profiles}
    symbols = sorted({p["cfg"]["symbol"] for p in profiles})
    report = [check_account(mt5c)]
    for fn in (lambda: check_news(mt5c, gcfg, symbols, cache_path, now, calendar),
               lambda: check_correlation(mt5c, gcfg, profiles, magics),
               lambda: check_bots(profiles)):
        try:
            report.append(fn())
        except Exception as exc:  # noqa: BLE001 - un fallo en una parte no oculta las demás
            sec = _section("Error en el diagnóstico")
            _add(sec, "bad", str(exc))
            report.append(sec)
    return report


def report_text(report: list[dict[str, Any]]) -> str:
    """Informe en texto plano (para copiar y pegar)."""
    out = [f"Diagnóstico MT5 Bot · {datetime.now():%Y-%m-%d %H:%M}"]
    for sec in report:
        out.append("")
        out.append(f"== {sec['title']} ==")
        out += [f"{LEVEL_MARK.get(level, '   ')} {text}" for level, text in sec["lines"]]
    return "\n".join(out)


__all__ = ["build_report", "report_text", "check_account", "check_news", "check_correlation", "check_bots"]
