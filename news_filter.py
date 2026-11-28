"""
news_filter.py
==============
Filtro de noticias económicas: evita abrir operaciones alrededor de las noticias de
alto (o medio) impacto de las divisas del símbolo.

Fuente: el calendario semanal público de Forex Factory en JSON
(``nfs.faireconomy.media/ff_calendar_thisweek.json``; la API de Python de MT5 no
expone el calendario económico de MetaTrader). El archivo se descarga como mucho
cada ``refresh_hours`` y se guarda en ``logs/news_calendar.json``, compartido por
todos los bots (un solo acceso a internet para todos). Si no se puede descargar se
usa la última copia guardada; si no hay ninguna (o tiene más de 8 días), el filtro
NO bloquea (el bot sigue operando) y lo indica en su estado.

Sin dependencias de MetaTrader 5: se puede probar sin terminal.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from bot_common import write_json_atomic

logger = logging.getLogger("news_filter")

DEFAULT_NEWS_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
#: Divisas que publica el calendario (las demás, p. ej. XAU, no tienen noticias propias).
CALENDAR_CURRENCIES: frozenset[str] = frozenset(
    {"USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF", "CNY"}
)
#: Nivel configurado -> impactos del calendario que bloquean.
IMPACT_LEVELS: dict[str, tuple[str, ...]] = {
    "alto": ("High",),
    "medio": ("High", "Medium"),
}
IMPACT_NAMES: dict[str, str] = {"High": "alto", "Medium": "medio", "Low": "bajo", "Holiday": "festivo"}
#: Una copia del calendario más antigua que esto ya no se usa.
MAX_CALENDAR_AGE_SECONDS = 8 * 86400
#: Tras un fallo de descarga no se reintenta antes de estos minutos (el servidor limita).
RETRY_MINUTES = 30
USER_AGENT = "MT5Bot-news/1.0"


@dataclass(frozen=True)
class NewsEvent:
    """Una noticia del calendario (hora en UTC, segundos epoch)."""

    ts: float
    currency: str
    impact: str
    title: str

    def local_text(self) -> str:
        """Hora local del PC, divisa, impacto y título, p. ej. '14:30 USD Non-Farm (alto)'."""
        when = datetime.fromtimestamp(self.ts)
        return f"{when:%d/%m %H:%M} {self.currency} {self.title} ({IMPACT_NAMES.get(self.impact, self.impact)})"

    def to_dict(self, now: float | None = None, blocking: bool = False) -> dict[str, Any]:
        """Versión serializable para el estado del bot."""
        now = time.time() if now is None else now
        return {"time": datetime.fromtimestamp(self.ts).isoformat(timespec="minutes"),
                "currency": self.currency, "impact": IMPACT_NAMES.get(self.impact, self.impact),
                "title": self.title, "minutes": round((self.ts - now) / 60), "blocking": blocking}


def parse_events(raw: Iterable[dict[str, Any]]) -> list[NewsEvent]:
    """Convierte las entradas del JSON del calendario; ignora las mal formadas."""
    events: list[NewsEvent] = []
    for item in raw or []:
        try:
            when = datetime.fromisoformat(str(item["date"]).replace("Z", "+00:00"))
            if when.tzinfo is None:  # sin zona: no se puede situar con seguridad
                continue
            events.append(NewsEvent(ts=when.timestamp(), currency=str(item.get("country", "")).upper(),
                                    impact=str(item.get("impact", "")), title=str(item.get("title", "")).strip()))
        except (KeyError, TypeError, ValueError):
            continue
    events.sort(key=lambda e: e.ts)
    return events


def impacts_for(level: str) -> tuple[str, ...]:
    """Impactos que bloquean según el nivel configurado ('alto' o 'medio')."""
    return IMPACT_LEVELS.get(str(level).lower(), IMPACT_LEVELS["alto"])


def _relevant(event: NewsEvent, currencies: Iterable[str], impacts: Iterable[str]) -> bool:
    cur = {c.upper() for c in currencies}
    return event.impact in set(impacts) and (event.currency in cur or event.currency == "ALL")


def find_blocking(events: Iterable[NewsEvent], currencies: Iterable[str], now: float,
                  before_minutes: float, after_minutes: float,
                  impacts: Iterable[str] = ("High",)) -> NewsEvent | None:
    """
    Noticia relevante cuya ventana [hora − antes, hora + después] contiene ``now``
    (la más próxima), o None.
    """
    currencies, impacts = list(currencies), list(impacts)
    hits = [e for e in events if _relevant(e, currencies, impacts)
            and e.ts - before_minutes * 60 <= now <= e.ts + after_minutes * 60]
    return min(hits, key=lambda e: abs(e.ts - now)) if hits else None


def next_event(events: Iterable[NewsEvent], currencies: Iterable[str], now: float,
               after_minutes: float = 0.0, impacts: Iterable[str] = ("High",)) -> NewsEvent | None:
    """Próxima noticia relevante (incluida la que acaba de pasar dentro de ``after_minutes``)."""
    currencies, impacts = list(currencies), list(impacts)
    upcoming = [e for e in events if _relevant(e, currencies, impacts) and e.ts + after_minutes * 60 >= now]
    return upcoming[0] if upcoming else None


def _default_fetch(url: str, timeout: float = 15.0) -> list[dict[str, Any]]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310 - URL configurada
        data = json.loads(resp.read().decode("utf-8"))
    if not isinstance(data, list):
        raise ValueError("el calendario no devolvió una lista")
    return data


class NewsCalendar:
    """
    Calendario con caché en disco compartida entre procesos.

    :param fetcher: función ``url -> lista de dicts`` (inyectable para pruebas).
    """

    def __init__(self, cache_path: Path, url: str = DEFAULT_NEWS_URL, refresh_hours: float = 6.0,
                 fetcher: Callable[[str], list[dict[str, Any]]] | None = None) -> None:
        self.cache_path = Path(cache_path)
        self.url = url or DEFAULT_NEWS_URL
        self.refresh_hours = float(refresh_hours)
        self.fetcher = fetcher or _default_fetch
        self.last_error = ""
        self._events: list[NewsEvent] = []
        self._loaded_mtime: float | None = None

    def _read_cache(self) -> dict[str, Any]:
        try:
            return json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def events(self, now: float | None = None) -> list[NewsEvent]:
        """Eventos vigentes; descarga una copia nueva si toca (nunca lanza excepciones)."""
        now = time.time() if now is None else now
        cache = self._read_cache()
        fetched = float(cache.get("fetched_at") or 0.0)
        attempted = float(cache.get("attempt_at") or 0.0)
        stale = now - fetched >= self.refresh_hours * 3600 or cache.get("url") != self.url
        if stale and now - attempted >= RETRY_MINUTES * 60:
            cache = self._refresh(cache, now)
            fetched = float(cache.get("fetched_at") or 0.0)
        self.last_error = str(cache.get("error") or "")
        if not fetched or now - fetched > MAX_CALENDAR_AGE_SECONDS:
            if not self.last_error:
                self.last_error = "calendario no descargado todavía"
            return []
        try:
            mtime = self.cache_path.stat().st_mtime
        except OSError:
            mtime = None
        if mtime != self._loaded_mtime:
            self._events = parse_events(cache.get("events") or [])
            self._loaded_mtime = mtime
        return self._events

    def _refresh(self, cache: dict[str, Any], now: float) -> dict[str, Any]:
        # Se marca el intento ANTES de descargar: los demás bots, que leen la misma caché,
        # no lanzan su propia descarga a la vez (el servidor limita las peticiones).
        write_json_atomic(self.cache_path, {**cache, "attempt_at": now})
        try:
            raw = self.fetcher(self.url)
            new = {"url": self.url, "fetched_at": now, "attempt_at": now, "error": "", "events": raw}
            logger.info("Calendario de noticias actualizado: %d eventos.", len(raw))
        except Exception as exc:  # noqa: BLE001 - sin internet o servidor caído: se sigue con la copia
            new = {**cache, "attempt_at": now, "error": f"no se pudo descargar el calendario: {exc}"}
            logger.warning("No se pudo descargar el calendario de noticias (%s); se usa la copia guardada "
                           "si existe.", exc)
        if not write_json_atomic(self.cache_path, new):
            logger.debug("No se pudo guardar la caché del calendario")
        return new


__all__ = [
    "DEFAULT_NEWS_URL", "CALENDAR_CURRENCIES", "IMPACT_LEVELS", "NewsEvent", "NewsCalendar",
    "parse_events", "impacts_for", "find_blocking", "next_event",
]
