# Autarky v0.24 — Economic selection kernel

**Simulation only.** This is the first economic lifecycle integration, not a profitable trading system. Research results never become revenue. Existing paper accounts, real expense records, historical reports and research-tree databases are not migrated or reset.

## Economic contract

The separate `economy.sqlite` ledger starts with $1,000 in simulated seed capital: three founding generations (alpha:1, beta:1, gamma:1) each have $200 productive capital and $50 operating capital; $250 is the community reserve. Productive capital is **idle** in this release. It is not the balance in the legacy paper account, and it cannot pay operating costs. All amounts are integer cents. Transfers are recorded with source, destination, reference, type and tick. Status reconstructs balances from the journal and verifies conservation, including the cumulative spent sink.

A tick is a deliberately synthetic decision cycle, not a day or hour. Every incumbent pays a 25-cent simulated upkeep charge if affordable. At most one experiment is allocated per tick, using the existing market/horizon allocator filtered to active agents. Its $1 simulated research fee is prepaid. A failed or interrupted experiment is charged, too. These prices are design assumptions, not measured costs; Python CPU/wall time and optionally estimated energy are separately recorded. No actual electricity, API spend or hardware cost is inferred from the fee. The optional energy estimate is system watts × elapsed hours × USD/kWh / 1000; it is an assumed whole-system draw during the task, not GPU telemetry.

Two unsuccessful research follow-ups trigger two conservation ticks; the counter resets after the cooldown starts. Improvement means the existing allocator's evidence-based research quality rule, not realized profits. An agent that cannot pay upkeep gets a two-missed-tick grace period (retired on the second miss). Its idle productive funds and remaining operating funds go to the reserve. A replacement receives the standard allocation only if the reserve can afford it; otherwise the slot stays vacant. A successor receives a new generation ID and predecessor link. The first generations use Ollama, the second deterministic selection, and later generations alternate. This simple replacement policy is a baseline experiment, not an optimized evolutionary algorithm. Historical research remains accessible to successors, and failures remain in shared research history. No automatic real top-up, deployment, or withdrawals.

The new kernel uses the existing research allocator and research tree. It adds `economic_agent`, `economic_task` and `economic_policy` to allocation metadata. The existing direct research commands remain available for development and are **not** budget-enforced; use this kernel for economic sessions. One kernel writer and the existing allocator/tree locks serialize its research. A prepaid task left running by a crash is marked interrupted on the next run; any completed research node remains intact and can be traced via the task ID. No automatic refund or retry. Runtime is measured, but the fee is a fixed simulated tariff, not a process timeout or hard real compute cap.

## Windows

```powershell
$homeDir = "$env:LOCALAPPDATA\Autarky"
$reviewCutoff = [DateTimeOffset]::Parse('2026-06-30T23:00:00Z').ToUnixTimeSeconds()
python -m autarky_trade.economic_kernel --db "$homeDir\economy.sqlite" init
python -m autarky_trade.economic_kernel --db "$homeDir\economy.sqlite" run `
  --tree "$homeDir\research-tree.sqlite" `
  --review "$homeDir\history\horizon-review-v20.json" `
  --btc "$homeDir\history\btc-hourly-full.csv" `
  --eth "$homeDir\history\eth-hourly-full.csv" `
  --cutoff $reviewCutoff --model qwen3:4b --ticks 4
$e = python -m autarky_trade.economic_kernel --db "$homeDir\economy.sqlite" status | ConvertFrom-Json
$e.agents | Select-Object id,state,policy,operating_cents,productive_cents,predecessor | Format-Table -AutoSize
$e | Select-Object tick,reserve_cents,spent_cents,remaining_cents,reconciled,self_sustaining
```

Use `init` once. `run` resumes the ledger and accepts 1–30 ticks. `status` is read-only. Optional `status --expenses-db "$homeDir\btc-v2.sqlite"` reads existing operator-entered operating expenses (if present); no records means unknown, not zero actual cost. `run --watts W --usd-per-kwh PRICE` enables the optional assumption-based energy estimate. `init --config PATH` accepts a complete JSON configuration with the fields in `economic_kernel.DEFAULT` for a separate disposable test economy; never reinitialize an existing ledger. Keep database backups outside Git.

## What this does not do yet

No production receipt is currently credited, so the economy is a finite subsidized research population and will eventually exhaust itself under repeated ticks. The simulated fee and upkeep rates are not a claim about real economic viability. The next increment should build a prospective paper-production adapter for frozen research-tree and funding-state artifacts, with idempotent realized net P&L settlement and no double-counting. Actual revenue and actual costs remain distinct from this economy. Independent forward evidence and operator-controlled execution gates are still required before any actual trading.
