#!/usr/bin/env python3
"""Portable Flow patch preparation/composition. No downloads or uploads.

All frame intervals are zero-based [start,end). All rectangles are x,y,w,h.
Paths in JSON are relative to that JSON. See references/parameters.md.
"""
import argparse
import contextlib
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

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


def probe(path):
    path = local_media(path)
    data = json.loads(run([FFPROBE, '-v', 'error', '-count_frames', '-show_streams',
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
    ts = json.loads(run([FFPROBE, '-v', 'error', '-select_streams', 'v:0',
                         '-show_frames', '-show_entries', 'frame=best_effort_timestamp_time',
                         '-of', 'json', '-protocol_whitelist', 'file', path]))['frames']
    times = [float(f['best_effort_timestamp_time']) for f in ts]
    require(len(times) > 0, 'No decoded frames')
    require(all(abs((b-a)-float(1/fps)) < max(0.0001, float(1/fps)*0.002)
                for a, b in zip(times, times[1:])), 'VFR or timestamp gaps; normalize explicitly')
    require(abs(times[0]) < 0.001, 'Video must start near timestamp zero; remux explicitly')
    audio = next((a for a in data['streams'] if a['codec_type'] == 'audio'), None)
    require(audio is None or abs(float(audio.get('start_time', 0))) < 0.001,
            'Audio starts at a nonzero timestamp; normalize offsets explicitly')
    require(len(times) == int(s['nb_read_frames']), 'Frame count mismatch')
    return {'size': [s['width'], s['height']], 'fps': str(fps), 'frames': len(times),
            'audio': audio is not None}


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
    """Sequential decoder; permits repeated indices, refuses backwards reads."""
    def __init__(self, path, size):
        self.w, self.h = checked_size(size)
        self.log = tempfile.TemporaryFile()
        self.p = subprocess.Popen([FFMPEG, '-v', 'error', '-noautorotate', *file_input(path),
                                   '-map', '0:v:0', '-an', '-fps_mode', 'passthrough',
                                   '-f', 'rawvideo', '-pix_fmt', 'bgr24', '-'],
                                  stdout=subprocess.PIPE, stderr=self.log)
        self.index = -1
        self.last = None

    def get(self, index):
        require(index >= self.index, 'Non-monotone mapping is unsupported')
        while self.index < index:
            count = self.w*self.h*3
            blocks = bytearray()
            while len(blocks) < count:
                b = self.p.stdout.read(count-len(blocks))
                if not b:
                    raise ValueError(f'Decoder ended before frame {index}')
                blocks.extend(b)
            self.last = np.frombuffer(blocks, np.uint8).reshape(self.h, self.w, 3)
            self.index += 1
        return self.last

    def close(self):
        self.p.stdout.close()
        stop_process(self.p)
        self.log.close()


def matte(spec, frame, size):
    checked_size(size)
    x, y, w, h = rectangle(spec['region'], frame, size)
    feather = float(spec.get('feather', 16))
    require(0 <= feather < min(w, h)/2, 'Feather must be >=0 and < half region width/height')
    yy, xx = np.mgrid[:size[1], :size[0]]
    if spec.get('shape', 'rect') == 'ellipse':
        distance = (1-np.sqrt(((xx-(x+(w-1)/2))/(w/2))**2 +
                              ((yy-(y+(h-1)/2))/(h/2))**2))*min(w,h)/2
    else:
        require(spec.get('shape', 'rect') == 'rect', 'Unknown matte shape')
        distance = np.minimum.reduce([xx-x, x+w-1-xx, yy-y, y+h-1-yy]).astype(float)
    m = (distance >= 0).astype(np.float32) if feather == 0 else np.clip(distance/feather, 0, 1)
    m = (m*m*(3-2*m)).astype(np.float32)
    for box in spec.get('protect', []):
        px, py, pw, ph = rectangle(box, frame, size)
        m[py:py+ph, px:px+pw] = 0
    return m


def blend(original, patch, m, method, levels):
    if method == 'alpha':
        out = original.astype(np.float32)*(1-m[:,:,None])+patch.astype(np.float32)*m[:,:,None]
    else:
        require(method == 'multiband', 'Unknown blend method')
        require(isinstance(levels, int) and 1 <= levels <= 6 and min(m.shape) >= 2**levels*2,
                'Invalid pyramid level count for patch size')
        ga, gb, gm = [original.astype(np.float32)], [patch.astype(np.float32)], [m]
        for _ in range(levels):
            ga.append(cv2.pyrDown(ga[-1])); gb.append(cv2.pyrDown(gb[-1])); gm.append(cv2.pyrDown(gm[-1]))
        out = ga[-1]*(1-gm[-1][:,:,None])+gb[-1]*gm[-1][:,:,None]
        for k in range(levels-1, -1, -1):
            size = (ga[k].shape[1], ga[k].shape[0])
            la = ga[k]-cv2.pyrUp(ga[k+1], dstsize=size)
            lb = gb[k]-cv2.pyrUp(gb[k+1], dstsize=size)
            out = cv2.pyrUp(out, dstsize=size)+la*(1-gm[k][:,:,None])+lb*gm[k][:,:,None]
    # Never let pyramid bleed change protected or out-of-support pixels.
    out = np.clip(out, 0, 255).round().astype(np.uint8)
    out[m == 0] = original[m == 0]
    return out


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
        for i in range(*p['target']):
            crop = rectangle(p['crop'], i, info['size'])
            require(abs((crop[2]/crop[3])/(size[0]/size[1])-1) < 0.005, 'Crop/work aspect mismatch')
            m = matte(p['matte'], i, size)
            affine = curve(p.get('source_to_generated_affine', [1,0,0,0,1,0]), i).reshape(2,3)
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


def encoder(path, size, fps, source, audio_filter=None):
    cmd = [FFMPEG, '-v', 'error', '-n', '-f', 'rawvideo', '-pix_fmt', 'bgr24',
           '-s', f'{size[0]}x{size[1]}', '-r', fps, '-protocol_whitelist', 'pipe', '-i', 'pipe:0', *file_input(source),
           '-map', '0:v:0', '-map', '1:a:0?', '-c:v', 'libx264', '-threads', '4',
           '-crf', '17', '-preset', 'fast', '-pix_fmt', 'yuv420p']
    if audio_filter is None:
        cmd += ['-c:a', 'copy']
    else:
        cmd += ['-af', audio_filter, '-c:a', 'aac', '-b:a', '192k']
    return cmd+['-movflags', '+faststart', str(path)]


def process(cfg, info, mode, out):
    with contextlib.ExitStack() as stack:
        source = Reader(cfg['source'], info['size']); stack.callback(source.close)
        if mode == 'prepare':
            plan = prepare_plan(cfg, info); p = cfg['prepare']; size = plan['size']
            fps = Fraction(info['fps']); first, last = plan['first'], plan['last']
            af = (f'atrim=start={float(first/fps)}:end={float(last/fps)},asetpts=PTS-STARTPTS,'
                  f'apad,atrim=duration={float((last-first)/fps)}')
            cmd = encoder(out, size, info['fps'], cfg['source'], af)
        else:
            plans = merge_plan(cfg, info); size = info['size']; first, last = 0, info['frames']
            readers = [Reader(p['generated'], q['info']['size']) for p,q in zip(cfg['patches'],plans)]
            for reader in readers:
                stack.callback(reader.close)
            cmd = encoder(out, size, info['fps'], cfg['source'])
            plan = {'size': size, 'frames': last, 'audio': info['audio']}
        log = stack.enter_context(tempfile.TemporaryFile())
        enc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=log)
        try:
            for i in range(first, last):
                scene = source.get(i).copy()
                if mode == 'prepare':
                    x,y,w,h = rectangle(p['crop'], i, info['size'])
                    scene = cv2.resize(scene[y:y+h,x:x+w], tuple(size), interpolation=cv2.INTER_LANCZOS4)
                    if p['core'][0] <= i < p['core'][1]:
                        for box in p['mask_regions']:
                            bx,by,bw,bh = rectangle(box, i, size)
                            scene[by:by+bh,bx:bx+bw] = p.get('color_rgb',[0,0,0])[::-1]
                else:
                    for p,q,reader in zip(cfg['patches'],plans,readers):
                        if not p['target'][0] <= i < p['target'][1]:
                            continue
                        j = q['indices'][i-p['target'][0]]
                        if j is None:
                            continue
                        x,y,w,h = rectangle(p['crop'], i, size); ws = tuple(p['work_size'])
                        roi = scene[y:y+h,x:x+w].copy()
                        original = cv2.resize(roi, ws, interpolation=cv2.INTER_LANCZOS4)
                        patch = cv2.resize(reader.get(j), ws, interpolation=cv2.INTER_LANCZOS4)
                        affine = curve(p.get('source_to_generated_affine',[1,0,0,0,1,0]),i).reshape(2,3)
                        patch = cv2.warpAffine(patch,affine,ws,
                                              flags=cv2.INTER_LINEAR|cv2.WARP_INVERSE_MAP,
                                              borderMode=cv2.BORDER_CONSTANT)
                        patch = np.clip(patch.astype(float)*p.get('gain_bgr',[1,1,1])+
                                        p.get('offset_bgr',[0,0,0]),0,255).round().astype(np.uint8)
                        m = matte(p['matte'],i,ws); fade = p.get('fade_frames',0)
                        if fade:
                            k = i-p['target'][0]
                            m *= min(1,k/fade,(len(q['indices'])-1-k)/fade)
                        merged = blend(original,patch,m,p.get('blend','alpha'),p.get('levels',5))
                        delta = cv2.resize(merged.astype(float)-original.astype(float),(w,h),
                                           interpolation=cv2.INTER_LINEAR)
                        # Enforce support again after scaling (including protected mouth).
                        support = cv2.resize(m,(w,h),interpolation=cv2.INTER_NEAREST)>0
                        delta[~support] = 0
                        scene[y:y+h,x:x+w] = np.clip(roi.astype(float)+delta,0,255).round().astype(np.uint8)
                enc.stdin.write(scene.tobytes())
            enc.stdin.close()
            code = enc.wait()
            log.seek(0)
            require(code == 0, 'Encoder failed: '+log.read().decode(errors='replace'))
        finally:
            if not enc.stdin.closed:
                enc.stdin.close()
            if enc.poll() is None:
                stop_process(enc)
        return plan, cmd


def verify(source, output, expected, copy_audio):
    result = probe(output)
    require(result['size'] == expected['size'] and result['frames'] == expected['frames']
            and result['fps'] == expected['fps'] and result['audio'] == expected['audio'],
            'Output size, frames, fps or audio presence mismatch')
    run([FFMPEG,'-v','error','-xerror',*file_input(output),'-f','null','-'])
    ah = None
    if copy_audio and result['audio']:
        ah = audio_hash(source)
        require(ah == audio_hash(output), 'Original audio payload changed')
    return {'decode_pass':True, 'output':result, 'audio_payload_sha256':ah}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=['doctor','inspect','prepare','merge'])
    parser.add_argument('--config',type=Path)
    parser.add_argument('--source',type=Path)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--dry-run',action='store_true')
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
                          'libx264':True},ensure_ascii=False,indent=2)); return
    if args.mode == 'inspect':
        require(args.source is not None, '--source required')
        print(json.dumps(probe(args.source),indent=2)); return
    require(args.config is not None and args.output is not None, '--config and --output required')
    cfg = load(args.config.resolve()); info = validate_source(cfg)
    plan = prepare_plan(cfg,info) if args.mode == 'prepare' else merge_plan(cfg,info)
    if args.dry_run:
        print(json.dumps({'mode':args.mode,'config':cfg,'source_info':info,'plan':plan},
                         ensure_ascii=False,indent=2)); return
    output = output_path(args.output); report = output_path(output.with_suffix('.report.json'))
    require(output.suffix.lower() in ('.mp4','.mov','.mkv'), 'Output must be MP4/MOV/MKV')
    require(not output.exists() and not report.exists(), 'Output/report exists; choose new name')
    output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.flow-patch-',dir=output.parent) as tmp:
        temp = Path(tmp)/output.name
        plan,cmd = process(cfg,info,args.mode,temp)
        expected = dict(plan,fps=info['fps'])
        result = verify(cfg['source'],temp,expected,args.mode=='merge')
        result['output'].update(plan)
        result.update(mode=args.mode,configuration=cfg,command_argv=cmd,
                      source_sha256=sha(cfg['source']),output_sha256=sha(temp),
                      visual_review='pending: decode/frame/audio checks do not prove visual quality or lip sync')
        result['generated_sha256'] = {p['generated']:sha(p['generated']) for p in cfg.get('patches',[])}
        # Reserve both paths exclusively; remove only files created here on failure.
        with private_output(output, binary=True) as dest, private_output(report) as rpt:
            with temp.open('rb') as src:
                shutil.copyfileobj(src, dest)
            json.dump(result, rpt, ensure_ascii=False, indent=2)
    print(f'PASS: {output}\nReport: {report}')


if __name__ == '__main__':
    try:
        main()
    except (ValueError,KeyError,OSError,subprocess.CalledProcessError,subprocess.TimeoutExpired) as e:
        if isinstance(e,subprocess.CalledProcessError):
            print(e.stderr.decode(errors='replace') if e.stderr else str(e),file=os.sys.stderr)
        else:
            print(str(e),file=os.sys.stderr)
        raise SystemExit(1)
