"""Compare existing preview and structure preview using synthetic media only.

Each timed call is a fresh complete CLI process (including verification, hashes and
publication). New per-trial directories make input probe caches empty; OS caches
are uncontrolled/warm. Different blend/color/view recipes are intentional.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--pairs', type=int, default=5)
    parser.add_argument('--frames')
    parser.add_argument('--warm-probes', action='store_true',
                        help='prime each input probe cache via an untimed dry run before timing')
    args = parser.parse_args()
    if args.pairs < 1:
        parser.error('--pairs must be positive')
    args.out.mkdir(parents=True, exist_ok=False)
    env = dict(os.environ, FLOW_PROCESS_THREADS='4', FLOW_ENCODE_THREADS='4', FLOW_SEGMENTS='1')
    source_file = ROOT/'flow-video-patch/scripts/flow_patch.py'
    report = dict(scope='complete fresh CLI; '+('warm' if args.warm_probes else 'empty')+
                  ' input probe cache per trial; warm/uncontrolled OS cache',
                  frames=args.frames, kernels=env.get('FLOW_KERNELS','auto'),
                  source_sha256=digest(source_file), pairs=[])
    for n in range(args.pairs):
        order = ['preview','structure'] if n % 2 == 0 else ['structure','preview']
        row = dict(order=order)
        for variant in order:
            folder = args.out/f'{n}-{variant}'
            folder.mkdir()
            out = folder/'check.mp4'
            argv = [sys.executable,str(ROOT/'flow-video-patch/scripts/flow_patch.py'),
                    'merge','--config',str(args.config.resolve()),'--output',str(out),
                    '--preview' if variant == 'preview' else '--structure-preview']
            if args.frames:
                argv += ['--frames',args.frames]
            assert digest(source_file) == report['source_sha256'], 'Source changed during experiment'
            if args.warm_probes:
                warmup = subprocess.run(argv+['--dry-run'],env=env,capture_output=True,timeout=600)
                if warmup.returncode:
                    raise RuntimeError(warmup.stderr.decode(errors='replace'))
            start = time.perf_counter()
            result = subprocess.run(argv,env=env,capture_output=True,timeout=600)
            seconds = time.perf_counter()-start
            (folder/'stdout.txt').write_bytes(result.stdout)
            (folder/'stderr.txt').write_bytes(result.stderr)
            if result.returncode:
                raise RuntimeError(result.stderr.decode(errors='replace'))
            data = json.loads(out.with_suffix('.report.json').read_text(encoding='utf-8'))
            row[variant] = dict(seconds=seconds, output_sha256=data['output_sha256'],
                                size=data['output']['size'], frames=data['output']['frames'],
                                fps=data['output']['fps'], decode_pass=data['decode_pass'],
                                scope=data['scope'], tools=data['tools'])
        report['pairs'].append(row)
        print(json.dumps(dict(pair=n,seconds={v:row[v]['seconds'] for v in order})),flush=True)
    medians = {v:statistics.median(row[v]['seconds'] for row in report['pairs'])
               for v in ['preview','structure']}
    report['medians_seconds'] = medians
    report['saved_percent'] = (1-medians['structure']/medians['preview'])*100
    report['speedup'] = medians['preview']/medians['structure']
    assert digest(source_file) == report['source_sha256'], 'Source changed during experiment'
    (args.out/'results.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8',newline='\n')
    print(json.dumps(dict(medians=medians,saved_percent=report['saved_percent'])),flush=True)


if __name__ == '__main__':
    main()
