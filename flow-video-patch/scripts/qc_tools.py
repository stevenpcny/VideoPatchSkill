#!/usr/bin/env python3
"""Read-only QC helpers for Flow returns. No downloads, uploads or media writes except --out images.

  green      frames where the solid mask color survived in a returned clip
  offset     global frame-offset scan between an upload and its return (+ residual camera shift)
  track      coarse head/box tracking by normalized template matching (OpenCV core only)
  sharpness  Laplacian variance of a (tracked) region, optionally resized to final scale
  slit       time-slice image of one column band along a track, for reading landmarks over time

All frame indices are zero-based. Rectangles are x,y,w,h in the measured video's pixels.
Results are evidence for a human decision, not proof of identity, lip sync or a seamless merge.
"""
import argparse
import json
from pathlib import Path
import sys

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import flow_patch as fp  # noqa: E402


def frames(path):
    info = fp.probe(path)
    reader = fp.Reader(path, info['size'])
    try:
        for i in range(info['frames']):
            yield i, reader.get(i)
    finally:
        reader.close()


def runs(indices):
    out = []
    for i in indices:
        if out and i == out[-1][1]:
            out[-1][1] = i + 1
        else:
            out.append([i, i + 1])
    return out


def cmd_green(a):
    target = np.array(a.color_rgb[::-1], int)
    hits = [i for i, f in frames(a.video)
            if int((np.abs(f.astype(int) - target).max(axis=2) <= a.tolerance).sum()) >= a.min_pixels]
    return {'video': a.video, 'mask_color_runs': runs(hits), 'frames_with_mask_color': len(hits)}


def gray_region(f, r):
    x, y, w, h = r
    return cv2.cvtColor(f[y:y+h, x:x+w], cv2.COLOR_BGR2GRAY).astype(np.float32)


def cmd_offset(a):
    U = [gray_region(f, a.region) for _, f in frames(a.upload)]
    G = [gray_region(f, a.region) for _, f in frames(a.returned)]
    U, G = np.array(U), np.array(G)
    m = min(len(U), len(G)); halves = {'first_half': (0, m // 2), 'second_half': (m // 2, m)}
    scan = {}
    for name, (lo, hi) in halves.items():
        res = {}
        for k in range(-a.max_shift, a.max_shift + 1):
            a0, a1 = max(lo, -k), min(hi, len(U) - k)
            if a1 - a0 > 2:
                res[k] = float(((U[a0+k:a1+k] - G[a0:a1]) ** 2).mean())
        scan[name] = {'best_offset': min(res, key=res.get), 'mse_by_offset': {str(k): round(v, 1) for k, v in res.items()}}
    out = {'upload_frames': len(U), 'returned_frames': len(G), 'offset_scan': scan,
           'note': 'offset k means returned frame j matches upload frame j+k; equal best offsets in both halves = no drift'}
    if a.motion_region:
        Um = [gray_region(f, a.motion_region) for _, f in frames(a.upload)]
        Gm = [gray_region(f, a.motion_region) for _, f in frames(a.returned)]
        sh = [float(np.hypot(*cv2.phaseCorrelate(Um[j], Gm[j])[0])) for j in range(m)]
        out['residual_shift_px'] = {'mean': round(float(np.mean(sh)), 3), 'max': round(float(np.max(sh)), 3)}
    return out


def cmd_track(a):
    x, y, w, h = a.init
    t0 = prev = None; pos = (x, y); boxes = []
    for i, f in frames(a.video):
        g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
        if t0 is None:
            t0 = g[y:y+h, x:x+w].copy(); prev = t0.copy(); boxes.append([i, x, y, w, h, 1.0]); continue
        px, py = pos; sx, sy = max(px - a.search, 0), max(py - a.search, 0)
        win = g[sy:py+h+a.search, sx:px+w+a.search]; best = None
        for t in (t0, prev):  # first-frame template limits drift, previous-frame template follows pose
            _, mv, _, ml = cv2.minMaxLoc(cv2.matchTemplate(win, t, cv2.TM_CCOEFF_NORMED))
            if best is None or mv > best[0]:
                best = (mv, ml)
        pos = (sx + best[1][0], sy + best[1][1]); prev = g[pos[1]:pos[1]+h, pos[0]:pos[0]+w].copy()
        boxes.append([i, int(pos[0]), int(pos[1]), w, h, round(float(best[0]), 3)])
    scores = [b[5] for b in boxes]
    return {'video': a.video, 'boxes': boxes, 'min_score': min(scores),
            'note': 'template matching follows translation; pitch/turns can slide sub-regions, verify on drawn frames'}


def region_for(a, n, boxes):
    if boxes:
        _, x, y, w, h, _ = boxes[n + a.track_offset]
        dx, dy, rw, rh = a.region
        return [int(x + dx), int(y + dy), int(rw if rw > 0 else w + rw), int(rh)]
    return a.region


def cmd_sharpness(a):
    boxes = json.loads(Path(a.track).read_text())['boxes'] if a.track else None
    vals = []
    for n, f in frames(a.video):
        if a.resize:
            f = cv2.resize(f, tuple(a.resize), interpolation=cv2.INTER_AREA)
        x, y, w, h = region_for(a, n, boxes)
        if a.origin:
            x, y = x - a.origin[0], y - a.origin[1]
        vals.append(float(cv2.Laplacian(cv2.cvtColor(f[y:y+h, x:x+w], cv2.COLOR_BGR2GRAY), cv2.CV_64F).var()))
    return {'video': a.video, 'median': round(float(np.median(vals)), 1), 'per_frame': [round(v, 1) for v in vals],
            'note': 'compare only same region, same final scale, same scene; noise also raises the value'}


def cmd_slit(a):
    boxes = json.loads(Path(a.track).read_text())['boxes']
    cols = []
    for n, f in frames(a.video):
        _, x, y, w, h, _ = boxes[n]
        cols.append(f[int(y)+a.rows[0]:int(y)+a.rows[1], int(x)+a.columns[0]:int(x)+a.columns[1]].mean(axis=1).astype(np.uint8))
    img = np.stack(cols, axis=1)
    img = cv2.resize(img, (img.shape[1] * a.zoom, img.shape[0] * a.zoom * 2), interpolation=cv2.INTER_NEAREST)
    out = fp.output_path(a.out)
    fp.require(not out.exists(), 'Refusing to overwrite an existing image')
    cv2.imwrite(str(out), img)
    return {'image': str(out), 'x_axis': 'frame index * zoom', 'y_axis': f'box-relative rows {a.rows[0]}..{a.rows[1]} * zoom*2'}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('green'); s.add_argument('--video', required=True)
    s.add_argument('--color-rgb', type=int, nargs=3, default=[0, 255, 0])
    s.add_argument('--tolerance', type=int, default=40); s.add_argument('--min-pixels', type=int, default=2000)
    s = sub.add_parser('offset'); s.add_argument('--upload', required=True); s.add_argument('--returned', required=True)
    s.add_argument('--region', type=int, nargs=4, required=True, help='unregenerated area that moves every frame')
    s.add_argument('--motion-region', type=int, nargs=4, help='static background area for camera-shift check')
    s.add_argument('--max-shift', type=int, default=4)
    s = sub.add_parser('track'); s.add_argument('--video', required=True)
    s.add_argument('--init', type=int, nargs=4, required=True, help='frame-0 box x y w h')
    s.add_argument('--search', type=int, default=60)
    s = sub.add_parser('sharpness'); s.add_argument('--video', required=True)
    s.add_argument('--region', type=int, nargs=4, required=True, help='fixed x y w h, or dx dy w h relative to --track box (w<=0: box width + w)')
    s.add_argument('--track'); s.add_argument('--track-offset', type=int, default=0, help='track frame = video frame + offset')
    s.add_argument('--resize', type=int, nargs=2, help='resize each frame to final-scale W H before measuring')
    s.add_argument('--origin', type=int, nargs=2, help='subtract this x y (e.g. crop origin) from tracked coordinates')
    s = sub.add_parser('slit'); s.add_argument('--video', required=True); s.add_argument('--track', required=True)
    s.add_argument('--rows', type=int, nargs=2, required=True); s.add_argument('--columns', type=int, nargs=2, required=True)
    s.add_argument('--zoom', type=int, default=4); s.add_argument('--out', required=True)
    a = ap.parse_args()
    print(json.dumps(globals()['cmd_' + a.cmd](a), ensure_ascii=False))


if __name__ == '__main__':
    main()
