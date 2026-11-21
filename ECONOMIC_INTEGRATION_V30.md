# Autarky v0.30 — Execution economics integrated into autonomous research

This is a **paper and historical-development-only** update. It does not enable live orders, move funds, access a private wallet, or establish any real external revenue. The frozen production evaluator and historical v0.20 protocol remain unchanged.

## What changed

- New `autarky_trade/economic_policy.py` replays each **new** directional research proposal's existing walk-forward predictions and fixed thresholds under four explicit fee/slippage assumptions. No parameter refitting occurs. The replay checks the entire input dataset fingerprint, each decision/label timestamp, training/calibration boundaries, selected entry count, and reproducibility of the original normal/stress account results.
- The Council stores the candidate's `execution_screen` with the paid experiment. Historical research credit now requires the existing positive stressed result **and** a cost-sensitive replay showing positive return relative to its controls. No agent receives actual earnings for a backtest. The screen separately reports whether simple annualized historical results would cover the simulated operating policy; a positive historical monthly return can still be economically insufficient.
- New `Economic feasibility` tab in the local Operations Center. It ranks saved v0.29 replay results for Kraken-taker and lower-fee **proxies**, including cost-stressed returns, annualized hypothetical surplus, fills, and the annual return required to cover simulated upkeep. The UI does not count stress or mean accounts as extra money.
- The runtime loads the optional local `history/economic-review-v29.json`, checks its **source report fingerprint**, and passes a bounded set of those results to the agents. This protects against accidentally using a result from a different historical review, but is **not a cryptographic attestation of the report's calculations**. The report remains development evidence, not an untouched holdout.
- If that review is missing and the matching v0.20 history is present, a one-time background worker automatically calculates the original v0.29 review on startup. It writes the result atomically, separately from agent research and the public-market monitor. Existing invalid/stale files are *not* silently overwritten. Warnings appear in the events feed. This requires no exchange credentials and no simulated treasury mutation.
- Summaries shown to the 4B local model are compact. Both a negative cost-adjusted result and failure to cover simulated upkeep remain visible to its research decisions.

## Update and run — Windows PowerShell

Close any previous Autarky runtime process, keep Ollama running, then:

```powershell
cd "C:\Users\danie\OneDrive\Documents\autarky-agent"
git pull --ff-only
python -m unittest discover -s tests -p "test_economic_policy_v30.py" -v

$homeDir = "$env:LOCALAPPDATA\Autarky"
$reviewCutoff = [DateTimeOffset]::Parse("2026-06-30T23:00:00Z").ToUnixTimeSeconds()

python -m autarky_trade.runtime --home "$homeDir" --cutoff $reviewCutoff --model qwen3:4b --check
python -m autarky_trade.runtime --home "$homeDir" --cutoff $reviewCutoff --model qwen3:4b --research-interval 300 --poll 30 --daily-budget 3
```

Open http://127.0.0.1:8768 and select **Economic feasibility**. On the first startup, the read-only historical review may be processing. An invalid/stale report will generate an explicit warning; preserve the old report before deciding to generate a replacement manually. If `horizon-review-v20.json` is missing, the agent community continues running without a cost ranking.

The auto-generated v0.29 report defaults to **$200 productive capital per strategy and $0.25/day *simulated* upkeep**. Neither number is a measurement of real operating expenses or your current exchange fees. You should enter independently verified account-specific fees and actual operating expenses in a later review before using results for any real-money decision. The lower-fee scenario applies a fee proxy to Kraken price history; it is not an OKX execution backtest.

## Verification

- 10 new focused tests pass in the reconstructed development environment (cost replay, economic shortfall, source fingerprint, modified labels, noncausal training, saved-account inconsistency, automatic generation and restart behavior).
- Python syntax validation passes for all changed runtime modules; JavaScript syntax validation passes for the UI.
- The frozen production engine, original simulated treasury accounts, and trial hashes have not been modified.
- Full-repository regression, Windows/Ollama integration, live-venue connectivity, and real historical-data replay have **not** been performed in this environment. Run the complete test suite and inspect the economics tab on the user's machine.

## What this cannot establish

A development backtest cannot prove a strategy will be profitable in the future. Simple annualized returns from 30-day account resets are illustrations, not predictions. The simulated economic kernel is not a real brokerage account. Funding and spot market access are subject to exchange jurisdiction, account eligibility, instrument minimums and actual price/fee schedules. No live execution has been authorized.

**Next economic decision:** inspect the generated cost ranking. If every strategy loses money after assumed taker costs or fails operating break-even, preserve capital and stop allocating research to small variations of those directional models. Investigate distinct mechanisms and independent forward tests rather than pretending repeated historical optimization is progress.
