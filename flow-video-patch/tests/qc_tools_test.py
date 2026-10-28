#!/usr/bin/env python3
"""qc_tools behavioral checks with disposable synthetic media only."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
sys.path.insert(0, str(ROOT/'tests'))
from smoke_test import encode  # noqa: E402


def qc(*args):
    p = subprocess.run([sys.executable, str(ROOT/'scripts/qc_tools.py'), *map(str, args)],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
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
        encode(cls.up, up); encode(cls.ret, ret)

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

    def test_sharpness_reports_per_frame(self):
        r = qc('sharpness', '--video', self.up, '--region', 0, 0, 160, 100)
        self.assertEqual(len(r['per_frame']), 30); self.assertGreater(r['median'], 0)


if __name__ == '__main__':
    unittest.main()
