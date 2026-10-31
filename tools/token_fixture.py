#!/usr/bin/env python3
"""Run the agent-token baseline fixture: synthetic media, fixed configs, every workflow command.

  python tools/token_fixture.py --out NEW_EMPTY_DIR [--package PATH] [--json result.json]

--package is the flow-video-patch directory whose scripts are measured (default: the one next to
this file). Check out the commit you want to measure there first, e.g. a git worktree.

For each command the record keeps: argv (relative to --out), exit code, wall time, stdout and
stderr byte counts and SHA-256 kept separately. For each artifact: bytes and SHA-256. It also
records the package commit (if it is a git checkout), the package SHA256SUMS.json digest, Python,
NumPy, OpenCV, FFmpeg/ffprobe versions, platform and every FLOW_* environment variable.

Uses only the standard library, FFmpeg and the package's own scripts (TOOL_POLICY.json). Media
is generated here; no user footage. Byte counts depend on the FFmpeg/x264 build and platform, so
compare runs by their recorded environment, not across machines byte for byte.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parents[1]
FFMPEG = os.environ.get('FLOW_FFMPEG', 'ffmpeg')

PREPARE = {
    'schema_version': 1, 'source': 'source.mp4', 'source_size': [1280, 720], 'fps': 24,
    'prepare': {'core': [48, 144], 'context_frames': 24, 'crop': [800, 40, 360, 640],
                'output_size': [360, 640], 'mask_regions': [[100, 80, 160, 200]],
                'color_rgb': [0, 0, 0]}}
# The upload is used as its own return (identity), so generated_start = core_in_upload[0] = 24.
MERGE = {
    'schema_version': 1, 'source': 'source.mp4', 'source_size': [1280, 720], 'fps': 24,
    'patches': [{
        'name': 'a', 'generated': 'upload.mp4', 'target': [48, 144], 'generated_start': 24,
        'crop': [800, 40, 360, 640], 'work_size': [360, 640],
        'source_to_generated_affine': [1, 0, 0, 0, 1, 0],
        'matte': {'region': [60, 40, 240, 300], 'shape': 'ellipse', 'feather': 20, 'protect': []},
        'blend': 'alpha', 'fade_frames': 0, 'gain_bgr': [1, 1, 1], 'offset_bgr': [0, 0, 0]}]}
SOURCE_ARGS = [FFMPEG, '-nostdin', '-v', 'error', '-n',
               '-f', 'lavfi', '-i', 'testsrc2=size=1280x720:rate=24:duration=10',
               '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=10',
               '-c:v', 'libx264', '-preset', 'medium', '-crf', '18', '-pix_fmt', 'yuv420p',
               '-g', '48', '-c:a', 'aac', '-b:a', '128k', '-shortest', 'source.mp4']


def sha(data):
    return hashlib.sha256(data).hexdigest()


def first_line(argv):
    try:
        return subprocess.run(argv, capture_output=True, text=True).stdout.splitlines()[0]
    except (OSError, IndexError):
        return None


def module_version(name):
    try:
        return __import__(name).__version__
    except ImportError:
        return None


def environment(package):
    commit = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=package, capture_output=True, text=True)
    sums = package/'SHA256SUMS.json'
    return {
        'package_commit': commit.stdout.strip() if commit.returncode == 0 else None,
        'package_sha256sums_digest': sha(sums.read_bytes()) if sums.exists() else None,
        'platform': platform.platform(), 'python': sys.version.split()[0],
        'numpy': module_version('numpy'), 'opencv': module_version('cv2'),
        'ffmpeg': first_line([FFMPEG, '-version']),
        'ffprobe': first_line([os.environ.get('FLOW_FFPROBE', 'ffprobe'), '-version']),
        'flow_env': {k: v for k, v in sorted(os.environ.items()) if k.startswith('FLOW_')},
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--out', type=Path, required=True, help='new or empty directory')
    ap.add_argument('--package', type=Path, default=HERE/'flow-video-patch')
    ap.add_argument('--json', type=Path, help='write the record here (default: OUT/fixture-record.json)')
    args = ap.parse_args()
    out, package = args.out.resolve(), args.package.resolve()
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f'{out} is not empty')
    out.mkdir(parents=True, exist_ok=True)
    scripts = package/'scripts'
    py = sys.executable
    (out/'prepare.json').write_text(json.dumps(PREPARE, indent=2)+'\n', encoding='utf-8', newline='\n')
    (out/'merge.json').write_text(json.dumps(MERGE, indent=2)+'\n', encoding='utf-8', newline='\n')
    fp, qc, fit = (str(scripts/n) for n in ('flow_patch.py', 'qc_tools.py', 'fit_patch.py'))
    steps = [
        ('source', SOURCE_ARGS),
        ('doctor', [py, fp, 'doctor']),
        ('inspect', [py, fp, 'inspect', '--source', 'source.mp4']),
        ('prepare_dry_run', [py, fp, 'prepare', '--config', 'prepare.json', '--output', 'upload.mp4', '--dry-run']),
        ('prepare', [py, fp, 'prepare', '--config', 'prepare.json', '--output', 'upload.mp4']),
        ('merge_dry_run', [py, fp, 'merge', '--config', 'merge.json', '--output', 'patched.mp4', '--dry-run']),
        ('precheck', [py, qc, 'precheck', '--config', 'merge.json', '--patch', '0']),
        ('seam', [py, qc, 'seam', '--config', 'merge.json', '--patch', '0']),
        ('fit', [py, fit, '--config', 'merge.json', '--patch', '0', '--output', 'proposed.json']),
        ('check_export', [py, fp, 'merge', '--config', 'merge.json', '--output', 'check.mp4',
                          '--frames', '60:90', '--preview']),
        ('full_merge', [py, fp, 'merge', '--config', 'merge.json', '--output', 'patched.mp4']),
    ]
    commands = []
    for name, argv in steps:
        t = time.monotonic()
        r = subprocess.run(argv, cwd=out, capture_output=True)
        shown = [a.replace(str(scripts), 'scripts').replace(py, 'python') for a in argv]
        commands.append({'name': name, 'argv': shown, 'exit_code': r.returncode,
                         'wall_s': round(time.monotonic()-t, 3),
                         'stdout_bytes': len(r.stdout), 'stdout_sha256': sha(r.stdout),
                         'stderr_bytes': len(r.stderr), 'stderr_sha256': sha(r.stderr)})
        (out/f'{name}.stdout').write_bytes(r.stdout)
        (out/f'{name}.stderr').write_bytes(r.stderr)
        if r.returncode != 0:
            print(f'{name} failed with exit code {r.returncode}; see {out/(name+".stderr")}', file=sys.stderr)
            break
    artifacts = {}
    for f in sorted(out.iterdir()):
        if f.is_file() and f.suffix in ('.mp4', '.json'):
            data = f.read_bytes()
            artifacts[f.name] = {'bytes': len(data), 'sha256': sha(data)}
    review = out/'patched.visual-review.json'
    checks = json.loads(review.read_text(encoding='utf-8'))['visual_checks'] if review.exists() else []
    record = {
        'fixture': 'tools/token_fixture.py', 'environment': environment(package),
        'configs': {'prepare.json': PREPARE, 'merge.json': MERGE}, 'commands': commands,
        'artifacts': artifacts,
        'visual_review_prefilled': {'checks_total': len(checks),
                                    'pass': sum(c['status'] == 'pass' for c in checks),
                                    'not_run': sum(c['status'] == 'not_run' for c in checks)},
        'limits': 'Synthetic media; byte counts depend on platform and FFmpeg/x264 build. '
                  'Not a measurement of real agent sessions or visual quality.'}
    dest = args.json or out/'fixture-record.json'
    dest.write_text(json.dumps(record, ensure_ascii=False, indent=2)+'\n', encoding='utf-8', newline='\n')
    for c in commands:
        print(f"{c['exit_code']:3d} {c['stdout_bytes']:7d} B out {c['stderr_bytes']:6d} B err  {c['name']}")
    print(f'record: {dest}')
    return 0 if all(c['exit_code'] == 0 for c in commands) and len(commands) == len(steps) else 1


if __name__ == '__main__':
    sys.exit(main())
