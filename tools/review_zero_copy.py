#!/usr/bin/env python3
"""Compare v2.9 and a candidate on the all-GOPs-dirty copy-unpatched path.

Synthetic media, existing dependencies, no network or production footage.
Run with --baseline PATH_TO_SKILL --candidate PATH_TO_SKILL --output NEW_JSON.
Neither checkout is changed; scratch media is deleted with TemporaryDirectory.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--baseline', required=True, type=Path)
    p.add_argument('--candidate', required=True, type=Path)
    p.add_argument('--output', required=True, type=Path)
    a = p.parse_args()
    if a.output.exists():
        p.error('Output exists; choose a new filename')
    ffmpeg = os.environ.get('FLOW_FFMPEG', 'ffmpeg')
    ffprobe = os.environ.get('FLOW_FFPROBE', 'ffprobe')
    env = dict(os.environ, FLOW_KERNELS='numpy', FLOW_PROCESS_THREADS='4',
               FLOW_ENCODE_THREADS='4', FLOW_SEGMENTS='1', FLOW_VIDEO_ENCODER='libx264',
               FLOW_CRF='17', FLOW_X264_PRESET='fast')
    def run(argv):
        return subprocess.run(argv, check=True, capture_output=True, env=env, timeout=120).stdout
    results = {}
    with tempfile.TemporaryDirectory(prefix='flow-zero-copy-review-') as tmp:
        root = Path(tmp)
        src = root/'source.mp4'
        run([ffmpeg, '-v', 'error', '-n', '-f', 'lavfi', '-i',
             'testsrc2=size=192x128:rate=24:duration=2', '-c:v', 'libx264', '-threads', '4',
             '-pix_fmt', 'yuv420p', '-x264-params',
             'keyint=12:min-keyint=12:scenecut=0:bframes=0', str(src)])
        cfg = root/'merge.json'
        cfg.write_text(json.dumps(dict(schema_version=1, source=str(src), source_size=[192, 128], fps=24,
            patches=[dict(generated=str(src), target=[0, 48], crop=[0, 0, 192, 128],
                          work_size=[192, 128], matte=dict(region=[32, 32, 64, 48], feather=4),
                          blend='alpha')])), encoding='utf-8')
        for label, skill in [('baseline', a.baseline), ('candidate', a.candidate)]:
            out = root/(label+'.mp4')
            run([sys.executable, str(skill.resolve()/'scripts/flow_patch.py'), 'merge',
                 '--config', str(cfg), '--output', str(out), '--copy-unpatched'])
            rows = run([ffmpeg, '-v', 'error', '-xerror', '-protocol_whitelist', 'file',
                        '-i', str(out), '-map', '0:v:0', '-f', 'framehash', '-hash', 'sha256', '-'])
            hashes = [line.decode().split(',')[-1].strip() for line in rows.splitlines()
                      if line and not line.startswith(b'#')]
            info = json.loads(run([ffprobe, '-v', 'error', '-protocol_whitelist', 'file',
                 '-select_streams', 'v:0', '-show_entries', 'stream=has_b_frames,nb_frames,r_frame_rate',
                 '-of', 'json', str(out)]))['streams'][0]
            results[label] = dict(frame_hashes=hashes, stream=info,
                sha256=hashlib.sha256(out.read_bytes()).hexdigest(),
                script_sha256=hashlib.sha256((skill/'scripts/flow_patch.py').read_bytes()).hexdigest())
    assert len(results['baseline']['frame_hashes']) == len(results['candidate']['frame_hashes']) == 48
    report = dict(schema_version=1, case='192x128/24fps,48 frames,GOP12,B0,all GOPs dirty,segments1',
        decoded_frames_different=sum(x != y for x, y in zip(results['baseline']['frame_hashes'],
                                                           results['candidate']['frame_hashes'])),
        encoded_bytes_equal=results['baseline']['sha256'] == results['candidate']['sha256'],
        details=results, visual_review='not_run', speedup_measured=False)
    a.output.parent.mkdir(parents=True, exist_ok=True)
    with a.output.open('x', encoding='utf-8', newline='\n') as f:
        json.dump(report, f, indent=2)
        f.write('\n')
    print(json.dumps({k: v for k, v in report.items() if k != 'details'}))


if __name__ == '__main__':
    main()
