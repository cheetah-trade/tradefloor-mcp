# Extracted from the TradeFloor monorepo by
# backend/scripts/extract_opensource_mcp.py -- do not edit by hand.
# Upstream: app/indicators/mss_blocks.py
"""Shared pure helpers for MSS FVG/OB block + pullback-zone computation.

WS-1 (single source of truth) — spec:
docs/superpowers/specs/2026-06-02-mss-blocks-single-source-and-impulse-leg-design.md

These functions were extracted VERBATIM from ``MSSAdapter`` so that BOTH
consumers call the SAME backend algorithm (the "single source" is single
ALGORITHM, not single invocation — chart route and adapter are separate
compute paths, see spec §1.9):

  * ``MSSAdapter`` (matcher path) — delegates its static methods here.
  * the chart ``/structure`` route (WS-1 Step 2) — calls these directly to
    serve server-computed blocks, so the frontend can stop client-side
    ``detectFVGs`` re-detection (the divergence WS-1 removes).

Pure: no DB / Redis / network. Operates on candle dicts + a
``compute_trend_v2`` result. Behaviour MUST stay byte-identical to the
former adapter methods — pinned by tests/test_mss_adapter_block_helpers_parity.py.
"""

from __future__ import annotations

from typing import Any, Literal

from tradefloor_mcp.engine.fvg_detector import (
    detect_fvgs,
    mark_invalidated_fvgs,
    mark_touched_fvgs,
)
from tradefloor_mcp.engine.order_block import (
    OrderBlock,
    detect_swing_order_block,
    mark_mitigated_obs,
)

PullbackZone = Literal["discount", "ote", "golden_retriever"]

# zone name -> (low_fib, high_fib) as retracement depth fraction
# (0 = no retrace, at swing extreme; 1 = full retrace to opposite swing).
# Matches frontend chartIndicators.ts:computePullbackZone.
FIB_ZONES: dict[str, tuple[float, float]] = {
    "discount": (0.5, 1.0),              # 50-100% retrace — entire cheap half
    "ote": (0.62, 0.79),                 # 62-79% retrace — classic OTE
    "golden_retriever": (0.618, 1.0),    # 61.8-100% retrace — fib 0.382 to swing low
}


def extract_ohlc(
    candles: list[dict],
) -> tuple[list[float], list[float], list[float], list[float], list[int]]:
    """Extract OHLC arrays + timestamps from candle dicts."""
    opens = [float(c.get("o", c.get("open", 0))) for c in candles]
    highs = [float(c.get("h", c.get("high", 0))) for c in candles]
    lows = [float(c.get("l", c.get("low", 0))) for c in candles]
    closes = [float(c.get("c", c.get("close", 0))) for c in candles]
    times = [int(c.get("t", c.get("open_time", 0))) for c in candles]
    return opens, highs, lows, closes, times


def _running_index(result: Any, attr_name: str) -> int:
    """Read ``result.<attr_name>`` defensively as an int sentinel.

    MagicMock-backed test results auto-create attributes that aren't ``int``,
    so a naive ``getattr(result, name, -1) >= 0`` would raise
    ``TypeError: '<' not supported between instances of 'MagicMock' and 'int'``.
    Returns -1 ("no active tracker") whenever the attribute is missing or
    not an ``int``.
    """
    val = getattr(result, attr_name, -1)
    return val if isinstance(val, int) else -1


def fib_range_for_trend(result: Any) -> tuple[float, float]:
    """Get (fib_low, fib_high) of the current swing leg based on trend.

    Operator semantic (round-table 2026-06-09, spec §11.10):
      long  fib_high = last_ph              if last_ph > 0
                     = running_max_high     elif running_max_high_index >= 0
                     = last_vh              otherwise  (broken-ceiling fallback)
      short fib_low  = mirror (PL → running_min_low → last_vl)

    The "либо PH либо running_max" rule mirrors operator's mental model:
    PH is the discrete validated pivot once it forms; before that, the
    running impulse peak (max(high) since the break, wicks included) is the
    legitimate peak the pullback fib should reach. Strict precedence — when
    PH exists, the discrete pivot takes priority regardless of running_max
    (running_max may include post-PH wick excursions; PH is the structural
    truth).

    Spec §11.8 invariant: this MUST match ``ws3_leg_end_index`` so block-
    based SL and fib-based SL anchor to the same pivot.
    """
    if result.trend == "long":
        fib_low = result.last_vl
        if result.last_ph > 0:
            fib_high = result.last_ph
        elif _running_index(result, "running_max_high_index") >= 0:
            fib_high = result.running_max_high
        else:
            fib_high = result.last_vh
        return fib_low, fib_high

    if result.trend == "short":
        fib_high = result.last_vh
        if result.last_pl > 0:
            fib_low = result.last_pl
        elif _running_index(result, "running_min_low_index") >= 0:
            fib_low = result.running_min_low
        else:
            fib_low = result.last_vl
        return fib_low, fib_high

    return 0.0, 0.0


def compute_zone_bounds(
    trend: str,
    fib_low: float,
    fib_high: float,
) -> dict[str, tuple[float, float]]:
    """Compute (zone_low, zone_high) for each pullback zone.

    Returns empty dict if fib range is invalid.
    """
    if fib_high <= fib_low:
        return {}

    rng = fib_high - fib_low
    zones: dict[str, tuple[float, float]] = {}

    for zone_name, (low_fib, high_fib) in FIB_ZONES.items():
        if trend == "long":
            z_low = fib_high - rng * high_fib
            z_high = fib_high - rng * low_fib
        elif trend == "short":
            z_low = fib_low + rng * low_fib
            z_high = fib_low + rng * high_fib
        else:
            continue
        zones[zone_name] = (z_low, z_high)

    return zones


def find_overlapping(
    zone_low: float,
    zone_high: float,
    fvgs: list,
    obs: list,
) -> tuple[list[dict], list[dict]]:
    """Find FVG/OB that overlap a price zone.

    Returns (fvg_zones, ob_zones) — full coordinate dicts for chart rendering.
    """
    fvg_zones = [
        {
            "direction": f.direction,
            "top": round(f.top, 8),
            "bottom": round(f.bottom, 8),
            "candle_time": f.candle_time,
            "candle_index": f.candle_index,
        }
        for f in fvgs
        if f.top >= zone_low and f.bottom <= zone_high
    ]
    ob_zones = [
        {
            "direction": o.direction,
            "top": round(o.top, 8),
            "bottom": round(o.bottom, 8),
            "candle_time": o.candle_time,
            "candle_index": o.candle_index,
        }
        for o in obs
        if o.top >= zone_low and o.bottom <= zone_high
    ]
    return fvg_zones, ob_zones


def enumerate_leg_blocks(
    leg_candles: list[dict],
    leg_start_idx: int,
    direction: str,
) -> dict[str, list[dict[str, Any]]]:
    """Enumerate FVG blocks in the impulsive leg.

    Args:
        leg_candles: Subset of candles from the leg start to the break candle.
        leg_start_idx: Absolute index of leg_candles[0] in the full candle array.
                       Used to compute block_index in absolute candle space.
        direction: "long" (bullish leg) or "short" (bearish leg).

    Returns ALL FVG blocks in the leg (filled or not) — the leg context stays
    relevant for block_touched detection even after a block is filled.

    §11.13 (OB rewrite): the OB side is ALWAYS empty here — Order Blocks
    are swing-anchored now and live in ``enumerate_swing_obs`` (needs the
    structure ``result``, which this slice-level helper doesn't have). Per
    the indicators "no fallback formulas" rule there is NO legacy
    displacement-scan fallback. Callers that need leg OBs compose via
    ``compute_live_leg_blocks`` / ``swing_ob_block_dicts``.

    Returns:
        {
            "fvg": [{"block_top": float, "block_bottom": float,
                      "block_index": int, "block_age_candles": int}, ...],
            "ob":  [] (always — see above),
        }
    """
    if len(leg_candles) < 3:
        return {"fvg": [], "ob": []}

    highs = [float(c.get("h", c.get("high", 0))) for c in leg_candles]
    lows = [float(c.get("l", c.get("low", 0))) for c in leg_candles]
    times = [int(c.get("t", c.get("open_time", 0))) for c in leg_candles]

    n = len(leg_candles)
    block_direction = "bullish" if direction == "long" else "bearish"

    raw_fvgs = detect_fvgs(highs, lows, times)
    fvg_blocks: list[dict[str, Any]] = [
        {
            "block_top": round(f.top, 8),
            "block_bottom": round(f.bottom, 8),
            "block_index": leg_start_idx + f.candle_index,
            "block_age_candles": max(0, n - 1 - f.candle_index),
        }
        for f in raw_fvgs
        if f.direction == block_direction
    ]

    return {"fvg": fvg_blocks, "ob": []}


def enumerate_swing_obs(
    result: Any,
    candles: list[dict],
    direction: str,
) -> list[OrderBlock]:
    """Swing-anchored Order Blocks for the trade direction (2026-07-07 rule).

    One OB candidate per structure swing: validated lows + the current
    potential low for ``long`` (bullish OBs); validated highs + the current
    potential high for ``short``. Each candidate runs through the strict
    variant-A detector (``detect_swing_order_block``): the swing candle is
    the OB, opposite-colour to the run, and the OB is born when a candle in
    the consecutive same-colour run CLOSES through the OB candle's opposite
    extreme (green above high / red below low). Zone = full candle range
    (top=high / bottom=low — wicks included).

    PH/PL revocation is automatic: OBs are re-enumerated from CURRENT
    structure each call, so a cancelled potential pivot simply stops
    enumerating (§11.13.1.8).

    Returned blocks carry ``mitigated`` per the body-close-through rule
    (= §11.13 invalidation) — callers filter ``not o.mitigated`` as needed.
    ``touched`` is NOT computed: per §11.13.1.9 touch is not a property of
    the block (existence is the matcher condition; touch is consumed
    contextually by the in-zone gate machinery).
    """
    if direction not in ("long", "short"):
        return []

    opens, highs, lows, closes, times = extract_ohlc(candles)

    want_type = "VL" if direction == "long" else "VH"
    idxs: set[int] = set()
    for sw in getattr(result, "validated_swings", None) or []:
        if sw.get("type") == want_type and isinstance(sw.get("index"), int):
            idxs.add(sw["index"])
    # Current validated swing (defensive dedup — normally already in the
    # validated_swings list) + the current POTENTIAL pivot (revocable OB).
    if direction == "long":
        vl_idx = _running_index(result, "last_vl_index")
        if vl_idx >= 0:
            idxs.add(vl_idx)
        pl_idx = _running_index(result, "last_pl_index")
        if pl_idx >= 0 and result.last_pl > 0:
            idxs.add(pl_idx)
    else:
        vh_idx = _running_index(result, "last_vh_index")
        if vh_idx >= 0:
            idxs.add(vh_idx)
        ph_idx = _running_index(result, "last_ph_index")
        if ph_idx >= 0 and result.last_ph > 0:
            idxs.add(ph_idx)

    obs: list[OrderBlock] = []
    for i in sorted(idxs):
        ob = detect_swing_order_block(
            opens, highs, lows, closes, times, i, direction,
        )
        if ob is not None:
            obs.append(ob)

    return mark_mitigated_obs(obs, highs, lows, closes)


def swing_ob_block_dicts(
    result: Any,
    candles: list[dict],
    direction: str,
    start_idx: int,
    end_idx: int,
) -> list[dict[str, Any]]:
    """Swing OBs within ``[start_idx, end_idx]`` as matcher block dicts.

    The §11.13 companion to ``enumerate_leg_blocks``'s FVG output —
    same ``{block_top, block_bottom, block_index, block_age_candles}``
    shape so downstream consumers (state matcher, position levels, chart
    payload) read both block classes identically. Invalidated (mitigated)
    blocks are dropped — a dead OB never reaches the matcher (§11.13.1.9).
    """
    n = len(candles)
    out: list[dict[str, Any]] = []
    for ob in enumerate_swing_obs(result, candles, direction):
        if ob.mitigated:
            continue
        if not (start_idx <= ob.candle_index <= end_idx):
            continue
        out.append({
            "block_top": round(ob.top, 8),
            "block_bottom": round(ob.bottom, 8),
            "block_index": int(ob.candle_index),
            "block_age_candles": max(0, n - 1 - int(ob.candle_index)),
        })
    return out


def leg_start_index(result: Any, direction: str | None = None) -> int:
    """Absolute candle index where the impulsive leg starts.

    Long leg starts at the last validated low; short leg at the last
    validated high. Mirrors ``MSSAdapter._leg_blocks_for`` (clamp to 0 when
    the swing index is negative). ``direction`` defaults to ``result.trend``;
    pass it explicitly to match a caller that already knows the direction.

    Returns -1 for neutral / unknown direction (no impulsive leg).
    """
    d = direction if direction is not None else getattr(result, "trend", None)
    if d == "long":
        return result.last_vl_index if result.last_vl_index >= 0 else 0
    if d == "short":
        return result.last_vh_index if result.last_vh_index >= 0 else 0
    return -1


def ws3_leg_end_index(result: Any) -> int:
    """WS-3 leg END index — mirrors ``fib_range_for_trend`` pivot precedence
    (spec §11.8 invariant pin).

    Long:  last_ph_index            if last_ph > 0
         = running_max_high_index   elif running_max_high_index >= 0
         = last_vh_index            otherwise  (broken-ceiling fallback)

    Mirror short.

    Returns -1 for neutral trend.
    """
    if result.trend == "long":
        if result.last_ph > 0:
            return result.last_ph_index
        if _running_index(result, "running_max_high_index") >= 0:
            return result.running_max_high_index
        return result.last_vh_index
    if result.trend == "short":
        if result.last_pl > 0:
            return result.last_pl_index
        if _running_index(result, "running_min_low_index") >= 0:
            return result.running_min_low_index
        return result.last_vl_index
    return -1


def compute_live_leg_blocks(
    result: Any,
    candles: list[dict],
    direction: str,
) -> dict[str, list[dict[str, Any]]]:
    """WS-3 — enumerate FVG/OB blocks in the LIVE-bounded impulse leg.

    Leg window is ``candles[leg_start_index(result, direction) :
    ws3_leg_end_index(result) + 1]`` — bounded by the END pivot
    (PH / running peak / broken VH per ``ws3_leg_end_index`` precedence)
    rather than open-ended ``candles[leg_start:]``. So blocks formed by
    candles AFTER the running peak (e.g. on the pullback itself) do NOT
    enter the matcher's leg-block set.

    Empty result when:
      * trend is neutral (no leg);
      * either index is sentinel (-1);
      * end_idx < start_idx (defensive — should not happen for a real leg).

    Pure: no DB / Redis. Replaces the open-ended ``candles[leg_start:]``
    slice the legacy ``MSSAdapter._leg_blocks_for`` was using.
    """
    leg_start = leg_start_index(result, direction)
    leg_end = ws3_leg_end_index(result)
    # Defensive — MagicMock-backed test results auto-create attributes that
    # aren't ints; reject anything non-int to avoid TypeError on `<`.
    if not isinstance(leg_start, int) or not isinstance(leg_end, int):
        return {"fvg": [], "ob": []}
    if leg_start < 0 or leg_end < 0 or leg_end < leg_start:
        return {"fvg": [], "ob": []}
    end_exclusive = min(leg_end + 1, len(candles))
    if end_exclusive <= leg_start:
        return {"fvg": [], "ob": []}
    leg_candles = candles[leg_start:end_exclusive]
    blocks = enumerate_leg_blocks(leg_candles, leg_start, direction)
    # §11.13: leg OBs = swing-anchored OBs within the leg window. The
    # leg-origin swing IS the leg start, so its OB (when born) lands at
    # block_index == leg_start. Detection runs on the FULL candle array —
    # the covering run may extend past the window edge for late swings.
    blocks["ob"] = swing_ob_block_dicts(
        result, candles, direction, leg_start, end_exclusive - 1,
    )
    return blocks


#: Bars kept AFTER the break bar in the break-evidence window (MSS-school PR-2).
#:
#: Three, so a 3-candle FVG straddling the break bar stays whole — the gap price
#: leaves going THROUGH the level is the most telling evidence of all, and a cut
#: exactly at the break bar would lose it. Widening this lets continuation gaps
#: back in, which are a CONSEQUENCE of the break rather than evidence for it.
#: Operator-locked 2026-08-25; changing it needs a plan amendment.
BREAK_LEG_TAIL_BARS = 3


def break_leg_end_index(result: Any, *, tail: int = BREAK_LEG_TAIL_BARS) -> int:
    """End bar of the BREAK-evidence window: the break bar plus a short tail.

    Distinct from ``ws3_leg_end_index``, which ends at the IMPULSE PEAK and
    feeds the wizard-preset matcher — that window answers "what is in this
    leg", this one answers "what drove the break". They must not be merged:
    the matcher window is load-bearing for existing presets.

    Rule, in one sentence: **the leg up to the broken level, plus three bars,
    but never past the impulse peak once the peak is beyond the break.**

    The tail exists because the gap price leaves going THROUGH the level forms
    on bars straddling the break, and a cut exactly at the break bar loses it.
    The peak cap exists because everything after the peak is the pullback, and
    a gap there is not evidence about the break (this is the same leak WS-3
    closed for the matcher window). When the peak sits only a bar or two past
    the break the cap wins and the tail is simply not needed — the break's own
    gap is inside either way.

    When ``ws3_leg_end_index`` falls BEFORE the break bar (its broken-VH
    fallback branch) the cap is ignored: capping there would end the window
    before the break and the helper would see nothing at all.

    Returns -1 for a neutral trend or when no BOS/MSS marker exists yet. The
    value is a raw bar number — clamping to the series is the caller's job
    (see ``compute_break_leg_blocks``), because this helper has no candles.
    """
    if getattr(result, "trend", None) not in ("long", "short"):
        return -1
    markers = [
        int(m["index"])
        for m in (getattr(result, "markers", None) or [])
        if m.get("type") in ("BOS", "MSS")
    ]
    if not markers:
        return -1
    break_bar = max(markers)
    peak = ws3_leg_end_index(result)
    if isinstance(peak, int) and peak >= break_bar:
        return min(break_bar + tail, peak)
    return break_bar + tail


def compute_break_leg_blocks(
    result: Any,
    candles: list[dict],
    direction: str,
    *,
    tail: int = BREAK_LEG_TAIL_BARS,
) -> dict[str, list[dict[str, Any]]]:
    """FVG/OB blocks that drove the break — the narrow sibling of
    ``compute_live_leg_blocks``.

    Same leg START, earlier END, so the result is always a subset of the
    matcher window (pinned in ``tests/test_mss_break_leg_blocks.py``). The
    OB side is swing-anchored exactly as in the wide window: an order block
    sits at the leg-origin extremum, so it is present in both windows by
    construction.

    Empty when there is no leg (neutral direction / sentinel index) or the
    window degenerates.
    """
    leg_start = leg_start_index(result, direction)
    leg_end = break_leg_end_index(result, tail=tail)
    if not isinstance(leg_start, int) or not isinstance(leg_end, int):
        return {"fvg": [], "ob": []}
    if leg_start < 0 or leg_end < 0 or leg_end < leg_start:
        return {"fvg": [], "ob": []}
    end_exclusive = min(leg_end + 1, len(candles))
    if end_exclusive <= leg_start:
        return {"fvg": [], "ob": []}
    blocks = enumerate_leg_blocks(candles[leg_start:end_exclusive], leg_start, direction)
    blocks["ob"] = swing_ob_block_dicts(
        result, candles, direction, leg_start, end_exclusive - 1,
    )
    return blocks


def _current_close(candles: list[dict]) -> float:
    last = candles[-1]
    return float(last.get("c", last.get("close", 0)))


def compute_zones_state(
    result: Any,
    candles: list[dict],
) -> dict[str, dict[str, Any]]:
    """Current pullback-zone state with ACTIVE FVG/OB confluence.

    Extracted VERBATIM from ``MSSAdapter._compute_zones_state`` so the chart
    route and the adapter compute zone confluence from ONE algorithm (WS-1
    single source).

    WS-5 (2026-06-10) — "active" now means "not invalidated" (body close-
    through) instead of legacy "not filled" (wick touch). Touched-but-alive
    blocks now count as confluence (matches the HYPER 15m display fix —
    wicks during leg formation no longer silently drop blocks from the
    pullback_entered event metadata).

    Returns dict keyed by zone name:
        {"<zone>": {"in_zone": bool, "zone_high": float, "zone_low": float,
                     "overlapping_fvgs": [...], "overlapping_obs": [...]}, ...}
    Empty dict when the fib range is invalid (neutral / no leg).
    """
    fib_low, fib_high = fib_range_for_trend(result)
    zone_bounds = compute_zone_bounds(result.trend, fib_low, fib_high)
    if not zone_bounds:
        return {}

    close_price = _current_close(candles)
    _last = candles[-1] if candles else {}
    last_high = float(_last.get("h", _last.get("high", close_price)) or close_price)
    last_low = float(_last.get("l", _last.get("low", close_price)) or close_price)

    opens, highs, lows, closes, times = extract_ohlc(candles)

    raw_fvgs = detect_fvgs(highs, lows, times)
    # WS-5 — invalidation (body rule), NOT filled (wick rule)
    active_fvgs = [
        f for f in mark_invalidated_fvgs(raw_fvgs, highs, lows, closes)
        if not f.invalidated
    ]

    # §11.13 — OB source = swing-anchored detector (already mitigation-
    # marked inside enumerate_swing_obs; trade-direction inherent).
    active_obs = [
        o for o in enumerate_swing_obs(result, candles, result.trend)
        if not o.mitigated
    ]

    zones_state: dict[str, dict[str, Any]] = {}
    for zone_name, (z_low, z_high) in zone_bounds.items():
        in_zone = z_low <= close_price <= z_high
        # TOUCHED and IN are different claims, and the school needs the first.
        #
        # «In» asks where the close landed — a property of THIS INSTANT, and
        # on 1h that is one sample per hour. «Touched» asks whether price
        # reached the zone at all, which is what a pullback setup is about:
        # the address was visited. A wick into the zone that closes above it
        # is a TEST of the zone, not the absence of one — and on a single
        # candle the close-only reading misses it completely (operator,
        # 2026-08-28).
        touched = last_low <= z_high and last_high >= z_low
        fvg_zones, ob_zones = find_overlapping(z_low, z_high, active_fvgs, active_obs)
        zones_state[zone_name] = {
            "in_zone": in_zone,
            "touched": touched,
            "zone_high": round(z_high, 8),
            "zone_low": round(z_low, 8),
            "overlapping_fvgs": fvg_zones,
            "overlapping_obs": ob_zones,
        }

    return zones_state


def _overlap_ratio(top: float, bottom: float, zone_low: float, zone_high: float) -> float:
    """Fraction of the block's own height that overlaps the zone band, in [0, 1]."""
    height = top - bottom
    if height <= 0:
        return 0.0
    overlap = min(top, zone_high) - max(bottom, zone_low)
    return max(0.0, overlap) / height


def select_zone_render_blocks(
    result: Any,
    candles: list[dict],
    zone_low: float,
    zone_high: float,
    *,
    min_overlap: float = 0.5,
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """§11.1 render set for ONE pullback zone (spec amendment 2026-06-05).

    Filtered to the TRADE DIRECTION only (D2: MSS shows entries, not targets —
    counter-blocks excluded), that overlap the zone band by >= ``min_overlap``
    of their own height (D1: the ">=50% in zone" rule), split into:
      * ``leg``        — block formed INSIDE the impulse-leg window
                         ``[leg_start, ws3_leg_end]`` (chart: bright layer).
                         Includes touched-alive blocks (matcher truth — the
                         touch fires the matcher trigger but the block stays
                         alive until body-pierced). ``spent`` flag carries
                         the touch info for chart styling. §11.14 fix
                         (2026-06-12): the window is BOUNDED above by
                         ``ws3_leg_end_index`` — same boundary the matcher
                         uses (``compute_zone_live_blocks``). Pre-fix the
                         render split was open-ended, so pullback-formed
                         FVGs (after the impulse peak) wrongly rendered at
                         leg brightness.
      * ``confluence`` — block in the zone but OUTSIDE the leg window (chart: muted layer).
                         Active-only by the WS-5 convention: not touched yet
                         AND not invalidated (a fresh extra-confluence block
                         available for first entry).

    WS-5 (2026-06-10) — drop rule swapped from "filled" (wick) to
    "invalidated" (body close-through). Touched-alive blocks render in the
    leg layer with ``spent=True`` (informational only, no longer triggers
    block death). The HYPER 15m fix: leg FVGs wicked during impulse no
    longer disappear visually — they stay solid until body-pierced.

    §11.13 (OB rewrite) — OB source = swing-anchored detector. For OBs
    ``spent`` is ALWAYS False: touch is not a property of the block
    (operator-ratified — alive = solid, dead = gone); only invalidation
    (already filtered inside ``enumerate_swing_obs`` consumers) removes
    an OB from render. FVG semantics unchanged.

    Each block dict: ``{direction, top, bottom, candle_time, candle_index, spent}``
    (``spent`` = touched FVG — informational; always False for OBs and for
    confluence-layer entries).
    Returns ``{"leg": {"fvg": [...], "ob": [...]},
               "confluence": {"fvg": [...], "ob": [...]}}``.
    Empty structure for neutral trend or an invalid zone band.
    """
    empty: dict[str, dict[str, list[dict[str, Any]]]] = {
        "leg": {"fvg": [], "ob": []},
        "confluence": {"fvg": [], "ob": []},
    }
    direction = getattr(result, "trend", None)
    if direction not in ("long", "short"):
        return empty
    if zone_high <= zone_low:
        return empty

    want_dir = "bullish" if direction == "long" else "bearish"
    leg_idx = leg_start_index(result, direction)
    # §11.14 fix: leg window bounded above by the WS-3 impulse end (PH →
    # running extremum → broken swing precedence) — mirrors the matcher's
    # compute_zone_live_blocks. Defensive open-ended fallback when the end
    # index is missing/behind leg_start OR the result object lacks the
    # ws3 fields entirely (partial/stale result objects) — render is a
    # display path, never raise.
    try:
        leg_end = ws3_leg_end_index(result)
    except Exception:
        leg_end = -1

    opens, highs, lows, closes, times = extract_ohlc(candles)
    raw_fvgs = detect_fvgs(highs, lows, times)
    # WS-5 lifecycle: touched (wick) + invalidated (body). Run both — they
    # write distinct fields, no overlap.
    fvgs = mark_touched_fvgs(raw_fvgs, highs, lows)
    fvgs = mark_invalidated_fvgs(fvgs, highs, lows, closes)
    # §11.13: swing-anchored OBs (mitigation-marked inside; direction inherent).
    obs = enumerate_swing_obs(result, candles, direction)

    def _is_invalidated(b: Any) -> bool:
        # FVG has explicit ``invalidated`` field. OB uses ``mitigated`` as its
        # invalidation signal (semantic equivalence — both are body-close rule).
        if hasattr(b, "invalidated"):
            return bool(b.invalidated)
        return bool(getattr(b, "mitigated", False))

    def _split(blocks: list, *, track_touch: bool) -> tuple[
        list[dict[str, Any]], list[dict[str, Any]]
    ]:
        leg_out: list[dict[str, Any]] = []
        conf_out: list[dict[str, Any]] = []
        for b in blocks:
            if b.direction != want_dir:
                continue
            # WS-5: drop invalidated blocks from BOTH layers.
            if _is_invalidated(b):
                continue
            if _overlap_ratio(b.top, b.bottom, zone_low, zone_high) < min_overlap:
                continue
            # §11.13.1.9: OBs never carry the touch flag (track_touch=False)
            # — existence is the matcher condition; alive renders solid.
            touched = track_touch and bool(getattr(b, "touched", False))
            in_leg = (
                leg_idx >= 0
                and b.candle_index >= leg_idx
                and (leg_end < leg_idx or b.candle_index <= leg_end)
            )
            entry = {
                "direction": b.direction,
                "top": round(b.top, 8),
                "bottom": round(b.bottom, 8),
                "candle_time": b.candle_time,
                "candle_index": b.candle_index,
                "spent": touched,  # informational only post-WS-5
            }
            if in_leg:
                leg_out.append(entry)
            elif not touched:
                # Confluence layer keeps the §11.8 "fresh-only" convention
                # for FVGs: an extra-confluence FVG stops being "fresh" the
                # moment it's first-touched. OBs: alive = shown.
                conf_out.append({**entry, "spent": False})
        return leg_out, conf_out

    fvg_leg, fvg_conf = _split(fvgs, track_touch=True)
    ob_leg, ob_conf = _split(obs, track_touch=False)
    return {
        "leg": {"fvg": fvg_leg, "ob": ob_leg},
        "confluence": {"fvg": fvg_conf, "ob": ob_conf},
    }


def compute_zone_live_blocks(
    result: Any,
    candles: list[dict],
    zone_low: float,
    zone_high: float,
    *,
    min_overlap: float = 0.5,
) -> dict[str, list[dict[str, Any]]]:
    """WS-5 §11.9 — LIVE leg blocks inside the active pullback zone.

    Returns ``{"fvg": [...], "ob": [...]}`` of leg blocks that are:
      * direction-matched to the trend (long → bullish, short → bearish);
      * NOT invalidated (body close-through rule — touched-alive blocks
        STAY ALIVE per WS-5 lifecycle split);
      * at least ``min_overlap`` (default 0.5 = 50%) of their height overlaps
        the zone band ``[zone_low, zone_high]``.

    Single source of truth for the matcher's new patient block-presence
    gate (``_patient_path_passes`` extension). When ``state_mss.zone_live_blocks``
    is populated, the matcher uses ``bool(zone_live_blocks["fvg"])`` /
    ``bool(zone_live_blocks["ob"])`` to gate ``preset.block_enabled``
    presets that DON'T use ``entry_rule=block_mitigation`` (the
    block_mitigation path stays on ``block_touched_in_leg`` — unchanged).

    Returns empty structure for neutral trend, missing leg window, or an
    invalid zone band. Each block dict matches ``enumerate_leg_blocks``
    shape (``block_top`` / ``block_bottom`` / ``block_index`` /
    ``block_age_candles``) so downstream consumers (state matcher / WS-4
    anchor on touched-in-zone block) read the same fields.
    """
    empty: dict[str, list[dict[str, Any]]] = {"fvg": [], "ob": []}
    direction = getattr(result, "trend", None)
    if direction not in ("long", "short"):
        return empty
    if zone_high <= zone_low:
        return empty

    leg_start = leg_start_index(result, direction)
    if leg_start < 0:
        return empty
    leg_end = ws3_leg_end_index(result)
    if leg_end < leg_start:
        # No bounded peak yet — leg extends to the latest candle (matches
        # the open-ended fallback in ``MSSAdapter._leg_blocks_for``).
        leg_end = len(candles) - 1
    if leg_end < 0:
        return empty

    want_dir = "bullish" if direction == "long" else "bearish"
    n = len(candles)

    opens, highs, lows, closes, times = extract_ohlc(candles)
    # Detect on FULL chart so block.candle_index is absolute; then filter
    # to leg window via index check (cheaper than re-slicing the OHLC arrays).
    raw_fvgs = detect_fvgs(highs, lows, times)
    fvgs = mark_invalidated_fvgs(raw_fvgs, highs, lows, closes)
    # §11.13: swing-anchored OBs (mitigation-marked inside; direction inherent).
    obs = enumerate_swing_obs(result, candles, direction)

    def _filter(blocks: list, invalid_attr: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for b in blocks:
            if b.direction != want_dir:
                continue
            if b.candle_index < leg_start or b.candle_index > leg_end:
                continue
            if bool(getattr(b, invalid_attr, False)):
                continue
            if _overlap_ratio(b.top, b.bottom, zone_low, zone_high) < min_overlap:
                continue
            out.append({
                "block_top": round(b.top, 8),
                "block_bottom": round(b.bottom, 8),
                "block_index": int(b.candle_index),
                "block_age_candles": max(0, n - 1 - int(b.candle_index)),
            })
        return out

    return {
        "fvg": _filter(fvgs, "invalidated"),
        "ob": _filter(obs, "mitigated"),
    }
