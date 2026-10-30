#!/usr/bin/env python3
"""Run this package's scripts on the one LAN worker named in the user's local profile.

  check                         every worker file must match this copy's SHA256SUMS.json, then doctor
  push TASK FILE...             copy files into the worker task folder; SHA-256 compared both ends
  run TASK SCRIPT [ARGS...]     run flow_patch / fit_patch / qc_tools there (cwd = task folder)
  pull TASK FILE... [--dest D]  copy results back; SHA-256 compared; never overwrites

Only `ssh` is used (OpenSSH client, BatchMode). The profile
~/.claude/flow-patch-local/remote-worker.json holds {"ssh_alias", "task_root", "skill_dir",
"python"}; machine details never enter the package. Every remote step runs the small helper
below, sent base64-encoded on the command line, so no shell quoting differs between Windows
and POSIX workers. Files travel over the ssh stdin/stdout stream. Nothing is deleted.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from flow_patch import require, private_output, utf8_console  # noqa: E402

PROFILE = Path(os.environ.get('FLOW_WORKER_PROFILE', '~/.claude/flow-patch-local/remote-worker.json')).expanduser()
SCRIPTS = ('flow_patch', 'fit_patch', 'qc_tools')
# One plain name: no folders, drive letters, wildcards, control characters or leading dot.
NAME = re.compile(r'^[^.\\/:*?"<>|\x00-\x1f][^\\/:*?"<>|\x00-\x1f]{0,199}$')
# Settings forwarded to remote runs; nothing else from the local environment is sent.
FORWARD = ('FLOW_ENCODE_THREADS', 'FLOW_PROCESS_THREADS', 'FLOW_SEGMENTS', 'FLOW_KERNELS', 'FLOW_CRF',
           'FLOW_X264_PRESET', 'FLOW_VIDEO_ENCODER', 'FLOW_HW_DELIVERABLE', 'FLOW_COMMAND_TIMEOUT')

# Runs on the worker. Keep it short: it travels base64-encoded inside the command line.
HELPER = r'''
import hashlib, json, os, subprocess, sys
q = json.loads(REQUEST)
def sha(p):
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''):
            h.update(b)
    return h.hexdigest()
task = os.path.join(q['task_root'], q['task']) if q.get('task') else None
op = q['op']
def inside(v):
    if os.path.isabs(v) or v[1:2] == ':' or v.startswith(('/', '\\')):
        return False
    base = os.path.realpath(task)
    return os.path.commonpath([base, os.path.realpath(os.path.join(base, v))]) == base
if op == 'verify':
    root = q['skill_dir']; sums = os.path.join(root, 'SHA256SUMS.json'); files, unsafe = {}, []
    for rel in (json.load(open(sums, encoding='utf-8')) if os.path.isfile(sums) else []):
        parts = rel.replace('\\', '/').split('/')
        if os.path.isabs(rel) or ':' in rel or '..' in parts:
            unsafe.append(rel); continue
        f = os.path.join(root, *parts)
        files[rel] = sha(f) if os.path.isfile(f) else None
    print(json.dumps({'sums': sha(sums) if os.path.isfile(sums) else None, 'files': files, 'unsafe': unsafe}))
elif op == 'doctor':
    r = subprocess.run([sys.executable, os.path.join(q['skill_dir'], 'scripts', 'flow_patch.py'), 'doctor'],
                       capture_output=True, text=True, encoding='utf-8')
    print(json.dumps({'doctor': r.stdout, 'code': r.returncode, 'error': r.stderr[-2000:]}))
elif op == 'push':
    os.makedirs(task, exist_ok=True)
    path = os.path.join(task, q['name'])
    with open(path, 'xb') as f:
        for b in iter(lambda: sys.stdin.buffer.read(1 << 20), b''):
            f.write(b)
    print(json.dumps({'sha': sha(path)}))
elif op == 'stat':
    path = os.path.join(task, q['name'])
    print(json.dumps({'sha': sha(path), 'size': os.path.getsize(path)}))
elif op == 'pull':
    with open(os.path.join(task, q['name']), 'rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''):
            sys.stdout.buffer.write(b)
elif op == 'run':
    for a in q['args']:
        for v in [a] + ([a.split('=', 1)[1]] if a.startswith('-') and '=' in a else []):
            if (any(c in v for c in '/\\') or v == '..' or v[1:2] == ':') and not inside(v):
                sys.exit('Refused on the worker: argument leaves the task folder: ' + a)
    os.environ.update(q['env'])
    script = os.path.join(q['skill_dir'], 'scripts', q['script'] + '.py')
    sys.exit(subprocess.run([sys.executable, script] + q['args'], cwd=task).returncode)
'''


def profile():
    require(PROFILE.is_file(), f'No worker profile at {PROFILE}; see references/remote-worker.md')
    p = json.loads(PROFILE.read_text(encoding='utf-8'))
    require(all(isinstance(p.get(k), str) and p[k] for k in ('ssh_alias', 'task_root', 'skill_dir')),
            'Profile needs ssh_alias, task_root and skill_dir')
    require(re.fullmatch(r'[A-Za-z0-9._-]+', p['ssh_alias']) is not None,
            'ssh_alias must be a plain ~/.ssh/config Host alias')
    # A full path is fine without spaces (cmd.exe quoting differs from sh); see remote-worker.md.
    require(re.fullmatch(r'[A-Za-z0-9._/\\:-]+', p.get('python', 'python')) is not None,
            'Invalid python: use a name on the worker PATH or a full path without spaces')
    return p


def ssh(p, request, **kw):
    """One remote step: the helper plus its request, base64 on the command line."""
    code = 'REQUEST = %r\n' % json.dumps(dict(request, task_root=p['task_root'], skill_dir=p['skill_dir']))
    payload = base64.b64encode((code + HELPER).encode()).decode()
    command = f'{p.get("python", "python")} -c "exec(__import__(\'base64\').b64decode(\'{payload}\'))"'
    # FLOW_SSH (tests): a JSON argv standing in for the ssh executable.
    program = json.loads(os.environ['FLOW_SSH']) if 'FLOW_SSH' in os.environ else ['ssh']
    argv = [*program, '-o', 'BatchMode=yes', p['ssh_alias'], command]
    return subprocess.run(argv, **kw)


def answer(result):
    require(result.returncode == 0, 'Remote step failed: ' + result.stderr.decode(errors='replace')[-2000:])
    return json.loads(result.stdout.decode().strip().splitlines()[-1])


def sha_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''):
            h.update(b)
    return h.hexdigest()


def task_name(value):
    require(NAME.fullmatch(value) is not None and '..' not in value, f'Invalid task name: {value}')
    return value


def plain_name(value):
    require(NAME.fullmatch(value) is not None and '..' not in value,
            f'Use a plain file name inside the task folder: {value}')
    return value


def cmd_check(p, a):
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root/'SHA256SUMS.json').read_text(encoding='utf-8'))
    local_bad = [k for k, v in manifest.items() if not (root/k).is_file() or sha_file(root/k) != v]
    require(not local_bad, 'This copy does not match its own SHA256SUMS.json: '+', '.join(local_bad[:10]))
    # Hash every listed file on the worker before running anything from it.
    r = answer(ssh(p, {'op': 'verify'}, capture_output=True))
    require(not r['unsafe'], 'Worker SHA256SUMS.json lists unsafe paths: '+', '.join(r['unsafe'][:10]))
    require(r['sums'] == sha_file(root/'SHA256SUMS.json'), 'Worker SHA256SUMS.json differs from this one; sync first')
    differ = sorted(k for k in manifest if r['files'].get(k) != manifest[k])
    require(not differ, 'Worker files missing or different from the manifest: '+', '.join(differ[:10]))
    d = answer(ssh(p, {'op': 'doctor'}, capture_output=True))
    require(d['code'] == 0, 'Worker doctor failed: ' + d['error'])
    print(d['doctor'], end='')
    print(f'PASS: all {len(manifest)} worker files match; compare the versions above with the profile record')


def cmd_push(p, a):
    for f in a.files:
        src = Path(f); name = plain_name(src.name)
        require(src.is_file(), f'Not a regular file: {f}')
        with src.open('rb') as stream:
            r = answer(ssh(p, {'op': 'push', 'task': a.task, 'name': name}, stdin=stream, capture_output=True))
        require(r['sha'] == sha_file(src), f'SHA-256 differs after copying {name}; do not use the copy')
        print(f'pushed {name} {r["sha"]}')


def leaves_task(value):
    """Absolute, drive, UNC or parent-directory paths, on either platform."""
    return (value.startswith(('/', '\\')) or re.match(r'^[A-Za-z]:', value) is not None
            or '..' in re.split(r'[\\/]', value))


def cmd_run(p, a):
    require(a.script in SCRIPTS, 'Only flow_patch, fit_patch and qc_tools run remotely')
    for arg in a.args:
        # --option=value carries a path after '=' too; the worker re-checks resolved paths.
        values = [arg] + ([arg.split('=', 1)[1]] if arg.startswith('-') and '=' in arg else [])
        require(not any(leaves_task(v) for v in values), f'Remote arguments stay inside the task folder: {arg}')
    env = {k: os.environ[k] for k in FORWARD if k in os.environ}
    code = ssh(p, {'op': 'run', 'task': a.task, 'script': a.script, 'args': a.args, 'env': env}).returncode
    raise SystemExit(code)


def cmd_pull(p, a):
    dest = Path(a.dest)
    require(dest.is_dir(), f'Destination folder does not exist: {dest}')
    for f in a.files:
        name = plain_name(f)
        target = dest/name
        require(not target.exists(), f'{target} exists; choose another destination')
        expected = answer(ssh(p, {'op': 'stat', 'task': a.task, 'name': name}, capture_output=True))['sha']
        with private_output(target, binary=True) as out:
            result = ssh(p, {'op': 'pull', 'task': a.task, 'name': name}, stdout=out, stderr=subprocess.PIPE)
            require(result.returncode == 0, 'Remote read failed: ' + result.stderr.decode(errors='replace')[-2000:])
        if sha_file(target) != expected:
            target.unlink()
            raise ValueError(f'SHA-256 differs after copying {name}; the local copy was removed')
        print(f'pulled {name} {expected}')


def main():
    utf8_console()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('check')
    s = sub.add_parser('push'); s.add_argument('task', type=task_name); s.add_argument('files', nargs='+')
    s = sub.add_parser('run'); s.add_argument('task', type=task_name); s.add_argument('script')
    s.add_argument('args', nargs=argparse.REMAINDER)
    s = sub.add_parser('pull'); s.add_argument('task', type=task_name); s.add_argument('files', nargs='+')
    s.add_argument('--dest', default='.')
    a = ap.parse_args()
    globals()['cmd_' + a.cmd](profile(), a)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as e:
        print(str(e), file=sys.stderr)
        raise SystemExit(1)
