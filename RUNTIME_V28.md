# Autarky v0.28 — Continuous operations, collaboration and economic mechanism research

This is an additive upgrade to v0.27 on `dev/v0.4-quant-research`. All capital and orders remain paper-only. Existing frozen production plans, evaluator source, hashes, events and ledgers are preserved. Stop old Council/production/economic commands before starting the runtime. No reset or re-freeze.

## Launch (PowerShell)

```powershell
cd "C:\Users\danie\OneDrive\Documents\autarky-agent"
git pull --ff-only
$homeDir = "$env:LOCALAPPDATA\Autarky"
$reviewCutoff = [DateTimeOffset]::Parse("2026-06-30T23:00:00Z").ToUnixTimeSeconds()
python -m unittest discover -s tests -p "test_runtime_v2*.py" -v
python -m autarky_trade.runtime --home "$homeDir" --cutoff $reviewCutoff --model qwen3:4b --check
python -m autarky_trade.runtime --home "$homeDir" --cutoff $reviewCutoff --model qwen3:4b
```

The read-only `--check` prints paths, economic reconciliation, history coverage, frozen evaluator identity and the existing trial count. It does not start workers. If `frozen_identity_ok` is false, do not launch until the frozen source mismatch is investigated. For operation, keep Ollama running and open `http://127.0.0.1:8768`. Ctrl+C stops the foreground runtime; a committed research job is allowed to finish (network downloads can delay clean shutdown). Restart resumes saved state. Do not launch legacy Council research or legacy economic ticks alongside the runtime. The runtime owns the Council scheduling lock; legacy tick-based economic runs reject a day-clock economy.

## Continuous operations

- Market monitoring, agent deliberation, and paid research use separate workers. Paid work has a single-worker limit, durable prepaid task registration, exact-once completion, and independent ledger lock for transactions. A research task no longer holds the economic transaction lock during computation or downloads. Daily upkeep/settlement can advance without recovering a live task as interrupted; an in-flight task defers retirement until settlement. Operator messages take precedence over autonomous decisions, and a valid `talk` addressed to the operator is required before consuming unread operator messages. Direct peer messages have independent delivery records; they are not consumed by an unrelated operator reply. Earlier v0.27 processed cursors are migrated as a baseline, not retroactively labeled as answered.
- Agent communication, delegation, projects, review and reproducibility are retained. A new experiment can be assigned or reviewed without an LLM waiting on the research execution. One local model deliberation at a time; operator response still depends on Ollama producing a valid response. The UI shows pending deliveries, not a promise that every question was answered correctly.
- The runtime keeps operating when research budget runs out; the existing daily budget (default 3), research pause, and market/risk gates still apply. A separate autonomy pause disables spontaneous turns and paid/deployment actions while leaving operator/peer replies and paper monitoring active.
- `Flatten all paper trials` permanently requests stopping all active paper trials and pauses research/deployment; closure occurs at a fresh quote, not an invented last price. Existing research may finish. It is not an exchange kill switch or a real-money control.

## New economic mechanisms

`hedged_carry`: agent-selected BTC/ETH and 3, 6, or 12 completed months at the configured cutoff. On an approved paid research turn, fetches public Binance same-venue spot and USD-M perpetual hourly archives and verifies SHA256 and exact monthly coverage. Cache: existing `history/carry-archives/{spot,perp}/SYMBOL` (created only when used). Uses existing verified funding dataset, equal-base long spot/short perpetual, 25% initial capital per leg, collateral >= entry short notional, no assumed borrowing, both legs' fees/slippage, funding settlement-hour trade-price proxy, and an adverse intrabar margin/forced-exit proxy. Normal and stress fee/slippage scenarios use 40/20 and 80/40 bps per side on both legs. Cash benchmark is zero. Outputs month-level net, funding, basis, fees, and drawdown proxy plus source URLs/hashes. These are historical research and do not qualify for deployment. Actual exchange availability, marks, depth, taxes, borrowing, rebalancing, and liquidation rules remain unverified; positive historical returns are not evidence of executable profit. Actual archival download on the user's network is not yet tested.

Public Kraken/Coinbase spot screen: compares synchronized observed top-of-book bid/ask in both directions at illustrative normal and stress friction, rejecting stale quote pairs. The screen is information available to agents, not order execution, arbitrage proof or a strategy recommendation. Secondary quotes are sampled after primary paper monitoring. No Coinbase or Binance private API credentials, no live orders.

## Operator center

- Existing overview, chat, research/reasoning, collaboration, and positions tabs stay intact. Overview has a UTC minute equity graph (marked canonical paper equity versus settled treasury) and a toggle for complete historical treasury ledger; no fabricated historical observations. Canonical normal-cost account only counts toward wealth. Stress/mean accounts are alternative scenarios.
- Trade feed defaults to canonical fills and decisions, with filters for canonical fills only and all simulation accounts. Actual recorded quantity, fee, cash, market and reason are shown. System events can be filtered for warning/error, research, or paper activity. Agent activity cards, worker duration, daily research budget, operator queue and market freshness make a paused or stale runtime visible. Real external revenue remains separate and zero.
- The local HTTP dashboard is loopback-only with request token and host checks. Operator messages and controls persist. UI uses no external frontend libraries.

## Verification

Run `python -m unittest discover -s tests -p test_runtime_v28.py -v` and the full suite on Windows. New tests cover independent message delivery, async reply during blocked paid research, concurrent daily upkeep, old delivery baseline migration, canonical equity sampling, spread freshness/costs, hedged price cancellation, margin proxy, archive validation and provenance, local HTTP dashboard controls. Existing v0.26/v0.27 behavior is regression-tested where available. Frozen production source is unchanged; live connectivity and model performance require on-machine validation.
