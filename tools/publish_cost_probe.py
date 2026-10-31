"""Bound the possible benefit of hashing while publishing; no production changes."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import statistics
import time


def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):
            h.update(block)
    return h.hexdigest()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--pairs',type=int,default=5)
    a=parser.parse_args();a.out.mkdir(parents=True,exist_ok=False)
    expected=sha(a.source);rows=[]
    for i in range(a.pairs):
        row=dict(order=['A','B'] if i%2==0 else ['B','A'])
        for mode in row['order']:
            dest=a.out/f'{mode}-{i}.bin';start=time.perf_counter()
            if mode=='A':
                digest=sha(a.source)
                with a.source.open('rb') as src,dest.open('xb') as dst:
                    shutil.copyfileobj(src,dst)
            else:
                h=hashlib.sha256()
                with a.source.open('rb') as src,dest.open('xb') as dst:
                    for block in iter(lambda:src.read(1024*1024),b''):
                        h.update(block);dst.write(block)
                digest=h.hexdigest()
            row[mode]=time.perf_counter()-start
            # Verification is outside timing on both sides. Only own scratch is removed.
            assert digest==expected==sha(dest)
            assert dest.parent.resolve()==a.out.resolve() and dest.resolve()!=a.source.resolve()
            dest.unlink()
        rows.append(row)
    first,second=(statistics.median(r[k] for r in rows) for k in ['A','B'])
    result=dict(scope='warm-cache hash+copy operator only; no strict validation removed; not OS physical I/O',
                bytes=a.source.stat().st_size,sha256=expected,raw=rows,A=first,B=second,
                seconds_saved=first-second,exact=True)
    (a.out/'results.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result))


if __name__=='__main__':main()
