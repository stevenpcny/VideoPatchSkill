#!/usr/bin/env python3
"""Security regressions: synthetic local fixtures; no external network."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import flow_patch as fp


class Security(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='flow-security-')
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_remote_and_device_inputs_rejected(self):
        for path in ('https://example.invalid/clip.mp4', '/dev/null', self.base):
            with self.assertRaises(ValueError):
                fp.probe(path)

    def test_nested_network_protocol_rejected(self):
        media = self.base/'remote.ffconcat'
        media.write_text('ffconcat version 1.0\nfile http://127.0.0.1:9/never-fetch.ts\n',encoding='utf-8')
        with self.assertRaises(subprocess.CalledProcessError) as ctx:
            fp.probe(media)
        # FFmpeg concat's safe-name policy or our protocol policy must reject it.
        self.assertTrue(any(x in ctx.exception.stderr for x in
                            (b'not on whitelist', b'Unsafe file name')))

    def test_option_like_filename_is_absolute(self):
        media = self.base/'-version'
        media.write_bytes(b'not a video')
        self.assertEqual(fp.file_input(media)[-1], str(media.resolve()))

    def test_symlinks_and_existing_outputs_refused(self):
        target = self.base/'missing.json'
        link = self.base/'link.json'
        link.symlink_to(target)
        with self.assertRaises(ValueError):
            with fp.private_output(link):
                self.fail('Opened symlink')
        self.assertFalse(target.exists())
        target.write_text('original',encoding='utf-8')
        with self.assertRaises(FileExistsError):
            with fp.private_output(target):
                self.fail('Replaced original')
        self.assertEqual(target.read_text(encoding='utf-8'), 'original')

    def test_private_permissions_and_failure_cleanup(self):
        dest = self.base/'report.json'
        with fp.private_output(dest) as stream:
            json.dump({'status': 'synthetic'}, stream)
        if os.name == 'posix':
            self.assertEqual(dest.stat().st_mode & 0o777, 0o600)
        bad = self.base/'failed.json'
        with self.assertRaises(TypeError):
            with fp.private_output(bad) as stream:
                json.dump({'bad': object()}, stream)
        self.assertFalse(bad.exists())

    def test_report_collision_rolls_back_only_new_video(self):
        video, report = self.base/'out.mp4', self.base/'out.report.json'
        report.write_text('original report',encoding='utf-8')
        with self.assertRaises(FileExistsError):
            with fp.private_output(video, binary=True), fp.private_output(report):
                pass
        self.assertFalse(video.exists())
        self.assertEqual(report.read_text(encoding='utf-8'), 'original report')

    @unittest.skipIf(os.name == 'nt', 'Windows refuses to rename a file that is still open, '
                     'so the replacement race simulated here cannot happen there')
    def test_replacement_file_survives_failure_cleanup(self):
        dest = self.base/'race.json'
        with self.assertRaises(RuntimeError):
            with fp.private_output(dest):
                dest.rename(self.base/'first.json')
                dest.write_text('other file',encoding='utf-8')
                raise RuntimeError('simulated write error')
        self.assertEqual(dest.read_text(encoding='utf-8'), 'other file')

    def test_pixel_and_config_budgets(self):
        fp.checked_size([7680,4320])
        for size in ([2**30,2**30], [0,32], [1.5,32], [True,32]):
            with self.assertRaises(ValueError):
                fp.checked_size(size)
        large = self.base/'oversize.json'
        with large.open('wb') as stream:
            stream.truncate(16*1024*1024+1)
        with self.assertRaises(ValueError):
            fp.load(large)
        with self.assertRaises(ValueError):
            fp.load(Path('/dev/null'))

    def test_command_timeout(self):
        with patch.dict(os.environ, {'FLOW_COMMAND_TIMEOUT': '0.05'}):
            with self.assertRaises(subprocess.TimeoutExpired):
                fp.run([sys.executable, '-c', 'import time; time.sleep(5)'])

    def test_encode_threads_bounded(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('FLOW_ENCODE_THREADS', None)
            self.assertEqual(fp.encode_threads(), '4')
        for good in ('0', '32'):
            with patch.dict(os.environ, {'FLOW_ENCODE_THREADS': good}):
                self.assertEqual(fp.encode_threads(), good)
        for bad in ('-1', '65', '4 -y', 'auto', ''):
            with patch.dict(os.environ, {'FLOW_ENCODE_THREADS': bad}):
                with self.assertRaises(ValueError):
                    fp.encode_threads()

    def test_x264_settings_bounded(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('FLOW_CRF', None); os.environ.pop('FLOW_X264_PRESET', None)
            self.assertEqual(fp.x264_settings(), ('17', 'fast'))
        with patch.dict(os.environ, {'FLOW_CRF': '12', 'FLOW_X264_PRESET': 'slow'}):
            self.assertEqual(fp.x264_settings(), ('12', 'slow'))
        for env in ({'FLOW_CRF': '52'}, {'FLOW_CRF': '17 -y'}, {'FLOW_CRF': ''},
                    {'FLOW_X264_PRESET': 'placebo'}, {'FLOW_X264_PRESET': 'fast -y'}):
            with patch.dict(os.environ, env):
                with self.assertRaises(ValueError):
                    fp.x264_settings()

    def test_segments_bounded(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('FLOW_SEGMENTS', None)
            self.assertEqual(fp.segment_count(1000), 1)
        with patch.dict(os.environ, {'FLOW_SEGMENTS': '8'}):
            self.assertEqual(fp.segment_count(1000), 8)
            self.assertEqual(fp.segment_count(30), 2)  # at least 12 frames per segment
        for bad in ('0', '33', '-2', '2 -y', 'auto', ''):
            with patch.dict(os.environ, {'FLOW_SEGMENTS': bad}):
                with self.assertRaises(ValueError):
                    fp.segment_count(1000)

    def test_fit_rejects_negative_index_and_nonfinite_parameters(self):
        script = Path(fp.__file__).with_name('fit_patch.py')
        for args in (['--patch', '-1'], ['--max-shift', 'inf'], ['--min-correlation', 'nan']):
            result = subprocess.run([sys.executable, str(script), '--config', str(self.base/'absent'),
                                     '--output', str(self.base/'out.json'), *args],
                                    capture_output=True, text=True,encoding='utf-8', timeout=30)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((self.base/'out.json').exists())
            self.assertNotIn('FileNotFoundError', result.stderr)


if __name__ == '__main__':
    unittest.main(verbosity=2)
