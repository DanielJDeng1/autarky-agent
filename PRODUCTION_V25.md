# v0.25 — prospective shadow paper production

This module is **paper-only**, not an eligible final evaluation or a real trading system. No order API, credentials, economic transfers, or real revenue. It binds a prospective plan to an existing living economic generation and uses its current productive balance as the initial *shadow* account. The economy continues to hold that balance; never add shadow NAV to economic NAV. One plan per generation per production ledger. Use a single production ledger for the community. Retirement or operator stop prevents further entries and flattens at the next fresh observed quote. No production settlement until a later separately reviewed integration.

## Frozen plans

Choose a source node and horizon from the full saved v0.20 horizon review. The existing allocator verifies dataset fingerprints, node specifications, dates and recorded metrics. Freeze the final training rows (up to 26 observed 30-day windows; exact original horizon-specific sampling), prior out-of-fold calibration rows, threshold, source identity and code fingerprints before the prospective start. The model is fitted deterministically from those exact frozen rows on each scheduled decision, never updated with forward returns. A null calibration threshold means abstention. The historical mean uses the same training labels and separate frozen calibration. All prior data is development data. The freeze deliberately tests a fixed final fit, not the historical rolling refit policy, so results are not a direct replay of historical monthly scores. Source code changes fail closed; keep the frozen implementation for the trial or design an explicitly reviewed versioned migration. Only BTC/ETH local/peer price features currently supported; funding-state rules require another live input adapter.

## Execution

- Predeclare a future UTC hourly start and 90–360 day duration; default is the second next hour, 90 days. A 30-day schedule contains the same nonoverlapping horizon slots as v0.20. No entry beyond the last scheduled exit, no monthly balance resets.
- Four alternative shadow accounts (candidate and historical mean, each at normal and stress costs). All start with the same capital. They are not additive funds. Entry 25% of cash, 8% observed drawdown latch, long spot only, fee/slippage plus observed spread. Risk stops and exit due dates are evaluated at each valid observed bid/ask.
- Public Kraken REST closed hourly candles only; use synchronized contiguous BTC/ETH history ending at the scheduled decision. Maximum 718-hour lookback. Scheduled entry must use a quote observed within 120 seconds after the decision; otherwise record missed. Missing data may retry inside the window, never backfill. Exits use next observed quote, record lateness. No price is invented during outages. Quote timestamp is local receipt time, not exchange-provided time; requests taking >15 seconds are rejected. Actual fillability and intrahour risk are not established.
- Persistent SQLite plan digest, snapshot and before/after simulated fills. Duplicate observations ignored, no replay. A foreground process polls; close terminal/Ctrl+C to stop polling (positions remain simulated and exits can be delayed). Explicit stop latches the trial and closes at next valid quote. Use one process. No autonomous Windows service. Monitoring outages are recorded via max quote gap, missed opportunities and late exits; a poorly observed trial is not evidence of viability.
- Shadow paper does not debit or credit the economic ledger. Do not advance the economy through hundreds of artificial upkeep ticks while treating these accounts as a real-time economic experiment. Later settlement needs a single authoritative escrow/position lifecycle and no retirement while positions are unresolved.

## PowerShell

```powershell
$homeDir = "$env:LOCALAPPDATA\Autarky"
$cutoff = [DateTimeOffset]::Parse('2026-06-30T23:00:00Z').ToUnixTimeSeconds()
$start = ([long][math]::Floor([DateTimeOffset]::UtcNow.ToUnixTimeSeconds()/3600)+2)*3600
$common = @('--db', "$homeDir\production.sqlite", 'freeze', '--economy', "$homeDir\economy.sqlite", '--tree', "$homeDir\research-tree.sqlite", '--review', "$homeDir\history\horizon-review-v20.json", '--btc', "$homeDir\history\btc-hourly-full.csv", '--eth', "$homeDir\history\eth-hourly-full.csv", '--cutoff', $cutoff, '--start', $start, '--days', 90)
python -m autarky_trade.production @common --agent gamma:1 --node 8 --pair XBTUSD --horizon 168
python -m autarky_trade.production @common --agent alpha:1 --node 6 --pair ETHUSD --horizon 336
python -m autarky_trade.production --db "$homeDir\production.sqlite" status
python -m autarky_trade.production --db "$homeDir\production.sqlite" run --poll 30
```

`--once` performs one observation cycle; before start it does not trade. `stop --trial ID` records a stop (run again to flatten). Do not re-register a failed trial to cherry-pick a new starting date. No real orders. Inspect both normal and stress results and the mean comparator, quote gaps, missed opportunities and actual fill counts; 90 days alone is not a passing test. Actual costs, funding and external revenue are distinct from all paper balances.
