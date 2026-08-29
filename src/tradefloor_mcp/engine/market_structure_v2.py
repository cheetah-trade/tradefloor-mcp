# Extracted from the TradeFloor monorepo by
# backend/scripts/extract_opensource_mcp.py -- do not edit by hand.
# Upstream: app/engine/market_structure_v2.py
"""
"Connected Arc" (Связанная Арка) — Market Structure Algorithm v2.

1:1 port of the TypeScript implementation in
frontend/src/lib/marketStructure.ts.

Detects swing highs/lows, valid highs/lows, BOS/MSS breakouts,
and determines the current trend direction for a series of candles.

Candle format: list of dicts with keys:
  - {o, h, l, c} (str or float) — compact format
  - {open, high, low, close} (float/Decimal) — verbose format
Both formats are supported transparently.

Usage:
    result = compute_trend_v2(candles)
    print(result.trend, result.bos_count, result.mss_count)
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


# ── Types ────────────────────────────────────────────────────────────────────


@dataclass
class StructureLevel:
    """A horizontal S/R level line on the chart."""
    start_index: int
    end_index: int
    price: float
    type: str  # "VH" | "VL" | "PH" | "PL"

    def to_dict(self) -> dict:
        return {
            "startIndex": self.start_index,
            "endIndex": self.end_index,
            "price": self.price,
            "type": self.type,
        }


@dataclass
class TrendResultV2:
    trend: str  # "long" | "short"
    bos_count: int
    mss_count: int
    last_vh: float
    last_vl: float
    markers: list[dict] = field(default_factory=list)
    # markers: [{index, type, price, direction}, ...]
    validated_swings: list[dict] = field(default_factory=list)
    # validated_swings: [{index, price, type}, ...] — the numbered dots on chart
    # VL/VH transition counts AFTER the last MSS (= what user sees on chart)
    vl_after_mss: int = 0  # For short: MSS(1) + BOS count = this + 1
    vh_after_mss: int = 0  # For long: MSS(1) + BOS count = this + 1
    bos_after_last_mss: int = 0  # BOS markers after last MSS (for screener: total = this + 1)
    last_mss_index: int = -1
    # Current PH/PL (potential high/low) — for pullback fib zones
    last_ph: float = 0.0
    last_pl: float = 0.0
    last_ph_index: int = -1
    last_pl_index: int = -1
    last_vh_index: int = -1
    last_vl_index: int = -1
    # WS-3 (2026-06-09 operator round-table, spec §11.10): running peak/trough
    # since the most recent structural break. running_max_high = max(high) of
    # every closed candle since BOS/MSS in long direction (inclusive of break
    # candle, wicks included); growth-only within a leg; reset on next BOS or
    # MSS. Mirror for running_min_low in short.  index = -1 sentinel means
    # "no active tracker for this side" (pre-bootstrap or trend-flipped).
    # Consumed by mss_blocks.fib_range_for_trend + ws3_leg_end_index so the
    # pullback fib zone tracks the impulse peak instead of freezing at the
    # broken VH/VL level.
    running_max_high: float = 0.0
    running_max_high_index: int = -1
    running_min_low: float = 0.0
    running_min_low_index: int = -1
    # Structure levels for chart rendering (VH/VL/PH/PL lines with start/end)
    levels: list[StructureLevel] = field(default_factory=list)

    def to_structure_dict(self, timestamps: list[int] | None = None) -> dict:
        """Serialize to the StructureResult format the frontend expects.

        If timestamps is provided, all index fields are converted to timestamps
        so the frontend can match by open_time instead of array index.
        This fixes the offset issue when the frontend has more candles loaded
        (e.g. from scroll-back history loading).
        """
        def idx_to_t(idx: int) -> int:
            if timestamps and 0 <= idx < len(timestamps):
                return timestamps[idx]
            return idx  # fallback to raw index

        levels_out = []
        for lv in self.levels:
            levels_out.append({
                "startIndex": idx_to_t(lv.start_index),
                "endIndex": idx_to_t(lv.end_index),
                "price": lv.price,
                "type": lv.type,
            })

        markers_out = []
        for m in self.markers:
            mc = dict(m)
            mc["index"] = idx_to_t(m["index"])
            markers_out.append(mc)

        swings_out = []
        for sw in self.validated_swings:
            sc = dict(sw)
            sc["index"] = idx_to_t(sw["index"])
            swings_out.append(sc)

        return {
            "swingHighs": [],
            "swingLows": [],
            "levels": levels_out,
            "markers": markers_out,
            "validatedSwings": swings_out,
            "currentVH": {"price": self.last_vh, "index": idx_to_t(self.last_vh_index)},
            "currentVL": {"price": self.last_vl, "index": idx_to_t(self.last_vl_index)},
            "currentPH": {"price": self.last_ph, "index": idx_to_t(self.last_ph_index)}
                if self.last_ph_index >= 0 else None,
            "currentPL": {"price": self.last_pl, "index": idx_to_t(self.last_pl_index)}
                if self.last_pl_index >= 0 else None,
            # WS-3 (spec §11.10): running impulse peak/trough since the most
            # recent BOS/MSS. None when no active tracker for that side.
            "runningMaxHigh": (
                {"price": self.running_max_high,
                 "index": idx_to_t(self.running_max_high_index)}
                if self.running_max_high_index >= 0 else None
            ),
            "runningMinLow": (
                {"price": self.running_min_low,
                 "index": idx_to_t(self.running_min_low_index)}
                if self.running_min_low_index >= 0 else None
            ),
            "currentTrend": "up" if self.trend == "long" else "down",
            "useTimestamps": timestamps is not None,
        }


# ── Candle access helper ─────────────────────────────────────────────────────


def _get_ohlc(c: dict) -> tuple[float, float, float, float]:
    """Extract OHLC as floats from either compact or verbose candle dict."""
    o = float(c.get("o", c.get("open", 0)))
    h = float(c.get("h", c.get("high", 0)))
    l_ = float(c.get("l", c.get("low", 0)))
    cl = float(c.get("c", c.get("close", 0)))
    return o, h, l_, cl


# ── Helpers ──────────────────────────────────────────────────────────────────


def closed_above(c: dict, level: float) -> bool:
    _, _, _, cl = _get_ohlc(c)
    return cl > level


def closed_below(c: dict, level: float) -> bool:
    _, _, _, cl = _get_ohlc(c)
    return cl < level


def is_swing_high(candles: list[dict], i: int) -> bool:
    """3-candle swing high: high >= left high AND high > right high."""
    if i < 1 or i >= len(candles) - 1:
        return False
    _, h, _, _ = _get_ohlc(candles[i])
    _, h_prev, _, _ = _get_ohlc(candles[i - 1])
    _, h_next, _, _ = _get_ohlc(candles[i + 1])
    return h >= h_prev and h > h_next


def is_swing_low(candles: list[dict], i: int) -> bool:
    """3-candle swing low: low <= left low AND low < right low."""
    if i < 1 or i >= len(candles) - 1:
        return False
    _, _, l_, _ = _get_ohlc(candles[i])
    _, _, l_prev, _ = _get_ohlc(candles[i - 1])
    _, _, l_next, _ = _get_ohlc(candles[i + 1])
    return l_ <= l_prev and l_ < l_next


def shares_candles(index_a: int, index_b: int) -> bool:
    """Do two swing patterns (centered at index_a and index_b) share any candle?"""
    return abs(index_a - index_b) <= 2


def _lowest_swing_low_in_range(
    candles: list[dict],
    from_index: int,
    to_index: int,
    exclude_index: int | None = None,
) -> dict | None:
    """Find the lowest swing low in candle range [from, to] — the TRUE arc extreme.
    v2.1 (NO substitution): the committed low MUST be the true lowest swing-low of the
    arc — we never 'pick a different low'. So we find the true extreme WITHOUT any
    non-share filtering inside the search, THEN check it: if that true extreme collides
    by non-share — within ±2 of the anchor (exclude_index) OR within ±2 of the break
    candle (to_index) — the arc is too tight = noise → return None (caller's throwaway
    rolls the whole tight pair up). We NEVER substitute a higher (non-extreme) low,
    because that would just turn a high-закол into a LOW-закол."""
    lowest: dict | None = None
    start = max(1, from_index)
    end = min(to_index, len(candles) - 2)
    for i in range(start, end + 1):
        if is_swing_low(candles, i):
            _, _, l_, _ = _get_ohlc(candles[i])
            if lowest is None or l_ < lowest["price"]:
                lowest = {"index": i, "price": l_, "type": "low"}
    if lowest is not None and (
        (exclude_index is not None and shares_candles(lowest["index"], exclude_index))
        or shares_candles(lowest["index"], to_index)
    ):
        return None  # true extreme collides → roll up, do NOT substitute
    return lowest


def _highest_swing_high_in_range(
    candles: list[dict],
    from_index: int,
    to_index: int,
    exclude_index: int | None = None,
) -> dict | None:
    """Find the highest swing high in candle range [from, to] — the TRUE arc extreme.
    v2.1 (NO substitution): mirror of _lowest_swing_low_in_range. Find the true highest
    swing-high WITHOUT non-share filtering inside the search, THEN if that true extreme
    is within ±2 of the anchor (exclude_index) OR the break candle (to_index) → return
    None (roll up). NEVER substitute a lower (non-extreme) high — that would create a
    HIGH-закол."""
    highest: dict | None = None
    start = max(1, from_index)
    end = min(to_index, len(candles) - 2)
    for i in range(start, end + 1):
        if is_swing_high(candles, i):
            _, h, _, _ = _get_ohlc(candles[i])
            if highest is None or h > highest["price"]:
                highest = {"index": i, "price": h, "type": "high"}
    if highest is not None and (
        (exclude_index is not None and shares_candles(highest["index"], exclude_index))
        or shares_candles(highest["index"], to_index)
    ):
        return None  # true extreme collides → roll up, do NOT substitute
    return highest


def _has_swing_low_between(candles: list[dict], from_idx: int, to_idx: int) -> bool:
    """Check if any swing low exists in candle range (from, to) exclusive."""
    for k in range(max(1, from_idx + 1), min(to_idx, len(candles) - 1)):
        if is_swing_low(candles, k):
            return True
    return False


def _has_swing_high_between(candles: list[dict], from_idx: int, to_idx: int) -> bool:
    """Check if any swing high exists in candle range (from, to) exclusive."""
    for k in range(max(1, from_idx + 1), min(to_idx, len(candles) - 1)):
        if is_swing_high(candles, k):
            return True
    return False


def _is_wick_covered(
    candles: list[dict],
    center_index: int,
    from_index: int,
    to_index: int,
    swing_type: str,  # "high" | "low"
) -> bool:
    """Check if a swing's center candle wick is 'covered' by a body of another candle.
    For 'high': some candle's body bottom (min(open,close)) <= center candle's low.
    For 'low':  some candle's body top  (max(open,close)) >= center candle's high.
    Range [from, to] inclusive; skips the center candle itself."""
    from_clamped = max(0, from_index)
    to_clamped = min(to_index, len(candles) - 1)
    if swing_type == "high":
        _, _, target_low, _ = _get_ohlc(candles[center_index])
        for j in range(from_clamped, to_clamped + 1):
            if j == center_index:
                continue
            o, _, _, cl = _get_ohlc(candles[j])
            if min(o, cl) <= target_low:
                return True
    else:
        _, target_high, _, _ = _get_ohlc(candles[center_index])
        for j in range(from_clamped, to_clamped + 1):
            if j == center_index:
                continue
            o, _, _, cl = _get_ohlc(candles[j])
            if max(o, cl) >= target_high:
                return True
    return False


# ── Main algorithm ───────────────────────────────────────────────────────────

MSS_MIN_GAP = 3


def compute_trend_v2(candles: list[dict]) -> TrendResultV2:
    """Run the Connected Arc market structure algorithm.

    Returns trend direction ("long" / "short"), BOS/MSS counts,
    last VH/VL prices, and all BOS/MSS markers.
    """
    return _compute_trend_v2_impl(candles)


def _compute_trend_v2_impl(candles: list[dict]) -> TrendResultV2:
    if len(candles) < 5:
        return TrendResultV2(
            trend="long", bos_count=0, mss_count=0,
            last_vh=0.0, last_vl=0.0, markers=[],
        )

    markers: list[dict] = []
    validated_swings: list[dict] = []
    raw_levels: list[list] = []  # [[start_index, price, type_str], ...]

    # ── Bootstrap: find first non-sharing swing high and swing low ────────
    first_high: dict | None = None
    first_low: dict | None = None

    for i in range(1, len(candles) - 1):
        if first_high is None and is_swing_high(candles, i):
            _, h, _, _ = _get_ohlc(candles[i])
            first_high = {"index": i, "price": h, "type": "high"}
        if first_low is None and is_swing_low(candles, i):
            _, _, l_, _ = _get_ohlc(candles[i])
            first_low = {"index": i, "price": l_, "type": "low"}
        if first_high is not None and first_low is not None:
            break

    if first_high is None or first_low is None:
        return TrendResultV2(
            trend="long", bos_count=0, mss_count=0,
            last_vh=0.0, last_vl=0.0, markers=[],
        )

    # If bootstrap pair shares candles, replace whichever came second
    if shares_candles(first_high["index"], first_low["index"]):
        if first_high["index"] > first_low["index"]:
            first_high = None
            for i in range(first_low["index"] + 2, len(candles) - 1):
                if is_swing_high(candles, i) and not shares_candles(i, first_low["index"]):
                    _, h, _, _ = _get_ohlc(candles[i])
                    first_high = {"index": i, "price": h, "type": "high"}
                    break
        else:
            first_low = None
            for i in range(first_high["index"] + 2, len(candles) - 1):
                if is_swing_low(candles, i) and not shares_candles(i, first_high["index"]):
                    _, _, l_, _ = _get_ohlc(candles[i])
                    first_low = {"index": i, "price": l_, "type": "low"}
                    break
        if first_high is None or first_low is None:
            return TrendResultV2(
                trend="long", bos_count=0, mss_count=0,
                last_vh=0.0, last_vl=0.0, markers=[],
            )

    # ── Initial trend ────────────────────────────────────────────────────
    if abs(first_high["index"] - first_low["index"]) <= 2:
        mid_price = (first_high["price"] + first_low["price"]) / 2
        latest_idx = max(first_high["index"], first_low["index"])
        _, _, _, close_val = _get_ohlc(candles[latest_idx])
        trend = "up" if close_val > mid_price else "down"
    else:
        trend = "down" if first_high["index"] < first_low["index"] else "up"

    vh_price = first_high["price"]
    vh_index = first_high["index"]
    vl_price = first_low["price"]
    vl_index = first_low["index"]

    validated_swings.append(first_high)
    validated_swings.append(first_low)

    raw_levels.append([vh_index, vh_price, "VH"])
    raw_levels.append([vl_index, vl_price, "VL"])

    ph_price: float | None = None
    ph_index: int | None = None
    pl_price: float | None = None
    pl_index: int | None = None

    # Raw levels tracking (for PH/PL validation — mutable type reference)
    last_ph_level: list | None = None  # [start_index, price, type_str] or None
    last_pl_level: list | None = None

    # ── Walk through candles ─────────────────────────────────────────────
    start_idx = max(first_high["index"], first_low["index"]) + 1

    last_broken_ceiling = -math.inf
    last_broken_floor = math.inf
    min_ph_price = -math.inf
    max_pl_price = math.inf
    last_mss_dir: str | None = None
    last_mss_idx = -999999

    # WS-3 (spec §11.10) — running peak/trough since the most recent
    # structural break. Sentinel "no active tracker" = (+/-inf price, -1
    # index). Activated on BOS/MSS, cleared on trend flip.
    running_max_high_v: float = -math.inf
    running_max_high_idx: int = -1
    running_min_low_v: float = math.inf
    running_min_low_idx: int = -1

    for i in range(start_idx, len(candles)):
        c = candles[i]

        # WS-3 (spec §11.10) — extend the active running peak/trough with this
        # bar BEFORE any transition check. Idempotent w.r.t. transition resets
        # (they overwrite the tracker to the break bar's high/low, which equals
        # what we'd update here on the same i). Sentinel index -1 = inactive.
        _, _h_iter, _l_iter, _ = _get_ohlc(c)
        if running_max_high_idx >= 0 and _h_iter > running_max_high_v:
            running_max_high_v = _h_iter
            running_max_high_idx = i
        if running_min_low_idx >= 0 and _l_iter < running_min_low_v:
            running_min_low_v = _l_iter
            running_min_low_idx = i

        if trend == "up" or trend == "neutral":
            # ── UPTREND LOGIC ────────────────────────────────────────────

            current_high = ph_price if ph_price is not None else vh_price

            # Body close above ceiling? → BOS/MSS
            if closed_above(c, current_high) and current_high > last_broken_ceiling:
                # ── PH→VH promotion pullback-gate (Rule 3, spec
                # 2026-06-19-mss-ph-promotion-pullback-gate-design §6 option (b))
                # Compute the candidate arc VL FIRST so the promotion can be
                # gated on whether a real pullback low was carved. Previously
                # the PH→VH mutation (`last_ph_level[2]="VH"`) fired
                # unconditionally on any continuation close above the PH →
                # spurious intra-impulse VH + vh_index/price desync (ONDO 1h).
                ceiling_index = ph_index if ph_index is not None else vh_index
                ph_confirmed = (
                    ph_index is not None
                    and _has_swing_low_between(candles, ph_index, i)
                )
                vl_search_from = ph_index if (ph_index is not None and ph_confirmed) else vh_index
                new_vl = _lowest_swing_low_in_range(candles, vl_search_from, i, ceiling_index)

                # Body coverage check — a wick-covered "low" isn't a real arc VL.
                if new_vl is not None and not _is_wick_covered(
                    candles, ceiling_index, ceiling_index, new_vl["index"], "high"
                ):
                    new_vl = None

                # GATE (Rule 3): a PH pierced by an impulse with NO carve-able
                # arc VL is a throwaway intra-impulse high — cancel it (exactly
                # like the shadowed cancellation at the `closed_above(PH)` branch
                # below) instead of promoting. No marker, no promotion, no
                # last_broken_ceiling update; fall THROUGH (no `continue`) so a
                # new, higher PH can form to the right (same semantics as the
                # existing cancellation path).
                if ph_price is not None and new_vl is None:
                    min_ph_price = ph_price
                    ph_price = None
                    ph_index = None
                    last_ph_level = None
                else:
                    marker_type = "BOS" if trend == "up" else "MSS"
                    markers.append({
                        "index": i, "type": marker_type,
                        "price": current_high, "direction": "up",
                    })

                    last_broken_ceiling = current_high
                    last_broken_floor = math.inf
                    min_ph_price = -math.inf

                    # WS-3 — reset running_max_high anchor at the break candle.
                    # Going-short tracker is cleared (irrelevant in long leg).
                    _, _h_brk, _, _ = _get_ohlc(c)
                    running_max_high_v = _h_brk
                    running_max_high_idx = i
                    running_min_low_v = math.inf
                    running_min_low_idx = -1

                    # Validate PH → VH (now gated: only on a real arc-VL carve)
                    if last_ph_level is not None:
                        last_ph_level[2] = "VH"

                    if new_vl is not None:
                        vl_price = new_vl["price"]
                        vl_index = new_vl["index"]
                        raw_levels.append([new_vl["index"], new_vl["price"], "VL"])
                        validated_swings.append(new_vl)

                        if ph_price is not None:
                            validated_swings.append({"index": ph_index, "price": ph_price, "type": "high"})
                        # Note: recovery_high path removed 2026-06-01 per operator
                        # decision — it was a synthetic chart-cosmetic that added
                        # non-validated swings to validated_swings (mixing real
                        # validated PH-promotions with "highest pivot in path").
                        # Polluted RSI divergence inputs (RsiOverlay reads
                        # validated_swings) and added rare-edge-case complexity
                        # for ~zero structural benefit. Chain alternation is
                        # guaranteed by the alternated[] filter regardless.

                    vh_price = current_high
                    vh_index = i if new_vl is not None else vh_index
                    ph_price = None
                    ph_index = None
                    pl_price = None
                    pl_index = None
                    last_ph_level = None
                    last_pl_level = None
                    trend = "up"
                    continue

            # PH cancellation: price closed above PH before a swing low appeared
            if ph_price is not None and closed_above(c, ph_price):
                if not _has_swing_low_between(candles, ph_index, i):
                    min_ph_price = ph_price
                    ph_price = None
                    ph_index = None
                    last_ph_level = None

            # Body close below floor? → MSS down
            if closed_below(c, vl_price) and vl_price < last_broken_floor:
                if last_mss_dir == "up" and i - last_mss_idx < MSS_MIN_GAP:
                    continue

                markers.append({
                    "index": i, "type": "MSS",
                    "price": vl_price, "direction": "down",
                })
                last_mss_dir = "down"
                last_mss_idx = i

                last_broken_floor = vl_price
                last_broken_ceiling = -math.inf
                min_ph_price = -math.inf

                # WS-3 — flip to short: clear running_max_high (now irrelevant);
                # anchor running_min_low at the break candle's low.
                _, _, _l_brk, _ = _get_ohlc(c)
                running_max_high_v = -math.inf
                running_max_high_idx = -1
                running_min_low_v = _l_brk
                running_min_low_idx = i

                # New VH: highest swing high in the arc.
                # exclude_index=vl_index added 2026-06-01 (non-share rule):
                # the new VH's 3-bar window must not share candles with
                # the broken floor anchor. Symmetric with BOS-up arc search
                # which already had exclude_index=ceiling_index.
                new_vh = _highest_swing_high_in_range(candles, vl_index, i, vl_index)
                if new_vh is not None:
                    vh_price = new_vh["price"]
                    vh_index = new_vh["index"]
                    raw_levels.append([new_vh["index"], new_vh["price"], "VH"])
                    validated_swings.append(new_vh)

                vl_price = vl_price  # keep same price
                vl_index = i
                ph_price = None
                ph_index = None
                pl_price = None
                pl_index = None
                last_ph_level = None
                last_pl_level = None
                trend = "down"
                continue

            # ── Inline PH detection ──────────────────────────────────────
            # Non-share rule (added 2026-06-01): new PH's 3-bar window
            # must NOT share candles with the latest VL's 3-bar window.
            # Applies to BOTH initial PH formation AND PH upgrade — every
            # swing in the chain (VL → VH → PH → VL → ...) must be ≥3
            # bars from its predecessor's center. Without this gate, a
            # PH could form on the very next bar after BOS-up while its
            # 3-bar window overlaps with the just-found arc VL.
            if i >= 2 and is_swing_high(candles, i - 1):
                sh_index = i - 1

                if shares_candles(sh_index, vl_index):
                    pass  # would share 3-bar window with latest VL → skip
                else:
                    _, sh_price_val, _, _ = _get_ohlc(candles[i - 1])

                    if ph_price is None:
                        if sh_price_val > vh_price and sh_price_val > min_ph_price:
                            ph_price = sh_price_val
                            ph_index = sh_index
                            last_ph_level = [sh_index, sh_price_val, "PH"]
                            raw_levels.append(last_ph_level)
                            min_ph_price = -math.inf
                    else:
                        # Higher PH upgrade. Fix 2026-06-16: keep `last_ph_level`
                        # aligned with the (ph_price, ph_index) state. Previously
                        # the two pointer-update lines were one indent level out
                        # (outside the inner gate), so when the gate REJECTED the
                        # upgrade (swing low between AND wick covered), `ph_price`/
                        # `ph_index` correctly stayed on the OLD PH but the
                        # pointer was pinned to the NEW entry. At BOS-up the wrong
                        # raw_level got mutated to "VH"; both the old PH row and
                        # the new "VH" row got filtered out (old by cleanup since
                        # `ph_index` no longer matched it, new by `validated_set`
                        # filter since `validated_swings` was appended from STATE,
                        # not pointer). Mirror of PL upgrade (line 737-738) which
                        # has always been correct. Collapsed `if A: if B:` into
                        # `if A and B:` to satisfy SIM102; logically equivalent.
                        if sh_price_val > ph_price and (
                            not _has_swing_low_between(candles, ph_index, sh_index)
                            or not _is_wick_covered(candles, ph_index, ph_index, sh_index, "high")
                        ):
                            ph_price = sh_price_val
                            ph_index = sh_index
                            last_ph_level = [sh_index, sh_price_val, "PH"]
                            raw_levels.append(last_ph_level)

        else:
            # ── DOWNTREND LOGIC (mirror of uptrend) ──────────────────────

            current_low = pl_price if pl_price is not None else vl_price

            # Body close below floor? → BOS/MSS
            if closed_below(c, current_low) and current_low < last_broken_floor:
                # ── PL→VL promotion pullback-gate (Rule 3 mirror, spec
                # 2026-06-19-mss-ph-promotion-pullback-gate-design §6 option (b))
                # Compute the candidate arc VH FIRST so the promotion can be
                # gated on whether a real pullback high was carved. Mirror of
                # the up-leg gate above.
                floor_index = pl_index if pl_index is not None else vl_index
                pl_confirmed = (
                    pl_index is not None
                    and _has_swing_high_between(candles, pl_index, i)
                )
                vh_search_from = pl_index if (pl_index is not None and pl_confirmed) else vl_index
                new_vh = _highest_swing_high_in_range(candles, vh_search_from, i, floor_index)

                # Body coverage check — a wick-covered "high" isn't a real arc VH.
                if new_vh is not None and not _is_wick_covered(
                    candles, floor_index, floor_index, new_vh["index"], "low"
                ):
                    new_vh = None

                # GATE (Rule 3 mirror): a PL pierced by a down-impulse with NO
                # carve-able arc VH is throwaway intra-impulse low — cancel it
                # (mirror of the `closed_below(PL)` shadowed cancellation below)
                # instead of promoting. No marker, no promotion, no
                # last_broken_floor update; fall THROUGH (no `continue`).
                if pl_price is not None and new_vh is None:
                    max_pl_price = pl_price
                    pl_price = None
                    pl_index = None
                    last_pl_level = None
                else:
                    marker_type = "BOS" if trend == "down" else "MSS"
                    markers.append({
                        "index": i, "type": marker_type,
                        "price": current_low, "direction": "down",
                    })

                    last_broken_floor = current_low
                    last_broken_ceiling = -math.inf
                    max_pl_price = math.inf

                    # WS-3 — reset running_min_low anchor at the break candle.
                    # Going-long tracker is cleared (irrelevant in short leg).
                    _, _, _l_brk, _ = _get_ohlc(c)
                    running_min_low_v = _l_brk
                    running_min_low_idx = i
                    running_max_high_v = -math.inf
                    running_max_high_idx = -1

                    # Validate PL → VL (now gated: only on a real arc-VH carve)
                    if last_pl_level is not None:
                        last_pl_level[2] = "VL"

                    if new_vh is not None:
                        vh_price = new_vh["price"]
                        vh_index = new_vh["index"]
                        raw_levels.append([new_vh["index"], new_vh["price"], "VH"])
                        validated_swings.append(new_vh)

                        if pl_price is not None:
                            validated_swings.append({"index": pl_index, "price": pl_price, "type": "low"})
                        # Note: recovery_low path removed 2026-06-01 per operator
                        # decision — mirror of recovery_high removal above.
                        # Same rationale: pollution of validated_swings with
                        # synthetic chart-cosmetic that wasn't a real validated
                        # PL-promotion. Chain alternation handled downstream.

                    vl_price = current_low
                    vl_index = i if new_vh is not None else vl_index
                    ph_price = None
                    ph_index = None
                    pl_price = None
                    pl_index = None
                    last_ph_level = None
                    last_pl_level = None
                    trend = "down"
                    continue

            # PL cancellation: price closed below PL before a swing high appeared
            if pl_price is not None and closed_below(c, pl_price):
                if not _has_swing_high_between(candles, pl_index, i):
                    max_pl_price = pl_price
                    pl_price = None
                    pl_index = None
                    last_pl_level = None

            # Body close above ceiling? → MSS up
            if closed_above(c, vh_price) and vh_price > last_broken_ceiling:
                if last_mss_dir == "down" and i - last_mss_idx < MSS_MIN_GAP:
                    continue

                markers.append({
                    "index": i, "type": "MSS",
                    "price": vh_price, "direction": "up",
                })
                last_mss_dir = "up"
                last_mss_idx = i

                last_broken_ceiling = vh_price
                last_broken_floor = math.inf
                max_pl_price = math.inf

                # WS-3 — flip to long: clear running_min_low (now irrelevant);
                # anchor running_max_high at the break candle's high.
                _, _h_brk, _, _ = _get_ohlc(c)
                running_max_high_v = _h_brk
                running_max_high_idx = i
                running_min_low_v = math.inf
                running_min_low_idx = -1

                # New VL: lowest swing low in the arc.
                # exclude_index=vh_index added 2026-06-01 (non-share rule):
                # the new VL's 3-bar window must not share candles with
                # the broken ceiling anchor. Symmetric with BOS-down arc
                # search which already had exclude_index=floor_index.
                new_vl = _lowest_swing_low_in_range(candles, vh_index, i, vh_index)
                if new_vl is not None:
                    vl_price = new_vl["price"]
                    vl_index = new_vl["index"]
                    raw_levels.append([new_vl["index"], new_vl["price"], "VL"])
                    validated_swings.append(new_vl)

                vh_price = vh_price  # keep same price
                vh_index = i
                ph_price = None
                ph_index = None
                pl_price = None
                pl_index = None
                last_ph_level = None
                last_pl_level = None
                trend = "up"
                continue

            # ── Inline PL detection ──────────────────────────────────────
            # Non-share rule (mirror of PH detection, added 2026-06-01):
            # new PL's 3-bar window must NOT share candles with the
            # latest VH's 3-bar window. Applies to BOTH initial PL
            # formation AND PL upgrade.
            if i >= 2 and is_swing_low(candles, i - 1):
                sl_index = i - 1

                if shares_candles(sl_index, vh_index):
                    pass  # would share 3-bar window with latest VH → skip
                else:
                    _, _, sl_price_val, _ = _get_ohlc(candles[i - 1])

                    if pl_price is None:
                        if sl_price_val < vl_price and sl_price_val < max_pl_price:
                            pl_price = sl_price_val
                            pl_index = sl_index
                            last_pl_level = [sl_index, sl_price_val, "PL"]
                            raw_levels.append(last_pl_level)
                            max_pl_price = math.inf
                    else:
                        # Lower PL upgrade
                        if sl_price_val < pl_price:
                            if (
                                not _has_swing_high_between(candles, pl_index, sl_index)
                                or not _is_wick_covered(candles, pl_index, pl_index, sl_index, "low")
                            ):
                                pl_price = sl_price_val
                                pl_index = sl_index
                                last_pl_level = [sl_index, sl_price_val, "PL"]
                                raw_levels.append(last_pl_level)

    # ── Deduplicate validated swings ─────────────────────────────────────
    seen_swings: set[str] = set()
    unique_swings: list[dict] = []
    for s in validated_swings:
        key = f"{s['index']}_{s['type']}_{s['price']}"
        if key not in seen_swings:
            seen_swings.add(key)
            unique_swings.append(s)

    unique_swings.sort(key=lambda s: s["index"])

    # ── Enforce strict alternation (HIGH-LOW-HIGH-LOW) + monotonicity ──
    alternated: list[dict] = []
    for s in unique_swings:
        if len(alternated) == 0:
            alternated.append(s)
            continue
        prev = alternated[-1]
        if s["type"] == prev["type"]:
            if s["type"] == "high" and s["price"] > prev["price"]:
                alternated[-1] = s
            elif s["type"] == "low" and s["price"] < prev["price"]:
                alternated[-1] = s
        else:
            # Enforce strict monotonicity: skip if index not strictly greater
            if s["index"] > prev["index"]:
                alternated.append(s)

    # ── Compute vl/vh counts after last MSS from validated_swings ──────
    # Find the index of the last MSS marker
    last_mss_marker_idx_final = -1
    for m in markers:
        if m["type"] == "MSS":
            last_mss_marker_idx_final = m["index"]

    vl_count_from_swings = 0
    vh_count_from_swings = 0
    if last_mss_marker_idx_final >= 0:
        for s in alternated:
            if s["index"] > last_mss_marker_idx_final:
                if s["type"] == "low":
                    vl_count_from_swings += 1
                elif s["type"] == "high":
                    vh_count_from_swings += 1

    # ── Cleanup: drop stale PH/PL from raw_levels ─────────────────────────
    # PH/PL detection AND upgrade both append to raw_levels without
    # removing predecessors. Cancellation nullifies the local ph_price /
    # pl_price but leaves the raw_levels entry behind. Result: chains of
    # `"PL"` entries (upgrade-out) and orphaned `"PH"`/`"PL"` (cancelled)
    # accumulate in raw_levels. The renderer's "active potential" branch
    # picks the LAST entry of type PH/PL and draws it dashed to the chart
    # edge — that's how stale potentials become visually misleading.
    #
    # We trust the local ph_index / pl_index as the source of truth for
    # "currently active". Anything else of type PH/PL is stale.
    #
    # INVARIANT (verified before adding this filter): when a PH/PL gets
    # validated by BOS/MSS, the type is mutated IN PLACE (line 396:
    # `last_ph_level[2] = "VH"`, line 525: `last_pl_level[2] = "VL"`) —
    # the entry stays in raw_levels but its type is now VH/VL. So the
    # filter below correctly preserves validated levels: they no longer
    # match `rl[2] == "PH"`/`"PL"` and pass through via the negation.
    # ph_index / pl_index meanwhile are nulled by every reset path
    # (BOS lines 433-434, MSS down 477-478, MSS up 606-607, BOS down
    # 563-564, cancellation 444-445 / 573-574), so a stale row's
    # start_index won't match a still-active sibling's index.
    raw_levels = [
        rl for rl in raw_levels
        if rl[2] not in ("PH", "PL")
        or (rl[2] == "PH" and ph_index is not None and rl[0] == ph_index)
        or (rl[2] == "PL" and pl_index is not None and rl[0] == pl_index)
    ]

    # ── Post-processing: filter rawLevels and compute endIndex ──────────
    validated_set = {f"{s['index']}_{s['price']}" for s in alternated}
    filtered_levels = [
        rl for rl in raw_levels
        if rl[2] in ("PH", "PL") or f"{rl[0]}_{rl[1]}" in validated_set
    ]

    # Compute next replacement indices for ceiling/floor groups
    n_candles = len(candles)
    n_levels = len(filtered_levels)
    next_ceiling_start = [n_candles] * n_levels
    next_floor_start = [n_candles] * n_levels
    last_ceiling = n_candles
    last_floor = n_candles
    for k in range(n_levels - 1, -1, -1):
        is_ceiling = filtered_levels[k][2] in ("VH", "PH")
        if is_ceiling:
            next_ceiling_start[k] = last_ceiling
            last_ceiling = filtered_levels[k][0]
        else:
            next_floor_start[k] = last_floor
            last_floor = filtered_levels[k][0]

    levels: list[StructureLevel] = []
    for k in range(n_levels):
        rl = filtered_levels[k]
        start_idx_l = rl[0]
        price_l = rl[1]
        type_l = rl[2]
        is_ceiling = type_l in ("VH", "PH")

        replacement_idx = next_ceiling_start[k] if is_ceiling else next_floor_start[k]
        end_index = min(replacement_idx, n_candles - 1)

        # Scan for body break
        for j in range(start_idx_l + 1, end_index + 1):
            _, _, _, cl = _get_ohlc(candles[j])
            if is_ceiling:
                if cl > price_l:
                    end_index = j
                    break
            else:
                if cl < price_l:
                    end_index = j
                    break

        if end_index > start_idx_l:
            levels.append(StructureLevel(
                start_index=start_idx_l,
                end_index=end_index,
                price=price_l,
                type=type_l,
            ))

    # ── Build result ─────────────────────────────────────────────────────
    bos_count = sum(1 for m in markers if m["type"] == "BOS")
    mss_count = sum(1 for m in markers if m["type"] == "MSS")
    bos_after_last_mss = sum(
        1 for m in markers
        if m["type"] == "BOS" and m["index"] > last_mss_marker_idx_final
    )

    # Map internal trend to external direction
    if trend == "up":
        direction = "long"
    elif trend == "down":
        direction = "short"
    else:
        # neutral — should not happen with enough data, default to long
        direction = "long"

    return TrendResultV2(
        trend=direction,
        bos_count=bos_count,
        mss_count=mss_count,
        last_vh=vh_price,
        last_vl=vl_price,
        markers=markers,
        validated_swings=alternated,
        vl_after_mss=vl_count_from_swings,
        vh_after_mss=vh_count_from_swings,
        bos_after_last_mss=bos_after_last_mss,
        last_mss_index=last_mss_marker_idx_final,
        last_ph=ph_price if ph_price is not None else 0.0,
        last_pl=pl_price if pl_price is not None else 0.0,
        last_ph_index=ph_index if ph_index is not None else -1,
        last_pl_index=pl_index if pl_index is not None else -1,
        last_vh_index=vh_index,
        last_vl_index=vl_index,
        # WS-3 (spec §11.10) — sentinel (+/-inf, -1) maps to public (0.0, -1).
        running_max_high=(
            running_max_high_v if running_max_high_idx >= 0 else 0.0
        ),
        running_max_high_index=running_max_high_idx,
        running_min_low=(
            running_min_low_v if running_min_low_idx >= 0 else 0.0
        ),
        running_min_low_index=running_min_low_idx,
        levels=levels,
    )
