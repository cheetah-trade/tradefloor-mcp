"""Definitions that travel with the answer.

A model reading ``golden_retriever`` has to do something with it, and what it
does without a definition is guess. Most of these terms it would guess right;
that one it would not, because it is this engine's name for what the rest of the
world calls the golden pocket.

Only the terms actually present in a given answer are attached, so the payload
stays the size of the answer rather than the size of the vocabulary.
"""

from __future__ import annotations

import json
import re
from typing import Any

#: Written for a model to relay to a person, so each one is a sentence rather
#: than a lookup key. Prices and levels are never described as advice.
TERMS: dict[str, str] = {
    "ChoCh": (
        "Change of character: a break that goes AGAINST the prevailing structure "
        "and is the first evidence the trend may be turning."
    ),
    "BOS": (
        "Break of structure: a break in the SAME direction as the prevailing "
        "trend, so it reads as continuation rather than reversal."
    ),
    "MSS": "Market structure shift. This engine's internal name for a ChoCh.",
    "discount": (
        "The lower half of the pullback range, measured from the impulse. In an "
        "uptrend this is the side buyers look to enter from."
    ),
    "premium": (
        "The upper half of the pullback range. In an uptrend this is the side "
        "sellers look to enter from."
    ),
    "equilibrium": "The midpoint of the pullback range, the 50% retracement.",
    "ote": (
        "Optimal trade entry: roughly the 0.62 to 0.79 retracement of the "
        "impulse, the band commonly watched for continuation entries."
    ),
    "golden_retriever": (
        "This engine's name for the golden pocket: the 0.618 to 0.65 "
        "retracement, the tightest part of the OTE band."
    ),
    "fvg": (
        "Fair value gap: a three-candle imbalance where price moved so fast that "
        "the middle candle's range was left untraded on one side."
    ),
    "order_block": (
        "Order block: the last opposing candle before an impulsive move, taken "
        "as the area institutional orders were filled from."
    ),
    "liquidity_sweep": (
        "A break whose candle first ran past prior swing levels, taking out the "
        "stops resting there, before closing back inside. Distinguishes an "
        "engineered break from a clean one."
    ),
    "validated_swing": (
        "A swing high or low the engine confirmed, as opposed to a potential one "
        "still waiting on a close to confirm it."
    ),
    "potential_high": (
        "A swing high that has formed but is not yet confirmed by structure. It "
        "can still be invalidated."
    ),
    "potential_low": (
        "A swing low that has formed but is not yet confirmed by structure. It "
        "can still be invalidated."
    ),
    "impulse_peak": (
        "The highest price reached since the most recent structural break. "
        "Pullback ranges are measured from it."
    ),
    "impulse_trough": (
        "The lowest price reached since the most recent structural break. "
        "Pullback ranges are measured from it."
    ),
    "block_age_candles": "How many candles have closed since the block formed.",
    "price_is_inside": (
        "The last candle CLOSED inside this zone. A stricter reading than "
        "price_touched, and on a high timeframe it is only one sample per bar."
    ),
    "price_touched": (
        "Price REACHED this zone during the last candle, wick included, even if "
        "it closed back outside. A wick into a zone is a test of it, which is "
        "usually what a pullback setup is waiting for."
    ),
}

#: Some keys carry a term without naming it, so a plain key scan would miss them.
_IMPLIED: dict[str, tuple[str, ...]] = {
    "sweeps": ("liquidity_sweep",),
    "validated_swings": ("validated_swing",),
    "order_blocks": ("order_block",),
    "fvg": ("fvg",),
}


def for_payload(payload: Any) -> dict[str, str]:
    """Definitions for the terms this particular answer actually uses."""
    blob = json.dumps(payload, default=str)
    # Word boundaries, not substrings: "ote" lives inside "potential_low", and
    # attaching a definition for a zone to an answer that has no zones is
    # precisely the confusion this module exists to prevent.
    used = {
        term: text
        for term, text in TERMS.items()
        if re.search(rf"\b{re.escape(term)}\b", blob)
    }
    for key, implied in _IMPLIED.items():
        if f'"{key}"' in blob:
            used.update({t: TERMS[t] for t in implied if t in TERMS})
    return dict(sorted(used.items()))
