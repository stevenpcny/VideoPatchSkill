#!/usr/bin/env python3
"""One-command behavioral verification with disposable, synthetic media only."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import flow_patch as fp


def cli(*args, ok=True):
    p = subprocess.run([sys.executable,str(ROOT/'scripts/flow_patch.py'),*map(str,args)],
                       stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8')
    if ok and p.returncode:
        raise AssertionError(p.stdout+p.stderr)
    if not ok and not p.returncode:
        raise AssertionError('Unexpected success')
    return p


def encode(path, images, audio=None):
    h,w = images[0].shape[:2]
    cmd = [fp.FFMPEG,'-v','error','-n','-f','rawvideo','-pix_fmt','bgr24','-s',f'{w}x{h}',
           '-r','24','-i','-']
    if audio:
        cmd += ['-i',str(audio),'-map','0:v:0','-map','1:a:0','-c:a','copy']
    cmd += ['-c:v','libx264','-crf','10','-pix_fmt','yuv420p',str(path)]
    subprocess.run(cmd,input=b''.join(im.tobytes() for im in images),check=True,
                   stdout=subprocess.PIPE,stderr=subprocess.PIPE)


def image(path,index):
    reader = fp.Reader(path,fp.probe(path)['size'])
    try:
        return reader.get(index).copy()
    finally:
        reader.close()


class Smoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='flow-patch-smoke-')
        cls.base = Path(cls.tmp.name)
        audio = cls.base/'tone.m4a'
        fp.run([fp.FFMPEG,'-v','error','-n','-f','lavfi','-i','sine=frequency=440:sample_rate=48000',
                '-t','1','-c:a','aac',audio])
        # Stable textured background and a deliberately distinct mouth rectangle.
        rng = np.random.default_rng(42)
        texture = rng.integers(30,180,(128,192,3),dtype=np.uint8)
        frames=[]
        for i in range(24):
            im=texture.copy(); im[8:24,8+i:24+i]=[30,200,50]
            im[75:91,25:71]=[180,40,40];frames.append(im)
        cls.source=cls.base/'source 中文.mp4';encode(cls.source,frames,audio)
        cls.generated=cls.base/'flow.mp4'
        generated=[]
        for i in range(23):
            im=frames[i][:,:96].copy(); im[25:65,25:71]=[40,70,220]
            # Generation also changes the mouth; upper-face route must not use it.
            im[75:91,25:71]=[20,220,220]; generated.append(im)
        encode(cls.generated,generated)
        cls.cfg=dict(schema_version=1,source=str(cls.source),source_size=[192,128],fps=24)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def config(self,name,**updates):
        cfg=json.loads(json.dumps(self.cfg));cfg.update(updates)
        path=self.base/(name+'.json');path.write_text(json.dumps(cfg),encoding='utf-8')
        return path

    def patch(self,**updates):
        p=dict(generated=str(self.generated),target=[0,24],frame_map=list(range(23))+[22],
               crop=[0,0,96,128],work_size=[96,128],matte=dict(region=[16,16,64,96],
               feather=8,protect=[[16,72,64,24]]),blend='alpha',fade_frames=0)
        p.update(updates);return p

    def test_01_doctor_and_inspect(self):
        cli('doctor')
        self.assertEqual(json.loads(cli('inspect','--source',self.source).stdout)['frames'],24)

    def test_02_prepare_context_and_tracked_crop(self):
        cfg=self.config('prepare',prepare=dict(core=[4,20],context_frames=2,
                        crop=[dict(frame=0,value=[0,0,96,128]),dict(frame=23,value=[24,0,96,128])],
                        output_size=[96,128],mask_regions=[[24,24,32,40]],color_rgb=[0,0,0]))
        out=self.base/'upload.mp4'
        cli('prepare','--config',cfg,'--output',out,'--dry-run')
        self.assertFalse(out.exists())
        cli('prepare','--config',cfg,'--output',out)
        rpt=json.loads(out.with_suffix('.report.json').read_text(encoding='utf-8'))
        self.assertEqual(rpt['output']['frames'],20)
        self.assertEqual(rpt['output']['core_in_upload'],[2,18])
        self.assertLess(image(out,5)[30:55,30:50].mean(),8)
        self.assertGreater(image(out,0)[30:55,30:50].mean(),30)
        self.assertTrue(rpt['decode_pass'])

    def test_03_protected_mouth_audio_and_tail_hold(self):
        cfg=self.config('upper',patches=[self.patch()]);out=self.base/'upper.mp4'
        cli('merge','--config',cfg,'--output',out)
        before,after=image(self.source,12),image(out,12)
        self.assertGreater(np.mean(abs(after[30:60,30:65].astype(float)-before[30:60,30:65])),20)
        self.assertLess(np.mean(abs(after[78:88,30:65].astype(float)-before[78:88,30:65])),5)
        self.assertEqual(fp.audio_hash(self.source),fp.audio_hash(out))
        self.assertEqual(fp.probe(out)['frames'],24)
        rejected=cli('merge','--config',cfg,'--output',out,ok=False)
        self.assertIn('exists',rejected.stderr)

    def test_04_multiband_two_people(self):
        p=self.patch(matte=dict(region=[8,8,80,112],feather=8),blend='multiband',levels=3)
        q=json.loads(json.dumps(p));q['crop']=[96,0,96,128]
        cfg=self.config('both',patches=[p,q]);out=self.base/'both.mp4'
        cli('merge','--config',cfg,'--output',out)
        before,after=image(self.source,10),image(out,10)
        for x in (0,96):
            self.assertGreater(np.mean(abs(after[30:60,x+30:x+65].astype(float)-
                                           before[30:60,x+30:x+65])),15)
        self.assertEqual(fp.audio_hash(self.source),fp.audio_hash(out))

    def test_05_missing_frame_original_fallback(self):
        p=self.patch();p['frame_map'][10]=None
        cfg=self.config('fallback',patches=[p]);out=self.base/'fallback.mp4'
        cli('merge','--config',cfg,'--output',out)
        before,after=image(self.source,10),image(out,10)
        self.assertLess(np.mean(abs(after.astype(float)-before)),5)

    def test_06_reject_unmapped_short_and_overlap(self):
        p=self.patch();p.pop('frame_map')
        cfg=self.config('short',patches=[p]);out=self.base/'bad.mp4'
        self.assertIn('Mapped generated frame missing',cli('merge','--config',cfg,'--output',out,ok=False).stderr)
        p=self.patch();cfg=self.config('overlap',patches=[p,p])
        self.assertIn('overlap',cli('merge','--config',cfg,'--output',out,ok=False).stderr)
        self.assertFalse(out.exists())

    def test_07_fit_known_translation_and_fixed_color(self):
        # Known geometry and fixed color offset, not a real Flow render.
        original=[image(self.source,i)[:,:96].copy() for i in range(24)]
        gen=[cv2.warpAffine(np.clip(im.astype(float)*.9+7,0,255).astype(np.uint8),
                           np.array([[1,0,2],[0,1,1]],np.float32),(96,128)) for im in original]
        gp=self.base/'fit-flow.mp4';encode(gp,gen)
        p=self.patch(generated=str(gp),frame_map=list(range(24)),
                     matte=dict(region=[24,24,48,48],feather=6),blend='alpha')
        cfg=self.config('fit',patches=[p]);out=self.base/'fit-proposed.json'
        result=subprocess.run([sys.executable,str(ROOT/'scripts/fit_patch.py'),'--config',str(cfg),
                               '--output',str(out)],capture_output=True,text=True,encoding='utf-8')
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        proposed=json.loads(out.read_text(encoding='utf-8'))
        self.assertTrue(all(r['accepted'] for r in proposed['fit_review']['registration']))
        for row in proposed['patches'][0]['source_to_generated_affine']:
            self.assertLess(abs(row['value'][2]-2),.6)
            self.assertLess(abs(row['value'][5]-1),.6)
        self.assertTrue(proposed['fit_review']['color']['applied'])
        proposed_path=self.base/'fit-merged.mp4'
        cli('merge','--config',out,'--output',proposed_path)

    def test_08_prepare_no_audio_at_source_boundary(self):
        cfg=self.config('no-audio',source=str(self.generated),source_size=[96,128],
                        prepare=dict(core=[0,10],context_frames=24,crop=[0,0,96,128],
                                     output_size=[96,128],mask_regions=[[24,24,32,40]]))
        out=self.base/'no-audio-upload.mp4'
        cli('prepare','--config',cfg,'--output',out)
        rpt=json.loads(out.with_suffix('.report.json').read_text(encoding='utf-8'))
        self.assertEqual(rpt['output']['frames'],23)
        self.assertEqual(rpt['output']['core_in_upload'],[0,10])
        self.assertFalse(rpt['output']['audio'])

    def test_09_cached_matte_and_banded_blend_are_exact(self):
        spec=dict(region=[8,6,80,60],shape='ellipse',feather=10,protect=[[30,40,20,10]])
        first=fp.matte(spec,0,[96,72]); first*=0
        self.assertGreater(fp.matte(spec,5,[96,72]).max(),0.99,'cached matte was mutated')
        np.testing.assert_array_equal(fp.matte(spec,5,[96,72]),fp._matte(spec,5,[96,72]))
        rng=np.random.default_rng(3)
        a,b=(rng.integers(0,256,(72,96,3),dtype=np.uint8) for _ in range(2))
        m=fp._matte(dict(region=[8,6,80,60],shape='ellipse',feather=10),0,[96,72])
        # Unbanded reference formulas from v2.4.
        ref=a.astype(np.float32)*(1-m[:,:,None])+b.astype(np.float32)*m[:,:,None]
        ga,gb,gm=[a.astype(np.float32)],[b.astype(np.float32)],[m]
        for _ in range(2):
            ga.append(cv2.pyrDown(ga[-1])); gb.append(cv2.pyrDown(gb[-1])); gm.append(cv2.pyrDown(gm[-1]))
        mb=ga[-1]*(1-gm[-1][:,:,None])+gb[-1]*gm[-1][:,:,None]
        for k in (1,0):
            size=(ga[k].shape[1],ga[k].shape[0])
            la=ga[k]-cv2.pyrUp(ga[k+1],dstsize=size); lb=gb[k]-cv2.pyrUp(gb[k+1],dstsize=size)
            mb=cv2.pyrUp(mb,dstsize=size)+la*(1-gm[k][:,:,None])+lb*gm[k][:,:,None]
        for method,levels,expect in (('alpha',5,ref),('multiband',2,mb)):
            expect=np.clip(expect,0,255).round().astype(np.uint8); expect[m==0]=a[m==0]
            np.testing.assert_array_equal(fp.blend(a,b,m,method,levels),expect)

    def test_10_segmented_merge_matches_single(self):
        # Second segment starts mid-return (generated frame 12) and ends on the held tail frame.
        cfg=self.config('segmented',patches=[self.patch()])
        single,split=self.base/'single.mp4',self.base/'split.mp4'
        cli('merge','--config',cfg,'--output',single)
        p=subprocess.run([sys.executable,str(ROOT/'scripts/flow_patch.py'),'merge','--config',str(cfg),
                          '--output',str(split)],env=dict(os.environ,FLOW_SEGMENTS='2'),
                         stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8')
        self.assertEqual(p.returncode,0,p.stderr)
        rpt=json.loads(split.with_suffix('.report.json').read_text(encoding='utf-8'))
        self.assertEqual([s['frames'] for s in rpt['command_argv']['segments']],[[0,12],[12,24]])
        self.assertEqual(fp.probe(split)['frames'],24)
        self.assertEqual(fp.audio_hash(self.source),fp.audio_hash(split))
        # Separate encodes differ by coding noise only: each segmented frame is still nearest to
        # the same frame of the single encode (the moving square makes neighbours distinct).
        def rows(path):
            reader=fp.Reader(path,[192,128])
            try:
                return [reader.get(k)[8:24].astype(float) for k in range(24)]
            finally:
                reader.close()
        ref=rows(single)
        for i,got in enumerate(rows(split)):
            self.assertEqual(int(np.argmin([np.abs(got-r).mean() for r in ref])),i,f'frame {i}')

    def test_11_partial_preview_export_is_marked_and_aligned(self):
        cfg=self.config('partial',patches=[self.patch()])
        full,part=self.base/'partial-full.mp4',self.base/'partial.mp4'
        cli('merge','--config',cfg,'--output',full)
        out=cli('merge','--config',cfg,'--output',part,'--frames','6:18','--preview').stdout
        self.assertIn('not deliverable',out)
        rpt=json.loads(part.with_suffix('.report.json').read_text(encoding='utf-8'))
        self.assertEqual(rpt['scope'],{'frames':[6,18],'preview_encode':True,'deliverable':False,
                                     'per_frame_checks':'exported frames only; a full export checks every patch frame'})
        self.assertTrue(rpt['visual_review'].startswith('not deliverable'))
        self.assertIn('ultrafast',rpt['command_argv'])
        info=fp.probe(part)
        self.assertEqual((info['frames'],info['audio']),(12,True))
        # Each exported frame is nearest to the same source-numbered frame of the full export.
        a,b=fp.Reader(full,[192,128]),fp.Reader(part,[192,128])
        try:
            ref=[a.get(k)[8:24].astype(float) for k in range(24)]
            for k in range(12):
                got=b.get(k)[8:24].astype(float)
                self.assertEqual(int(np.argmin([np.abs(got-r).mean() for r in ref])),6+k)
        finally:
            a.close(); b.close()
        # Unchanged inputs were cached for check runs; a deliverable run never writes the cache.
        self.assertEqual(len(list((self.base/'.flow-probe-cache').glob('*.json'))),2)
        rejected=cli('prepare','--config',cfg,'--output',self.base/'x.mp4','--preview',ok=False)
        self.assertIn('merge only',rejected.stderr)
        for bad in ('18:6','0:25','a:b'):
            cli('merge','--config',cfg,'--output',self.base/'bad.mp4','--frames',bad,ok=False)

    def test_12_seeking_reader_matches_sequential_decode(self):
        info=fp.probe(self.source)
        self.assertTrue(info['on_frame_grid'])
        seq=fp.Reader(self.source,info['size'])
        try:
            frames=[seq.get(i).copy() for i in range(24)]
        finally:
            seq.close()
        for start in (1,11,12,23):
            for fps in (None,info['fps']):  # frame-number select and timestamp seek
                r=fp.Reader(self.source,info['size'],start=start,fps=fps)
                try:
                    np.testing.assert_array_equal(r.get(start),frames[start])
                finally:
                    r.close()

    def test_13_probe_cache_tracks_file_changes(self):
        cache=self.base/'probe-cache'
        clip=self.base/'cache-clip.mp4'; clip.write_bytes(self.generated.read_bytes())
        path=fp.local_media(clip)
        first=fp.cached_probe(path,cache)
        entry,=cache.glob('*.json')
        entry.write_text('{"frames": 999}',encoding='utf-8')
        self.assertEqual(fp.cached_probe(path,cache)['frames'],999)  # same stat: cache is used
        os.utime(clip,ns=(1,1))  # a changed file gets a fresh, full probe
        self.assertEqual(fp.cached_probe(path,cache),first)

    def test_14_fit_affine_scale(self):
        original=[image(self.source,i)[:,:96].copy() for i in range(24)]
        known=np.array([[1.04,0,-1.0],[0,1.04,-2.0]],np.float32)
        gen=[cv2.warpAffine(im,known,(96,128)) for im in original]
        gp=self.base/'fit-scale.mp4';encode(gp,gen)
        p=self.patch(generated=str(gp),frame_map=list(range(24)),
                     matte=dict(region=[30,30,36,48],feather=6),blend='alpha')
        cfg=self.config('fit-scale',patches=[p]);out=self.base/'fit-scale-proposed.json'
        run=lambda *extra:subprocess.run([sys.executable,str(ROOT/'scripts/fit_patch.py'),'--config',str(cfg),
                                          '--output',str(out),*extra],capture_output=True,text=True,encoding='utf-8')
        result=run('--motion','affine')
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        proposed=json.loads(out.read_text(encoding='utf-8'))
        self.assertEqual(proposed['fit_review']['motion'],'affine')
        for row in proposed['patches'][0]['source_to_generated_affine']:
            np.testing.assert_allclose(row['value'],known.flatten(),atol=.6)
            self.assertLess(abs(row['value'][0]-1.04),.01); self.assertLess(abs(row['value'][4]-1.04),.01)
        out.unlink()
        # The scale exceeds a tighter bound, so no proposal is written.
        self.assertNotEqual(run('--motion','affine','--max-linear-change','0.01').returncode,0)
        self.assertFalse(out.exists())

    def test_15_one_sided_fade(self):
        cfg=self.config('fade-in',patches=[self.patch(fade_in_frames=6,protect=None)]);out=self.base/'fade-in.mp4'
        cli('merge','--config',cfg,'--output',out)
        src,res=fp.Reader(self.source,[192,128]),fp.Reader(out,[192,128])
        try:
            diff=lambda i:np.mean(abs(res.get(i)[30:60,30:65].astype(float)-src.get(i)[30:60,30:65]))
            self.assertLess(diff(0),6)       # weight 0 on the first frame: coding noise only
            self.assertGreater(diff(12),20)  # full weight after the fade-in
            self.assertGreater(diff(23),20)  # no fade-out at the end
        finally:
            src.close(); res.close()
        bad=self.config('fade-bad',patches=[self.patch(fade_in_frames=20,fade_out_frames=10)])
        self.assertIn('fade',cli('merge','--config',bad,'--output',self.base/'fade-bad.mp4',ok=False).stderr)

    def test_16_notices_and_millisecond_mkv(self):
        src=self.base/'422-two-tracks.mkv'
        fp.run([fp.FFMPEG,'-v','error','-n','-f','lavfi','-i','testsrc2=s=96x64:r=30:d=1',
                '-f','lavfi','-i','sine=d=1','-f','lavfi','-i','sine=f=880:d=1',
                '-map','0','-map','1','-map','2','-c:v','libx264','-pix_fmt','yuv422p','-c:a','flac',src])
        info=fp.probe(src)  # Matroska stores 1 ms ticks: steps alternate 33/34 ms at 30 fps.
        self.assertEqual((info['fps'],info['frames'],info['chroma'],info['streams']['audio']),('30',30,'4:2:2',2))
        self.assertTrue(info['on_frame_grid'])
        cfg=self.config('notices',source=str(src),source_size=[96,64],fps=30,
                        prepare=dict(core=[5,20],crop=[0,0,96,64],output_size=[96,64],mask_regions=[[8,8,16,16]]))
        p=cli('prepare','--config',cfg,'--output',self.base/'n.mp4','--dry-run')
        notices=json.loads(p.stdout)['notices']
        self.assertTrue(any('4:2:2' in n for n in notices) and any('2 audio tracks' in n for n in notices))
        self.assertIn('NOTICE:',p.stderr)

    @unittest.skipUnless(fp.native_library().is_file(), 'optional native kernels not built (references/native-kernels.md)')
    def test_17_native_kernels_match_numpy(self):
        # Same frames in, same libx264 settings: the encoded files must be byte-identical.
        p=self.patch(matte=dict(region=[4,4,40,56],feather=4),blend='multiband',levels=2,work_size=[48,64])
        q=self.patch(crop=[96,0,96,128])
        cfg=self.config('kernels',patches=[p,q])
        outs={}
        for mode in ('numpy','native'):
            out=self.base/f'kernels-{mode}.mp4'
            r=subprocess.run([sys.executable,str(ROOT/'scripts/flow_patch.py'),'merge','--config',str(cfg),
                              '--output',str(out)],env=dict(os.environ,FLOW_KERNELS=mode),
                             capture_output=True,text=True,encoding='utf-8')
            self.assertEqual(r.returncode,0,r.stderr)
            outs[mode]=json.loads(out.with_suffix('.report.json').read_text(encoding='utf-8'))['output_sha256']
        self.assertEqual(outs['numpy'],outs['native'])

    def test_18_copy_unpatched_gops(self):
        # 48 frames, keyframe every 12, B-frames (reorder delay 2), with audio.
        src=self.base/'gops.mp4'
        fp.run([fp.FFMPEG,'-v','error','-n','-f','lavfi','-i','testsrc2=s=192x128:r=24:d=2','-f','lavfi',
                '-i','sine=d=2','-c:v','libx264','-x264-params','keyint=12:min-keyint=12:scenecut=0:bframes=3',
                '-pix_fmt','yuv420p','-c:a','aac','-shortest',src])
        hashes=lambda path:[l.rsplit(',',1)[1].strip() for l in fp.run([fp.FFMPEG,'-v','error','-i',str(path),
                            '-map','0:v:0','-f','framehash','-hash','murmur3','-']).decode().splitlines()
                            if l and not l.startswith('#')]
        source=hashes(src)
        for name,target,render in (('middle',[14,20],(12,24)),('start',[0,5],(0,12))):
            p=self.patch(generated=str(self.generated),target=target,frame_map=list(range(target[1]-target[0])),
                         crop=[0,0,96,128],protect=None)
            cfg=self.config('gops-'+name,source=str(src),source_size=[192,128],patches=[p])
            out=self.base/f'gops-{name}.mp4'
            cli('merge','--config',cfg,'--output',out,'--copy-unpatched')
            rpt=json.loads(out.with_suffix('.report.json').read_text(encoding='utf-8'))
            c=rpt['copy_unpatched']
            self.assertEqual((c['rendered_frames'],c['copied_frames']),(12,36),name)
            self.assertTrue(c['copied_frames_identical_to_source'])
            got=hashes(out)
            self.assertEqual(len(got),48)
            # Independently: frames outside the re-encoded GOP equal the source bit for bit.
            self.assertTrue(all(got[i]==source[i] for i in range(48) if not render[0]<=i<render[1]),name)
            self.assertTrue(any(got[i]!=source[i] for i in range(*target)),name)
            self.assertEqual(fp.audio_hash(src),fp.audio_hash(out))
            info=fp.probe(out)
            self.assertEqual((info['frames'],info['fps']),(48,'24'))

    def test_19_copy_unpatched_falls_back_when_ineligible(self):
        src=self.base/'444.mp4'
        fp.run([fp.FFMPEG,'-v','error','-n','-i',str(self.source),'-c:v','libx264','-pix_fmt','yuv444p','-c:a','copy',src])
        cfg=self.config('copy-fallback',source=str(src),patches=[self.patch()])
        out=self.base/'copy-fallback.mp4'
        p=cli('merge','--config',cfg,'--output',out,'--copy-unpatched')
        self.assertIn('--copy-unpatched not used',p.stderr)
        rpt=json.loads(out.with_suffix('.report.json').read_text(encoding='utf-8'))
        self.assertEqual(rpt['copy_unpatched'],{'used':False})
        self.assertEqual(fp.probe(out)['frames'],24)
        rejected=cli('merge','--config',cfg,'--output',self.base/'x2.mp4','--copy-unpatched','--frames','0:4',ok=False)
        self.assertIn('full merges',rejected.stderr)

    def test_20_copy_unpatched_millisecond_source_and_late_fallback(self):
        # Matroska milliseconds cannot hold 23.976 fps frame times: refused before any work.
        src=self.base/'ms.mkv'
        fp.run([fp.FFMPEG,'-v','error','-n','-f','lavfi','-i','testsrc2=s=192x128:r=24000/1001','-frames:v','96',
                '-c:v','libx264','-pix_fmt','yuv420p','-x264-params','keyint=24:min-keyint=24:scenecut=0:bframes=3',src])
        p=self.patch(generated=str(src),target=[26,34],generated_start=26,frame_map=None,crop=[0,0,192,128],work_size=[192,128])
        p.pop('frame_map')
        cfg=self.config('ms',source=str(src),source_size=[192,128],fps='24000/1001',patches=[p])
        out=self.base/'ms-copy.mp4'
        r=cli('merge','--config',cfg,'--output',out,'--copy-unpatched')
        self.assertIn('cannot hold',r.stderr)
        self.assertEqual(fp.probe(out)['frames'],96)
        # Without that early rule the joined stream fails its timing check after the join; the fallback
        # must then remove the joined file and render in full (Windows: no process may still hold it).
        out2=self.base/'ms-late.mp4'
        code=("import sys; sys.path.insert(0, sys.argv[1]); import flow_patch as fp; "
              "fp.copy_unpatched_reason = lambda info: None; sys.argv = sys.argv[1:2] + sys.argv[2:]; fp.main()")
        r=subprocess.run([sys.executable,'-c',code,str(ROOT/'scripts'),'merge','--config',str(cfg),'--output',str(out2),
                          '--copy-unpatched'],capture_output=True,text=True,encoding='utf-8')
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertIn('joined stream failed',r.stderr)
        self.assertEqual(fp.probe(out2)['frames'],96)
        self.assertEqual(json.loads(out2.with_suffix('.report.json').read_text(encoding='utf-8'))['copy_unpatched'],
                         {'used':False})
        self.assertFalse(any(self.base.glob('.flow-patch-*')),'temporary folder left behind')

    def test_21_prefilled_review_and_tools(self):
        cfg=self.config('review',patches=[self.patch()]);out=self.base/'review.mp4'
        p=cli('merge','--config',cfg,'--output',out)
        review=out.with_suffix('.visual-review.json')
        self.assertIn('Visual review record',p.stdout)
        rpt=json.loads(out.with_suffix('.report.json').read_text(encoding='utf-8'))
        rec=json.loads(review.read_text(encoding='utf-8'))
        self.assertEqual((rec['output_sha256'],rec['output_frames'],rec['output']),(rpt['output_sha256'],24,'review.mp4'))
        self.assertEqual(rec['overall_visual_status'],'pending_visual_review')
        status={c['id']:c['status'] for c in rec['visual_checks']}
        self.assertEqual((status['decode_dimensions_fps_frames'],status['original_audio_payload']),('pass','pass'))
        self.assertEqual({s for k,s in status.items() if k not in ('decode_dimensions_fps_frames','original_audio_payload')},
                         {'not_run'})
        self.assertEqual(rec['frame_mapping_evidence']['patches'][0]['repeated_frames'],1)  # tail hold
        tools={t['tool']:t for t in rpt['tools']}
        self.assertTrue({'CPython','NumPy','OpenCV','FFmpeg','ffprobe','video encoder'} <= set(tools))
        self.assertEqual(tools['video encoder']['version'],'libx264')
        # Check-only exports get no review record; an existing record blocks a new output of that name.
        cli('merge','--config',cfg,'--output',self.base/'review-check.mp4','--preview')
        self.assertFalse((self.base/'review-check.visual-review.json').exists())
        out.unlink(); out.with_suffix('.report.json').unlink()
        self.assertIn('exists',cli('merge','--config',cfg,'--output',out,ok=False).stderr)

    def test_22_regrid_millisecond_mkv(self):
        # A plain -c copy remux keeps the millisecond rounding and fails the CFR check (issue #13);
        # regrid snaps each timestamp to n/fps and proves frames and audio are unchanged.
        src=self.base/'regrid.mkv'
        fp.run([fp.FFMPEG,'-v','error','-n','-f','lavfi','-i','testsrc2=s=192x128:r=24000/1001','-f','lavfi',
                '-i','sine=sample_rate=48000:duration=5','-frames:v','96','-c:v','libx264','-pix_fmt','yuv420p',
                '-x264-params','keyint=24:min-keyint=24:scenecut=0:bframes=3','-c:a','flac','-shortest',src])
        plain=self.base/'regrid-plain.mp4'
        fp.run([fp.FFMPEG,'-v','error','-n','-i',src,'-map','0','-c','copy',plain])
        with self.assertRaises(ValueError):
            fp.probe(plain)
        out=self.base/'regrid.mp4'
        r=cli('regrid','--source',src,'--output',out)
        self.assertIn('frames and audio unchanged',r.stdout)
        rpt=json.loads(out.with_suffix('.report.json').read_text(encoding='utf-8'))
        self.assertEqual((rpt['frames'],rpt['fps'],rpt['audio_payload_identical']),(96,'24000/1001',True))
        self.assertLess(rpt['max_timestamp_shift_ms'],1)
        self.assertTrue(rpt['copy_unpatched_eligible'])
        self.assertEqual(fp.decode_pass(out)[1],fp.decode_pass(src)[1])
        self.assertIn('exists',cli('regrid','--source',src,'--output',out,ok=False).stderr)
        # The regridded file now takes the copy path.
        p=self.patch(generated=str(out),target=[26,34],generated_start=26,crop=[0,0,192,128],work_size=[192,128])
        p.pop('frame_map')
        cfg=self.config('regrid',source=str(out),source_size=[192,128],fps='24000/1001',patches=[p])
        merged=self.base/'regrid-copy.mp4'
        r=cli('merge','--config',cfg,'--output',merged,'--copy-unpatched')
        self.assertNotIn('not used',r.stderr)
        self.assertGreater(json.loads(merged.with_suffix('.report.json').read_text(encoding='utf-8'))['copy_unpatched']['copied_frames'],0)

    def gop_source(self,name,audio=True):
        src=self.base/name
        sound=['-f','lavfi','-i','sine=d=2','-c:a','aac','-shortest'] if audio else []
        fp.run([fp.FFMPEG,'-v','error','-n','-f','lavfi','-i','testsrc2=s=192x128:r=24:d=2',*sound,'-c:v','libx264',
                '-x264-params','keyint=12:min-keyint=12:scenecut=0:bframes=3','-pix_fmt','yuv420p',src])
        return src

    def gop_config(self,name,src):
        p=self.patch(generated=str(self.generated),target=[14,20],frame_map=list(range(6)),crop=[0,0,96,128],protect=None)
        return self.config(name,source=str(src),source_size=[192,128],patches=[p])

    def test_23_corrupt_audio_is_rejected(self):
        # issue #14: video intact, one AAC packet's payload garbled; the output check must decode audio too.
        src=self.gop_source('bad-audio.mp4')
        pk=json.loads(fp.run([fp.FFPROBE,'-v','error','-select_streams','a:0','-show_entries','packet=pos,size',
                              '-of','json',src]))['packets'][20]
        with open(src,'r+b') as f:
            f.seek(int(pk['pos'])); f.write(bytes((i*37+11)%256 for i in range(int(pk['size']))))
        self.assertEqual(fp.decode_pass(src)[0][:1],[0.0])  # the video alone still decodes
        out=self.base/'bad-audio-out.mp4'
        r=cli('merge','--config',self.gop_config('bad-audio',src),'--output',out,ok=False)
        self.assertIn('aac',r.stderr.lower())  # FFmpeg 6.1: 'Decode errors: ...'; 7+: -xerror exit with the decoder message
        self.assertFalse(out.exists() or out.with_suffix('.report.json').exists())

    def test_24_fallback_render_gets_its_own_strict_check(self):
        # issue #15: after a late --copy-unpatched fallback, verify() must not reuse the joined file's checks.
        # Two injected faults: the copied-frame comparison fails, and the fallback render is not a video.
        src=self.gop_source('gop-fallback.mp4',audio=False)  # no audio hash to catch the bad file instead
        cfg=self.gop_config('gop-fallback',src); out=self.base/'gop-fallback-out.mp4'
        code=("import sys; sys.path.insert(0, sys.argv[1]); import flow_patch as fp\n"
              "fp.frame_hashes = lambda path: [str(path)] * 48\n"
              "real = fp.process\n"
              "def broken(cfg, info, mode, out, *a, **k):\n"
              "    r = real(cfg, info, mode, out, *a, **k)\n"
              "    if 'pieces' not in str(out): open(out, 'wb').write(b'not a video' * 4)\n"
              "    return r\n"
              "fp.process = broken; fp.merge_segmented = lambda *a, **k: (_ for _ in ()).throw(AssertionError)\n"
              "sys.argv = sys.argv[1:2] + sys.argv[2:]; fp.main()")
        r=subprocess.run([sys.executable,'-c',code,str(ROOT/'scripts'),'merge','--config',str(cfg),'--output',str(out),
                          '--copy-unpatched'],capture_output=True,text=True,encoding='utf-8',env=dict(os.environ,FLOW_SEGMENTS='1'))
        self.assertNotEqual(r.returncode,0,r.stdout)
        self.assertIn('copied frames do not decode identically',r.stderr)
        self.assertFalse(out.exists() or out.with_suffix('.report.json').exists())

    def test_25_span_starts_no_decoder_for_unused_returns(self):
        # The second person's patch lies entirely after the exported span; its return is never read.
        p=self.patch(target=[0,12],frame_map=list(range(12)))
        q=self.patch(target=[12,24],frame_map=list(range(12)),crop=[96,0,96,128])
        cfg=fp.load(self.config('unused-reader',patches=[p,q]))
        info=fp.validate_source(cfg); plans=fp.merge_plan(cfg,info)
        opened=[]; original=fp.Reader
        class Counted(original):
            def __init__(self,path,*a,**k):
                opened.append(Path(path).name); super().__init__(path,*a,**k)
        fp.Reader=Counted
        try:
            for span,expect in (((0,12),['source 中文.mp4','flow.mp4']),
                                ((4,16),['source 中文.mp4','flow.mp4','flow.mp4'])):
                opened.clear(); out=self.base/f'unused-reader-{span[0]}.mp4'
                fp.process(cfg,info,'merge',out,plans,span)
                self.assertEqual(opened,expect)
                self.assertEqual(fp.probe(out)['frames'],span[1]-span[0])
        finally:
            fp.Reader=original
        before,after=image(self.source,6),image(self.base/'unused-reader-0.mp4',6)
        self.assertGreater(np.mean(abs(after[30:60,30:65].astype(float)-before[30:60,30:65])),20)

    def test_26_check_export_checks_only_its_frames(self):
        # From PR #17. Keyframed affine that leaves holes in the patch support from frame 20 on.
        shift=[dict(frame=0,value=[1,0,0,0,1,0]),dict(frame=19,value=[1,0,0,0,1,0]),
               dict(frame=20,value=[1,0,40,0,1,0])]
        cfg=self.config('span-check',patches=[self.patch(source_to_generated_affine=shift)])
        cli('merge','--config',cfg,'--output',self.base/'span-ok.mp4','--frames','2:8','--preview')
        late=cli('merge','--config',cfg,'--output',self.base/'span-late.mp4','--frames','18:22','--preview',ok=False)
        self.assertIn('source frame 20',late.stderr)
        # Deliverable and dry runs still check every patched frame, also a dry run given --frames.
        full=cli('merge','--config',cfg,'--output',self.base/'span-full.mp4',ok=False)
        self.assertIn('source frame 20',full.stderr)
        for extra in ((),('--frames','2:8')):
            self.assertIn('source frame 20',cli('merge','--config',cfg,'--output',self.base/'span-dry.mp4',
                                                '--dry-run',*extra,ok=False).stderr)
        # A patch entirely outside the span still has its first frame checked.
        broken=self.patch(target=[12,24],frame_map=list(range(12)),crop=[96,0,96,128],
                          source_to_generated_affine=[1,0,40,0,1,0])
        cfg=self.config('span-outside',patches=[self.patch(target=[0,12],frame_map=list(range(12))),broken])
        self.assertIn('source frame 12',cli('merge','--config',cfg,'--output',self.base/'span-out.mp4',
                                            '--frames','2:8','--preview',ok=False).stderr)
        # The interval is checked before any plan work.
        self.assertIn('Invalid --frames',cli('merge','--config',cfg,'--output',self.base/'span-bad.mp4',
                                             '--frames','20:30',ok=False).stderr)

    def test_27_grouped_conversion_matches_one_union_box(self):
        # Patches far apart are converted in separate boxes; adjacent ones sharing a chroma block stay
        # together. Encoder input must equal converting one union box, so outputs are byte-identical.
        g=lambda *crops:[(None,None,None,0,c) for c in crops]
        boxes=lambda groups:[(len(m),b) for m,b in groups]
        self.assertEqual(boxes(fp.chroma_groups(g((3,5,41,41),(141,81,45,45)))),[(1,[2,4,44,46]),(1,[140,80,186,126])])
        self.assertEqual(boxes(fp.chroma_groups(g((10,11,51,51),(61,11,51,51)))),[(2,[10,10,112,62])])
        self.assertEqual(boxes(fp.chroma_groups(g((0,0,61,61),(61,0,61,61),(120,61,61,61)))),[(3,[0,0,182,122])])
        self.assertEqual(fp.chroma_groups([]),[])
        def union(active):
            if not active: return []
            x0=min(r[0] for *_,r in active)&~1; y0=min(r[1] for *_,r in active)&~1
            x1=max(r[0]+r[2] for *_,r in active); y1=max(r[1]+r[3] for *_,r in active)
            return [(active,[x0,y0,x1+(x1&1),y1+(y1&1)])]
        ps=[self.patch(crop=[3,5,45,60],work_size=[96,128],matte=dict(region=[16,16,64,96],feather=8)),
            self.patch(crop=[141,61,45,60],work_size=[96,128],blend='multiband',levels=3,protect=None,
                       matte=dict(region=[8,8,80,112],feather=8))]
        cfg=fp.load(self.config('grouped',patches=ps)); info=fp.validate_source(cfg); plans=fp.merge_plan(cfg,info)
        grouped,joined=self.base/'grouped.mp4',self.base/'grouped-union.mp4'
        fp.process(cfg,info,'merge',grouped,plans)
        original=fp.chroma_groups; fp.chroma_groups=union
        try:
            fp.process(cfg,info,'merge',joined,plans)
        finally:
            fp.chroma_groups=original
        self.assertEqual(grouped.read_bytes(),joined.read_bytes())
        self.assertGreater(np.mean(abs(image(grouped,6)[20:50,15:40].astype(float)-image(self.source,6)[20:50,15:40])),10)

    def test_28_copy_unpatched_without_copyable_gops_keeps_source_reordering(self):
        # Every GOP holds a patch frame. The render still follows the source's B-frame reordering
        # (here none), as it always did; a plain full render would use the encoder's default B-frames.
        src=self.base/'gop-nob.mp4'
        fp.run([fp.FFMPEG,'-v','error','-n','-f','lavfi','-i','testsrc2=s=192x128:r=24:d=2','-c:v','libx264',
                '-x264-params','keyint=12:min-keyint=12:scenecut=0:bframes=0','-pix_fmt','yuv420p',src])
        p=self.patch(generated=str(self.generated),target=[6,29],frame_map=list(range(23)),crop=[0,0,96,128],protect=None)
        q=self.patch(generated=str(self.generated),target=[40,44],frame_map=list(range(4)),crop=[96,0,96,128],protect=None)
        cfg=self.config('gop-nob',source=str(src),source_size=[192,128],patches=[p,q])
        out=self.base/'gop-nob-out.mp4'
        r=subprocess.run([sys.executable,str(ROOT/'scripts/flow_patch.py'),'merge','--config',str(cfg),'--output',str(out),
                          '--copy-unpatched'],capture_output=True,text=True,encoding='utf-8',env=dict(os.environ,FLOW_SEGMENTS='1'))
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertIn('copied no frames',r.stderr)
        rpt=json.loads(out.with_suffix('.report.json').read_text(encoding='utf-8'))
        self.assertEqual((rpt['copy_unpatched']['copied_frames'],rpt['copy_unpatched']['rendered_frames']),(0,48))
        b=lambda path:json.loads(fp.run([fp.FFPROBE,'-v','error','-select_streams','v:0','-show_entries',
                                         'stream=has_b_frames','-of','json',str(path)]))['streams'][0]['has_b_frames']
        self.assertEqual(b(out),b(src))


    def test_29_decoder_pipe_fallback_and_failure_cleanup(self):
        # Non-Windows must keep subprocess' standard pipe, without touching Win32.
        sentinel = object()
        with mock.patch.object(fp.sys, 'platform', 'linux'), mock.patch.object(fp.subprocess, 'Popen', return_value=sentinel) as start:
            self.assertIs(fp.decoder_process(['decoder'], None), sentinel)
            start.assert_called_once_with(['decoder'], stdout=subprocess.PIPE, stderr=None)
        if sys.platform != 'win32':
            return
        import ctypes
        from ctypes import wintypes
        import msvcrt
        import gc
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        def handles():
            gc.collect()
            n = wintypes.DWORD()
            self.assertTrue(kernel.GetProcessHandleCount(kernel.GetCurrentProcess(), ctypes.byref(n)))
            return n.value
        before = handles()
        # Both raw-handle and fd/stream ownership transfers must unwind.
        for target in ('start', 'second_fd', 'second_stream'):
            original_fd, original_stream = msvcrt.open_osfhandle, fp.os.fdopen
            calls = [0]
            def fail_second(original):
                def call(*a, **kw):
                    calls[0] += 1
                    if calls[0] == 2:
                        raise OSError('injected '+target)
                    return original(*a, **kw)
                return call
            if target == 'start':
                patcher = mock.patch.object(fp.subprocess, 'Popen', side_effect=OSError('injected start'))
            elif target == 'second_fd':
                patcher = mock.patch.object(msvcrt, 'open_osfhandle', side_effect=fail_second(original_fd))
            else:
                patcher = mock.patch.object(fp.os, 'fdopen', side_effect=fail_second(original_stream))
            with patcher:
                with self.assertRaisesRegex(OSError, 'injected'):
                    fp.decoder_process(['decoder'], None)
            self.assertEqual(handles(), before, target+' leaked handles')
        proc = fp.decoder_process([sys.executable, '-c', 'import sys; sys.stdout.buffer.write(bytes(range(256))*8192)'], None)
        self.assertEqual(proc.stdout.read(), bytes(range(256))*8192)
        proc.stdout.close()
        self.assertEqual(proc.wait(), 0)
        del proc
        self.assertEqual(handles(), before)

    def test_30_fixed_color_lut_exact_and_bounded(self):
        pixels = np.tile(np.arange(256, dtype=np.uint8)[:,None,None], (1,3,3))
        original = pixels.copy()
        fp.color_lut.cache_clear()
        for gain, offset in [([1,1,1],[-.5,.5,1.5]),([.001,3.7,1.001],[-231.5,127.5,-.5]),
                             ([.9,1.02,1.2],[3,-1,.5])]:
            expected = np.clip(pixels.astype(float)*gain+offset,0,255).round().astype(np.uint8)
            actual = cv2.LUT(pixels, fp.color_lut(tuple(gain),tuple(offset)))
            self.assertTrue(np.array_equal(actual, expected))
        for i in range(64):
            fp.color_lut((1+i/100,1,1),(0,0,0))
        self.assertEqual(fp.color_lut.cache_info().currsize, 32)
        self.assertFalse(fp.color_lut((1,1,1),(0,0,0)).flags.writeable)
        self.assertTrue(np.array_equal(pixels, original))


    def test_31_pyramid_workspace_ownership_budget_and_exactness(self):
        rng = np.random.default_rng(3107)
        o = rng.integers(0,256,(128,192,3),dtype=np.uint8)
        p = rng.integers(0,256,o.shape,dtype=np.uint8)
        m = fp.matte(dict(region=[16,16,160,96],feather=8),0,[192,128])
        for levels in range(1,7):
            for fade in (0,.13,.5,1):
                fp._BLEND_LOCAL.workspace = None
                with mock.patch.object(fp.PyramidWorkspace,'BUDGET',0):
                    ref = fp.blend(o,p,m*fade,'multiband',levels)
                fp._BLEND_LOCAL.workspace = None
                actual = fp.blend(o,p,m*fade,'multiband',levels)
                saved = actual.copy()
                self.assertTrue(np.array_equal(actual,ref),(levels,fade))
                fp.blend(p,o,m*(1-fade),'multiband',levels)
                self.assertTrue(np.array_equal(actual,saved),'a later frame mutated prior output')
                self.assertLessEqual(fp._BLEND_LOCAL.workspace.bytes,fp.PyramidWorkspace.BUDGET)
        # A low budget caches only what fits; the other slots retain original allocation.
        fp._BLEND_LOCAL.workspace = None
        with mock.patch.object(fp.PyramidWorkspace,'BUDGET',1024):
            small = fp.blend(o,p,m,'multiband',3)
            self.assertLessEqual(fp._BLEND_LOCAL.workspace.bytes,1024)
        self.assertTrue(np.array_equal(small,fp.blend(o,p,m,'multiband',3)))
        fp._BLEND_LOCAL.workspace = None
        fp.blend(o,p,m,'alpha',3)
        self.assertIsNone(fp._BLEND_LOCAL.workspace)
        # Calls from different Python threads must never share destination arrays.
        import threading
        barrier = threading.Barrier(2)
        def threaded(offset):
            result = fp.blend(o,p,m*offset,'multiband',3)
            workspace = fp._BLEND_LOCAL.workspace
            barrier.wait()
            return result,workspace
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(threaded,.5);second = pool.submit(threaded,1)
            a,wa=first.result();b,wb=second.result()
        self.assertIsNot(wa,wb)
        self.assertTrue(np.array_equal(a,fp.blend(o,p,m*.5,'multiband',3)))
        self.assertTrue(np.array_equal(b,fp.blend(o,p,m,'multiband',3)))


    def test_32_frame_pool_leases_acknowledgement_and_reader_ownership(self):
        import threading
        with mock.patch.object(fp,'FRAME_POOL_MIN',1):
            stop=threading.Event();pool=fp._FramePool(8,8*(fp.PREFETCH+2))
            held=[pool.acquire(stop) for _ in range(fp.PREFETCH+2)]
            self.assertEqual(len({id(x) for x in held}),fp.PREFETCH+2)
            result=[];waiting=threading.Thread(target=lambda:result.append(pool.acquire(stop)))
            waiting.start();stop.set();waiting.join(timeout=2)
            self.assertFalse(waiting.is_alive());self.assertEqual(result,[None])
            pool.release(held[0]);pool.release(held[0])
            self.assertEqual(pool.available.qsize(),1,'duplicate release duplicated a lease')
            fallback=fp._FramePool(100,499)
            self.assertEqual(fallback.limit,0)
            a=fallback.acquire(threading.Event());b=fallback.acquire(threading.Event())
            self.assertIsNot(a,b);self.assertFalse(fallback.buffers)

            class SlowPipe:
                def __init__(self):
                    self.entered=threading.Event();self.proceed=threading.Event();self.written=[]
                def write(self,item):
                    self.entered.set()
                    if not self.proceed.wait(timeout=5):raise OSError('test writer timeout')
                    self.written.append(bytes(item))
            pipe=SlowPipe();writer=fp.FrameWriter(pipe)
            first=writer.acquire(8);first[:]=b'A'*8;writer.write(first)
            self.assertTrue(pipe.entered.wait(timeout=2))
            second=writer.acquire(8);self.assertIsNot(first,second)
            self.assertTrue(writer.pool.available.empty(),'buffer recycled before write finished')
            try:
                with self.assertRaisesRegex(ValueError,'already queued'):
                    writer.write(first)
            finally:
                pipe.proceed.set();writer.finish()
            self.assertEqual(pipe.written,[b'A'*8]);self.assertEqual(writer.pool.available.qsize(),1)

            class BrokenPipe:
                def write(self,item):raise BrokenPipeError('injected write failure')
            writer=fp.FrameWriter(BrokenPipe());buf=writer.acquire(8);writer.write(buf)
            error=writer.finish();self.assertIsInstance(error,BrokenPipeError)
            self.assertEqual(writer.pool.available.qsize(),1,'failed write did not return its lease')
            with self.assertRaises(BrokenPipeError):writer.acquire(8)

            info=fp.probe(self.source)
            readers=[fp.Reader(self.source,info['size'],info['color'],recycle=x) for x in (False,True)]
            try:
                prior=readers[1].get(0);saved=prior.copy()
                raw=readers[0].get(0);old_raw=readers[0].raw;saved_raw=old_raw.copy()
                for index in [0,0,5,6,12,23]:
                    self.assertTrue(np.array_equal(readers[0].get(index),readers[1].get(index)),index)
                self.assertTrue(np.array_equal(prior,saved),'pooled raw data mutated returned BGR')
                self.assertTrue(np.array_equal(old_raw,saved_raw),'default Reader recycled caller-held raw data')
                self.assertLessEqual(len(readers[1]._raw_pool.buffers),fp.PREFETCH+2)
            finally:
                for reader in readers:reader.close()
            reader=fp.Reader(self.source,info['size'],info['color'],recycle=True)
            reader.get(0);reader.close();self.assertFalse(reader.thread.is_alive())


    def test_33_pooled_merge_matches_unpooled_with_mapping_fade_and_spans(self):
        mapping=list(range(23))+[22];mapping[5]=None
        patch=self.patch(frame_map=mapping,fade_frames=3,gain_bgr=[1.01,.99,1.02],
                         offset_bgr=[.5,-.5,2],source_to_generated_affine=[1,0,.25,0,1,-.5])
        cfg=fp.load(self.config('pooled-quality',patches=[patch]));info=fp.validate_source(cfg)
        plans=fp.merge_plan(cfg,info)
        with mock.patch.object(fp,'FRAME_POOL_MIN',1):
            for n,span in enumerate([None,(0,12),(4,16)]):
                hashes=[]
                for enabled in [False,True]:
                    out=self.base/f'pooled-{n}-{enabled}.mp4'
                    with mock.patch.object(fp,'RAW_POOL_BUDGET',128*1024*1024 if enabled else 0), \
                         mock.patch.object(fp,'OUTPUT_POOL_BUDGET',64*1024*1024 if enabled else 0):
                        plan,_=fp.process(cfg,info,'merge',out,plans,span,preset='ultrafast')
                        fp.verify(cfg['source'],str(out),dict(plan,fps=info['fps']),span is None)
                    hashes.append(fp.sha(out))
                self.assertEqual(hashes[0],hashes[1],span)
        # RGB readers must keep fresh storage even when pooling is explicitly requested.
        rgb=self.base/'rgb-pool.nut'
        fp.run([fp.FFMPEG,'-v','error','-n','-f','lavfi','-i','testsrc2=s=96x64:r=24',
                '-frames:v','12','-c:v','rawvideo','-pix_fmt','bgr24',rgb])
        meta=fp.probe(rgb)
        with mock.patch.object(fp,'FRAME_POOL_MIN',1):
            reader=fp.Reader(rgb,meta['size'],meta['color'],recycle=True)
            try:
                self.assertIsNone(reader._raw_pool)
                first=reader.get(0);saved=first.copy();reader.get(11)
                self.assertTrue(np.array_equal(first,saved))
            finally:reader.close()


    def test_34_structure_view_matches_full_alpha_before_encoding(self):
        # Nonzero view origin, odd moving crops, subpixel affine, null/repeat mappings,
        # fades and a protected mouth must survive the check-only path unchanged.
        source=self.base/'structure-source.mp4'
        fp.run([fp.FFMPEG,'-v','error','-n','-i',self.source,'-vf','pad=384:256:100:64',
                '-c:v','libx264','-crf','10','-c:a','copy',source])
        indices=list(range(23))+[22];indices[5]=None
        p=self.patch(crop=[dict(frame=0,value=[99,65,96,128]),
                           dict(frame=23,value=[109,65,96,128])],
                     frame_map=indices,fade_frames=3,gain_bgr=[.5,1.2,.8],offset_bgr=[30,0,-10],
                     source_to_generated_affine=[1,0,.25,0,1,-.5])
        cfg=fp.load(self.config('structure-oracle',source=str(source),source_size=[384,256],patches=[p]))
        saved=json.dumps(cfg,sort_keys=True);info=fp.validate_source(cfg)
        plans=fp.merge_plan(cfg,info)
        effective,box=fp.structure_preview(cfg,info,plans,(2,16))
        self.assertEqual(json.dumps(cfg,sort_keys=True),saved)
        self.assertEqual(effective['patches'][0]['gain_bgr'],[1,1,1])
        self.assertEqual(effective['patches'][0]['matte'],p['matte'])
        self.assertGreater(box[0],0);self.assertGreaterEqual(box[1],0)
        self.assertTrue(all(v%2==0 for v in box))
        captured=[];original_write=fp.FrameWriter.write
        def capture(writer,frame):
            captured.append(bytes(frame));return original_write(writer,frame)
        results=[]
        with mock.patch.object(fp.FrameWriter,'write',capture):
            for name,view in [('full',None),('local',box)]:
                captured.clear();out=self.base/f'structure-{name}.mp4'
                plan,_=fp.process(effective,info,'merge',out,plans,(2,16),preset='ultrafast',view_box=view)
                fp.verify(source,out,dict(plan,fps=info['fps']),False)
                results.append(list(captured))
        x,y,w,h=box
        for full,local in zip(*results):
            _,Y,U,V=fp.planes420(full,384,256)
            expected=b''.join(c.tobytes() for c in (Y[y:y+h,x:x+w],
                            U[y//2:(y+h)//2,x//2:(x+w)//2],V[y//2:(y+h)//2,x//2:(x+w)//2]))
            self.assertEqual(local,expected)
        self.assertEqual(len(results[0]),14)
        # A missing patch frame is source-only, not silently held or removed.
        reader=fp.Reader(source,info['size'],info['color'],view_box=box)
        try:self.assertEqual(results[1][3],bytes(reader.passthrough420(5)[0]))
        finally:reader.close()
        # Every even-aligned crop retains the full-view source chroma grid.
        rgb=self.base/'structure-rgb.nut'
        fp.run([fp.FFMPEG,'-v','error','-n','-f','lavfi','-i','testsrc2=s=192x128:r=24',
                '-frames:v','12','-c:v','rawvideo','-pix_fmt','bgr24',rgb])
        for path in (source,self.source,rgb):
            meta=fp.probe(path);reader=fp.Reader(path,meta['size'],meta['color'])
            try:
                for rect in ([2,2,64,32],[0,0,96,128]):
                    local=fp.Reader(path,meta['size'],meta['color'],view_box=rect)
                    try:raw=local.passthrough420(0)[0]
                    finally:local.close()
                    _,Y,U,V=reader.passthrough420(0)
                    x,y,w,h=rect
                    self.assertEqual(bytes(raw),b''.join(c.tobytes() for c in (
                        Y[y:y+h,x:x+w],U[y//2:(y+h)//2,x//2:(x+w)//2],
                        V[y//2:(y+h)//2,x//2:(x+w)//2])))
                with self.assertRaisesRegex(ValueError,'view rectangle'):
                    fp.Reader(path,meta['size'],meta['color'],view_box=[1,0,64,32])
            finally:reader.close()

    def test_35_structure_cli_scope_and_safety(self):
        p=self.patch(blend='multiband',levels=3,matte=dict(region=[16,16,64,96],feather=8),
                     gain_bgr=[.5,1,1],offset_bgr=[30,0,0])
        config=self.config('structure-cli',patches=[p]);before=config.read_bytes()
        out=self.base/'structure-cli.mp4'
        cli('merge','--config',config,'--output',out,'--structure-preview','--frames','3:12')
        report=json.loads(out.with_suffix('.report.json').read_text(encoding='utf-8'))
        scope=report['scope'];view=scope['structure_preview']
        self.assertFalse(scope['deliverable']);self.assertTrue(scope['preview_encode'])
        self.assertEqual(scope['frames'],[3,12]);self.assertEqual(view['source_frame_at_output_zero'],3)
        self.assertEqual(report['output']['size'],view['view_box_source_xywh'][2:])
        self.assertEqual(report['output']['frames'],9);self.assertTrue(report['decode_pass'])
        self.assertEqual(report['configuration']['patches'][0]['blend'],'multiband')
        self.assertEqual(report['effective_configuration']['patches'][0]['blend'],'alpha')
        self.assertEqual(config.read_bytes(),before)
        self.assertFalse(out.with_suffix('.visual-review.json').exists())
        rejected=cli('merge','--config',config,'--output',self.base/'invalid-copy.mp4',
                     '--structure-preview','--copy-unpatched',ok=False)
        self.assertIn('cannot use',rejected.stderr)
        cli('prepare','--config',config,'--output',self.base/'invalid-prepare.mp4',
            '--structure-preview',ok=False)
        # Original delivery parameters are validated before any simplified recipe is applied.
        bad=self.config('invalid-color-structure',patches=[self.patch(gain_bgr=[0,1,1])])
        rejected=cli('merge','--config',bad,'--output',self.base/'invalid-color.mp4',
                     '--structure-preview',ok=False)
        self.assertIn('Invalid fixed color transform',rejected.stderr)
        empty=self.config('structure-empty',patches=[self.patch(frame_map=[None]*24)])
        rejected=cli('merge','--config',empty,'--output',self.base/'empty.mp4',
                     '--structure-preview',ok=False)
        self.assertIn('no mapped patch frames',rejected.stderr)

    def test_36_structure_preview_keeps_known_seam_and_occlusion_defects(self):
        # A seam crossing line jumps four pixels in the return. An occluder is erased.
        # The check recipe must reveal both, and respect the same protection if supplied.
        source=self.base/'defect-source.mp4';generated=self.base/'defect-return.mp4'
        im=np.full((256,384,3),40,np.uint8)
        im[80:84,40:344]=230
        im[96:174,144:152]=[20,220,20]
        returned=im[64:192,100:196].copy()
        returned[16:20]=40;returned[20:24]=230
        returned[32:110,44:52]=[20,20,220]
        encode(source,[im.copy() for _ in range(24)])
        encode(generated,[returned.copy() for _ in range(24)])
        p=self.patch(generated=str(generated),crop=[100,64,96,128],
                     matte=dict(region=[8,8,80,112],feather=4),blend='multiband',levels=3)
        cfg=fp.load(self.config('defects',source=str(source),source_size=[384,256],patches=[p]))
        info=fp.validate_source(cfg);plans=fp.merge_plan(cfg,info)
        effective,box=fp.structure_preview(cfg,info,plans)
        out=self.base/'defects.mp4'
        fp.process(effective,info,'merge',out,plans,preset='ultrafast',view_box=box)
        actual=image(out,12);x,y,_,_=box
        # There is a visible step at the mask edge; the internal line was not smoothed away.
        self.assertLess(actual[81-y,130-x].mean(),75)
        self.assertGreater(actual[85-y,130-x].mean(),180)
        self.assertGreater(actual[81-y,104-x].mean(),180)
        wrong=actual[120-y,148-x].astype(int)
        self.assertGreater(wrong[2]-wrong[1],120,'bad foreground order was concealed')
        # The correct foreground layer must also survive the simplified recipe.
        p=dict(p,blend='alpha',matte=dict(region=[8,8,80,112],feather=4,protect=[[44,32,8,78]]))
        cfg=dict(cfg,patches=[p]);plans=fp.merge_plan(cfg,info)
        effective,box=fp.structure_preview(cfg,info,plans)
        out=self.base/'defects-protected.mp4'
        fp.process(effective,info,'merge',out,plans,preset='ultrafast',view_box=box)
        correct=image(out,12)[120-y,148-x].astype(int)
        self.assertGreater(correct[1]-correct[2],120,'foreground protection was lost')


if __name__=='__main__':
    unittest.main(verbosity=2)
