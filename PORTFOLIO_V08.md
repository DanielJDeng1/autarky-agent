# v0.8 — Unified multi-market paper economy

## Design

The existing single-market `community` databases are research sandboxes. Their simulated balances are **not** deposits into the unified portfolio. The new `portfolio.sqlite` is an independent, shared treasury for BTC/USD and ETH/USD. Register one or more current-protocol eligible research trials before its first market cycle. The exact strategy, trained artifact, source round and dataset digest are frozen; no model can submit orders or alter an active sleeve. Each sleeve has its own cash, asset units, shares, cost basis in net funding and risk history. The reserve is unallocated cash. No credentials or authenticated endpoint exist.

This is a new forward paper experiment, not a merger/migration of old paper balances. Run research workers separately; do not count the old simulated communities' returns or capital as portfolio gains. Registration is manual and one-time for a portfolio. No automatic replacement, replication, live exchange or actual income is implemented.

## Controls and accounting

- Initial funding: at most 25% of initial equity per sleeve, 40% per market, at least 20% cash reserve. A sleeve uses the existing 25%-of-cash position sizing rule, 40 bps fee and 10 bps additional slippage per side; observed bid/ask provides the spread. These are hypothetical fills; verify real fee schedules and liquidity separately.
- Mark every configured market at a common closed-candle timestamp and observed quotes within 30 seconds of each other; observation age at processing must be <=90 seconds. Candle time must be aligned to the configured interval. Quotes are locally timestamped after fetching, not exchange-stamped. Missing, invalid or stale snapshots abort the cycle without fills; do not forward-fill missing prices. A stop can only flatten once a fresh complete snapshot arrives. Polling, API delays, gaps and outages can exceed loss thresholds.
- A single transaction journals fills and updates every sleeve, treasury peak, marks and accepted cycle. Every quote checks risk even on the same candle; only a new candle can trigger a new entry or normal signal exit. Older or duplicate snapshots are ignored. No historical trades are fabricated for missed observations.
- Aggregate actual bid-marked asset exposure is limited to 30% of total equity and 20% per market. Entries use a conservative pre-trade bound; a market move that breaches exposure latches the portfolio stop. 8% observed portfolio drawdown latches a stop and flattens all positions; individual 8% observed sleeve drawdown flattens that sleeve. These are sampled paper limits, not guaranteed maximum losses. The engine prioritizes exits before new entries and checks the risk limit after costs. The operator can latch a stop; there is no automatic restart/reset.
- Weekly flat-only rebalance requires fresh synchronized marks, >=30 days of current frozen strategy history, >=20 fills and >=95% expected candle coverage. Withdraw up to 2.5% of total equity (and <=25% of donor cash) from one eligible losing or stopped sleeve, then fund up to 2.5% of total equity to one qualifying positive risk-adjusted sleeve. The winner must remain below 25% sleeve and 40% market funding concentration, with >=20% reserve. Ranking uses current NAV return minus lifetime sleeve drawdown. Cash transfers issue/redeem shares at current NAV and scale the peak, preserving the historical return and drawdown. No capital transfers while any sleeve holds an asset. A strategy with a small number of fills is not statistically validated by this rule.
- `status` reconstructs cash, units, fees, funding, fill count and reserve from the new ledger. This is accounting reconciliation of simulated executions, not exchange reconciliation or tamper-proof auditing. The app does not independently protect against direct DB edits. SQLite is on local disk; back up via its backup API or stop processes before copying the DB and WAL. No need to combine old databases.

## Example workflow

Use real-history research data (CSV or public Kraken) in the existing community first. The registration CLI refuses ineligible, synthetic and old-protocol reports. Replace `1` with actual eligible round IDs; a model may never produce one.

```sh
python -m unittest discover -s tests -v
python -m autarky_trade.community --db data/btc.sqlite init --pair XBTUSD
python -m autarky_trade.community --db data/eth.sqlite init --pair ETHUSD
python -m autarky_trade.community --db data/btc.sqlite research --source kraken --model qwen3:4b
python -m autarky_trade.community --db data/eth.sqlite research --source kraken --model qwen3:4b
python -m autarky_trade.community --db data/btc.sqlite trials
python -m autarky_trade.community --db data/eth.sqlite trials
python -m autarky_trade.portfolio init --capital 1000
python -m autarky_trade.portfolio register --source data/btc.sqlite --agent alpha --round 1 --weight-bps 2000
python -m autarky_trade.portfolio register --source data/eth.sqlite --agent beta --round 1 --weight-bps 2000
python -m autarky_trade.portfolio run --once
python -m autarky_trade.portfolio run --poll 60
python -m autarky_trade.portfolio status
python -m autarky_trade.portfolio rebalance
python -m autarky_trade.portfolio stop
```

The example registers $200 per sleeve and $600 reserve. Research and final evaluation remain separate; final evaluation must follow its own preregistered protocol. Keep the `run` process active after stop until status confirms flat exposure. All capital is simulated. Existing v0.7 evaluation evidence remains unchanged. Actual fees, latency, partial fills, exchange order minimums, market depth, multi-venue liquidity, execution recovery, and real-world P&L remain untested. The next execution milestone should build an isolated validation-only order gateway and reconciliation state machine; no real-money order submission is included here.
