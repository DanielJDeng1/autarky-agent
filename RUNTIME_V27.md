# Autarky v0.27 — Continuous runtime and operations center

This release preserves v0.24/v0.26 ledgers and v0.25 frozen production artifacts. It runs on public Kraken spot data and local Ollama. Everything trading-related is **paper only**. It has no credentials, live orders, leverage, real revenue or self-sustaining profitability claim.

## Start (Windows PowerShell)

Stop all older Autarky dashboard, Council listener/research and production monitor terminals first. Keep the Ollama application running. No database resets or deletion of lock files. Run from the repository:

```powershell
cd "C:\Users\danie\OneDrive\Documents\autarky-agent"
git pull --ff-only
python -m unittest discover -s tests -p test_runtime_v27.py -v
$homeDir = "$env:LOCALAPPDATA\Autarky"
$cutoff = [DateTimeOffset]::Parse("2026-06-30T23:00:00Z").ToUnixTimeSeconds()
ollama list
python -m autarky_trade.runtime --home "$homeDir" --cutoff $cutoff --model qwen3:4b
```

Open http://127.0.0.1:8768. Use Ctrl+C to stop. Restart the same command; state is persistent. The existing `council.sqlite` is extended in place (created if absent), `economy.sqlite` must already exist, and `production.sqlite` is migrated transactionally if present. The existing frozen artifact hashes, rows and event IDs do not change. No new data directory is needed. Do not run legacy Council, economic research or production processes concurrently with the unified runtime.

Optional parameters: `--research-interval 300` (minimum 30 seconds between spontaneous turns per agent), `--poll 30` (minimum 15 seconds for public market polling), `--daily-budget 3` (0–20 paid research attempts per UTC day, default 3), `--port 8768`. The budget is a ceiling, not a quota; dialogue continues after it is exhausted. An operator message receives priority at the next available turn; a slow or stuck local-model request can still delay its response. The market worker continues separately. The runtime is foreground-only: Windows sleep/shutdown stops it, and restart cannot reconstruct missed fills.

## Operations center

- Overview: simulated settled money pool and community reserve history from the actual append-only ledger, marked paper equity, operating expenses, real revenue (zero), individual generation balances, process heartbeat, market bid/ask, current informational forecasts, recorded paper trades and missed opportunities.
- Conversation: operator and peer messages, addressed to a living slot or the whole council. Direct peer messages wake recipients subject to rate limiting. Community broadcasts are read on spontaneous turns; they do not wake everyone to avoid feedback loops.
- Research & reasoning: validated proposals, measured results, raw *local model-emitted* reasoning when available, errors and rejected plans. The text is not guaranteed to be a faithful internal thought process.
- Collaboration: shared projects, delegated assignments, accept/decline, contributions, peer critique, and paid exact reproducibility checks on the same historical dataset. Peer profiles report measured recent activity and simulated research cost / settled P&L, not invented ability scores. Reproducibility is not independent forward evidence.
- Paper positions: one active trial per generation, all historical trials retained. Canonical `candidate_normal` is the only account that settles economically; stressed and mean accounts remain alternative comparisons. Agents may stop their own active paper trial and request deployment of a previously qualified own candidate. A stop permanently ends that trial and flattens on a fresh quote; an operator can also stop a trial. No resetting risk by redeploying the same historical candidate for the same generation.
- Pause research: prevents new paid tasks and new trial deployment; current tasks finish, dialogue and market monitoring continue. Resume re-enables research subject to available funds and the daily cap.

## Economics and compatibility

The original $1 simulated research charge still prepays each paid experiment. The runtime switches the existing economy to $0.25 (or existing configured rate) per agent per elapsed UTC day, beginning with the first run. Missed days are caught up on restart in batches of 30. Previous tick-based charges are unchanged. Legacy economic-kernel `run` is disabled for a migrated elapsed-time ledger. Research cooldown remains measured in economic ticks (paid attempts or daily upkeep), not wall seconds.

An active trial reserves its generation's productive funds. The normal candidate's cash is settled to cents after it is flat, exactly once; unrealized equity is marked separately. Settlement gains/losses are external **simulated** cash flows, journaled separately from costs. No treasury withdrawal during a frozen trial. When terminal and flat, half of positive trial net gain is transferred from productive to operating cash; otherwise the remaining cash stays productive. The reserve cannot fund a replacement by taking an incumbent's reserved capital; insolvency first requests flattening and then retirement. A missed quote never becomes a fictional historical fill. All benchmark balances are alternative simulations and must not be added to the money pool.

A new directional paper candidate requires positive stressed historical return, at least 10 fills and 5 active windows, and stressed return above both the mean and risk controls. The report is recorded, the agent must request deployment, and the model is frozen with a future start, exact historical dataset identity, calibration and unchanged risk rules. Null candidate threshold rejects new deployment. Existing trials are not silently changed. The runtime uses current closed hourly candles for informational forecasts; actual entries still follow the frozen schedule. The current research tools remain constrained to supported strategy specifications and diagnostics; carry screen is not a complete hedged strategy, and no real trading authority is granted. Real costs are shown only if recorded in the existing resource ledger; estimated or simulated costs are not real bills.

## Tests

`python -m unittest discover -s tests -p test_runtime_v27.py -v` covers daily clock, settlement, reservations, collaboration and verification, persistence, HTTP control surface, migration, market adapter and an integration test using the unchanged production evaluator. Full repository regression: `python -m unittest discover -s tests -v`.
