"""The MCP server: three tools over stdio.

Every tool takes candles the same way, so the model only has to learn it once:

* pass ``coin`` (and optionally ``interval``) and they are fetched from
  Hyperliquid's public endpoint, from this machine's own IP;
* pass ``candles`` and yours are used verbatim, from any venue at all;
* pass neither and the bundled ETH-15m sample is analysed, so the server is
  useful the moment it is installed.

Tool output is data and nothing else. No upsell text is injected into it: that
channel is read by a model, sometimes by a model that is also holding tools
which can open positions, and putting marketing there is exactly the practice
this project defends against elsewhere.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

from mcp.server.mcpserver import MCPServer

from tradefloor_mcp import __version__, glossary, tools
from tradefloor_mcp import candles as candle_source

INSTRUCTIONS = """\
Reads smart-money market structure from candles: trend, ChoCh/BOS breaks,
liquidity sweeps, fair value gaps, order blocks and Fibonacci pullback zones.

This is the analysis engine that runs in production at TradeFloor. It is
offline by default and never contacts TradeFloor.

Nothing here places an order, holds a key, or gives advice. Live signals and
execution with server-side risk limits are a separate, account-scoped product:
https://tradefloor.co\
"""

# stdout IS the protocol transport on stdio. Anything that prints there
# corrupts the JSON-RPC stream, so every log line is pinned to stderr and the
# HTTP client is quietened to warnings.
logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
for _noisy in ("httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

mcp = MCPServer("tradefloor", version=__version__, instructions=INSTRUCTIONS)


def _resolve(
    coin: str | None, interval: str, limit: int, candles: list[dict] | None
) -> tuple[list[dict], dict[str, Any]]:
    """Turn the three input shapes into one candle list, plus its provenance."""
    if candles:
        rows = candle_source.normalize(candles)
        return rows, {"source": "caller", "candles": len(rows)}
    if coin:
        rows = candle_source.fetch_hl_candles(coin, interval, limit)
        return rows, {
            "source": "hyperliquid_public",
            "coin": coin,
            "interval": interval,
            "candles": len(rows),
            "note": "Fetched from this machine, direct to Hyperliquid. TradeFloor is not involved.",
        }
    rows = candle_source.load_sample()
    return rows, {
        "source": "bundled_sample",
        "coin": "ETH",
        "interval": "15m",
        "candles": len(rows),
        "note": "No coin or candles given, so the bundled ETH-15m sample was analysed.",
    }


def _label(provenance: dict[str, Any]) -> str:
    """What was actually analysed, as a prefix.

    The summary is the line a model is most likely to quote back verbatim, and
    without this it reads as an answer about whatever the person asked. Someone
    who asked about BTC and is shown the bundled ETH sample has been told
    something false about a market, which is not a cosmetic failure.
    """
    source = provenance.get("source")
    if source == "caller":
        return "Your candles"
    if source == "bundled_sample":
        return "BUNDLED ETH 15m SAMPLE (no coin was given)"
    return f"{provenance.get('coin')} {provenance.get('interval')}"


#: Enough to draw a readable chart, few enough to stay inside a reply. Drawing
#: is the point of shipping candles at all, and nobody reads 5000 of them.
_CHART_CANDLES = 400


def _for_drawing(rows: list[dict]) -> dict[str, Any]:
    """Candles in the most compact honest form: rows of [time, o, h, l, c]."""
    tail = rows[-_CHART_CANDLES:]
    return {
        "format": "[open_time_ms, open, high, low, close]",
        "note": (
            f"Last {len(tail)} of {len(rows)} candles, for drawing. Levels and "
            f"blocks elsewhere in this response carry times on the same scale."
            if len(tail) < len(rows)
            else f"All {len(tail)} candles, for drawing."
        ),
        "rows": [[c["t"], c["o"], c["h"], c["l"], c["c"]] for c in tail],
    }


def _run(fn, coin, interval, limit, candles, include_candles=False) -> str:
    try:
        rows, provenance = _resolve(coin, interval, limit, candles)
        result = fn(rows)
        result["analysed"] = _label(provenance)
        result["summary"] = f"{result['analysed']}: {result['summary']}"
        result["input"] = provenance
        # Terms a model would otherwise have to guess at. Only the ones this
        # answer actually uses.
        result["glossary"] = glossary.for_payload(result)
        if include_candles:
            result["candles_for_chart"] = _for_drawing(rows)
        return json.dumps(result, indent=2, default=str)
    except Exception as exc:  # noqa: BLE001 - the model gets to read the reason
        return json.dumps({"error": str(exc), "error_type": type(exc).__name__}, indent=2)


@mcp.tool()
def analyze_structure(
    coin: str | None = None,
    interval: str = "1h",
    limit: int = 1000,
    candles: list[dict] | None = None,
    include_candles: bool = False,
) -> str:
    """Read market structure: trend, character shifts (ChoCh) and continuation breaks (BOS).

    Returns the current trend, every structural break in the window with its
    time and price, the last validated swing high and low, the potential swing
    levels a pullback is forming against, and the impulse peak or trough of the
    current leg.

    Give a coin (BTC, ETH, SOL; tokenised equities as xyz:AAPL) to fetch candles
    from Hyperliquid's public endpoint, or pass your own candles, or neither to
    see a worked example on bundled ETH-15m data.

    Set include_candles=true when you intend to DRAW the result: the response
    then carries the candles themselves, on the same time scale as the levels.
    """
    return _run(tools.analyze_structure, coin, interval, limit, candles, include_candles)


@mcp.tool()
def find_liquidity_sweeps(
    coin: str | None = None,
    interval: str = "1h",
    limit: int = 1000,
    candles: list[dict] | None = None,
    include_candles: bool = False,
) -> str:
    """Find stop-hunts: breaks that first ran the liquidity resting past a level.

    A sweep is a structural break whose candle pushed through one or more prior
    swing levels before closing back inside. Each result names the sweeping
    candle and every level it took out, which is what separates an engineered
    break from a clean one.

    Same input options as analyze_structure.
    """
    return _run(tools.find_liquidity_sweeps, coin, interval, limit, candles, include_candles)


@mcp.tool()
def find_blocks(
    coin: str | None = None,
    interval: str = "1h",
    limit: int = 1000,
    candles: list[dict] | None = None,
    include_candles: bool = False,
) -> str:
    """Find fair value gaps, order blocks and the pullback zones holding them.

    Reports blocks on the current impulse leg, then again per Fibonacci pullback
    zone (discount, equilibrium, premium, golden pocket), and says which zones
    price is sitting in right now.

    Same input options as analyze_structure.
    """
    return _run(tools.find_blocks, coin, interval, limit, candles, include_candles)


def main() -> None:
    mcp.run()
