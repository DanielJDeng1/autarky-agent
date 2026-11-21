"""Read-only loopback dashboard for the autonomous research ledger."""
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sqlite3
from .autonomous_research import status


def render(data):
    def text(value): return escape(str(value),quote=True)
    def pct(value): return f'{100*value:+.3f}%' if value is not None else '—'
    rows=[]
    for e in data['experiments']:
        s=e.get('summary') or {}; b=s.get('XBTUSD',{}); h=s.get('ETHUSD',{})
        rows.append('<tr>'+''.join('<td>'+text(v)+'</td>' for v in [e['id'],e['agent'],e['name'],e['status'],pct(b.get('net')),pct(b.get('stress')),pct(h.get('net')),pct(h.get('stress')),pct(b.get('net_delta_mean')),pct(h.get('net_delta_mean')),e.get('reason') or e.get('error') or ''])+'</tr>')
    runs=[]
    for r in data['runs']:
        runs.append('<tr>'+''.join('<td>'+text(v)+'</td>' for v in [r['run_id'],r['status'],r.get('model') or 'deterministic',r.get('completed',0),r['dataset'][:12],r.get('error','')])+'</tr>')
    bases=[]
    for pair,r in (data.get('baseline') or {}).items():
        for name,m in r['summary'].items():
            bases.append('<tr>'+''.join('<td>'+text(v)+'</td>' for v in [pair,name,m['windows'],pct(m['mean_net']),pct(m['mean_stress']),m['fills']])+'</tr>')
    return '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta http-equiv="refresh" content="15"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Autarky Research</title><style>
body{font:15px system-ui,sans-serif;background:#101820;color:#e7edf4;margin:32px auto;padding:0 20px;max-width:1500px}h1{font-size:28px;margin:0 0 8px}h2{font-size:19px;margin-top:28px}p{color:#b4c3d0}table{width:100%;border-collapse:collapse;background:#17232e}td,th{text-align:left;padding:10px;border-bottom:1px solid #30404d;vertical-align:top}th{color:#a9c6dc;font-size:12px}a{color:#89d4f6}.scroll{overflow:auto;border:1px solid #30404d;border-radius:8px}.note{color:#ffd296}td:nth-child(n+5){font-variant-numeric:tabular-nums}</style></head><body><h1>Autarky / Autonomous Research</h1><p class="note">Development only · No paper or live orders · No candidate promotion</p><p>BTC and ETH results use separate chronological windows. Returns are average 30-day account returns; deltas are versus the same-market historical mean. Refreshes every 15 seconds. <a href="/api/status">JSON status</a></p>
<h2>Experiment ledger</h2><div class="scroll"><table><thead><tr><th>ID</th><th>Agent</th><th>Experiment</th><th>Status</th><th>BTC net</th><th>BTC stress</th><th>ETH net</th><th>ETH stress</th><th>BTC Δ mean</th><th>ETH Δ mean</th><th>Rationale / error</th></tr></thead><tbody>'''+(''.join(rows) or '<tr><td colspan="11">No experiments recorded.</td></tr>')+'''</tbody></table></div><h2>Research runs</h2><div class="scroll"><table><thead><tr><th>Run</th><th>Status</th><th>Model</th><th>Completed</th><th>Dataset</th><th>Error</th></tr></thead><tbody>'''+(''.join(runs) or '<tr><td colspan="6">No runs recorded.</td></tr>')+'''</tbody></table></div><h2>Current dataset baseline</h2><div class="scroll"><table><thead><tr><th>Market</th><th>Model</th><th>Windows</th><th>Net</th><th>Stress</th><th>Fills</th></tr></thead><tbody>'''+(''.join(bases) or '<tr><td colspan="6">Baseline not yet completed.</td></tr>')+'''</tbody></table></div></body></html>'''


def serve(path, port=8766):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in ('/','/api/status'):
                self.send_error(404); return
            try:
                data=status(path)
                body=json.dumps(data,allow_nan=False).encode() if self.path=='/api/status' else render(data).encode()
            except (OSError,sqlite3.Error,ValueError) as exc:
                self.send_error(503, 'Research ledger unavailable'); return
            self.send_response(200); self.send_header('Content-Type','application/json' if self.path=='/api/status' else 'text/html; charset=utf-8')
            self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
        def log_message(self, *_): pass
    with ThreadingHTTPServer(('127.0.0.1',port), Handler) as server:
        print(f'Research dashboard: http://127.0.0.1:{port}',flush=True)
        try: server.serve_forever()
        except KeyboardInterrupt: pass
