#!/usr/bin/env python3
"""remote_run.py against a stand-in ssh that runs the worker side locally (no network)."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'tests'))
from smoke_test import encode  # noqa: E402

import numpy as np  # noqa: E402


class RemoteRun(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='flow-patch-remote-')
        cls.base = Path(cls.tmp.name)
        cls.worker = cls.base/'worker tasks'; cls.worker.mkdir()
        cls.profile = cls.base/'profile.json'
        python = 'python' if shutil.which('python') else 'python3'
        cls.profile.write_text(json.dumps({'ssh_alias': 'worker', 'task_root': str(cls.worker),
                                           'skill_dir': str(ROOT), 'python': python}), encoding='utf-8')
        cls.clip = cls.base/'原片 clip.mp4'
        encode(cls.clip, [np.full((64, 96, 3), 40+i*5, np.uint8) for i in range(12)])
        cls.env = dict(os.environ, FLOW_WORKER_PROFILE=str(cls.profile),
                       FLOW_SSH=json.dumps([sys.executable, str(ROOT/'tests'/'fake_ssh.py')]))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def remote(self, *args, ok=True):
        p = subprocess.run([sys.executable, str(ROOT/'scripts'/'remote_run.py'), *map(str, args)],
                           env=self.env, capture_output=True, text=True, encoding='utf-8')
        if ok:
            self.assertEqual(p.returncode, 0, p.stdout+p.stderr)
        else:
            self.assertNotEqual(p.returncode, 0, p.stdout)
        return p

    def test_round_trip(self):
        self.assertIn('PASS', self.remote('check').stdout)
        self.assertIn('pushed 原片 clip.mp4', self.remote('push', 'job 1', self.clip).stdout)
        self.assertEqual((self.worker/'job 1'/self.clip.name).read_bytes(), self.clip.read_bytes())
        self.remote('push', 'job 1', self.clip, ok=False)  # never overwrites on the worker
        out = self.remote('run', 'job 1', 'flow_patch', 'inspect', '--source', self.clip.name).stdout
        self.assertEqual(json.loads(out)['frames'], 12)
        back = self.base/'back'; back.mkdir()
        self.assertIn('pulled', self.remote('pull', 'job 1', self.clip.name, '--dest', back).stdout)
        self.assertEqual((back/self.clip.name).read_bytes(), self.clip.read_bytes())
        self.remote('pull', 'job 1', self.clip.name, '--dest', back, ok=False)  # never overwrites locally

    def test_refusals(self):
        for args in (('run', 'job 2', 'rm', 'x'), ('run', 'job 2', 'flow_patch', 'inspect', '--source', '../x.mp4'),
                     ('run', 'job 2', 'flow_patch', 'inspect', '--source', str(self.clip)),
                     ('push', '..', self.clip), ('push', 'a/b', self.clip), ('pull', 'job 2', '../profile.json')):
            self.assertIn('', self.remote(*args, ok=False).stderr)
        self.assertFalse((self.base/'x.mp4').exists())
        bad = self.base/'bad-profile.json'
        bad.write_text(json.dumps({'ssh_alias': 'worker; rm -rf ~', 'task_root': 'x', 'skill_dir': 'y'}), encoding='utf-8')
        p = subprocess.run([sys.executable, str(ROOT/'scripts'/'remote_run.py'), 'check'],
                           env=dict(self.env, FLOW_WORKER_PROFILE=str(bad)), capture_output=True, text=True,
                           encoding='utf-8')
        self.assertIn('ssh_alias', p.stderr)


if __name__ == '__main__':
    unittest.main(verbosity=2)
