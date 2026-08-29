"""Two things the model needs that the raw numbers do not carry.

A definition it would otherwise guess at, and the candles it needs if it is
going to draw what it just read.
"""

from __future__ import annotations

import json

import pytest

from tradefloor_mcp import candles, glossary, tools
from tradefloor_mcp.server import _CHART_CANDLES, mcp

EXPECTED_TOOLS = ("analyze_structure", "find_liquidity_sweeps", "find_blocks")


async def _call(name: str, args: dict) -> dict:
    result = await mcp.call_tool(name, args)
    return json.loads(result.content[0].text)


def test_glossary_matches_whole_words_only() -> None:
    """"ote" lives inside "potential_low".

    Attaching a zone definition to an answer with no zones is the same class of
    confusion the glossary exists to remove, so it must not do it itself.
    """
    structure = tools.analyze_structure(candles.load_sample())
    assert "potential_low" in json.dumps(structure, default=str)
    assert "ote" not in glossary.for_payload(structure)


def test_glossary_covers_the_terms_a_model_cannot_guess() -> None:
    """Standard terms it would get right; this one it would not."""
    blocks = tools.find_blocks(candles.load_sample())
    terms = glossary.for_payload(blocks)
    assert "golden_retriever" in terms
    assert "golden pocket" in terms["golden_retriever"]
    assert "0.618" in terms["golden_retriever"]


def test_glossary_is_scoped_to_the_answer() -> None:
    """Every term shipped, and nothing else, so the payload stays the size of
    the answer rather than the size of the vocabulary."""
    sweeps = tools.find_liquidity_sweeps(candles.load_sample())
    terms = glossary.for_payload(sweeps)
    assert "liquidity_sweep" in terms
    assert "golden_retriever" not in terms
    assert len(terms) < len(glossary.TERMS)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", EXPECTED_TOOLS)
async def test_every_answer_carries_its_definitions(name: str) -> None:
    payload = await _call(name, {})
    assert payload["glossary"]


@pytest.mark.asyncio
async def test_candles_are_absent_unless_asked_for() -> None:
    """Drawing data is opt-in: it quadruples the reply and most calls never draw."""
    assert "candles_for_chart" not in await _call("analyze_structure", {})


@pytest.mark.asyncio
@pytest.mark.parametrize("name", EXPECTED_TOOLS)
async def test_chart_candles_share_the_time_scale_of_the_levels(name: str) -> None:
    """A chart is only drawable if the candles and the levels agree on time."""
    payload = await _call(name, {"include_candles": True})
    chart = payload["candles_for_chart"]

    assert len(chart["rows"]) == _CHART_CANDLES
    assert all(len(row) == 5 for row in chart["rows"])
    times = [row[0] for row in chart["rows"]]
    assert times == sorted(times)

    window = payload["window"]
    assert times[-1] == window["to_time"]


@pytest.mark.asyncio
async def test_truncation_is_disclosed_not_silent() -> None:
    """Silently handing back a slice of the analysed window would misdraw it."""
    chart = (await _call("analyze_structure", {"include_candles": True}))["candles_for_chart"]
    assert "of 6000 candles" in chart["note"]
    assert "[open_time_ms" in chart["format"]


def test_touched_and_inside_are_reported_separately() -> None:
    """A wick into a zone is a test of it, and closing back out does not undo that.

    Reporting only "inside" would hide exactly the event a pullback setup waits
    for. This window is one where the last candle closed OUTSIDE every zone but
    reached one, so collapsing the two readings would lose it.
    """
    window = candles.load_sample()[:2900]
    blocks = tools.find_blocks(window)

    assert blocks["price_currently_in"] == []
    assert "discount" in blocks["price_touched_zones"]
    assert blocks["zones"]["discount"]["price_is_inside"] is False
    assert blocks["zones"]["discount"]["price_touched"] is True

    terms = glossary.for_payload(blocks)
    assert "price_touched" in terms and "price_is_inside" in terms
