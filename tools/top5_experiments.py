"""Local development experiments; never changes the installed media pipeline.

Uses an explicit synthetic source/return pair. Outputs and raw measurements stay
in the requested new directory. Timed main-thread stages are exclusive wall time,
including waits, not summed FFmpeg/worker CPU time.
"""
import argparse
import collections
import contextlib
import functools
import hashlib
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import threading
import time
import types

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'flow-video-patch/scripts'))
import flow_patch as fp


class Stages:
    def __init__(self):
        self.seconds = collections.Counter()
        self.calls = collections.Counter()
        self.stack = []
        self.owner = threading.get_ident()

    def wrap(self, obj, name, label):
        original = getattr(obj, name)
        @functools.wraps(original)
        def call(*args, **kwargs):
            if threading.get_ident() != self.owner:
                return original(*args, **kwargs)
            node = [time.perf_counter(), 0.0]
            self.stack.append(node)
            try:
                return original(*args, **kwargs)
            finally:
                elapsed = time.perf_counter() - node[0]
                self.stack.pop()
                self.seconds[label] += elapsed - node[1]
                self.calls[label] += 1
                if self.stack:
                    self.stack[-1][1] += elapsed
        setattr(obj, name, call)
        return lambda: setattr(obj, name, original)

    @contextlib.contextmanager
    def enabled(self):
        specs = [(fp, 'probe', 'input_validation'),
                 (fp, 'verify', 'output_validation'),
                 (fp, 'merge_plan', 'plan_other'),
                 (fp, 'process', 'render_other'),
                 (fp.Reader, 'advance', 'decoder_wait'),
                 (fp.Reader, 'passthrough420', 'source_chroma_layout'),
                 (fp, 'yuv_to_bgr', 'color_conversion'),
                 (fp, 'bgr_to_yuv420', 'color_conversion'),
                 (fp, 'blend', 'blend'),
                 (fp, 'patch_inputs', 'patch_transform'),
                 (fp, 'matte', 'mask'),
                 (fp.FrameWriter, 'write', 'encoder_wait'),
                 (fp.FrameWriter, 'finish', 'encoder_wait')]
        with contextlib.ExitStack() as stack:
            for obj, name, label in specs:
                stack.callback(self.wrap(obj, name, label))
            yield


def save(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding='utf-8')


def stats(values):
    m = statistics.median(values)
    return dict(median=m, mad=statistics.median(abs(v-m) for v in values),
                minimum=min(values), maximum=max(values))


@contextlib.contextmanager
def direct_bands():
    original = fp.in_bands
    fp.in_bands = lambda height, work: work(0, height)
    try:
        yield
    finally:
        fp.in_bands = original


def lut_patch_inputs(p, i, roi, generated, count):
    """Exact uint8 fixed-color lookup; geometry/fade semantics are unchanged."""
    ws = tuple(p['work_size'])
    original = cv2.resize(roi, ws, interpolation=cv2.INTER_LANCZOS4)
    patch = cv2.resize(generated, ws, interpolation=cv2.INTER_LANCZOS4)
    affine = fp.curve(p.get('source_to_generated_affine', [1,0,0,0,1,0]), i).reshape(2,3)
    patch = cv2.warpAffine(patch, affine, ws,
                           flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
                           borderMode=cv2.BORDER_CONSTANT)
    gain, offset = p.get('gain_bgr', [1,1,1]), p.get('offset_bgr', [0,0,0])
    if list(gain) != [1,1,1] or list(offset) != [0,0,0]:
        key = (tuple(gain), tuple(offset))
        if key not in LUTS:
            # Same float64 operation order, clipping and ties-to-even as production.
            values = np.arange(256, dtype=np.uint8)[:, None].astype(float)
            LUTS[key] = np.clip(values*gain+offset, 0, 255).round().astype(np.uint8).reshape(256,1,3)
        patch = cv2.LUT(patch, LUTS[key])
    m = fp.matte(p['matte'], i, ws)
    fade_in, fade_out = fp.fades(p)
    if fade_in or fade_out:
        k = i-p['target'][0]
        m *= min(1, k/fade_in if fade_in else 1,
                 (count-1-k)/fade_out if fade_out else 1)
    return original, patch, m


LUTS = {}


def windows_reader_pipe(popen, argv, args, kwargs):
    """Bounded 1 MiB anonymous pipe; retain Popen's handle inheritance rules."""
    import ctypes
    from ctypes import wintypes
    import msvcrt
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    create = kernel.CreatePipe
    create.argtypes = [ctypes.POINTER(wintypes.HANDLE), ctypes.POINTER(wintypes.HANDLE),
                       ctypes.c_void_p, wintypes.DWORD]
    create.restype = wintypes.BOOL
    close = kernel.CloseHandle
    close.argtypes, close.restype = [wintypes.HANDLE], wintypes.BOOL
    rh, wh = wintypes.HANDLE(), wintypes.HANDLE()
    if not create(ctypes.byref(rh), ctypes.byref(wh), None, 1024*1024):
        raise ctypes.WinError(ctypes.get_last_error())
    fds = []
    try:
        fds.append(msvcrt.open_osfhandle(rh.value, os.O_RDONLY | os.O_BINARY))
        rh = wintypes.HANDLE()
        fds.append(msvcrt.open_osfhandle(wh.value, os.O_WRONLY | os.O_BINARY))
        wh = wintypes.HANDLE()
    except BaseException:
        for fd in fds:
            os.close(fd)
        raise
    finally:
        if rh.value: close(rh)
        if wh.value: close(wh)
    reader, writer = os.fdopen(fds[0], 'rb'), os.fdopen(fds[1], 'wb')
    parent, child = reader, writer
    try:
        proc = popen(argv, *args, **dict(kwargs, stdout=child))
    except BaseException:
        parent.close()
        raise
    finally:
        child.close()
    proc.stdout = parent
    return proc


@contextlib.contextmanager
def candidate(name):
    """One factor per experiment, scoped to this process and restored on errors."""
    with contextlib.ExitStack() as stack:
        if name == 'patch_transform':
            old = fp.patch_inputs
            fp.patch_inputs = lut_patch_inputs
            stack.callback(setattr, fp, 'patch_inputs', old)
        elif name in ('decoder_wait', 'pipe_capacity'):
            old = subprocess.Popen
            def popen(argv, *a, **kw):
                # Reader's rawvideo decoder only. Never changes encoder/probe checks.
                if isinstance(argv, list) and argv[0] == fp.FFMPEG and 'rawvideo' in argv and argv[-1] == '-':
                    if name == 'pipe_capacity':
                        if sys.platform != 'win32':
                            raise RuntimeError('pipe_capacity is a Windows-only experiment')
                        return windows_reader_pipe(old, argv, a, kw)
                    else:
                        argv = argv[:1]+['-threads', '4', '-filter_threads', '1']+argv[1:]
                return old(argv, *a, **kw)
            subprocess.Popen = popen
            stack.callback(setattr, subprocess, 'Popen', old)
        elif name in ('blend', 'color_conversion', 'source_chroma_layout'):
            specs = {'blend': [(fp, 'blend')],
                     'color_conversion': [(fp, 'yuv_to_bgr'), (fp, 'bgr_to_yuv420')],
                     'source_chroma_layout': [(fp.Reader, 'passthrough420')]}[name]
            for obj, attr in specs:
                old = getattr(obj, attr)
                def wrapped(*a, _old=old, **kw):
                    with direct_bands():
                        return _old(*a, **kw)
                setattr(obj, attr, wrapped)
                stack.callback(setattr, obj, attr, old)
        elif name != 'baseline':
            raise ValueError(name)
        yield


def decoded_raw(path, info, frames=2):
    r = fp.Reader(str(path), info['size'], info['color'])
    result = []
    try:
        for i in range(frames):
            r.advance(i)
            result.append(r.raw.copy())
    finally:
        r.close()
    return result


def experiments(cfg, out, pairs, variant=None):
    info = fp.probe(cfg['source'])
    gi = fp.probe(cfg['patches'][0]['generated'])
    assert info['size'] == [3840,2160] and info['frames'] == 240 and info['fps'] == '24'
    assert gi['size'] == [640,640] and gi['frames'] == 240 and gi['fps'] == '24'
    source = decoded_raw(cfg['source'], info)
    returned = decoded_raw(cfg['patches'][0]['generated'], gi)
    color = info['color']
    original = fp.yuv_to_bgr(*(c[400:1040,400:1040] for c in source[0]), color)
    generated = fp.yuv_to_bgr(*returned[0], gi['color'])
    p = cfg['patches'][0]
    o, g, m = fp.patch_inputs(p, 0, original, generated, 240)
    report = dict(scope='isolated real production stage calls, 240 frames; cache construction included per run; quality excluded from timing',
                  pairs=pairs, quality={}, stages={})
    # Extra numerical/geometry controls, independent of the timing fixture.
    rng = np.random.default_rng(3105)
    controls = 0
    for size in [(64, 64), (96, 128), (640, 640)]:
        w, h = size
        roi = rng.integers(0, 256, (h, w, 3), dtype=np.uint8)
        ret = rng.integers(0, 256, roi.shape, dtype=np.uint8)
        for affine in ([1,0,0,0,1,0], [1,0,.25,0,1,-.5]):
            for fade in (0, 3):
                pp = dict(work_size=size, target=[0, 12],
                          matte=dict(region=[8,8,w-16,h-16], feather=3,
                                     protect=[[w//2,h//2,4,4]]),
                          gain_bgr=[1.001,.5,3.7], offset_bgr=[-.5,127.5,-231.5],
                          source_to_generated_affine=affine, fade_frames=fade)
                for frame in [0, 2, 6, 11]:
                    a = fp.patch_inputs(pp, frame, roi, ret, 12)
                    b = lut_patch_inputs(pp, frame, roi, ret, 12)
                    assert all(np.array_equal(x,y) for x,y in zip(a,b))
                    controls += 1
        for matrix in fp.KR_KB:
            for ran in ['pc', 'tv']:
                cc = dict(color, matrix=matrix, range=ran)
                planes = [rng.integers(0,256,(h,w),dtype=np.uint8) for _ in range(3)]
                a = fp.yuv_to_bgr(*planes, cc)
                ab = fp.bgr_to_yuv420(roi, cc)
                with candidate('color_conversion'):
                    b = fp.yuv_to_bgr(*planes, cc)
                    bb = fp.bgr_to_yuv420(roi, cc)
                assert np.array_equal(a,b) and ab == bb
                controls += 1
    report['extra_quality_controls'] = controls
    save(out/'experiments.json', report)

    def workload(name, quality=False):
        hashes = hashlib.sha256() if quality else None
        if name == 'decoder_wait':
            for path, meta in [(cfg['source'], info), (p['generated'], gi)]:
                r = fp.Reader(path, meta['size'], meta['color'])
                try:
                    for i in range(240):
                        r.advance(i)
                        if quality:
                            hashes.update(r.raw)
                finally:
                    r.close()
        else:
            fake = types.SimpleNamespace(raw=source[0], w=3840, h=2160,
                                         color=color, advance=lambda i:None)
            for i in range(240 if not quality else 3):
                if name == 'patch_transform':
                    values = fp.patch_inputs(p, i, original, generated, 240)
                elif name == 'blend':
                    values = [fp.blend(o, g, m, 'multiband', 3)]
                elif name == 'color_conversion':
                    values = [fp.yuv_to_bgr(*(c[400:1040,400:1040] for c in source[0]), color),
                              fp.yuv_to_bgr(*returned[0], gi['color']),
                              fp.bgr_to_yuv420(o, color)]
                else:
                    values = [fp.Reader.passthrough420(fake, i)[0]]
                if quality:
                    for value in values:
                        hashes.update(value if isinstance(value, bytearray) else np.ascontiguousarray(value))
        return hashes.hexdigest() if quality else None

    names = ['decoder_wait'] if variant else ['decoder_wait', 'patch_transform', 'blend', 'color_conversion', 'source_chroma_layout']
    report['variant'] = variant
    for name in names:
        LUTS.clear()
        with candidate('baseline'):
            a = workload(name, True)
        with candidate(variant or name):
            b = workload(name, True)
        assert a == b, (name, a, b)
        report['quality'][name] = dict(exact=True, reference_sha256=a, candidate_sha256=b)
        # Warm-up then A/A control and alternating AB/BA. No hash in timings.
        workload(name)
        aa = []
        for _ in range(1 if variant else 3):
            row = []
            for _ in range(2):
                t = time.perf_counter(); workload(name); row.append(time.perf_counter()-t)
            aa.append(row)
        rows = []
        for i in range(pairs):
            row = dict(order=['A', 'B'] if i%2 == 0 else ['B', 'A'])
            for mode in row['order']:
                LUTS.clear()
                with candidate((variant or name) if mode == 'B' else 'baseline'):
                    t = time.perf_counter(); workload(name)
                    row[mode] = time.perf_counter()-t
            rows.append(row)
            print(name, i, json.dumps(row), flush=True)
        sa, sb = (stats([r[k] for r in rows]) for k in ['A', 'B'])
        report['stages'][name] = dict(aa=aa, raw=rows, A=sa, B=sb,
                                     saving_percent=100*(1-sb['median']/sa['median']))
        save(out/'experiments.json', report)
    return report


def config(source, returned):
    # Full 10-second source, real production functions, non-identity color.
    return dict(schema_version=1, source=str(source), source_size=[3840, 2160], fps=24,
                patches=[dict(generated=str(returned), target=[0, 240],
                              crop=[400, 400, 640, 640], work_size=[640, 640],
                              matte=dict(region=[100, 100, 400, 400], feather=32),
                              blend='multiband', levels=3,
                              gain_bgr=[1.01, .99, 1.0], offset_bgr=[1, 0, -1])])


def trial(cfg, out, profile=False):
    fp._PROBED.clear()
    fp._PASSES.clear()
    fp._MATTES.clear()
    stages = Stages()
    start = time.perf_counter()
    with stages.enabled() if profile else contextlib.nullcontext():
        info = fp.validate_source(cfg)
        assert info['frames'] == 240
        plan = fp.merge_plan(cfg, info)
        fp.process(cfg, info, 'merge', out, plan, preset='ultrafast')
        verified = fp.verify(cfg['source'], str(out),
                             dict(size=info['size'], frames=info['frames'],
                                  fps=info['fps'], audio=info['audio']), True)
    elapsed = time.perf_counter()-start
    return dict(wall=elapsed, stages=dict(stages.seconds), calls=dict(stages.calls),
                sha256=fp.sha(out), output_bytes=out.stat().st_size,
                frames=verified['output']['frames'],
                audio_payload_sha256=verified['audio_payload_sha256'])


def cli_trial(config_path, out, selected, native_dir):
    # A fresh task directory prevents an earlier A run warming B's probe cache.
    run_dir = out.with_suffix('')
    run_dir.mkdir(exist_ok=False)
    out = run_dir/out.name
    bootstrap = '''import contextlib,json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import top5_experiments as e
e.cv2.setNumThreads(4)
e.fp.NATIVE_DIR=Path(sys.argv[2])
selected=json.loads(sys.argv[3]); cfg=sys.argv[4]; out=sys.argv[5]
with contextlib.ExitStack() as stack:
    for name in selected: stack.enter_context(e.candidate(name))
    sys.argv=['flow_patch.py','merge','--config',cfg,'--output',out,'--preview']
    e.fp.main()
'''
    command = [sys.executable, '-c', bootstrap, str(ROOT/'tools'), str(native_dir),
               json.dumps(selected), str(config_path), str(out)]
    t = time.perf_counter()
    p = subprocess.run(command, capture_output=True, text=True, encoding='utf-8')
    elapsed = time.perf_counter()-t
    out.with_suffix('.stdout.txt').write_text(p.stdout, encoding='utf-8')
    out.with_suffix('.stderr.txt').write_text(p.stderr, encoding='utf-8')
    if p.returncode:
        raise RuntimeError(p.stdout+p.stderr)
    r = json.loads(out.with_suffix('.report.json').read_text(encoding='utf-8'))
    assert r['decode_pass'] and r['output']['frames'] == 240
    assert r['output']['size'] == [3840,2160] and r['scope']['frames'] == [0,240]
    assert fp.sha(out) == r['output_sha256']
    return dict(wall=elapsed, output=str(out), sha256=r['output_sha256'], output_bytes=out.stat().st_size,
                frames=r['output']['frames'], audio_payload_sha256=r['audio_payload_sha256'])


def quality_cases(out, selected):
    """Full encode/strict verify on geometry, masks, fades and frame-map controls."""
    import importlib.util
    spec = importlib.util.spec_from_file_location('top5_smoke_fixture', ROOT/'flow-video-patch/tests/smoke_test.py')
    sm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sm)
    sm.Smoke.setUpClass()
    results = []
    # Start the fixed worker pools before comparing handle counts across tasks.
    fp.in_bands(128, lambda a,b: None)
    gate = threading.Event()
    workers = [fp._POOL.submit(gate.wait) for _ in range(fp.process_threads())]
    gate.set()
    for worker in workers:
        worker.result()
    cv2.pyrDown(np.zeros((1080,1920,3), np.float32))
    def handles():
        if sys.platform != 'win32':
            return None
        import ctypes
        from ctypes import wintypes
        k = ctypes.WinDLL('kernel32', use_last_error=True)
        k.GetCurrentProcess.restype = wintypes.HANDLE
        k.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        n = wintypes.DWORD()
        if not k.GetProcessHandleCount(k.GetCurrentProcess(), ctypes.byref(n)):
            raise ctypes.WinError(ctypes.get_last_error())
        return n.value
    before_failure = handles()
    if sys.platform == 'win32' and 'pipe_capacity' in selected:
        def fail_start(*a, **kw):
            raise OSError('intentional child-start failure')
        try:
            windows_reader_pipe(fail_start, [], (), {})
        except OSError as exc:
            assert str(exc) == 'intentional child-start failure'
        else:
            raise AssertionError('injected failure was swallowed')
        assert handles() == before_failure, 'pipe handles leaked on child-start failure'
    try:
        t = sm.Smoke()
        common = dict(gain_bgr=[.9,1.02,1.2], offset_bgr=[3,-1,.5])
        mapping = list(range(23))+[22]
        mapping[5] = None
        cases = {
            'alpha_dynamic_protect_fade_affine_none_repeat': t.patch(**common, frame_map=mapping,
                fade_frames=3, source_to_generated_affine=[1,0,.25,0,1,-.5],
                matte=dict(region=[dict(frame=0,value=[16,16,64,96]), dict(frame=23,value=[18,16,64,96])],
                           feather=8, protect=[[16,72,64,24]])),
            'multiband_fade': t.patch(**common, blend='multiband', levels=2, fade_frames=3,
                                      matte=dict(region=[16,16,64,96],feather=8)),
            'scaled_work_size': t.patch(**common, work_size=[48,64],
                                       matte=dict(region=[8,8,32,48],feather=4))}
        for name, p in cases.items():
            cfg = fp.load(t.config(name, patches=[p]))
            info = fp.validate_source(cfg)
            row = dict(case=name)
            for mode in ['A','B']:
                path = out/f'{name}-{mode}.mp4'
                with contextlib.ExitStack() as stack:
                    if mode == 'B':
                        for stage in selected:
                            stack.enter_context(candidate(stage))
                    plan = fp.merge_plan(cfg,info)
                    fp.process(cfg, info, 'merge', path, plan, preset='ultrafast')
                    verified = fp.verify(cfg['source'], str(path),
                        dict(size=info['size'],frames=24,fps=info['fps'],audio=info['audio']), True)
                row[mode] = dict(sha256=fp.sha(path),frames=24,
                                 audio=verified['audio_payload_sha256'])
            assert row['A'] == row['B'], name
            row['handles_after_pair'] = handles()
            results.append(row)
        if sys.platform == 'win32' and 'pipe_capacity' in selected:
            assert results[-1]['handles_after_pair'] <= results[0]['handles_after_pair'], 'pipe handles grow across tasks'
        save(out/'quality.json', dict(selected=selected, exact=True, cases=results,
                                     failed_start_handle_count=before_failure,
                                     failed_start_cleanup='pass' if 'pipe_capacity' in selected else 'not_run'))
    finally:
        sm.Smoke.tearDownClass()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--returned', type=Path)
    parser.add_argument('--native-dir', type=Path)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--mode', choices=['fixture', 'profile', 'experiments', 'transport', 'combined', 'quality'], default='profile')
    parser.add_argument('--pairs', type=int, default=5)
    parser.add_argument('--selected', nargs='+', default=[])
    args = parser.parse_args()
    if args.pairs < 1:
        parser.error('--pairs must be positive')
    if args.mode == 'quality' and not args.native_dir:
        parser.error('--native-dir is required for quality controls')
    if args.mode not in ('fixture','quality') and not all((args.source, args.returned, args.native_dir)):
        parser.error('--source, --returned and --native-dir are required for measurements')
    args.out.mkdir(parents=True, exist_ok=False)
    if args.mode == 'fixture':
        source, returned = args.out/'source.mp4', args.out/'return.mp4'
        cmds = [[fp.FFMPEG, '-v','error','-n','-f','lavfi','-i','testsrc2=size=3840x2160:rate=24',
                 '-f','lavfi','-i','sine=frequency=440:sample_rate=48000','-t','10',
                 '-c:v','libx264','-threads','4','-preset','ultrafast','-crf','20',
                 '-g','48','-bf','3','-pix_fmt','yuv420p','-c:a','aac',str(source)],
                [fp.FFMPEG,'-v','error','-n','-protocol_whitelist','file','-i',str(source.resolve()),'-vf','crop=640:640:400:400',
                 '-an','-c:v','libx264','-threads','4','-preset','ultrafast',str(returned)]]
        for cmd in cmds:
            subprocess.run(cmd, check=True, capture_output=True, timeout=600)
        save(args.out/'fixture.json', dict(commands=cmds, source_sha256=fp.sha(source),
                                          returned_sha256=fp.sha(returned)))
        return
    os.environ.update(FLOW_KERNELS='native', FLOW_PROCESS_THREADS='4',
                      FLOW_ENCODE_THREADS='4')
    cv2.setNumThreads(4)
    fp.NATIVE_DIR = args.native_dir.resolve()
    assert fp.kernels() is not None
    if args.mode == 'quality':
        quality_cases(args.out, args.selected or ['patch_transform'])
        return
    cfg = config(args.source.resolve(), args.returned.resolve())
    save(args.out/'config.json', cfg)
    if args.mode in ('experiments', 'transport'):
        experiments(cfg, args.out, args.pairs, 'pipe_capacity' if args.mode == 'transport' else None)
        return
    report = dict(commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT,
                                                 text=True).strip(),
                  environment=dict(python=platform.python_version(), numpy=np.__version__,
                                   opencv=cv2.__version__, opencv_threads=cv2.getNumThreads(),
                                   native_sha256=fp.sha(fp.native_library()),
                                   ffmpeg=fp.tool_version(fp.FFMPEG),
                                   variables={k:v for k,v in os.environ.items() if k.startswith('FLOW_')}),
                  scope='Synthetic 4K/240 frames, one 640px multiband return; warm OS cache; no publication/report assembly',
                  runs=[])
    if args.mode == 'combined':
        assert args.selected, '--selected must name the individually validated stages'
        report['selected'] = args.selected
        report['scope'] = 'Complete actual main() --preview at 4K/240 frames in fresh Python processes and empty task probe caches (warm OS cache), includes input prefetch, source/return/output hashes, strict output/audio checks, tool inventory, report and publication. No resolution/duration changes.'
        for i in range(args.pairs):
            row = dict(order=['A','B'] if i%2 == 0 else ['B','A'])
            for mode in row['order']:
                LUTS.clear()
                row[mode] = cli_trial((args.out/'config.json').resolve(),
                                      (args.out/f'{mode}-{i}.mp4').resolve(),
                                      args.selected if mode == 'B' else [], args.native_dir.resolve())
                print(i, mode, row[mode]['wall'], flush=True)
            assert row['A']['sha256'] == row['B']['sha256'], 'encoded output changed'
            assert row['A']['audio_payload_sha256'] == row['B']['audio_payload_sha256']
            report['runs'].append(row)
            save(args.out/'combined.json', report)
        report['A'] = stats([r['A']['wall'] for r in report['runs']])
        report['B'] = stats([r['B']['wall'] for r in report['runs']])
        report['saving_percent'] = 100*(1-report['B']['median']/report['A']['median'])
        with contextlib.ExitStack() as stack:
            for name in args.selected:
                stack.enter_context(candidate(name))
            report['candidate_profile'] = trial(cfg, args.out/'candidate-profile.mp4', True)
        assert report['candidate_profile']['sha256'] == report['runs'][0]['A']['sha256']
        save(args.out/'combined.json', report)
        print(json.dumps(dict(A=report['A'], B=report['B'], saving_percent=report['saving_percent'])), flush=True)
        return
    for i, instrument in enumerate([False, True, True, False]):
        result = trial(cfg, args.out/f'profile-{i}.mp4', instrument)
        result['instrumented'] = instrument
        report['runs'].append(result)
        print(json.dumps(result), flush=True)
        save(args.out/'profile.json', report)
    profiled = [r for r in report['runs'] if r['instrumented']]
    report['ranking'] = sorted([dict(stage=k, seconds=statistics.median(r['stages'][k] for r in profiled))
                                for k in profiled[0]['stages']], key=lambda x:-x['seconds'])
    report['top5'] = report['ranking'][:5]
    report['instrumentation_overhead_percent'] = 100*(statistics.median(r['wall'] for r in profiled)/statistics.median(r['wall'] for r in report['runs'] if not r['instrumented'])-1)
    assert len({r['sha256'] for r in report['runs']}) == 1
    save(args.out/'profile.json', report)
    print(json.dumps(dict(top5=report['top5'], overhead=report['instrumentation_overhead_percent'])), flush=True)


if __name__ == '__main__':
    main()
