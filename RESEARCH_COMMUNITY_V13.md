# v0.13 — Continuous development research

Storage: keep the Git checkout in Documents/OneDrive and put databases, archives and logs under `%LOCALAPPDATA%\Autarky`. All new CLI write destinations are explicit (`--root`, `--db`). Existing data is not moved. Ollama stores models separately; Git and OneDrive may sync source; requests to Kraken fetch public data; the configured Ollama endpoint is loopback. Do not put credentials or trading databases in Git. The coordinator is a foreground process, not a Windows service.

## Windows quick start

```powershell
cd "C:\Users\danie\OneDrive\Documents\autarky-agent"
git pull --ff-only
python -m unittest discover -s tests -v
$homeDir = "$env:LOCALAPPDATA\Autarky"
$cutoff = [DateTimeOffset]::Parse('2026-09-30T23:00:00Z').ToUnixTimeSeconds()
```

The original quarterly CSV is still usable. To download the complete Kraken archive, use the following **explicit** command. The archive comprises five ~2 GB parts plus an assembled copy, so allow at least 25 GB free space. It may use substantial bandwidth. The downloader verifies every part against Kraken's published checksum list and the assembled ZIP against the pinned September 2026 checksum. A changed upstream release fails closed. Existing verified parts are reused; partial downloads are retained but restarted, not resumed byte-for-byte. No automatic deletion. Extract only exact BTC/ETH hourly filenames and record SHA-256, first/last timestamps, gaps and contiguous suffix; do not fill absent candles.

```powershell
python -m autarky_trade.history_data --root "$homeDir\history" download --pair XBTUSD
```

For a ZIP you already own, use `extract --archive PATH --pair XBTUSD`. Such an import is not claimed to have an official checksum. The lab uses up to 100,000 contiguous hours ending at the cutoff; if gaps exist it uses only the latest contiguous suffix, so inspect the report. See https://support.kraken.com/articles/360047124832-downloadable-historical-ohlcvt-open-high-low-close-volume-trades-data for source and release information.

Compare frozen Alpha trials 1, 2, 3 on multi-year history:

```powershell
$review = python -m autarky_trade.research_review --memory "$homeDir\research-memory.sqlite" --db "$homeDir\long-review.sqlite" --csv "$homeDir\history\btc-hourly-full.csv" --pair XBTUSD --cutoff $cutoff --trials 1 2 3 | ConvertFrom-Json
$review.results | Select-Object @{n='Name';e={$_.policy.name}},mean_net,mean_stress,mean_passive,positive_windows,fills,worst_drawdown | Format-Table -AutoSize
$review | Select-Object bars,first,last,folds,development_only
```

The control/variant identities are checked against recorded trials and deduplicated. Renaming labels does not change their executable identity. All historical results are development only. Longer folds reduce boundary-liquidation effects; not a prospective holdout. The review report is in the normal lab schema so an operator can use `lab_paper init --source ... --experiment ... --policy ...` for a separately frozen paper trial, with no automatic promotion.

Run a sequential research round (one session per agent):

```powershell
python -m autarky_trade.coordinator --db "$homeDir\research-memory.sqlite" --csv "$homeDir\history\btc-hourly-full.csv" --pair XBTUSD --cutoff $cutoff --model qwen3:4b --budget 3 --rounds 1
```

For a smoke test, substitute the existing `btc-hourly.csv`. `--rounds 0 --pause 300` keeps a foreground process running until Ctrl+C. Each agent has a matched-comparison budget per round; it does not poll Kraken or automatically replace its dataset. Live paper trading remains separate. The shared memory file has an OS-held sidecar lock; current manual research CLI and coordinator share it. Do not mix with older running research CLI versions. On restart, unfinished sessions/trials are marked interrupted, preserving completed trials; previously reserved unfinished comparisons are not repeated automatically. Bounded runs stop on errors; continuous runs log failures and back off. No automatic model fallback. Do not run multiple coordinators on different DBs if sharing a small GPU.

Agent choice: no-trade controls get entry-only revisions that increase measured joint coverage (not holding/cooldown changes), and unproductive directions lose ranking priority. If no single-rule change increases coverage, that control has no offered revisions. Shared ridge experiments remain available. The champion is the best active measured seed-family policy on the same dataset/folds ranked by net then stressed return; exploration may intentionally use a worse control. Prior completed variants are retained across prompt history rollover. This is descriptive, not a promotion criterion. Research is still a constrained menu, not unrestricted scientific discovery.

## Limitations

Full Kraken download and Windows lock behavior must be validated on the operator machine; synthetic/local tests verify archive parsing, checksum failure, recovery and scheduling. The coordinator runs one fixed data prefix per invocation; no automatic real-time refresh, no shared-treasury promotion or real orders. Repeated development runs can overfit; compare frozen candidates on additional regimes and require prospective evidence. The existing original community and v0.11 forward paper runner remain independent. No profitability claim.
