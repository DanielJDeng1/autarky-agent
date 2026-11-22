"""Check historical OHLCV files before using them for research.

The live research path requires complete, evenly spaced candles. Kraken's
downloadable archive may have missing intervals, so this command inspects gaps
and can produce a contiguous suffix without silently filling prices.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path

from .core import Bar, validate


def read_rows(path):
    """Read a Kraken-style CSV, preserving source timestamps and five OHLCV fields."""
    bars = []
    with open(path, newline='', encoding='utf-8-sig') as handle:
        for line, row in enumerate(csv.reader(handle), start=1):
            if not row or not any(value.strip() for value in row):
                continue
            if line == 1 and row[0].strip().lower() in ('timestamp', 'time', 'ts'):
                continue
            if len(row) < 6:
                raise ValueError(f'Line {line}: expected six OHLCV columns')
            try:
                bar = Bar(int(row[0].strip()), *(float(value) for value in row[1:6]))
            except (TypeError, ValueError) as exc:
                raise ValueError(f'Line {line}: invalid timestamp or numeric field') from exc
            prices = (bar.open, bar.high, bar.low, bar.close)
            if (bar.ts <= 0 or not all(math.isfinite(value) and value > 0 for value in prices)
                    or not math.isfinite(bar.volume) or bar.volume < 0
                    or bar.low > min(bar.open, bar.close)
                    or bar.high < max(bar.open, bar.close)):
                raise ValueError(f'Line {line}: invalid candle values')
            if bars and bar.ts <= bars[-1].ts:
                raise ValueError(f'Line {line}: timestamps must increase without duplicates')
            bars.append(bar)
    if not bars:
        raise ValueError('The CSV contains no observations')
    return bars


def inspect(path, interval_seconds=3600):
    if type(interval_seconds) is not int or interval_seconds <= 0:
        raise ValueError('Interval must be positive seconds')
    bars = read_rows(path)
    gaps = []
    missing = 0
    suffix = 1
    for previous, current in zip(bars, bars[1:]):
        difference = current.ts - previous.ts
        if difference % interval_seconds:
            raise ValueError(
                f'Irregular timestamp difference between {previous.ts} and {current.ts}'
            )
        if difference > interval_seconds:
            count = difference // interval_seconds - 1
            missing += count
            gaps.append({'after_ts': previous.ts, 'before_ts': current.ts, 'missing': count})
            suffix = 1
        else:
            suffix += 1

    return {
        'file': str(path),
        'interval_seconds': interval_seconds,
        'observations': len(bars),
        'first_ts': bars[0].ts,
        'last_ts': bars[-1].ts,
        'gap_count': len(gaps),
        'missing_intervals': missing,
        'contiguous_suffix': suffix,
        'sample_gaps': gaps[:10],
    }


def extract_suffix(source, destination, interval_seconds=3600):
    """Write a validated OHLCV suffix; never overwrite an existing file."""
    source_path = Path(source)
    destination_path = Path(destination)
    if source_path.resolve() == destination_path.resolve():
        raise ValueError('Choose a different output path')
    if destination_path.exists():
        raise FileExistsError(f'Output already exists: {destination_path}')
    report = inspect(source, interval_seconds)
    if report['contiguous_suffix'] < 220:
        raise ValueError('Contiguous suffix has fewer than 220 bars')
    bars = read_rows(source)[-report['contiguous_suffix']:]
    validate(bars, interval_seconds)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    with destination_path.open('x', newline='', encoding='utf-8') as handle:
        writer = csv.writer(handle)
        writer.writerow(('timestamp', 'open', 'high', 'low', 'close', 'volume'))
        for bar in bars:
            writer.writerow((bar.ts, bar.open, bar.high, bar.low, bar.close, bar.volume))
    return {**report, 'output': str(destination_path), 'written': len(bars)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest='command', required=True)
    for command in ('inspect', 'extract-suffix'):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument('source')
        command_parser.add_argument('--interval', type=int, default=60,
                                    choices=(15, 60, 240, 1440),
                                    help='Candle interval in minutes')
        if command == 'extract-suffix':
            command_parser.add_argument('--output', required=True)
    args = parser.parse_args()
    if args.command == 'inspect':
        result = inspect(args.source, args.interval * 60)
    else:
        result = extract_suffix(args.source, args.output, args.interval * 60)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
