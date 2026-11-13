# v0.12 — Evidence-driven research sessions

The first real v0.11 experiment used 2,208 hourly BTC candles. The four Qwen policies chose 1% allocation, versus 25% for the built-in policies. Trend-volatility and breakout-volume made small positive net development returns but underperformed the allocation-matched passive benchmark and lost under stressed costs. The 24-hour ridge sign rule traded too frequently; the 1.2% threshold variant never traded. This motivates matched allocations, bounded ablations, and investigation of the signal and forecast distributions before further tuning.

## Upgrade and run on Windows

```powershell
cd "C:\Users\danie\OneDrive\Documents\autarky-agent"
git pull --ff-only
python -m unittest discover -s tests -v
$homeDir = "$env:LOCALAPPDATA\Autarky"
$cutoff = [DateTimeOffset]::Parse('2026-09-30T23:00:00Z').ToUnixTimeSeconds()
ollama list
```

Use the existing Q3 `btc-hourly.csv` and the same fixed cutoff, with a **new research-memory database**, not `lab.sqlite` or `btc-v2.sqlite`. Run the agents sequentially initially to avoid contention on the single GPU:

```powershell
$common = @('--db', "$homeDir\research-memory.sqlite", 'run', '--csv', "$homeDir\history\btc-hourly.csv", '--pair', 'XBTUSD', '--cutoff', "$cutoff", '--model', 'qwen3:4b', '--budget', '3')
$alpha = python -m autarky_trade.research_agent @common --agent alpha | ConvertFrom-Json
$beta = python -m autarky_trade.research_agent @common --agent beta | ConvertFrom-Json
$gamma = python -m autarky_trade.research_agent @common --agent gamma | ConvertFrom-Json
```

Each run recomputes two baseline policies, measures the seed's entry features and the shared ridge forecast, and performs at most three matched comparisons (up to 20 via `--budget`). Omit `--model` to select the first untested menu item deterministically. No model is required for quantitative tools. The model can select a validated candidate, not execute code or modify the budget. Each model choice uses a bounded menu and up to two attempts; malformed output fails visibly. Qwen receives earlier completed matched trials, including other agents' trials from the same dataset and fold count. The engine does not automatically schedule further sessions.

Compact report:

```powershell
$alpha.trials | Select-Object trial_id,change,@{n='Control%';e={[math]::Round($_.control.mean_net*100,3)}},@{n='Variant%';e={[math]::Round($_.variant.mean_net*100,3)}},@{n='Stress%';e={[math]::Round($_.variant.mean_stress*100,3)}},@{n='Fills';e={$_.variant.fills}} | Format-Table -Wrap
$alpha.diagnostics.forecast.threshold_coverage
$alpha.diagnostics.forecast.windows | Format-Table -AutoSize
```

The same commands work for `$beta` and `$gamma`. To recover a report after closing PowerShell:

```powershell
$recent = python -m autarky_trade.research_agent --db "$homeDir\research-memory.sqlite" status | ConvertFrom-Json
$recent | Select-Object session_id,agent,status,completed_trials,bars | Format-Table
python -m autarky_trade.research_agent --db "$homeDir\research-memory.sqlite" trial 1
```

Status reports the last ten sessions and the authoritative trial states. `trial` accepts the actual trial ID and includes the full matched window reports, exact policies, rationale, and measured comparisons. Failed/interrupted attempts remain in memory; a new run skips the same ordered control/variant pair on the identical dataset and fold count. A hard process kill can leave a session marked running and a trial pending; these are not completed evidence. Start a new session rather than deleting records. Each comparison is logged before execution. There is no automatic resume of the interrupted comparison, and no global search budget across operator-initiated sessions.

## Research design

- The v0.11 `lab run --model` pathway now forces a 25% allocation for every generated policy (custom manually authored policies can still set sizing explicitly). Old recorded experiments remain unchanged. The agent's controls and variants use the same 25% cash allocation. This is a fixed allocation, not equal volatility or equal realized exposure.
- Alpha starts from `trend-volatility`, Beta from `reversion-recovery`, Gamma from `breakout-volume`; all can investigate `ridge-24-sign`. The engine retains the seed and shared predictor as controls and may add the best active variant from the agent's recent trials. One threshold, minimum hold, cooldown, or maximum hold changes at a time. Forecast entry candidates include 0.2%, 0.4%, 0.6% and 1.2%; those thresholds are exploratory, not known to cover costs. Lower turnover or improved net return alone does not imply economic edge.
- Feature quantile boundaries are frozen using only the initial 721-bar warmup, with subsequent nonoverlapping open-to-open labels. Equal boundaries collapse, empty buckets report null. Forecast diagnostics fit independently before each development fold; out-of-fold prediction quantiles and threshold coverage describe the development distribution. Prediction coverage is not actual trading frequency. Development results may inform future experiments, never an untouched test.
- Shared memory is keyed by exact dataset digest and fold count. It keeps complete results, not just winners; the model sees a bounded recent subset, plus baseline and diagnostics. It cannot see a promotion or final evaluation database. Reusing a historical dataset or changing its cutoff does not create independent evidence. Same-path gross returns diagnose costs, while net and stressed results can have different trade paths. Window resets and passive benchmark conventions remain those of v0.11.
- The agent module does not use the legacy community's promotion path, and cannot place exchange orders. Its output is always development-only and ineligible. Existing `lab.sqlite` and community databases are not modified. The new session store is separate; experimental policies are not yet wired into the unified paper portfolio or final evaluator.

## Next

Run these sessions on the existing Q3 dataset to validate local integration, not to select a live winner. Then obtain several years of contiguous Kraken BTC hourly history (the lab accepts the same CSV format, up to 100,000 bars, and selects the latest contiguous suffix). Preserve source provenance, inspect actual coverage, and establish a new development cutoff before evaluating. The current release does not automate downloading/combining multi-year archives, run scheduled agent sessions, or establish a profitable strategy. Future work: stronger signal tests, multi-year analysis, independent promotion for the new Policy representation, and compatible shared paper execution.
