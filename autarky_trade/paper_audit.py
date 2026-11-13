"""Append-only simulated fill/quote journal; no exchange credentials or execution."""
import json


def setup(c):
    # Use individual statements: executescript would implicitly commit an active transaction.
    c.execute('''CREATE TABLE IF NOT EXISTS paper_quotes(id INTEGER PRIMARY KEY,
        candle INTEGER NOT NULL, observed INTEGER NOT NULL, bid REAL NOT NULL, ask REAL NOT NULL,
        UNIQUE(candle,observed,bid,ask))''')
    c.execute('''CREATE TABLE IF NOT EXISTS paper_fills(id INTEGER PRIMARY KEY,
        quote_id INTEGER NOT NULL REFERENCES paper_quotes(id), agent TEXT NOT NULL REFERENCES agents(id),
        sequence INTEGER NOT NULL, side TEXT NOT NULL, reason TEXT NOT NULL,
        quantity REAL NOT NULL, price REAL NOT NULL, fee REAL NOT NULL,
        cash_before REAL NOT NULL, cash_after REAL NOT NULL, units_before REAL NOT NULL, units_after REAL NOT NULL,
        UNIQUE(agent,sequence))''')
    c.execute('''CREATE TABLE IF NOT EXISTS paper_journal_baseline(agent TEXT PRIMARY KEY REFERENCES agents(id),
        sequence INTEGER NOT NULL, account TEXT NOT NULL, funding REAL NOT NULL)''')
    for r in c.execute('SELECT id,account,funding FROM agents').fetchall():
        c.execute('INSERT OR IGNORE INTO paper_journal_baseline VALUES(?,?,?,?)',
                  (r['id'],json.loads(r['account'])['trades'],r['account'],r['funding']))


def record_quote(c, ts, observed, bid, ask):
    """Reject a quote observed before the last accepted one, including same-candle races."""
    last=c.execute('SELECT observed FROM paper_quotes ORDER BY id DESC LIMIT 1').fetchone()
    if last and observed<last[0]: return None
    c.execute('INSERT OR IGNORE INTO paper_quotes(candle,observed,bid,ask) VALUES(?,?,?,?)',(ts,observed,bid,ask))
    return c.execute('SELECT id FROM paper_quotes WHERE candle=? AND observed=? AND bid=? AND ask=?',(ts,observed,bid,ask)).fetchone()[0]


def fill(c, quote_id, agent, account, buy, bid, ask, costs, reason):
    """Record the actual simulated fill atomically with its account mutation."""
    before_cash, before_units, before_fees, before_trades = account.cash, account.units, account.fees, account.trades
    account.fill(buy,bid,ask,costs)
    if account.trades==before_trades: return
    quantity=abs(account.units-before_units)
    fee=account.fees-before_fees
    price=(before_cash-account.cash-fee)/quantity if buy else (account.cash-before_cash+fee)/quantity
    c.execute('''INSERT INTO paper_fills(quote_id,agent,sequence,side,reason,quantity,price,fee,
        cash_before,cash_after,units_before,units_after) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
        (quote_id,agent,account.trades,'buy' if buy else 'sell',reason,quantity,price,fee,
         before_cash,account.cash,before_units,account.units))


def report(path, limit=30):
    from .community import db
    if not 1<=limit<=1000: raise ValueError('Limit must be 1..1000')
    with db(path) as c:
        c.execute('BEGIN IMMEDIATE'); setup(c)
        rows=[]
        for r in c.execute('SELECT a.id,a.account,a.funding,b.account AS baseline,b.funding AS baseline_funding,b.sequence FROM agents a JOIN paper_journal_baseline b ON a.id=b.agent ORDER BY a.id').fetchall():
            account=json.loads(r['account']); baseline=json.loads(r['baseline'])
            count, cash_delta, units_delta, fees=c.execute('SELECT count(*),coalesce(sum(cash_after-cash_before),0),coalesce(sum(units_after-units_before),0),coalesce(sum(fee),0) FROM paper_fills WHERE agent=?',(r['id'],)).fetchone()
            cash_expected=baseline['cash']+cash_delta+r['funding']-r['baseline_funding']
            units_expected=baseline['units']+units_delta
            cash_error=account['cash']-cash_expected; units_error=account['units']-units_expected
            fee_error=account['fees']-baseline['fees']-fees
            rows.append({'agent':r['id'],'legacy_fills_before_journal':r['sequence'],'journaled_fills':count,
                         'expected_new_fills':account['trades']-r['sequence'],'cash_error':cash_error,'units_error':units_error,'fee_error':fee_error,
                         'reconciled':count==account['trades']-r['sequence'] and abs(cash_error)<1e-6 and abs(units_error)<1e-10 and abs(fee_error)<1e-6})
        fills=[dict(r) for r in c.execute('SELECT f.*,q.candle,q.observed,q.bid,q.ask FROM paper_fills f JOIN paper_quotes q ON f.quote_id=q.id ORDER BY f.id DESC LIMIT ?',(limit,))]
        last=c.execute('SELECT * FROM paper_quotes ORDER BY id DESC LIMIT 1').fetchone(); c.commit()
    return {'mode':'PAPER ONLY','agents':rows,'latest_quote':dict(last) if last else None,'recent_fills':fills,
            'note':'Journal starts at upgrade; legacy fills are not reconstructed. Funding transfers are not profit.'}
