"""Second-round single-factor prototypes on the production P0 baseline.

Run this module for isolated quality/timing; top5_experiments.py --mode combined
accepts --selected next:NAME to measure an actual full CLI process.
"""
import argparse
import collections
import contextlib
import ctypes
import functools
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys
import time
import types

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'flow-video-patch/scripts'))
import flow_patch as fp


@contextlib.contextmanager
def bands(count, threshold=0):
    old = fp.in_bands
    def run(height, work):
        if height <= threshold:
            return work(0, height)
        if fp._POOL is None:
            old(2, lambda a,b:None)
        step = max(2, ((height+count-1)//count+1) & ~1)
        list(fp._POOL.map(lambda a:work(a,min(a+step,height)),range(0,height,step)))
    fp.in_bands = run
    try:
        yield
    finally:
        fp.in_bands = old


@contextlib.contextmanager
def candidate(name):
    with contextlib.ExitStack() as stack:
        if name == 'baseline':
            yield
            return
        if name == 'chroma_fused':
            library = ctypes.CDLL(os.environ['FLOW_EXPERIMENT_LIBRARY'])
            fn = library.fk_passthrough420
            fn.argtypes = [ctypes.c_void_p,ctypes.c_ssize_t]*6+[ctypes.c_size_t]*2
            fn.restype = None
            old = fp.Reader.passthrough420
            def passthrough(reader,index):
                if reader.color['rgb_input']:
                    return old(reader,index)
                reader.advance(index)
                buf,Y,U,V = fp.planes420(bytearray(reader.w*reader.h*3//2),reader.w,reader.h)
                y,u,v = reader.raw
                def work(a,b):
                    arrays=[y[a:b],u[a:b],v[a:b],Y[a:b],U[a//2:b//2],V[a//2:b//2]]
                    values=[]
                    for array in arrays:values.extend(fp.NativeKernels.arg(array,np.uint8))
                    fn(*values,reader.w,b-a)
                fp.in_bands(reader.h,work)
                return buf,Y,U,V
            fp.Reader.passthrough420 = passthrough
            stack.callback(setattr,fp.Reader,'passthrough420',old)
        elif name == 'blend_buffers':
            if hasattr(fp, 'PyramidWorkspace'):
                raise RuntimeError('Run this prototype on the fixed P0 worktree (71d7029); production already reuses pyramid buffers')
            original_blend = fp.blend
            down, up = cv2.pyrDown, cv2.pyrUp
            cache, index = {}, [0]
            def pyramid(kind, original, src, *args, **kw):
                idx = index[0]; index[0] += 1
                h,w = src.shape[:2]
                w,h = kw.get('dstsize', ((w+1)//2,(h+1)//2) if kind == 'down' else (w*2,h*2))
                key = (kind,idx,src.shape,src.dtype.str,tuple(sorted(kw.items())))
                if key not in cache:
                    # One bounded shape set; a different configuration replaces it.
                    if len(cache) >= 18:
                        cache.clear()
                    cache[key] = np.empty((h,w)+src.shape[2:],src.dtype)
                return original(src,*args,dst=cache[key],**kw)
            def blend(*args, **kw):
                index[0] = 0
                cv2.pyrDown = functools.partial(pyramid, 'down', down)
                cv2.pyrUp = functools.partial(pyramid, 'up', up)
                try:
                    return original_blend(*args, **kw)
                finally:
                    cv2.pyrDown, cv2.pyrUp = down, up
            fp.blend = blend
            stack.callback(setattr,fp,'blend',original_blend)
        else:
            options = {'blend_adaptive': (fp,'blend',4,160),
                       'blend_bands16': (fp,'blend',16,0),
                       'chroma_bands4': (fp.Reader,'passthrough420',4,0),
                       'chroma_bands16': (fp.Reader,'passthrough420',16,0),
                       'color_bands4': (fp,'yuv_to_bgr',4,0)}
            if name not in options:
                raise ValueError(name)
            obj,attr,count,threshold = options[name]
            specs = [(obj,attr)]
            if name == 'color_bands4':
                specs.append((fp,'bgr_to_yuv420'))
            for obj, attr in specs:
                old = getattr(obj,attr)
                def call(*a,_old=old,**kw):
                    with bands(count,threshold):
                        return _old(*a,**kw)
                setattr(obj,attr,call)
                stack.callback(setattr,obj,attr,old)
        yield


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--pairs',type=int,default=5)
    parser.add_argument('--only',nargs='+')
    args = parser.parse_args()
    args.out.mkdir(parents=True,exist_ok=False)
    os.environ.update(FLOW_KERNELS='native',FLOW_PROCESS_THREADS='4',FLOW_ENCODE_THREADS='4')
    cv2.setNumThreads(4)
    assert fp.kernels() is not None
    rng = np.random.default_rng(3106)
    o = rng.integers(0,256,(640,640,3),dtype=np.uint8)
    p = rng.integers(0,256,o.shape,dtype=np.uint8)
    m = fp.matte(dict(region=[100,100,400,400],feather=32),0,[640,640])
    raw = rng.integers(0,256,(3,2160,3840),dtype=np.uint8)
    color = dict(matrix='bt709',range='tv',rgb_input=False)
    fake = types.SimpleNamespace(raw=raw,w=3840,h=2160,color=color,advance=lambda i:None)
    names = args.only or ['blend_buffers','blend_adaptive','blend_bands16','chroma_bands4','chroma_bands16','color_bands4']
    report = dict(frames=120,pairs=args.pairs,scope='isolated native operators; quality separated; cold workspace included per timed run',results={})
    def workload(name,frames):
        for i in range(frames):
            if name.startswith('blend'):
                v = fp.blend(o,p,m,'multiband',3)
            elif name.startswith('chroma'):
                v = fp.Reader.passthrough420(fake,i)[0]
            else:
                v = fp.bgr_to_yuv420(fp.yuv_to_bgr(*raw[:,:640,:640],color),color)
        return v
    for name in names:
        a = workload(name,2)
        with candidate(name):
            b = workload(name,2)
        assert bytes(a) == bytes(b), name
        # Generalize exactness across levels, dynamic masks and fade values.
        if name.startswith('blend'):
            for levels in [1,2,3,4,5,6]:
                for fade in [0,.13,.5,1]:
                    mm = m*fade
                    ref = fp.blend(o,p,mm,'multiband',levels)
                    with candidate(name):
                        actual = fp.blend(o,p,mm,'multiband',levels)
                    assert np.array_equal(ref,actual),(name,levels,fade)
        workload(name,10)
        aa=[]
        for _ in range(2):
            t=time.perf_counter();workload(name,120);aa.append(time.perf_counter()-t)
        rows=[]
        for i in range(args.pairs):
            row=dict(order=['A','B'] if i%2==0 else ['B','A'])
            for mode in row['order']:
                with candidate(name if mode=='B' else 'baseline'):
                    t=time.perf_counter();workload(name,120);row[mode]=time.perf_counter()-t
            rows.append(row)
            print(name,i,json.dumps(row),flush=True)
        a,b=(statistics.median(r[k] for r in rows) for k in ['A','B'])
        report['results'][name]=dict(exact=True,aa=aa,raw=rows,A=a,B=b,
            A_mad=statistics.median(abs(r['A']-a) for r in rows),
            B_mad=statistics.median(abs(r['B']-b) for r in rows),saving_percent=100*(1-b/a))
        (args.out/'results.json').write_text(json.dumps(report,indent=2),encoding='utf-8')


if __name__ == '__main__':
    main()
