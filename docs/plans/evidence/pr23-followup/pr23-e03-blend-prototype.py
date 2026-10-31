import sys,time,json,statistics
from pathlib import Path
import numpy as np, cv2
sys.path.insert(0,str(Path('VideoPatchSkill-pr23-review/flow-video-patch/scripts').resolve()))
import flow_patch as fp
rng=np.random.default_rng(2303); h,w=540,960
orig=rng.integers(0,256,(h,w,3),dtype=np.uint8); patch=rng.integers(0,256,(h,w,3),dtype=np.uint8)
spec={'region':[200,100,500,300],'feather':30,'protect':[[350,200,70,45]]}; mask=fp.matte(spec,0,[w,h]); levels=3
orig_pyr=cv2.pyrDown; cache={}; calls=0
def cached_pyr(src,*a,**kw):
 global calls
 calls+=1
 if calls%3==0:
  key=(src.shape,src.tobytes())
  if key not in cache: cache[key]=orig_pyr(src,*a,**kw)
  return cache[key]
 return orig_pyr(src,*a,**kw)
def candidate(m):
 global calls
 calls=0; cv2.pyrDown=cached_pyr
 try:return fp.blend(orig,patch,m,'multiband',levels)
 finally:cv2.pyrDown=orig_pyr
for fade in [1.0,0.5,0.0,1.0]:
 m=mask*fade
 baseline=fp.blend(orig,patch,m,'multiband',levels); actual=candidate(m)
 assert np.array_equal(baseline,actual),fade
base=[]; test=[]
for i in range(7):
 vals={}
 for name in (('base','cache') if i%2==0 else ('cache','base')):
  start=time.perf_counter()
  for j in range(3):
   (fp.blend(orig,patch,mask,'multiband',levels) if name=='base' else candidate(mask))
  vals[name]=time.perf_counter()-start
 if i:base.append(vals['base']);test.append(vals['cache'])
r={'scope':'real fp.blend, synthetic 960x540, multiband levels=3, NumPy/auto; experimental monkeypatch uses complete mask bytes as correctness key, not production cache','equal_for_fades':[1,0.5,0,1],'base_seconds':base,'candidate_seconds':test,'baseline_median':statistics.median(base),'candidate_median':statistics.median(test),'cache_entry_count':len(cache),'cache_value_bytes':sum(x.nbytes for x in cache.values()),'limits':'no full video, dynamic masks, bounded memory, native A/B, failure/invalidation; tobytes keys cost and memory included in timing but not cache value bytes'}
Path('pr23-e03-blend-prototype.json').write_text(json.dumps(r,indent=2),encoding='utf8');print(json.dumps(r,indent=2))
