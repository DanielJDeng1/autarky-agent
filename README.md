# Autarky

Autarky is a Python research lab for comparing simple strategies on historical market data. It runs three independent research workers, can ask a local language model to suggest parameters, and keeps a record of what each experiment did. SQLite stores the research history and coordinates workers.

The model is a source of hypotheses. It doesn't write executable strategies or control accounts. Python validates proposed lookback windows, runs the backtests, applies the acceptance rules, and records the result. The same fixed baselines are always available when Ollama is offline.

This project simulates trading. It does not connect to an exchange account or send live orders.

## Start here

You need Python 3.11 or newer. The runtime uses only the standard library.

```sh
python -m unittest discover -s tests -v
python -m autarky_trade.demo
```

The demo runs a complete research cycle against seeded synthetic prices in a temporary database. It needs no API key or internet connection and does not add synthetic results to your saved research history. The summary shows each agent's selected parameters, held-out return, stress return, and acceptance result. It's fine if none of the strategies passes.

## Work with market data

Input files use Unix-second timestamps and `timestamp,open,high,low,close,volume` columns. Kraken's extra trade-count column is ignored. Historical archives sometimes skip hours, so audit a file before giving it to the evaluator:

```sh
python -m autarky_trade.data_audit inspect history/btc-hourly.csv --interval 60
```

If a file has gaps, you can extract its latest uninterrupted segment to a different path:

```sh
python -m autarky_trade.data_audit extract-suffix history/btc-hourly.csv --interval 60 --output history/btc-clean.csv
```

No missing prices are invented. The tool refuses to overwrite an existing file. A valid research batch needs at least 600 completed candles, including 120 held-out test candles.

Initialize a database and run one research cycle:

```sh
python -m autarky_trade.community --db data/btc.sqlite init --pair XBTUSD --interval 60
python -m autarky_trade.community --db data/btc.sqlite research --source csv --csv history/btc-clean.csv
python -m autarky_trade.community --db data/btc.sqlite trials
python -m autarky_trade.community --db data/btc.sqlite status
```

The `--db` flag goes **before** the command. Use a separate database for a different asset or candle interval. The public Kraken API can also supply recent data through `research --source kraken`, though a short recent window may not contain enough observations.

## Optional local model

If you have [Ollama](https://ollama.com) installed, you can request additional parameter suggestions:

```sh
ollama pull qwen3:4b
python -m autarky_trade.community --db data/btc.sqlite research --source kraken --model qwen3:4b
```

The model sees training statistics and past training/validation summaries. It does not receive the reserved test interval, credentials, or executable tools. Bad proposals are ignored. If the local model is unavailable, a warning is logged and fixed baselines are evaluated instead.

For sustained research, the coordinator and workers can run as separate terminal processes. See [operations](docs/operations.md).

## Local research monitor

After initializing a database, start the read-only operator page:

```sh
python -m autarky_trade.dashboard --db data/btc.sqlite --port 8765
```

Open `http://127.0.0.1:8765`. The page shows simulated equity history, per-agent status, recent events and worker jobs. It updates every 15 seconds and has no action buttons. The server binds only to loopback and doesn't require internet access.

## What the results mean

Strategies are selected using training and validation data, then evaluated on a chronological holdout and again with higher assumed trading costs. The coordinator reserves a test interval before workers begin, and it never presents that interval as unseen data again if the batch fails.

The results are experimental. Transaction costs are simplified, parameter searches can overfit, and the program does not establish that a strategy would make money in a live market. Those limits are part of the research record, not a reason to hide unsuccessful trials. See [research notes](docs/research.md).

For example, the seeded offline demonstration selected a 12/48-window trend candidate with a test return of about **+0.06%**. Its higher-cost stress return was **-0.69%**, so it was rejected. The reversion and breakout candidates also failed. The demo reports these outcomes instead of forcing an approval.

## Code map

| File | Responsibility |
| --- | --- |
| `core.py` | Candles, strategy rules, fills and historical evaluation |
| `community.py` | Research cycles, agent state, account simulation and diagnostics |
| `jobs.py` | Coordinator, job leases and worker execution |
| `data_audit.py` | Historical gap inspection and clean CSV extraction |
| `demo.py` | Fully offline demonstration |
| `__main__.py` | Older single-agent interface, kept for compatibility |
| `tests/` | Offline and mocked-provider regression tests |

The Python modules live in `autarky_trade/`. Read the [architecture](docs/architecture.md) for the state boundaries, [operations](docs/operations.md) for starting and stopping workers, and the [release checklist](docs/release-checklist.md) before changing repository visibility.

## Development

```sh
python -m compileall -q autarky_trade tests
python -m unittest discover -s tests -v
python -m autarky_trade.demo
```

CI runs the checks on Python 3.11 and 3.12. The repository deliberately excludes local databases, data archives, environment files, and model caches.

## License

Autarky is released under the [MIT License](LICENSE). The license permits use, modification, and redistribution, including commercial use, as long as the copyright and license notice is retained. It comes without warranty.
