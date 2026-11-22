# Operating Autarky

Commands assume Python 3.11 or newer and a shell in the repository root. Put `--db` before the subcommand.

## Local checks

```sh
python -m unittest discover -s tests -v
python -m autarky_trade.demo
```

For an existing database:

```sh
python -m autarky_trade.community --db data/btc.sqlite doctor
python -m autarky_trade.community --db data/btc.sqlite status
python -m autarky_trade.community --db data/btc.sqlite history
```

`doctor` reads the database without contacting a provider. It flags missing initialization, stale candles, and a latched stop. It doesn't certify Ollama connectivity, disk health, or ongoing process supervision. A newly initialized database will report that no market cycles have been observed.

## Research cycle

Audit your data first (see the README), then initialize a database:

```sh
python -m autarky_trade.community --db data/btc.sqlite init --pair XBTUSD --interval 60
python -m autarky_trade.community --db data/btc.sqlite research --source csv --csv history/btc-clean.csv
```

Repeating the experiment without enough new observations returns `waiting for new unseen data`. This is intentional.

## Separate worker processes

Initialize the database and open four terminals:

```sh
# Coordinator:
python -m autarky_trade.jobs --db data/btc.sqlite coordinate --loop --poll 300

# Run each worker in a separate terminal:
python -m autarky_trade.jobs --db data/btc.sqlite worker --agent alpha --model qwen3:4b --loop
python -m autarky_trade.jobs --db data/btc.sqlite worker --agent beta --model qwen3:4b --loop
python -m autarky_trade.jobs --db data/btc.sqlite worker --agent gamma --model qwen3:4b --loop
```

Omit `--model` to use only deterministic baselines. Start one worker at a time if Ollama is short on VRAM. Use `python -m autarky_trade.jobs --db data/btc.sqlite status` to inspect leases and retries. Stop terminals with Ctrl+C. State persists in SQLite.

Do not run the single-process research command alongside an unfinished queued research batch.

## Read-only dashboard

```sh
python -m autarky_trade.dashboard --db data/btc.sqlite --port 8765
```

Open `http://127.0.0.1:8765`. It shows observed simulated equity, agents, recent research events and queued jobs. Only the local browser can reach it; there are no HTTP write operations.

## Simulated account loop

```sh
python -m autarky_trade.community --db data/btc.sqlite run --paper-only
```

The loop updates simulated account state when new complete candles appear. Without an accepted candidate, no new position is opened. To halt:

```sh
python -m autarky_trade.community --db data/btc.sqlite stop
```

Keep the loop active until it confirms that existing simulated positions were flattened. A stop remains latched in that database.

## Troubleshooting

| Message | What to check |
| --- | --- |
| Missing or irregular candle | Run the CSV audit and extract an uninterrupted segment |
| Need at least 600 bars | Collect more completed observations |
| Waiting for new unseen data | Existing holdout was already consumed |
| Batch already open | Check the job status and lease expiry |
| Lease lost | Another worker attempt owns the job; the stale result is rejected |
| Model unavailable; baselines used | Check Ollama locally if you require model proposals |
| Stopped | Operator or risk stop is latched; start a separate experiment |

## Backups and monitoring

SQLite uses write-ahead logging. Use its backup API rather than copying a live database file:

```python
import sqlite3

with sqlite3.connect("data/btc.sqlite") as source:
    with sqlite3.connect("data/btc-backup.sqlite") as target:
        source.backup(target)
```

Backups belong outside the repository. Monitor process exits, last completed candle, job failures, disk space, and logged provider errors. There is no built-in paging or uptime guarantee.

Consult the [release checklist](release-checklist.md) and [security notes](../SECURITY.md) before making a public release.
