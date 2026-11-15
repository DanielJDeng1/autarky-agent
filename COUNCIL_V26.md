# v0.26 — Autonomous research council

The objective is actual net trading revenue, not a better backtest. This release widens research autonomy and makes the community inspectable and conversational. It does not authorize live trading or settle paper gains into the economic ledger. No existing trading, production, research-tree or economic data are reset. The new database is `council.sqlite` in the **same** `%LOCALAPPDATA%\Autarky` directory. The existing v0.25 production monitors are unchanged.

## Quick start (PowerShell)

```powershell
cd "C:\Users\danie\OneDrive\Documents\autarky-agent"
git pull --ff-only
python -m unittest discover -s tests -p test_council_v26.py -v
$homeDir = "$env:LOCALAPPDATA\Autarky"
python -m autarky_trade.council --db "$homeDir\council.sqlite" init
python -m autarky_trade.council --db "$homeDir\council.sqlite" dashboard
```

Open http://127.0.0.1:8768. The UI displays addressed messages, broadcast results, rejected plans, structured action/rationale and model reasoning text (if Ollama returns `message.thinking` or a visible `<think>` block), with a message form. Refresh after agents respond. The dashboard is local only; do not expose to a network. It is not an authentication or OS-isolation boundary; all local users with DB access can read the transcript. The server checks loopback Host and a per-instance form token, escapes user/model text, and has no external scripts.

In a **second terminal**, start the dialogue listener (no paid experiments):

```powershell
$homeDir = "$env:LOCALAPPDATA\Autarky"
python -m autarky_trade.council --db "$homeDir\council.sqlite" run `
    --economy "$homeDir\economy.sqlite" --model qwen3:4b --listen `
    --evidence "$homeDir\history\funding-state-v23.json"
```

Send a message in the browser to Alpha, Beta, Gamma or all. They read unread operator messages on their next turn, along with recent relevant peer messages and measured council findings. `--listen` only wakes for unread operator messages and does not spend simulated research money. It polls every 30 seconds and runs until Ctrl+C. It is not a Windows service; the computer must be running and Ollama accessible. If the local model returns invalid JSON after retry, the error appears in the terminal; invalid structured plans are also recorded in the dashboard. No message is acknowledged by a rejected plan. Local model reasoning is an emitted text trace, not a guarantee of faithful internal computation; a fallback response may have no trace. The operator can inspect all channels. Only directed or broadcast messages are included in a recipient's recent conversation context; measured research results are shared by design. The UI addresses slots, while CLI supports a generation address:

```powershell
python -m autarky_trade.council --db "$homeDir\council.sqlite" send `
    --to alpha:1 --message "Why do you think your ETH model might cover transaction costs?"
```

Stop the listener before starting a paid research session (the turn lock prevents concurrent agent decisions). The browser and existing production monitor can stay open. Run bounded autonomous research:

```powershell
$reviewCutoff = [DateTimeOffset]::Parse("2026-06-30T23:00:00Z").ToUnixTimeSeconds()
python -m autarky_trade.council --db "$homeDir\council.sqlite" run `
    --economy "$homeDir\economy.sqlite" --model qwen3:4b `
    --btc "$homeDir\history\btc-hourly-full.csv" `
    --eth "$homeDir\history\eth-hourly-full.csv" `
    --funding "$homeDir\history\funding-btc-eth-v22.json" `
    --evidence "$homeDir\history\funding-state-v23.json" `
    --cutoff $reviewCutoff --rounds 2 --research-budget 3
```

`--rounds` gives each living generation a turn per round. `--agent alpha` limits turns to one slot. The paid-task ceiling is a maximum, not a requirement to spend. Each agent chooses whether to discuss, conserve, run directional research, run a funding-tail diagnostic, screen funding carry, or request a missing capability. They can direct their reply to a peer. Messages and outcomes persist; subsequent agents see earlier findings. Deterministic successor policies remain deterministic and conserve rather than impersonating an Ollama conversation.

## Research and economic contract

- `directional`: choose any valid research-tree `Spec` (up to 12 supported local/cross features, lookbacks, estimator, transform and calibration), BTC or ETH and 24/72/168/336-hour horizon. Not restricted to a menu or one-dimensional parent mutation. Use the existing aligned chronological evaluator and matched historical mean/risk controls. Results live in the council and economic journals; no automatic research-tree promotion or deployment.
- `funding_tail`: choose a pair, horizon and one of the four v0.23 fixed conditions. Replay the matched random-control diagnostic. Existing evidence should be supplied to avoid needless rediscovery.
- `carry_screen`: calculate completed-month funding receipts for a hypothetical constant-notional short perpetual, with illustrative 1% and 2% friction sensitivity. This is **not** a hedge simulation, annual return, basis trade, or live opportunity. Excludes hedge P&L, margin, liquidation, borrow and actual execution; no derivatives authority. Full venue/access/cost and basis analysis is required before any deployment.
- `request_capability`: record a specific missing data source or strategy mechanism and the evidence needed. No arbitrary shell, network, generated code, new data source or live exchange access is granted.
- `talk` and `conserve`: no prepaid research fee; local inference still consumes real resources. Turn records include wall/Python CPU time, not GPU power or actual bills.

Paid experiments prepay the existing research charge and one existing economic cycle's upkeep for all living agents; no revenue credit is created. The economy lock prevents concurrent economic writes. Successful directional research must be positive under stress, beat both controls and meet activity floors (10 fills, 5 active windows) to reset the research failure counter. Diagnostics do not earn financial credit; technical failures still count. The inherited per-cycle upkeep is a simulation convention, not elapsed-time cost. Do not run hundreds of cycles while prospective trials are pending. Deduplication is scoped to this council and exact data identity/parameters; previous tree/funding evidence is advisory. Interrupted tasks are not replayed or refunded. Council status is a bounded view of the latest records; full history remains in SQLite.

No live orders, customer activity, real revenue or proof of self-sustainability. Existing shadow production is independent; actual economic settlement remains future work.
