# Security

Autarky is designed for a trusted local machine. It reads public market data, optionally sends bounded requests to an Ollama server running on loopback, and keeps research results in SQLite.

Models propose parameter values, not executable programs. Validation and all simulated account decisions are handled by Python. Job tokens prevent an expired worker from publishing after its lease has been reassigned. The repository assumes one trusted local operator, not an exposed multi-tenant service.

Keep Ollama bound to `127.0.0.1`. Never commit `.env` files, credentials, local databases, private historical datasets, model caches, or identifying log output. `.gitignore` helps prevent accidents but cannot remove a secret from Git history. Inspect **all commits** before publishing and rotate any credential that was previously committed.

This repository has no authenticated exchange interface. Do not add keys or wallet access to the model process.

If you discover a security issue, contact the owner privately or use GitHub's private vulnerability reporting where available. Avoid disclosing exploitable details in public issues.
