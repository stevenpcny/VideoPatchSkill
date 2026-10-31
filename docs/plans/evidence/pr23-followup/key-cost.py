import os,sys,time,json,statistics
from pathlib import Path
import numpy as np,cv2
os.environ['FLOW_KERNELS']='numpy';os.environ['FLOW_PROCESS_THREADS']='4';cv2.setNumThreads(4)
sys.path.insert(0,str(Path('VideoPatchSkill-pr23-review/flow-video-patch/scripts').resolve()))
import flow_patch as fp
r=[]
for w,h in [(640,640),(960,540),(1920,1080)]:
 spec={'region':[w//5,h//5,w*3//5,h*3//5],'feather':16};m=fp.matte(spec,0,[w,h]);data=[]
 for _ in range(10):
  start=time.perf_counter()
  for j in range(30):key=(json.dumps(spec,sort_keys=True,separators=(',',':')),(w,h),1.,3,cv2.__version__,cv2.BORDER_DEFAULT);hash(key)
  sem=time.perf_counter()-start;start=time.perf_counter()
  for j in range(30):key=(m.shape,m.tobytes(),3);hash(key)
  data.append({'semantic_key_30_calls':sem,'bytes_key_30_calls':time.perf_counter()-start})
 r.append({'size':[w,h],'raw':data,'semantic_median':statistics.median(x['semantic_key_30_calls'] for x in data),'bytes_median':statistics.median(x['bytes_key_30_calls'] for x in data)})
Path('pr23-followup/key-cost.json').write_text(json.dumps(r,indent=2))
print('DONE key cost')
