# Research notes

Autarky is intended to make experiments repeatable. It does not claim to discover profitable strategies.

## Data and selection

Research accepts completed OHLCV candles with ascending Unix-second timestamps. Duplicate, malformed or irregular candles are rejected, not interpolated. The data audit reports gaps separately so a researcher can make an explicit choice about the segment they use.

There are three fixed strategy families: trend, mean reversion and breakout. Each begins with four baseline parameter pairs. An optional local model can propose up to five additional valid pairs. Deterministic code ranks candidates using training and validation data.

The coordinator reserves an unseen chronological test period before workers begin. Only the selected candidate receives the test and a higher-cost stress evaluation. Failed research batches also consume their reserved test interval.

## What this does not prove

- Repeated parameter searches create a multiple-testing problem; the current research gate has no formal correction.
- Repeatedly reading test results and modifying the process can compromise a nominally untouched test interval.
- Fee and slippage assumptions do not model execution latency, real order-book depth, or changing liquidity.
- A favorable result from one market regime can disappear in another.
- Simulated fills do not establish the existence of an executable trading edge.

An unsuccessful trial should remain visible. It is evidence about the specific strategy, data window and cost assumptions rather than a failure of the research software.

## Reporting

Useful experimental records contain the asset and interval, source of historical observations, date range, sample size, split boundaries, selected parameters, baseline comparisons, transaction costs, test performance, and stress-test performance. Show negative results. A final independent evaluation set should be kept outside the iterative research loop if making statistical claims.
