"""Read-only loopback dashboard for the paper community."""
from __future__ import annotations
import argparse
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from . import community as m, archive


def snapshot(path):
    state=m.status(path)
    with m.db(path) as c:
        trials=[]
        for r in c.execute('SELECT t.agent,t.report,t.round_id FROM trials t JOIN (SELECT agent,max(round_id) AS rid FROM trials GROUP BY agent) x ON x.agent=t.agent AND x.rid=t.round_id ORDER BY t.agent'):
            report=json.loads(r['report']); trials.append({'agent':r['agent'],'round':r['round_id'],
                'strategy':report['strategy'],'test':report['test'],'stress':report['stress'],
                'benchmarks':report.get('benchmarks'),'eligible':report['eligible']})
        events=[dict(r) for r in c.execute('SELECT ts,kind,detail FROM events ORDER BY id DESC LIMIT 12')]
    return {'portfolio':state,'archive':archive.info(path),'research':trials,'events':events,'readiness':m.readiness(path)}


def render(data):
    s=data['portfolio']; rows=[]
    for a in s['agents']:
        rows.append('<tr>'+''.join('<td>'+escape(str(v))+'</td>' for v in (a['id'],a['family'],f"${a['equity']:.2f}",f"${a['net_pnl']:.2f}",f"{a['drawdown']:.1%}",a['fills'],(a['active'] or {}).get('name','none'),a['stopped']))+'</tr>')
    def details(value): return escape(json.dumps(value,indent=2))
    return f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><meta http-equiv="refresh" content="30"><title>Autarky | paper lab</title><style>body{{max-width:1000px;margin:40px auto;padding:0 18px;font:16px system-ui;color:#142634;background:#f5f7fa}} h1{{margin-bottom:0}} table{{border-collapse:collapse;width:100%;background:white}} th,td{{text-align:left;padding:10px;border-bottom:1px solid #dce4eb}} pre{{background:white;padding:16px;overflow:auto}} .stats{{display:flex;gap:30px;flex-wrap:wrap}} .note{{color:#526474}} </style><h1>Autarky</h1><p class="note">PAPER ONLY · {escape(s['pair'])} · {s['interval']} minute candles · refreshes every 30 seconds</p><div class="stats"><h2>Equity ${s['equity']:.2f}</h2><h2>P&amp;L ${s['net_pnl']:.2f}</h2><h2>Reserve ${s['reserve']:.2f}</h2></div><p>Portfolio stopped: {s['stopped']}</p><h2>Agents</h2><table><thead><tr><th>Agent</th><th>Family</th><th>Equity</th><th>P&amp;L</th><th>Drawdown</th><th>Fills</th><th>Strategy</th><th>Stopped</th></tr></thead><tbody>{''.join(rows)}</tbody></table><h2>Market archive</h2><pre>{details(data['archive'])}</pre><h2>Latest research &amp; benchmarks</h2><pre>{details(data['research'])}</pre><h2>Readiness review</h2><pre>{details(data['readiness'])}</pre><h2>Recent events</h2><pre>{details(data['events'])}</pre></html>'''


def serve(path, port=8765):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in ('/','/api/status'):
                self.send_error(404); return
            try:
                data=snapshot(path)
                body=(json.dumps(data,allow_nan=False) if self.path=='/api/status' else render(data)).encode()
            except Exception:
                self.send_error(503,'Community unavailable'); return
            self.send_response(200); self.send_header('Content-Type','application/json' if self.path=='/api/status' else 'text/html; charset=utf-8')
            self.send_header('Cache-Control','no-store'); self.send_header('X-Content-Type-Options','nosniff'); self.send_header('Content-Security-Policy',"default-src 'none'; style-src 'unsafe-inline'")
            self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
    with ThreadingHTTPServer(('127.0.0.1',port),Handler) as server:
        print(f'Paper dashboard: http://127.0.0.1:{port}',flush=True); server.serve_forever()


def main():
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--db',default='data/community.sqlite'); p.add_argument('--port',type=int,default=8765); x=p.parse_args(); serve(x.db,x.port)

if __name__=='__main__': main()
