#!/usr/bin/env python3
"""Portable Flow patch preparation/composition. No downloads or uploads.

All frame intervals are zero-based [start,end). All rectangles are x,y,w,h.
Paths in JSON are relative to that JSON. See references/parameters.md.
"""
import argparse
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
import contextlib
from fractions import Fraction
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import queue
import shutil
import subprocess
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
    def work(a, b):
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
    def work(a, b):
        Y[a:b], U[a//2:b//2], V[a//2:b//2] = _bgr_to_yuv420(bgr[a:b], color)
    in_bands(h, work)
    return buf


_PROBED = {}


PROBE_CACHE_FORMAT = 1


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


def probe(path):
    path = local_media(path)
    if path in _PROBED:
        return _PROBED[path].result()
    return _probe(path)


def _probe(path):
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
    require(fps > 0 and fps == Fraction(s['r_frame_rate']), 'Require constant frame rate')
    # Equal rate fields alone do not prove CFR. Check decoded timestamps.
    decoded = json.loads(run([FFPROBE, '-v', 'error', '-select_streams', 'v:0', '-count_frames',
                              '-show_frames', '-show_entries',
                              'stream=nb_read_frames:frame=best_effort_timestamp_time',
                              '-of', 'json', '-protocol_whitelist', 'file', path]))
    s['nb_read_frames'] = decoded['streams'][0]['nb_read_frames']
    times = [float(f['best_effort_timestamp_time']) for f in decoded['frames']]
    require(len(times) > 0, 'No decoded frames')
    require(all(abs((b-a)-float(1/fps)) < max(0.0001, float(1/fps)*0.002)
                for a, b in zip(times, times[1:])), 'VFR or timestamp gaps; normalize explicitly')
    require(abs(times[0]) < 0.001, 'Video must start near timestamp zero; remux explicitly')
    audio = next((a for a in data['streams'] if a['codec_type'] == 'audio'), None)
    require(audio is None or abs(float(audio.get('start_time', 0))) < 0.001,
            'Audio starts at a nonzero timestamp; normalize offsets explicitly')
    require(len(times) == int(s['nb_read_frames']), 'Frame count mismatch')
    # Frame n at n/fps (within a quarter frame) lets readers seek straight to a frame.
    grid = all(abs(t-n/fps) < 0.25/fps for n, t in enumerate(times))
    return {'size': [s['width'], s['height']], 'fps': str(fps), 'frames': len(times),
            'audio': audio is not None, 'color': color_info(s), 'on_frame_grid': grid}


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
        def work(a, b):
            Y[a:b] = y[a:b]
            for src, dst in ((u, U), (v, V)):
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
        def work(a, b):
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
                mm = gm[k][a:b, :, None]
                la = ga[k][a:b]-ua[a:b]
                lb = gb[k][a:b]-ub[a:b]
                level[a:b] = uo[a:b]+la*(1-mm)+lb*mm
            in_bands(size[1], work)
    # Never let pyramid bleed change protected or out-of-support pixels.
    result = np.empty(original.shape, np.uint8)
    def finish(a, b):
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
        fade = p.get('fade_frames', 0)
        require(isinstance(fade, int) and 0 <= fade <= len(indices)//2, 'Invalid fade_frames')
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


def segment_count(frames):
    # Opt-in: 1 (default) keeps the single-encoder bitstream; segments shorter than 12 frames are merged.
    value = os.environ.get('FLOW_SEGMENTS', '1')
    # 32 stays below the 61-process limit of Windows process pools.
    require(value.isdigit() and 1 <= int(value) <= 32, 'FLOW_SEGMENTS must be 1 to 32')
    return max(1, min(int(value), frames//12))


def encoder(path, size, fps, source, color, audio_filter=None, audio=True, preset='fast'):
    require(size[0] % 2 == 0 and size[1] % 2 == 0, 'Output dimensions must be even for yuv420p')
    # Frames arrive as yuv420p already converted with the source matrix; tag via bitstream
    # metadata, because output-side -colorspace flags make FFmpeg insert a lossy conversion.
    tags = (f"h264_metadata=colour_primaries={PRIMARY_CODES[color['primaries']]}"
            f":transfer_characteristics={TRANSFER_CODES[color['transfer']]}"
            f":matrix_coefficients={MATRIX_CODES[color['matrix_tag']]}"
            f":video_full_range_flag={int(color['range'] == 'pc')}")
    cmd = [FFMPEG, '-v', 'error', '-n', '-f', 'rawvideo', '-pix_fmt', 'yuv420p',
           '-s', f'{size[0]}x{size[1]}', '-r', fps, '-protocol_whitelist', 'pipe', '-i', 'pipe:0',
           *(file_input(source) if audio else []),
           '-map', '0:v:0', *(['-map', '1:a:0?'] if audio else ['-an']),
           '-c:v', 'libx264', '-threads', encode_threads(),
           '-crf', '17', '-preset', preset, '-pix_fmt', 'yuv420p', '-bsf:v', tags]
    if audio and audio_filter is None:
        cmd += ['-c:a', 'copy']
    elif audio:
        cmd += ['-af', audio_filter, '-c:a', 'aac', '-b:a', '192k']
    return cmd+['-movflags', '+faststart', str(path)]


def trimmed_audio(first, last, fps):
    """Source audio for frames [first,last), padded to the exact video duration."""
    return (f'atrim=start={float(first/fps)}:end={float(last/fps)},asetpts=PTS-STARTPTS,'
            f'apad,atrim=duration={float((last-first)/fps)}')


def process(cfg, info, mode, out, plan, span=None, segment=False, preset='fast'):
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
                          audio=not segment, preset=preset)
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
                            original = cv2.resize(roi, ws, interpolation=cv2.INTER_LANCZOS4)
                            patch = cv2.resize(reader.get(j), ws, interpolation=cv2.INTER_LANCZOS4)
                            affine = curve(p.get('source_to_generated_affine',[1,0,0,0,1,0]),i).reshape(2,3)
                            patch = cv2.warpAffine(patch,affine,ws,
                                                  flags=cv2.INTER_LINEAR|cv2.WARP_INVERSE_MAP,
                                                  borderMode=cv2.BORDER_CONSTANT)
                            gain, offset = p.get('gain_bgr',[1,1,1]), p.get('offset_bgr',[0,0,0])
                            if list(gain) != [1,1,1] or list(offset) != [0,0,0]:  # identity is a no-op
                                patch = np.clip(patch.astype(float)*gain+offset,0,255).round().astype(np.uint8)
                            m = matte(p['matte'],i,ws); fade = p.get('fade_frames',0)
                            if fade:
                                k = i-p['target'][0]
                                m *= min(1,k/fade,(len(q['indices'])-1-k)/fade)
                            merged = blend(original,patch,m,p.get('blend','alpha'),p.get('levels',5))
                            # Enforce support again after scaling (including protected mouth).
                            support = cv2.resize(m,(w,h),interpolation=cv2.INTER_NEAREST)>0
                            if (w, h) == ws:
                                # Same-size resizes are exact copies, so roi+delta is merged itself.
                                scene[y:y+h,x:x+w] = merged
                            else:
                                diff = np.empty(merged.shape, float)
                                def work(a, b):
                                    diff[a:b] = merged[a:b].astype(float)-original[a:b].astype(float)
                                in_bands(diff.shape[0], work)
                                delta = cv2.resize(diff,(w,h),interpolation=cv2.INTER_LINEAR)
                                def work(a, b, x=x, y=y):
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
    cfg, info, plans, span, path, preset = job
    return process(cfg, info, 'merge', Path(path), plans, span, segment=True, preset=preset)[1]


def merge_segmented(cfg, info, plans, out, count, preset='fast'):
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


def verify(source, output, expected, copy_audio):
    # Independent full reads of the output run side by side; every check still runs.
    with ThreadPoolExecutor(4) as pool:
        probed = pool.submit(probe, output)
        decoded = pool.submit(run, [FFMPEG,'-v','error','-xerror',*file_input(output),'-f','null','-'])
        hashes = ([pool.submit(audio_hash, source), pool.submit(audio_hash, output)]
                  if copy_audio and expected['audio'] else None)
        result = probed.result()
        require(result['size'] == expected['size'] and result['frames'] == expected['frames']
                and result['fps'] == expected['fps'] and result['audio'] == expected['audio'],
                'Output size, frames, fps or audio presence mismatch')
        decoded.result()
        ah = None
        if hashes:
            ah = hashes[0].result()
            require(ah == hashes[1].result(), 'Original audio payload changed')
    return {'decode_pass':True, 'output':result, 'audio_payload_sha256':ah}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=['doctor','inspect','prepare','merge'])
    parser.add_argument('--config',type=Path)
    parser.add_argument('--source',type=Path)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--dry-run',action='store_true')
    parser.add_argument('--frames',metavar='START:END',
                        help='merge only: export source frames [START,END) for checking (not deliverable)')
    parser.add_argument('--preview',action='store_true',
                        help='merge only: fast ultrafast encode for checking (not deliverable)')
    args = parser.parse_args()
    for t in (FFMPEG,FFPROBE):
        require(shutil.which(t), f'Tool missing: {t}; see references/install.md')
    if args.mode == 'doctor':
        encoders = run([FFMPEG,'-v','error','-encoders']).decode()
        require('libx264' in encoders, 'FFmpeg needs libx264 encoder')
        print(json.dumps({'security_notice':'Capability check only. Verify patched runtime versions per SECURITY.md before using untrusted media.',
                          'python':os.sys.version,'opencv':cv2.__version__, 'numpy':np.__version__,
                          'ffmpeg':run([FFMPEG,'-version']).decode().splitlines()[0],
                          'ffprobe':run([FFPROBE,'-version']).decode().splitlines()[0],
                          'libx264':True,'encode_threads':encode_threads(),
                          'segments':segment_count(1 << 30)},ensure_ascii=False,indent=2)); return
    if args.mode == 'inspect':
        require(args.source is not None, '--source required')
        print(json.dumps(probe(args.source),indent=2)); return
    require(args.config is not None and args.output is not None, '--config and --output required')
    require(args.mode == 'merge' or not (args.frames or args.preview),
            '--frames/--preview apply to merge only; Flow uploads always use the full prepare encode')
    span = None
    if args.frames is not None:
        parts = args.frames.split(':')
        require(len(parts) == 2 and all(v.isdigit() for v in parts), '--frames must be START:END')
        span = (int(parts[0]), int(parts[1]))
    preset = 'ultrafast' if args.preview else 'fast'
    deliverable = span is None and not args.preview
    cfg = load(args.config.resolve())
    # Deliverable runs always re-probe in full; check runs may reuse probes of unchanged inputs.
    cache = None if deliverable else output_path(args.output).parent/'.flow-probe-cache'
    prefetch_probes([cfg['source'], *(p['generated'] for p in cfg.get('patches', []))], cache)
    info = validate_source(cfg)
    plan = prepare_plan(cfg,info) if args.mode == 'prepare' else merge_plan(cfg,info)
    require(span is None or 0 <= span[0] < span[1] <= info['frames'],
            'Invalid --frames interval for this source')
    if args.dry_run:
        print(json.dumps({'mode':args.mode,'config':cfg,'source_info':info,'plan':plan},
                         ensure_ascii=False,indent=2)); return
    output = output_path(args.output); report = output_path(output.with_suffix('.report.json'))
    require(output.suffix.lower() in ('.mp4','.mov','.mkv'), 'Output must be MP4/MOV/MKV')
    require(not output.exists() and not report.exists(), 'Output/report exists; choose new name')
    output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.flow-patch-',dir=output.parent) as tmp:
        temp = Path(tmp)/output.name
        count = segment_count(info['frames']) if args.mode == 'merge' and span is None else 1
        if count > 1:
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
        result['generated_sha256'] = {p['generated']:sha(p['generated']) for p in cfg.get('patches',[])}
        # Reserve both paths exclusively; remove only files created here on failure.
        with private_output(output, binary=True) as dest, private_output(report) as rpt:
            with temp.open('rb') as src:
                shutil.copyfileobj(src, dest)
            json.dump(result, rpt, ensure_ascii=False, indent=2)
    print(f'PASS{"" if deliverable else " (check only, not deliverable)"}: {output}\nReport: {report}')


if __name__ == '__main__':
    try:
        main()
    except (ValueError,KeyError,OSError,subprocess.CalledProcessError,subprocess.TimeoutExpired) as e:
        if isinstance(e,subprocess.CalledProcessError):
            print(e.stderr.decode(errors='replace') if e.stderr else str(e),file=os.sys.stderr)
        else:
            print(str(e),file=os.sys.stderr)
        raise SystemExit(1)
