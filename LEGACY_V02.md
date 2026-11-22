# Older single-agent interface (v0.2)

The original research commands are retained for compatibility with saved single-agent experiments. New work should generally use [the main workflow](README.md) and the multi-agent coordinator.

```sh
python -m autarky_trade research
python -m autarky_trade research --source csv --csv history/btc-clean.csv --pair XBTUSD --interval 60 --out data/research.json
python -m autarky_trade status --db data/paper.sqlite
```

The first command uses seeded synthetic data and writes a local JSON report. The CSV command expects completed, uninterrupted OHLCV candles. The old interface's report format and SQLite database are **separate** from the community/coordinator database; don't reuse one as the other.

A historical report may mark a strategy as eligible for simulated forward evaluation. That eligibility flag is an exploratory research decision, not an authorization to place exchange orders.

This compatibility path has its own model-request behavior and hasn't been redesigned around the multi-worker job queue. The current documentation and regression tests focus primarily on `autarky_trade.community`, `autarky_trade.jobs`, and the standalone offline demonstration. Prefer those for new experiments.

Kraken publishes downloadable history [here](https://support.kraken.com/articles/360047543791-downloadable-historical-ohlcvt-open-high-low-close-volume-trades-data).
