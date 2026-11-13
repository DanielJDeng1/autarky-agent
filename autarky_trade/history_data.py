"""Verified Kraken archive acquisition. All writes are under the specified root."""
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import urllib.error
import urllib.request
import zipfile
from .core import Bar, validate

BASE = 'https://assets.kraken.com/marketing/institutions/'
PARTS = [f'Kraken_OHLCVT_Full.zip.part{i:02}' for i in range(5)]
FULL_SHA = 'bc273e9027470b07d790a7fa4df9bb43f0dfc527c20c6ecae48402e123e7b1c7'
CHECKSUMS = 'OHLCVT_Full_PARTS_SHA256SUMS.txt'
PAIRS = {'XBTUSD': 'btc', 'ETHUSD': 'eth'}


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1024*1024), b''):
            h.update(block)
    return h.hexdigest()


def fetch(url, path, expected=None):
    path = Path(path)
    if path.exists() and expected and sha256(path) == expected:
        return
    temp = path.with_name(path.name + '.partial')
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Autarky-history/0.13'})
        with urllib.request.urlopen(req, timeout=120) as response, open(temp, 'wb') as f:
            shutil.copyfileobj(response, f, 1024*1024)
    except urllib.error.HTTPError as exc:
        if exc.code != 403:
            raise
        # Kraken documents curl for these public static archive downloads.
        curl = shutil.which('curl.exe') or shutil.which('curl')
        if not curl:
            raise
        result = subprocess.run([curl, '--fail', '--location', '--retry', '2',
                                 '--connect-timeout', '30', '--output', str(temp), url],
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        if result.returncode:
            raise urllib.error.HTTPError(url, 403, 'Python download refused; curl also failed: ' +
                                         result.stderr[-500:], exc.headers, None) from exc
    if expected and sha256(temp) != expected:
        raise ValueError(f'Checksum mismatch: {path.name}; partial retained')
    os.replace(temp, path)


def checksums(text):
    result = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) != 2:
            continue
        h, name = fields
        name = name.lstrip('*')
        if name in PARTS and len(h) == 64 and all(x in '0123456789abcdefABCDEF' for x in h):
            if name in result and result[name] != h.lower():
                raise ValueError('Conflicting part checksums')
            result[name] = h.lower()
    if set(result) != set(PARTS):
        raise ValueError('Official checksum list does not match pinned archive; review Kraken release')
    return result


def acquire(root):
    root = Path(root); root.mkdir(parents=True, exist_ok=True)
    archive = root / 'Kraken_OHLCVT_Full.zip'
    if archive.exists() and sha256(archive) == FULL_SHA:
        return archive
    manifest = root / CHECKSUMS
    try:
        fetch(BASE + CHECKSUMS, manifest)
    except urllib.error.HTTPError as exc:
        if exc.code not in (403, 404):
            raise
        # The manifest is optional per Kraken. The pinned full hash remains mandatory.
        import sys
        print('Part checksum manifest unavailable; verifying the assembled ZIP against the published SHA-256.', file=sys.stderr)
    hashes = checksums(manifest.read_text(encoding='utf-8')) if manifest.exists() else {}
    for name in PARTS:
        part = root / name
        if not hashes and part.exists():
            continue  # Previously downloaded part; full archive hash still must pass.
        fetch(BASE + name, part, hashes.get(name))
    temp = archive.with_suffix('.partial')
    with open(temp, 'wb') as out:
        for name in PARTS:
            with open(root / name, 'rb') as f:
                shutil.copyfileobj(f, out, 1024*1024)
    if sha256(temp) != FULL_SHA:
        raise ValueError('Assembled archive differs from pinned published checksum; review Kraken release or redownload archive parts')
    os.replace(temp, archive)
    return archive


def inspect_csv(path):
    count = gaps = missing = suffix = 0
    first = last = None
    with open(path, newline='', encoding='utf-8-sig') as f:
        for i, row in enumerate(csv.reader(f)):
            if i == 0 and row and row[0].lower() in ('timestamp','time','ts'):
                continue
            if len(row) not in (6,7):
                raise ValueError('Expected six or seven OHLCVT columns')
            b = Bar(int(row[0]), *(float(x) for x in row[1:6]))
            # Validate each row without core.validate's minimum-length restriction.
            import math
            if b.ts < 0 or b.ts % 3600 or not all(math.isfinite(v) for v in (b.open,b.high,b.low,b.close,b.volume)) or min(b.open,b.high,b.low,b.close)<=0 or b.low>min(b.open,b.close) or b.high<max(b.open,b.close) or b.volume<0:
                raise ValueError(f'Invalid hourly candle at row {i+1}')
            if last is not None:
                if b.ts <= last:
                    raise ValueError('Duplicate or unsorted timestamps')
                if b.ts-last != 3600:
                    gaps += 1; missing += (b.ts-last)//3600-1; suffix=0
            if first is None: first=b.ts
            last=b.ts; count+=1; suffix+=1
    if not count: raise ValueError('Empty CSV')
    return {'first':first,'last':last,'bars':count,'gaps':gaps,'missing_hours':missing,'contiguous_suffix':suffix,'sha256':sha256(path)}


def extract(archive, root, pair, verified=False):
    if pair not in PAIRS: raise ValueError('Unsupported pair')
    root=Path(root); root.mkdir(parents=True,exist_ok=True)
    dest=root/f'{PAIRS[pair]}-hourly-full.csv'; temp=dest.with_suffix('.partial')
    with zipfile.ZipFile(archive) as z:
        names=[n for n in z.namelist() if PurePosixPath(n).name.upper()==pair+'_60.CSV']
        if len(names)!=1: raise ValueError(f'Expected exactly one {pair}_60.csv: {names}')
        with z.open(names[0]) as f, open(temp,'wb') as out:
            shutil.copyfileobj(f,out,1024*1024)
    info=inspect_csv(temp)
    os.replace(temp,dest)
    info.update(pair=pair,interval=60,path=str(dest),archive=str(archive),official_checksum_verified=verified,development_only=True)
    manifest=dest.with_suffix('.manifest.json'); tmp=manifest.with_suffix('.partial')
    tmp.write_text(json.dumps(info,indent=2),encoding='utf-8'); os.replace(tmp,manifest)
    return info


def main():
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--root',required=True)
    s=p.add_subparsers(dest='cmd',required=True)
    a=s.add_parser('download'); a.add_argument('--pair',choices=PAIRS,default='XBTUSD')
    a=s.add_parser('extract'); a.add_argument('--archive',required=True); a.add_argument('--pair',choices=PAIRS,default='XBTUSD')
    x=p.parse_args()
    if x.cmd=='download': result=extract(acquire(x.root),x.root,x.pair,True)
    else: result=extract(x.archive,x.root,x.pair)
    print(json.dumps(result,indent=2))

if __name__=='__main__': main()
