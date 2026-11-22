# Release checklist

This is a **public-source release checklist**, not a claim that the system is suitable for real-money trading. The owner must approve changing repository visibility.

## Automated checks

- [ ] Python 3.11 and 3.12 CI is green
- [ ] All unit and regression tests pass from a clean checkout
- [ ] `python -m autarky_trade.demo` runs without external services
- [ ] CSV auditing and suffix extraction reject malformed data and prevent overwrites
- [ ] Model-request failures produce warnings and deterministic baseline results
- [ ] Job leases reject stale workers and failed batches consume held-out data

## Manual checks before publishing

- [ ] Independently reproduce the offline demo
- [ ] Test Ollama and Kraken connectivity when those options are enabled
- [ ] Verify a long-running worker cycle and a restored backup on a different machine
- [ ] Scan all committed Git history, not just current files, for credentials and private information
- [ ] Inspect any screenshots, logs, databases, or local configuration before sharing
- [x] Select a license and add its full text: MIT License (LICENSE)
- [ ] Confirm the README matches actual observed behavior
- [ ] Obtain explicit owner approval before making the repository public

Unverified items should remain unchecked. Unit tests and reproducible demos are necessary, but they don't establish the behavior of the system under continuous operation or live market conditions.

There is no authenticated trading API, independent execution risk service, fill reconciliation, or production monitoring in this repository. Public visibility should not be mistaken for authorization to trade.
