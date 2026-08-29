"""The three tools, as plain functions.

Kept separate from the MCP plumbing so they can be tested, and read, without a
protocol in the way. Everything here is pure: candles in, JSON-shaped dict out.

Index-to-time is done on the way out. Indices are relative to whichever window
the caller happened to pass, so they mean nothing to anyone else; open times
survive being handed around.
"""

from __future__ import annotations

from typing import Any

from tradefloor_mcp.candles import timestamps as _timestamps
from tradefloor_mcp.engine import mss_blocks, mss_chart_blocks, mss_sweeps
from tradefloor_mcp.engine.market_structure_v2 import compute_trend_v2

#: Our engine calls a change of character MSS (market structure shift). Most
#: SMC material calls the same event ChoCh, so both names travel.
_BREAK_NAMES = {"MSS": "ChoCh", "BOS": "BOS"}


def _at(times: list[int], idx: int) -> int | None:
    return times[idx] if 0 <= idx < len(times) else None


def _level(price: float, idx: int, times: list[int]) -> dict | None:
    if idx < 0:
        return None
    return {"price": price, "time": _at(times, idx)}


def _window(candles: list[dict], times: list[int]) -> dict:
    return {
        "candles_analyzed": len(candles),
        "from_time": times[0] if times else None,
        "to_time": times[-1] if times else None,
    }


def analyze_structure(candles: list[dict]) -> dict[str, Any]:
    """Trend, validated swings and every structural break in the window."""
    times = _timestamps(candles)
    r = compute_trend_v2(candles)

    breaks = [
        {
            "type": _BREAK_NAMES.get(m.get("type"), m.get("type")),
            "engine_type": m.get("type"),
            "direction": "up" if m.get("direction") == "up" else "down",
            "price": m.get("price"),
            "time": _at(times, int(m.get("index", -1))),
        }
        for m in r.markers
    ]
    choch = [b for b in breaks if b["engine_type"] == "MSS"]

    return {
        "trend": r.trend,
        "summary": (
            f"{r.trend.upper()} structure over {len(candles)} candles: "
            f"{r.mss_count} character shift(s), {r.bos_count} continuation break(s), "
            f"{r.bos_after_last_mss} break(s) since the last shift."
        ),
        "breaks": {
            "choch_count": r.mss_count,
            "bos_count": r.bos_count,
            "bos_since_last_choch": r.bos_after_last_mss,
            "last_choch": choch[-1] if choch else None,
            "events": breaks,
        },
        "levels": {
            "last_validated_high": _level(r.last_vh, r.last_vh_index, times),
            "last_validated_low": _level(r.last_vl, r.last_vl_index, times),
            "potential_high": _level(r.last_ph, r.last_ph_index, times),
            "potential_low": _level(r.last_pl, r.last_pl_index, times),
            "impulse_peak": _level(r.running_max_high, r.running_max_high_index, times),
            "impulse_trough": _level(r.running_min_low, r.running_min_low_index, times),
        },
        "validated_swings": [
            {
                "type": s.get("type"),
                "price": s.get("price"),
                "time": _at(times, int(s.get("index", -1))),
            }
            for s in r.validated_swings
        ],
        "window": _window(candles, times),
    }


def find_liquidity_sweeps(candles: list[dict]) -> dict[str, Any]:
    """Stop-hunts: a break that first ran the liquidity resting past a level.

    Each entry names the candle that did the sweeping and every level it took
    out, so the caller can see whether a break was engineered or clean.
    """
    times = _timestamps(candles)
    payload = mss_sweeps.build_sweep_payload(candles, times)

    long_side = sum(1 for s in payload if s.get("direction") == "long")
    return {
        "sweeps": payload,
        "counts": {
            "total": len(payload),
            "bullish": long_side,
            "bearish": len(payload) - long_side,
        },
        "summary": (
            f"{len(payload)} liquidity sweep(s) in the window "
            f"({long_side} bullish, {len(payload) - long_side} bearish)."
            if payload
            else "No liquidity sweeps in this window: breaks here were clean."
        ),
        "window": _window(candles, times),
    }


def find_blocks(candles: list[dict]) -> dict[str, Any]:
    """Fair value gaps, order blocks and the pullback zones they sit in.

    Blocks are reported for the current impulse leg, and again per Fibonacci
    pullback zone (discount, equilibrium, premium and the golden pocket), which
    is where a smart-money entry is usually looked for.
    """
    times = _timestamps(candles)
    r = compute_trend_v2(candles)
    chart = mss_chart_blocks.build_chart_blocks(r, candles, times)

    leg = chart.get("leg", {})
    current = leg.get("current", {})
    zones = chart.get("zones", {})
    # The chart payload carries "in_zone" but not "touched": upstream that
    # distinction belongs to the signal matcher, not to the drawing. Read it
    # from the same place the matcher does rather than change what the chart
    # route emits.
    zone_state = mss_blocks.compute_zones_state(r, candles)
    in_zone = [name for name, z in zones.items() if z.get("in_zone")]
    # Touched and inside are different claims. Inside asks where the close
    # landed, which on a 1h chart is one sample an hour; touched asks whether
    # price reached the zone at all, which is what a pullback setup is about.
    touched = [name for name, z in zone_state.items() if z.get("touched")]

    return {
        "trend": chart.get("trend"),
        "leg": {
            "start_time": leg.get("start_time"),
            "fvg": current.get("fvg", []),
            "order_blocks": current.get("ob", []),
        },
        "zones": {
            name: {
                "price_is_inside": z.get("in_zone"),
                "price_touched": zone_state.get(name, {}).get("touched"),
                "low": z.get("zone_low"),
                "high": z.get("zone_high"),
                "fvg": z.get("fvg", []),
                "order_blocks": z.get("ob", []),
            }
            for name, z in zones.items()
        },
        "price_currently_in": in_zone,
        "price_touched_zones": touched,
        "summary": (
            f"{chart.get('trend', 'neutral')} leg holds "
            f"{len(current.get('fvg', []))} fair value gap(s) and "
            f"{len(current.get('ob', []))} order block(s). "
            + (
                f"Price is inside: {', '.join(in_zone)}."
                if in_zone
                else (
                    f"The last candle did not close inside a zone but reached: "
                    f"{', '.join(touched)}."
                    if touched
                    else "Price neither closed in nor reached a pullback zone."
                )
            )
        ),
        "window": _window(candles, times),
    }


__all__ = ["analyze_structure", "find_blocks", "find_liquidity_sweeps"]
