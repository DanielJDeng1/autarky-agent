"""Run one complete deterministic research demonstration with no API keys."""
from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from .community import init, research, status
from .core import synthetic


def run():
    with TemporaryDirectory(prefix='autarky-demo-') as directory:
        database = Path(directory) / 'research.sqlite'
        init(database)
        result = research(database, synthetic(), 'synthetic')
        state = status(database)
        return {
            'status': result['status'],
            'agents': {
                name: {
                    'family': report['strategy']['kind'],
                    'lookback': [report['strategy']['fast'], report['strategy']['slow']],
                    'test_return': round(report['test']['return'], 5),
                    'stress_return': round(report['stress']['return'], 5),
                    'eligible': report['eligible'],
                }
                for name, report in result['reports'].items()
            },
            'persistent_trials': state['last_holdout'] != 0,
        }


if __name__ == '__main__':
    print(json.dumps(run(), indent=2))
