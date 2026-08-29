# Extracted from the TradeFloor monorepo by
# backend/scripts/extract_opensource_mcp.py -- do not edit by hand.
# Upstream: app/engine/fvg_detector.py
"""
Fair Value Gap (FVG) / Imbalance detector.

An FVG is a 3-candle price inefficiency where the wicks of candle[i-2]
and candle[i] don't overlap, leaving a gap that institutions tend to
fill later. FVGs serve as high-probability entry zones.

Bullish FVG: candle[i].low  > candle[i-2].high  (gap above)
Bearish FVG: candle[i].high < candle[i-2].low   (gap below)

Pure-logic module — no I/O.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

#: Minimum FVG size (as a % of the gap midpoint) for the gap to be considered
#: tradeable. THE single source of truth — every FVG surface (this module's
#: ``detect_fvgs`` / ``get_unfilled_fvgs`` / ``fvg_entry_price``, the SIE HTF
#: zone classifier, and the SMC event snapshot builder) defaults to this value
#: rather than repeating a literal.
#:
#: Raised 0.05 -> 0.10 (operator-ratified 2026-07-07) after visual review on
#: the MSS harness (ETH 15m, pinned dataset): 0.05% yielded 893 FVGs vs 611 at
#: 0.10% (-32%), the difference being micro-gaps thinner than ~$1.9 on ETH that
#: are noise rather than tradeable inefficiency.
#:
#: The threshold is scale-INVARIANT — ``size_pct = (top - bottom) / mid * 100``
#: — so one value applies uniformly across ETH (~$1.9k), BTC (~$100k) and cheap
#: alts without per-asset tuning.
DEFAULT_FVG_MIN_GAP_PCT = 0.10


@dataclass
class FairValueGap:
    """Represents a single Fair Value Gap.

    Lifecycle flags (WS-5, 2026-06-10): the legacy ``filled``/``fill_index``
    fields are wick-based — bullish FVG ``filled`` the moment ANY candle's
    ``low <= top``. This conflates the matcher TRIGGER (any wick = "user
    intended to enter") with block DEATH (body close-through opposite side).

    WS-5 splits the two:

      * ``touched`` / ``touch_index`` — wick rule, matcher trigger.
        ``low <= top`` (bullish) / ``high >= bottom`` (bearish). The block
        STAYS ALIVE after a touch — only used to signal "user intent fired".
      * ``invalidated`` / ``invalidation_index`` — body rule, block death.
        ``close < bottom`` (bullish) / ``close > top`` (bearish). Drops the
        block from BOTH display AND matcher.

    The legacy ``filled``/``fill_index`` keep their wick semantic so callers
    like ``fvg_entry_price`` / ``get_unfilled_fvgs`` (which genuinely want
    "FVG that was never wicked yet" for first-entry suggestions) continue
    unchanged. New code (MSS leg-block lifecycle / matcher gate / display)
    MUST use the new touched/invalidated flags.
    """
    direction: Literal["bullish", "bearish"]
    # Gap zone
    top: float          # upper boundary of the gap
    bottom: float       # lower boundary of the gap
    midpoint: float     # (top + bottom) / 2 — ideal entry level
    # Context
    candle_index: int   # index of the middle candle (displacement candle)
    candle_time: int    # epoch ms of the middle candle (0 if not available)
    size_pct: float     # gap size as % of price
    # Legacy lifecycle (wick rule) — kept unchanged for `fvg_entry_price`
    # and `get_unfilled_fvgs` semantics. See dataclass docstring.
    filled: bool = False
    fill_index: int | None = None
    # WS-5 lifecycle split — matcher trigger (wick) vs block death (body).
    touched: bool = False
    touch_index: int | None = None
    invalidated: bool = False
    invalidation_index: int | None = None
    # Overlap with Order Block
    overlaps_ob: bool = False


def detect_fvgs(
    highs: list[float],
    lows: list[float],
    candle_times: list[int] | None = None,
    min_gap_pct: float = DEFAULT_FVG_MIN_GAP_PCT,
) -> list[FairValueGap]:
    """Detect all Fair Value Gaps in the candle data.

    Args:
        highs: List of candle high prices.
        lows: List of candle low prices.
        candle_times: Optional list of candle open times (epoch ms).
        min_gap_pct: Minimum gap size as % of price to be considered valid.
            Defaults to :data:`DEFAULT_FVG_MIN_GAP_PCT`.

    Returns:
        List of FairValueGap objects sorted by candle index.
    """
    if len(highs) < 3 or len(lows) < 3:
        return []

    times = candle_times or [0] * len(highs)
    fvgs: list[FairValueGap] = []

    for i in range(2, len(highs)):
        # Bullish FVG: gap between candle[i-2].high and candle[i].low
        # The middle candle (i-1) is the displacement candle
        if lows[i] > highs[i - 2]:
            gap_top = lows[i]
            gap_bottom = highs[i - 2]
            gap_mid = (gap_top + gap_bottom) / 2
            size_pct = (gap_top - gap_bottom) / gap_mid * 100 if gap_mid > 0 else 0

            if size_pct >= min_gap_pct:
                fvgs.append(FairValueGap(
                    direction="bullish",
                    top=gap_top,
                    bottom=gap_bottom,
                    midpoint=round(gap_mid, 8),
                    candle_index=i - 1,
                    candle_time=times[i - 1] if i - 1 < len(times) else 0,
                    size_pct=round(size_pct, 4),
                ))

        # Bearish FVG: gap between candle[i].high and candle[i-2].low
        if highs[i] < lows[i - 2]:
            gap_top = lows[i - 2]
            gap_bottom = highs[i]
            gap_mid = (gap_top + gap_bottom) / 2
            size_pct = (gap_top - gap_bottom) / gap_mid * 100 if gap_mid > 0 else 0

            if size_pct >= min_gap_pct:
                fvgs.append(FairValueGap(
                    direction="bearish",
                    top=gap_top,
                    bottom=gap_bottom,
                    midpoint=round(gap_mid, 8),
                    candle_index=i - 1,
                    candle_time=times[i - 1] if i - 1 < len(times) else 0,
                    size_pct=round(size_pct, 4),
                ))

    return fvgs


def mark_filled_fvgs(
    fvgs: list[FairValueGap],
    highs: list[float],
    lows: list[float],
) -> list[FairValueGap]:
    """Mark FVGs as filled when price returns to close the gap (LEGACY wick rule).

    An FVG is considered filled when:
      - Bullish FVG: a candle's low penetrates the gap top (price retraces down into it)
      - Bearish FVG: a candle's high penetrates the gap bottom (price retraces up into it)

    Only candles AFTER the FVG formation are checked.

    WS-5 note: this function keeps its wick-rule semantic for backward
    compat with ``fvg_entry_price`` / ``get_unfilled_fvgs`` (which want
    "FVG never wicked yet" for first-entry suggestions). New MSS lifecycle
    code uses ``mark_touched_fvgs`` (wick = matcher trigger) and
    ``mark_invalidated_fvgs`` (body close-through = block death) instead.
    """
    for fvg in fvgs:
        if fvg.filled:
            continue

        start = fvg.candle_index + 2  # first candle after FVG formation
        is_bullish = fvg.direction == "bullish"
        for j in range(start, len(highs)):
            if (is_bullish and lows[j] <= fvg.top) or (
                not is_bullish and highs[j] >= fvg.bottom
            ):
                fvg.filled = True
                fvg.fill_index = j
                break

    return fvgs


def mark_touched_fvgs(
    fvgs: list[FairValueGap],
    highs: list[float],
    lows: list[float],
) -> list[FairValueGap]:
    """Mark FVGs as TOUCHED on wick entry (WS-5 matcher trigger).

    A touch fires when:
      * Bullish FVG: a subsequent candle's ``low <= top`` (wick crossed gap top)
      * Bearish FVG: a subsequent candle's ``high >= bottom`` (wick crossed bottom)

    The wick rule is IDENTICAL to legacy ``mark_filled_fvgs`` — same wire,
    new field names. Touched blocks STAY ALIVE; the touch is only the
    matcher's "user entered the zone, fire signal" trigger. Block death
    is detected separately by ``mark_invalidated_fvgs`` (body rule).

    Sets ``touched``/``touch_index`` (NOT ``filled``/``fill_index`` — those
    keep their legacy semantic for entry-suggestion callers).

    First-touch wins — ``touch_index`` is pinned to the first crossing.
    """
    for fvg in fvgs:
        if fvg.touched:
            continue

        start = fvg.candle_index + 2  # first candle after FVG formation
        is_bullish = fvg.direction == "bullish"
        for j in range(start, len(highs)):
            if (is_bullish and lows[j] <= fvg.top) or (
                not is_bullish and highs[j] >= fvg.bottom
            ):
                fvg.touched = True
                fvg.touch_index = j
                break

    return fvgs


def mark_invalidated_fvgs(
    fvgs: list[FairValueGap],
    highs: list[float],
    lows: list[float],
    closes: list[float],
) -> list[FairValueGap]:
    """Mark FVGs as INVALIDATED on body close-through (WS-5 block death).

    Invalidation fires when:
      * Bullish FVG: a candle's BODY closes ``close < bottom`` (full body past
        gap floor — opposite direction of the impulse that created the gap)
      * Bearish FVG: a candle's BODY closes ``close > top`` (full body past gap ceiling)

    Strict inequality — ``close == bottom`` does NOT invalidate (the body
    is touching, not through). Only invalidated blocks are dropped from
    display AND matcher. Touched-but-not-invalidated blocks STAY ALIVE
    (the HYPER 15m display fix — wicks during leg formation no longer
    silently kill the block).

    Sets ``invalidated``/``invalidation_index``. Independent of
    ``touched`` and legacy ``filled`` — a block can be touched (wick)
    without ever being invalidated (body never closes through), and
    a block can be invalidated without ever being touched (rare —
    requires a body-pierce gap-down without intra-gap wick first).

    First close-through wins — ``invalidation_index`` is pinned.
    """
    n_closes = len(closes)
    for fvg in fvgs:
        if fvg.invalidated:
            continue

        start = fvg.candle_index + 2  # first candle after FVG formation
        is_bullish = fvg.direction == "bullish"
        for j in range(start, n_closes):
            if (is_bullish and closes[j] < fvg.bottom) or (
                not is_bullish and closes[j] > fvg.top
            ):
                fvg.invalidated = True
                fvg.invalidation_index = j
                break

    return fvgs


def get_unfilled_fvgs(
    highs: list[float],
    lows: list[float],
    candle_times: list[int] | None = None,
    direction: Literal["bullish", "bearish"] | None = None,
    min_gap_pct: float = DEFAULT_FVG_MIN_GAP_PCT,
) -> list[FairValueGap]:
    """Get all unfilled FVGs, optionally filtered by direction.

    These represent active entry zones where price is likely to return.
    """
    fvgs = detect_fvgs(highs, lows, candle_times, min_gap_pct)
    fvgs = mark_filled_fvgs(fvgs, highs, lows)

    result = [f for f in fvgs if not f.filled]
    if direction:
        result = [f for f in result if f.direction == direction]

    return result


def find_nearest_fvg(
    fvgs: list[FairValueGap],
    current_price: float,
    direction: Literal["long", "short"],
) -> FairValueGap | None:
    """Find the nearest unfilled FVG suitable for entry.

    For long entries: find the nearest bullish FVG below current price.
    For short entries: find the nearest bearish FVG above current price.
    """
    unfilled = [f for f in fvgs if not f.filled]

    if direction == "long":
        candidates = [
            f for f in unfilled
            if f.direction == "bullish" and f.midpoint < current_price
        ]
        if not candidates:
            return None
        # Closest to current price (highest midpoint)
        return max(candidates, key=lambda f: f.midpoint)
    else:
        candidates = [
            f for f in unfilled
            if f.direction == "bearish" and f.midpoint > current_price
        ]
        if not candidates:
            return None
        # Closest to current price (lowest midpoint)
        return min(candidates, key=lambda f: f.midpoint)


def fvg_entry_price(
    highs: list[float],
    lows: list[float],
    current_price: float,
    direction: Literal["long", "short"],
    candle_times: list[int] | None = None,
    min_gap_pct: float = DEFAULT_FVG_MIN_GAP_PCT,
) -> dict | None:
    """Calculate the optimal FVG-based entry price.

    Returns dict with entry details or None if no suitable FVG found.
    """
    fvgs = detect_fvgs(highs, lows, candle_times, min_gap_pct)
    fvgs = mark_filled_fvgs(fvgs, highs, lows)

    nearest = find_nearest_fvg(fvgs, current_price, direction)
    if nearest is None:
        return None

    # Distance from current price to FVG midpoint
    distance_pct = abs(current_price - nearest.midpoint) / current_price * 100

    return {
        "entry_price": nearest.midpoint,
        "fvg_top": nearest.top,
        "fvg_bottom": nearest.bottom,
        "fvg_size_pct": nearest.size_pct,
        "distance_from_current_pct": round(distance_pct, 4),
        "candle_index": nearest.candle_index,
        "candle_time": nearest.candle_time,
    }
