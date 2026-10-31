#!/usr/bin/env python3
"""Read-only v2.9 performance evidence using disposable synthetic media.

This is an observation harness, not a speed benchmark or a replacement renderer.
Uses only the existing Python/NumPy/OpenCV/FFmpeg dependencies. No network.
Output is exclusive-create; paths, footage and credentials are never serialized.
"""
import argparse
import collections
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skill', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    a = parser.parse_args()
    if a.output.exists():
        parser.error('Output exists; choose a new filename')
    skill = a.skill.resolve()
    sys.path.insert(0, str(skill / 'scripts'))
    import flow_patch as fp
    import qc_tools as qc

    counts = collections.Counter()
    decode, reader = fp.decode_pass, fp.Reader
    roles = {}

    def counted_decode(path, strict=False):
        counts['decode_pass_total'] += 1
        counts['decode_pass_' + roles.get(str(Path(path).resolve()), 'other')] += 1
        return decode(path, strict)

    def counted_reader(*args, **kwargs):
        counts['reader_total'] += 1
        return reader(*args, **kwargs)

    with tempfile.TemporaryDirectory(prefix='flow-perf-evidence-') as tmp:
        root = Path(tmp)
        source, generated = root / 'source.mp4', root / 'generated.mp4'
        cmd = [fp.FFMPEG, '-v', 'error', '-n', '-f', 'lavfi', '-i',
               'testsrc2=size=192x128:rate=24:duration=1.25',
               '-c:v', 'libx264', '-threads', '4', '-pix_fmt', 'yuv420p', str(source)]
        subprocess.run(cmd, check=True, capture_output=True, timeout=60)
        generated.write_bytes(source.read_bytes())
        roles = {str(source.resolve()): 'source', str(generated.resolve()): 'generated'}
        cfg = dict(schema_version=1, source=str(source), source_size=[192, 128], fps=24,
                   patches=[dict(generated=str(generated), target=[0, 30],
                                 crop=[0, 0, 192, 128], work_size=[192, 128],
                                 matte=dict(region=[60, 40, 48, 40], feather=8), blend='alpha')])
        config = root / 'merge.json'
        config.write_text(json.dumps(cfg), encoding='utf-8')
        fp._PROBED.clear()
        fp._PASSES.clear()
        fp.decode_pass, fp.Reader = counted_decode, counted_reader
        started = time.perf_counter()
        try:
            result = qc.cmd_precheck(argparse.Namespace(config=str(config), patch=0, samples=8))
        finally:
            fp.decode_pass, fp.Reader = decode, reader
        elapsed = time.perf_counter() - started
        # Distinct files with identical synthetic frames: valid mapping without private media.
        assert result['summary']['spec']['mapped_frames'] == 30
        assert result['summary']['frames_sampled'] == 8
        observation = dict(counts)
        observation.update(wall_seconds=round(elapsed, 6),
                           provisional_call=result['provisional_call'],
                           frames_sampled=result['summary']['frames_sampled'],
                           source_frames=30, generated_frames=30, size=[192, 128],
                           note='Single instrumented run; counts are evidence, timing is not a speed claim.')

    manifest = json.loads((skill / 'SHA256SUMS.json').read_text(encoding='utf-8'))
    mismatch = [name for name, sha in manifest.items()
                if hashlib.sha256((skill / name).read_bytes()).hexdigest() != sha]
    assert not mismatch, mismatch
    manifest_sha = hashlib.sha256((skill/'SHA256SUMS.json').read_bytes()).hexdigest()
    reference_sha = 'ca7e278408eba4db53ea1b7110f95dc820b51b8664f48dc0ecf5cb4e331a137e'
    policy = json.loads((skill/'TOOL_POLICY.json').read_text(encoding='utf-8'))
    report = dict(schema_version=2, package_version=policy['package_version'],
                  reference_baseline_commit='401f8dadef2382d98e8a653dde2aa4a069f1310c',
                  matches_reference_manifest=manifest_sha == reference_sha,
                  manifest_entries_verified=len(manifest),
                  package_manifest_sha256=manifest_sha,
                  environment=dict(os=platform.system(), python=platform.python_version(),
                                   numpy=importlib.metadata.version('numpy'),
                                   opencv=fp.opencv_distribution(),
                                   ffmpeg=fp.tool_version(fp.FFMPEG),
                                   flow_overrides={k: os.environ[k] for k in
                                       ('FLOW_KERNELS', 'FLOW_PROCESS_THREADS', 'FLOW_ENCODE_THREADS',
                                        'FLOW_SEGMENTS') if k in os.environ}),
                  precheck=observation,
                  static_memory_model=dict(reader_queue_depth=fp.PREFETCH,
                      raw_8k_frame_bytes=7680*4320*3,
                      three_8k_readers_queued_bytes=3*fp.PREFETCH*7680*4320*3,
                      note='Queue payload lower bound only; excludes current/in-flight frames, BGR, pyramids, encoder and decoder.'),
                  speedup_measured=False, visual_acceptance='not_run')
    a.output.parent.mkdir(parents=True, exist_ok=True)
    with a.output.open('x', encoding='utf-8', newline='\n') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
        f.write('\n')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
