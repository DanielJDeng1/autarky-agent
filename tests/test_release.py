"""Regressions for model fallback, CSV ingestion and operator diagnostics."""
import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autarky_trade import community
from autarky_trade.core import Bar, BASELINES, Costs, backtest, load_csv, synthetic, validate
from autarky_trade.data_audit import inspect, extract_suffix
from autarky_trade.demo import run as run_demo


class ModelFailureTests(unittest.TestCase):
    def setUp(self):
        self.training = synthetic(600)

    def test_provider_unavailable_still_runs_fixed_baselines(self):
        with patch('autarky_trade.community.request', side_effect=OSError('connection refused')):
            results = community.candidates('trend', 'offline-model', self.training, 'alpha', [])
        self.assertEqual(len(results), 4)

    def test_invalid_model_parameters_are_ignored(self):
        reply = {'message': {'content': '{"strategies":[{"fast":5,"slow":20},'
                                        '{"fast":7,"slow":21},{"fast":200,"slow":300},{}]}'}}
        with patch('autarky_trade.community.request', return_value=reply):
            results = community.candidates('trend', 'mock', self.training, 'alpha', [])
        self.assertEqual(len(results), 5)
        self.assertEqual(sum(s.fast == 7 and s.slow == 21 for s in results), 1)

    def test_malformed_response_falls_back(self):
        with patch('autarky_trade.community.request',
                   return_value={'message': {'content': '{"strategies":"invalid"}'}}):
            results = community.candidates('reversion', 'mock', self.training, 'beta', [])
        self.assertEqual(len(results), 4)


class DataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name) / 'history.csv'

    def save(self, bars):
        with self.source.open('w', newline='') as handle:
            writer = csv.writer(handle)
            writer.writerow(('timestamp', 'open', 'high', 'low', 'close', 'volume', 'trades'))
            for bar in bars:
                writer.writerow((bar.ts, bar.open, bar.high, bar.low, bar.close,
                                 bar.volume, 1))

    def test_audit_and_export_contiguous_suffix(self):
        bars = synthetic(260)
        self.save(bars[:10] + bars[11:])
        report = inspect(self.source)
        self.assertEqual(report['gap_count'], 1)
        self.assertEqual(report['missing_intervals'], 1)
        self.assertEqual(report['contiguous_suffix'], 249)
        output = Path(self.temp.name) / 'clean.csv'
        extracted = extract_suffix(self.source, output)
        self.assertEqual(extracted['written'], 249)
        self.assertEqual(len(load_csv(output)), 249)
        with self.assertRaises(FileExistsError):
            extract_suffix(self.source, output)

    def test_malformed_csv_line_has_context(self):
        self.source.write_text('timestamp,open,high,low,close,volume\n1,2,3\n')
        with self.assertRaisesRegex(ValueError, 'Line 2'):
            inspect(self.source)

    def test_rejects_bad_timestamp_and_window(self):
        bars = synthetic(250)
        bars[1] = Bar(-1, 100, 101, 99, 100, 1)
        with self.assertRaisesRegex(ValueError, 'timestamp'):
            validate(bars, 3600)
        with self.assertRaisesRegex(ValueError, 'window'):
            backtest(synthetic(260), BASELINES[0], Costs(), 200, 180)


class OperatorTests(unittest.TestCase):
    def test_missing_database_diagnostic_does_not_create_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'missing.sqlite'
            report = community.doctor(path)
            self.assertFalse(report['ok'])
            self.assertFalse(path.exists())

    def test_demo_has_no_persistent_holdout(self):
        report = run_demo()
        self.assertEqual(len(report['agents']), 3)
        self.assertFalse(report['persistent_trials'])


if __name__ == '__main__':
    unittest.main()
