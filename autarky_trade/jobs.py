"""Leased jobs for running independent research workers on one machine."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import logging
import sqlite3
import time
import uuid

from . import community
from .core import Bar, kraken, load_csv, validate

LEASE = 600
MAX_ATTEMPTS = 3


def setup(path):
    """Create queue tables without touching existing research state."""
    with community.db(path) as connection:
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS batches (
                id INTEGER PRIMARY KEY,
                created INTEGER NOT NULL,
                source TEXT NOT NULL,
                test_start INTEGER NOT NULL,
                test_end INTEGER NOT NULL UNIQUE,
                hold_index INTEGER NOT NULL,
                bars TEXT NOT NULL,
                state TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS jobs (
                batch_id INTEGER REFERENCES batches(id),
                agent TEXT REFERENCES agents(id),
                state TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                token TEXT,
                lease_until INTEGER,
                report TEXT,
                error TEXT,
                PRIMARY KEY (batch_id, agent)
            );
        """)


def schedule(path, bars, source, now=None):
    """Reserve a held-out period and queue one job per strategy family."""
    if source not in ('csv', 'kraken'):
        raise ValueError('Only real history can be queued')

    config = community.config(path)
    bars = validate(bars, config['interval'] * 60)
    now = int(time.time()) if now is None else now
    interval_seconds = config['interval'] * 60

    if len(bars) < community.MIN_HISTORY or bars[-1].ts + interval_seconds > now:
        raise ValueError('Insufficient or incomplete history')

    setup(path)
    with community.db(path) as connection:
        connection.execute('BEGIN IMMEDIATE')
        treasury = connection.execute('SELECT * FROM treasury').fetchone()
        if treasury['stopped']:
            return {'status': 'stopped'}

        existing = connection.execute(
            "SELECT id FROM batches WHERE state='open'"
        ).fetchone()
        if existing:
            return {'status': 'batch already open'}

        cutoff = next(
            (index for index, bar in enumerate(bars)
             if bar.ts > treasury['last_holdout']),
            len(bars),
        )
        holdout_start = max(int(len(bars) * 0.8), cutoff)
        remaining = len(bars) - holdout_start
        if remaining < community.MIN_TEST:
            return {'status': 'waiting for fresh data', 'new_test_bars': remaining}

        batch_id = connection.execute(
            'INSERT INTO batches(created,source,test_start,test_end,hold_index,bars,state) '
            'VALUES(?,?,?,?,?,?,?)',
            (
                now, source, bars[holdout_start].ts, bars[-1].ts,
                holdout_start, community.encode([asdict(bar) for bar in bars]), 'open',
            ),
        ).lastrowid

        for agent in community.AGENTS:
            connection.execute(
                "INSERT INTO jobs(batch_id,agent,state) VALUES(?,?,'queued')",
                (batch_id, agent),
            )

        # Reserve the holdout now. A crashed batch must not retry on the same test data.
        connection.execute(
            'UPDATE treasury SET last_holdout=?', (bars[-1].ts,)
        )
        community.event(
            connection, 'batch_scheduled', {'batch': batch_id, 'test_end': bars[-1].ts}
        )
        connection.commit()

    return {
        'status': 'scheduled',
        'batch': batch_id,
        'test_start': bars[holdout_start].ts,
        'test_end': bars[-1].ts,
    }


def _expire_exhausted_jobs(connection, now):
    connection.execute(
        "UPDATE jobs SET state='failed', error='lease expired after retry limit' "
        "WHERE state='running' AND lease_until<=? AND attempts>=?",
        (now, MAX_ATTEMPTS),
    )


def claim(path, agent, now=None):
    """Claim a queued job or an expired lease in one SQLite transaction."""
    if agent not in community.AGENTS:
        raise ValueError('Unknown agent')

    now = int(time.time()) if now is None else now
    setup(path)
    with community.db(path) as connection:
        connection.execute('BEGIN IMMEDIATE')
        _expire_exhausted_jobs(connection, now)

        row = connection.execute(
            'SELECT j.*, b.bars, b.hold_index FROM jobs j '
            'JOIN batches b ON b.id=j.batch_id '
            'WHERE b.state=\'open\' AND j.agent=? AND j.attempts<? '
            "AND (j.state='queued' OR (j.state='running' AND j.lease_until<=?)) "
            'ORDER BY j.batch_id LIMIT 1',
            (agent, MAX_ATTEMPTS, now),
        ).fetchone()

        if row is None:
            connection.commit()
            return None

        token = uuid.uuid4().hex
        connection.execute(
            "UPDATE jobs SET state='running', attempts=attempts+1, "
            'token=?, lease_until=? WHERE batch_id=? AND agent=?',
            (token, now + LEASE, row['batch_id'], agent),
        )
        connection.commit()

        return {
            'batch': row['batch_id'],
            'agent': agent,
            'token': token,
            'bars': json.loads(row['bars']),
            'hold': row['hold_index'],
        }


def finish(path, job, report=None, error=None, now=None):
    """Publish only if the job is open and this worker still owns its lease."""
    now = int(time.time()) if now is None else now
    with community.db(path) as connection:
        connection.execute('BEGIN IMMEDIATE')
        row = connection.execute(
            'SELECT j.*, b.state AS batch_state FROM jobs j '
            'JOIN batches b ON b.id=j.batch_id '
            'WHERE batch_id=? AND agent=?',
            (job['batch'], job['agent']),
        ).fetchone()

        if (
            row is None
            or row['batch_state'] != 'open'
            or row['state'] != 'running'
            or row['token'] != job['token']
            or row['lease_until'] <= now
        ):
            return False

        if error is None:
            if not isinstance(report, dict):
                raise ValueError('Successful jobs need a report dictionary')
            state = 'done'
        elif row['attempts'] >= MAX_ATTEMPTS:
            state = 'failed'
        else:
            state = 'queued'

        connection.execute(
            'UPDATE jobs SET state=?, report=?, error=?, token=NULL, lease_until=NULL '
            'WHERE batch_id=? AND agent=?',
            (
                state, community.encode(report) if error is None else None,
                str(error)[:500] if error is not None else None,
                job['batch'], job['agent'],
            ),
        )
        connection.commit()
        return True


def work(path, agent, model=None):
    """Evaluate an assigned family, then report a result or retryable error."""
    job = claim(path, agent)
    if job is None:
        return {'status': 'idle', 'agent': agent}

    try:
        bars = [Bar(**item) for item in job['bars']]
        hold = job['hold']
        with community.db(path) as connection:
            previous = connection.execute(
                'SELECT report FROM trials WHERE agent=? '
                'ORDER BY round_id DESC LIMIT 1',
                (agent,),
            ).fetchone()
        prior = json.loads(previous[0])['candidates'][:3] if previous else []

        # Candidate generation sees training statistics, not the reserved test period.
        strategies = community.candidates(
            community.AGENTS[agent], model, bars[:int(hold * 0.75)], agent, prior
        )
        result = community.assess(bars, strategies, hold)
        accepted = finish(path, job, report=result)

        return {
            'status': 'completed' if accepted else 'lease lost',
            'agent': agent,
            'batch': job['batch'],
        }
    except Exception as exc:
        # Keep a worker failure from terminating the coordinator. The job will
        # be retried while attempts remain, and the error stays in SQLite.
        logging.exception('Research worker %s failed in batch %s', agent, job['batch'])
        finish(path, job, error=exc)
        return {'status': 'worker error', 'agent': agent, 'error': str(exc)}


def finalize(path, now=None):
    """Commit a complete batch, or record its terminal failure."""
    now = int(time.time()) if now is None else now
    setup(path)
    with community.db(path) as connection:
        connection.execute('BEGIN IMMEDIATE')
        _expire_exhausted_jobs(connection, now)
        batch = connection.execute(
            "SELECT * FROM batches WHERE state='open' ORDER BY id LIMIT 1"
        ).fetchone()

        if batch is None:
            connection.commit()
            return {'status': 'no open batch'}

        rows = list(connection.execute(
            'SELECT * FROM jobs WHERE batch_id=? ORDER BY agent', (batch['id'],)
        ))
        if any(row['state'] == 'failed' for row in rows):
            connection.execute(
                "UPDATE batches SET state='failed' WHERE id=?", (batch['id'],)
            )
            community.event(
                connection, 'batch_failed',
                {'batch': batch['id'], 'holdout_consumed': True},
            )
            connection.commit()
            return {'status': 'failed; holdout consumed', 'batch': batch['id']}

        if any(row['state'] != 'done' for row in rows):
            connection.commit()
            return {
                'status': 'waiting for workers',
                'jobs': {row['agent']: row['state'] for row in rows},
            }

        round_id = connection.execute(
            'INSERT INTO rounds(created,source,test_start,test_end,observations) '
            'VALUES(?,?,?,?,?)',
            (
                now, batch['source'], batch['test_start'],
                batch['test_end'], len(json.loads(batch['bars'])),
            ),
        ).lastrowid

        for row in rows:
            report = json.loads(row['report'])
            agent = row['agent']
            connection.execute(
                'INSERT INTO trials VALUES(?,?,?)',
                (round_id, agent, row['report']),
            )
            if report['eligible']:
                current = connection.execute(
                    'SELECT active,pending FROM agents WHERE id=?', (agent,)
                ).fetchone()
                proposal = community.encode(report['strategy'])
                if proposal not in (current['active'], current['pending']):
                    connection.execute(
                        'UPDATE agents SET pending=?, pending_after=? WHERE id=?',
                        (proposal, batch['test_end'], agent),
                    )

        connection.execute(
            "UPDATE batches SET state='done' WHERE id=?", (batch['id'],)
        )
        community.event(
            connection, 'batch_finalized',
            {'batch': batch['id'], 'round': round_id},
        )
        connection.commit()
        return {'status': 'finalized', 'batch': batch['id'], 'round': round_id}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default='data/community.sqlite')
    commands = parser.add_subparsers(dest='cmd', required=True)

    schedule_parser = commands.add_parser('schedule')
    schedule_parser.add_argument('--csv')

    worker_parser = commands.add_parser('worker')
    worker_parser.add_argument('--agent', choices=list(community.AGENTS), required=True)
    worker_parser.add_argument('--model')
    worker_parser.add_argument('--loop', action='store_true')
    worker_parser.add_argument('--poll', type=int, default=15)

    coordinator_parser = commands.add_parser('coordinate')
    coordinator_parser.add_argument('--loop', action='store_true')
    coordinator_parser.add_argument('--poll', type=int, default=60)

    commands.add_parser('status')
    args = parser.parse_args()

    config = community.config(args.db)
    setup(args.db)

    def output(value):
        print(json.dumps(value, indent=2), flush=True)

    if args.cmd == 'status':
        with community.db(args.db) as connection:
            output([
                dict(row) for row in connection.execute(
                    'SELECT batch_id,agent,state,attempts,error FROM jobs '
                    'ORDER BY batch_id DESC,agent LIMIT 60'
                )
            ])
        return

    if args.cmd == 'schedule':
        bars = (
            load_csv(args.csv, config['interval'] * 60)
            if args.csv else kraken(config['pair'], config['interval'])
        )
        output(schedule(args.db, bars, 'csv' if args.csv else 'kraken'))
        return

    if args.poll < 5:
        parser.error('poll must be at least five seconds')

    while True:
        try:
            if args.cmd == 'worker':
                output(work(args.db, args.agent, args.model))
            else:
                output(finalize(args.db))
                if not community.status(args.db)['stopped']:
                    candles = kraken(config['pair'], config['interval'])
                    output(schedule(args.db, candles, 'kraken'))
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            output({'status': 'cycle skipped', 'error': str(exc)})
            if not args.loop:
                raise

        if not args.loop:
            break
        time.sleep(args.poll)


if __name__ == '__main__':
    main()
