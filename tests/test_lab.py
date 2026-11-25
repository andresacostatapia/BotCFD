"""Tests del laboratorio (lab.py) con datos sintéticos y MT5 simulado."""

from __future__ import annotations

import json

import pandas as pd
import pytest

import lab
import mt5_connector
from tests.fake_mt5 import FakeMT5

CFG = {"population": 6, "generations": 2, "history_bars": 1500, "validation_windows": 2,
       "min_trades": 5, "min_trades_oos": 2}


def _config() -> dict:
    from bot_common import DEFAULT_CONFIG
    return {**DEFAULT_CONFIG, **CFG}


def test_run_lab_serial_sorted(synthetic_ohlc: pd.DataFrame) -> None:
    data = {("EURUSD", "H1"): synthetic_ohlc}
    jobs = [("EURUSD", "H1", s) for s in ("cruce_medias", "reversion_media", "ruptura")]
    jobs.append(("GBPUSD", "H1", "ruptura"))  # sin datos -> se omite
    seen = []
    results = lab.run_lab(jobs, data, _config(), {"EURUSD": 0.0001}, workers=1,
                          on_result=lambda r, d, n: seen.append((d, n)))
    assert len(results) == 3 and seen[-1] == (3, 3)
    assert [lab.score(r) for r in results] == sorted((lab.score(r) for r in results), reverse=True)
    r = results[0]
    assert {"symbol", "timeframe", "strategy", "accepted", "pf_oos", "trades_oos", "windows",
            "params", "spread"} <= set(r)
    assert r["spread"] == 0.0001 and r["windows"].endswith("/2")


def test_run_lab_parallel_matches_serial(synthetic_ohlc: pd.DataFrame) -> None:
    data = {("EURUSD", "H1"): synthetic_ohlc}
    jobs = [("EURUSD", "H1", "ruptura"), ("EURUSD", "H1", "cruce_medias")]
    cfg = {**_config(), "evolver_seed": 3}
    serial = lab.run_lab(jobs, data, cfg, workers=1)
    parallel = lab.run_lab(jobs, data, cfg, workers=2)
    key = lambda rs: sorted((r["strategy"], r["pf_oos"], r["trades_oos"]) for r in rs)  # noqa: E731
    assert key(serial) == key(parallel)


def test_run_lab_stops(synthetic_ohlc: pd.DataFrame) -> None:
    data = {("EURUSD", "H1"): synthetic_ohlc}
    jobs = [("EURUSD", "H1", s) for s in ("cruce_medias", "reversion_media", "ruptura")]
    results = lab.run_lab(jobs, data, _config(), workers=1, should_stop=lambda: True)
    assert results == []


def test_score_prefers_accepted() -> None:
    good = {"accepted": True, "pf_oos": 1.2, "trades_oos": 30}
    great_but_rejected = {"accepted": False, "pf_oos": 4.0, "trades_oos": 30}
    assert lab.score(good) > lab.score(great_but_rejected)


def test_lab_main_with_fake_mt5(monkeypatch, tmp_path, synthetic_ohlc: pd.DataFrame, capsys) -> None:
    fake = FakeMT5(synthetic_ohlc)
    monkeypatch.setattr(mt5_connector, "mt5", fake)
    monkeypatch.setattr(mt5_connector, "_connected", False)
    monkeypatch.setattr(lab, "load_global_config", lambda base: _config())
    monkeypatch.setattr(lab, "STOP_FLAG", tmp_path / "stop_lab.flag")
    out = tmp_path / "lab.json"
    code = lab.main(["--symbols", "EURUSD,XAUXXX", "--timeframes", "H1", "--strategies",
                     "ruptura,cruce_medias", "--bars", "1500", "--workers", "1", "--out", str(out)])
    assert code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert len(payload["results"]) == 2 and payload["finished"]
    assert any("XAUXXX" in p for p in payload["problems"])
    printed = capsys.readouterr().out
    assert printed.count("LAB_RESULT ") == 2 and '"stage": "fin"' in printed
    assert not mt5_connector.is_connected()


def test_lab_main_rejects_bad_args() -> None:
    assert lab.main(["--symbols", "EURUSD", "--timeframes", "D7"]) == 2
    with pytest.raises(SystemExit):
        lab.main([])
