"""Read-only operator page for one local Autarky research database.

The server binds to 127.0.0.1. It does not offer order, research, or stop
operations through HTTP.
"""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3

from . import community


PAGE = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Autarky | Research monitor</title>
<style>
:root{color-scheme:dark;--bg:#101419;--panel:#1b222a;--edge:#34404c;--text:#e8edf2;--muted:#9caab7;--cyan:#74d0c7;--amber:#e5b66f;--red:#f18e88}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:1180px;margin:auto;padding:32px 22px 72px}
header{display:flex;justify-content:space-between;gap:22px;align-items:center;margin-bottom:28px;flex-wrap:wrap}
h1{font-size:28px;font-weight:650;letter-spacing:-.8px;margin:0}
h2{font-size:16px;font-weight:650;margin:0 0 14px}
p{margin:4px 0;color:var(--muted)}
small,.muted{color:var(--muted)}.eyebrow{letter-spacing:1.5px;text-transform:uppercase;font-size:11px;color:var(--cyan);font-weight:700}
.status{border:1px solid var(--edge);border-radius:999px;padding:7px 13px;font-size:13px;color:var(--muted)}
.status.live{color:var(--cyan);border-color:#376a65}.status.stopped{color:var(--red);border-color:#774641}
.stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:0 0 16px}
.panel,.metric{background:var(--panel);border:1px solid var(--edge);border-radius:12px;padding:18px}
.metric label{display:block;color:var(--muted);font-size:12px;margin-bottom:7px}
.metric strong{display:block;font-size:25px;font-weight:650;letter-spacing:-.5px;font-variant-numeric:tabular-nums}
.rows{display:grid;grid-template-columns:minmax(0,2fr) minmax(280px,1fr);gap:16px}
section{min-width:0}.plot{width:100%;height:205px;display:block;margin:10px 0}
#chart-line{stroke:var(--cyan);stroke-width:2.5;fill:none;stroke-linecap:round;stroke-linejoin:round}
#chart-fill{fill:var(--cyan);fill-opacity:.07;stroke:none}
#agents{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:9px}
.agent{border:1px solid var(--edge);border-radius:10px;padding:12px;min-width:0}
.agent strong{display:block;font-size:16px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.agent .subtitle{font-size:12px;color:var(--muted)}
.agent .amount{font-variant-numeric:tabular-nums;font-size:20px;margin-top:8px}
.agent .small{font-size:12px;color:var(--muted);margin-top:2px}
.activity{margin-top:16px}
.list{list-style:none;margin:0;padding:0;max-height:330px;overflow:auto}
.list li{display:grid;grid-template-columns:105px 1fr;gap:12px;border-top:1px solid var(--edge);padding:10px 0;font-size:13px}
.list li:first-child{border-top:0}.list time{color:var(--muted);font-size:12px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;color:var(--muted);font-weight:500}
th,td{padding:10px 6px;border-top:1px solid var(--edge)}
td{font-variant-numeric:tabular-nums}
.note{font-size:12px;margin-top:12px}
@media(max-width:900px){.stats{grid-template-columns:repeat(2,1fr)}.rows{grid-template-columns:1fr}}
@media(max-width:540px){main{padding:20px 12px 56px}.stats{gap:8px}#agents{grid-template-columns:1fr}.metric strong{font-size:21px}.list li{grid-template-columns:80px 1fr}}
</style>
</head>
<body>
<main>
<header>
<div><div class="eyebrow">Autarky / local research</div><h1>Research monitor</h1>
<p>Account values are simulated. This page cannot place orders or change experiments.</p></div>
<div><div id="status" class="status">Connecting...</div><small id="refresh-time">No update yet</small></div>
</header>
<div class="stats">
<div class="metric"><label>Simulated equity</label><strong id="equity">—</strong></div>
<div class="metric"><label>Net P&amp;L</label><strong id="pnl">—</strong></div>
<div class="metric"><label>Reserve</label><strong id="reserve">—</strong></div>
<div class="metric"><label>Latest candle (UTC)</label><strong id="candle" style="font-size:16px">—</strong></div>
</div>
<div class="rows">
<section class="panel">
<h2>Equity history</h2>
<svg class="plot" viewBox="0 0 900 205" preserveAspectRatio="none" aria-label="Simulated equity history">
<path id="chart-fill"></path><path id="chart-line"></path>
</svg>
<p id="chart-caption" class="note">Loading recorded market cycles...</p>
</section>
<section class="panel">
<h2>Agents</h2>
<div id="agents"></div>
<p class="note">Values reflect the latest observed quote, not live exchange balances.</p>
</section>
</div>
<div class="rows activity">
<section class="panel"><h2>Recent events</h2><ul class="list" id="events"></ul></section>
<section class="panel"><h2>Research jobs</h2>
<div style="overflow-x:auto"><table><thead><tr><th>Batch</th><th>Agent</th><th>State</th><th>Attempts</th></tr></thead>
<tbody id="jobs"></tbody></table></div><p class="note" id="jobs-note"></p>
</section>
</div>
</main>
<script>
'use strict';
const $ = (id) => document.getElementById(id);
const fmtMoney = (value) => Number.isFinite(Number(value)) ?
    Number(value).toLocaleString('en-US',{style:'currency',currency:'USD',maximumFractionDigits:2}) : '—';
const fmtTime = (seconds) => seconds ?
    new Date(Number(seconds)*1000).toLocaleString('en-US',{timeZone:'UTC',hour12:false}) : 'Not recorded';
function clear(node){ while(node.firstChild) node.removeChild(node.firstChild); }
function node(tag, content, className){
    const element=document.createElement(tag);
    if(content!==undefined) element.textContent=String(content);
    if(className) element.className=className;
    return element;
}
function renderChart(rows){
    const line=$('chart-line'),area=$('chart-fill');
    if(!rows.length){line.setAttribute('d','');area.setAttribute('d','');
        $('chart-caption').textContent='No market cycles recorded yet.';return;}
    const values=rows.map((row)=>Number(row.equity));
    const min=Math.min(...values), max=Math.max(...values);
    const padding=Math.max(1,(max-min)*0.1),lo=min-padding,hi=max+padding;
    const coords=values.map((value,index)=>[
        12+(rows.length===1?0:index/(rows.length-1))*876,
        180-((value-lo)/(hi-lo))*155
    ]);
    const d=coords.map((p,i)=>(i?'L':'M')+p[0].toFixed(1)+' '+p[1].toFixed(1)).join(' ');
    line.setAttribute('d',d);area.setAttribute('d',d+' L '+coords[coords.length-1][0].toFixed(1)+' 195 L '+coords[0][0].toFixed(1)+' 195 Z');
    $('chart-caption').textContent=rows.length+' recorded candles · '+fmtMoney(min)+' to '+fmtMoney(max);
}
function renderAgents(agents){
    const root=$('agents');clear(root);
    if(!agents.length){root.appendChild(node('p','No agent state recorded.'));return;}
    for(const agent of agents){
        const box=node('article',undefined,'agent');
        box.appendChild(node('strong',agent.id));
        box.appendChild(node('div',agent.family+' strategy','subtitle'));
        box.appendChild(node('div',fmtMoney(agent.equity),'amount'));
        box.appendChild(node('div','Fills: '+agent.fills+' · Drawdown: '+(100*agent.drawdown).toFixed(1)+'%','small'));
        box.appendChild(node('div',agent.stopped?'Stopped':agent.active?'Active':'No active strategy','small'));
        root.appendChild(box);
    }
}
function renderEvents(events){
    const root=$('events');clear(root);
    if(!events.length){root.appendChild(node('li','No events yet.'));return;}
    for(const event of events){
        const li=node('li');
        li.appendChild(node('time',fmtTime(event.ts)));
        let detail=event.kind;
        if(event.detail && typeof event.detail==='object'){
            const short=JSON.stringify(event.detail);
            detail+=' · '+(short.length>180?short.slice(0,180)+'…':short);
        }
        li.appendChild(node('span',detail));
        root.appendChild(li);
    }
}
function renderJobs(jobs){
    const root=$('jobs');clear(root);
    $('jobs-note').textContent=jobs.length?'Most recent jobs only.':'No batches scheduled.';
    for(const job of jobs){
        const row=node('tr');
        for(const value of [job.batch_id,job.agent,job.state,job.attempts])row.appendChild(node('td',value));
        root.appendChild(row);
    }
}
async function refresh(){
    try{
        const response=await fetch('/api/overview',{cache:'no-store'});
        if(!response.ok)throw Error('HTTP '+response.status);
        const data=await response.json();
        $('equity').textContent=fmtMoney(data.account.equity);
        $('pnl').textContent=fmtMoney(data.account.net_pnl);
        $('reserve').textContent=fmtMoney(data.account.reserve);
        $('candle').textContent=fmtTime(data.account.last_ts);
        $('status').textContent=data.account.stopped?'Stopped':'Research database available';
        $('status').className='status '+(data.account.stopped?'stopped':'live');
        $('refresh-time').textContent='Updated '+new Date().toLocaleTimeString();
        renderChart(data.ticks);
        renderAgents(data.account.agents);
        renderEvents(data.events);
        renderJobs(data.jobs);
    }catch(error){
        $('status').textContent='Cannot read database';
        $('status').className='status stopped';
        $('refresh-time').textContent=String(error.message||error);
    }
}
refresh();
setInterval(refresh,15000);
</script>
</body>
</html>'''


def snapshot(path, history=200):
    """Read the latest account, research, and job records without modifying them."""
    database = Path(path)
    if not database.is_file():
        raise FileNotFoundError(f'Database does not exist: {database}')

    account = community.status(path)
    with community.db(path) as connection:
        ticks = [
            dict(row) for row in connection.execute(
                'SELECT ts, equity FROM ticks ORDER BY ts DESC LIMIT ?', (history,)
            )
        ]
        events = []
        for row in connection.execute(
            'SELECT ts, kind, detail FROM events ORDER BY id DESC LIMIT 50'
        ):
            entry = dict(row)
            entry['detail'] = json.loads(entry['detail'])
            events.append(entry)

        exists = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='jobs'"
        ).fetchone()
        jobs = (
            [dict(row) for row in connection.execute(
                'SELECT batch_id, agent, state, attempts '
                'FROM jobs ORDER BY batch_id DESC, agent LIMIT 24'
            )]
            if exists else []
        )

    ticks.reverse()
    return {'account': account, 'ticks': ticks, 'events': events, 'jobs': jobs}


def make_server(database, port=8765):
    """Create a local-only HTTP server. Pass port=0 for an ephemeral test port."""
    database = Path(database)
    if not database.is_file():
        raise FileNotFoundError(f'Initialize a database first: {database}')
    if not 0 <= port <= 65535:
        raise ValueError('Invalid dashboard port')

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format_string, *args):
            # The page auto-refreshes; don't log one line per request.
            return

        def do_GET(self):
            if self.path == '/':
                content = PAGE.encode('utf-8')
                content_type = 'text/html; charset=utf-8'
            elif self.path == '/api/overview':
                try:
                    content = json.dumps(snapshot(database), allow_nan=False).encode('utf-8')
                except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
                    self._respond(500, 'text/plain; charset=utf-8', str(exc).encode('utf-8'))
                    return
                content_type = 'application/json; charset=utf-8'
            else:
                self._respond(404, 'text/plain; charset=utf-8', b'Not found')
                return
            self._respond(200, content_type, content)

        def _respond(self, status, content_type, content):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(content)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.end_headers()
            self.wfile.write(content)

    return ThreadingHTTPServer(('127.0.0.1', port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default='data/community.sqlite')
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    server = make_server(args.db, args.port)
    print(f'Autarky research monitor: http://127.0.0.1:{server.server_port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('Stopping monitor.', flush=True)
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
