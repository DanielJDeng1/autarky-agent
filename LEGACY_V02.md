# Autarky Trading Lab v0.2

A local-first, Conway-inspired economic research laboratory. The system proposes strategies, tests them against chronological market data, rejects weak candidates, and tracks an eligible strategy in a **simulated** spot account using public market quotes. No exchange credentials, real orders, wallet custody, or paid model API are required. This is a replacement research direction for the original Autarky storefront, not a claim of profitability.

## Start in five minutes

Requires Python 3.11+; no third-party Python packages. On Windows use `py -3.11` instead of `python` if necessary. Run commands from this directory.

```bash
python -m unittest discover -s tests -v
python -m autarky_trade research
```

The default run uses deterministic synthetic candles and writes `data/research.json`. Synthetic results are always ineligible for paper trading. Do not interpret these as market performance.

### Local AI

Install Ollama from https://ollama.com/download. With an existing local Qwen model, e.g.:

```bash
ollama pull qwen3:4b
ollama serve
```

On Windows Ollama may already be running as a background application; don't start a second server if port 11434 is occupied. In a separate terminal:

```bash
python -m autarky_trade research --source kraken --pair XBTUSD --model qwen3:4b --out data/btc.json
```

The model sees training-only summary statistics and proposes up to five parameterized trend, breakout, or reversion rules. Ollama's JSON-schema output and Python validation constrain candidates; the model cannot issue orders, run shell commands, or change risk rules. Baseline strategies are also evaluated. The deterministic research/validation engine works without Ollama. An unavailable model fails the run rather than claiming AI research succeeded; omit `--model` to run baseline-only.

### Market data and honest testing

`--source kraken` fetches public, unauthenticated Kraken OHLC data; `--pair XBTUSD` or `ETHUSD`, `--interval 15|60|240|1440` minutes. Kraken's endpoint is limited to roughly 720 candles; it is suitable for a smoke test, not strong investment evidence. The incomplete final candle is discarded. For a serious experiment use longer historical CSV data (Kraken publishes downloadable OHLCVT history):

```bash
python -m autarky_trade research --source csv --csv path/to/XBTUSD_60.csv --pair XBTUSD --interval 60 --out data/btc-long.json --model qwen3:4b
```

CSV columns: `timestamp,open,high,low,close,volume` (additional trades column ignored); Unix seconds, UTC, chronological, exact interval spacing, fully closed candles. A header is optional. Data must correspond to the pair/interval you declare. Missing candles are rejected rather than silently filled. At least 220 rows are required; use years, not days, for research. Fee and slippage defaults (40 and 10 bps each side) are illustrative, not verified current fees. Set `--fee-bps` and `--slip-bps` to conservative estimates appropriate to your account and venue.

Chronological split 60% train / 20% validation / 20% holdout. First 100 bars are warmup. Signals see prior completed bars and execute at next open with costs. Select by validation return, then evaluate only the selected rule on holdout, including double costs. Positive returns, at least four fills and less than 8% maximum drawdown in all splits and stress test are required for paper eligibility. A cash baseline and holdout buy-and-hold comparison are implicit/recorded. The gate is deliberately modest and **not** sufficient for live capital. Repeatedly testing variants on the same holdout contaminates it. Freeze strategy and collect new data; never use the gate as proof of profitability. Backtest assumes fills at modeled prices and closes the last position at final close (evaluation only).

### Forward paper trading

If the report says `paper_eligible: true` from real data, run:

```bash
python -m autarky_trade paper --report data/btc-long.json --db data/btc.sqlite --loop
python -m autarky_trade status --db data/btc.sqlite
```

Use a second terminal for status. Ctrl+C stops polling. Paper accounts start with simulated $1,000; at most 25% of cash goes into one long-only position. Every new closed candle produces at most one decision. The current bid/ask is fetched when the cycle runs, with modeled fees and slippage. Decisions are not replayed retrospectively after downtime. Each account and event is persisted transactionally in SQLite. A changed strategy requires a new database; preserve old records. A report must precede the new market candle. Max drawdown is measured at observed bid/close, and an 8% threshold stops buying and triggers an exit on the next decision; it is not a guaranteed stop price, and outages can increase losses. This is not an exchange sandbox or real execution; no funds are at risk.

If the report fails: **do not edit the flag**. Investigate costs, data, strategy quality, and genuinely new hypotheses; do not repeatedly tune on the same holdout. It is normal for all candidates to fail.

## Architecture and next steps

- Researcher/strategist: optional Ollama call creates parameterized proposals from training data.
- Quant: deterministic simulator compares candidates chronologically.
- Risk manager: hard-coded validation, cost stress test, position and drawdown rules.
- Trader: forward paper execution; no live exchange adapter.
- Founder: operator-driven selection and simulated capital allocation, with persistent logs.

These are modular roles, **not six independent autonomous processes**. Future work: persistent independent research agents with queues, experiment provenance and budgets; broader price/volume features; walk-forward evaluation and multiple-testing correction; multi-asset portfolio and exposure limits; market data storage and dashboards; a separate execution service with exchange sandbox, reconciliation, idempotency, key permissions, kill switch and an operator-approved capital limit. Only consider live spot after prolonged profitable forward paper results, realistic costs, and a manual review. No leverage, borrowing, withdrawals, or autonomous transfers. No promise of earnings. Free model inference still consumes electricity and hardware time. Measure actual electricity, data, hosting, and exchange costs before calling this self-sustaining.

## GitHub

This package is a separate working tree. Keep the original storefront release if useful. Create an empty private repository (e.g. `autarky-trading`), then publish using GitHub CLI:

```bash
git init -b main
git add .
git commit -m "Initial trading research lab"
gh auth login
gh repo create autarky-trading --private --source=. --remote=origin --push
```

The remote has not been created by this package. No credentials are included.

## References

- Conway Automaton: https://github.com/Conway-Research/automaton
- Kraken historical data: https://support.kraken.com/articles/360047124832-downloadable-historical-ohlcvt-open-high-low-close-volume-trades-data
- Kraken public OHLC: https://docs.kraken.com/api/docs/rest-api/get-ohlc-data/
- Ollama structured outputs: https://ollama.com/blog/structured-outputs
