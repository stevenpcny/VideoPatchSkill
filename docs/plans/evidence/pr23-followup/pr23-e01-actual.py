import sys,json,time,subprocess,tempfile,hashlib,statistics,os
from pathlib import Path
sys.path.insert(0,str(Path('VideoPatchSkill-pr23-review/flow-video-patch/scripts').resolve()))
import flow_patch as fp
root=Path('pr23-actual-experiments');root.mkdir(exist_ok=True)
def ff(args):subprocess.run(['ffmpeg','-v','error','-n']+args,check=True,capture_output=True,timeout=600)
source=root/'4k240.mp4'
if not source.exists():ff(['-f','lavfi','-i','testsrc2=size=3840x2160:rate=24','-f','lavfi','-i','sine=frequency=440:sample_rate=48000','-t','10','-c:v','libx264','-threads','4','-preset','ultrafast','-crf','20','-g','48','-bf','3','-pix_fmt','yuv420p','-c:a','aac',str(source)])
generated=root/'patch240.mp4'
if not generated.exists():ff(['-i',str(source),'-vf','crop=640:640:400:400','-an','-c:v','libx264','-threads','4','-preset','ultrafast',str(generated)])
cfg=dict(schema_version=1,source=str(source.resolve()),source_size=[3840,2160],fps=24,patches=[dict(generated=str(generated.resolve()),target=[0,240],crop=[400,400,640,640],work_size=[640,640],matte=dict(region=[100,100,400,400],feather=32),blend='multiband',levels=3,color_gain=[1.01,0.99,1.0],color_offset=[1,0,-1])])
(root/'config.json').write_text(json.dumps(cfg,indent=2))
encoder=fp.encoder

def enc(*args,**kwargs):
 cmd=encoder(*args,**kwargs);return cmd[:-1]+['-vf','scale=1920:1080:flags=lanczos']+cmd[-1:]
def trial(mode,number):
 fp._PROBED.clear();fp._PASSES.clear();start=time.perf_counter();info=fp.probe(cfg['source']);plan=fp.merge_plan(cfg,info)
 out=root/f'{mode}-{number}.mp4';fp.encoder=enc if mode=='direct' else encoder
 try:fp.process(cfg,info,'merge',out,plan,preset='ultrafast')
 finally:fp.encoder=encoder
 if mode=='baseline':
  final=root/f'review-{number}.mp4';ff(['-i',str(out),'-vf','scale=1920:1080:flags=lanczos','-c:v','libx264','-threads','4','-preset','ultrafast','-crf','17','-c:a','copy',str(final)])
 else:final=out
 decoded=fp.decode_pass(str(final),strict=True); wall=time.perf_counter()-start
 meta=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-of','json',str(final)]))
 v=next(s for s in meta['streams'] if s['codec_type']=='video');assert (v['width'],v['height'])==(1920,1080);assert len(decoded[0])==240
 return dict(seconds=wall,sha256=hashlib.sha256(final.read_bytes()).hexdigest(),frames=len(decoded[0]),bytes=final.stat().st_size,audio=any(s['codec_type']=='audio' for s in meta['streams']))
report={'scope':'actual merge_plan/process, synthetic 4K 240 frames, one 640px multiband patch; post-composite FFmpeg downscale prototype; strict output decode, not product approval workflow','runs':[]}
for i in range(6):
 r={}
 for mode in (['baseline','direct'] if i%2==0 else ['direct','baseline']):
  r[mode]=trial(mode,i);print(i,mode,r[mode]['seconds'],flush=True)
 report['runs'].append(r);(root/'e01-full-raw.json').write_text(json.dumps(report,indent=2))
print('DONE',flush=True)
