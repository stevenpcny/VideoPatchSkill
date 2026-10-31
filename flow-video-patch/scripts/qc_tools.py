#!/usr/bin/env python3
"""Read-only QC helpers for Flow returns. No downloads, uploads or media writes except --out images.

  green      frames where the solid mask color survived in a returned clip
  offset     global frame-offset scan between an upload and its return (+ residual camera shift)
  track      coarse head/box tracking by normalized template matching (OpenCV core only)
  sharpness  Laplacian variance of a (tracked) region, optionally resized to final scale
  slit       time-slice image of one column band along a track, for reading landmarks over time
  seam       transition-band mismatch of one merge patch (w=4a(1-a)), before or between candidates
  precheck   return fit pre-check before merging: file, spec, mapping, geometry, residual motion,
             colour, canvas holes and seam, with a provisional "fits / hard / regenerate" call
  review-set     record one visual check in a *.visual-review.json; the overall status is computed
  review-status  evaluate a visual review record without changing it

--brief prints a summary (per-frame lists omitted); --json-out PATH writes the full result to a new file.

All frame indices are zero-based. Rectangles are x,y,w,h in the measured video's pixels.
Results are evidence for a human decision, not proof of identity, lip sync or a seamless merge.
"""
import argparse
import contextlib
import datetime
import functools
from fractions import Fraction
import json
import os
from pathlib import Path
import sys

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import flow_patch as fp  # noqa: E402


def shared_probes(cmd):
    """Each input is fully decoded once per command, however many steps probe it (fp.shared_probes)."""
    @functools.wraps(cmd)
    def run(a):
        with fp.shared_probes():
            return cmd(a)
    return run


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


def gray_regions(path, rects):
    """Per-frame grayscale crops of several rectangles from one sequential read."""
    out = [[] for _ in rects]
    for _, f in frames(path):
        for crops, r in zip(out, rects):
            crops.append(gray_region(f, r))
    return out


@shared_probes
def cmd_offset(a):
    rects = [a.region] + ([a.motion_region] if a.motion_region else [])
    (U, *Um), (G, *Gm) = gray_regions(a.upload, rects), gray_regions(a.returned, rects)
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
        Um, Gm = Um[0], Gm[0]
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
    boxes = json.loads(Path(a.track).read_text(encoding='utf-8'))['boxes'] if a.track else None
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
    boxes = json.loads(Path(a.track).read_text(encoding='utf-8'))['boxes']
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


def mapped_frames(cfg, info, gi, p, indices, ks):
    """(source frame, generated frame index, source crop, generated frame) for each target offset in ks
    (ascending, all mapped), from one sequential pass over each file; seam and precheck share it."""
    first = p['target'][0]
    grid = lambda i: i['fps'] if i.get('on_frame_grid') else None
    src = fp.Reader(cfg['source'], info['size'], info['color'], first+ks[0], grid(info))
    gen = fp.Reader(p['generated'], gi['size'], gi['color'], min(indices[k] for k in ks), grid(gi))
    try:
        for k in ks:
            i, j = first+k, indices[k]
            yield i, j, src.region(i, fp.rectangle(p['crop'], i, info['size'])), gen.get(j)
    finally:
        src.close(); gen.close()


def seam_row(p, i, roi, generated, count, sigma, threshold):
    original, patch, m = fp.patch_inputs(p, i, roi, generated, count)
    o, g = original.astype(np.float32), patch.astype(np.float32)
    blur = lambda im: cv2.GaussianBlur(im, (0, 0), sigma)
    lo, lg = blur(o), blur(g)
    high = np.abs((g-lg)-(o-lo)).mean(2)  # new vs old detail, each minus its own low-pass
    low = np.abs(lg-lo).mean(2)           # colour/brightness step across the seam
    wgt = 4*m*(1-m); band = wgt > .2
    total = float(wgt[band].sum())
    return {'frame': i, 'band_pixels': int(band.sum()),
            'high_freq_weighted_mean': float((high*wgt)[band].sum()/total) if total else 0.0,
            'low_freq_weighted_mean': float((low*wgt)[band].sum()/total) if total else 0.0,
            'large_high_freq_pixels': int(((wgt > .5) & (high > threshold)).sum())}


def seam_ks(indices, step):
    ks = [k for k, j in enumerate(indices) if j is not None and not k % step]
    fp.require(ks, 'No mapped frames to measure')
    return ks


def seam_result(patch, rows, sigma, threshold):
    worst = sorted(rows, key=lambda r: -r['high_freq_weighted_mean'])[:5]
    key = lambda name: [r[name] for r in rows]
    return {'patch': patch, 'frames_measured': len(rows), 'sigma': sigma, 'threshold': threshold,
            'summary': {name: {'mean': float(np.mean(key(name))), 'max': float(np.max(key(name)))}
                        for name in ('high_freq_weighted_mean', 'low_freq_weighted_mean',
                                     'large_high_freq_pixels')},
            'worst_frames': [r['frame'] for r in worst], 'frames': rows,
            'note': 'Compare candidates of the same shot; lower is a less visible seam. Not visual acceptance.'}


@shared_probes
def cmd_seam(a):
    """Score one patch's seam exactly as merge would blend it (references/return-fit-assessment.md 5)."""
    cfg = fp.load(Path(a.config).resolve()); info = fp.validate_source(cfg)
    fp.require(0 <= a.patch < len(cfg.get('patches', [])), 'Patch index out of range')
    fp.require(a.step >= 1 and a.sigma > 0 and a.threshold >= 0, 'Invalid step/sigma/threshold')
    p = cfg['patches'][a.patch]; gi = fp.probe(p['generated'])
    indices = fp.merge_plan(dict(cfg, patches=[p]), info)[0]['indices']
    with contextlib.closing(mapped_frames(cfg, info, gi, p, indices, seam_ks(indices, a.step))) as frames:
        rows = [seam_row(p, i, roi, g, len(indices), a.sigma, a.threshold) for i, _, roi, g in frames]
    return seam_result(a.patch, rows, a.sigma, a.threshold)


def precheck_row(p, i, j, roi, generated):
    ws = tuple(p['work_size'])
    original = cv2.resize(roi, ws, interpolation=cv2.INTER_LANCZOS4)
    patch = cv2.resize(generated, ws, interpolation=cv2.INTER_LANCZOS4)
    m = fp.matte(p['matte'], i, ws)
    outside = cv2.dilate((m > 0).astype(np.uint8), np.ones((15, 15), np.uint8)) == 0
    for box in p.get('fit_exclude', []):
        bx, by, bw, bh = fp.rectangle(box, i, ws); outside[by:by+bh, bx:bx+bw] = False
    ga, gb = cv2.cvtColor(original, cv2.COLOR_BGR2GRAY), cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)
    row = {'frame': i, 'generated_frame': j}
    try:
        corr, warp = cv2.findTransformECC(ga, gb, np.eye(2, 3, dtype=np.float32), cv2.MOTION_AFFINE,
                                          (cv2.TERM_CRITERIA_COUNT | cv2.TERM_CRITERIA_EPS, 100, 1e-5),
                                          outside.astype(np.uint8)*255, 5)
    except cv2.error:
        return dict(row, correlation=None)
    aligned = cv2.warpAffine(patch, warp, ws, flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP)
    inside = cv2.warpAffine(np.ones(ws[::-1], np.uint8), warp, ws,
                            flags=cv2.INTER_NEAREST | cv2.WARP_INVERSE_MAP) > 0
    flow = cv2.calcOpticalFlowFarneback(ga, cv2.cvtColor(aligned, cv2.COLOR_BGR2GRAY), None,
                                        0.5, 3, 21, 3, 5, 1.1, 0)
    texture = np.abs(cv2.Laplacian(ga, cv2.CV_32F)) > 8  # flat backgrounds give no motion evidence
    use = outside & inside & texture
    motion = np.hypot(flow[..., 0], flow[..., 1])[use]
    diff = (aligned.astype(np.float32)-original.astype(np.float32))[outside & inside]
    return dict(row, correlation=float(corr), scale=float(np.sqrt(abs(np.linalg.det(warp[:, :2])))),
                shift=[float(warp[0, 2]), float(warp[1, 2])],
                residual_motion_median=float(np.median(motion)) if motion.size else None,
                residual_motion_p90=float(np.percentile(motion, 90)) if motion.size else None,
                colour_offset_bgr=[float(v) for v in np.median(diff, 0)] if diff.size else None,
                canvas_holes_in_patch=int(((m > 0) & ~inside).sum()))


@shared_probes
def cmd_precheck(a):
    """references/return-fit-assessment.md section 1 as numbers; people still judge blinks, expression,
    identity and performance. Geometry uses everything outside the (dilated) patch matte."""
    cfg = fp.load(Path(a.config).resolve()); info = fp.validate_source(cfg)
    fp.require(0 <= a.patch < len(cfg.get('patches', [])), 'Patch index out of range')
    fp.require(a.samples >= 2, 'Need at least two sample frames')
    p = cfg['patches'][a.patch]; gi = fp.probe(p['generated'])
    plan = fp.merge_plan(dict(cfg, patches=[p]), info)[0]; indices = plan['indices']
    valid = [(k, j) for k, j in enumerate(indices) if j is not None]
    fp.require(valid, 'No mapped frames to check')
    picks = {k for k, _ in {valid[round(i*(len(valid)-1)/(a.samples-1))] for i in range(a.samples)}}
    # The seam score samples its own frames (every step-th); both sets are kept exactly, read in one pass.
    seam_frames = set(seam_ks(indices, max(1, len(indices)//a.samples)))
    rows, seam_rows = [], []
    with contextlib.closing(mapped_frames(cfg, info, gi, p, indices, sorted(picks | seam_frames))) as frames:
        for i, j, roi, g in frames:
            k = i-p['target'][0]
            if k in picks:
                rows.append(precheck_row(p, i, j, roi, g))
            if k in seam_frames:
                seam_rows.append(seam_row(p, i, roi, g, len(indices), 3, 12))
    ok = [r for r in rows if r.get('correlation') is not None]
    seam = seam_result(a.patch, seam_rows, 3, 12)['summary']
    mapped = [j for j in indices if j is not None]
    summary = {
        'generated_sha256': fp.sha(p['generated']),
        'spec': {'generated_size': gi['size'], 'generated_frames': gi['frames'], 'fps': gi['fps'],
                 'target_frames': len(indices), 'mapped_frames': len(mapped),
                 'repeated_frames': len(mapped)-len(set(mapped)), 'kept_original_frames': indices.count(None)},
        'frames_sampled': len(rows), 'registration_failed': len(rows)-len(ok)}
    if ok:
        scales = [r['scale'] for r in ok]; shifts = [r['shift'] for r in ok]
        p90 = [r['residual_motion_p90'] for r in ok if r['residual_motion_p90'] is not None]
        summary.update(correlation_min=min(r['correlation'] for r in ok), scale_range=[min(scales), max(scales)],
                       shift_range_px=[float(np.ptp([s[0] for s in shifts])), float(np.ptp([s[1] for s in shifts]))],
                       residual_motion_p90_max=max(p90) if p90 else None,
                       colour_offset_bgr_median=[float(v) for v in np.median([r['colour_offset_bgr'] for r in ok
                                                                             if r['colour_offset_bgr']], 0)],
                       canvas_holes_max=max(r['canvas_holes_in_patch'] for r in ok))
    # Provisional call from the magnitudes in return-fit-assessment.md; a person confirms it.
    if len(ok) < len(rows) or not ok:
        call = 'regenerate'
    else:
        drift = max(abs(summary['scale_range'][0]-1), abs(summary['scale_range'][1]-1))
        motion = summary['residual_motion_p90_max'] or 0
        if summary['correlation_min'] >= .97 and drift <= .01 and max(summary['shift_range_px']) <= 2 and motion <= 2 \
                and summary['canvas_holes_max'] == 0:
            call = 'fits'
        elif summary['correlation_min'] >= .9 and drift <= .03 and motion <= 5:
            call = 'hard'
        else:
            call = 'regenerate'
    return {'patch': a.patch, 'provisional_call': call, 'summary': summary, 'seam': seam, 'samples': rows,
            'manual_checks_remaining': ['blinks and gaze frame by frame', 'expression and mouth/lip sync',
                                        'identity and accessories', 'mic/hand and face residuals by region',
                                        'time mapping against the upload (qc_tools offset)'],
            'note': 'Evidence for a decision, not visual acceptance; thresholds are orders of magnitude.'}


REVIEW_TEMPLATE = Path(__file__).resolve().parents[1]/'assets'/'visual-review-template.json'
CHECK_STATUSES = ('not_run', 'pass', 'fail', 'not_applicable')


def review_template():
    return json.loads(REVIEW_TEMPLATE.read_text(encoding='utf-8'))


def under(base, value):
    p = Path(value).expanduser()
    return p if p.is_absolute() else base/p


def rel(path, base):
    try:
        return Path(os.path.relpath(Path(path).resolve(), base.resolve())).as_posix()
    except ValueError:
        return str(Path(path).resolve())


def evidence_problem(base, entry):
    """None if the evidence entry still points at the file it was recorded with."""
    path = under(base, entry['path'] if isinstance(entry, dict) else entry)
    if not path.is_file():
        return f'missing evidence file {path.name}'
    if isinstance(entry, dict) and entry.get('sha256') and fp.sha(path) != entry['sha256']:
        return f'evidence file {path.name} changed after it was recorded'
    return None


def uncovered(intervals, required):
    """Parts of the required [a,b) intervals outside the union of the checked [a,b) intervals."""
    merged = []
    for x, y in sorted(tuple(i) for i in intervals):
        if merged and x <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], y)
        else:
            merged.append([x, y])
    gaps = []
    for a, b in required:
        pos = a
        for x, y in merged:
            if y <= pos:
                continue
            if x >= b:
                break
            if x > pos:
                gaps.append([pos, x])
            pos = y
            if pos >= b:
                break
        if pos < b:
            gaps.append([pos, b])
    return gaps


def required_coverage(record, report, template):
    """Per coverage-type check, the source intervals its review must span (from the technical report)."""
    targets = [p['target'] for p in report['configuration']['patches']]
    frames = report['output']['frames']
    half = round(Fraction(str(report['output']['fps']))*Fraction(str(
        template['coverage_requirements']['join_window_seconds'])))
    joins = []
    for a, b in targets:
        joins += [[max(0, a-half), min(frames, a+half)], [max(0, b-half), min(frames, b+half)]]
    joins = [w for w in joins if w[0] < w[1]]
    req = {cid: targets for cid in template['coverage_requirements']['repair_intervals']}
    req.update({cid: joins for cid in template['coverage_requirements']['join_windows']})
    return req


def review_evaluate(record_path, record):
    """Overall status by rule: any fail -> visual_fail; any not_run / missing / unconfirmed -> pending;
    visual_pass only when every gate below also holds. Returns (status, blocking reasons)."""
    base, template = record_path.parent, review_template()
    ids = [c['id'] for c in template['visual_checks']]
    checks = {c.get('id'): c for c in record.get('visual_checks', [])}
    failed, pending = [], []
    for cid in checks:
        if cid not in ids:
            pending.append(f'unknown check id {cid!r}')
    for cid in ids:
        c = checks.get(cid)
        if c is None:
            pending.append(f'{cid}: missing from the record'); continue
        status = c.get('status')
        if status not in CHECK_STATUSES:
            pending.append(f'{cid}: invalid status {status!r}')
        elif status == 'fail':
            failed.append(f'{cid}: fail')
        elif status == 'not_run':
            pending.append(f'{cid}: not_run')
        elif status == 'not_applicable' and not str(c.get('notes', '')).strip():
            pending.append(f'{cid}: not_applicable without a reason')
        elif status == 'pass' and c.get('reviewer') == 'user' and not c.get('confirmation'):
            pending.append(f'{cid}: user review without a confirmation source')
    if failed:
        return 'visual_fail', failed+pending
    if pending:
        return 'pending_visual_review', pending
    gates = []
    if record.get('technical_validation', {}).get('status') != 'pass':
        gates.append('technical_validation is not pass')
    output = under(base, record.get('output', ''))
    current = fp.sha(output) if output.is_file() else None
    if current is None:
        gates.append('output file not found')
    elif current != record.get('output_sha256'):
        gates.append('output file SHA-256 differs from the record')
    report_path = under(base, record.get('technical_report', ''))
    report = json.loads(report_path.read_text(encoding='utf-8')) if report_path.is_file() else None
    if report is None:
        gates.append('technical report not found')
    else:
        if report.get('mode') != 'merge' or report.get('scope', {}).get('deliverable') is not True:
            gates.append('technical report is not a deliverable full merge (check exports never pass)')
        if report.get('output_sha256') != record.get('output_sha256'):
            gates.append('technical report output SHA-256 differs from the record')
    for cid in ids:
        c = checks[cid]
        if c['status'] != 'pass':
            continue
        if not c.get('evidence'):
            gates.append(f'{cid}: pass without evidence')
        gates += [f'{cid}: {e}' for e in filter(None, (evidence_problem(base, x) for x in c.get('evidence', [])))]
    if report is not None and 'configuration' in report:
        for cid, need in required_coverage(record, report, template).items():
            if checks[cid]['status'] == 'pass':
                gaps = uncovered(checks[cid].get('source_intervals_checked', []), need)
                if gaps:
                    gates.append(f'{cid}: reviewed intervals miss source frames {gaps}')
    return ('pending_visual_review', gates) if gates else ('visual_pass', [])


def parse_interval(text, frames):
    parts = text.split(':')
    fp.require(len(parts) == 2 and all(v.isdigit() for v in parts), f'Interval {text!r} must be START:END')
    a, b = map(int, parts)
    fp.require(0 <= a < b <= frames, f'Interval {text!r} outside [0,{frames})')
    return [a, b]


def cmd_review_set(a):
    """Record one check of a visual review record bound to the current final file. The overall status
    is recomputed by review_evaluate and cannot be set by hand."""
    path = Path(a.record).resolve(); base = path.parent
    record = json.loads(path.read_text(encoding='utf-8'))
    ids = [c['id'] for c in review_template()['visual_checks']]
    fp.require(a.check in ids, f'Unknown check id {a.check!r}; see assets/visual-review-template.json')
    checks = {c.get('id'): c for c in record.get('visual_checks', [])}
    fp.require(a.check in checks, f'Check {a.check!r} is missing from this record; regenerate it from the template')
    output = under(base, record.get('output', ''))
    fp.require(output.is_file(), 'The record\'s output file is not found next to the record')
    current = fp.sha(output)
    fp.require(current == record.get('output_sha256'), 'Output SHA-256 differs from the record: the record belongs '
               'to another file and its checks no longer apply; use the record written with the new export')
    fp.require(a.status in CHECK_STATUSES, 'Invalid status')
    if a.status in ('pass', 'fail'):
        fp.require(a.evidence, f'{a.status} needs at least one --evidence file')
    if a.status == 'not_applicable':
        fp.require(a.note and a.note.strip(), 'not_applicable needs --note with the reason')
    if a.reviewer == 'user' and a.status != 'not_run':
        fp.require(a.confirmation and a.confirmation.strip(),
                   '--reviewer user needs --confirmation: where the user confirmed it (quoted message with time, or '
                   'a signed review file); the agent never writes the user\'s confirmation for them')
    evidence = []
    for e in a.evidence or []:
        f = Path(e).resolve()
        fp.require(f.is_file(), f'Evidence file not found: {e}')
        evidence.append({'path': rel(f, base), 'sha256': fp.sha(f)})
    frames = int(record.get('output_frames') or 0)
    fp.require(frames > 0, 'Record has no output_frames')
    c = checks[a.check]
    c.update(status=a.status, source_intervals_checked=[parse_interval(i, frames) for i in a.intervals or []],
             evidence=evidence, notes=a.note or '', reviewer=a.reviewer, output_sha256=current,
             recorded_at=datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'))
    c.pop('confirmation', None)
    if a.reviewer == 'user' and a.confirmation:
        f = Path(a.confirmation)
        c['confirmation'] = ({'path': rel(f, base), 'sha256': fp.sha(f)} if f.is_file()
                             else {'quote': a.confirmation.strip()})
    status, blocking = review_evaluate(path, record)
    record['overall_visual_status'] = status
    record['overall_status_evaluation'] = {'by': 'qc_tools.py review-set', 'output_sha256': current,
                                           'blocking': blocking}
    fp.replace_json(path, record)
    return {'record': path.name, 'check': a.check, 'status': a.status, 'overall_visual_status': status,
            'blocking': blocking}


def cmd_review_status(a):
    path = Path(a.record).resolve()
    status, blocking = review_evaluate(path, json.loads(path.read_text(encoding='utf-8')))
    return {'record': path.name, 'overall_visual_status': status, 'blocking': blocking}


def brief(cmd, result):
    """Summary for agents: per-frame lists dropped, everything else kept."""
    out = dict(result)
    drop = {'seam': ['frames'], 'precheck': ['samples']}.get(cmd, [])
    for key, value in result.items():
        if key in drop or (isinstance(value, list) and len(value) > 12 and key != 'blocking'):
            out[key] = {'omitted_items': len(value) if isinstance(value, list) else None}
    if any(isinstance(v, dict) and 'omitted_items' in v for v in out.values()):
        out['detail'] = 'omitted (--brief); --json-out writes the full result'
    return out


def main():
    fp.utf8_console()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--brief', action='store_true', help='summary only; per-frame lists omitted')
    common.add_argument('--json-out', metavar='PATH', help='also write the full result to this new file')
    sub = ap.add_subparsers(dest='cmd', required=True)
    _add = sub.add_parser
    sub.add_parser = lambda *x, **k: _add(*x, parents=[common], **k)
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
    s = sub.add_parser('seam'); s.add_argument('--config', required=True)
    s.add_argument('--patch', type=int, default=0); s.add_argument('--step', type=int, default=1)
    s.add_argument('--sigma', type=float, default=3, help='Gaussian low-pass sigma in work pixels')
    s.add_argument('--threshold', type=float, default=12, help='high-frequency difference counted as large')
    s = sub.add_parser('precheck'); s.add_argument('--config', required=True)
    s.add_argument('--patch', type=int, default=0); s.add_argument('--samples', type=int, default=8)
    s = sub.add_parser('review-set'); s.add_argument('--record', required=True)
    s.add_argument('--check', required=True); s.add_argument('--status', required=True, choices=CHECK_STATUSES)
    s.add_argument('--intervals', nargs='*', metavar='START:END', help='source frames reviewed, [START,END)')
    s.add_argument('--evidence', nargs='*', help='evidence files (SHA-256 recorded)')
    s.add_argument('--reviewer', choices=['agent', 'user'], default='agent')
    s.add_argument('--confirmation', help='user reviews: quoted confirmation with time, or a signed review file')
    s.add_argument('--note')
    s = sub.add_parser('review-status'); s.add_argument('--record', required=True)
    a = ap.parse_args()
    json_out = fp.output_path(a.json_out) if a.json_out else None
    fp.require(json_out is None or not json_out.exists(), '--json-out file exists; choose a new name')
    result = globals()['cmd_' + a.cmd.replace('-', '_')](a)
    text = json.dumps(result, ensure_ascii=False)
    if json_out:
        with fp.private_output(json_out) as f:
            f.write(text+'\n')
    print(json.dumps(brief(a.cmd, result), ensure_ascii=False) if a.brief else text)


if __name__ == '__main__':
    main()
