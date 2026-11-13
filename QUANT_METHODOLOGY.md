# Quantitative research protocol v0.6

## Design and research decisions

Autarky is a paper-only experimental community. A trained model is a research candidate, not evidence of a tradeable edge. We intentionally use a cheap, inspectable ridge regression before adding PyTorch, RL, or arbitrary generated programs. The LLM proposes parameters and hypotheses; a deterministic CPU fitter produces the model. The three agents keep their rule families and each receives a distinct baseline predictor, plus up to three optional model-proposed predictor specifications. No model can issue orders, load executable code, or access credentials. Model artifacts include feature window, regularization, decision threshold, normalization statistics, coefficients, label count, training end and training SHA-256. A change in fitted parameters or training data produces a new strategy identity. Existing non-ML identities remain stable.

### Temporal contract

For an order at open i, features use completed closes through i-1. The supervised target is open[i+1]/open[i]-1; a training row is discarded if its target needs an open outside the training prefix. Means/scales are fitted only in that prefix. Training replay is explicitly labeled in-sample; it is not evidence of predictive skill. The fixed artifact is independently reproduced from the exact training prefix before assessment. It is then evaluated on three subsequent validation subwindows and a reserved holdout, with selection performed before inspecting holdout. Only the winner is evaluated on holdout. Training/validation results from earlier completed trials may be used to propose new ideas; they are not included in a final untouched evaluation claim. Agents cannot automatically modify an active artifact between paper observations.

For a selected predictor, rolling-origin refit diagnostics separately fit on the strictly earlier history at each of the three validation fold starts. These are diagnostic results of a hypothetical refit policy, not results of the frozen artifact actually paper traded. The fixed-artifact gate remains authoritative. No hyperparameter selection uses holdout. Long-horizon labels would require additional purging/embargo; the present one-bar target has an explicit boundary. This is not a full nested CV or cross-market study.

### Inference and multiplicity

- Bailey et al., *The Probability of Backtest Overfitting*, https://doi.org/10.21314/jcf.2016.322: ordinary holdout and optimization can be misleading; record every candidate, including rejected ones, preserve reserved intervals, and eventually estimate selection risk across experiments. We do not implement CSCV/PBO.
- Bailey and López de Prado, *The Deflated Sharpe Ratio*, https://doi.org/10.3905/jpm.2014.40.5.094: multiple trials and non-normality inflate apparent performance. Reports record the unique candidate count and the full ranked development results; this count is not the number of independent trials. We do not implement DSR or claim multiplicity control.
- Hyndman and Athanasopoulos, *Forecasting: Principles and Practice*, https://otexts.com/fpp3/tscv.html: rolling forecast origins keep training before evaluation. The ridge diagnostics follow that principle.
- Lo, *The Statistics of Sharpe Ratios*, https://doi.org/10.2469/faj.v58.n4.2453: serial dependence affects risk-adjusted performance estimation. Avoid naive annualization and independent-bar inference.
- Politis and Romano, *The Stationary Bootstrap*, https://doi.org/10.1080/01621459.1994.10476870: resampling dependent observations in blocks motivates the diagnostic. Our implementation uses fixed-length circular moving blocks, **not** their random-length stationary bootstrap.

### Economic comparison

The same backtest path calculates cost-aware candidate, passive and random-timing returns and drawdown. It marks opening gaps before deciding whether to enter or exit. Signals cannot see the execution bar's close. End-of-period liquidation is a common accounting convention. OHLC cannot prove attainable fills; no intrabar order-book replay. Cost assumptions are fixed and hypothetical. The paper engine still uses actual public quotes and modeled fills, not exchange-confirmed fills.

`evidence` in a trial report contains per-bar log return differences versus 25%-allocated passive exposure with the same risk stop and costs, compounded relative return, and 95% percentile intervals for the mean paired difference using 999 seeded circular-block resamples at three block scales. Both paths share identical time indices. These intervals are descriptive sensitivity checks, conditional on the selected artifact and available data. They are not adjusted for adaptive selection, stationarity failure, nonrepresentative regimes, or multiple comparisons. Neither a positive interval nor the paper promotion gate authorizes live trading. Keep a separate untouched final dataset and test a predeclared strategy and analysis plan with independent verification before live capital.

### Resource accounting and worker lifecycle

Queued workers renew only their own unexpired lease, periodically, for a bounded renewal window. Expired/stale results still fail closed. Long jobs may be reclaimed; the heartbeat does not kill a hung process. Record completed or failed attempts' elapsed wall time and Python process CPU time keyed by lease token. CPU excludes the Ollama server; crashes can omit usage. Actual expenses are operator-entered USD cents, idempotent by reference. Paper trading gains are never used as real revenue and never pay these costs. Do not claim sustainability until actual reconciled realized returns exceed actual costs. No compute budget allocation, automatic retirement, GPU metering, or multi-market portfolio is implied by this ledger.

## Operator commands

```sh
python -m unittest discover -s tests -v
python -m autarky_trade.community init
python -m autarky_trade.community research --source synthetic
python -m autarky_trade.community trials
python -m autarky_trade.resources status
python -m autarky_trade.resources expense --reference electricity-2026-11 --usd 2.50 --category electricity --note "Measured lab share"
```

Use a separate database for an honest experiment; stop workers and back up existing databases before upgrades. Old in-flight v0.5 research plans cannot be reused under the new protocol and will fail/consume their reserved interval. Existing active paper strategies keep operating unless risk-stopped; they are not retroactively validated by v0.6. Actual exchange execution, independently isolated process identities, order-book replay, multi-market risk allocation, and real profitability remain future work. No credentials or real orders are involved.
