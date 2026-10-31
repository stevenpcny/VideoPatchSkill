import os,sys,json,time,statistics,subprocess,argparse,collections
from pathlib import Path
import cv2,numpy as np
p=argparse.ArgumentParser();p.add_argument('--kernel',required=True);a=p.parse_args()
os.environ['FLOW_KERNELS']=a.kernel;os.environ['FLOW_PROCESS_THREADS']='4';os.environ['FLOW_ENCODE_THREADS']='4';cv2.setNumThreads(4)
sys.path.insert(0,str(Path('VideoPatchSkill-pr23-review/flow-video-patch/scripts').resolve()))
import flow_patch as fp
rng=np.random.default_rng(2303);o=rng.integers(0,256,(640,640,3),dtype=np.uint8);g=rng.integers(0,256,o.shape,dtype=np.uint8);m=fp.matte({'region':[128,128,384,384],'feather':16},0,[640,640])
D,U=cv2.pyrDown,cv2.pyrUp
components=collections.Counter();counts=collections.Counter()
def down(x,*args,**kw):
 t=time.perf_counter();v=D(x,*args,**kw);name='mask_pyrDown' if x.ndim==2 else 'image_pyrDown';components[name]+=time.perf_counter()-t;counts[name]+=1;return v
def up(x,*args,**kw):
 t=time.perf_counter();v=U(x,*args,**kw);components['pyrUp']+=time.perf_counter()-t;counts['pyrUp']+=1;return v
def run(instrument=False):
 cv2.pyrDown=down if instrument else D;cv2.pyrUp=up if instrument else U
 t=time.perf_counter();cpu=time.process_time()
 try:
  for _ in range(30):v=fp.blend(o,g,m,'multiband',3)
 finally:cv2.pyrDown=D;cv2.pyrUp=U
 return {'wall':time.perf_counter()-t,'process_cpu':time.process_time()-cpu}
def stat(v):
 med=statistics.median(v);return {'median':med,'mad':statistics.median(abs(x-med) for x in v),'min':min(v),'max':max(v)}
run();aa=[{'A1':run(),'A2':run()} for _ in range(5)];run(True);run(False);ab=[]
for i in range(10):
 row={'order':['off','on'] if i%2==0 else ['on','off']}
 for mode in row['order']:row[mode]=run(mode=='on')
 ab.append(row)
# diagnostic profile separate from all benchmark pairs
components.clear();counts.clear();diag=run(True);component=dict(components);component['blend_other_wall_residual']=diag['wall']-sum(components.values())
r={'kernel':a.kernel,'frames':30,'size':[640,640],'aa_pairs':aa,'instrumentation_pairs':ab,'off_stats':stat([x['off']['wall'] for x in ab]),'on_stats':stat([x['on']['wall'] for x in ab]),'profile':{'wall_seconds':diag['wall'],'process_cpu_seconds':diag['process_cpu'],'components_seconds':component,'counts':dict(counts)},'timing_scope':'synchronous actual blend calls only, no encoder or child process; excludes task reports; CPU is parent process including threads','limits':'environment and power plan not changed; thermal/frequency/background load not controlled. profile residual combines allocations, bands, blend and finish; not independent phase timers.'}
Path(f'pr23-followup/t0-{a.kernel}.json').write_text(json.dumps(r,indent=2));print(json.dumps({'kernel':a.kernel,'off_stats':r['off_stats'],'on_stats':r['on_stats'],'profile':r['profile']},indent=2))
