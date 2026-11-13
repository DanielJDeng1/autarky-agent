# v0.7 — final evaluation and research-slot allocation

## Independent evaluation

The research community remains paper-only. Its development holdouts are repeatedly used for research selection, even when each interval is unique. The final evaluator introduces a separate operator-owned ledger (`data/final_evaluation.sqlite`), a frozen decision, and a predeclared future evaluation period. It does not send any final observations to Ollama, refit models, select a winner, or change the research community. It is an independent *workflow*, not an independently administered security boundary. Run it under a separate OS identity with a private ledger and data directory for stronger isolation. Agents and operators with filesystem write access can tamper with SQLite. Preserve backups and the plan digest externally; never reset the ledger to rerun a losing plan.

Motivation: White, *A Reality Check for Data Snooping* (https://doi.org/10.1111/1468-0262.00152), Sullivan/Timmermann/White, *Data-Snooping, Technical Trading Rule Performance, and the Bootstrap* (https://doi.org/10.1111/0022-1082.00163), and Hansen, *A Test for Superior Predictive Ability* (https://doi.org/10.1198/073500105000000063) emphasize that selection across a strategy universe changes the interpretation of its best result. Markowitz, *Portfolio Selection* (https://doi.org/10.1111/j.1540-6261.1952.tb01525.x), motivates evaluating combined risk instead of treating each asset as independent. The evaluator does not implement White's Reality Check or Hansen's SPA test: the current development history does not contain aligned net returns for every attempted strategy, including failed or interrupted experiments. Counts of completed candidate evaluations are descriptive, not effective independent trial counts.

### Operator workflow

1. Use two independently initialized research databases for BTC and ETH (or one market). Each has its own paper capital, agents and archive. For example `python -m autarky_trade.community --db data/eth.sqlite init --pair ETHUSD`. Run research with each DB; consult `community --db ... trials` for completed eligible current-protocol (`v0.6-predictive-1`) trials. Do not use synthetic results or manually edit a trial into eligibility.
2. Create a JSON plan outside Git. Example below: replace the round IDs with actual eligible trials and the timestamps with a *future* UTC start and last candle-open time. The values below are illustrative, not a currently scheduled experiment. All sleeves must use the same interval; at hourly frequency the dates below cover 90 days. The plan holds at least 20% unallocated cash at inception; a sleeve can have at most 50% initial weight. One sleeve per market. Each sleeve's existing strategy uses 25% of its cash on entry; sleeve allocation is not the same as asset notional exposure.

```json
{
  "name": "BTC and ETH first prospective portfolio",
  "mode": "prospective",
  "start": 1801440000,
  "end": 1809212400,
  "legs": [
    {"db": "data/community.sqlite", "agent": "alpha", "round_id": 1, "weight_bps": 4000},
    {"db": "data/eth.sqlite", "agent": "beta", "round_id": 2, "weight_bps": 4000}
  ]
}
```

```sh
python -m autarky_trade.final_eval freeze data/plan.json
python -m autarky_trade.final_eval status
```

The plan includes exact strategy artifacts, source development trial identities and dataset fingerprints, the prior completed candidate count per source DB, weights, costs, analysis thresholds and evaluator version. The SHA-256 identifier binds the canonical plan. The source DB is opened read-only; final start must follow all recorded source archive, holdout and paper observations, and model training. The ledger reserves the entire interval for each market, prohibiting overlapping registrations even across candle frequencies. A prospective plan must start after the registration clock. It cannot be altered or cancelled to recycle its window. Keep a record of every registered plan, including failed and unused plans. The evaluator cannot prove other copies of the dataset or ledgers have not been used, nor authenticate a CSV as actual exchange data; retain provenance from an independently managed data source.

3. After the last candle closes, collect each market's exact contiguous final span plus 101 prior candles for feature warmup. The CSV format is `timestamp,open,high,low,close,volume` (UTC Unix seconds, header optional); the first timestamp must equal start minus 101 intervals, and the last must equal end. Use data from the declared exchange/market/interval and preserve a source checksum. The interval must cover at least 90 days and 600 final candles (max 20,000). For daily data that means at least 600 days. At this stage no final dataset is necessary for continued development or paper trading.

```sh
python -m autarky_trade.final_eval evaluate PLAN_ID --csv XBTUSD=data/final-btc.csv --csv ETHUSD=data/final-eth.csv
python -m autarky_trade.final_eval status
```

Replace `PLAN_ID` with the returned digest. The plan is consumed transactionally before any file opens. A missing, mismatched, malformed or interrupted evaluation is terminal once data access begins; correct file paths and timestamps before running. A wrong set of market arguments or an unfinished time window is rejected before consumption. Results remain in the ledger. Separate ledger path: `--db path/to/final.sqlite` before the subcommand.

For a **sealed historical** diagnostic, change mode to `sealed` and add `csv_sha256` to each sleeve (hash of the exact CSV bytes, provided by a custodian before the evaluator opens the dataset). This allows a previously withheld past period strictly after the source DB's known history. It cannot demonstrate that no one has already looked at the period, and never returns `eligible_for_manual_review=true`. It is not a shortcut to live trading. For prospective mode the final data does not exist at registration, so its digest is recorded when evaluated rather than falsely precommitted.

### Evaluation contract

The frozen portfolio consists of independent, fixed initial funding sleeves with unallocated cash, no transfers or rebalancing. All are evaluated on the exact same time grid. The combined NAV is cash reserve plus the sum of the initial weights times each normalized sleeve equity path, not a sum of returns or a daily-rebalanced weighted return. This includes each sleeve's modeled fees and final liquidation. The evaluator reports the combined close-marked drawdown, cost-stressed returns, maximum sleeve equity concentration, and correlation of paired per-bar sleeve log returns (undefined for zero variance). The same simulation produces passive sleeve references. The passive comparator is fractional buy-and-hold with the same local risk stop, not an unrestricted full-market buy-and-hold.

The predefined checks require positive normal and stress returns, outperformance of the passive portfolio, combined drawdown below 8% under normal and stressed assumptions, max sleeve equity weight no more than 60%, positive normal and stress sleeve returns, at least 20 normal simulated fills per sleeve and no sleeve stop. The portfolio excess log returns receive paired 95% circular moving-block bootstrap intervals for the mean at one-, three- and seven-day block scales (999 seeded resamples). A positive lower bound at each scale is a conservative *diagnostic*, not a valid general multiple-testing correction or a guarantee that the true edge is positive. Returns can be nonstationary and correlations change. Review eligibility requires all checks and prospective mode. It does not authorize any order or override forward paper review, exchange reconciliation, actual expense or risk checks. The replay does not implement a joint real-time stop, intrabar depth, cross-sleeve capital transfers, partial fills or live execution. No real exchange credentials or funds are required.

## Research slot allocation and batch audit

The coordinator now accepts `--slots 1|2|3` (default 3, preserving the previous behavior):

```sh
python -m autarky_trade.jobs coordinate --slots 1 --loop --poll 300
```

All three worker processes can stay running; only the selected agents get jobs. A slot is a scheduled research opportunity, not a dollar, GPU-second or hard wall-time budget. Each scheduled batch consumes one fresh development holdout, shared among its selected workers. Untried and overdue agents are prioritized, with a three-batch starvation guard; otherwise qualifying positive current-strategy forward NAV less observed drawdown is used, then time since last scheduling. Only fresh market observations, at least 30 days on the current strategy, 20 fills, 95% coverage, no pending switch and no risk stop qualify a forward score. Agents without evidence rotate rather than being classified as failures. The choice and rankings are recorded in the batch event. This deliberately avoids rewarding raw backtest results or historical lifetime gains. Actual resource usage remains measured separately; slot budgets are not GPU enforcement or autonomous monetary allocations.

A single failed job no longer discards completed evidence. The coordinator waits until every selected job is terminal, then persists completed reports, records failed agents and stages only eligible completed trials. A totally failed batch still consumes its holdout. If a missing/stuck worker prevents completion, `python -m autarky_trade.jobs abandon` marks incomplete jobs failed, invalidates their leases and finalizes completed reports. It does not terminate the operating-system process; stop that process separately if necessary. Continue to use the same ledger/database and fresh windows. Budgeted scheduling is available in queued mode only; the synchronous demo/research path retains all three agents.

## Upgrade and verification

Stop existing workers and back up the community DBs. The research protocol remains v0.6; no strategy/artifact format or existing community schema changes are required. The new final ledger is additive and separate. Run `python -m unittest discover -s tests -v`. Tests use synthetic fixtures explicitly to test accounting/validation, not to claim market returns. Real Kraken/Ollama integration, a sustained prospective evaluation, genuine profitability, multi-market real-time portfolio execution, shared cross-market treasury, isolated exchange gateway and live trading remain unverified or unimplemented. Next: a shared paper portfolio coordinator with exposure enforcement and aligned quote observations, then isolated exchange validate-only execution and independently reconciled orders. Keep separate final evaluation data outside agent-accessible research directories.
