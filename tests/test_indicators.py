import math

import numpy as np
import pandas as pd
import pytest

from app import indicators as ind
from tests.conftest import make_ohlcv


def test_sma_matches_hand_computed_values():
    s = pd.Series([1.0, 2, 3, 4, 5])
    out = ind.sma(s, 3)
    assert out.iloc[:2].isna().all()
    assert out.iloc[2:].tolist() == [2.0, 3.0, 4.0]


def test_ema_follows_recursive_definition():
    s = pd.Series(np.linspace(10, 30, 40))
    n = 10
    alpha = 2 / (n + 1)
    expected = [s.iloc[0]]
    for x in s.iloc[1:]:
        expected.append(alpha * x + (1 - alpha) * expected[-1])
    out = ind.ema(s, n)
    assert out.iloc[: n - 1].isna().all()
    np.testing.assert_allclose(out.iloc[n - 1:], expected[n - 1:])


def _wilder_rsi_reference(closes, n=14):
    out = [math.nan] * len(closes)
    deltas = np.diff(closes)
    # pandas ewm(adjust=False) seeds with the first delta rather than an n-bar mean
    avg_g, avg_l = max(deltas[0], 0), max(-deltas[0], 0)
    for i, d in enumerate(deltas[1:], start=2):
        avg_g = avg_g + (max(d, 0) - avg_g) / n
        avg_l = avg_l + (max(-d, 0) - avg_l) / n
        if i >= n:
            out[i] = 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
    return out


def test_rsi_matches_wilder_reference():
    closes = make_ohlcv(120)["close"]
    out = ind.rsi(closes, 14)
    ref = _wilder_rsi_reference(closes.to_numpy())
    np.testing.assert_allclose(out.iloc[14:], ref[14:], rtol=1e-9)
    assert out.dropna().between(0, 100).all()


def test_rsi_extremes():
    up = pd.Series(np.arange(1.0, 40.0))
    down = pd.Series(np.arange(40.0, 1.0, -1))
    assert ind.rsi(up).iloc[-1] == 100.0
    assert ind.rsi(down).iloc[-1] == pytest.approx(0.0)


def test_macd_components():
    s = make_ohlcv(200)["close"]
    line, sig, hist = ind.macd(s)
    np.testing.assert_allclose(line.dropna(), (ind.ema(s, 12) - ind.ema(s, 26)).dropna())
    np.testing.assert_allclose(hist.dropna(), (line - sig).dropna())


def test_bollinger_bands_use_population_std():
    s = pd.Series([2.0, 4, 4, 4, 5, 5, 7, 9])  # population std of these 8 values is exactly 2
    upper, mid, lower = ind.bollinger(s, n=8, k=2)
    assert mid.iloc[-1] == 5.0
    assert upper.iloc[-1] == 9.0
    assert lower.iloc[-1] == 1.0


def test_series_to_list_cleans_nan_and_inf():
    assert ind.series_to_list(pd.Series([1.234567, np.nan, np.inf, None])) == [1.2346, None, None, None]


def test_compute_all_shapes_and_signals():
    df = make_ohlcv(400)
    out = ind.compute_all(df)
    expected_keys = {"sma20", "sma50", "sma200", "ema12", "ema26", "rsi14", "macd", "macd_signal",
                     "macd_hist", "bb_upper", "bb_mid", "bb_lower"}
    assert set(out["series"]) == expected_keys
    assert all(len(v) == len(df) for v in out["series"].values())
    assert out["latest"]["price"] == round(df["close"].iloc[-1], 4)
    names = {s["indicator"] for s in out["signals"]}
    assert {"SMA 20", "SMA 200", "50/200 Cross", "RSI 14", "MACD Hist", "Bollinger %B"} <= names
    assert all(s["signal"] in {"bullish", "bearish", "neutral", "n/a"} for s in out["signals"])


def test_compute_all_with_short_history_marks_sma200_na():
    out = ind.compute_all(make_ohlcv(60))
    sma200 = next(s for s in out["signals"] if s["indicator"] == "SMA 200")
    assert sma200["signal"] == "n/a"
    assert not any(s["indicator"] == "50/200 Cross" for s in out["signals"])


def test_performance_returns():
    idx = pd.bdate_range("2025-01-01", "2026-10-20")
    close = pd.Series(100.0, index=idx)
    close.iloc[-1] = 110.0
    df = pd.DataFrame({"open": close, "high": close, "low": close, "close": close, "volume": 1.0})
    perf = ind.performance(df)
    for k in ("1W", "1M", "1Y", "YTD"):
        assert perf[k] == pytest.approx(10.0)
    assert perf["max_drawdown_1y"] == 0.0
