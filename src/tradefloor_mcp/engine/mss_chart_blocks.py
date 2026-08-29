# Extracted from the TradeFloor monorepo by
# backend/scripts/extract_opensource_mcp.py -- do not edit by hand.
# Upstream: app/indicators/mss_chart_blocks.py
"""Server-side FVG/OB blocks + pullback-zone bounds for a chart overlay.

Extracted verbatim from ``app.api.candles_structure_route_runtime`` so the
function can be consumed without dragging FastAPI and SQLAlchemy in with it.
The route module re-exports the name, so every existing import and every
``monkeypatch.setattr(rt, "build_chart_blocks", ...)`` keeps working.

Pure: reads a ``compute_trend_v2`` result plus the candle window it was
computed on, calls only ``mss_blocks``, touches no DB, Redis, network or env.
That purity is what makes it exportable to the open-source
``tradefloor-mcp`` package (plan
``docs/superpowers/plans/2026-08-27-tradefloor-mcp-opensource.md`` task A2).
"""

from __future__ import annotations

from tradefloor_mcp.engine import mss_blocks

__all__ = ["build_chart_blocks"]


def build_chart_blocks(
    trend_result: object,
    candles_data: list[dict],
    timestamps: list[int],
) -> dict:
    """Server-side FVG/OB blocks + pullback-zone bounds for the chart overlay.

    WS-1 single source: computed from the route's OWN ``compute_trend_v2``
    result using the SAME backend detectors the matcher uses (``mss_blocks``),
    so chart blocks == matcher blocks (spec §1.9 — single ALGORITHM, separate
    invocations). Route-layer ONLY — added as a sibling ``blocks`` payload key,
    NEVER folded into ``to_structure_dict`` (P1-4 / spec §1.7: that would
    resurrect the dead matcher-state ``structure.fvg_blocks`` field).

    Two leg-block boundaries are emitted for WS-2's dual-boundary visual:
      * ``current``       — ``[VL, open-ended]`` = what the matcher uses TODAY
        (``_leg_blocks_for``-equivalent, open-ended candle window).
      * ``ws3_candidate`` — ``[VL, PH-or-VH]`` = the boundary WS-3 will move the
        matcher to (chart-only here; NOT money-path).
    Leg blocks include FILLED blocks (matcher truth — ``enumerate_leg_blocks``
    does not filter fill status). Zone-confluence blocks are ACTIVE-only.

    Each block dict gains a route-layer ``block_time`` (ms, from the candle's
    open time) so the frontend can anchor on time (window-independent), not on
    ``block_index`` (relative to THIS route's candle window, which may differ
    from the frontend's own fetch window).
    """
    trend = getattr(trend_result, "trend", None)

    empty_leg = {
        "start_index": -1,
        "start_time": None,
        "current": {"fvg": [], "ob": []},
        "ws3_candidate": {"fvg": [], "ob": []},
        "ws3_end_index": -1,
        "ws3_end_time": None,
    }
    if trend not in ("long", "short"):
        return {"trend": trend or "neutral", "leg": empty_leg, "zones": {}}

    n = len(candles_data)

    def _ts(idx: int) -> int | None:
        return timestamps[idx] if 0 <= idx < len(timestamps) else None

    def _with_time(blocks: list[dict]) -> list[dict]:
        out: list[dict] = []
        for b in blocks:
            bt = dict(b)
            bt["block_time"] = _ts(b.get("block_index", -1))
            out.append(bt)
        return out

    leg_start = mss_blocks.leg_start_index(trend_result)
    leg = dict(empty_leg)
    leg["start_index"] = leg_start
    leg["start_time"] = _ts(leg_start)

    if 0 <= leg_start < n:
        current = mss_blocks.enumerate_leg_blocks(
            candles_data[leg_start:], leg_start, trend
        )
        # §11.13: leg OBs = swing-anchored detector (enumerate_leg_blocks
        # carries the FVG side only post-OB-rewrite). Same source the
        # matcher consumes — chart blocks == matcher blocks (WS-1).
        current_obs = mss_blocks.swing_ob_block_dicts(
            trend_result, candles_data, trend, leg_start, n - 1
        )
        leg["current"] = {
            "fvg": _with_time(current["fvg"]),
            "ob": _with_time(current_obs),
        }

        ws3_end = mss_blocks.ws3_leg_end_index(trend_result)
        leg["ws3_end_index"] = ws3_end
        leg["ws3_end_time"] = _ts(ws3_end)
        if leg_start <= ws3_end < n:
            ws3 = mss_blocks.enumerate_leg_blocks(
                candles_data[leg_start : ws3_end + 1], leg_start, trend
            )
            ws3_obs = mss_blocks.swing_ob_block_dicts(
                trend_result, candles_data, trend, leg_start, ws3_end
            )
            leg["ws3_candidate"] = {
                "fvg": _with_time(ws3["fvg"]),
                "ob": _with_time(ws3_obs),
            }

    zones_state = mss_blocks.compute_zones_state(trend_result, candles_data)
    zones = {
        name: {
            "in_zone": zs["in_zone"],
            "zone_low": zs["zone_low"],
            "zone_high": zs["zone_high"],
            "fvg": zs["overlapping_fvgs"],
            "ob": zs["overlapping_obs"],
            # §11.1 render set (spec amendment 2026-06-05): in-zone (>=50%
            # overlap), trade-direction only, split leg (yellow) vs confluence
            # (green). DISPLAY-only — route layer, NOT money-path.
            "render": mss_blocks.select_zone_render_blocks(
                trend_result, candles_data, zs["zone_low"], zs["zone_high"]
            ),
        }
        for name, zs in zones_state.items()
    }

    return {"trend": trend, "leg": leg, "zones": zones}
