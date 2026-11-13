# Autarky — autonomous trading research community

**v0.4.0 · paper trading only · Python 3.11+ · Ollama optional · SQLite**

Conway-inspired economic selection for a population of trading research agents. Alpha researches trends, Beta mean reversion, Gamma breakouts. Each can run in its own process, maintains persistent research and paper-account state, and receives resources according to observed performance. The local model proposes bounded strategy parameters; deterministic code performs all calculations and trading decisions. No paid AI API is required. This is experimental quantitative research software, not a profitable trading system or a financial recommendation.

## v0.4 — quantitative research expansion

This release remains **paper-only**. Existing v0.3 SQLite databases remain compatible. Old strategy JSON lacking `filters` is interpreted as an empty list; unchanged strategies retain their performance records.

- The local model proposes up to three AND-combined close-price filters on each agent's base rule: momentum (fractional return), distance from SMA, return volatility, and simple-window RSI. All fields and thresholds are validated. No generated Python or shell code runs. Deterministic defaults now include two filtered variants per agent.
- The selected candidate is compared against cash, an allocation-matched buy-and-hold reference, and 31 seeded random-timing paths calibrated on training signal occupancy and turnover. Approval additionally requires beating the random 75th percentile. These are descriptive comparisons, not significance tests; legacy trial reports retain their original gates.
- Synchronous research consumes its holdout before evaluation, like the job queue. A failed inference or crash can burn an interval without a result; this prevents inadvertent holdout reuse.
- The SQLite market archive retains closed candles across sessions. Research uses the latest contiguous suffix, capped at 20,000 candles. Exact duplicates are ignored, conflicting overlapping bars abort ingestion, and gaps are never interpolated. A CSV is operator-supplied: verify its pair, provenance, and time interval.

```bash
python -m autarky_trade.community init
python -m autarky_trade.archive import path/to/XBTUSD_60.csv
python -m autarky_trade.archive sync
python -m autarky_trade.archive status
python -m autarky_trade.jobs coordinate --loop --poll 300
```

Importing a CSV is optional but recommended for long-history research; synchronization pulls the recent Kraken window and appends it. If overlapping historical and exchange candles disagree, investigate the source instead of overwriting used history. The archive belongs to the database's configured market and interval.

Start the three research workers and paper trader as documented below. Open a separate terminal for the new read-only monitor:

```bash
python -m autarky_trade.dashboard
```

Visit http://127.0.0.1:8765 to inspect paper equity, agent portfolios, archive coverage, latest trial benchmarks, readiness, and recent events. The dashboard serves a JSON view at `/api/status`, refreshes every 30 seconds, and binds only to localhost. Do not expose it publicly. Supply `--db` before the subcommand in archive/community/jobs, or before `--port` for dashboard, to select a different database.

The research language is deliberately bounded. Arbitrary strategy-code generation, ML model training, multiple markets per portfolio, real exchange execution, and proven profitability are not implemented. The next milestone is richer independent experiments and an isolated, auditable execution service, with real trading gated on forward evidence and operator approval.

## 1. Quick start (Windows PowerShell, macOS, Linux)

Extract this repository and open a terminal in its root. Python 3.11 or later is sufficient; there are no third-party Python dependencies. On Windows, substitute `py -3` for `python` if necessary.

```sh
python -m unittest discover -s tests -v
python -m autarky_trade.community init
python -m autarky_trade.community research --source synthetic
python -m autarky_trade.community status
```

The offline demo evaluates synthetic candles without writing trials, approving strategies or touching the treasury. The default database is `data/community.sqlite`. Initial simulated capital is $1,000: $250 reserve plus $250 for each of three agents. It is not money you have deposited. Use a new database for another market or starting allocation; existing databases are not reset by `init`.

### Single-process mode (easiest)

```sh
# Optional: start Ollama locally, then pull a model (if not already installed).
ollama pull qwen3:4b
python -m autarky_trade.community research --source kraken --model qwen3:4b
python -m autarky_trade.community run --model qwen3:4b
```

Omit `--model qwen3:4b` for deterministic baseline experiments. The daemon polls Kraken public market data and evaluates strategies on new completed candles. The default research cadence is 24 hours; fresh unseen data is required regardless of cadence. `--once` runs one cycle. `--paper-only` disables research in this process.

For the 8 GB RTX 5060, start with Qwen3 4B or an already-installed smaller model; a 4-bit 8B model may fit depending on context size and other GPU workloads. Ollama shares a model server. The runtime requests bounded JSON outputs, not shell code. No model receives exchange credentials. Internet is needed for Kraken market data; model inference remains local. Ollama must be running on `127.0.0.1:11434`. Model weights and electricity still have costs (and model licenses differ).

### Multi-process mode (the agent community)

Run these in **separate terminals**, all from the same directory and with the same database. Do not run the single-process research mode at the same time.

```sh
# Terminal 1: founder/coordinator, schedules and finalizes research batches
python -m autarky_trade.jobs coordinate --loop --poll 300
# Terminal 2: Alpha, trend research
python -m autarky_trade.jobs worker --agent alpha --model qwen3:4b --loop
# Terminal 3: Beta, mean-reversion research
python -m autarky_trade.jobs worker --agent beta --model qwen3:4b --loop
# Terminal 4: Gamma, breakout research
python -m autarky_trade.jobs worker --agent gamma --model qwen3:4b --loop
# Terminal 5: portfolio paper execution, independent of inference latency
python -m autarky_trade.community run --paper-only
```

On a small GPU, start each worker once without `--loop`, one at a time, or run one worker process at a time in rotation; simultaneous Ollama requests may queue and consume memory. A worker claims only its own job. Its lease expires after ten minutes; crashed jobs can be reclaimed, with up to three attempts. A stale worker cannot publish after losing a lease. The coordinator commits a batch only when all three reports complete; a failed batch consumes the holdout, preventing optimistic retry against the same test period. The latest results may take days to refresh because fresh data is required. The portfolio can keep running even while the research agents are busy or stopped. Use Ctrl+C to stop processes; SQLite state persists.

One-off queued research from a local CSV:

```sh
python -m autarky_trade.jobs schedule --csv history/btc-hourly.csv
python -m autarky_trade.jobs worker --agent alpha
python -m autarky_trade.jobs worker --agent beta
python -m autarky_trade.jobs worker --agent gamma
python -m autarky_trade.jobs coordinate
```

`coordinate` also attempts to schedule Kraken history. To finalize only with no network use `python -c "from autarky_trade.jobs import finalize; print(finalize('data/community.sqlite'))"`.

## 2. Market data

- Default: Kraken `XBTUSD` at 60-minute intervals; optionally `ETHUSD` and 15, 60, 240 or 1440 minutes. Each database has one pair/interval. Example: `python -m autarky_trade.community --db data/eth.sqlite init --pair ETHUSD --interval 60`.
- Kraken OHLC returns at most 720 recent candles including the unclosed candle; the runtime discards the final unfinished candle. A first community research batch requires at least 600 complete candles; its test slice requires at least 120. At hourly resolution the recent history is roughly 30 days, far too short for confidence in market regimes. Use longer history for serious research.
- CSV format: `timestamp,open,high,low,close,volume`, Unix UTC seconds at the configured interval. Strict contiguous timestamps, finite positive prices, nonnegative volume. Kraken's downloadable OHLCVT CSVs contain additional trade counts at the end, which are ignored. Extract the correct pair/interval from the official archive and retain the original provenance and checksum. CSV identity is operator-provided, not independently authenticated: never label synthetic or another market's data as real.
- Obtain Kraken history: https://support.kraken.com/articles/360047543791-downloadable-historical-ohlcvt-open-high-low-close-volume-trades-data. OHLC API: https://docs.kraken.com/api/docs/rest-api/get-ohlc-data/.

```sh
python -m autarky_trade.community research --source csv --csv history/btc-hourly.csv --model qwen3:4b
```

## 3. What the research system does

1. The coordinator freezes a dataset and reserves a never-before-used test interval. The first split is approximately 60% training / 20% validation / 20% test; later rounds may use previously tested data for training but not as unseen test data. At least 120 genuinely new test candles are required. SQLite records the latest reserved test timestamp across all agents for this market.
2. Each agent selects a family and proposes up to five model-generated parameter sets alongside four baselines. Training summaries and prior training/validation results are sent to the local model; the unseen test data is not included in the prompt. Strategy parameters must satisfy `2 <= fast < slow <= 100`. Model output is validated and no generated code runs.
3. Backtests use signals from prior closed candles, fill at next open with assumed fees and slippage, and mark to market. The validation return ranks candidates; only each agent's winner receives a test and doubled-cost stress test. Require positive training, validation, test and stress returns, >=4 fills each, <8% drawdown, and no stop.
4. Successful candidates become pending and may start paper trading only on a candle strictly after the holdout. Existing positions are closed before a changed strategy is activated. An identical re-selected strategy retains its forward record. Rejections do not automatically retire an already active strategy; its independent risk controls still apply.

The gate is intentionally exploratory. Three simultaneous candidates, repeated experiments, finite data and regime changes mean a positive backtest is **not** proof of an edge. No p-value, multiple-testing adjustment, benchmark outperformance requirement, exchange order book replay, liquidity model, or cross-validation across multiple regimes is implemented. A future promotion process must add those checks. Store separate untouched final evaluation data outside the lab before changing algorithms.

## 4. Paper economy and risk

Each agent owns an independent simulated spot account; no shorts or leverage. A single position uses at most 25% of its available cash at entry. Orders are filled using a fresh public bid/ask observed by the process, plus 40 bps assumed fees and 10 bps extra slippage per side. These are configurable only in the older v0.2 path; community costs are pinned to prevent silently changing assumptions. Actual exchange fees, spreads and market impact differ. A candle is consumed once per market, and a transaction writes all accounts and the tick atomically. Missed candles are not replayed at fictional historical prices. Bid/ask quotes have no exchange-provided timestamp here; freshness is inferred from fetching them and the closed-candle clock. This is a simulation, not a realistic fill guarantee.

A per-agent 8% maximum observed drawdown stops that agent; a portfolio-wide 8% observed drawdown latches the global stop and flattens positions at the observed simulated bid. Stops are checked on new closed candles, not continuously intrabar. A fast crash could exceed the threshold. The reserve cannot be spent on orders. The founder can allocate up to 5% of total equity from the reserve once per week, only when all agents are flat and the winning agent has at least 30 days since first activation, 20 fills, positive cumulative flow-adjusted return, and no risk stop. It retains at least 15% reserve and caps the receiving agent at 40% of equity at allocation time. Allocation uses share issuance at current NAV so transfers do not manufacture profits. Capital grows/shrinks with simulated P&L; no bank or exchange balance is connected. It does not yet withdraw capital from underperformers or spawn new agents automatically.

```sh
python -m autarky_trade.community status
python -m autarky_trade.community trials
python -m autarky_trade.community history
python -m autarky_trade.jobs status
python -m autarky_trade.community rebalance
python -m autarky_trade.community readiness
python -m autarky_trade.community stop
```

`stop` latches the paper treasury; the next successful paper poll flattens existing positions, even on the same candle. Keep the paper process running until status confirms it has flattened. There is no automatic reset; retain the stopped DB for analysis and use a new experiment for changes. `readiness` flags whether a single unchanged strategy has 60 days of paper history, >=40 fills, >=95% candle coverage, positive flow-adjusted return, <5% lifetime drawdown, no pending switch and no stop. It is a **manual-review gate only**, not live authorization. A strategy may never meet it.

## 5. Persistence and operations

Database tables: `treasury`, `agents`, `rounds`, `trials`, `events`, `ticks`, `batches`, `jobs`. SQLite WAL and transactions protect process coordination; use a local disk, not a network drive. Back up the database with SQLite's backup API while processes run, or stop every process before copying the database and WAL sidecars. Never edit the accounting rows to improve a result. The process console logs errors and retries network failures. For unattended operation use an OS service manager with restart and log rotation. Never expose the Ollama port to the public internet. Keep `data/`, `.env`, model files and credentials out of Git. Disk use from full research snapshots grows over time; archive old experiments as needed.

The original single-agent interface is retained (`python -m autarky_trade research ...`, `paper`, `status`); see `LEGACY_V02.md`. Its database format is separate. Do not reuse a legacy DB as a community DB.

## 6. Real trading roadmap — not implemented in this release

No authenticated exchange endpoint, API key, wallet, real order submission, or live balance reconciliation exists in v0.3. No money can move. This is intentional: before real money is introduced the system needs evidence and a hardened execution boundary, not merely a `--live` switch.

1. Collect out-of-sample evidence across multiple years and regimes; compare against cash and passive BTC/ETH exposure of comparable risk. Account for costs and inference/electricity, benchmark-adjusted returns, multiple comparisons, liquidity and missing data. Independently reproduce results.
2. Freeze one strategy/version, run forward paper for at least the readiness window, compare quote assumptions with actual available liquidity, and audit portfolio accounting. No model-driven parameter changes while measuring that strategy.
3. Implement a separately permissioned execution adapter with exchange sandbox or validate-only orders where supported. Verify exchange eligibility and terms for your jurisdiction, instrument minimums, decimals, fee schedule, API rate limits, request signing, and recovery. Use a dedicated account/subaccount and API key with trading only, no withdrawal permission. Secrets must be outside the LLM and repository.
4. Add durable order intent IDs, reconciliation of open orders/fills/balances, partial fills, cancel/reject handling, duplicate retry protection, stale-data circuit breakers, independent risk service, daily loss limits, global exposure limits, hard notional ceiling, kill switch and alerting. Test crash/restart before actual orders. No leverage or borrowing initially.
5. Only after independent verification and explicit operator authorization, use a small amount of disposable capital, with manual activation and monitored operation. Pause on unexplained discrepancies. Actual losses may exceed intended limits; no guaranteed return. Do not rely on AI-generated investment advice.

## 7. Publish to GitHub

Target: `DanielJDeng1/autarky-agent`. If the remote does not exist, use GitHub's new repo page (private initially) or GitHub CLI. Create an empty repository without initial files if using the command line. No credentials belong in the ZIP or the repo.

```sh
# Run inside extracted repository root; Git and GitHub CLI must be installed.
gh auth login
git init -b main
git add .
git commit -m "Autarky v0.3 persistent trading research community"
gh repo create DanielJDeng1/autarky-agent --private --source=. --remote=origin --push
```

If you already created the empty remote in GitHub's web UI, replace the last command with:

```sh
git remote add origin https://github.com/DanielJDeng1/autarky-agent.git
git push -u origin main
```

If a remote already contains commits, do not force-push or overwrite it. Clone it, add these project files on a development branch, and review a pull request. GitHub Actions runs the Python unit tests on push/PR; no secrets or network calls needed.

## 8. Scope and verification

The local code and unit tests were exercised with offline fixtures and mocked data, including job leases, idempotency, budget conservation, data segregation, cost modeling, strategy activation and portfolio stops. Live Kraken and Ollama availability on your machine, sustained performance, and exchange execution are not verified. The agents are independent research workers with distinct portfolios, not self-modifying general-purpose agents; the founder implements deterministic allocation rules. The ultimate Conway-style goal is a population that learns and trades independently and pays its operating costs from real measured profits. This release does not demonstrate profitability or economic self-sufficiency.
