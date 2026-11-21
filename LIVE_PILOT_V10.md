# v0.10 — bounded live execution pilot (experimental)

**This module can place real orders only after explicit operator setup and per-order confirmation. No real order was placed during development.** The existing community, portfolio, final evaluator and v0.9 validation gateway remain paper/validation-only. No autonomous live trading loop is included. Passing tests with a simulated exchange does not establish live connectivity, trading edge, or profitability.

## Design decisions

- One frozen BTC/USD or ETH/USD spot strategy and one dedicated USD-only account. Initial balance must be positive and no more than $100; no deposits, transfers, withdrawals, leverage, other assets, or external trading during a session. Do not use a main exchange account with other holdings. There is no automatic account funding or key creation.
- Arm only from a **completed, passing, single-market prospective** v0.7 final evaluation (90+ days), with the exact strategy ID and plan hash. The final ledger is read-only. A passing historical sealed test is insufficient. The operator owns the final database and must independently verify provenance, evidence, forward paper behavior, and actual fee schedule. A local SQLite record is not a cryptographic attestation of market history or profitability.
- Dedicated key with exactly `query-funds`, `query-open-trades`, `query-closed-trades`, `modify-trades`, `close-trades`. `GetApiKeyInfo` checks these at arm and before sending. No withdrawal permissions. Use a separate key/nonce store, IP restrictions where available, and protect the key against all research processes. No credentials enter Git, Ollama, output, or the ledger. Key rotation requires a new reviewed session; do not delete a session with unresolved orders.
- Separate `live.sqlite`, explicit arm confirmation, and per-order `AUTARKY_LIVE_ENABLE=YES` plus `--confirm 'SUBMIT REAL ORDER'`. The ordinary Kraken transport still rejects live mutations. The live transport allows only bounded IOC spot limit orders and cancellation of a specified order ID. The CLI is an operator process, not a security sandbox; someone with access to the credential or execution source can bypass application checks. OS isolation is required below.
- Normal plans use a newly closed, aligned candle (within 90 seconds of close), after the final evaluation. The frozen signal proposes a long entry or exit; no buys if already holding an asset. Prices use the fresh bid/ask plus a 20 bps limit buffer, rounded to the market tick; quantities are rounded down to exchange lot precision. The preflight rechecks account balances, instrument metadata, permissions, minimums and live price. Plan expires after 30 seconds. There is at most one buy and one sell intent per candle; normal decisions use one candle once. No auto-retry or catch-up trades.
- Each order is capped at $25, entry at 25% of available cash (with 1% fee allowance), aggregate asset exposure at $50 and daily buy turnover at $50. Observed daily equity loss of 3% or peak-to-current drawdown of 8% stops new buys. The day rolls on UTC; overnight gaps count against prior observed equity. These are hard software development limits, not loss guarantees or an optimal investment policy. A stopped account can still propose an operator-approved exit, including inside the same candle. A partial sell or exchange minimum can leave residual inventory; use the exchange UI for emergency resolution if needed. No automatic liquidation is implied.
- Send `timeinforce=IOC`, `ordertype=limit`, `oflags=fciq`, and a 20-second matching-engine deadline. IOC cancels unfilled remainder rather than leaving a resting order. The venue may reject or partially fill. Fee currency preference is not a guarantee. Validate-only and live transports are distinct. No market orders or conditional orders.

## Durable lifecycle and accounting

The intent is committed before any mutation. A final database writer lock serializes the submission against the operator stop; a stop may wait for one in-flight network request, and it cannot undo accepted fills. A persisted `submitting` row after a crash is never resent. Acknowledgments record one exchange transaction ID; uncertain responses halt the session. Reconcile by known transaction ID or search open/closed orders with the exact client UUID. No match is **not** proof of absence. If a claim is interrupted, stop all submitting processes, wait at least 60 seconds from creation, then reconcile. Never recreate the order to resolve an unknown outcome.

The order summary and individual trade records must agree on client ID, market, side, volume, price limits, cumulative quantity, cost and fee. After a terminal order, actual balances must exactly agree with cash and inventory reconstructed from the confirmed fills. This deliberately rejects unexpected fee currencies, rounding differences, deposits or other external transactions instead of quietly adjusting the ledger. A partial IOC fill with canceled remainder is valid. An unexpectedly open IOC order stops the session and requires reconciliation and explicit cancellation. An acknowledgment or cancellation is not a fill. Confirm terminal state and inspect balances separately. No new intent while a submission is unresolved. Once confirmed, fill IDs are unique and reconciliation is idempotent; status reconstructs realized P&L and cost basis from actual fill records, with separately marked equity. It is not a tax ledger. The mark can be stale; check `last_observed` and `last_bid`.

Risk observations are explicit, not a background daemon. While inventory is open, poll `observe` from an operator-managed process. It stops on market/account drift and updates observed equity independently of candles. It does not place a sell. If a quote or exchange request fails, stop new entries, inspect Kraken directly, and do not assume exposure was closed. A stopped session cannot be reset in place. Reconcile and close positions before creating any successor session. Never delete the database or nonce file to bypass a stop.

## Setup and operation

Run all tests: `python -m unittest discover -s tests -v`. First exercise the v0.9 validation gateway and real authenticated read-only connectivity in `EXCHANGE_GATEWAY_V09.md`. Complete actual prospective evidence and independently review it before arming. Do not generate fake evidence or override the evaluation gate. Verify Kraken availability, account eligibility, current fees, precision and minimums; real orders risk loss. A $100 cap may be too small for a particular market/minimum; no order is preferable to increasing capital to bypass a test.

For live execution, create a dedicated OS account (e.g. `autarky-exec` on Linux, a separate Windows local user on Windows). Install a reviewed copy of this commit in an operator-owned, read-only code directory; grant only that account read access and a private writable data directory. Copy the reviewed final SQLite ledger into the execution directory after stopping its writers or using SQLite backup. Research accounts must not have access to the execution environment variables, database, nonce store, code directory or process-control permissions. Restrict access to the operator and executor; this is a deployment requirement, not automatically enforced by the Python package. Do not use the same API key from another process or database.

Set `AUTARKY_KRAKEN_KEY` and `AUTARKY_KRAKEN_SECRET` **only** in the execution process environment using a secret manager or interactive shell; do not pass them on command lines or store them in the repository. For the commands below, use a private execution working directory and `--db data/live.sqlite` if necessary. Bash examples (PowerShell syntax differs for environment variables).

```sh
# Operator-reviewed plan ID from final_eval status; no order is submitted by arm.
python -m autarky_trade.live arm --final-db data/final.sqlite --plan-id PLAN_ID \
  --confirm 'I UNDERSTAND LIVE TRADING CAN LOSE MONEY'
python -m autarky_trade.live status
# Refresh mark independently of candle decisions.
python -m autarky_trade.live observe
# Within 90s of a new closed candle; returns null if there is no valid trade.
python -m autarky_trade.live plan
```

Review the exact returned quantity, limit, side and intent ID. The next command can place a **REAL** order. Only run it if you independently authorize the trade and have completed exchange testing. The intent expires 30 seconds after creation. Do not hardcode confirmation into an agent script.

```sh
AUTARKY_LIVE_ENABLE=YES python -m autarky_trade.live submit INTENT_ID --confirm 'SUBMIT REAL ORDER'
# Never blindly retry submit, including after a timeout.
python -m autarky_trade.live reconcile INTENT_ID
python -m autarky_trade.live status
```

Discard an expired/unwanted unsent proposal with `python -m autarky_trade.live discard INTENT_ID`. Stop with `python -m autarky_trade.live stop` (latches entry stop and discards unsent proposals). If an unexpected order remains open, reconcile first to establish its exact exchange ID, then `AUTARKY_LIVE_ENABLE=YES python -m autarky_trade.live cancel INTENT_ID --confirm 'SUBMIT REAL ORDER'`, and reconcile again. Do not treat cancellation as liquidation. To request an exit even after a stop, use `python -m autarky_trade.live plan --exit-only`, review the result and authorize the sell separately. This requires working market data and a reconciled balance, and may be unavailable if the order is below exchange minimum or another sell already used the candle. Use the exchange UI for emergencies. Keep a private backup of the full journal and exchange statements; reconcile external changes before any successor experiment.

## Verification and next steps

Tests use a simulated HTTP exchange that actually updates balances and returns fills; they test wire fields, partial fills, lost acknowledgement after venue acceptance, recovery, stops, permission refusal, stale plans, discrepancies, duplicate prevention, and a buy/sell round trip. Neither real Kraken authentication nor any actual order was executed during development. Exact accounting can refuse legitimate venue rounding; first conduct an independently reviewed tiny pilot with your actual account. The operator must monitor this pilot; there is no continuous execution service or automatic live agent authorization. A subsequent milestone should add an isolated supervised service, continuous market observation, durable closed-candle scheduling, exchange ledger reconciliation including fee currencies, and explicit bounded session authorization after actual validation. Multi-market live exposure and automatic reinvestment remain disabled.

Kraken documentation: [Add Order](https://docs.kraken.com/api-reference/trading/add-order), [Query Orders Info](https://docs.kraken.com/api-reference/account-data/query-orders-info), [Closed Orders](https://docs.kraken.com/api-reference/account-data/get-closed-orders), [Cancel Order](https://docs.kraken.com/api-reference/trading/cancel-order), [Get API Key Info](https://docs.kraken.com/api-reference/account-data/get-api-key-info), [Query Trades Info](https://docs.kraken.com/api-reference/account-data/query-trades-info). The v0.9 signature tests still apply.
