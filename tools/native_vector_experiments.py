"""Runtime-gated AVX2 prototypes. Build/measure with existing std-only Rust.

Production source is not modified. Full CLI trials use a separate frozen worktree
and top5_experiments --selected vector:all (FLOW_VECTOR_DIR points to built libs).
"""
import argparse
import contextlib
import ctypes
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import statistics
import subprocess
import sys
import time

import cv2
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'flow-video-patch/scripts'))
import flow_patch as fp

GROUPS={'control':[], 'chroma':['fk_chroma_avg'],
        'color':['fk_yuv_to_bgr','fk_bgr_to_yuv420'],
        'blend':['fk_alpha','fk_level','fk_finish','fk_diff64','fk_apply64']}
GROUPS['all']=GROUPS['chroma']+GROUPS['color']+GROUPS['blend']
GROUPS['manual_chroma']=GROUPS['chroma']
GROUPS['selected']=GROUPS['chroma']+GROUPS['color']
ARCH='any(target_arch="x86",target_arch="x86_64")'
LOADED={}


def transform(source,names):
    pattern=re.compile(r'#\[no_mangle\]\s*pub unsafe extern "C" fn (fk_\w+)\((.*?)\) \{',re.S)
    replacements=[]
    for match in pattern.finditer(source):
        name,args=match.groups()
        if name not in names:continue
        cursor=match.end();depth=1
        while depth:
            if source[cursor]=='{':depth+=1
            elif source[cursor]=='}':depth-=1
            cursor+=1
        body=source[match.end()-1:cursor]
        values=','.join(part.split(':',1)[0].strip() for part in args.split(',') if part.strip())
        text=f'''unsafe fn {name}_baseline({args}) {body}
#[cfg({ARCH})]
#[target_feature(enable="avx2")]
unsafe fn {name}_avx2({args}) {body}
#[no_mangle]
pub unsafe extern "C" fn {name}({args}) {{
    #[cfg({ARCH})]
    if std::is_x86_feature_detected!("avx2") {{ return {name}_avx2({values}); }}
    {name}_baseline({values})
}}
'''
        replacements.append((match.start(),cursor,text))
    assert len(replacements)==len(names)
    for first,last,text in reversed(replacements):source=source[:first]+text+source[last:]
    return source+f'''
#[no_mangle]
pub extern "C" fn fk_vector_features() -> u32 {{
    #[cfg({ARCH})]
    if std::is_x86_feature_detected!("avx2") {{ return 1; }}
    0
}}
'''


def load(name):
    if name not in LOADED:
        path=Path(os.environ['FLOW_VECTOR_DIR'])/(name+'.dll')
        k=fp.NativeKernels(path);fp.native_self_check(k)
        k.lib.fk_vector_features.restype=ctypes.c_uint32
        LOADED[name]=k
    return LOADED[name]


@contextlib.contextmanager
def candidate(name):
    if name in ('pipe4','pipe16'):
        old=fp.decoder_process
        source=inspect.getsource(old)
        assert source.count('1024*1024')==1
        namespace=dict(fp.__dict__)
        source=source.replace('1024*1024',('4' if name=='pipe4' else '16')+'*1024*1024')
        exec(compile(source,'<pipe-capacity-prototype>','exec'),namespace)
        fp.decoder_process=namespace['decoder_process']
        try:yield
        finally:fp.decoder_process=old
        return
    k=load(name);old=fp._KERNELS
    fp._KERNELS=[k]
    try:yield
    finally:fp._KERNELS=old


def build(args):
    args.out.mkdir(parents=True,exist_ok=False)
    source=(ROOT/'flow-video-patch/native/src/lib.rs').read_text(encoding='utf-8')
    records=[]
    for name,names in GROUPS.items():
        text=transform(source,names)
        if name in ('manual_chroma','selected'):
            start=text.index('unsafe fn fk_chroma_avg_avx2(');cursor=text.index('{',start)+1;depth=1
            while depth:
                if text[cursor]=='{':depth+=1
                elif text[cursor]=='}':depth-=1
                cursor+=1
            text=text[:start]+(ROOT/'tools/chroma_avx2_experiment.rs').read_text(encoding='utf-8')+text[cursor:]
        src=args.out/(name+'.rs');src.write_text(text,encoding='utf-8',newline='\n')
        cmd=[str(args.rustc),str(src),'--crate-type','cdylib','--edition=2021',
             '-C','opt-level=3','-C','codegen-units=1','-C','lto=true','-C','panic=abort',
             '-C','linker='+str(args.linker),'-o',str(args.out/(name+'.dll'))]
        p=subprocess.run(cmd,capture_output=True,text=True)
        if p.returncode:raise RuntimeError(p.stderr)
        records.append(dict(name=name,kernels=names,source_sha256=hashlib.sha256(src.read_bytes()).hexdigest(),
                            library_sha256=hashlib.sha256((args.out/(name+'.dll')).read_bytes()).hexdigest()))
        print(name,'built',flush=True)
    (args.out/'build.json').write_text(json.dumps(records,indent=2),encoding='utf-8')


def measure(args):
    args.out.mkdir(parents=True,exist_ok=False)
    os.environ.update(FLOW_KERNELS='native',FLOW_PROCESS_THREADS='4',FLOW_ENCODE_THREADS='4')
    cv2.setNumThreads(4)
    original=fp.kernels()
    report=dict(scope='native operators only; warm cache; 120 frames per pair; quality outside timing',
                baseline_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
                cpu_avx2=load('all').lib.fk_vector_features(),quality={},timing={})
    for name in GROUPS:load(name)
    rng=np.random.default_rng(3110)
    o=rng.integers(0,256,(640,640,3),dtype=np.uint8);p=rng.integers(0,256,o.shape,dtype=np.uint8)
    mask=fp.matte(dict(region=[100,100,400,400],feather=32),0,[640,640])
    planes=rng.integers(0,256,(3,640,640),dtype=np.uint8)
    large=rng.integers(0,256,(2160,3840),dtype=np.uint8)
    avg=np.empty((1080,1920),np.uint8)
    color=dict(matrix='bt709',range='tv')
    def work(stage,frames):
        for _ in range(frames):
            if stage=='chroma':fp.kernels().chroma_avg(large,avg);value=avg
            elif stage=='color':value=fp.bgr_to_yuv420(fp.yuv_to_bgr(*planes,color),color)
            else:value=fp.blend(o,p,mask,'multiband',3)
        return bytes(value)
    # Include original cargo build versus a compiler-matched control build.
    choices=[x.split('/') for x in args.only] if args.only else [('chroma','control'),('chroma','chroma'),('color','color'),('blend','blend')]
    for stage,name in choices:
        ref=work(stage,2)
        with candidate(name):actual=work(stage,2)
        assert ref==actual,(stage,name)
        key=stage+'/'+name;report['quality'][key]=True
        work(stage,5);aa=[]
        for _ in range(2):
            t=time.perf_counter();work(stage,120);aa.append(time.perf_counter()-t)
        rows=[]
        for i in range(args.pairs):
            row=dict(order=['A','B'] if i%2==0 else ['B','A'])
            for mode in row['order']:
                with candidate(name) if mode=='B' else contextlib.nullcontext():
                    t=time.perf_counter();work(stage,120);row[mode]=time.perf_counter()-t
            rows.append(row);print(key,i,json.dumps(row),flush=True)
        a,b=(statistics.median(r[k] for r in rows) for k in ['A','B'])
        report['timing'][key]=dict(aa=aa,raw=rows,A=a,B=b,saving_percent=100*(1-b/a))
        (args.out/'results.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    assert fp.kernels() is original


def quality(args):
    args.out.mkdir(parents=True,exist_ok=False)
    os.environ['FLOW_KERNELS']='native'
    baseline=fp.kernels();vector=load('selected')
    u=np.tile(np.arange(256,dtype=np.uint8),(256,1));v=u.T.copy();y=np.empty_like(u)
    first=np.empty((256,256,3),np.uint8);second=np.empty_like(first)
    bgr=np.empty_like(first);bgr[:,:,0]=u;bgr[:,:,1]=v
    planes1=[np.empty((256,256),np.uint8),np.empty((128,128),np.uint8),np.empty((128,128),np.uint8)]
    planes2=[np.empty_like(x) for x in planes1]
    count=0
    for matrix in ['bt709','bt601']:
        for ran in ['pc','tv']:
            c=dict(matrix=matrix,range=ran)
            for value in range(256):
                y.fill(value);bgr[:,:,2]=value
                baseline.yuv_to_bgr(y,u,v,first,c);vector.yuv_to_bgr(y,u,v,second,c)
                assert np.array_equal(first,second),(matrix,ran,value,'yuv')
                baseline.bgr_to_yuv420(bgr,*planes1,c);vector.bgr_to_yuv420(bgr,*planes2,c)
                assert all(np.array_equal(a,b) for a,b in zip(planes1,planes2)),(matrix,ran,value,'bgr')
                count+=1
            print(matrix,ran,'all RGB/YUV values exact',flush=True)
    # Every integer sum, vector blocks and scalar tails; row strides and padding.
    rounds=0
    for width in [32,46,64]:
        for total in range(1021):
            values=[min(255,max(0,total-255*k)) for k in range(4)]
            src=np.empty((2,width),np.uint8)
            src[0,0::2]=values[0];src[0,1::2]=values[1]
            src[1,0::2]=values[2];src[1,1::2]=values[3]
            a=np.full((1,width//2+8),165,np.uint8);b=a.copy()
            baseline.chroma_avg(src,a[:,:width//2]);vector.chroma_avg(src,b[:,:width//2])
            assert np.array_equal(a,b),(width,total)
            rounds+=1
    rng=np.random.default_rng(3111)
    for width in [2,14,32,46,64,3840]:
        src=rng.integers(0,256,(34,width+16),dtype=np.uint8)[:,:width]
        for image in [src,src[::-1]]:
            a=np.full((17,width//2+8),165,np.uint8);b=a.copy()
            baseline.chroma_avg(image,a[:,:width//2]);vector.chroma_avg(image,b[:,:width//2])
            assert np.array_equal(a,b),(width,'stride')
    result=dict(exact=True,cpu_avx2=vector.lib.fk_vector_features(),
                exhaustive_color_cases=count,yuv_pixels=count*65536,rgb_pixels=count*65536,
                chroma_sum_cases=rounds,stride_tail_cases=12,
                scope='all 24-bit RGB/YUV values per matrix/range; BGR UV uses adjacent-grid groups, not all four-pixel combinations; every chroma integer sum plus tails/strides')
    (args.out/'quality.json').write_text(json.dumps(result,indent=2),encoding='utf-8');print(json.dumps(result),flush=True)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--mode',choices=['build','measure','quality'],required=True)
    p.add_argument('--out',type=Path,required=True)
    p.add_argument('--rustc',type=Path)
    p.add_argument('--linker',type=Path)
    p.add_argument('--pairs',type=int,default=5)
    p.add_argument('--only',nargs='+')
    a=p.parse_args()
    if a.mode=='build':
        if not a.rustc or not a.linker:p.error('--rustc and --linker required')
        build(a)
    elif a.mode=='quality':quality(a)
    else:measure(a)


if __name__=='__main__':main()
