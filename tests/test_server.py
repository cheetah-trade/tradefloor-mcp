"""The MCP surface: what a client sees, and what it must never see."""

from __future__ import annotations

import json

import pytest

from tradefloor_mcp.server import mcp

EXPECTED_TOOLS = {"analyze_structure", "find_liquidity_sweeps", "find_blocks"}


async def _call(name: str, args: dict) -> dict:
    result = await mcp.call_tool(name, args)
    return json.loads(result.content[0].text)


@pytest.mark.asyncio
async def test_exactly_three_tools_are_published() -> None:
    """The contract the README promises semver on. Adding one is a minor
    release; changing one of these three is a major."""
    assert {t.name for t in await mcp.list_tools()} == EXPECTED_TOOLS


@pytest.mark.asyncio
async def test_every_tool_is_described_for_a_model() -> None:
    """A model picks a tool by its name and description. Blank ones do not get
    picked, and vague ones get picked wrongly."""
    for tool in await mcp.list_tools():
        assert tool.description and len(tool.description) > 120, tool.name


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(EXPECTED_TOOLS))
async def test_no_arguments_analyses_the_bundled_sample(name: str) -> None:
    """Installed and immediately useful: no key, no account, no network."""
    payload = await _call(name, {})
    assert payload["input"]["source"] == "bundled_sample"
    assert payload["input"]["candles"] == 6000
    assert payload["summary"]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(EXPECTED_TOOLS))
async def test_caller_supplied_candles_are_used_verbatim(name: str) -> None:
    rows = [
        {"t": 1000 + i * 60, "o": 10 + i, "h": 12 + i, "l": 9 + i, "c": 11 + i}
        for i in range(80)
    ]
    payload = await _call(name, {"candles": rows})
    assert payload["input"] == {"source": "caller", "candles": 80}


@pytest.mark.asyncio
async def test_bad_input_returns_a_readable_reason_not_a_traceback() -> None:
    """The model has to relay this to a person, so it must be a sentence."""
    payload = await _call("analyze_structure", {"candles": [{"o": 1}]})
    assert payload["error_type"] == "ValueError"
    assert "at least 5 candles" in payload["error"]
    assert "Traceback" not in payload["error"]


@pytest.mark.asyncio
async def test_server_instructions_point_at_the_door_without_selling() -> None:
    """The upgrade path belongs here, where a person reads it, and nowhere in
    tool output, which a model reads."""
    assert mcp.instructions
    assert "https://tradefloor.co" in mcp.instructions
    # never an endpoint that can be switched off: a 404 at the moment of
    # highest interest is the worst possible place to fail.
    assert "/api/agent" not in mcp.instructions
    assert "never contacts TradeFloor" in mcp.instructions


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(EXPECTED_TOOLS))
async def test_summary_always_names_what_was_analysed(name: str) -> None:
    """The summary is the line a model quotes verbatim.

    Someone who asked about BTC and is handed the bundled ETH sample has been
    told something false about a market. So the instrument leads the sentence,
    and the sample says out loud that it is the sample.
    """
    sample = await _call(name, {})
    assert sample["summary"].startswith("BUNDLED ETH 15m SAMPLE")
    assert sample["analysed"] == "BUNDLED ETH 15m SAMPLE (no coin was given)"

    rows = [
        {"t": 1000 + i * 60, "o": 10 + i, "h": 12 + i, "l": 9 + i, "c": 11 + i}
        for i in range(80)
    ]
    mine = await _call(name, {"candles": rows})
    assert mine["summary"].startswith("Your candles:")
