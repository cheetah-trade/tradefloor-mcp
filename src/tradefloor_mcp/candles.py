"""Where candles come from.

Three sources, in the order a new user meets them:

1. the bundled ETH-15m sample, so the server does something useful the second
   it is installed, with no key, no account and no network;
2. candles the caller passes in directly, from wherever they already keep them;
3. Hyperliquid's public ``candleSnapshot`` endpoint.

Source 3 is worth being precise about: the request leaves from YOUR machine and
YOUR IP. It does not touch TradeFloor at all. Nothing here phones home, and
nothing here needs a key.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from importlib import resources
from typing import Any

HL_INFO_URL = "https://api.hyperliquid.xyz/info"

#: Perps are bare (BTC), tokenised equities carry a dex prefix (xyz:AAPL).
#: Anything else is refused here rather than sent to the exchange to be
#: refused there: a 500 from a venue is not an answer a model can act on.
_COIN_RE = re.compile(r"^[A-Za-z0-9]{1,20}(?::[A-Za-z0-9]{1,20})?$")

#: Hyperliquid allows considerably more than this per IP. We stay well under it
#: on purpose: this package runs on other people's machines, and a library that
#: makes its users look like abusers is a library people uninstall.
_MAX_REQUESTS = 20
_WINDOW_SECONDS = 60.0

#: Nothing is served by analysing more than this, and an unbounded list is
#: an unbounded response straight into a model's context window.
MAX_CANDLES = 20_000

_VALID_INTERVALS = frozenset(
    {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "8h", "12h", "1d", "3d", "1w", "1M"}
)

_INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "8h": 28_800_000,
    "12h": 43_200_000, "1d": 86_400_000, "3d": 259_200_000, "1w": 604_800_000,
    "1M": 2_592_000_000,
}


class RateLimited(RuntimeError):
    """Raised instead of hammering the exchange."""


class _Throttle:
    def __init__(self) -> None:
        self._hits: deque[float] = deque()
        self._lock = threading.Lock()

    def take(self) -> None:
        now = time.monotonic()
        with self._lock:
            while self._hits and now - self._hits[0] > _WINDOW_SECONDS:
                self._hits.popleft()
            if len(self._hits) >= _MAX_REQUESTS:
                wait = _WINDOW_SECONDS - (now - self._hits[0])
                raise RateLimited(
                    f"This client allows {_MAX_REQUESTS} Hyperliquid requests per "
                    f"minute and that budget is spent. Try again in {wait:.0f}s, "
                    f"or pass candles in directly."
                )
            self._hits.append(now)


_throttle = _Throttle()


def load_sample() -> list[dict]:
    """The bundled ETH-15m window: 6000 closed candles.

    This is the exact fixture TradeFloor's own test suite pins its market
    structure against, so the numbers this server reports for it are the
    numbers production reports for it.
    """
    with resources.files("tradefloor_mcp.data").joinpath("eth_15m_sample.json").open(
        encoding="utf-8"
    ) as fh:
        return json.load(fh)


def normalize(candles: list[Any]) -> list[dict]:
    """Coerce arbitrary candle dicts into what the engine reads.

    Accepts compact (``t/o/h/l/c``) and verbose (``open``/``high``/...) keys
    alike. Raises on anything the engine would silently read as zero, because a
    zero high is not a degenerate answer, it is a wrong one.
    """
    if not isinstance(candles, list):
        raise TypeError("candles must be a list of objects")
    if len(candles) < 5:
        raise ValueError(
            f"need at least 5 candles to say anything about structure, got {len(candles)}"
        )
    if len(candles) > MAX_CANDLES:
        raise ValueError(
            f"{len(candles):,} candles is past the {MAX_CANDLES:,} ceiling. "
            f"Structure does not improve past this and the answer stops "
            f"fitting in a reply. Pass a shorter window."
        )

    out: list[dict] = []
    for i, raw in enumerate(candles):
        if not isinstance(raw, dict):
            raise TypeError(f"candle {i} is {type(raw).__name__}, expected an object")
        try:
            row = {
                "t": int(raw.get("t", raw.get("time", raw.get("open_time", 0)))),
                "o": float(raw.get("o", raw.get("open"))),
                "h": float(raw.get("h", raw.get("high"))),
                "l": float(raw.get("l", raw.get("low"))),
                "c": float(raw.get("c", raw.get("close"))),
            }
        except (TypeError, ValueError) as exc:
            raise ValueError(f"candle {i} has unreadable OHLC: {exc}") from exc
        if row["h"] < row["l"]:
            raise ValueError(f"candle {i} has high < low")
        out.append(row)
    return out


def timestamps(candles: list[dict]) -> list[int]:
    """Open times in ms, used to anchor blocks on time rather than on index."""
    return [int(c.get("t", 0)) for c in candles]


def fetch_hl_candles(coin: str, interval: str, limit: int = 1000) -> list[dict]:
    """Pull closed candles from Hyperliquid's public endpoint.

    Leaves from the caller's own IP. TradeFloor is not involved and no
    credential exists. Tokenised equities and commodities live on a separate
    Hyperliquid dex and are addressed as ``xyz:AAPL``; the prefix is the only
    trigger and it is handled here.
    """
    coin = (coin or "").strip()
    if not coin:
        raise ValueError("coin is required, e.g. BTC or xyz:AAPL")
    if not _COIN_RE.match(coin):
        raise ValueError(
            f"{coin!r} is not a symbol shape this venue uses. Perps are bare "
            f"(BTC, ETH, SOL); tokenised equities are prefixed (xyz:AAPL)."
        )
    if interval not in _VALID_INTERVALS:
        raise ValueError(
            f"interval {interval!r} is not one Hyperliquid serves. "
            f"Valid: {', '.join(sorted(_VALID_INTERVALS))}"
        )
    limit = max(5, min(int(limit), 5000))

    span = _INTERVAL_MS[interval] * (limit + 2)
    now_ms = int(time.time() * 1000)
    req: dict[str, Any] = {
        "type": "candleSnapshot",
        "req": {
            "coin": coin,
            "interval": interval,
            "startTime": now_ms - span,
            "endTime": now_ms,
        },
    }
    if ":" in coin:
        req["dex"] = coin.split(":", 1)[0]

    _throttle.take()
    request = urllib.request.Request(
        HL_INFO_URL,
        data=json.dumps(req).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "tradefloor-mcp"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20.0) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"Hyperliquid refused the request for {coin} {interval} "
            f"(HTTP {exc.code}). Check the symbol."
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach Hyperliquid: {exc.reason}") from exc

    if not isinstance(payload, list) or not payload:
        raise ValueError(
            f"Hyperliquid returned no candles for {coin} {interval}. "
            f"Check the symbol: perps are bare (BTC), tokenised equities are "
            f"prefixed (xyz:AAPL)."
        )

    rows = normalize(payload)
    # The last bar is still forming. Structure is a closed-bar concept and
    # including it makes the answer flicker, so it goes.
    if len(rows) > 5:
        rows = rows[:-1]
    # We over-fetch by a couple of bars to be sure the window is covered;
    # honour the limit that was actually asked for.
    return rows[-limit:] if len(rows) > limit else rows
