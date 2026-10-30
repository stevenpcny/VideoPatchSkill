#!/usr/bin/env python3
"""Build a flow-video-patch release: version, package hashes, reproducible ZIP, self-check.

  python tools/release.py 2.9

Sets TOOL_POLICY.json package_version, rewrites SHA256SUMS.json from the files git tracks (or would
track) in flow-video-patch/, writes dist/flow-video-patch-vX.Y.zip with fixed timestamps and
permissions, its .sha256, then re-reads the ZIP and checks every entry against the manifest.
CHANGELOG.md, README files and VALIDATION.json stay hand-written: the script only reports whether
they mention the version. Text files are written with LF on every OS, so a rebuild on Windows
gives the same bytes as on Linux/macOS.
"""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT/'flow-video-patch'
STAMP = (2020, 1, 1, 0, 0, 0)  # fixed, so the same content always gives the same ZIP bytes


def package_files():
    out = subprocess.check_output(['git', 'ls-files', '--cached', '--others', '--exclude-standard', '.'],
                                  cwd=PACKAGE, text=True)
    return sorted(set(out.split()) - {'SHA256SUMS.json'})


def main():
    if len(sys.argv) != 2 or not re.fullmatch(r'\d+\.\d+(\.\d+)?', sys.argv[1]):
        raise SystemExit('usage: python tools/release.py X.Y')
    version = sys.argv[1]
    policy = PACKAGE/'TOOL_POLICY.json'
    data = json.loads(policy.read_text(encoding='utf-8'))
    data['package_version'] = version
    policy.write_text(json.dumps(data, ensure_ascii=False, indent=2)+'\n', encoding='utf-8', newline='\n')
    files = package_files()
    sums = {f: hashlib.sha256((PACKAGE/f).read_bytes()).hexdigest() for f in files}
    (PACKAGE/'SHA256SUMS.json').write_text(json.dumps(sums, ensure_ascii=False, indent=2)+'\n', encoding='utf-8', newline='\n')
    archive = ROOT/'dist'/f'flow-video-patch-v{version}.zip'
    with zipfile.ZipFile(archive, 'w') as z:
        for name in sorted(files+['SHA256SUMS.json']):
            info = zipfile.ZipInfo('flow-video-patch/'+name, date_time=STAMP)
            info.compress_type, info.external_attr, info.create_system = zipfile.ZIP_DEFLATED, 0o644 << 16, 3
            z.writestr(info, (PACKAGE/name).read_bytes())
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_name(archive.name+'.sha256').write_text(f'{digest}  {archive.name}\n', encoding='utf-8', newline='\n')
    with zipfile.ZipFile(archive) as z:
        manifest = json.loads(z.read('flow-video-patch/SHA256SUMS.json'))
        bad = [f for f, h in manifest.items() if hashlib.sha256(z.read('flow-video-patch/'+f)).hexdigest() != h]
        binaries = [n for n in z.namelist() if n.endswith(('.so', '.dll', '.dylib', '.exe'))]
    if bad or binaries:
        raise SystemExit(f'ZIP check failed: mismatched {bad}, binaries {binaries}')
    print(f'{archive.relative_to(ROOT)}: {len(manifest)+1} files, sha256 {digest}')
    for doc in ('flow-video-patch/CHANGELOG.md', 'flow-video-patch/README.md', 'README.md',
                'flow-video-patch/VALIDATION.json'):
        if f'{version}' not in (ROOT/doc).read_text(encoding='utf-8'):
            print(f'reminder: {doc} does not mention {version} yet')


if __name__ == '__main__':
    main()
