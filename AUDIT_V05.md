# v0.5 audit and decision log

Scope: v0.4 research, strategy identity, queued experiments, simulation, allocation, archive, dashboard and operating model. No exchange authentication or real funds are introduced.

## Changes made

- **Selection:** train and full validation must pass before a candidate outranks a failing one. Three chronological validation subwindows; at least two profitable, bounded-drawdown windows and no stop. Rank by full-validation return minus drawdown plus worst-fold return minus drawdown. A single selected candidate is evaluated on the reserved holdout. If no candidate passes, the report remains diagnostic and cannot promote. No ranking on holdout. Strategy IDs exclude names and hypotheses; duplicate configurations are evaluated once.
- **Economic evidence:** historical tests model a 10 bps half-spread proxy in addition to 10 bps execution slippage and 40 bps fees per side. Double the modeled costs for stress. Require outperformance of random timing at its 75th percentile and a passive allocation-matched return-minus-drawdown score. Neither is a significance test or actual exchange spread. The passive reference uses the same risk stop and final cost marking.
- **Independent experiments:** the optional model can give a falsifiable hypothesis; queued research stores the exact proposals, model and protocol before assessment. Recovery reuses that plan; changed protocol cannot replay an old plan. Reports store dataset fingerprint, protocol, windows, costs, candidate identities, all validation results and rejection checks. Past training/validation results feed subsequent model calls. Prior claims are not facts. A legacy report cannot promote under the new protocol.
- **Forward evidence:** preserve a pending candidate and freeze an active strategy for 60 days of market-clock time. Later research can continue without restarting the active trial; log staging decisions. A halted portfolio or agent cannot stage. Allocation uses current strategy NAV, active fills, 95% candle coverage, age and risk stop rather than lifetime P&L. No new capital for a pending switch.
- **Risk:** each fresh quote revalues paper accounts, including within an already-processed candle. No duplicate entries or inflated tick counts. Intrabar stops flatten at the observed simulated bid; a gap or failed network request can still exceed limits. Post-fill agent stops also flatten immediately. A pending candidate whose holdout is still in the future does not interrupt the current strategy. Old candle messages are ignored.
- **Inputs:** reject invalid candle timestamps. Existing strategy records without hypothesis remain compatible; queued plan table is additive.

## Remaining risks and design decisions

| Priority | Issue | Next decision |
| --- | --- | --- |
| High | Repeated adaptive experiments, three agents testing correlated hypotheses, small sample and regime dependence. | Reserve an independently managed final dataset; add purged walk-forward and block-bootstrap confidence intervals, multiplicity-aware testing, cross-asset validation. |
| High | Historical OHLC and ticker polling cannot model depth, partial fills, latency, outages or actual spreads; no exchange timestamps. | Add observed quote/depth archive, replay and independently reconciled sandbox execution before live funds. |
| High | Paper profits do not deduct electricity, hardware or human labor. | Record measured operating costs and separate capital from income; only claim sustainability on realized external returns net of operating costs. |
| High | Local agents share a process-level trust boundary and a writable database. | Move toward separate service identities and a restricted execution gateway; no model-generated shell or exchange credentials. |
| Medium | Research is still bounded by three base families, not autonomous model training or arbitrary strategies. | Add typed feature graphs and isolated ML experiments with frozen datasets and evaluation; avoid arbitrary unsandboxed code. |
| Medium | Portfolio concentration in one market and correlated agents; reserve only grows via unused funds and allocation is one-way. | Multi-market exposure and portfolio risk budget; evaluate retirement/reallocation policies rather than reward raw return. |
| Medium | Older v0.2 legacy CLI remains separate and has weaker research gates. | Keep for reproducibility only; do not use it for promotion or real trading. |
| Medium | Imported CSV provenance is operator-asserted, archived gaps truncate usable history, and exact overlapping candle differences require investigation. | Preserve source manifests and dataset lineage; reconcile vendor precision without silently changing evaluated data. |
| Medium | Dashboard reads across multiple transactions; intrabar exit accounting is in events, not candle history. | Add consistent portfolio snapshots and an order/fill journal for exact execution audit. |
| Medium | Long-running jobs can exceed leases; current retry freezes plans but has no heartbeat. | Add renewal with bounded inference and watchdog; run one model at a time on constrained GPU. |

## Deployment acceptance

Use a stopped/backed-up database when upgrading. Existing legacy active paper strategies are not retroactively validated by the new protocol; use a new database for a clean research experiment. Verify tests, local Ollama, Kraken public data, and a sustained paper trial. No live adapter exists. Do not infer actual profitability from synthetic tests or readiness gates. No real orders have been placed.
