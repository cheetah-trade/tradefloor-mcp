# Extracted from the TradeFloor monorepo by
# backend/scripts/extract_opensource_mcp.py -- do not edit by hand.
# Upstream: app/indicators/mss_sweeps.py
"""MSS liquidity sweep («закол») — CHART-VISIBILITY-ONLY downstream helper.

Detects the liquidity-sweep / stop-hunt event on the MSS pipeline and maps it
to an **ms-time sibling payload** for the chart structure route. A sweep is a
wick that pierces a prior swing level then the body closes back inside (grabs
resting liquidity, then reclaims) — in the MSS school this VALIDATES the
reversal the break sprang from. It is a filter-strengthener: NO entries, NO
stops (operator-locked 2026-07-16, «break-validates-swing»).

Scope (SCOPE v2, operator 2026-07-23) — CHART-ONLY, fully-isolated additive
layer:
  * ``structure_events`` + ``detect_sweeps`` are PORTED VERBATIM from the
    harness (``claude/admiring-keller-55b2d1:backend/scripts/local_matcher.py``)
    — the operator-locked definition; do NOT re-derive it. The ONLY deviation
    from the harness is OHLC extraction: the harness reads verbose ``c["high"]``
    keys, but prod candles are SHORT-key ``{t,o,h,l,c}`` with STRING values
    (``candle_storage`` / the chart route's ``candles_data``), so a verbatim
    ``c["high"]`` KeyErrors on every prod candle. Both functions read OHLC
    through the canonical accessor ``market_structure_v2._get_ohlc`` (short-first,
    verbose-fallback, str→float) → they work on BOTH prod (short/str) AND verbose
    test/harness fixtures.
  * ``build_sweep_payload`` maps ``detect_sweeps`` output to the ms-time sibling
    shape (mirrors the ``blocks`` sibling on ``candles_structure_route_runtime``)
    so the FE draws by TIME via ``timeToCoordinate`` — no ``resolveStructureIndices``
    dance, and the golden ``compute_trend_v2`` / ``TrendResultV2`` / serializer
    stay byte-identical (this module is a DOWNSTREAM consumer of the engine, not
    a change to it).

This module still imports NOTHING from the adapter / state builder / matcher
(source-grep tripwire in ``tests/test_mss_sweeps.py``) — it is a pure downstream
helper. As of PR-1 of the MSS-school plan (2026-08-25) it has a SECOND reader
besides the chart route: the MSS adapter calls ``sweep_for_last_break`` and the
state builder stores the result under ``state:mss.last_swing_break.sweep``. The
wiring goes one way only (adapter → this module), so the isolation invariant
holds and ``compute_trend_v2`` / the chart payload stay byte-identical.

``result=`` on the three entry points is a REUSE seam, not new behaviour: both
``structure_events`` and ``detect_sweeps`` call the golden engine internally, so
an adapter that already holds a ``TrendResultV2`` would otherwise pay for it
three times per (coin, tf) — on a sequential cycle over ~200 coins that is a
regression for every other consumer of the adapter.
"""

from __future__ import annotations

import logging

from tradefloor_mcp.engine.market_structure_v2 import TrendResultV2, _get_ohlc, compute_trend_v2

_log = logging.getLogger(__name__)


def structure_events(
    candles: list[dict],
    result: TrendResultV2 | None = None,
) -> list[dict]:
    """Per-break structure events carrying the pullback swing legs.

    Verbatim from the harness (§3a) except OHLC via ``_get_ohlc`` (see module
    docstring). ``detect_sweeps`` only reads ``direction / marker_idx / type /
    short_low_idx / short_high_idx``; the remaining fields are carried for the
    ms-time payload (``short_low_px`` / ``short_high_px`` = the sweeper price).
    """
    full = result if result is not None else compute_trend_v2(candles)
    markers = sorted(
        [m for m in full.markers if m.get("type") in ("BOS", "MSS")],
        key=lambda m: int(m["index"]),
    )
    highs = [_get_ohlc(cd)[1] for cd in candles]
    lows = [_get_ohlc(cd)[2] for cd in candles]
    n = len(candles)
    sw = full.validated_swings

    events: list[dict] = []
    for k, m in enumerate(markers):
        i = int(m["index"])
        up = m["direction"] == "up"
        pre = [s for s in sw if int(s["index"]) < i]
        last_high = next((s for s in reversed(pre) if s["type"] == "high"), None)
        last_low = next((s for s in reversed(pre) if s["type"] == "low"), None)
        if last_high is None or last_low is None:
            continue  # can't build legs yet

        low_idx, low_px = int(last_low["index"]), float(last_low["price"])
        high_idx, high_px = int(last_high["index"]), float(last_high["price"])

        # long-leg extremum = the impulse after the break, until the NEXT break
        next_i = int(markers[k + 1]["index"]) if k + 1 < len(markers) else n
        window = range(i, min(next_i, n))
        if not window:
            continue
        if up:
            ext_idx = max(window, key=lambda j: highs[j])
            ext_px = highs[ext_idx]
            origin_idx, origin_px = low_idx, low_px  # long leg grows UP from the low
        else:
            ext_idx = min(window, key=lambda j: lows[j])
            ext_px = lows[ext_idx]
            origin_idx, origin_px = high_idx, high_px  # long leg grows DOWN from the high

        events.append(
            {
                "marker_idx": i,
                "type": "ChoCh" if m["type"] == "MSS" else "BOS",
                "direction": "long" if up else "short",
                # SHORT leg (stop anchor): the two swings; the "level" is the broken one.
                "short_low_idx": low_idx,
                "short_low_px": round(low_px, 4),
                "short_high_idx": high_idx,
                "short_high_px": round(high_px, 4),
                "bos_level_px": round(high_px if up else low_px, 4),  # the broken level
                # LONG leg (entry-zone base): origin → extremum of the impulse.
                "long_origin_idx": origin_idx,
                "long_origin_px": round(origin_px, 4),
                "long_ext_idx": ext_idx,
                "long_ext_px": round(ext_px, 4),
                "invalidated_at_idx": next_i if next_i < n else None,
            }
        )
    return events


# ──────────────────────────────────────────────────────────────────────────────
#  LIQUIDITY SWEEP (закол) — BREAK-VALIDATES-SWING (operator 2026-07-16, final).
#  Sweeper = the pullback swing the break SPRANG FROM, checked in the break's OWN
#  direction. up-break(long) → sweeper = pullback LOW (short_low_idx); grabbed
#  lows below + reclaimed up → bullish закол. down-break(short) → sweeper =
#  pullback HIGH (short_high_idx); grabbed highs above + reclaimed down → bearish
#  закол. A swing is a sweeper ONLY when a break confirms it → POINT-IN-TIME,
#  known at the break bar, no repaint. Window = [prev validated swing of SAME
#  type … sweeper]; snagged set = that prev swing IF taken + every internal 3-bar
#  same-type extremum inside it.
#    low-sweeper : L[s] < lvl <= C[s]  AND all(C[k] >= lvl for k in [j..s))
#    high-sweeper: H[s] > lvl >= C[s]  AND all(C[k] <= lvl for k in [j..s))
#  Verbatim from the harness (§3b) except OHLC via ``_get_ohlc``.
# ──────────────────────────────────────────────────────────────────────────────
def detect_sweeps(
    candles: list[dict],
    events: list[dict] | None = None,
    result: TrendResultV2 | None = None,
) -> list[dict]:
    H = [_get_ohlc(cd)[1] for cd in candles]
    L = [_get_ohlc(cd)[2] for cd in candles]
    C = [_get_ohlc(cd)[3] for cd in candles]
    n = len(candles)
    _full = result if result is not None else compute_trend_v2(candles)
    swings = sorted(_full.validated_swings, key=lambda x: int(x["index"]))
    evs = sorted(
        events if events is not None else structure_events(candles, result=result),
        key=lambda e: e["marker_idx"],
    )
    out: list[dict] = []
    seen: set[int] = set()  # dedup: one закол per sweeper swing
    for e in evs:
        lng = e["direction"] == "long"  # up-break → LOW sweeper; down-break → HIGH
        s = int(e["short_low_idx"] if lng else e["short_high_idx"])  # swing the break sprang from
        if s in seen or s > n - 1:
            continue
        typ = "low" if lng else "high"
        # window-start = previous validated swing of the SAME type before the sweeper
        w0 = next(
            (
                int(swings[k]["index"])
                for k in range(len(swings) - 1, -1, -1)
                if swings[k]["type"] == typ and int(swings[k]["index"]) < s
            ),
            None,
        )
        if w0 is None or s - w0 < 3:
            continue
        # snagged set: the previous same-type swing (w0) IF taken + internal 3-bar extrema
        levels: list[tuple[int, float]] = []
        lv0 = L[w0] if lng else H[w0]
        # The SIM114 suppression keeps the verbatim operator-locked harness (§3b)
        # if/elif structure — the two direction branches are NOT collapsed to `or`.
        if lng and L[s] < lv0 <= C[s] and all(C[k] >= lv0 for k in range(w0, s)):
            levels.append((w0, lv0))
        elif (not lng) and H[s] > lv0 >= C[s] and all(C[k] <= lv0 for k in range(w0, s)):
            levels.append((w0, lv0))
        for j in range(w0 + 1, s):
            if lng:  # trough: internal 3-bar LOW, wick below + close back above, held
                if not (L[j] <= L[j - 1] and L[j] <= L[j + 1]):
                    continue
                lv = L[j]
                if L[s] < lv <= C[s] and all(C[k] >= lv for k in range(j, s)):
                    levels.append((j, lv))
            else:  # peak: internal 3-bar HIGH, wick above + close back below, held
                if not (H[j] >= H[j - 1] and H[j] >= H[j + 1]):
                    continue
                lv = H[j]
                if H[s] > lv >= C[s] and all(C[k] <= lv for k in range(j, s)):
                    levels.append((j, lv))
        if not levels:
            continue
        seen.add(s)
        # TIGHTEST (nearest the reclaim) — kept only for a FUTURE boolean filter gate;
        # NOT a stop (stops come from the BOS/ChoCh setup).
        tight = max(levels, key=lambda t: t[1]) if lng else min(levels, key=lambda t: t[1])
        out.append(
            {
                "direction": e["direction"],
                "break_type": e["type"],
                "break_idx": int(e["marker_idx"]),
                "anchor_idx": s,
                "level": round(tight[1], 4),
                "level_idx": tight[0],
                "levels": [{"idx": j, "px": round(lv, 4)} for j, lv in levels],
            }
        )
    return out


def sweep_for_last_break(
    candles: list[dict],
    *,
    result: TrendResultV2 | None = None,
    events: list[dict] | None = None,
) -> dict | None:
    """The закол attached to the MOST RECENT structure break, or ``None``.

    The scorer asks exactly one question of this module — «был ли последний
    слом усилен заколом» — so this is the whole state-facing surface. Returns
    the raw ``detect_sweeps`` entry (the state builder compacts it); ``None``
    means the last break exists but no закол validated it, OR there is no
    break at all. The two cases are indistinguishable here on purpose: the
    state already carries ``last_swing_break`` separately.

    Fail-soft by contract: this runs inside the adapter cycle, where one bad
    pair must never take the whole cycle down.
    """
    try:
        evs = events if events is not None else structure_events(candles, result=result)
        if not evs:
            return None
        last_marker = max(int(e["marker_idx"]) for e in evs)
        for sw in detect_sweeps(candles, events=evs, result=result):
            if int(sw["break_idx"]) == last_marker:
                return sw
        return None
    except Exception:
        _log.debug("mss_sweep_last_break_failed", exc_info=True)
        return None


def build_sweep_payload(
    candles: list[dict],
    timestamps: list[int],
    events: list[dict] | None = None,
) -> list[dict]:
    """Map ``detect_sweeps`` output to the ms-time sibling payload for the chart.

    Mirrors the ``blocks`` sibling shape (ms open-times, drawn via
    ``timeToCoordinate``). Each закол →::

        {"sweeper_time":  <ms open_time of anchor_idx>,
         "sweeper_price": <swing price at anchor_idx: short_low_px | short_high_px>,
         "direction":     "long" | "short",
         "break_type":    "ChoCh" | "BOS",
         "levels": [{"level_time": <ms open_time of levels[i].idx>,
                     "price":      <levels[i].px>}]}

    ``sweeper_price`` lets the FE draw the purple cover-dot exactly on the grey
    swing dot.
    Fail-soft: any error yields ``[]`` and never breaks the chart response.
    """
    try:
        evs = events if events is not None else structure_events(candles)
        sweeps = detect_sweeps(candles, evs)
        if not sweeps:
            return []
        ev_by_marker = {int(e["marker_idx"]): e for e in evs}
        n_ts = len(timestamps)

        out: list[dict] = []
        for sw in sweeps:
            e = ev_by_marker.get(int(sw["break_idx"]))
            if e is None:
                continue
            anchor = int(sw["anchor_idx"])
            if not (0 <= anchor < n_ts):
                continue
            long = sw["direction"] == "long"
            sweeper_price = e["short_low_px"] if long else e["short_high_px"]

            levels: list[dict] = []
            for lv in sw["levels"]:
                li = int(lv["idx"])
                if not (0 <= li < n_ts):
                    continue
                levels.append({"level_time": timestamps[li], "price": lv["px"]})
            if not levels:
                continue

            out.append(
                {
                    "sweeper_time": timestamps[anchor],
                    "sweeper_price": sweeper_price,
                    "direction": sw["direction"],
                    "break_type": sw["break_type"],
                    "levels": levels,
                }
            )
        return out
    except Exception:
        # Fail-soft: an isolated overlay must NEVER break the caller.
        _log.debug("mss_sweep_build_failed", exc_info=True)
        return []
