#!/usr/bin/env python3
"""Color pipeline checks with disposable synthetic media: matrix, range, tags, no drift."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import flow_patch as fp

W,H = 256,144
KR_KB = {'bt709':(0.2126,0.0722),'bt601':(0.299,0.114)}


def cli(*args):
    p = subprocess.run([sys.executable,str(ROOT/'scripts/flow_patch.py'),*map(str,args)],
                       stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8')
    if p.returncode:
        raise AssertionError(p.stdout+p.stderr)


def frames():
    """Flat swatches (stage blue, skin, gray, dark) plus mild texture, static over time."""
    rng = np.random.default_rng(7)
    im = np.zeros((H,W,3),np.uint8)
    im[:, :64] = [150,40,20]; im[:, 64:128] = [140,170,215]
    im[:, 128:192] = [128,128,128]; im[:, 192:] = [40,35,30]
    im = np.clip(im.astype(int)+rng.integers(-6,7,im.shape),0,255).astype(np.uint8)
    return [im]*12


def encode(path, images, vf, tags):
    cmd = [fp.FFMPEG,'-v','error','-n','-f','rawvideo','-pix_fmt','bgr24','-s',f'{W}x{H}',
           '-r','24','-i','-','-vf',vf,'-c:v','libx264','-crf','4']
    if tags:
        cmd += ['-bsf:v','h264_metadata='+tags]
    subprocess.run(cmd+[str(path)],input=b''.join(im.tobytes() for im in images),check=True,
                   stdout=subprocess.PIPE,stderr=subprocess.PIPE)


def planes(path, index, pix='yuv420p'):
    raw = subprocess.run([fp.FFMPEG,'-v','error','-i',str(path),'-vf',f'select=eq(n\\,{index})',
                          '-frames:v','1','-f','rawvideo','-pix_fmt',pix,'-'],
                         stdout=subprocess.PIPE,check=True).stdout
    a = np.frombuffer(raw,np.uint8)
    y = a[:W*H].reshape(H,W).astype(float)
    u = a[W*H:W*H*5//4].reshape(H//2,W//2).astype(float)
    v = a[W*H*5//4:].reshape(H//2,W//2).astype(float)
    return y,u,v


def stream(path):
    return json.loads(fp.run([fp.FFPROBE,'-v','error','-select_streams','v:0','-show_streams',
                              '-of','json',str(path)]))['streams'][0]


class ColorPipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='flow-patch-color-')
        cls.base = Path(cls.tmp.name)
        cls.hd = cls.base/'tagged709.mp4'
        encode(cls.hd,frames(),'scale=out_color_matrix=bt709:out_range=tv,format=yuv420p',
               'colour_primaries=1:transfer_characteristics=1:matrix_coefficients=1:video_full_range_flag=0')
        cls.sd = cls.base/'untagged.mp4'
        encode(cls.sd,frames(),'format=yuv420p',None)
        cls.full = cls.base/'full709.mp4'
        encode(cls.full,frames(),'scale=out_color_matrix=bt709:out_range=pc,format=yuv420p',
               'colour_primaries=1:transfer_characteristics=1:matrix_coefficients=1:video_full_range_flag=1')

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def merge(self, source, name, generated=None):
        # Identity patch: generated clip is the source itself, so the patch area must not move either.
        cfg = dict(schema_version=1,source=str(source),source_size=[W,H],fps=24,
                   patches=[dict(generated=str(generated or source),target=[0,12],crop=[0,0,W,H],
                                 work_size=[W,H],matte=dict(region=[64,16,96,112],feather=8),
                                 blend='alpha',fade_frames=0)])
        path = self.base/(name+'.json'); path.write_text(json.dumps(cfg),encoding='utf-8')
        out = self.base/(name+'.mp4'); cli('merge','--config',path,'--output',out)
        return out

    def assert_same_yuv(self, a, b, mean_tol=0.35):
        for pa,pb,name in zip(planes(a,6),planes(b,6),'YUV'):
            d = pb-pa
            self.assertLess(abs(d.mean()),mean_tol,f'{name} plane mean shift {d.mean():.3f}')
            self.assertLess(np.abs(d).mean(),2.0,f'{name} plane mean abs {np.abs(d).mean():.3f}')

    def test_01_probe_reports_matrix_and_range(self):
        self.assertEqual(fp.probe(self.hd)['color']['matrix'],'bt709')
        sd = fp.probe(self.sd)['color']
        self.assertEqual((sd['matrix'],sd['range'],sd['assumed']),('bt601','tv',True))
        self.assertEqual(fp.probe(self.full)['color']['range'],'pc')

    def test_02_reader_matches_exact_conversion(self):
        raw = subprocess.run([fp.FFMPEG,'-v','error','-i',str(self.hd),'-frames:v','1','-f','rawvideo',
                              '-pix_fmt','yuv444p','-'],stdout=subprocess.PIPE,check=True).stdout
        y,u,v = (np.frombuffer(raw,np.uint8).reshape(3,H,W).astype(float))
        kr,kb = KR_KB['bt709']
        Y = (y-16)*255/219; Cb = (u-128)*255/224; Cr = (v-128)*255/224
        R = Y+2*(1-kr)*Cr; B = Y+2*(1-kb)*Cb; G = (Y-kr*R-kb*B)/(1-kr-kb)
        ref = np.stack([B,G,R],-1)
        reader = fp.Reader(self.hd,[W,H])
        try:
            got = reader.get(0).astype(float)
        finally:
            reader.close()
        bias = (got-ref).mean((0,1))
        self.assertTrue(np.all(np.abs(bias) < 0.3),f'decode bias {bias}')

    def test_03_merge_keeps_709_values_and_tags(self):
        out = self.merge(self.hd,'hd_once')
        self.assert_same_yuv(self.hd,out)
        s = stream(out)
        self.assertEqual((s.get('color_space'),s.get('color_primaries'),s.get('color_transfer'),
                          s.get('color_range')),('bt709','bt709','bt709','tv'))

    def test_04_untagged_sd_stays_601_and_gets_tagged(self):
        out = self.merge(self.sd,'sd_once')
        self.assert_same_yuv(self.sd,out)
        self.assertEqual(stream(out).get('color_space'),'smpte170m')

    def test_05_full_range_preserved(self):
        out = self.merge(self.full,'full_once')
        self.assert_same_yuv(self.full,out)
        self.assertEqual(stream(out).get('color_range'),'pc')

    def test_06_repeated_merges_do_not_drift(self):
        once = self.merge(self.hd,'hd_a')
        twice = self.merge(once,'hd_b',generated=self.hd)
        self.assert_same_yuv(self.hd,twice,mean_tol=0.45)

    def test_07_prepare_output_tagged(self):
        cfg = dict(schema_version=1,source=str(self.hd),source_size=[W,H],fps=24,
                   prepare=dict(core=[0,12],context_frames=0,crop=[0,0,W,H],output_size=[W,H],
                                mask_regions=[[0,0,2,2]],color_rgb=[0,0,0]))
        path = self.base/'prep.json'; path.write_text(json.dumps(cfg),encoding='utf-8')
        out = self.base/'prep.mp4'; cli('prepare','--config',path,'--output',out)
        self.assertEqual(stream(out).get('color_space'),'bt709')
        y0 = planes(self.hd,6)[0][8:,8:]; y1 = planes(out,6)[0][8:,8:]
        self.assertLess(abs((y1-y0).mean()),0.35)

    def test_08_reject_unsupported_matrix(self):
        bad = self.base/'bt2020.mp4'
        encode(bad,frames(),'format=yuv420p',
               'colour_primaries=9:transfer_characteristics=1:matrix_coefficients=9:video_full_range_flag=0')
        with self.assertRaises(ValueError):
            fp.probe(bad)

    def test_09_outside_support_luma_is_decoded_source(self):
        # Pre-encode frames: luma outside the matte is the decoded source exactly, inside it changes.
        cfg = dict(schema_version=1,source=str(self.hd),source_size=[W,H],fps=24,
                   patches=[dict(generated=str(self.hd),target=[0,12],crop=[33,17,128,72],
                                 work_size=[256,144],matte=dict(region=[40,20,176,104],feather=6),
                                 blend='alpha',gain_bgr=[1.3,1.3,1.3])])
        info = fp.validate_source(cfg); raw = self.base/'pre_encode.yuv'
        raw_encoder = lambda path, size, *a, **k: [fp.FFMPEG,'-v','error','-f','rawvideo','-pix_fmt','yuv420p',
                                                  '-s',f'{size[0]}x{size[1]}','-i','pipe:0','-f','rawvideo',str(path)]
        with unittest.mock.patch.object(fp,'encoder',raw_encoder):
            fp.process(cfg,info,'merge',raw,fp.merge_plan(cfg,info))
        got = np.fromfile(raw,np.uint8).reshape(-1,H*W*3//2)[:,:H*W].reshape(-1,H,W)
        src = np.frombuffer(subprocess.run([fp.FFMPEG,'-v','error','-i',str(self.hd),'-f','rawvideo',
                                            '-pix_fmt','yuv420p','-'],stdout=subprocess.PIPE,check=True).stdout,
                            np.uint8).reshape(-1,H*W*3//2)[:,:H*W].reshape(-1,H,W)
        crop = np.zeros((H,W),bool); crop[17:17+72,33:33+128] = True
        self.assertTrue(np.array_equal(got[:,~crop],src[:,~crop]))
        centre = got[:,17+25:17+47,33+40:33+88].astype(int)-src[:,17+25:17+47,33+40:33+88]
        self.assertGreater(np.abs(centre).mean(),5)


if __name__ == '__main__':
    unittest.main(verbosity=2)
