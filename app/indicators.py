"""Pure pandas/numpy technical indicators. Provider-independent: they operate
on a normalized OHLCV DataFrame with columns open/high/low/close/volume."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(window=n, min_periods=n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def rsi(s: pd.Series, n: int = 14) -> pd.Series:
    """Wilder's RSI."""
    delta = s.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    avg_loss = loss.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.where(avg_loss != 0, 100.0)


def macd(s: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    line = ema(s, fast) - ema(s, slow)
    sig = line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return line, sig, line - sig


def bollinger(s: pd.Series, n: int = 20, k: float = 2.0):
    mid = sma(s, n)
    std = s.rolling(window=n, min_periods=n).std(ddof=0)
    return mid + k * std, mid, mid - k * std


def _clean(v):
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else round(f, 4)


def series_to_list(s: pd.Series) -> list:
    return [_clean(v) for v in s.tolist()]


def compute_all(df: pd.DataFrame) -> dict:
    """Returns full indicator series (aligned to df index) plus a latest-value
    snapshot and derived human-readable signals."""
    close = df["close"]
    series = {
        "sma20": sma(close, 20),
        "sma50": sma(close, 50),
        "sma200": sma(close, 200),
        "ema12": ema(close, 12),
        "ema26": ema(close, 26),
        "rsi14": rsi(close, 14),
    }
    m_line, m_sig, m_hist = macd(close)
    series.update(macd=m_line, macd_signal=m_sig, macd_hist=m_hist)
    bb_u, bb_m, bb_l = bollinger(close)
    series.update(bb_upper=bb_u, bb_mid=bb_m, bb_lower=bb_l)

    latest = {k: _clean(v.iloc[-1]) if len(v) else None for k, v in series.items()}
    price = _clean(close.iloc[-1])
    latest["price"] = price

    return {
        "series": {k: series_to_list(v) for k, v in series.items()},
        "latest": latest,
        "signals": derive_signals(latest, series),
    }


def derive_signals(latest: dict, series: dict) -> list[dict]:
    """Translate raw indicator values into labelled signals (bullish/bearish/neutral)."""
    out: list[dict] = []
    p = latest.get("price")

    def add(name, value, verdict, note):
        out.append({"indicator": name, "value": value, "signal": verdict, "note": note})

    for key, label in (("sma20", "SMA 20"), ("sma50", "SMA 50"), ("sma200", "SMA 200")):
        v = latest.get(key)
        if v is None or p is None:
            add(label, None, "n/a", "Insufficient history")
        else:
            pct = (p - v) / v * 100
            add(label, v, "bullish" if p > v else "bearish", f"Price {pct:+.2f}% vs average")

    s50, s200 = latest.get("sma50"), latest.get("sma200")
    if s50 is not None and s200 is not None:
        # Detect a recent cross within the last 10 sessions
        diff = (series["sma50"] - series["sma200"]).dropna().tail(11)
        crossed = len(diff) > 1 and (np.sign(diff.iloc[0]) != np.sign(diff.iloc[-1]))
        if s50 > s200:
            add("50/200 Cross", round(s50 - s200, 2), "bullish",
                "Golden cross (recent)" if crossed else "50 above 200 — uptrend regime")
        else:
            add("50/200 Cross", round(s50 - s200, 2), "bearish",
                "Death cross (recent)" if crossed else "50 below 200 — downtrend regime")

    e12, e26 = latest.get("ema12"), latest.get("ema26")
    if e12 is not None and e26 is not None:
        add("EMA 12/26", round(e12 - e26, 4), "bullish" if e12 > e26 else "bearish",
            "Fast EMA above slow" if e12 > e26 else "Fast EMA below slow")

    r = latest.get("rsi14")
    if r is not None:
        if r >= 70:
            add("RSI 14", r, "bearish", "Overbought (≥70)")
        elif r <= 30:
            add("RSI 14", r, "bullish", "Oversold (≤30)")
        else:
            add("RSI 14", r, "neutral", "Within 30–70 range")

    h = latest.get("macd_hist")
    if h is not None:
        hist = series["macd_hist"].dropna()
        rising = len(hist) > 1 and hist.iloc[-1] > hist.iloc[-2]
        add("MACD Hist", h, "bullish" if h > 0 else "bearish",
            f"{'Above' if h > 0 else 'Below'} zero, momentum {'rising' if rising else 'falling'}")

    u, lo = latest.get("bb_upper"), latest.get("bb_lower")
    if u is not None and lo is not None and p is not None and u != lo:
        pct_b = (p - lo) / (u - lo)
        verdict = "bearish" if pct_b > 1 else "bullish" if pct_b < 0 else "neutral"
        add("Bollinger %B", round(pct_b, 3), verdict,
            f"Band width {((u - lo) / latest['bb_mid'] * 100):.2f}%")
    return out


def performance(df: pd.DataFrame) -> dict:
    """Trailing returns, realized volatility and ATR from daily bars."""
    close = df["close"]
    last = close.iloc[-1]
    out: dict = {}
    for label, days in (("1W", 7), ("1M", 30), ("3M", 91), ("6M", 182), ("1Y", 365)):
        past = close[close.index <= close.index[-1] - pd.Timedelta(days=days)]
        out[label] = _clean((last / past.iloc[-1] - 1) * 100) if len(past) else None
    ytd = close[close.index.year < close.index[-1].year]
    out["YTD"] = _clean((last / ytd.iloc[-1] - 1) * 100) if len(ytd) else None

    rets = np.log(close).diff().dropna()
    out["vol_20d"] = _clean(rets.tail(20).std() * np.sqrt(252) * 100)
    out["vol_1y"] = _clean(rets.tail(252).std() * np.sqrt(252) * 100)
    prev_close = close.shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev_close).abs(),
                    (df["low"] - prev_close).abs()], axis=1).max(axis=1)
    out["atr14"] = _clean(tr.ewm(alpha=1 / 14, adjust=False).mean().iloc[-1])
    peak = close.tail(252).cummax()
    out["max_drawdown_1y"] = _clean(((close.tail(252) / peak) - 1).min() * 100)
    return out
