# Contributing

Autarky is small enough that most changes should be understandable in one review. Keep the offline research path working without API keys, and prefer explicit, testable behavior over extra orchestration layers.

Run before committing:

```sh
python -m compileall -q autarky_trade tests
python -m unittest discover -s tests -v
python -m autarky_trade.demo
```

Bug fixes should include a regression test. If you change a cost assumption, research split, or strategy rule, explain how it changes the interpretation of existing results. Keep failed experiments in the ledger.

Documentation should state what the program does and how to verify it. Avoid claiming that an experiment demonstrates profitability or autonomous operation unless there is evidence.

Autarky is licensed under the [MIT License](LICENSE). Contributions submitted for inclusion should be compatible with that license. Preserve required notices when reusing third-party code.
