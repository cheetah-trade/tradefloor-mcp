# tradefloor-mcp

Smart-money market structure as an MCP server. Ask your AI assistant what the
structure of a market is, and get a real answer: trend, character shifts,
liquidity sweeps, fair value gaps, order blocks and pullback zones.

This is the same engine that runs in production at
[TradeFloor](https://tradefloor.co/?utm_source=github&utm_medium=oss&utm_campaign=tradefloor-mcp),
extracted from the production monorepo rather than reimplemented for show.

```
> What is ETH doing structurally on the 1h?

SHORT structure over 1000 candles: 4 character shift(s), 11 continuation
break(s), 2 break(s) since the last shift. Price is inside: discount, ote,
golden_retriever.
```

## It does not phone home

The package makes **no request to TradeFloor**, ever. There is no key, no
account, no telemetry, no usage ping. Read `candles.py`: the only outbound call
in the whole package is to Hyperliquid's public endpoint, it happens only when
you ask for a coin by name, and it leaves from your own machine and your own IP.

Analysing the bundled sample or your own candles touches the network zero times.

The package has exactly one dependency, the MCP SDK itself. The one HTTP call
it can make uses the standard library, so there is no HTTP client in your
install to audit or to trust.

## Install

```bash
claude mcp add tradefloor-structure -- uvx tradefloor-mcp
```

Or by hand, in any MCP client:

```json
{
  "mcpServers": {
    "tradefloor-structure": {
      "command": "uvx",
      "args": ["tradefloor-mcp"]
    }
  }
}
```

Nothing else to configure. It works immediately.

## The three tools

Each takes candles the same way: name a `coin`, or pass your own `candles`, or
pass neither and get a worked example on the bundled ETH-15m data.

### `analyze_structure`

Trend, plus every structural break with its time and price. Character shifts
(ChoCh, called MSS internally) are separated from continuation breaks (BOS),
and you get the last validated swing high and low, the potential levels a
pullback is forming against, and the impulse peak of the current leg.

### `find_liquidity_sweeps`

Stop-hunts. A sweep is a break whose candle first ran the liquidity resting
past one or more prior swing levels before closing back inside. Each result
names the sweeping candle and every level it took out, which is what tells an
engineered break apart from a clean one.

### `find_blocks`

Fair value gaps and order blocks on the current impulse leg, then again per
Fibonacci pullback zone (discount, equilibrium, premium, golden pocket), plus
which zones price is sitting in right now.

## It explains itself, and it can be drawn

Every answer carries definitions for the terms it actually used. A model reading
`golden_retriever` would otherwise guess, and that one it would guess wrong: it
is this engine's name for the golden pocket. Standard terms travel too, so what
reaches the person is the real meaning rather than a plausible one.

Ask for a chart and you get one. Pass `include_candles: true` and the response
carries the candles themselves, on the same time scale as the levels and blocks,
so your assistant can draw the structure it just described.

```
> show me ETH 4h structure and draw it
```

## Bring your own candles

Any venue, any source. Compact or verbose keys both work:

```python
[{"t": 1778300100000, "o": "2320.9", "h": "2321.2", "l": "2315.9", "c": "2316.7"}]
[{"time": 1778300100000, "open": 2320.9, "high": 2321.2, "low": 2315.9, "close": 2316.7}]
```

Candles are validated on the way in. A malformed high raises instead of being
read as zero, because a zero high is not a degenerate answer, it is a wrong one.

Windows are capped at 20,000 candles. Structure does not get better past that,
and an unbounded window means an unbounded reply into your context.

Symbols are checked before anything leaves your machine: perps are bare (BTC),
tokenised equities are prefixed (xyz:AAPL), and anything else is refused here
rather than sent to the venue to be refused there.

## Verifying it is the real engine

The bundled sample is the exact fixture TradeFloor's own test suite pins its
market structure against: 6000 ETH-15m candles, which the engine resolves to
50 structural events and 23 liquidity sweeps (11 bullish, 12 bearish). Those
numbers are asserted both in this repository and upstream, so if they ever
diverge, one of the two is broken and CI says so.

```bash
uv run pytest
```

## Live signals and execution

Not here, and deliberately so. This package analyses; it never places an order,
holds a key, or gives advice. There is no code path in it that reaches money.

TradeFloor runs a separate, account-scoped MCP server for that. It authorises
through your browser, where you pick which wallet an agent may touch and what
its spending ceilings are, and every write goes through server-side risk limits.
An agent there cannot loosen a stop, and cannot open a position without one.

It is invitation-gated while it settles. If you want in:
[tradefloor.co](https://tradefloor.co/?utm_source=github&utm_medium=oss&utm_campaign=tradefloor-mcp).

## About `src/tradefloor_mcp/engine/`

That directory is generated. It is extracted verbatim from the TradeFloor
monorepo, where the same code is a money path, and a CI gate on both sides
keeps the two byte-identical. Pull requests against it cannot be merged.

Everything else here is authored in this repository and contributions are
welcome: the server, the tools, the docs, the examples.

## Support

Best-effort on response times, and honest about it: this is maintained
alongside a product, so an issue may sit for a while.

The tool contracts are a different matter. They follow semantic versioning
strictly, so you can build on them: a breaking change to the three tools only
ever ships in a major version.

## Licence

Apache 2.0.

---

Market structure analysis is not investment advice, and past structure does not
predict future price. Trading leveraged instruments can lose you more than you
put in.
