#!/usr/bin/env python3
"""Propose per-frame spatial registration and ONE fixed color fit.

Does not find time offsets or track crop rectangles. Review the proposal before
using it: a high ECC score can still align the wrong repeated background.
--motion euclidean adds rotation; affine adds scale and shear. Both stay bounded by
--max-linear-change so a drifting fit is rejected instead of written.
"""
import argparse
import copy
import json
from pathlib import Path
import contextlib
import cv2
import numpy as np
from flow_patch import (load, validate_source, probe, mapping, Reader, rectangle,
                        matte, require, curve, checked_size, private_output, output_path,
                        utf8_console)

MOTIONS = {'translation': cv2.MOTION_TRANSLATION, 'euclidean': cv2.MOTION_EUCLIDEAN,
           'affine': cv2.MOTION_AFFINE}


def compact_json(cfg):
    """The indent=2 layout, except that per-frame lists (affine keyframes, registration rows) keep one
    entry per line. json.loads gives back exactly the same object as the fully indented form."""
    obj, lists = copy.deepcopy(cfg), {}

    def mark(value):
        key = f'@@flow-compact-{len(lists)}@@'
        lists[key] = value
        return key
    for p in obj.get('patches', []):
        affine = p.get('source_to_generated_affine')
        if isinstance(affine, list) and affine and isinstance(affine[0], dict):
            p['source_to_generated_affine'] = mark(affine)
    if isinstance(obj.get('fit_review', {}).get('registration'), list):
        obj['fit_review']['registration'] = mark(obj['fit_review']['registration'])
    text = json.dumps(obj, ensure_ascii=False, indent=2)
    for key, value in lists.items():
        quoted = json.dumps(key)
        require(text.count(quoted) == 1, 'Configuration text collides with the compact-layout marker')
        at = text.index(quoted)
        line = text[text.rfind('\n', 0, at)+1:at]
        indent = ' '*(len(line)-len(line.lstrip(' ')))
        body = ',\n'.join(indent+'  '+json.dumps(v, ensure_ascii=False, separators=(',', ':')) for v in value)
        text = text.replace(quoted, '[\n'+body+'\n'+indent+']' if value else '[]')
    return text+'\n'


def main():
    utf8_console()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--config', type=Path, required=True)
    ap.add_argument('--patch', type=int, default=0)
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--min-correlation', type=float, default=.8)
    ap.add_argument('--max-shift', type=float, default=24)
    ap.add_argument('--motion', choices=list(MOTIONS), default='translation')
    ap.add_argument('--max-linear-change', type=float, default=.08,
                    help='largest allowed change of the 2x2 linear part from the initial affine')
    args = ap.parse_args()
    require(args.patch >= 0, 'Patch index must be nonnegative')
    require(np.isfinite(args.min_correlation) and -1 <= args.min_correlation <= 1,
            'Correlation must be finite and in [-1,1]')
    require(np.isfinite(args.max_shift) and args.max_shift >= 0, 'Shift must be finite and nonnegative')
    require(np.isfinite(args.max_linear_change) and 0 <= args.max_linear_change <= .5,
            'Linear change bound must be finite and in [0,0.5]')
    args.output = output_path(args.output)
    cfg = load(args.config.resolve()); info = validate_source(cfg)
    require(args.patch < len(cfg.get('patches', [])), 'Patch index out of range')
    require(not args.output.exists(), 'Choose a new output config')
    p = cfg['patches'][args.patch]; gi = probe(p['generated'])
    require(info['fps'] == gi['fps'], 'Align frame rates explicitly first')
    indices = mapping(p, info['frames']); ws = tuple(checked_size(p['work_size']))
    require(all(j is None or j < gi['frames'] for j in indices), 'Invalid frame map')
    require(abs((ws[0]/ws[1])/(gi['size'][0]/gi['size'][1])-1)<.005, 'Aspect mismatch')
    rows, colors, initials = [], [], []
    previous = np.array([[1,0,0],[0,1,0]],np.float32)
    with contextlib.ExitStack() as stack:
        sr = Reader(cfg['source'],info['size']); stack.callback(sr.close)
        gr = Reader(p['generated'],gi['size']); stack.callback(gr.close)
        for k,i in enumerate(range(*p['target'])):
            j = indices[k]
            if j is None:
                continue
            x,y,w,h = rectangle(p['crop'],i,info['size'])
            a = cv2.resize(sr.region(i,(x,y,w,h)),ws)  # same values as the full-frame slice
            b = cv2.resize(gr.get(j),ws)
            m = matte(p['matte'],i,ws)
            excluded = cv2.dilate((m>0).astype(np.uint8),np.ones((15,15),np.uint8))
            # Exclusions use work coordinates, absolute source-frame keyframes.
            for box in p.get('fit_exclude',[]):
                bx,by,bw,bh = rectangle(box,i,ws); excluded[by:by+bh,bx:bx+bw] = 1
            mask = ((excluded == 0)*255).astype(np.uint8)
            require(np.count_nonzero(mask)>max(128,ws[0]*ws[1]*.05),
                    'Too little unchanged background/clothing; use manual alignment')
            initial = curve(p.get('source_to_generated_affine',[1,0,0,0,1,0]),i).reshape(2,3).astype(np.float32)
            try:
                # Translation (default) is deliberately conservative; other motions are bounded.
                guess = initial.copy() if k == 0 else previous.copy()
                corr,warp = cv2.findTransformECC(cv2.cvtColor(a,cv2.COLOR_BGR2GRAY),
                            cv2.cvtColor(b,cv2.COLOR_BGR2GRAY),guess,MOTIONS[args.motion],
                            (cv2.TERM_CRITERIA_COUNT|cv2.TERM_CRITERIA_EPS,100,1e-5),mask,5)
                accepted = bool(corr >= args.min_correlation and
                                np.linalg.norm(warp[:,2]-initial[:,2]) <= args.max_shift and
                                np.abs(warp[:,:2]-initial[:,:2]).max() <= args.max_linear_change)
            except cv2.error:
                corr,warp,accepted = None,initial,False
            if accepted:
                previous = warp.copy()
            rows.append({'frame':i,'correlation':corr,'accepted':accepted,
                         'warp':warp.flatten().tolist()})
            initials.append(initial.flatten())
            # Color sampling spaced over the clip; no per-frame color parameters.
            if accepted and k % max(1,len(indices)//7) == 0:
                aligned = cv2.warpAffine(b,warp,ws,flags=cv2.INTER_LINEAR|cv2.WARP_INVERSE_MAP)
                valid = cv2.warpAffine(np.ones((ws[1],ws[0]),np.uint8),warp,ws,
                                      flags=cv2.INTER_NEAREST|cv2.WARP_INVERSE_MAP)>0
                use = (mask>0)&valid&np.all((a>8)&(a<247)&(aligned>8)&(aligned<247),axis=2)
                colors.append((aligned[use][::13].astype(float),a[use][::13].astype(float)))
    require(len(rows)>0 and all(r['accepted'] for r in rows),
            'One or more registration frames failed; no proposed config written. Adjust crop/exclusions/time mapping')
    warps = np.array([r['warp'] for r in rows])
    smooth = (.25*np.vstack([warps[0],warps[:-1]])+.5*warps+
              .25*np.vstack([warps[1:],warps[-1]]))
    p['source_to_generated_affine'] = [{'frame':r['frame'],'value':v.tolist()} for r,v in zip(rows,smooth)]
    color_report = {'applied':False,'reason':'insufficient samples or held-out improvement'}
    if len(colors)>=3 and all(len(x)>=64 for x,y in colors):
        train_x = np.concatenate([x for x,y in colors[:-1]])
        train_y = np.concatenate([y for x,y in colors[:-1]])
        gains, offsets = [], []
        for c in range(3):
            x,y = train_x[:,c],train_y[:,c]; keep = np.ones(len(x),bool)
            for _ in range(3):
                if np.std(x[keep]) <= 4:
                    break
                g,o = np.linalg.lstsq(np.column_stack([x[keep],np.ones(keep.sum())]),y[keep],rcond=None)[0]
                residual = np.abs(g*x+o-y); keep = residual<=np.quantile(residual,.8)
            else:
                gains.append(float(np.clip(g,.75,1.33))); offsets.append(float(np.clip(o,-24,24)))
                continue
            # Uninformative samples: keep spatial proposal, leave existing color unchanged.
            gains = []
            break
        if len(gains) == 3:
            x,y = colors[-1]; before = float(np.mean(abs(x-y)))
            after = float(np.mean(abs(np.clip(x*gains+offsets,0,255)-y)))
            color_report = {'applied':after<before*.95,'held_out_mae_before':before,
                            'held_out_mae_after':after,'gain_bgr':gains,'offset_bgr':offsets}
            if color_report['applied']:
                p['gain_bgr'],p['offset_bgr'] = gains,offsets
    cfg['fit_review'] = {'status':'proposal; inspect representative frames and playback before final export',
                         'motion':args.motion,
                         'temporal_alignment':'provided mapping, not automatically proven',
                         'registration':rows,'smoothing':'[0.25,0.5,0.25] on adjacent observed frames',
                         'color':color_report}
    with private_output(args.output) as f:
        f.write(compact_json(cfg))
    print('Proposal written:',args.output)
    # What changed, so the proposal need not be opened to find out.
    delta = smooth-np.array(initials)
    print(json.dumps({
        'patch': args.patch, 'motion': args.motion, 'frames_registered': len(rows),
        'correlation_min': min(r['correlation'] for r in rows),
        'max_shift_from_initial_px': float(np.hypot(delta[:, 2], delta[:, 5]).max()),
        'max_linear_change': float(np.abs(delta[:, [0, 1, 3, 4]]).max()),
        'translation_range_px': [float(np.ptp(smooth[:, 2])), float(np.ptp(smooth[:, 5]))],
        'color': color_report,
        'next': 'review representative frames, then qc_tools.py precheck / merge --frames --preview'},
        ensure_ascii=False))


if __name__ == '__main__':
    main()
