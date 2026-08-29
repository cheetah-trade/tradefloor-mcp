# Extracted from the TradeFloor monorepo by
# backend/scripts/extract_opensource_mcp.py -- do not edit by hand.
# Upstream: app/engine/order_block.py
"""
Order Block (OB) detection module.

An Order Block is the last opposing candle before a displacement move.
It marks the institutional entry zone where large orders were placed.

  Bullish OB: Last bearish candle before a sharp bullish displacement
  Bearish OB: Last bullish candle before a sharp bearish displacement

The body of the Order Block candle is the zone where price is expected
to return for entries. OBs that overlap with Fair Value Gaps are the
highest probability entry zones.

Pure-logic module — no I/O.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass
class OrderBlock:
    """Represents a single Order Block zone.

    Lifecycle flags (WS-5, 2026-06-10): ``mitigated`` keeps its body-close-
    through semantic — this IS the WS-5 "invalidation" rule for OB by design
    (no rename, no new field). ``touched``/``touch_index`` are added for
    symmetry with ``FairValueGap`` and to support the WS-5 matcher trigger
    (wick = "user intended to enter", block stays alive). See
    ``FairValueGap`` docstring for the full lifecycle-split rationale.
    """
    direction: Literal["bullish", "bearish"]
    # Zone boundaries (the OB candle's body)
    top: float              # max(open, close) of the OB candle
    bottom: float           # min(open, close) of the OB candle
    midpoint: float         # 50% of the OB body — optimal entry
    # Full candle range (including wicks)
    high: float
    low: float
    # Context
    candle_index: int
    candle_time: int
    displacement_strength: float  # how strong the move after OB was (in %)
    # Mitigation (body close-through) — WS-5 invalidation rule. Unchanged
    # from legacy semantic; ``mark_mitigated_obs`` is the correct WS-5
    # invalidation helper for OB.
    mitigated: bool = False       # price has returned and traded through the OB
    mitigation_index: int | None = None
    # WS-5 touched (wick entry) — matcher trigger ONLY, doesn't kill block.
    touched: bool = False
    touch_index: int | None = None


def detect_order_blocks(
    opens: list[float],
    highs: list[float],
    lows: list[float],
    closes: list[float],
    candle_times: list[int] | None = None,
    displacement_threshold: float = 0.5,
    lookback: int = 50,
) -> list[OrderBlock]:
    """Detect Order Blocks in candle data.

    Algorithm:
    1. Scan for displacement candles (body > 70% of range, large move).
    2. For each displacement, find the last opposing candle before it.
    3. That opposing candle is the Order Block.

    Args:
        opens: Candle open prices.
        highs: Candle high prices.
        lows: Candle low prices.
        closes: Candle close prices.
        candle_times: Optional epoch ms timestamps.
        displacement_threshold: Min move as % of price to qualify as displacement.
        lookback: How many candles back to search.

    Returns:
        List of OrderBlock objects.
    """
    if len(opens) < 5:
        return []

    times = candle_times or [0] * len(opens)
    obs: list[OrderBlock] = []
    start = max(1, len(opens) - lookback)

    for i in range(start, len(opens)):
        body = abs(closes[i] - opens[i])
        candle_range = highs[i] - lows[i]

        if candle_range == 0:
            continue

        body_ratio = body / candle_range

        # Is this a displacement candle? Body > 60% of range (universal across TFs)
        if body_ratio < 0.6:
            continue

        is_bullish_displacement = closes[i] > opens[i]

        # Find the last opposing candle before displacement
        ob_idx = None
        for j in range(i - 1, max(start - 1, i - 6), -1):
            if j < 0:
                break
            candle_is_bearish = closes[j] < opens[j]
            candle_is_bullish = closes[j] > opens[j]

            if (is_bullish_displacement and candle_is_bearish) or (
                not is_bullish_displacement and candle_is_bullish
            ):
                ob_idx = j
                break

        if ob_idx is None:
            continue

        ob_open = opens[ob_idx]
        ob_close = closes[ob_idx]
        ob_top = max(ob_open, ob_close)
        ob_bottom = min(ob_open, ob_close)
        ob_mid = (ob_top + ob_bottom) / 2

        direction: Literal["bullish", "bearish"] = (
            "bullish" if is_bullish_displacement else "bearish"
        )

        # Displacement strength: how far price moved from OB midpoint (#586)
        move_pct = abs(closes[i] - ob_mid) / ob_mid if ob_mid != 0 else 0

        obs.append(OrderBlock(
            direction=direction,
            top=round(ob_top, 8),
            bottom=round(ob_bottom, 8),
            midpoint=round(ob_mid, 8),
            high=highs[ob_idx],
            low=lows[ob_idx],
            candle_index=ob_idx,
            candle_time=times[ob_idx] if ob_idx < len(times) else 0,
            displacement_strength=round(move_pct, 4),
        ))

    return obs


def detect_swing_order_block(
    opens: list[float],
    highs: list[float],
    lows: list[float],
    closes: list[float],
    candle_times: list[int] | None,
    swing_index: int,
    direction: Literal["long", "short"],
) -> OrderBlock | None:
    """Swing-anchored Order Block — operator-ratified 2026-07-07.

    Variant A, for ``direction="long"`` (bullish OB at a swing LOW; fully
    mirrored for short):

      1. The OB candle is STRICTLY the swing candle at ``swing_index`` —
         no scan-back, no neighbours.
      2. It must be RED (close < open). Green / doji → None.
      3. The overcoming run = consecutive GREEN candles starting IMMEDIATELY
         at ``swing_index + 1``. A red or doji breaks the run — strictly,
         no exceptions, NO SECOND CHANCE (a later green sequence cannot
         resurrect this swing's OB).
      4. Birth: the OB is born the moment a candle IN the same-colour run
         CLOSES through the OB candle's OPPOSITE extreme — for a bullish OB
         a green in the run CLOSES ABOVE the OB candle's ``high``; for a
         bearish OB (mirror) a red in the run CLOSES BELOW the OB candle's
         ``low``. This replaces the earlier full-body-coverage requirement,
         which rejected visually-valid OBs whenever the run's first candle
         gap-opened past the OB body (leaving a thin sliver uncovered) —
         see the ETH 15m false-negative in ``test_swing_order_block``. Birth
         is decided by the CLOSE, not by wicks: a run candle whose wick
         pierces the extreme but whose body stops short does not qualify.
      5. Zone = the OB candle's FULL range ``[low, high]`` (wicks included)
         — ``top``/``bottom`` are set to high/low, so the existing
         lifecycle helpers (``mark_mitigated_obs`` body-close-through =
         invalidation) operate on the full-range zone.

    Returns ``None`` when: index out of range, wrong-colour swing candle,
    run broken before birth, or the run is still pending (intact at the
    data edge but no candle has yet closed through the extreme — may be
    born on a later cycle).

    Pure function — replaces the legacy displacement-scan
    ``detect_order_blocks`` on MSS surfaces ONLY (the legacy detector
    stays for Setup Analyzer / Workshop SMC callers — ratified scope).
    """
    n = len(closes)
    if not (0 <= swing_index < n):
        return None
    if not (len(opens) == len(highs) == len(lows) == n):
        return None

    i = swing_index
    o, c = opens[i], closes[i]
    ob_high, ob_low = highs[i], lows[i]
    if not (ob_high > ob_low > 0):
        return None

    if direction == "long":
        # Bullish OB: the swing candle must be RED.
        if not (c < o):
            return None
    elif direction == "short":
        # Bearish OB: the swing candle must be GREEN.
        if not (c > o):
            return None
    else:
        return None

    # Walk the same-colour run; the OB is born when a run candle CLOSES
    # through the OB candle's opposite extreme (green above high / red
    # below low). The first opposite-colour / doji candle breaks the run.
    for j in range(i + 1, n):
        oj, cj = opens[j], closes[j]
        if direction == "long":
            if not (cj > oj):  # red or doji → run broken, no second chance
                return None
            born = cj > ob_high  # green closes ABOVE the OB candle's high
        else:
            if not (cj < oj):  # green or doji → run broken
                return None
            born = cj < ob_low  # red closes BELOW the OB candle's low
        if born:
            mid = (ob_high + ob_low) / 2
            move_pct = abs(cj - mid) / mid if mid > 0 else 0.0
            return OrderBlock(
                direction="bullish" if direction == "long" else "bearish",
                top=round(ob_high, 8),
                bottom=round(ob_low, 8),
                midpoint=round(mid, 8),
                high=ob_high,
                low=ob_low,
                candle_index=i,
                candle_time=(
                    candle_times[i]
                    if candle_times and i < len(candle_times)
                    else 0
                ),
                displacement_strength=round(move_pct, 4),
            )

    return None  # run intact at the data edge but no close-through yet — pending


def mark_mitigated_obs(
    obs: list[OrderBlock],
    highs: list[float],
    lows: list[float],
    closes: list[float],
) -> list[OrderBlock]:
    """Mark Order Blocks as mitigated when price trades through them.

    An OB is mitigated when:
      - Bullish OB: a candle closes below the OB bottom
      - Bearish OB: a candle closes above the OB top

    WS-5 note: this body-close-through rule IS the WS-5 "invalidation"
    semantic for OB — no separate ``mark_invalidated_obs`` helper exists.
    The wick-touch matcher trigger lives in ``mark_touched_obs``.
    """
    for ob in obs:
        if ob.mitigated:
            continue

        start = ob.candle_index + 1
        is_bullish = ob.direction == "bullish"
        for j in range(start, len(closes)):
            if (is_bullish and closes[j] < ob.bottom) or (
                not is_bullish and closes[j] > ob.top
            ):
                ob.mitigated = True
                ob.mitigation_index = j
                break

    return obs


def mark_touched_obs(
    obs: list[OrderBlock],
    highs: list[float],
    lows: list[float],
) -> list[OrderBlock]:
    """Mark Order Blocks as TOUCHED on wick entry (WS-5 matcher trigger).

    A touch fires when:
      * Bullish OB: a subsequent candle's ``low <= top`` (wick crossed OB top)
      * Bearish OB: a subsequent candle's ``high >= bottom`` (wick crossed bottom)

    Touched blocks STAY ALIVE — the touch is only the matcher's "user
    entered the zone, fire signal" trigger. Block death is detected
    separately by ``mark_mitigated_obs`` (body rule).

    Sets ``touched``/``touch_index`` (NOT ``mitigated``/``mitigation_index``
    — those follow the legacy body-rule semantic, which IS the WS-5
    invalidation contract for OB).

    First-touch wins — ``touch_index`` is pinned to the first crossing.
    """
    for ob in obs:
        if ob.touched:
            continue

        start = ob.candle_index + 1
        is_bullish = ob.direction == "bullish"
        for j in range(start, len(highs)):
            if (is_bullish and lows[j] <= ob.top) or (
                not is_bullish and highs[j] >= ob.bottom
            ):
                ob.touched = True
                ob.touch_index = j
                break

    return obs


def get_active_order_blocks(
    opens: list[float],
    highs: list[float],
    lows: list[float],
    closes: list[float],
    candle_times: list[int] | None = None,
    direction: Literal["bullish", "bearish"] | None = None,
    displacement_threshold: float = 0.5,
) -> list[OrderBlock]:
    """Get all unmitigated (active) Order Blocks.

    Args:
        direction: Filter to only bullish or bearish OBs.
    """
    obs = detect_order_blocks(
        opens, highs, lows, closes, candle_times, displacement_threshold
    )
    obs = mark_mitigated_obs(obs, highs, lows, closes)

    active = [ob for ob in obs if not ob.mitigated]
    if direction:
        active = [ob for ob in active if ob.direction == direction]

    return active


def find_nearest_ob(
    obs: list[OrderBlock],
    current_price: float,
    direction: Literal["long", "short"],
) -> OrderBlock | None:
    """Find the nearest active Order Block suitable for entry.

    For long: nearest bullish OB below current price.
    For short: nearest bearish OB above current price.
    """
    if direction == "long":
        candidates = [
            ob for ob in obs
            if ob.direction == "bullish" and not ob.mitigated
            and ob.midpoint < current_price
        ]
        return max(candidates, key=lambda ob: ob.midpoint) if candidates else None
    else:
        candidates = [
            ob for ob in obs
            if ob.direction == "bearish" and not ob.mitigated
            and ob.midpoint > current_price
        ]
        return min(candidates, key=lambda ob: ob.midpoint) if candidates else None


def ob_fvg_confluence(
    ob: OrderBlock,
    fvg_top: float,
    fvg_bottom: float,
) -> bool:
    """Check if an Order Block overlaps with a Fair Value Gap.

    OB+FVG confluence = highest probability entry zone.
    Returns True if there is any overlap between the zones.
    """
    return ob.top >= fvg_bottom and ob.bottom <= fvg_top
