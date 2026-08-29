"""The engine here must be the engine that runs in production.

The bundled sample is the exact fixture TradeFloor's own suite pins its market
structure against. If these numbers move, either this package drifted from the
monorepo or the monorepo changed its answer. Both are worth failing over.
"""

from __future__ import annotations

from tradefloor_mcp import candles
from tradefloor_mcp.engine.mss_sweeps import detect_sweeps, structure_events

# Frozen upstream: 6000 ETH-15m candles -> 50 structural events -> 23 sweeps.
GOLDEN_CANDLES = 6000
GOLDEN_EVENTS = 50
GOLDEN_SWEEPS = 23
GOLDEN_BULLISH = 11
GOLDEN_BEARISH = 12


def test_sample_is_the_pinned_window() -> None:
    assert len(candles.load_sample()) == GOLDEN_CANDLES


def test_engine_reproduces_the_frozen_answer() -> None:
    rows = candles.load_sample()
    events = structure_events(rows)
    sweeps = detect_sweeps(rows, events)

    assert len(events) == GOLDEN_EVENTS
    assert len(sweeps) == GOLDEN_SWEEPS
    bullish = sum(1 for s in sweeps if s["direction"] == "long")
    assert (bullish, len(sweeps) - bullish) == (GOLDEN_BULLISH, GOLDEN_BEARISH)


def test_production_kill_switch_is_not_shipped(monkeypatch) -> None:
    """``MSS_SWEEP_ENABLED`` gates the chart upstream. Here it must mean nothing."""
    import tradefloor_mcp.engine.mss_sweeps as mod

    assert not hasattr(mod, "_is_enabled")
    monkeypatch.setenv("MSS_SWEEP_ENABLED", "false")
    rows = candles.load_sample()
    assert len(detect_sweeps(rows, structure_events(rows))) == GOLDEN_SWEEPS
