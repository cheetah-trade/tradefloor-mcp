"""Candle intake: both key styles, and a loud failure on anything malformed."""

from __future__ import annotations

import pytest

from tradefloor_mcp import candles


def _rows(n: int = 6) -> list[dict]:
    return [
        {"t": 1000 + i, "o": 10 + i, "h": 11 + i, "l": 9 + i, "c": 10.5 + i}
        for i in range(n)
    ]


def test_compact_and_verbose_keys_agree() -> None:
    verbose = [
        {"time": r["t"], "open": r["o"], "high": r["h"], "low": r["l"], "close": r["c"]}
        for r in _rows()
    ]
    assert candles.normalize(_rows()) == candles.normalize(verbose)


def test_string_prices_are_accepted() -> None:
    """Hyperliquid sends prices as strings; so does our own fixture."""
    rows = [{k: (str(v) if k != "t" else v) for k, v in r.items()} for r in _rows()]
    assert candles.normalize(rows)[0]["h"] == 11.0


@pytest.mark.parametrize(
    ("bad", "because"),
    [
        ([], "empty"),
        (_rows(3), "too few to say anything"),
        ([{"o": 1, "h": 2, "l": 1}] * 6, "missing close"),
        ([{"o": 1, "h": "abc", "l": 1, "c": 1}] * 6, "unreadable price"),
    ],
)
def test_malformed_input_raises_instead_of_reading_as_zero(bad, because) -> None:
    """A zero high is not a degenerate answer, it is a wrong one."""
    with pytest.raises((TypeError, ValueError)):
        candles.normalize(bad)


def test_inverted_candle_is_rejected() -> None:
    rows = _rows()
    rows[2]["h"], rows[2]["l"] = rows[2]["l"], rows[2]["h"]
    with pytest.raises(ValueError, match="high < low"):
        candles.normalize(rows)


def test_rate_limiter_refuses_before_hammering_the_exchange() -> None:
    throttle = candles._Throttle()
    for _ in range(candles._MAX_REQUESTS):
        throttle.take()
    with pytest.raises(candles.RateLimited):
        throttle.take()


def test_unknown_interval_is_refused_without_a_request() -> None:
    with pytest.raises(ValueError, match="not one Hyperliquid serves"):
        candles.fetch_hl_candles("BTC", "7m")
