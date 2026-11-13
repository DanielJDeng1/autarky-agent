# v0.9 — Kraken exchange gateway (validation only)

## Scope and decision

Autarky now has an authenticated Kraken Spot REST adapter, a separate persistent order-intent database and an operator-approved validation workflow. **No actual orders can be placed, cancelled, amended or withdrawn by this release.** `AddOrder` always uses `validate=true`; Kraken documents that such requests do not enter the matching engine. An exchange validation is not a fill, a sandbox fill simulation, a trading authorization, or evidence of a profitable strategy. Paper balances are never copied into an exchange ledger.

The gateway accepts only BTC/USD (`XBTUSD`) and ETH/USD (`ETHUSD`) spot limit orders, no margin, no leverage, no shorts. A validation intent is limited to $25 of order notional, with a 1% quote-currency fee buffer for buys. These are conservative development constants, not an account-specific fee estimate or risk model for real trading. It refuses prices more than 2% away from the relevant current quote, spreads above 2%, and unsupported exchange lot/tick increments or minimums. It fetches actual pair metadata rather than hardcoding current minimum sizes. Very small orders may be impossible at a given market price; do not bypass the cap. The gateway requires an unchanged account baseline and no open orders, credit or held funds. It checks balances before the validation and again afterward. It does not estimate attainable execution, queue priority, liquidity, latency or future fees. A current quote is a local REST observation without an exchange timestamp; preflight expires after 60 seconds, and an intent expires after five minutes. These checks are not adequate for real capital by themselves.

## Install and credentials

Run from the repository root with Python 3.11+. No external Python packages or paid LLM needed. Complete the existing paper research workflow in `PORTFOLIO_V08.md` before using paper-fill provenance. An account at Kraken and exchange access appropriate to your jurisdiction/account are needed for authenticated validation. Check current Kraken eligibility, product availability and API terms directly. Do not deposit funds just to test this code; offline tests need none. Read-only connectivity can be tested with a query-only API key. Exchange validation can require create/modify order permission and sufficient funds or asset inventory; it may reject an unfunded account. Do not enable withdrawals, deposits, transfers, margin, Earn or other permissions for this project. Use a dedicated key and, where available, a separate dedicated account/subaccount. Restrict IP addresses where practical, set expiration, and revoke the key after testing. The gateway does not inspect the key's permission set itself. Never share API credentials in chat, source code, GitHub, logs or screenshots. Kraken API-key OTP/2FA is not supported by this adapter; if your key requires it, the call fails. Do not weaken your security to work around this limitation.

Set `AUTARKY_KRAKEN_KEY` and `AUTARKY_KRAKEN_SECRET` in the gateway process environment through your OS secret manager or an interactive shell. The secret is Kraken's base64-encoded private API key. Example in PowerShell (prompts, not literal key values in shell history):

```powershell
$env:AUTARKY_KRAKEN_KEY = Read-Host "Kraken API key"
$env:AUTARKY_KRAKEN_SECRET = Read-Host "Kraken API secret" -MaskInput
python -m autarky_trade.gateway init
python -m autarky_trade.gateway account
```

`-MaskInput` needs a recent PowerShell; use a secure OS secret manager if unavailable. Environment variables are readable by the same user/process environment and are not a security sandbox. Start research agents separately without these variables, and never give an LLM access to this terminal. Clear them when finished (`Remove-Item Env:AUTARKY_KRAKEN_KEY,Env:AUTARKY_KRAKEN_SECRET`). For real execution later, use an OS/service identity with a separate credential store, filesystem ACLs, and a restricted intent channel. Current module separation is logical, not an OS-enforced security boundary. No endpoint is exposed to the network.

The gateway uses `data/gateway.sqlite`, a durable nonce store `data/gateway.sqlite.nonce` and a local SQLite serialization lock `data/gateway.sqlite.nonce.lock`. Keep them private on a local disk. Do not use the same key with another nonce store, service or machine; Kraken requires increasing nonces per key. Keep the nonce store across restarts. A key change requires a new gateway database; a dedicated new key must also get its own nonce store. No raw credential is persisted; a hash of the public API key binds the gateway to its key. Account snapshots contain financial information, so protect the database and backups.

## Validation workflow

1. Run `python -m autarky_trade.gateway account` to read `BalanceEx` and `OpenOrders` and establish the baseline. Use an account with no open orders, holds or credit. This action makes no trades. Later snapshots compare against the original baseline (not the previous snapshot), so a deposit, withdrawal, external trade, reward or other change is flagged, even if legitimate. The gateway stops on detected drift. Resolve discrepancies outside the program; intentionally establish a fresh database after review if an account changes. Never erase an unresolved intent or replace the nonce store to avoid recovery.
2. Create a manual validation intent. This example uses an illustrative BTC price and size; replace them with a current exchange-valid price and quantity. It can fail minimums, balance or price-band checks. No actual order or payment is made.

```sh
python -m autarky_trade.gateway propose --reference smoke-001 --pair XBTUSD --side buy --quantity 0.0001 --price 100000
```

3. Alternatively import a previously executed **simulated** fill from the unified portfolio's journal (read-only). Inspect `python -m autarky_trade.portfolio journal --limit 30` first. Supply a quantity no larger than that paper fill and an exchange-valid limit price; the paper position, price, fees and profit are never imported as real money. The source row's hash identifies the reference. This is a manual validation test, not an automatic trading signal, and old paper fills are not live signals.

```sh
python -m autarky_trade.gateway propose-paper --source data/portfolio.sqlite --fill-id 1 --quantity 0.0001 --price 100000
```

4. Review the exact proposed order and copy its UUID. Within five minutes, approve then validate:

```sh
python -m autarky_trade.gateway approve INTENT_UUID
python -m autarky_trade.gateway validate INTENT_UUID
python -m autarky_trade.gateway status
```

A successful validation records `validated` and the exchange's order description, without a Kraken order ID. An unexpected order ID, malformed/ambiguous response, failed post-validation account check or unexplained account movement is a stop condition. An explicit exchange rejection is terminal for that intent. No automatic retry or re-approval. For a legitimate new attempt, use a new reference only after reviewing the prior outcome. Reusing a reference with identical economics returns the original intent; different economics with the same reference is rejected. A database transaction claims an approved intent before any private exchange submission, and only one intent can be in validation at a time. A process interruption after the claim leaves it `validating`; the next attempt refuses to resubmit it. After stopping all gateway processes, run `recover` to classify interrupted intents as unknown and latch the stop. The stop command cancels unsent intents and prevents new validation; it does **not** cancel or flatten exchange positions. In-flight network requests cannot be withdrawn. If a real order or position unexpectedly exists, use the exchange interface and investigate it; this software has no cancellation endpoint. Inspect status and the exchange account before further work.

```sh
python -m autarky_trade.gateway stop
python -m autarky_trade.gateway recover
```

Authenticated calls are signed with HMAC-SHA512 and persisted monotonic nonces; the body actually sent is the same body used for the signature. The transport is pinned to Kraken's HTTPS host, disallows redirects, excludes environment proxies, limits response size and has a per-request timeout. There is no automatic retry. Several private calls are required per validation; avoid rapid repeated tests and follow Kraken's rate limits. An application-level stop is not a defense against arbitrary user-written code using an exchange key; do not hand that key to research agents.

## Read-only exchange reconciliation

`account` records real balances, holds, credit and open-order IDs, and flags drift from the original snapshot. It is not a complete exchange cash-flow ledger and cannot explain whether a change arose from a deposit, fee, trade or other activity. `inspect-order` can read an existing explicitly identified Kraken order (for example, an order you previously created yourself). It records cumulative order status, executed quantity, cost, fees and individual trade IDs and details from `QueryTrades`. It checks that all returned trades belong to the order, and that summed volume/cost/fee agree with Kraken's cumulative order summary; partial fills and cancelled partial orders remain partial, not fictitious full fills. It latches a stop when reported totals disagree or observed order totals regress. Exact equality is deliberately conservative; rounding differences or venue corrections may require manual investigation. It supports up to 200 trade IDs per order and does not reconstruct account-wide holdings or order attribution from these observations. No real money is recorded as paper P&L.

```sh
python -m autarky_trade.gateway inspect-order EXCHANGE_ORDER_ID
```

This is a read-only diagnostic; it does not place or cancel the inspected order. A validation-only intent should not have an exchange order ID to inspect.

## Audit and next development

- Offline tests cover Kraken's published signature vector, nonce persistence and clock rollback, forbidden endpoints and exact validation-only payload, idempotency, concurrent claims, expiry, stop/recovery, precision and minimums, available balances, open-order drift, post-validation changes, paper provenance, and partial/cumulative fill reconciliation. All 114 tests passed locally. No authenticated call or real exchange order was performed during development.
- No actual order submission, exchange sandbox fills, order cancellation, order book replay, reliable exchange timestamps, fee-currency ledger or exchange balance accounting across real transactions exists yet. The gateway is operator-invoked; the unified portfolio does not automatically call it. Existing paper portfolios retain their old risk rules and separate accounts. Validation-only requests are not a full integration test of live fills.
- Before bounded live trading: build a distinct service identity with restricted credentials and intent admission, prove predeclared and forward strategy evidence, authenticate with a dedicated key, use exchange-confirmed market metadata and actual fee schedule, implement durable real-order states and reconciliation (including ambiguous submission via client ID, complete fills, fees, balance movements, cancellations and emergency per-market exits), enforce real aggregate risk and daily loss limits independently of the paper database, and test failure/restart behavior with a mock venue or appropriate exchange sandbox. Real submission must require explicit operator authorization. No deployment should assume profitability merely because validation succeeds.

## Primary sources

- Kraken, Spot REST authentication and published signature example: https://docs.kraken.com/exchange/guides/rest/authentication
- Kraken, Add Order (`validate`, `cl_ord_id`, `deadline`): https://docs.kraken.com/api-reference/trading/add-order
- Kraken, tradable asset pairs (`ordermin`, `costmin`, `tick_size`, `lot_decimals`, status): https://docs.kraken.com/api-reference/market-data/get-tradable-asset-pairs
- Kraken, extended balance and holds: https://docs.kraken.com/api-reference/account-data/get-extended-balance
- Kraken, open orders: https://docs.kraken.com/api-reference/account-data/get-open-orders
- Kraken, query orders/trades: https://docs.kraken.com/api-reference/account-data/query-orders-info and https://docs.kraken.com/api-reference/account-data/query-trades-info
- Kraken, API key permissions: https://docs.kraken.com/exchange/guides/rest/authentication (permissions link in navigation)
