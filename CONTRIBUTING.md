# Contributing

Contributions are welcome, with one boundary worth explaining up front.

## The one place you cannot change

`src/tradefloor_mcp/engine/` is generated. It is extracted verbatim from the
TradeFloor monorepo, where the same code sits on a trading path, and the sync
runs one way only. CI refuses pull requests that touch it, on both sides.

That is not gatekeeping for its own sake: a change accepted here would either be
overwritten by the next extract, or, worse, not be, and then two copies of code
that decides real trades would disagree.

**Found something wrong with the analysis itself?** Open an issue with the
candles that reproduce it. That is genuinely useful and it is how the fix gets
made: it lands upstream and arrives here on the next extract.

## Everything else is fair game

The server, the tools, the candle intake, docs, examples, tests. Concretely
useful things:

- more candle sources (this only knows Hyperliquid, and people have their
  candles elsewhere);
- better tool descriptions, since a model picks a tool by reading them;
- output shapes that read more clearly to a model;
- examples for MCP clients other than Claude Code.

## Running it

```bash
uv sync
uv run pytest
uv run ruff check .
```

Test on the oldest supported Python before opening a PR, and pass `--no-cache`:

```bash
uv run --python 3.11 --isolated --no-cache --with pytest --with pytest-asyncio --with . pytest tests/ -q
```

Without `--no-cache` uv may hand you a stale build and the run goes green
without testing your change. This is not hypothetical, it happened here.

## Two things that will be asked in review

**Tool output stays free of marketing.** That channel is read by a model,
sometimes one that is also holding tools which can open positions. There is a
test pinning this and it should stay passing.

**The summary names what was analysed.** Someone who asked about BTC and is
shown the bundled ETH sample has been told something false about a market. Also
pinned.
