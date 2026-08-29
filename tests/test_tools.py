"""The three tools, and the two promises the README makes about them."""

from __future__ import annotations

import json

import pytest

from tradefloor_mcp import candles, tools

ALL_TOOLS = (tools.analyze_structure, tools.find_liquidity_sweeps, tools.find_blocks)


@pytest.fixture(scope="module")
def sample() -> list[dict]:
    return candles.load_sample()


@pytest.mark.parametrize("fn", ALL_TOOLS, ids=lambda f: f.__name__)
def test_output_is_json_serialisable_and_carries_its_window(fn, sample) -> None:
    result = fn(sample)
    json.dumps(result, default=str)
    assert result["window"]["candles_analyzed"] == len(sample)
    assert result["summary"]


def test_structure_reports_breaks_in_time_not_index(sample) -> None:
    """Indices only mean something inside one window; open times travel."""
    events = tools.analyze_structure(sample)["breaks"]["events"]
    assert events
    first, last = sample[0]["t"], sample[-1]["t"]
    for ev in events:
        assert first <= ev["time"] <= last
        assert ev["type"] in {"ChoCh", "BOS"}
        assert ev["direction"] in {"up", "down"}


def test_choch_and_bos_are_reported_separately(sample) -> None:
    """Conflating a character shift with a continuation break loses the signal."""
    breaks = tools.analyze_structure(sample)["breaks"]
    assert breaks["choch_count"] > 0
    assert breaks["bos_count"] > 0
    events = breaks["events"]
    assert sum(1 for e in events if e["type"] == "ChoCh") == breaks["choch_count"]
    assert sum(1 for e in events if e["type"] == "BOS") == breaks["bos_count"]


def test_sweeps_match_the_frozen_answer(sample) -> None:
    counts = tools.find_liquidity_sweeps(sample)["counts"]
    assert counts == {"total": 23, "bullish": 11, "bearish": 12}


def test_blocks_report_zone_membership(sample) -> None:
    blocks = tools.find_blocks(sample)
    assert blocks["trend"] in {"long", "short", "neutral"}
    for name, zone in blocks["zones"].items():
        if zone["price_is_inside"]:
            assert name in blocks["price_currently_in"]
        assert zone["low"] is None or zone["low"] <= zone["high"]


@pytest.mark.parametrize("fn", ALL_TOOLS, ids=lambda f: f.__name__)
def test_tools_are_pure_and_do_not_touch_the_network(fn, sample, monkeypatch) -> None:
    """A tool given candles must never reach for a socket."""
    import socket

    def _boom(*_a, **_k):
        raise AssertionError("tool attempted a network call")

    monkeypatch.setattr(socket, "socket", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)
    assert fn(sample)["summary"]


@pytest.mark.parametrize("fn", ALL_TOOLS, ids=lambda f: f.__name__)
def test_tool_output_carries_no_marketing(fn, sample) -> None:
    """Tool output is read by a model, sometimes one holding trading tools.

    Putting an upsell in that channel is the practice this project defends
    against elsewhere, so the absence is pinned rather than merely intended.
    """
    blob = json.dumps(fn(sample), default=str).lower()
    for banned in ("tradefloor", "http://", "https://", "sign up", "upgrade", "account"):
        assert banned not in blob, f"{fn.__name__} output leaked {banned!r}"


@pytest.mark.parametrize("fn", ALL_TOOLS, ids=lambda f: f.__name__)
def test_tools_do_not_mutate_their_input(fn, sample) -> None:
    before = json.dumps(sample, default=str)
    fn(sample)
    assert json.dumps(sample, default=str) == before
