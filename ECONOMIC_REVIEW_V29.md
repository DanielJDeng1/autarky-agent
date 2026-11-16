# v0.29 — Economic feasibility review (2026-11-15)

This is a read-only research utility, not a trading-engine release. It does not modify the runtime, frozen trials, paper accounts, or economy database.

## Decision

- Reprice the original v0.20 predictions at current fee assumptions before tuning another model. Keep the original entry thresholds, label coverage, and risk stops; re-run the same historical accounts. Confirm that the saved normal and stress results reproduce. This is a sensitivity analysis, not an independent holdout.
- Kraken currently publishes an entry-tier spot fee of 40 bps maker / 80 bps taker. The legacy 40-bps marketable-order assumption is no longer an appropriate base case for a new account. The original normal and stress scenarios remain in the report to preserve comparability. At 80 bps fee + 20 bps slippage per side, a round trip needs 2.0202% gross appreciation before breaking even.
- OKX US became available to eligible Texas residents in September 2026. Its 2026 US fee framework lists 35 bps regular taker for Group 1 (BTC and ETH). This is a sensitivity proxy on Kraken data, not an OKX backtest; verify the actual current account fee, order book, minimum size and API access before changing venues. Binance.US's much lower published fees are not an option for a Texas resident under its current unsupported-state list.
- US Kraken perpetuals are Bitnomial products, not the offshore Binance USD-M instruments in the funding dataset. The Kraken-listed BTC and ETH contracts have sizes 0.01 BTC and 0.5 ETH respectively. A $200 agent with $50 per leg cannot assume these contracts fit. There may be other contract sizes on Bitnomial; verify broker listing, eligibility, margin and execution before any proposed trading. No derivatives orders.
- Public secondary research (Edgoro, Nov 9, 2026) reports approximately 3.472% BTC and 2.5085% ETH cumulative funding on constant notional over Nov 2025–Oct 2026. At $50 fixed notional, that is $1.74 and $1.25 gross per year, before hedge P&L and costs. These figures are not verified from the raw archives in this environment and are not US venue returns. The local review separately computes actual archived funding sums for the 12 full months preceding the saved development cutoff, using the existing validated funding dataset. At $200 productive capital, $0.25/day simulated upkeep needs 45.625% annual return on the entire account; if only $50 is the funding notional, gross funding alone would need 182.5% per year to cover upkeep. The simulated upkeep is a policy, not measured real electricity or inference cost.

## Run

```powershell
$homeDir = "$env:LOCALAPPDATA\Autarky"
python -m unittest discover -s tests -p test_economic_review_v29.py -v
python -m autarky_trade.economic_review --home "$homeDir" --output "$homeDir\history\economic-review-v29.json"
```

Inputs: `history/horizon-review-v20.json`, `history/btc-hourly-full.csv`, `history/eth-hourly-full.csv`, optionally `history/funding-btc-eth-v22.json`. Override `--report` or `--funding` if needed. No network requests. The saved review's dataset identity and raw candles must match. The report contains the original models, control returns, fills and drawdown under all four cost scenarios, annualized simple dollar illustrations, break-even productive capital and the funding-only screen. It rejects incomplete funding coverage rather than silently filling gaps. To model real measured operating expenses, pass `--daily-cost` with measured dollars/day rather than treating $0.25 simulated upkeep as actual cost. To rerun choose a new output filename. The original runtime may remain running.

This module does not generate a new trading strategy, make a live order, or promote a paper strategy. The direct local replay must be run on the user's machine; the raw historical dataset is not accessible in the development environment. Tests use synthetic rising BTC/ETH candles through the existing research and account evaluators, not real market performance. A future OKX evaluation should use its own actual historical candles and prospective quotes, and a US carry evaluation must use actual domestic contract data and trade sizing. Do not equate lower costs with a proven edge.

## Sources checked November 15, 2026

- Kraken Pro fee schedule: https://www.kraken.com/pro/fees
- OKX US fee framework: https://www.okx.com/en-us/help/updates-to-us-fee-framework-2026
- OKX Texas launch: https://www.okx.com/learn/okx-available-in-texas
- Binance.US geography: https://support.binance.us/en/articles/9842798-list-of-supported-and-unsupported-states-and-regions
- Kraken US perpetuals: https://support.kraken.com/articles/us-perpetual-futures
- Kraken contract specs: https://support.kraken.com/in/articles/contract-specifications
- Bitnomial funding API: https://bitnomial.com/exchange/docs/api/rest/funding-rates/
- Secondary historical funding summary: https://edgoro.com/blog/crypto-funding-rates-what-they-cost
