"""Configuración de pytest: añade la raíz del proyecto al sys.path."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def synthetic_ohlc() -> pd.DataFrame:
    """Serie OHLC sintética (tendencias cíclicas + ruido) de 2000 velas H1."""
    rng = np.random.default_rng(42)
    n = 2000
    ret = rng.normal(0.0, 0.001, n) + 0.0003 * np.sin(np.arange(n) / 35.0)
    close = 1.10 * np.exp(np.cumsum(ret))
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) + rng.uniform(0, 0.0008, n)
    low = np.minimum(open_, close) - rng.uniform(0, 0.0008, n)
    return pd.DataFrame({
        "time": pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC"),
        "open": open_, "high": high, "low": low, "close": close,
    })


@pytest.fixture(autouse=True)
def _no_network_news(monkeypatch: pytest.MonkeyPatch) -> None:
    """Las pruebas nunca descargan el calendario de noticias real."""
    import news_filter

    def _offline(url: str, timeout: float = 15.0) -> list:
        raise OSError("sin red en las pruebas")

    monkeypatch.setattr(news_filter, "_default_fetch", _offline)
