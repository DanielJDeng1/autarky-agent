# v0.11 — Cost-aware development laboratory

This release adds a separate research and forward-paper system. It does not change the legacy community, queued workers, promotion/final evaluation, unified portfolio, or live gateway. No real orders or API credentials are required. The lab's results are always **development only**, even if a policy appears profitable. The new forward runner is a manually selected experimental paper trial, not a promotion or live-authorization mechanism.

## Why this design

The first local BTC experiment used 720 hourly candles and had three important failures: high turnover, zero-activity forecasts and prose hypotheses describing conditions absent from executable filters. A 24-hour forecast with an economically meaningful threshold, longer momentum/volatility features, explicit state-dependent exits and a controlled costs comparison are practical experiments. None guarantees profitability.

Research basis (papers are not interchangeable with our long-only BTC/USD or ETH/USD use case):

- Liu and Tsyvinski, *Risks and Returns of Cryptocurrency*, RFS (2021), https://www.nber.org/papers/w24877 — crypto time-series momentum and attention are candidate signals, not a universal intraday edge.
- Novy-Marx and Velikov, *A Taxonomy of Anomalies and Their Trading Costs*, RFS (2016), https://www.nber.org/papers/w20721 — separate buy and hold requirements reduce turnover in equity strategies; test the principle rather than transplant the reported returns.
- *Technical analysis in cryptocurrency markets: Do transaction costs and bubbles matter?* (2022), https://doi.org/10.1016/j.intfin.2022.101601 — costs reduce the number of profitable rules; effects depend on asset and market regime.
- *Are simple technical trading rules profitable in bitcoin markets?* (2024), https://www.sciencedirect.com/science/article/abs/pii/S1059056024003010 — evidence is mixed across periods and methods; no assumed edge.
- Bailey and López de Prado, *The Deflated Sharpe Ratio*, https://doi.org/10.2139/ssrn.2460551 — trial selection and non-normal returns inflate apparent performance; the lab logs reuse and makes no statistical promotion claim.
- Hyndman, *Cross-validation for time series*, https://robjhyndman.com/hyndsight/tscv/ — chronological refits only on prior observations.
- Kraken historical data, https://support.kraken.com/articles/360047124832-downloadable-historical-ohlcvt-open-high-low-close-volume-trades-data — CSVs can contain gaps, and the REST window is limited. The lab never invents candles.

## Windows PowerShell: first run

From the repository root, update and verify:

```powershell
git pull --ff-only
python -m unittest discover -s tests -v
```

Obtain the Kraken XBTUSD hourly CSV from the official historical-data page. The full archive is large; use relevant quarterly archives if you already have history. Extract the **60-minute XBTUSD** file, not a different venue, pair, or interval. The format is timestamp (Unix seconds UTC), open, high, low, close, volume, optional trade count. A header is optional. Do not use final evaluation data as development data. Pick and record a cutoff before inspecting results; example below uses the end of September 2026 UTC. This is historical development, not an untouched independent test.

```powershell
$homeDir = "$env:LOCALAPPDATA\Autarky"
New-Item -ItemType Directory -Force "$homeDir\history"
# Copy the extracted file to $homeDir\history\btc-hourly.csv.
$cutoff = [DateTimeOffset]::Parse('2026-09-30T23:00:00Z').ToUnixTimeSeconds()
python -m autarky_trade.lab --db "$homeDir\lab.sqlite" run --csv "$homeDir\history\btc-hourly.csv" --pair XBTUSD --cutoff $cutoff --model qwen3:4b
```

Omit `--model qwen3:4b` for the seven deterministic benchmarks alone. A model run adds up to four typed policies. All conditions must be represented in executable entry/exit arrays; the free-text hypothesis is not executable. The report includes an authoritative generated description. Invalid model output is retried once, then fails without touching the community or its holdout.

The lab also accepts `--source "$homeDir\btc-v2.sqlite"` instead of `--csv ... --pair ...`. It reads the existing archive read-only. The current 720-bar archive is too short: six folds require at least 1,729 closed hourly bars (721 warmup + 6×168). Three folds require 1,225. Several years are preferable. The lab selects the latest contiguous suffix at or before the cutoff, up to 100,000 bars (~11.4 years). A gap truncates the earlier history; inspect `first`, `last`, and `bars` in the report. A CSV's pair/provenance is operator-supplied; preserve source files and checksums. For live-data archive continuity, inspect the latest contiguous suffix and avoid silently filling missing observations.

Review the most recent five experiments:

```powershell
python -m autarky_trade.lab --db "$homeDir\lab.sqlite" status
```

For a compact summary in PowerShell:

```powershell
$r = python -m autarky_trade.lab --db "$homeDir\lab.sqlite" status | ConvertFrom-Json
$r[0].results | Select-Object @{n='Name';e={$_.policy.name}},mean_net,mean_gross,mean_stress,mean_passive,positive_windows,fills,diagnosis | Format-Table -AutoSize
```

`--policies path.json` adds a JSON array of custom policies, with fields name, entry, exit, min_hold, cooldown, max_hold, fraction, hypothesis. Entry is ALL; exit is ANY. The default max hold is 720 hours, cash fraction .25. Rules: momentum (fractional close return), distance (close/SMA-1), volatility (standard deviation of hourly fractional returns), RSI (0..100), volume_ratio (last bar volume divided by preceding mean), breakout (last close/prior maximum high-1). Windows 2..720. A predictor policy can specify `predictor: {"horizon":24,"window":72,"ridge":1.0}` and use a `forecast` rule whose window equals horizon. Qwen proposes indicator policies; the deterministic forecast pair is built in. No arbitrary Python execution.

## Method and interpretation

- Decision sees only completed preceding bars; fill is next open in historical experiments. Risk mark at open and close; a close-observed stop exits at next open, except scheduled end-of-window liquidation. No assumed intrabar stop fills. Exposure long only, fraction at most 25%, no leverage.
- Each of 3–12 chronological development windows starts with independent $1,000 capital; these are descriptive repeated experiments, not one continuous equity curve. The report gives average per-window returns, not annualized performance. Model refits use up to 8,760 earlier decision observations and exclude any label whose endpoint reaches the new window. Model weights, scales, labels' time bounds and training digest are saved.
- Normal costs 40 bps fee + 20 bps execution proxy per side; stress 80+40. These are assumptions, not quotes. A frictionless shadow replays the exact net strategy's entry/exit decisions and cash fraction, isolating cost drag without changing the trade path. Stress may change stop/entry behavior. The passive comparison holds the same starting fraction with the same normal friction; it is not stopped at 8%, so compare risk separately. Cash benchmark is zero.
- Record net, same-path gross, fees, execution friction, turnover, fill count, exposure, cooldown blocks, stop and exit reasons. Each window reports its ex-post market direction and volatility (diagnostics only). Forecast RMSE, zero-forecast RMSE and direction accuracy use nonoverlapping labels ending inside each window. A matched comparison of the two built-in ridge variants isolates the entry threshold change; it is exploratory, not independent evidence. No activity is not evidence of edge. A positive gross return with nonpositive net return is cost dominated. All other conclusions remain exploratory.
- Lab reports contain dataset fingerprint, cutoff, actual span, model label, exact policy identities and number of prior experiments (same dataset and total). Names/hypotheses do not change executable identity. Reusing data is deliberate and logged; overlap across datasets is not treated as independent. Rank by average net return, stress as tie-breaker, not statistical significance. No automatic promotion or final evaluation.

## Optional forward-paper trial

Choose a policy from a completed report for an explicit prospective experiment. This is **not** the existing community or unified portfolio. No live qualification is implied, and there is no requirement to select a winner. One frozen policy and market per database. A predictor uses the final development window's pre-window fitted artifact, not a refit on future data.

```powershell
python -m autarky_trade.lab_paper --db "$homeDir\lab-forward.sqlite" init --source "$homeDir\lab.sqlite" --experiment 1 --policy trend-daily
python -m autarky_trade.lab_paper --db "$homeDir\lab-forward.sqlite" run --poll 60
```

Use the actual experiment ID and policy name. `status` displays the frozen specification, account and recent quote/decision journal. `stop` latches the stop; keep the process running until the next fresh quote flattens the paper position. Ctrl+C stops polling without closing positions. New entries occur only on closed candles after both development and initialization. Polling uses Kraken REST bid/ask and actual spread plus simulated 20 bps additional slippage and fees; this is intentionally more conservative than the historical proxy. No missed-candle replay; age/cooldown use elapsed candle time. Quote freshness is receipt-time based, not an exchange timestamp. Lookbacks above 718 hours are refused for the forward runner because REST history is limited. Missing data may prevent execution/flattening; this is paper, not live execution. Operator must monitor continuity and report gaps.

## Remaining work

This is the first research intelligence milestone, not a replacement for the production community. Existing independent workers do not yet iterate through lab experiments or exchange findings; the unified portfolio and final evaluator still use the old Strategy artifact, deliberately preventing unvalidated promotion. Next: bounded agent research sessions with development memory, matched ablations, cross-regime diagnostics, then an independent promotion protocol and compatible shared paper execution. Do not enable live funds based on these results. Synthetic tests check mechanics, not economic merit. A sustained real historical run and prospective forward validation on the operator's machine remain necessary.
