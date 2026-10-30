#!/usr/bin/env python3
"""Portable Flow patch preparation/composition. No downloads or uploads.

All frame intervals are zero-based [start,end). All rectangles are x,y,w,h.
Paths in JSON are relative to that JSON. See references/parameters.md.
"""
import argparse
import bisect
from concurrent.futures import Future, ProcessPoolExecutor, ThreadPoolExecutor
import contextlib
import ctypes
import functools
import importlib.metadata
from fractions import Fraction
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading

import cv2
import numpy as np

FFMPEG = os.environ.get('FLOW_FFMPEG', 'ffmpeg')
FFPROBE = os.environ.get('FLOW_FFPROBE', 'ffprobe')


def run(argv):
    return subprocess.check_output([str(x) for x in argv], stderr=subprocess.PIPE,
                                   timeout=float(os.environ.get('FLOW_COMMAND_TIMEOUT', '600')))


def require(condition, message):
    if not condition:
        raise ValueError(message)


def local_media(path):
    """Only regular local files; resolve leading '-' before passing to native tools."""
    path = Path(path).expanduser().resolve()
    require(path.is_file(), 'Media input must be an existing regular local file')
    return str(path)


def file_input(path):
    # Input-specific: nested HTTP/TCP/UDP/crypto/data/pipe protocols are forbidden.
    return ['-protocol_whitelist', 'file', '-i', local_media(path)]


def checked_size(size):
    require(isinstance(size, (list, tuple)) and len(size) == 2 and
            all(type(v) is int and v > 0 for v in size), 'Invalid pixel dimensions')
    limit = int(os.environ.get('FLOW_MAX_PIXELS', '67108864'))
    require(limit > 0 and size[0]*size[1] <= limit, 'Pixel budget exceeded; see SECURITY.md')
    return size


def output_path(path):
    # Resolve parents, never follow a dangling leaf symlink into another directory.
    path = Path(path).expanduser().absolute()
    require(not path.is_symlink(), 'Output symlinks are not allowed')
    return path.parent.resolve()/path.name


@contextlib.contextmanager
def private_output(path, binary=False):
    """Exclusive creation, private POSIX mode, cleanup only our own failed file."""
    path = output_path(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    identity = os.fstat(fd)
    try:
        with os.fdopen(fd, 'wb' if binary else 'w',
                       **({} if binary else {'encoding': 'utf-8'})) as stream:
            yield stream
    except BaseException:
        try:
            current = path.lstat()
            if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                path.unlink()
        except FileNotFoundError:
            pass
        raise


def stop_process(process):
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


# Explicit YUV<->BGR conversion. FFmpeg's implicit swscale paths truncate (about -1 level
# per decode) and encode untagged BT.601, which shifts colour on every pass.
KR_KB = {'bt709': (0.2126, 0.0722), 'bt601': (0.299, 0.114)}
MATRIX_CODES = {'bt709': 1, 'bt470bg': 5, 'smpte170m': 6}
PRIMARY_CODES = {'bt709': 1, 'bt470m': 4, 'bt470bg': 5, 'smpte170m': 6, 'smpte240m': 7}
TRANSFER_CODES = {'bt709': 1, 'gamma22': 4, 'gamma28': 5, 'smpte170m': 6, 'smpte240m': 7,
                  'iec61966-2-1': 13}


def color_info(s):
    """Matrix/range of a video stream; untagged follows players (>=720 lines BT.709)."""
    if s.get('pix_fmt') in ('rgb24', 'bgr24'):
        space, assumed = ('bt709' if s['height'] >= 720 else 'smpte170m'), True
        full = False
    else:
        space = s.get('color_space')
        assumed = space in (None, 'unknown')
        if assumed:
            space = 'bt709' if s['height'] >= 720 else 'smpte170m'
        require(space in MATRIX_CODES,
                f'Unsupported color matrix {space}; convert to BT.709 or BT.601 explicitly')
        full = s.get('color_range') == 'pc' or s['pix_fmt'].startswith('yuvj')
    default = 'bt709' if space == 'bt709' else 'smpte170m'
    return {'matrix': 'bt709' if space == 'bt709' else 'bt601', 'matrix_tag': space,
            'range': 'pc' if full else 'tv', 'assumed': assumed,
            'primaries': s.get('color_primaries') if s.get('color_primaries') in PRIMARY_CODES else default,
            'transfer': s.get('color_transfer') if s.get('color_transfer') in TRANSFER_CODES else default,
            'rgb_input': s.get('pix_fmt') in ('rgb24', 'bgr24')}


def stream_color(path):
    s = json.loads(run([FFPROBE, '-v', 'error', '-select_streams', 'v:0', '-show_streams',
                        '-of', 'json', '-protocol_whitelist', 'file', local_media(path)]))['streams'][0]
    return color_info(s)


def process_threads():
    # Row bands are converted in parallel; NumPy releases the GIL, results are identical.
    value = os.environ.get('FLOW_PROCESS_THREADS', '0')
    require(value.isdigit() and int(value) <= 64, 'FLOW_PROCESS_THREADS must be 0 (auto) to 64')
    return int(value) or min(os.cpu_count() or 1, 32)


_POOL = None


def in_bands(height, work):
    """Run work(a, b) over even-aligned row bands covering [0, height)."""
    global _POOL
    if _POOL is None:
        _POOL = ThreadPoolExecutor(process_threads())
    bands = 2*process_threads()
    step = max(2, (-(-height // bands) + 1) & ~1)
    list(_POOL.map(lambda a: work(a, min(a+step, height)), range(0, height, step)))


def _yuv_to_bgr(y, u, v, color):
    kr, kb = KR_KB[color['matrix']]
    y, u, v = (c.astype(np.float32) for c in (y, u, v))
    if color['range'] == 'tv':
        y, u, v = (y-16)*(255/219), (u-128)*(255/224), (v-128)*(255/224)
    else:
        u, v = u-128, v-128
    r = y+2*(1-kr)*v
    b = y+2*(1-kb)*u
    g = (y-kr*r-kb*b)/(1-kr-kb)
    return np.clip(np.rint(np.stack([b, g, r], -1)), 0, 255).astype(np.uint8)


def yuv_to_bgr(y, u, v, color):
    out = np.empty(y.shape+(3,), np.uint8)
    k = kernels()
    def work(a, b):
        if k:
            k.yuv_to_bgr(y[a:b], u[a:b], v[a:b], out[a:b], color)
        else:
            out[a:b] = _yuv_to_bgr(y[a:b], u[a:b], v[a:b], color)
    in_bands(y.shape[0], work)
    return out


def _bgr_to_yuv420(bgr, color):
    kr, kb = KR_KB[color['matrix']]
    f = bgr.astype(np.float32)
    b, g, r = f[..., 0], f[..., 1], f[..., 2]
    y = kr*r+(1-kr-kb)*g+kb*b
    u, v = (b-y)/(2*(1-kb)), (r-y)/(2*(1-kr))
    if color['range'] == 'tv':
        y, u, v = 16+y*(219/255), u*(224/255), v*(224/255)
    # 2x2 chroma average, centred like the encoder's default siting.
    u = (u[0::2, 0::2]+u[1::2, 0::2]+u[0::2, 1::2]+u[1::2, 1::2])/4+128
    v = (v[0::2, 0::2]+v[1::2, 0::2]+v[0::2, 1::2]+v[1::2, 1::2])/4+128
    return (np.clip(np.rint(c), 0, 255).astype(np.uint8) for c in (y, u, v))


class NativeKernels:
    """Optional Rust kernels (native/, standard library only), called through ctypes.

    ctypes releases the GIL, so in_bands still spreads the rows over threads."""
    ABI = 1

    def __init__(self, path):
        self.lib = ctypes.CDLL(str(path))
        self.lib.fk_abi_version.restype = ctypes.c_uint32
        require(self.lib.fk_abi_version() == self.ABI, 'Native kernel ABI mismatch; rebuild native/')
        P, S, N, F = ctypes.c_void_p, ctypes.c_ssize_t, ctypes.c_size_t, ctypes.c_double
        sig = {'fk_yuv_to_bgr': [P, S]*4+[N, N, F, F, ctypes.c_int],
               'fk_bgr_to_yuv420': [P, S]*4+[N, N, F, F, ctypes.c_int],
               'fk_chroma_avg': [P, S]*2+[N, N], 'fk_alpha': [P, S]*4+[N, N],
               'fk_level': [P, S]*7+[N, N], 'fk_finish': [P, S]*4+[N, N],
               'fk_diff64': [P, S]*3+[N, N], 'fk_apply64': [P, S]*4+[N, N]}
        for name, args in sig.items():
            getattr(self.lib, name).argtypes, getattr(self.lib, name).restype = args, None

    @staticmethod
    def arg(a, dtype, channels=1):
        """Base pointer and row stride (elements) of a row-major view with packed pixels."""
        require(a.dtype == dtype and a.ndim == (2 if channels == 1 else 3), 'Native kernel array type')
        item = a.itemsize
        packed = a.strides[-1] == item and (channels == 1 or a.strides[1] == channels*item)
        require(packed and a.strides[0] % item == 0, 'Native kernel needs packed rows')
        return [a.ctypes.data, a.strides[0]//item]

    def yuv_to_bgr(self, y, u, v, out, color):
        kr, kb = KR_KB[color['matrix']]
        u8 = np.uint8
        self.lib.fk_yuv_to_bgr(*self.arg(y, u8), *self.arg(u, u8), *self.arg(v, u8), *self.arg(out, u8, 3),
                               y.shape[1], y.shape[0], kr, kb, int(color['range'] == 'tv'))

    def bgr_to_yuv420(self, bgr, Y, U, V, color):
        kr, kb = KR_KB[color['matrix']]
        u8 = np.uint8
        self.lib.fk_bgr_to_yuv420(*self.arg(bgr, u8, 3), *self.arg(Y, u8), *self.arg(U, u8), *self.arg(V, u8),
                                  bgr.shape[1], bgr.shape[0], kr, kb, int(color['range'] == 'tv'))

    def chroma_avg(self, src, dst):
        self.lib.fk_chroma_avg(*self.arg(src, np.uint8), *self.arg(dst, np.uint8), src.shape[1], src.shape[0])

    def alpha(self, o, p, m, out):
        self.lib.fk_alpha(*self.arg(o, np.uint8, 3), *self.arg(p, np.uint8, 3), *self.arg(m, np.float32),
                          *self.arg(out, np.float32, 3), m.shape[1], m.shape[0])

    def level(self, ga, ua, gb, ub, uo, gm, out):
        f = np.float32
        self.lib.fk_level(*self.arg(ga, f, 3), *self.arg(ua, f, 3), *self.arg(gb, f, 3), *self.arg(ub, f, 3),
                          *self.arg(uo, f, 3), *self.arg(gm, f), *self.arg(out, f, 3), gm.shape[1], gm.shape[0])

    def finish(self, v, m, o, res):
        self.lib.fk_finish(*self.arg(v, np.float32, 3), *self.arg(m, np.float32), *self.arg(o, np.uint8, 3),
                           *self.arg(res, np.uint8, 3), m.shape[1], m.shape[0])

    def diff64(self, a, b, out):
        self.lib.fk_diff64(*self.arg(a, np.uint8, 3), *self.arg(b, np.uint8, 3), *self.arg(out, np.float64, 3),
                           a.shape[1], a.shape[0])

    def apply64(self, roi, d, support, out):
        self.lib.fk_apply64(*self.arg(roi, np.uint8, 3), *self.arg(d, np.float64, 3),
                            *self.arg(support.view(np.uint8), np.uint8), *self.arg(out, np.uint8, 3),
                            roi.shape[1], roi.shape[0])


NATIVE_DIR = Path(__file__).resolve().parent.parent/'native'
_KERNELS = []
_KERNELS_LOCK = threading.Lock()


def native_library():
    name = {'win32': 'flow_kernels.dll', 'darwin': 'libflow_kernels.dylib'}.get(sys.platform,
                                                                                'libflow_kernels.so')
    return NATIVE_DIR/'target'/'release'/name


def kernels():
    """Native kernels when built and verified bit-identical here, else None (NumPy paths).

    FLOW_KERNELS: auto (default, use if built), native (require), numpy (never)."""
    with _KERNELS_LOCK:
        if not _KERNELS:
            _init_kernels()
    return _KERNELS[0]


def _init_kernels():
    if not _KERNELS:
        mode = os.environ.get('FLOW_KERNELS', 'auto')
        require(mode in ('auto', 'native', 'numpy'), 'FLOW_KERNELS must be auto, native or numpy')
        path = native_library()
        if mode == 'numpy' or not path.is_file():
            require(mode != 'native', f'FLOW_KERNELS=native but {path.name} is not built; see references/native-kernels.md')
            _KERNELS.append(None)
        else:
            k = NativeKernels(path)
            native_self_check(k)
            _KERNELS.append(k)


def native_self_check(k):
    """Refuse a library whose results differ from the NumPy expressions by a single bit."""
    rng = np.random.default_rng(1)
    h, w = 34, 46
    u8 = lambda *s: rng.integers(0, 256, s, dtype=np.uint8)
    f32 = lambda *s: (rng.random(s, np.float32)*300-20).astype(np.float32)
    planes, bgr = [u8(h, w) for _ in range(3)], u8(h, w, 3)
    m = rng.random((h, w), np.float32); m[::5] = 0; m[1::7] = 1
    for matrix in ('bt709', 'bt601'):
        for rng_name in ('tv', 'pc'):
            color = {'matrix': matrix, 'range': rng_name}
            out = np.empty((h, w, 3), np.uint8); k.yuv_to_bgr(*planes, out, color)
            Y, U, V = np.empty((h, w), np.uint8), np.empty((h//2, w//2), np.uint8), np.empty((h//2, w//2), np.uint8)
            k.bgr_to_yuv420(bgr, Y, U, V, color)
            ref = list(_bgr_to_yuv420(bgr, color))
            ok = (np.array_equal(out, _yuv_to_bgr(*planes, color)) and
                  all(np.array_equal(x, r) for x, r in zip((Y, U, V), ref)))
            require(ok, 'Native colour kernels differ from NumPy; rebuild or set FLOW_KERNELS=numpy')
    c = planes[1].astype(np.uint16); s = c[0::2, 0::2]+c[1::2, 0::2]+c[0::2, 1::2]+c[1::2, 1::2]
    q, r = s >> 2, s & 3
    avg = np.empty((h//2, w//2), np.uint8); k.chroma_avg(planes[1], avg)
    o, p = u8(h, w, 3), u8(h, w, 3)
    al = np.empty((h, w, 3), np.float32); k.alpha(o, p, m, al)
    ga, ua, gb, ub, uo = (f32(h, w, 3) for _ in range(5))
    lv = np.empty((h, w, 3), np.float32); k.level(ga, ua, gb, ub, uo, m, lv)
    mm = m[:, :, None]
    fin = np.empty((h, w, 3), np.uint8); k.finish(ga, m, o, fin)
    ref_fin = np.clip(ga, 0, 255).round().astype(np.uint8); ref_fin[m == 0] = o[m == 0]
    d64 = np.empty((h, w, 3), float); k.diff64(o, p, d64)
    delta = rng.normal(0, 40, (h, w, 3)); sup = m > .3
    ap = np.empty((h, w, 3), np.uint8); k.apply64(o, delta, sup, ap)
    dd = delta.copy(); dd[~sup] = 0
    checks = [(avg, q+((r > 2) | ((r == 2) & (q & 1 == 1)))),
              (al, o.astype(np.float32)*(1-mm)+p.astype(np.float32)*mm),
              (lv, uo+(ga-ua)*(1-mm)+(gb-ub)*mm), (fin, ref_fin),
              (d64, o.astype(float)-p.astype(float)),
              (ap, np.clip(o.astype(float)+dd, 0, 255).round().astype(np.uint8))]
    require(all(np.array_equal(a, b) for a, b in checks),
            'Native blend kernels differ from NumPy; rebuild or set FLOW_KERNELS=numpy')


PREFETCH = 3


class FrameWriter:
    """Writes encoder input in a thread so encoding overlaps the next frame's work."""
    def __init__(self, pipe):
        self.pipe, self.error = pipe, None
        self.items = queue.Queue(PREFETCH)
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        while (item := self.items.get()) is not None:
            if self.error is None:
                try:
                    self.pipe.write(item)
                except (OSError, ValueError) as e:
                    self.error = e

    def write(self, buf):
        if self.error is not None:
            raise self.error
        self.items.put(buf)

    def finish(self):
        self.items.put(None)
        self.thread.join()
        return self.error


def planes420(buf, w, h):
    planes = np.frombuffer(buf, np.uint8)
    return (buf, planes[:h*w].reshape(h, w), planes[h*w:h*w*5//4].reshape(h//2, w//2),
            planes[h*w*5//4:].reshape(h//2, w//2))


def bgr_to_yuv420(bgr, color):
    h, w = bgr.shape[:2]
    buf, Y, U, V = planes420(bytearray(h*w*3//2), w, h)
    k = kernels()
    def work(a, b):
        if k:
            k.bgr_to_yuv420(bgr[a:b], Y[a:b], U[a//2:b//2], V[a//2:b//2], color)
        else:
            Y[a:b], U[a//2:b//2], V[a//2:b//2] = _bgr_to_yuv420(bgr[a:b], color)
    in_bands(h, work)
    return buf


_PROBED = {}


PROBE_CACHE_FORMAT = 2  # 2: probe info gained time_base


def cached_probe(path, cache):
    """Probe result reused while the file's path, size, mtime and inode are unchanged."""
    st = os.stat(path)
    key = hashlib.sha256(json.dumps([PROBE_CACHE_FORMAT, path, st.st_size, st.st_mtime_ns,
                                     st.st_ino]).encode()).hexdigest()
    entry = cache/(key+'.json')
    with contextlib.suppress(OSError, ValueError):
        return json.loads(entry.read_text(encoding='utf-8'))
    info = _probe(path)
    with contextlib.suppress(OSError):
        cache.mkdir(mode=0o700, exist_ok=True)
        with private_output(entry.with_suffix(f'.{os.getpid()}.tmp')) as f:
            json.dump(info, f)
        os.replace(entry.with_suffix(f'.{os.getpid()}.tmp'), entry)
    return info


def prefetch_probes(paths, cache=None):
    """Start probing several inputs at once; probe() then picks up the pending result.
    With a cache folder (check-only runs), unchanged inputs skip their full decoding pass."""
    pool = ThreadPoolExecutor(max(1, len(set(paths))))
    for path in dict.fromkeys(local_media(p) for p in paths):
        _PROBED[path] = pool.submit(_probe, path) if cache is None else pool.submit(cached_probe, path, cache)
    pool.shutdown(wait=False)


_PASSES = {}


def decode_pass(path, strict=False):
    """One full decode giving per-frame timestamps (seconds, demuxer time base, no rounding) and
    128-bit murmur3 hashes. strict stops at the first decode error (-xerror), as output checks need,
    and also decodes every audio stream: a payload hash alone does not prove the audio decodes."""
    argv = [FFMPEG, '-v', 'error', *(['-xerror'] if strict else []), *file_input(path), '-map', '0:v:0',
            *(['-map', '0:a?'] if strict else []), '-fps_mode', 'passthrough', '-enc_time_base:v', 'demux',
            '-f', 'framehash', '-hash', 'murmur3', '-']
    done = subprocess.run(argv, capture_output=True, timeout=float(os.environ.get('FLOW_COMMAND_TIMEOUT', '600')))
    if done.returncode:
        raise subprocess.CalledProcessError(done.returncode, argv, done.stdout, done.stderr)
    # Some FFmpeg builds (6.1) report a failed decoder thread but still exit 0 under -xerror.
    require(not (strict and done.stderr.strip()),
            'Decode errors: '+done.stderr.decode(errors='replace').strip()[-1000:])
    lines = done.stdout.decode().splitlines()
    tb = next(Fraction(line.split(':', 1)[1].strip()) for line in lines if line.startswith('#tb 0:'))
    rows = [[x.strip() for x in line.split(',')] for line in lines if line and not line.startswith('#')]
    rows = [r for r in rows if r[0] == '0']  # output stream 0 is the video
    result = ([float(int(r[2])*tb) for r in rows], [r[5] for r in rows])
    _PASSES[local_media(path)] = result
    return result


def forget(path):
    """Drop this process's cached probe and decoding pass of a file that is about to be replaced."""
    key = str(Path(path).expanduser().resolve())
    _PROBED.pop(key, None)
    _PASSES.pop(key, None)


def probe(path, strict=False):
    path = local_media(path)
    if path in _PROBED:
        return _PROBED[path].result()
    return _probe(path, strict)


def _probe(path, strict=False):
    # Headers only; the single decoding pass below counts frames and reads timestamps.
    data = json.loads(run([FFPROBE, '-v', 'error', '-show_streams',
                           '-of', 'json', '-protocol_whitelist', 'file', path]))
    videos = [s for s in data['streams'] if s['codec_type'] == 'video']
    require(len(videos) == 1, 'Require exactly one video stream')
    s = videos[0]
    checked_size([s['width'], s['height']])
    require(s.get('color_transfer') not in ('smpte2084', 'arib-std-b67'),
            'HDR is unsupported; explicitly convert to SDR first')
    require(s.get('pix_fmt') in ('yuv420p', 'yuvj420p', 'yuv422p', 'yuvj422p',
                                'yuv444p', 'yuvj444p', 'rgb24', 'bgr24'),
            'Only supported 8-bit SDR pixel formats are accepted')
    require(not int(s.get('tags', {}).get('rotate', 0)) and
            not any(int(d.get('rotation', 0)) for d in s.get('side_data_list', [])),
            'Bake rotation into pixels first')
    require(s.get('sample_aspect_ratio', '1:1') in ('1:1', 'N/A'), 'Require square pixels')
    fps = Fraction(s['avg_frame_rate'])
    require(fps > 0 and fps == Fraction(s['r_frame_rate']),
            f"Require constant frame rate (avg_frame_rate {s['avg_frame_rate']}, r_frame_rate {s['r_frame_rate']}, "
            f"time_base {s.get('time_base')}, duration_ts {s.get('duration_ts')}, nb_frames {s.get('nb_frames')})")
    # Equal rate fields alone do not prove CFR. Check decoded timestamps; the same decoding pass
    # also hashes every frame for later bit-exact comparisons.
    times = decode_pass(path, strict)[0]
    require(len(times) > 0, 'No decoded frames')
    # A coarse time base (e.g. MKV milliseconds) rounds each step by up to one tick.
    tick = float(Fraction(s.get('time_base', '0/1'))) if s.get('time_base') else 0.0
    step_tolerance = max(0.0001, float(1/fps)*0.002, tick*1.01)
    require(all(abs((b-a)-float(1/fps)) <= step_tolerance
                for a, b in zip(times, times[1:])), 'VFR or timestamp gaps; normalize explicitly')
    require(abs(times[0]) < 0.001, 'Video must start near timestamp zero; remux explicitly')
    audio = next((a for a in data['streams'] if a['codec_type'] == 'audio'), None)
    require(audio is None or abs(float(audio.get('start_time', 0))) < 0.001,
            'Audio starts at a nonzero timestamp; normalize offsets explicitly')
    # Frame n at n/fps (within a quarter frame) lets readers seek straight to a frame.
    grid = all(abs(t-n/fps) < 0.25/fps for n, t in enumerate(times))
    counts = {kind: sum(x['codec_type'] == kind for x in data['streams'])
              for kind in ('audio', 'subtitle', 'data', 'attachment')}
    chroma = {'yuv420p': '4:2:0', 'yuvj420p': '4:2:0', 'yuv422p': '4:2:2', 'yuvj422p': '4:2:2',
              'yuv444p': '4:4:4', 'yuvj444p': '4:4:4'}.get(s['pix_fmt'], 'rgb')
    return {'size': [s['width'], s['height']], 'fps': str(fps), 'frames': len(times),
            'audio': audio is not None, 'color': color_info(s), 'on_frame_grid': grid,
            'chroma': chroma, 'streams': counts, 'codec': s.get('codec_name'),
            'time_base': s.get('time_base')}


def output_notices(info, mode):
    """What the output cannot carry from this source; reported, never silently dropped."""
    notes = []
    if info['chroma'] != '4:2:0':
        notes.append(f"Source chroma is {info['chroma']}; the H.264 output is 4:2:0, so colour "
                     'detail is subsampled everywhere, not only in patches')
    streams = info['streams']
    if streams['audio'] > 1:
        notes.append(f"Source has {streams['audio']} audio tracks; only the first is "
                     + ('copied' if mode == 'merge' else 're-encoded'))
    dropped = [f'{n} {k}' for k, n in streams.items() if k != 'audio' and n]
    if dropped:
        notes.append('Not carried to the output: '+', '.join(dropped)+' stream(s)')
    return notes


def sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1024*1024), b''):
            h.update(block)
    return h.hexdigest()


def audio_hash(path):
    return run([FFMPEG, '-v', 'error', *file_input(path), '-map', '0:a:0', '-c:a', 'copy',
                '-f', 'hash', '-hash', 'sha256', '-']).decode().strip()


def curve(value, frame):
    """A constant numeric vector, or linear keyframes [{frame,value}]."""
    require(isinstance(value, list) and len(value) > 0, 'Require a nonempty numeric vector')
    if isinstance(value[0], dict):
        require(all(a['frame'] < b['frame'] for a, b in zip(value, value[1:])),
                'Keyframes must be strictly increasing')
        require(all(len(k['value']) == len(value[0]['value']) for k in value),
                'Keyframe vector dimensions must agree')
        require(np.isfinite([k['value'] for k in value]).all(), 'Non-finite keyframe')
        return np.array([np.interp(frame, [k['frame'] for k in value],
                                  [k['value'][j] for k in value])
                         for j in range(len(value[0]['value']))])
    require(np.isfinite(value).all(), 'Non-finite vector')
    return np.array(value, dtype=float)


def rectangle(value, frame, size):
    vals = curve(value, frame)
    require(len(vals) == 4, 'Rectangle must contain x,y,w,h')
    x, y, w, h = (int(round(v)) for v in vals)
    require(x >= 0 and y >= 0 and w > 0 and h > 0 and x+w <= size[0] and y+h <= size[1],
            f'Rectangle outside parent at frame {frame}: {[x,y,w,h]} / {size}')
    return x, y, w, h


class Reader:
    """Sequential decoder; permits repeated indices, refuses backwards reads.

    start skips earlier frames: by seeking halfway between frames start-1 and start when fps is
    given (probe found every frame on the n/fps grid), otherwise by decoded frame number."""
    def __init__(self, path, size, color=None, start=0, fps=None):
        self.w, self.h = checked_size(size)
        self.color = color or stream_color(path)
        self.log = tempfile.TemporaryFile()
        fmt = 'bgr24' if self.color['rgb_input'] else 'yuv444p'
        # State the range on both sides: older FFmpeg (e.g. 6.1) otherwise squeezes full-range
        # (yuvj) input into limited range when converting to yuv444p.
        rng = 'pc' if self.color['range'] == 'pc' else 'tv'
        require(type(start) is int and start >= 0, 'Invalid reader start frame')
        seek = []
        if start and fps:
            seek = ['-ss', f'{float((start-Fraction(1, 2))/Fraction(fps)):.6f}']
        chain = [f'select=gte(n\\,{start})'] if start and not seek else []
        if self.color['rgb_input']:
            convert = (['-vf', ','.join(chain)] if chain else [])+['-pix_fmt', fmt]
        else:
            convert = ['-vf', ','.join(chain+[f'scale=in_range={rng}:out_range={rng},format={fmt}'])]
        self.p = subprocess.Popen([FFMPEG, '-v', 'error', '-noautorotate', *seek, *file_input(path),
                                   '-map', '0:v:0', '-an', '-fps_mode', 'passthrough',
                                   *convert, '-f', 'rawvideo', '-'],
                                  stdout=subprocess.PIPE, stderr=self.log)
        self.index = start-1
        self.last = None
        # Decode ahead in a thread; each frame gets its own buffer, so order and bytes are unchanged.
        self.frames = queue.Queue(PREFETCH)
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self._read_ahead, daemon=True)
        self.thread.start()

    def _read_ahead(self):
        try:
            while not self.stopping.is_set():
                blocks = bytearray(self.w*self.h*3)
                view, done = memoryview(blocks), 0
                while done < len(blocks):
                    n = self.p.stdout.readinto(view[done:])
                    if not n:
                        break
                    done += n
                if done < len(blocks):
                    break
                self._put(blocks)
        except (OSError, ValueError):
            pass
        self._put(None)

    def _put(self, item):
        while not self.stopping.is_set():
            try:
                self.frames.put(item, timeout=0.1)
                return
            except queue.Full:
                pass

    def advance(self, index):
        """Read up to index; raw planes are kept, BGR conversion happens on demand."""
        require(index >= self.index, 'Non-monotone mapping is unsupported')
        while self.index < index:
            blocks = self.frames.get() if self.thread.is_alive() or not self.frames.empty() else None
            if blocks is None:
                self.thread.join()
                raise ValueError(f'Decoder ended before frame {index}')
            if self.color['rgb_input']:
                self.raw = np.frombuffer(blocks, np.uint8).reshape(self.h, self.w, 3)
            else:
                self.raw = np.frombuffer(blocks, np.uint8).reshape(3, self.h, self.w)
            self.last = None
            self.index += 1

    def get(self, index):
        self.advance(index)
        if self.last is None:
            self.last = (self.raw if self.color['rgb_input'] else
                         yuv_to_bgr(*self.raw, self.color))
        return self.last

    def region(self, index, rect):
        """BGR of one rectangle; same values as the matching slice of get()."""
        self.advance(index)
        x, y, w, h = rect
        if self.color['rgb_input']:
            return self.raw[y:y+h, x:x+w].copy()
        return yuv_to_bgr(*(c[y:y+h, x:x+w] for c in self.raw), self.color)

    def passthrough420(self, index):
        """Output buffer and Y/U/V views holding this frame without an RGB round trip."""
        self.advance(index)
        if self.color['rgb_input']:
            return planes420(bgr_to_yuv420(self.raw, self.color), self.w, self.h)
        buf, Y, U, V = planes420(bytearray(self.w*self.h*3//2), self.w, self.h)
        y, u, v = self.raw
        k = kernels()
        def work(a, b):
            Y[a:b] = y[a:b]
            for src, dst in ((u, U), (v, V)):
                if k:
                    k.chroma_avg(src[a:b], dst[a//2:b//2])
                    continue
                c = src[a:b].astype(np.uint16)
                s = c[0::2, 0::2]+c[1::2, 0::2]+c[0::2, 1::2]+c[1::2, 1::2]
                # Integer form of rint(s/4): ties (remainder 2) go to the even quotient.
                q, r = s >> 2, s & 3
                dst[a//2:b//2] = q+((r > 2) | ((r == 2) & (q & 1 == 1)))
        in_bands(self.h, work)
        return buf, Y, U, V

    def close(self):
        self.stopping.set()
        # A decoder with unread frames is blocked writing to the pipe and would sit out the
        # terminate timeout (5 s); its output is no longer wanted, so stop it at once.
        if self.p.poll() is None:
            self.p.kill()
        self.p.wait(timeout=5)
        self.thread.join(timeout=10)
        self.p.stdout.close()
        self.log.close()


def keyframed(value):
    return isinstance(value, list) and len(value) > 0 and isinstance(value[0], dict)


def static_matte(spec):
    return not keyframed(spec['region']) and not any(keyframed(b) for b in spec.get('protect', []))


_MATTES = {}


def matte(spec, frame, size):
    """Soft matte; one without keyframes is computed once and returned as a fresh copy."""
    if not static_matte(spec):
        return _matte(spec, frame, size)
    key = (json.dumps(spec, sort_keys=True), tuple(size))
    if key not in _MATTES:
        _MATTES[key] = _matte(spec, frame, size)
    return _MATTES[key].copy()


def _matte(spec, frame, size):
    checked_size(size)
    x, y, w, h = rectangle(spec['region'], frame, size)
    feather = float(spec.get('feather', 16))
    require(0 <= feather < min(w, h)/2, 'Feather must be >=0 and < half region width/height')
    yy, xx = np.ogrid[:size[1], :size[0]]
    if spec.get('shape', 'rect') == 'ellipse':
        distance = (1-np.sqrt(((xx-(x+(w-1)/2))/(w/2))**2 +
                              ((yy-(y+(h-1)/2))/(h/2))**2))*min(w,h)/2
    else:
        require(spec.get('shape', 'rect') == 'rect', 'Unknown matte shape')
        distance = np.minimum(np.minimum(xx-x, x+w-1-xx), np.minimum(yy-y, y+h-1-yy)).astype(float)
    m = (distance >= 0).astype(np.float32) if feather == 0 else np.clip(distance/feather, 0, 1)
    m = (m*m*(3-2*m)).astype(np.float32)
    for box in spec.get('protect', []):
        px, py, pw, ph = rectangle(box, frame, size)
        m[py:py+ph, px:px+pw] = 0
    return m


def blend(original, patch, m, method, levels):
    # Element-wise steps run in row bands (same expressions, so identical values);
    # OpenCV's pyramid calls are already multithreaded.
    out = np.empty(original.shape, np.float32)
    if method == 'alpha':
        k = kernels()
        def work(a, b):
            if k:
                return k.alpha(original[a:b], patch[a:b], m[a:b], out[a:b])
            mm = m[a:b, :, None]
            out[a:b] = original[a:b].astype(np.float32)*(1-mm)+patch[a:b].astype(np.float32)*mm
        in_bands(m.shape[0], work)
    else:
        require(method == 'multiband', 'Unknown blend method')
        require(isinstance(levels, int) and 1 <= levels <= 6 and min(m.shape) >= 2**levels*2,
                'Invalid pyramid level count for patch size')
        ga, gb, gm = [original.astype(np.float32)], [patch.astype(np.float32)], [m]
        for _ in range(levels):
            ga.append(cv2.pyrDown(ga[-1])); gb.append(cv2.pyrDown(gb[-1])); gm.append(cv2.pyrDown(gm[-1]))
        level = ga[-1]*(1-gm[-1][:,:,None])+gb[-1]*gm[-1][:,:,None]
        for k in range(levels-1, -1, -1):
            size = (ga[k].shape[1], ga[k].shape[0])
            ua, ub, uo = (cv2.pyrUp(v, dstsize=size) for v in (ga[k+1], gb[k+1], level))
            level = out if k == 0 else np.empty(ga[k].shape, np.float32)
            def work(a, b, k=k, ua=ua, ub=ub, uo=uo, level=level):
                if kernels():
                    return kernels().level(ga[k][a:b], ua[a:b], gb[k][a:b], ub[a:b], uo[a:b],
                                           gm[k][a:b], level[a:b])
                mm = gm[k][a:b, :, None]
                la = ga[k][a:b]-ua[a:b]
                lb = gb[k][a:b]-ub[a:b]
                level[a:b] = uo[a:b]+la*(1-mm)+lb*mm
            in_bands(size[1], work)
    # Never let pyramid bleed change protected or out-of-support pixels.
    result = np.empty(original.shape, np.uint8)
    def finish(a, b):
        if kernels():
            return kernels().finish(out[a:b], m[a:b], original[a:b], result[a:b])
        result[a:b] = np.clip(out[a:b], 0, 255).round().astype(np.uint8)
        keep = m[a:b] == 0
        result[a:b][keep] = original[a:b][keep]
    in_bands(m.shape[0], finish)
    return result


def resolved(base, value):
    p = Path(value).expanduser()
    return p.resolve() if p.is_absolute() else (base/p).resolve()


def load(path):
    require(path.is_file(), 'Config must be a regular local file')
    with path.open('rb') as stream:
        content = stream.read(16*1024*1024+1)
    require(len(content) <= 16*1024*1024, 'Config exceeds 16 MiB')
    cfg = json.loads(content.decode('utf-8'))
    require(cfg.get('schema_version') == 1, 'Require schema_version=1')
    cfg['source'] = str(resolved(path.parent, cfg['source']))
    for p in cfg.get('patches', []):
        p['generated'] = str(resolved(path.parent, p['generated']))
    return cfg


def validate_source(cfg):
    info = probe(cfg['source'])
    require(all(v % 2 == 0 for v in info['size']), 'Source dimensions must be even for yuv420p output')
    require(info['size'] == cfg['source_size'] and info['fps'] == str(Fraction(str(cfg['fps']))),
            'Source size/fps differs from config')
    return info


def fades(p):
    """Temporal fade lengths; fade_frames sets both ends, fade_in/out_frames override one end."""
    both = p.get('fade_frames', 0)
    return p.get('fade_in_frames', both), p.get('fade_out_frames', both)


def mapping(p, count):
    start, end = p['target']
    require(isinstance(start, int) and isinstance(end, int) and 0 <= start < end <= count,
            'Invalid target interval')
    if 'frame_map' in p:
        indices = p['frame_map']
        require(len(indices) == end-start, 'frame_map length differs from target')
    else:
        offset = p.get('generated_start', 0)
        indices = list(range(offset, offset+end-start))
    valid = [v for v in indices if v is not None]
    require(all(isinstance(v, int) and v >= 0 for v in valid), 'Invalid frame index')
    require(all(a <= b for a,b in zip(valid,valid[1:])), 'Require monotone frame_map')
    return indices


def prepare_plan(cfg, info):
    p = cfg['prepare']; start, end = p['core']
    require(isinstance(start, int) and isinstance(end, int) and 0 <= start < end <= info['frames'],
            'Invalid core interval')
    context = p.get('context_frames', 24)
    require(isinstance(context, int) and context >= 0, 'Invalid context_frames')
    first, last = max(0, start-context), min(info['frames'], end+context)
    size = checked_size(p['output_size'])
    require(len(size) == 2 and all(isinstance(v,int) and v > 0 and v%2 == 0 for v in size),
            'Output size must contain two positive even integers')
    color = p.get('color_rgb', [0,0,0])
    require(len(color) == 3 and all(isinstance(v,int) and 0 <= v <= 255 for v in color), 'Invalid color_rgb')
    for i in range(first, last):
        crop = rectangle(p['crop'], i, info['size'])
        require(abs((crop[2]/crop[3])/(size[0]/size[1])-1) < 0.005,
                'Crop/output aspect ratios differ; use matching geometry')
        if start <= i < end:
            for box in p['mask_regions']:
                rectangle(box, i, size)
    return {'size': size, 'frames': last-first, 'first': first, 'last': last,
            'core_in_upload': [start-first, end-first], 'audio': info['audio']}


def merge_plan(cfg, info):
    require(len(cfg['patches']) > 0, 'No patches')
    plans = []
    for p in cfg['patches']:
        gi = probe(p['generated'])
        require(gi['fps'] == info['fps'], 'Generated fps differs; normalize explicitly before mapping')
        indices = mapping(p, info['frames'])
        require(all(j is None or j < gi['frames'] for j in indices),
                'Mapped generated frame missing; provide explicit frame_map, never silently hold/retime')
        size = checked_size(p['work_size'])
        require(len(size) == 2 and all(isinstance(v,int) and v > 0 for v in size), 'Invalid work_size')
        require(abs((gi['size'][0]/gi['size'][1])/(size[0]/size[1])-1) < 0.005,
                'Generated/work aspect ratio mismatch')
        gain = np.array(p.get('gain_bgr', [1,1,1])); offset = np.array(p.get('offset_bgr', [0,0,0]))
        require(gain.shape == (3,) and offset.shape == (3,) and np.isfinite(gain).all()
                and np.isfinite(offset).all() and (gain > 0).all(), 'Invalid fixed color transform')
        require(p.get('blend', 'alpha') != 'multiband' or not p['matte'].get('protect'),
                'Use alpha blending for protected mouth regions; multiband can mix low frequencies')
        fade_in, fade_out = fades(p)
        require(all(type(v) is int and v >= 0 for v in (fade_in, fade_out)) and
                fade_in+fade_out <= len(indices), 'Invalid fade_frames/fade_in_frames/fade_out_frames')
        if 'fade_frames' in p and not ({'fade_in_frames', 'fade_out_frames'} & set(p)):
            require(fade_in <= len(indices)//2, 'Invalid fade_frames')
        affine_spec = p.get('source_to_generated_affine', [1,0,0,0,1,0])
        static = static_matte(p['matte']) and not keyframed(affine_spec)
        for i in range(*p['target']):
            crop = rectangle(p['crop'], i, info['size'])
            require(abs((crop[2]/crop[3])/(size[0]/size[1])-1) < 0.005, 'Crop/work aspect mismatch')
            if static and i > p['target'][0]:
                continue  # Same matte and affine on every frame: the first check covers them all.
            m = matte(p['matte'], i, size)
            affine = curve(affine_spec, i).reshape(2,3)
            require(abs(np.linalg.det(affine[:,:2])) > 1e-6, 'Singular affine')
            valid = cv2.warpAffine(np.ones((size[1],size[0]), np.float32), affine,
                                   tuple(size), flags=cv2.INTER_LINEAR|cv2.WARP_INVERSE_MAP,
                                   borderMode=cv2.BORDER_CONSTANT)
            require(not np.any((m > 0) & (valid < .999)),
                    f'Affine leaves holes inside patch support at source frame {i}; shrink mask/review')
            if i == p['target'][0]:
                blend(np.zeros((size[1],size[0],3),np.uint8),
                      np.zeros((size[1],size[0],3),np.uint8), m, p.get('blend','alpha'), p.get('levels',5))
        plans.append({'info': gi, 'indices': indices})
    # Conservative overlap guard: full crop rectangles, not just soft mask support.
    for i in range(info['frames']):
        active = [p for p in cfg['patches'] if p['target'][0] <= i < p['target'][1]]
        boxes = [rectangle(p['crop'], i, info['size']) for p in active]
        for a, (x,y,w,h) in enumerate(boxes):
            for xx,yy,ww,hh in boxes[a+1:]:
                require(x+w <= xx or xx+ww <= x or y+h <= yy or yy+hh <= y,
                        f'Patch crops overlap at frame {i}; split jobs or redesign masks explicitly')
    return plans


def encode_threads():
    # 0 lets libx264 pick from the core count; the default 4 keeps earlier outputs reproducible.
    value = os.environ.get('FLOW_ENCODE_THREADS', '4')
    require(value.isdigit() and int(value) <= 64, 'FLOW_ENCODE_THREADS must be 0 (auto) to 64')
    return value


X264_PRESETS = ('ultrafast', 'superfast', 'veryfast', 'faster', 'fast', 'medium', 'slow',
                'slower', 'veryslow')


def x264_settings():
    """Deliverable libx264 quality: FLOW_CRF (0-51, default 17), FLOW_X264_PRESET (default fast)."""
    crf = os.environ.get('FLOW_CRF', '17')
    require(crf.isdigit() and int(crf) <= 51, 'FLOW_CRF must be an integer 0 to 51')
    preset = os.environ.get('FLOW_X264_PRESET', 'fast')
    require(preset in X264_PRESETS, 'FLOW_X264_PRESET must be one of '+', '.join(X264_PRESETS))
    return crf, preset


VIDEO_ENCODERS = ('libx264', 'h264_nvenc', 'h264_qsv', 'h264_amf')


def video_encoder():
    """FLOW_VIDEO_ENCODER: libx264 (default) or a hardware H.264 encoder of the FFmpeg build."""
    name = os.environ.get('FLOW_VIDEO_ENCODER', 'libx264')
    require(name in VIDEO_ENCODERS, 'FLOW_VIDEO_ENCODER must be one of '+', '.join(VIDEO_ENCODERS))
    return name


def codec_args(preset=None):
    """Encoder options. FLOW_CRF is the quality knob for every encoder (CQ/global_quality/QP);
    preset 'ultrafast' (check-only exports) picks each encoder's fastest mode."""
    crf, x264_preset = x264_settings()
    fast = preset == 'ultrafast'
    name = video_encoder()
    if name == 'libx264':
        return ['-c:v', 'libx264', '-threads', encode_threads(), '-crf', crf,
                '-preset', preset or x264_preset, '-pix_fmt', 'yuv420p']
    if name == 'h264_nvenc':
        return ['-c:v', name, '-preset', 'p1' if fast else 'p5', '-tune', 'hq', '-rc', 'vbr',
                '-cq', crf, '-b:v', '0', '-pix_fmt', 'yuv420p']
    if name == 'h264_qsv':  # QSV takes NV12, a lossless re-packing of yuv420p
        return ['-c:v', name, '-preset', 'veryfast' if fast else 'medium', '-global_quality', crf,
                '-pix_fmt', 'nv12']
    return ['-c:v', name, '-quality', 'speed' if fast else 'quality', '-rc', 'cqp',
            '-qp_i', crf, '-qp_p', crf, '-qp_b', crf, '-pix_fmt', 'yuv420p']


def segment_count(frames):
    # Opt-in: 1 (default) keeps the single-encoder bitstream; segments shorter than 12 frames are merged.
    value = os.environ.get('FLOW_SEGMENTS', '1')
    # 32 stays below the 61-process limit of Windows process pools.
    require(value.isdigit() and 1 <= int(value) <= 32, 'FLOW_SEGMENTS must be 1 to 32')
    return max(1, min(int(value), frames//12))


def colour_tags(color):
    return (f"h264_metadata=colour_primaries={PRIMARY_CODES[color['primaries']]}"
            f":transfer_characteristics={TRANSFER_CODES[color['transfer']]}"
            f":matrix_coefficients={MATRIX_CODES[color['matrix_tag']]}"
            f":video_full_range_flag={int(color['range'] == 'pc')}")


def encoder(path, size, fps, source, color, audio_filter=None, audio=True, preset=None, bframes=None):
    require(size[0] % 2 == 0 and size[1] % 2 == 0, 'Output dimensions must be even for yuv420p')
    # Frames arrive as yuv420p already converted with the source matrix; tag via bitstream
    # metadata, because output-side -colorspace flags make FFmpeg insert a lossy conversion.
    tags = colour_tags(color)
    cmd = [FFMPEG, '-v', 'error', '-n', '-f', 'rawvideo', '-pix_fmt', 'yuv420p',
           '-s', f'{size[0]}x{size[1]}', '-r', fps, '-protocol_whitelist', 'pipe', '-i', 'pipe:0',
           *(file_input(source) if audio else []),
           '-map', '0:v:0', *(['-map', '1:a:0?'] if audio else ['-an']),
           *codec_args(preset), *(['-bf', str(bframes)] if bframes is not None else []), '-bsf:v', tags]
    if audio and audio_filter is None:
        cmd += ['-c:a', 'copy']
    elif audio:
        cmd += ['-af', audio_filter, '-c:a', 'aac', '-b:a', '192k']
    return cmd+['-movflags', '+faststart', str(path)]


def patch_inputs(p, i, roi, generated, count):
    """Work-size original, aligned colour-fixed patch and faded matte for source frame i,
    exactly as merge blends them (qc_tools seam measures the same layers)."""
    ws = tuple(p['work_size'])
    original = cv2.resize(roi, ws, interpolation=cv2.INTER_LANCZOS4)
    patch = cv2.resize(generated, ws, interpolation=cv2.INTER_LANCZOS4)
    affine = curve(p.get('source_to_generated_affine',[1,0,0,0,1,0]),i).reshape(2,3)
    patch = cv2.warpAffine(patch,affine,ws,
                           flags=cv2.INTER_LINEAR|cv2.WARP_INVERSE_MAP,
                           borderMode=cv2.BORDER_CONSTANT)
    gain, offset = p.get('gain_bgr',[1,1,1]), p.get('offset_bgr',[0,0,0])
    if list(gain) != [1,1,1] or list(offset) != [0,0,0]:  # identity is a no-op
        patch = np.clip(patch.astype(float)*gain+offset,0,255).round().astype(np.uint8)
    m = matte(p['matte'],i,ws)
    fade_in, fade_out = fades(p)
    if fade_in or fade_out:
        k = i-p['target'][0]
        m *= min(1, k/fade_in if fade_in else 1, (count-1-k)/fade_out if fade_out else 1)
    return original, patch, m


def trimmed_audio(first, last, fps):
    """Source audio for frames [first,last), padded to the exact video duration."""
    return (f'atrim=start={float(first/fps)}:end={float(last/fps)},asetpts=PTS-STARTPTS,'
            f'apad,atrim=duration={float((last-first)/fps)}')


def process(cfg, info, mode, out, plan, span=None, segment=False, preset=None, bframes=None):
    """Encode one output. A merge span=(first,last) exports only those source frames, with the
    matching audio re-encoded; segment=True makes it video-only for joining."""
    with contextlib.ExitStack() as stack:
        start = plan['first'] if mode == 'prepare' else (span or (0,))[0]
        grid = lambda i: i['fps'] if i.get('on_frame_grid') else None
        source = Reader(cfg['source'], info['size'], info['color'], start, grid(info))
        stack.callback(source.close)
        if mode == 'prepare':
            p = cfg['prepare']; size = plan['size']
            fps = Fraction(info['fps']); first, last = plan['first'], plan['last']
            cmd = encoder(out, size, info['fps'], cfg['source'], info['color'],
                          trimmed_audio(first, last, fps), preset=preset)
        else:
            plans = plan; size = info['size']; first, last = span or (0, info['frames'])
            readers = []
            for p,q in zip(cfg['patches'],plans):
                # Each return is read from the first generated frame this span uses.
                used = [j for k,j in enumerate(q['indices'])
                        if j is not None and first <= p['target'][0]+k < last]
                readers.append(Reader(p['generated'], q['info']['size'], q['info']['color'],
                                      min(used, default=0), grid(q['info'])))
                stack.callback(readers[-1].close)
            af = None if span is None else trimmed_audio(first, last, Fraction(info['fps']))
            cmd = encoder(out, size, info['fps'], cfg['source'], info['color'], af,
                          audio=not segment, preset=preset, bframes=bframes)
            plan = {'size': size, 'frames': last-first, 'audio': info['audio']}
        log = stack.enter_context(tempfile.TemporaryFile())
        enc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=log)
        out_frames = FrameWriter(enc.stdin)
        try:
            try:
                for i in range(first, last):
                    if mode == 'prepare':
                        x,y,w,h = rectangle(p['crop'], i, info['size'])
                        scene = cv2.resize(source.region(i,(x,y,w,h)), tuple(size), interpolation=cv2.INTER_LANCZOS4)
                        if p['core'][0] <= i < p['core'][1]:
                            for box in p['mask_regions']:
                                bx,by,bw,bh = rectangle(box, i, size)
                                scene[by:by+bh,bx:bx+bw] = p.get('color_rgb',[0,0,0])[::-1]
                        out_frames.write(bgr_to_yuv420(scene, info['color']))
                        continue
                    active = []
                    for p,q,reader in zip(cfg['patches'],plans,readers):
                        if not p['target'][0] <= i < p['target'][1]:
                            continue
                        j = q['indices'][i-p['target'][0]]
                        if j is not None:
                            active.append((p,q,reader,j,rectangle(p['crop'], i, size)))
                    # Pixels outside every patch support go out as decoded, without an RGB round trip.
                    frame, Y, U, V = source.passthrough420(i)
                    if active:
                        # Even-aligned union of the crops, so chroma blocks touching a patch are recomputed whole.
                        x0 = min(r[0] for *_,r in active) & ~1; y0 = min(r[1] for *_,r in active) & ~1
                        x1 = max(r[0]+r[2] for *_,r in active); y1 = max(r[1]+r[3] for *_,r in active)
                        x1 += x1 & 1; y1 += y1 & 1
                        scene = source.region(i,(x0,y0,x1-x0,y1-y0))
                        changed = np.zeros(scene.shape[:2], bool)
                        for p,q,reader,j,(x,y,w,h) in active:
                            x -= x0; y -= y0; ws = tuple(p['work_size'])
                            roi = scene[y:y+h,x:x+w].copy()
                            original, patch, m = patch_inputs(p, i, roi, reader.get(j), len(q['indices']))
                            merged = blend(original,patch,m,p.get('blend','alpha'),p.get('levels',5))
                            # Enforce support again after scaling (including protected mouth).
                            support = cv2.resize(m,(w,h),interpolation=cv2.INTER_NEAREST)>0
                            if (w, h) == ws:
                                # Same-size resizes are exact copies, so roi+delta is merged itself.
                                scene[y:y+h,x:x+w] = merged
                            else:
                                diff = np.empty(merged.shape, float)
                                def work(a, b):
                                    if kernels():
                                        return kernels().diff64(merged[a:b], original[a:b], diff[a:b])
                                    diff[a:b] = merged[a:b].astype(float)-original[a:b].astype(float)
                                in_bands(diff.shape[0], work)
                                delta = cv2.resize(diff,(w,h),interpolation=cv2.INTER_LINEAR)
                                def work(a, b, x=x, y=y):
                                    if kernels():
                                        return kernels().apply64(roi[a:b], delta[a:b], support[a:b],
                                                                 scene[y+a:y+b,x:x+w])
                                    d = delta[a:b]; d[~support[a:b]] = 0
                                    scene[y+a:y+b,x:x+w] = np.clip(roi[a:b].astype(float)+d,0,255).round().astype(np.uint8)
                                in_bands(h, work)
                            changed[y:y+h,x:x+w] |= support
                        _, Yr, Ur, Vr = planes420(bgr_to_yuv420(scene, info['color']), x1-x0, y1-y0)
                        Y[y0:y1,x0:x1][changed] = Yr[changed]
                        block = changed.reshape((y1-y0)//2, 2, (x1-x0)//2, 2).any((1, 3))
                        U[y0//2:y1//2,x0//2:x1//2][block] = Ur[block]
                        V[y0//2:y1//2,x0//2:x1//2][block] = Vr[block]
                    out_frames.write(frame)
            except OSError as e:
                # Encoder stopped reading; its own log below says why.
                if e is not out_frames.error:
                    raise
            error = out_frames.finish()
            with contextlib.suppress(OSError):
                enc.stdin.close()
            code = enc.wait()
            log.seek(0)
            require(code == 0, 'Encoder failed: '+log.read().decode(errors='replace'))
            if error is not None:
                raise error
        finally:
            if out_frames.thread.is_alive():
                stop_process(enc)
                out_frames.finish()
            if not enc.stdin.closed:
                with contextlib.suppress(OSError):
                    enc.stdin.close()
            if enc.poll() is None:
                stop_process(enc)
        return plan, cmd


def _segment(job):
    cfg, info, plans, span, path, preset, *bframes = job
    return process(cfg, info, 'merge', Path(path), plans, span, segment=True, preset=preset,
                   bframes=bframes[0] if bframes else None)[1]


def merge_segmented(cfg, info, plans, out, count, preset=None):
    """Merge frame ranges in parallel processes, join without re-encoding, then copy audio once.

    Each segment is its own libx264 encode starting on a keyframe, so the bitstream differs from a
    single encode; pixels sent to the encoder are identical. The joined file is verified as a whole."""
    frames = info['frames']
    bounds = [frames*k//count for k in range(count+1)]
    spans = list(zip(bounds, bounds[1:]))
    work = out.parent/'segments'  # the output name always has a media suffix, so no clash
    work.mkdir()
    parts = [work/f'part{k:02d}.mp4' for k in range(count)]
    listing = work/'parts.txt'
    listing.write_text(''.join(f"file '{part.name}'\n" for part in parts), encoding='utf-8')
    # Share the cores between segment processes unless the user set a thread count.
    saved = os.environ.get('FLOW_PROCESS_THREADS')
    if saved in (None, '0'):
        os.environ['FLOW_PROCESS_THREADS'] = str(max(1, (os.cpu_count() or 1)//count))
    try:
        with ProcessPoolExecutor(count, mp_context=multiprocessing.get_context('spawn')) as pool:
            commands = list(pool.map(_segment, [(cfg, info, plans, s, str(p), preset)
                                                  for s, p in zip(spans, parts)]))
    finally:
        if saved is None:
            os.environ.pop('FLOW_PROCESS_THREADS', None)
        else:
            os.environ['FLOW_PROCESS_THREADS'] = saved
    join = [FFMPEG, '-v', 'error', '-n', '-protocol_whitelist', 'file', '-f', 'concat', '-i', str(listing),
            *file_input(cfg['source']), '-map', '0:v:0', '-map', '1:a:0?', '-c', 'copy',
            '-movflags', '+faststart', str(out)]
    run(join)
    for part in parts:
        part.unlink()
    plan = {'size': info['size'], 'frames': frames, 'audio': info['audio']}
    return plan, {'segments': [{'frames': list(s), 'command_argv': c} for s, c in zip(spans, commands)],
                  'join_argv': join}


class CopyFallback(Exception):
    """--copy-unpatched cannot be used safely for this source; render in full instead."""


def copy_unpatched_reason(info):
    """None when unpatched source GOPs can be copied into the output, else why not."""
    if info.get('codec') != 'h264':
        return f"source video is {info.get('codec')}, not H.264"
    if info['chroma'] != '4:2:0':
        return f"source chroma is {info['chroma']}, not 4:2:0"
    if not info.get('on_frame_grid'):
        return 'source timestamps are not on the frame grid'
    # Copied packets keep the source's timestamps, so its time base must hold every frame time exactly;
    # Matroska's milliseconds cannot at 23.976 or 60 fps, which would fail the joined-stream check.
    tb = Fraction(info.get('time_base') or '0/1')
    if not tb or (1/Fraction(info['fps'])/tb).denominator != 1:
        return (f"source time base {info.get('time_base')} cannot hold {info['fps']} fps frame times exactly "
                '(run `flow_patch.py regrid` first to use --copy-unpatched)')
    return None


def regrid(source, output):
    """Stream-copy SOURCE into MP4/MOV with every timestamp snapped to n/fps.

    A plain `-c copy` remux keeps Matroska's millisecond rounding (and per-packet durations), so the
    MP4 is not constant-rate. Each packet time here moves by under a quarter frame to its exact grid
    slot; frame pixels, frame count, order and audio payload are unchanged, and all are checked."""
    # The source's rate fields are rounded too (avg_frame_rate differs by FFmpeg version), so it is
    # judged by r_frame_rate and its decoded timestamps; the output then passes the full strict probe.
    data = json.loads(run([FFPROBE, '-v', 'error', '-show_streams', '-of', 'json',
                           '-protocol_whitelist', 'file', local_media(source)]))
    videos = [s for s in data['streams'] if s['codec_type'] == 'video']
    require(len(videos) == 1, 'Require exactly one video stream')
    fps = Fraction(videos[0]['r_frame_rate'])
    require(fps > 0, 'Source has no frame rate')
    times = decode_pass(source)[0]
    require(times and abs(times[0]) < 0.001, 'Video must start near timestamp zero; remux explicitly')
    shift = max(abs(t-n/fps) for n, t in enumerate(times))
    require(shift < 0.25/fps, 'Frames are not within a quarter frame of n/fps; this is not timestamp rounding, '
            'so regrid would retime the video. Normalize explicitly instead')
    streams = {kind: sum(x['codec_type'] == kind for x in data['streams'])
               for kind in ('audio', 'subtitle', 'data', 'attachment')}
    n = fps.numerator
    timescale = n if n >= 1000 else 90000 if 90000 % n == 0 else n*-(-1000//n)
    snap = f'*TB*{fps.numerator}/{fps.denominator})'
    cmd = [FFMPEG, '-v', 'error', '-n', '-copyts', *file_input(source), '-map', '0:v:0', '-map', '0:a?',
           '-c', 'copy', '-bsf:v', f'setts=pts=round(PTS{snap}:dts=round(DTS{snap}:duration=1'
           f':time_base={fps.denominator}/{fps.numerator}', '-video_track_timescale', str(timescale),
           '-movflags', '+faststart', output]
    run(cmd)
    try:
        out = probe(output, True)
    except ValueError as e:
        raise ValueError(f'Regridded output failed its check: {e}') from e
    require(Fraction(out['fps']) == fps and out['frames'] == len(times) and out['on_frame_grid'],
            'Regridded output does not match the source frame rate or frame count')
    require(frame_hashes(output) == decode_pass(source)[1], 'Regridded frames differ from the source')
    if streams['audio']:
        require(audio_hash(output) == audio_hash(source), 'Regridded audio payload differs from the source')
    dropped = {k: v for k, v in streams.items() if k != 'audio' and v}
    return {'source': str(Path(source).resolve()), 'output': str(output), 'fps': out['fps'], 'frames': out['frames'],
            'max_timestamp_shift_ms': round(shift*1000, 3), 'decoded_frames_identical': True,
            'audio_payload_identical': bool(streams['audio']) or None, 'audio_tracks': streams['audio'],
            'not_carried': dropped, 'copy_unpatched_eligible': copy_unpatched_reason(out) is None,
            'command_argv': [str(x) for x in cmd]}


def keyframe_indices(path, fps, frames):
    """Display-order frame numbers of the keyframes and the stream's reorder delay in frames,
    read from packets (no decoding)."""
    data = json.loads(run([FFPROBE, '-v', 'error', '-select_streams', 'v:0', '-show_entries',
                           'packet=pts_time,dts_time,flags', '-of', 'json', '-protocol_whitelist', 'file',
                           local_media(path)]))
    fps = Fraction(fps)
    packets = data.get('packets', [])
    keys = sorted({round(float(p['pts_time'])*fps) for p in packets
                   if 'K' in p.get('flags', '') and p.get('pts_time') not in (None, 'N/A')})
    if not keys or keys[0] != 0 or keys[-1] >= frames:
        raise CopyFallback('keyframe positions do not map onto source frames')
    first = packets[0]
    delay = round((float(first['pts_time'])-float(first['dts_time']))*fps) if first.get('dts_time') not in (None, 'N/A') else 0
    return keys, delay


def frame_hashes(path):
    """Per-frame murmur3 hashes, reusing this process's decoding pass of the file when there is one."""
    key = local_media(path)
    return (_PASSES.get(key) or decode_pass(path))[1]


def merge_copy_unpatched(cfg, info, plans, out, preset=None):
    """Copy the source's unpatched GOPs bit for bit and re-encode only GOPs holding patch frames.

    Pieces are cut at source keyframes, joined without re-encoding (parameter sets in-band, colour
    tags rewritten for the whole stream), and the original audio is copied once. Every copied frame
    must then decode to the same MD5 as the source frame; otherwise CopyFallback is raised."""
    frames = info['frames']
    keys, delay = keyframe_indices(cfg['source'], info['fps'], frames)
    # Re-encoded pieces reorder like the source (x264: 0, 1 or a 3-frame pyramid giving delay 2),
    # so decode timestamps join without being squeezed; other delays fall back after the timing check.
    dirty = sorted({p['target'][0]+k for p, q in zip(cfg['patches'], plans)
                    for k, j in enumerate(q['indices']) if j is not None})
    bounds = keys+[frames]
    pieces = []  # [first, last, render?] with neighbouring GOPs of the same kind merged
    for a, b in zip(bounds, bounds[1:]):
        render = bisect.bisect_left(dirty, a) < bisect.bisect_left(dirty, b)
        if pieces and pieces[-1][2] == render:
            pieces[-1][1] = b
        else:
            pieces.append([a, b, render])
    work = out.parent/'pieces'
    work.mkdir()
    # Hash the source frames while pieces are cut and rendered.
    checks = ThreadPoolExecutor(3)
    try:
        return _copy_unpatched(cfg, info, plans, out, preset, frames, keys, delay, pieces, work, checks)
    finally:
        # Wait for background checks, so no FFmpeg still holds a file when a fallback deletes or
        # rewrites it (Windows refuses to remove open files: issue #10).
        checks.shutdown(wait=True, cancel_futures=True)


def _copy_unpatched(cfg, info, plans, out, preset, frames, keys, delay, pieces, work, checks):
    bframes = {0: 0, 1: 1}.get(delay, 3)
    source_md5 = checks.submit(frame_hashes, cfg['source'])
    cuts = [a for a, _, _ in pieces[1:]]
    copy_cmd = [FFMPEG, '-v', 'error', '-n', *file_input(cfg['source']), '-map', '0:v:0', '-c', 'copy',
                *(['-f', 'segment', '-segment_frames', ','.join(map(str, cuts)), '-reset_timestamps', '1',
                   '-segment_format', 'mp4', str(work/'copy%04d.mp4')] if cuts else [str(work/'copy0000.mp4')])]
    run(copy_cmd)
    files, jobs = [], []
    for k, (a, b, render) in enumerate(pieces):
        if render:
            files.append(work/f'render{k:04d}.mp4')
            jobs.append((cfg, info, plans, (a, b), str(files[-1]), preset, bframes))
            continue
        files.append(work/f'copy{k:04d}.mp4')
        count = json.loads(run([FFPROBE, '-v', 'error', '-count_packets', '-select_streams', 'v:0',
                                '-show_entries', 'stream=nb_read_packets', '-of', 'json',
                                '-protocol_whitelist', 'file', str(files[-1])]))['streams'][0]['nb_read_packets']
        if int(count) != b-a:
            raise CopyFallback(f'copied GOPs {a}-{b} hold {count} frames, expected {b-a}')
    workers = min(len(jobs), segment_count(1 << 30)) if jobs else 0
    if workers > 1:
        saved = os.environ.get('FLOW_PROCESS_THREADS')
        if saved in (None, '0'):
            os.environ['FLOW_PROCESS_THREADS'] = str(max(1, (os.cpu_count() or 1)//workers))
        try:
            with ProcessPoolExecutor(workers, mp_context=multiprocessing.get_context('spawn')) as pool:
                commands = list(pool.map(_segment, jobs))
        finally:
            if saved is None:
                os.environ.pop('FLOW_PROCESS_THREADS', None)
            else:
                os.environ['FLOW_PROCESS_THREADS'] = saved
    else:
        commands = [_segment(job) for job in jobs]
    listing = work/'pieces.txt'
    listing.write_text(''.join(f"file '{f.name}'\n" for f in files), encoding='utf-8')
    join = [FFMPEG, '-v', 'error', '-n', '-protocol_whitelist', 'file', '-f', 'concat', '-i', str(listing),
            *file_input(cfg['source']), '-map', '0:v:0', '-map', '1:a:0?', '-c', 'copy',
            '-bsf:v', 'h264_mp4toannexb,'+colour_tags(info['color']), '-movflags', '+faststart', str(out)]
    run(join)
    try:
        joined = _probe(str(out), strict=True)  # one strict decode: timing, errors and frame hashes
    except (ValueError, subprocess.CalledProcessError) as e:
        raise CopyFallback(f'joined stream failed its decode/timing check ({e})') from e
    if joined['frames'] != frames:
        raise CopyFallback('joined stream has a different frame count')
    source_md5, output_md5 = source_md5.result(), frame_hashes(out)
    copied = [i for a, b, render in pieces if not render for i in range(a, b)]
    if len(output_md5) != frames or any(source_md5[i] != output_md5[i] for i in copied):
        raise CopyFallback('copied frames do not decode identically to the source')
    # Only a joined file that passed every check is reused by verify(); a fallback render at the
    # same path must get its own strict decode (issue #15).
    probed = Future(); probed.set_result(joined)
    _PROBED[local_media(out)] = probed
    for f in work.iterdir():
        f.unlink()
    work.rmdir()
    plan = {'size': info['size'], 'frames': frames, 'audio': info['audio']}
    rendered = sum(b-a for a, b, render in pieces if render)
    return plan, {'copy_argv': copy_cmd, 'render_argv': commands, 'join_argv': join}, {
        'source_keyframes': len(keys), 'copied_frames': frames-rendered, 'rendered_frames': rendered,
        'pieces': [[a, b, 'render' if render else 'copy'] for a, b, render in pieces],
        'copied_frames_identical_to_source': True}


def verify(source, output, expected, copy_audio):
    # One strict decoding pass checks errors, frame count and timing; audio hashes run alongside.
    with ThreadPoolExecutor(3) as pool:
        probed = pool.submit(probe, output, True)
        hashes = ([pool.submit(audio_hash, source), pool.submit(audio_hash, output)]
                  if copy_audio and expected['audio'] else None)
        result = probed.result()
        require(result['size'] == expected['size'] and result['frames'] == expected['frames']
                and result['fps'] == expected['fps'] and result['audio'] == expected['audio'],
                'Output size, frames, fps or audio presence mismatch')
        ah = None
        if hashes:
            ah = hashes[0].result()
            require(ah == hashes[1].result(), 'Original audio payload changed')
    return {'decode_pass':True, 'output':result, 'audio_payload_sha256':ah}


@functools.lru_cache(maxsize=None)
def tool_version(tool):
    return run([tool, '-version']).decode().splitlines()[0]


def opencv_distribution():
    for name in ('opencv-python', 'opencv-python-headless', 'opencv-contrib-python', 'opencv-contrib-python-headless'):
        with contextlib.suppress(importlib.metadata.PackageNotFoundError):
            return f'{name} {importlib.metadata.version(name)}'
    return 'unknown distribution'


def tool_inventory():
    """Tools this run actually uses, for the delivery list SKILL.md asks for. Licence terms are in
    THIRD_PARTY_NOTICES.md; optional components are those recorded in TOOL_POLICY.json."""
    notices = 'THIRD_PARTY_NOTICES.md'
    encoder_name = video_encoder()
    items = [{'tool': 'CPython', 'version': sys.version.split()[0], 'allowlist': 'default', 'license_reference': notices},
             {'tool': 'NumPy', 'version': np.__version__, 'allowlist': 'default', 'license_reference': notices},
             {'tool': 'OpenCV', 'version': f'cv2 {cv2.__version__} ({opencv_distribution()})', 'allowlist': 'default',
              'license_reference': notices},
             {'tool': 'FFmpeg', 'version': tool_version(FFMPEG), 'allowlist': 'default', 'license_reference': notices},
             {'tool': 'ffprobe', 'version': tool_version(FFPROBE), 'allowlist': 'default', 'license_reference': notices},
             {'tool': 'video encoder', 'version': encoder_name, 'license_reference': notices,
              'allowlist': 'default' if encoder_name == 'libx264' else 'optional: ffmpeg_hardware_h264 (TOOL_POLICY.json)'}]
    if kernels():
        items.append({'tool': 'flow_kernels (native/)', 'version': f'ABI {NativeKernels.ABI}, built locally',
                      'allowlist': 'optional: flow_kernels_native (TOOL_POLICY.json)', 'license_reference': notices})
    return items


def prefilled_review(result, cfg, output, report):
    """Visual review record with only the fields the run itself establishes; every visual check that
    needs a person stays not_run and the overall status stays pending_visual_review."""
    record = json.loads((Path(__file__).resolve().parents[1]/'assets'/'visual-review-template.json')
                        .read_text(encoding='utf-8'))
    out = result['output']
    record.update(source=Path(cfg['source']).name, output=output.name, output_sha256=result['output_sha256'],
                  fps=out['fps'], output_frames=out['frames'], technical_report=report.name,
                  repair_source_intervals_half_open=[p['target'] for p in cfg['patches']])
    record['technical_validation'] = {'status': 'pass', 'evidence': [
        f'{report.name}: strict full decode, size/fps/frame count'
        + (', original audio payload SHA-256' if result.get('audio_payload_sha256') else '')]}
    declared = []
    for p in cfg['patches']:
        indices = mapping(p, p['target'][1])
        valid = [j for j in indices if j is not None]
        declared.append({'target': p['target'], 'generated': Path(p['generated']).name,
                        'generated_range': [min(valid), max(valid)] if valid else None,
                        'repeated_frames': len(valid)-len(set(valid)), 'kept_original_frames': indices.count(None)})
    record['frame_mapping_evidence'] = {'kind': 'declared in the configuration (not proof of alignment)',
                                        'patches': declared}
    for check in record['visual_checks']:
        if check['id'] == 'decode_dimensions_fps_frames':
            check.update(status='pass', evidence=[report.name], notes='Automatic: strict decode, size, fps and frames')
        elif check['id'] == 'original_audio_payload':
            if result.get('audio_payload_sha256'):
                check.update(status='pass', evidence=[report.name],
                             notes='Automatic: copied audio payload SHA-256 equals the source')
            elif not out.get('audio'):
                check.update(status='not_applicable', notes='Source has no audio stream')
    record['prefilled'] = {'by': 'flow_patch.py merge', 'note': 'Only automatic fields are filled; '
                           'all other checks remain not_run until a person reviews the final file.'}
    return record


def utf8_console():
    """Chinese paths and messages print the same on every platform, without PYTHONUTF8."""
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(encoding='utf-8', errors='backslashreplace')


def main():
    utf8_console()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=['doctor','inspect','prepare','merge','regrid'])
    parser.add_argument('--config',type=Path)
    parser.add_argument('--source',type=Path)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--dry-run',action='store_true')
    parser.add_argument('--frames',metavar='START:END',
                        help='merge only: export source frames [START,END) for checking (not deliverable)')
    parser.add_argument('--copy-unpatched',action='store_true',
                        help='merge only: copy unpatched source GOPs bit for bit, re-encode only GOPs with patches')
    parser.add_argument('--preview',action='store_true',
                        help='merge only: fast ultrafast encode for checking (not deliverable)')
    args = parser.parse_args()
    for t in (FFMPEG,FFPROBE):
        require(shutil.which(t), f'Tool missing: {t}; see references/install.md')
    if args.mode == 'doctor':
        encoders = run([FFMPEG,'-v','error','-encoders']).decode()
        require('libx264' in encoders, 'FFmpeg needs libx264 encoder')
        print(json.dumps({'security_notice':'Capability check only. Verify patched runtime versions per SECURITY.md before using untrusted media.',
                          'python':sys.version,'opencv':cv2.__version__, 'numpy':np.__version__,
                          'ffmpeg':tool_version(FFMPEG),'ffprobe':tool_version(FFPROBE),
                          'opencv_distribution':opencv_distribution(),
                          'libx264':True,'encode_threads':encode_threads(),
                          'crf_preset':list(x264_settings()),
                          'video_encoder':video_encoder(),
                          'hardware_encoders_in_build':[e for e in VIDEO_ENCODERS[1:] if e in encoders],
                          'segments':segment_count(1 << 30),
                          'native_kernels':('enabled, bit-identical self-check passed' if kernels() else
                                            'off (FLOW_KERNELS=numpy)' if os.environ.get('FLOW_KERNELS') == 'numpy'
                                            else 'not built (references/native-kernels.md)')},
                         ensure_ascii=False,indent=2)); return
    if args.mode == 'inspect':
        require(args.source is not None, '--source required')
        print(json.dumps(probe(args.source),indent=2)); return
    if args.mode == 'regrid':
        require(args.source is not None and args.output is not None, '--source and --output required')
        output = output_path(args.output); report = output_path(output.with_suffix('.report.json'))
        require(output.suffix.lower() in ('.mp4','.mov'), 'regrid output must be MP4/MOV')
        require(not output.exists() and not report.exists(), 'Output/report exists; choose new name')
        with tempfile.TemporaryDirectory(prefix='.flow-patch-',dir=output.parent) as tmp:
            temp = Path(tmp)/output.name
            result = regrid(args.source, temp)
            result.update(output=str(output), output_sha256=sha(temp), source_sha256=sha(args.source))
            with contextlib.ExitStack() as files:
                dest = files.enter_context(private_output(output, binary=True))
                rpt = files.enter_context(private_output(report))
                with temp.open('rb') as src:
                    shutil.copyfileobj(src, dest)
                json.dump(result, rpt, ensure_ascii=False, indent=2)
        if result['not_carried']:
            print('NOTICE: Not carried to the output: '+', '.join(f'{v} {k}' for k, v in result['not_carried'].items())
                  +' stream(s)', file=sys.stderr)
        print(f"PASS: {output} (timestamps moved at most {result['max_timestamp_shift_ms']} ms; frames and "
              f"audio unchanged)\nReport: {report}"); return
    require(args.config is not None and args.output is not None, '--config and --output required')
    require(args.mode == 'merge' or not (args.frames or args.preview or args.copy_unpatched),
            '--frames/--preview apply to merge only; Flow uploads always use the full prepare encode')
    span = None
    if args.frames is not None:
        parts = args.frames.split(':')
        require(len(parts) == 2 and all(v.isdigit() for v in parts), '--frames must be START:END')
        span = (int(parts[0]), int(parts[1]))
    preset = 'ultrafast' if args.preview else None  # None: FLOW_X264_PRESET
    deliverable = span is None and not args.preview
    # A hardware encoder may make check-only exports; deliverables need a quality comparison first.
    hardware = video_encoder() != 'libx264'
    require(not hardware or not deliverable or os.environ.get('FLOW_HW_DELIVERABLE') == '1',
            f'{video_encoder()} for a deliverable or Flow upload needs FLOW_HW_DELIVERABLE=1 after '
            'comparing its quality with libx264; see references/remote-worker.md')
    cfg = load(args.config.resolve())
    # Deliverable runs always re-probe in full; check runs and dry runs may reuse probes of unchanged inputs.
    cache = None if deliverable and not args.dry_run else output_path(args.output).parent/'.flow-probe-cache'
    prefetch_probes([cfg['source'], *(p['generated'] for p in cfg.get('patches', []))], cache)
    info = validate_source(cfg)
    plan = prepare_plan(cfg,info) if args.mode == 'prepare' else merge_plan(cfg,info)
    require(not (args.copy_unpatched and span), '--copy-unpatched applies to full merges, not --frames')
    require(span is None or 0 <= span[0] < span[1] <= info['frames'],
            'Invalid --frames interval for this source')
    notices = output_notices(info, args.mode)
    if hardware:
        notices.append(f'Video encoder {video_encoder()} (hardware); compare quality with libx264 '
                       'before relying on it')
    for note in notices:
        print('NOTICE: '+note, file=sys.stderr)
    if args.dry_run:
        print(json.dumps({'mode':args.mode,'config':cfg,'source_info':info,'plan':plan,'notices':notices},
                         ensure_ascii=False,indent=2)); return
    output = output_path(args.output); report = output_path(output.with_suffix('.report.json'))
    require(output.suffix.lower() in ('.mp4','.mov','.mkv'), 'Output must be MP4/MOV/MKV')
    # Deliverable merges also get a pre-filled visual review record next to the report.
    review = output_path(output.with_suffix('.visual-review.json')) if args.mode == 'merge' and deliverable else None
    require(not output.exists() and not report.exists() and not (review and review.exists()),
            'Output/report/review record exists; choose new name')
    output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.flow-patch-',dir=output.parent) as tmp:
        temp = Path(tmp)/output.name
        smart = None
        if args.copy_unpatched:
            reason = copy_unpatched_reason(info)
            if reason is None:
                try:
                    plan_used,cmd,smart = merge_copy_unpatched(cfg,info,plan,temp,preset)
                except (CopyFallback, subprocess.CalledProcessError) as e:
                    reason = (str(e) if isinstance(e, CopyFallback) else
                              'FFmpeg could not cut or join the source GOPs')
                    shutil.rmtree(temp.parent/'pieces', ignore_errors=True)
                    forget(temp)
                    with contextlib.suppress(FileNotFoundError):
                        temp.unlink()
            if reason is not None:
                notices.append(f'--copy-unpatched not used ({reason}); rendered in full instead')
                print('NOTICE: '+notices[-1], file=sys.stderr)
        count = segment_count(info['frames']) if args.mode == 'merge' and span is None else 1
        if smart is not None:
            plan = plan_used
            if smart['copied_frames'] == 0:
                notices.append('--copy-unpatched copied no frames: every source GOP holds a patch frame, '
                               'so it saved nothing over a full render')
                print('NOTICE: '+notices[-1], file=sys.stderr)
        elif count > 1:
            plan,cmd = merge_segmented(cfg,info,plan,temp,count,preset)
        else:
            plan,cmd = process(cfg,info,args.mode,temp,plan,span,preset=preset)
        expected = dict(plan,fps=info['fps'])
        # A partial export re-encodes its audio slice, so only full merges compare audio payloads.
        result = verify(cfg['source'],temp,expected,args.mode=='merge' and span is None)
        result['output'].update(plan)
        result.update(mode=args.mode,configuration=cfg,command_argv=cmd,
                      source_sha256=sha(cfg['source']),output_sha256=sha(temp),
                      visual_review='pending: decode/frame/audio checks do not prove visual quality or lip sync')
        if args.mode == 'merge':
            result['scope'] = {'frames': list(span) if span else [0, info['frames']],
                               'preview_encode': args.preview, 'deliverable': deliverable}
        if not deliverable:
            result['visual_review'] = ('not deliverable: partial/preview export for checking only; '
                                       'final acceptance must use a full export without --frames/--preview')
        result['notices'] = notices
        if args.copy_unpatched:
            result['copy_unpatched'] = smart or {'used': False}
        result['generated_sha256'] = {p['generated']:sha(p['generated']) for p in cfg.get('patches',[])}
        result['tools'] = tool_inventory()
        # Reserve every path exclusively; remove only files created here on failure.
        with contextlib.ExitStack() as files:
            dest = files.enter_context(private_output(output, binary=True))
            rpt = files.enter_context(private_output(report))
            with temp.open('rb') as src:
                shutil.copyfileobj(src, dest)
            json.dump(result, rpt, ensure_ascii=False, indent=2)
            if review is not None:
                json.dump(prefilled_review(result, cfg, output, report),
                          files.enter_context(private_output(review)), ensure_ascii=False, indent=2)
    print(f'PASS{"" if deliverable else " (check only, not deliverable)"}: {output}\nReport: {report}'
          + (f'\nVisual review record (pre-filled, pending): {review}' if review is not None else ''))


if __name__ == '__main__':
    try:
        main()
    except (ValueError,KeyError,OSError,subprocess.CalledProcessError,subprocess.TimeoutExpired) as e:
        if isinstance(e,subprocess.CalledProcessError):
            print(e.stderr.decode(errors='replace') if e.stderr else str(e),file=sys.stderr)
        else:
            print(str(e),file=sys.stderr)
        raise SystemExit(1)
