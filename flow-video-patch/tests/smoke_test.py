#!/usr/bin/env python3
"""One-command behavioral verification with disposable, synthetic media only."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

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
        self.assertEqual(rpt['scope'],{'frames':[6,18],'preview_encode':True,'deliverable':False})
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


if __name__=='__main__':
    unittest.main(verbosity=2)
