#!/usr/bin/env python3
"""qc_tools behavioral checks with disposable synthetic media only."""
import argparse
import collections
import contextlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
sys.path.insert(0, str(ROOT/'tests'))
from smoke_test import encode  # noqa: E402


def qc(*args):
    p = subprocess.run([sys.executable, str(ROOT/'scripts/qc_tools.py'), *map(str, args)],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,encoding='utf-8')
    if p.returncode:
        raise AssertionError(p.stdout + p.stderr)
    return json.loads(p.stdout)


class QcTools(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='flow-patch-qc-')
        base = Path(cls.tmp.name); rng = np.random.default_rng(7)
        bg = rng.integers(20, 120, (160, 160, 3), dtype=np.uint8)
        blob = rng.integers(120, 255, (24, 24, 3), dtype=np.uint8)
        def frame(i):
            im = bg.copy(); x = 20 + 3*i; y = 40 + (i % 5)
            im[y:y+24, x:x+24] = blob                       # moving "head"
            im[120:150, 10 + (i*7) % 100:40 + (i*7) % 100] = 230  # moving "hand", never masked
            return im
        up = [frame(i) for i in range(30)]
        ret = [frame(i + 2) for i in range(28)]             # returned clip starts 2 frames later
        for i in range(5, 9):
            ret[i][10:60, 60:120] = [0, 255, 0]             # residual BGR green block
        cls.up, cls.ret = base/'up.mp4', base/'ret.mp4'
        encode(cls.up, up); encode(cls.ret, ret); cls.frames = up

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_green_runs(self):
        self.assertEqual(qc('green', '--video', self.ret, '--min-pixels', 500)['mask_color_runs'], [[5, 9]])
        self.assertEqual(qc('green', '--video', self.up, '--min-pixels', 500)['mask_color_runs'], [])

    def test_offset_scan_finds_constant_shift(self):
        r = qc('offset', '--upload', self.up, '--returned', self.ret, '--region', 0, 110, 160, 50)
        self.assertEqual({v['best_offset'] for v in r['offset_scan'].values()}, {2})
        self.assertEqual((r['upload_frames'], r['returned_frames']), (30, 28))

    def test_track_follows_moving_box(self):
        b = qc('track', '--video', self.up, '--init', 20, 40, 24, 24, '--search', 12)['boxes']
        self.assertTrue(all(abs(x - (20 + 3*i)) <= 1 and abs(y - (40 + i % 5)) <= 1 for i, x, y, *_ in b))

    def test_seam_ranks_matching_candidate_lower(self):
        base = Path(self.tmp.name); rng = np.random.default_rng(3)
        noisy = base/'noisy.mp4'
        encode(noisy, [np.clip(fp_image + rng.normal(0, 25, fp_image.shape), 0, 255).astype(np.uint8)
                       for fp_image in self.frames])
        scores = {}
        for name, gen in (('same', self.up), ('noisy', noisy)):
            cfg = base/f'seam-{name}.json'
            cfg.write_text(json.dumps(dict(schema_version=1, source=str(self.up), source_size=[160, 160], fps=24,
                patches=[dict(generated=str(gen), target=[0, 30], crop=[0, 0, 160, 160], work_size=[160, 160],
                              matte=dict(region=[30, 30, 100, 100], feather=20))])), encoding='utf-8')
            r = qc('seam', '--config', cfg, '--step', 5)
            self.assertEqual(r['frames_measured'], 6)
            scores[name] = r['summary']['high_freq_weighted_mean']['mean']
        self.assertLess(scores['same'], .01)
        self.assertGreater(scores['noisy'], 5)

    def test_precheck_calls(self):
        base = Path(self.tmp.name)
        zoomed = base/'zoomed.mp4'  # a return that drifted in scale, like an unlocked generation
        encode(zoomed, [cv2.warpAffine(f, cv2.getRotationMatrix2D((80, 80), 0, 0.92 + 0.004*(i % 5)), (160, 160))
                        for i, f in enumerate(self.frames)])
        calls = {}
        for name, gen in (('same', self.up), ('zoomed', zoomed)):
            cfg = base/f'pre-{name}.json'
            cfg.write_text(json.dumps(dict(schema_version=1, source=str(self.up), source_size=[160, 160], fps=24,
                patches=[dict(generated=str(gen), target=[0, 30], crop=[0, 0, 160, 160], work_size=[160, 160],
                              matte=dict(region=[50, 30, 60, 60], feather=8))])), encoding='utf-8')
            r = qc('precheck', '--config', cfg, '--samples', 4)
            self.assertEqual(r['summary']['frames_sampled'], 4)
            self.assertIn('blinks and gaze frame by frame', r['manual_checks_remaining'])
            calls[name] = r['provisional_call']
        self.assertEqual(calls['same'], 'fits')
        self.assertNotEqual(calls['zoomed'], 'fits')

    def test_sharpness_reports_per_frame(self):
        r = qc('sharpness', '--video', self.up, '--region', 0, 0, 160, 100)
        self.assertEqual(len(r['per_frame']), 30); self.assertGreater(r['median'], 0)

    def counting(self, fp):
        """Count full decoding passes per file name until the test ends."""
        counts = collections.Counter(); original = fp.decode_pass
        def counted(path, strict=False):
            counts[(Path(path).name, strict)] += 1
            return original(path, strict)
        fp.decode_pass = counted
        self.addCleanup(setattr, fp, 'decode_pass', original)
        return counts

    def test_precheck_decodes_each_input_once_with_identical_result(self):
        import flow_patch as fp
        import qc_tools
        base = Path(self.tmp.name); gen = base/'shared-gen.mp4'
        shutil.copyfile(self.up, gen)  # a distinct file, so source and return are counted apart
        cfg = base/'shared.json'
        cfg.write_text(json.dumps(dict(schema_version=1, source=str(self.up), source_size=[160, 160], fps=24,
            patches=[dict(generated=str(gen), target=[0, 30], crop=[0, 0, 160, 160], work_size=[160, 160],
                          matte=dict(region=[50, 30, 60, 60], feather=8))])), encoding='utf-8')
        a = argparse.Namespace(config=str(cfg), patch=0, samples=4)
        counts = self.counting(fp)
        readers = []; reader = fp.Reader
        def counted_reader(path, *args, **kwargs):
            readers.append(Path(path).name); return reader(path, *args, **kwargs)
        self.addCleanup(setattr, fp, 'Reader', reader); fp.Reader = counted_reader
        shared = qc_tools.cmd_precheck(a)
        self.assertEqual(dict(counts), {('up.mp4', False): 1, ('shared-gen.mp4', False): 1})
        self.assertEqual(readers, ['up.mp4', 'shared-gen.mp4'])  # geometry and seam samples in one pass
        self.assertIsNone(fp._SHARED)  # nothing outlives the command
        counts.clear()
        with mock.patch.object(fp, 'shared_probes', contextlib.nullcontext):
            unshared = qc_tools.cmd_precheck(a)  # per-step probing: the return again in merge_plan
        self.assertEqual(dict(counts), {('up.mp4', False): 1, ('shared-gen.mp4', False): 2})
        self.assertEqual(json.dumps(shared, sort_keys=True), json.dumps(unshared, sort_keys=True))
        # The seam inside precheck equals the standalone seam command at the same step.
        seam = qc_tools.cmd_seam(argparse.Namespace(config=str(cfg), patch=0, step=30//4, sigma=3, threshold=12))
        self.assertEqual(shared['seam'], seam['summary'])

    def test_shared_probe_rereads_replaced_file_and_strict_requests(self):
        import flow_patch as fp
        base = Path(self.tmp.name); clip = base/'replaced.mp4'; other = base/'replacement.mp4'
        shutil.copyfile(self.up, clip); encode(other, self.frames[:20])
        counts = self.counting(fp)
        with fp.shared_probes():
            self.assertEqual(fp.probe(clip)['frames'], 30)
            self.assertEqual(fp.probe(clip)['frames'], 30)
            self.assertEqual(counts[('replaced.mp4', False)], 1)
            fp.probe(clip, strict=True)                    # strict checks never reuse a weaker pass
            self.assertEqual(counts[('replaced.mp4', True)], 1)
            os.replace(other, clip)                        # same path, new file
            self.assertEqual(fp.probe(clip)['frames'], 20)
            self.assertEqual(counts[('replaced.mp4', False)], 2)
            fp.probe(clip)['frames'] = -1                  # callers get copies
            self.assertEqual(fp.probe(clip)['frames'], 20)
        fp.probe(clip)                                     # outside a scope: decoded again
        self.assertEqual(counts[('replaced.mp4', False)], 3)


if __name__ == '__main__':
    unittest.main()
