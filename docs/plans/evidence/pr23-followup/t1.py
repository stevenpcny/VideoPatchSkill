import os,sys,json,time,statistics,hashlib,subprocess,platform,collections,argparse
from pathlib import Path
import numpy as np,cv2
parser=argparse.ArgumentParser();parser.add_argument('--kernel',choices=['numpy','native'],required=True);args=parser.parse_args()
os.environ['FLOW_KERNELS']=args.kernel;os.environ['FLOW_PROCESS_THREADS']='4';os.environ['FLOW_ENCODE_THREADS']='4';cv2.setNumThreads(4)
sys.path.insert(0,str(Path('VideoPatchSkill-pr23-review/flow-video-patch/scripts').resolve()))
import flow_patch as fp
DOWN=cv2.pyrDown
OUT=Path('pr23-followup')
def stats(v):
 med=statistics.median(v);return dict(median=med,mad=statistics.median(abs(x-med) for x in v),range=[min(v),max(v)])
class Cache:
 def __init__(self,budget):self.budget=budget;self.data=collections.OrderedDict();self.used=0;self.peak=0;self.evictions=0
 def get(self,key,m,levels):
  if key in self.data:self.data.move_to_end(key);return self.data[key]
  g=[];prev=m
  for _ in range(levels):prev=DOWN(prev);prev.flags.writeable=False;g.append(prev)
  n=sum(x.nbytes for x in g)
  while self.data and self.used+n>self.budget:
   _,old=self.data.popitem(last=False);self.used-=sum(x.nbytes for x in old);self.evictions+=1
  if n<=self.budget:self.data[key]=g;self.used+=n;self.peak=max(self.peak,self.used)
  return g

def make(size,protect=False):
 w,h=size;spec={'region':[w//5,h//5,w*3//5,h*3//5],'feather':16}
 if protect:spec['protect']=[[w//2,h//2,20,20]]
 return spec

def semantic(spec,size,fade,levels):return (json.dumps(spec,sort_keys=True,separators=(',',':')),tuple(size),float(fade),levels,cv2.__version__,cv2.BORDER_DEFAULT)
def blend(mode,o,p,m,key,levels,cache,pre=None,counter=None):
 if mode=='A':return fp.blend(o,p,m,'multiband',levels)
 if mode=='B':g=cache.get((m.shape,m.tobytes(),levels),m,levels)
 elif mode=='C':g=cache.get(key,m,levels)
 else:g=pre
 layer=0
 def down(v,*a,**kw):
  nonlocal layer
  if v.ndim==2:
   r=g[layer];layer+=1
   if counter is not None:counter['gm_hits']+=1
   return r
  if counter is not None:counter['image_pyrDown']+=1
  return DOWN(v,*a,**kw)
 cv2.pyrDown=down
 try:return fp.blend(o,p,m,'multiband',levels)
 finally:cv2.pyrDown=DOWN

rng=np.random.default_rng(2303)
report={'kernel':args.kernel,'seed':2303,'environment':{'python':platform.python_version(),'numpy':np.__version__,'opencv':cv2.__version__,'opencv_threads':cv2.getNumThreads(),'FLOW_PROCESS_THREADS':os.environ['FLOW_PROCESS_THREADS'],'FLOW_ENCODE_THREADS':os.environ['FLOW_ENCODE_THREADS'],'ffmpeg':subprocess.check_output(['ffmpeg','-version'],text=True).splitlines(),'power':subprocess.check_output(['powercfg','/getactivescheme'],text=True).strip(),'background_load_and_temperature':'not controlled; no concurrent task benchmarks; temperatures not measured'},'quality':[],'timing':[],'counts':[]}
# Quality includes internal pressure cases, explicitly separated from production legality.
for size in [(640,640),(960,540),(1920,1080)]:
 w,h=size;o=rng.integers(0,256,(h,w,3),dtype=np.uint8);p=rng.integers(0,256,(h,w,3),dtype=np.uint8)
 for protected in [False,True]:
  spec=make(size,protected);cache=Cache(4*1024*1024)
  for levels in [2,3]:
   for fade in [1,.5,0,1,.13,.29,.87]:
    m=fp.matte(spec,0,size)*fade;key=semantic(spec,size,fade,levels);ref=fp.blend(o,p,m,'multiband',levels)
    for mode in ['B','C','D']:
     ideal=Cache(0).get(None,m,levels);actual=blend(mode,o,p,m,key,levels,cache,ideal);assert np.array_equal(ref,actual),(size,protected,levels,fade,mode)
    assert cache.used<=cache.budget
  # alpha+protect is legal and kept separate; no pyramid cache involved.
  if protected:assert np.array_equal(fp.blend(o,p,m,'alpha',3)[m==0],o[m==0])
  report['quality'].append(dict(size=size,protect=protected,classification='internal pressure only: merge_plan rejects multiband+protect' if protected else 'legal multiband mask',exact=True,cache_peak_array_bytes=cache.peak,evictions=cache.evictions))
 # same address mutation: bytes key must see changed contents; semantic C owns masks generated from immutable specs, not arbitrary caller arrays.
 spec=make(size);m=fp.matte(spec,0,size);c=Cache(4*1024*1024);k=semantic(spec,size,1,3);x=blend('B',o,p,m,k,3,c);m[:]=0;y=blend('B',o,p,m,k,3,c);assert np.array_equal(y,o)
 # timing: legal no-protect static only; all candidates same originals, each includes lookup and cold build except D.
 m=fp.matte(spec,0,size);ideal=Cache(0).get(None,m,3)
 for frames in [3,30]:
  cold={};caches={mode:Cache(4*1024*1024) for mode in 'ABCD'}
  for mode in 'ABCD':
   t=time.perf_counter()
   for _ in range(frames):blend(mode,o,p,m,k,3,caches[mode],ideal)
   cold[mode]=time.perf_counter()-t
  runs=[]
  for i in range(10):
   row={'order':list('ABCD' if i%2==0 else 'DCBA')}
   for mode in row['order']:
    t=time.perf_counter()
    for _ in range(frames):blend(mode,o,p,m,k,3,caches[mode],ideal)
    row[mode]=time.perf_counter()-t
   runs.append(row)
  report['timing'].append({'size':size,'frames_per_run':frames,'cold_seconds':cold,'raw':runs,'stats':{mode:stats([x[mode] for x in runs]) for mode in 'ABCD'},'D_scope':'prebuilt gm excludes cache build and lookup; not deliverable speed'})
  print(args.kernel,size,frames,'complete',flush=True)
 # Diagnostic counts separated from timing; baseline counter external.
 count=collections.Counter()
 def counted(v,*a,**kw):count['gm_pyrDown' if v.ndim==2 else 'image_pyrDown']+=1;return DOWN(v,*a,**kw)
 cv2.pyrDown=counted
 try:fp.blend(o,p,m,'multiband',3)
 finally:cv2.pyrDown=DOWN
 hit=collections.Counter();c=Cache(4*1024*1024);c.get(k,m,3);blend('C',o,p,m,k,3,c,counter=hit)
 assert count['gm_pyrDown']==3 and count['image_pyrDown']==6 and hit['gm_hits']==3 and hit['image_pyrDown']==6
 report['counts'].append(dict(size=size,baseline=dict(count),cache_hit=dict(hit)))
 (OUT/f't1-{args.kernel}.json').write_text(json.dumps(report,indent=2),encoding='utf8')
print('DONE',args.kernel,flush=True)
