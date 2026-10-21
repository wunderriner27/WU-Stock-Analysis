"""Lightweight lexicon sentiment tagger for headlines. Swap for an NLP model
(or Alpaca/Benzinga sentiment fields) without touching callers."""
import re

POSITIVE = {
    "beat", "beats", "surge", "surges", "soar", "soars", "jump", "jumps", "rally", "rallies",
    "gain", "gains", "record", "upgrade", "upgrades", "upgraded", "bullish", "strong", "growth",
    "profit", "outperform", "raises", "raise", "boost", "boosts", "rebound", "rebounds", "tops",
    "higher", "rise", "rises", "climb", "climbs", "buy", "optimism", "approval", "approved",
}
NEGATIVE = {
    "miss", "misses", "plunge", "plunges", "drop", "drops", "fall", "falls", "slump", "slumps",
    "downgrade", "downgrades", "downgraded", "bearish", "weak", "loss", "losses", "cut", "cuts",
    "lawsuit", "probe", "investigation", "recall", "layoffs", "lower", "decline", "declines",
    "sell", "selloff", "sell-off", "fears", "warning", "warns", "tumble", "tumbles", "crash",
    "slides", "slide", "risk", "recession", "tariff", "tariffs", "default",
}


def tag(text: str) -> dict:
    words = re.findall(r"[a-z\-]+", (text or "").lower())
    pos = sum(w in POSITIVE for w in words)
    neg = sum(w in NEGATIVE for w in words)
    score = pos - neg
    label = "bullish" if score > 0 else "bearish" if score < 0 else "neutral"
    return {"label": label, "score": score}
