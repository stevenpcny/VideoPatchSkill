#!/usr/bin/env python3
"""Workflow tools that save agent steps: brief outputs, compact fit proposals, init-merge with pending
matte / unverified timing, and visual review records updated by command. Synthetic media only."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import flow_patch as fp  # noqa: E402
import qc_tools as qc  # noqa: E402


def run(script, *args, ok=True, cwd=None):
    p = subprocess.run([sys.executable, str(ROOT/'scripts'/script), *map(str, args)], cwd=cwd,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8')
    if ok and p.returncode:
        raise AssertionError(p.stdout+p.stderr)
    if not ok and not p.returncode:
        raise AssertionError('Unexpected success: '+p.stdout)
    return p


def encode(path, images, audio=None):
    h, w = images[0].shape[:2]
    cmd = [fp.FFMPEG, '-v', 'error', '-n', '-f', 'rawvideo', '-pix_fmt', 'bgr24', '-s', f'{w}x{h}',
           '-r', '24', '-i', '-']
    if audio:
        cmd += ['-i', str(audio), '-map', '0:v:0', '-map', '1:a:0', '-c:a', 'copy']
    cmd += ['-c:v', 'libx264', '-crf', '10', '-pix_fmt', 'yuv420p', str(path)]
    subprocess.run(cmd, input=b''.join(im.tobytes() for im in images), check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Workflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='flow-patch-workflow-')
        b = cls.base = Path(cls.tmp.name)
        audio = b/'tone.m4a'
        fp.run([fp.FFMPEG, '-v', 'error', '-n', '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000',
                '-t', '2', '-c:a', 'aac', audio])
        rng = np.random.default_rng(7)
        texture = rng.integers(30, 180, (128, 192, 3), dtype=np.uint8)
        frames = []
        for i in range(48):
            im = texture.copy(); im[40:80, 100+i % 20:130+i % 20] = [30, 200, 50]; frames.append(im)
        encode(b/'source.mp4', frames, audio)
        (b/'prepare.json').write_text(json.dumps({
            'schema_version': 1, 'source': 'source.mp4', 'source_size': [192, 128], 'fps': 24,
            'prepare': {'core': [16, 32], 'context_frames': 8, 'crop': [96, 0, 96, 128],
                        'output_size': [96, 128], 'mask_regions': [[16, 24, 48, 64]]}}), encoding='utf-8')
        run('flow_patch.py', 'prepare', '--config', b/'prepare.json', '--output', b/'upload.mp4')
        # A return with a changed face area, full length and in step with the upload.
        ret = []
        for i in range(32):
            im = frames[8+i][:, 96:].copy(); im[30:80, 20:70] = [40, 70, 220]; ret.append(im)
        encode(b/'flow.mp4', ret)
        # Full length but shifted by two frames: sizes and counts pass, timing is wrong.
        encode(b/'flow-shifted.mp4', [frames[min(47, 10+i)][:, 96:].copy() for i in range(32)])
        encode(b/'flow-short.mp4', ret[:20])
        cls.hand = {'name': 'a', 'generated': 'flow.mp4', 'target': [16, 32], 'generated_start': 8,
                    'crop': [96, 0, 96, 128], 'work_size': [96, 128],
                    'source_to_generated_affine': [1, 0, 0, 0, 1, 0],
                    'matte': {'region': [16, 24, 64, 80], 'shape': 'ellipse', 'feather': 8, 'protect': []},
                    'blend': 'alpha', 'fade_frames': 0, 'gain_bgr': [1, 1, 1], 'offset_bgr': [0, 0, 0]}
        cls.write('hand.json', patches=[cls.hand])

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @classmethod
    def write(cls, name, **cfg):
        path = cls.base/name
        path.write_text(json.dumps(dict({'schema_version': 1, 'source': 'source.mp4', 'source_size': [192, 128],
                                         'fps': 24}, **cfg)), encoding='utf-8')
        return path

    def init(self, name, generated='flow.mp4', *extra, ok=True):
        return run('flow_patch.py', 'init-merge', '--prepare-report', self.base/'upload.report.json',
                   '--generated', self.base/generated, '--output', self.base/name, *extra, ok=ok)

    # T1 -------------------------------------------------------------------------------------------
    def test_01_qc_brief_and_json_out(self):
        full = run('qc_tools.py', 'seam', '--config', self.base/'hand.json').stdout
        out = self.base/'seam-full.json'
        brief = run('qc_tools.py', 'seam', '--config', self.base/'hand.json', '--brief', '--json-out', out)
        self.assertEqual(out.read_text(encoding='utf-8'), full)
        self.assertLess(len(brief.stdout.encode()), 1024)
        b = json.loads(brief.stdout)
        self.assertEqual(b['frames'], {'omitted_items': 16})
        self.assertEqual(b['summary'], json.loads(full)['summary'])
        self.assertEqual(b['worst_frames'], json.loads(full)['worst_frames'])
        run('qc_tools.py', 'seam', '--config', self.base/'hand.json', '--json-out', out, ok=False)
        pre = json.loads(run('qc_tools.py', 'precheck', '--config', self.base/'hand.json', '--brief').stdout)
        self.assertEqual(pre['samples'], {'omitted_items': 8})
        self.assertIn('provisional_call', pre)
        self.assertIn('manual_checks_remaining', pre)

    def test_02_dry_run_brief_and_json_out(self):
        cfg = self.base/'hand.json'
        full = run('flow_patch.py', 'merge', '--config', cfg, '--output', self.base/'d.mp4', '--dry-run')
        out = self.base/'dry-full.json'
        brief = run('flow_patch.py', 'merge', '--config', cfg, '--output', self.base/'d.mp4', '--dry-run',
                    '--brief', '--json-out', out)
        self.assertEqual(out.read_text(encoding='utf-8'), full.stdout)
        self.assertEqual(brief.stderr, full.stderr)  # NOTICE lines are never shortened
        self.assertLess(len(brief.stdout.encode()), 1024)
        row = json.loads(brief.stdout)['patches'][0]
        self.assertEqual((row['mapped'], row['matte'], row['timing']), (16, 'set', 'not declared'))
        run('flow_patch.py', 'merge', '--config', cfg, '--output', self.base/'d.mp4', '--brief', ok=False)

    # T2 -------------------------------------------------------------------------------------------
    def test_03_fit_compact_proposal_and_summary(self):
        p = dict(self.hand, source_to_generated_affine=[1, 0, 1.5, 0, 1, -1])
        cfg = self.write('fit-in.json', patches=[p])
        out = self.base/'fit-out.json'
        res = run('fit_patch.py', '--config', cfg, '--patch', 0, '--output', out)
        lines = res.stdout.strip().splitlines()
        self.assertTrue(lines[0].startswith('Proposal written:'))
        summary = json.loads(lines[1])
        for key in ('frames_registered', 'correlation_min', 'max_shift_from_initial_px', 'color'):
            self.assertIn(key, summary)
        self.assertEqual(summary['frames_registered'], 16)
        text = out.read_text(encoding='utf-8'); obj = json.loads(text)
        self.assertLess(len(text), len(json.dumps(obj, ensure_ascii=False, indent=2)))
        # Same object, so the same composite as the fully indented layout.
        indented = self.base/'fit-indented.json'
        indented.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')
        run('flow_patch.py', 'merge', '--config', out, '--output', self.base/'fit-a.mp4')
        run('flow_patch.py', 'merge', '--config', indented, '--output', self.base/'fit-b.mp4')
        self.assertEqual(sha(self.base/'fit-a.mp4'), sha(self.base/'fit-b.mp4'))

    # C1 -------------------------------------------------------------------------------------------
    def test_04_init_merge_copies_fields_and_matches_hand_config(self):
        self.init('im.json', 'flow.mp4', '--name', 'a', '--matte', 'ellipse', '16', '24', '64', '80',
                  '--feather', '8')
        cfg = json.loads((self.base/'im.json').read_text(encoding='utf-8'))
        p = cfg['patches'][0]
        self.assertEqual(cfg['source'], 'source.mp4')
        self.assertEqual(p['generated'], 'flow.mp4')
        for key in ('target', 'generated_start', 'crop', 'work_size', 'matte'):
            self.assertEqual(p[key], self.hand[key], key)
        self.assertEqual(p['timing']['status'], 'unverified')
        run('flow_patch.py', 'mark-timing', '--config', self.base/'im.json', '--basis', 'test: same frames')
        run('flow_patch.py', 'merge', '--config', self.base/'im.json', '--output', self.base/'im.mp4')
        run('flow_patch.py', 'merge', '--config', self.base/'hand.json', '--output', self.base/'hand.mp4')
        self.assertEqual(sha(self.base/'im.mp4'), sha(self.base/'hand.mp4'))
        rep = json.loads((self.base/'hand.report.json').read_text(encoding='utf-8'))
        self.assertNotIn('timing', rep['scope'])  # hand-written configs behave as before
        self.assertEqual(json.loads((self.base/'im.report.json').read_text(encoding='utf-8'))
                         ['scope']['timing'], {'a': 'verified'})

    def test_05_pending_matte_blocks_render_and_qc(self):
        self.init('pend.json')
        cfg = self.base/'pend.json'
        self.assertIn('pending', json.loads(cfg.read_text(encoding='utf-8'))['patches'][0]['matte'])
        for args in (['flow_patch.py', 'merge', '--config', cfg, '--output', self.base/'p1.mp4'],
                     ['flow_patch.py', 'merge', '--config', cfg, '--output', self.base/'p2.mp4', '--frames', '16:20'],
                     ['flow_patch.py', 'merge', '--config', cfg, '--output', self.base/'p3.mp4', '--preview'],
                     ['qc_tools.py', 'seam', '--config', cfg], ['qc_tools.py', 'precheck', '--config', cfg],
                     ['fit_patch.py', '--config', cfg, '--output', self.base/'p4.json']):
            p = run(*args, ok=False)
            self.assertIn('atte pending', p.stderr)
        self.assertFalse(any((self.base/n).exists() for n in ('p1.mp4', 'p2.mp4', 'p3.mp4', 'p4.json')))
        row = json.loads(run('flow_patch.py', 'merge', '--config', cfg, '--output', self.base/'p.mp4',
                             '--dry-run', '--brief').stdout)['patches'][0]
        self.assertEqual((row['matte'], row['timing']), ('pending', 'unverified'))

    def test_06_unverified_timing_blocks_only_full_export(self):
        self.init('t.json', 'flow-shifted.mp4', '--matte', 'rect', '16', '24', '64', '80')
        cfg = self.base/'t.json'
        p = run('flow_patch.py', 'merge', '--config', cfg, '--output', self.base/'t-full.mp4', ok=False)
        self.assertIn('Timing unverified', p.stderr)
        self.assertFalse((self.base/'t-full.mp4').exists())
        chk = run('flow_patch.py', 'merge', '--config', cfg, '--output', self.base/'t-chk.mp4', '--frames', '16:24')
        self.assertIn('Timing unverified', chk.stderr)
        scope = json.loads((self.base/'t-chk.report.json').read_text(encoding='utf-8'))['scope']
        self.assertEqual((scope['deliverable'], list(scope['timing'].values())), (False, ['unverified']))
        run('flow_patch.py', 'mark-timing', '--config', cfg, '--basis', ' ', ok=False)
        run('flow_patch.py', 'mark-timing', '--config', cfg, '--basis', 'test basis')
        # Changing the mapping puts it back to unverified.
        data = json.loads(cfg.read_text(encoding='utf-8')); data['patches'][0]['generated_start'] = 10
        cfg.write_text(json.dumps(data), encoding='utf-8')
        self.assertIn('changed since', run('flow_patch.py', 'merge', '--config', cfg, '--output',
                                           self.base/'t2.mp4', ok=False).stderr)
        run('flow_patch.py', 'mark-timing', '--config', cfg, '--basis', 'checked offset 2')
        run('flow_patch.py', 'merge', '--config', cfg, '--output', self.base/'t3.mp4')
        # Replacing the return file does too.
        shutil.copy(self.base/'flow-shifted.mp4', self.base/'flow-shifted.bak')
        shutil.copy(self.base/'flow.mp4', self.base/'flow-shifted.mp4')
        try:
            run('flow_patch.py', 'merge', '--config', cfg, '--output', self.base/'t4.mp4', ok=False)
        finally:
            shutil.move(self.base/'flow-shifted.bak', self.base/'flow-shifted.mp4')

    def test_07_init_merge_refusals_and_append(self):
        p = self.init('short.json', 'flow-short.mp4', ok=False)
        self.assertIn('frame_map', p.stderr)
        self.assertFalse((self.base/'short.json').exists())
        self.init('app.json', 'flow.mp4', '--name', 'a')
        self.init('app.json', ok=False)  # exists without --append
        self.init('app.json', 'flow.mp4', '--name', 'a', '--append', ok=False)  # duplicate name
        self.init('app.json', 'flow.mp4', '--name', 'b', '--append')
        self.assertEqual([q['name'] for q in json.loads((self.base/'app.json').read_text(encoding='utf-8'))
                          ['patches']], ['a', 'b'])
        self.init('bad-matte.json', 'flow.mp4', '--matte', 'circle', '1', '1', '9', '9', ok=False)

    # E4 -------------------------------------------------------------------------------------------
    def review_setup(self, name):
        cfg = self.write(name+'.json', patches=[self.hand])
        run('flow_patch.py', 'merge', '--config', cfg, '--output', self.base/(name+'.mp4'))
        ev = self.base/(name+'-evidence.png'); ev.write_bytes(b'sheet')
        return self.base/(name+'.visual-review.json'), ev

    def set(self, record, check, status, **kw):
        a = argparse.Namespace(record=str(record), check=check, status=status, intervals=kw.get('intervals'),
                               evidence=kw.get('evidence'), reviewer=kw.get('reviewer', 'agent'),
                               confirmation=kw.get('confirmation'), note=kw.get('note'))
        return qc.cmd_review_set(a)

    def status(self, record):
        return qc.cmd_review_status(argparse.Namespace(record=str(record)))

    def all_pass(self, record, ev):
        """Positive control: every check reviewed with evidence and full coverage."""
        coverage = qc.review_template()['coverage_requirements']
        for c in json.loads(record.read_text(encoding='utf-8'))['visual_checks']:
            if c['status'] == 'pass':
                continue
            if c['id'] in coverage['join_windows']:
                self.set(record, c['id'], 'pass', evidence=[ev], intervals=['4:28', '20:44'])
            elif c['id'] in coverage['repair_intervals']:
                self.set(record, c['id'], 'pass', evidence=[ev], intervals=['16:32'])
            else:
                self.set(record, c['id'], 'not_applicable', note='not in this synthetic shot')
        return self.status(record)

    def test_08_review_set_refusals(self):
        record, ev = self.review_setup('rv1')
        with self.assertRaisesRegex(ValueError, 'Unknown check'):
            self.set(record, 'bogus', 'pass', evidence=[ev])
        with self.assertRaisesRegex(ValueError, 'evidence'):
            self.set(record, 'all_repair_frames_contact_sheets', 'pass')
        with self.assertRaisesRegex(ValueError, 'note'):
            self.set(record, 'native_player_color', 'not_applicable')
        with self.assertRaisesRegex(ValueError, 'confirmation'):
            self.set(record, 'full_scene_and_closeup_normal_speed_playback', 'pass', evidence=[ev],
                     intervals=['16:32'], reviewer='user')
        with self.assertRaisesRegex(ValueError, 'outside'):
            self.set(record, 'all_repair_frames_contact_sheets', 'pass', evidence=[ev], intervals=['16:99'])
        data = json.loads(record.read_text(encoding='utf-8')); data['output_sha256'] = '0'*64
        other = record.with_name('rv1-other.visual-review.json'); other.write_text(json.dumps(data), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'SHA-256 differs'):
            self.set(other, 'all_repair_frames_contact_sheets', 'pass', evidence=[ev], intervals=['16:32'])

    def test_09_overall_status_rules(self):
        record, ev = self.review_setup('rv2')
        self.assertEqual(self.status(record)['overall_visual_status'], 'pending_visual_review')
        r = self.set(record, 'expression_and_gaze_unchanged', 'fail', evidence=[ev], note='blink changed')
        self.assertEqual(r['overall_visual_status'], 'visual_fail')  # fail outranks the not_run ones
        self.set(record, 'expression_and_gaze_unchanged', 'not_run')
        result = self.all_pass(record, ev)
        self.assertEqual(result, {'record': record.name, 'overall_visual_status': 'visual_pass', 'blocking': []})
        self.assertEqual(json.loads(record.read_text(encoding='utf-8'))['overall_visual_status'], 'visual_pass')
        # User review with a recorded confirmation still passes.
        self.set(record, 'full_scene_and_closeup_normal_speed_playback', 'pass', evidence=[ev], intervals=['0:48'],
                 reviewer='user', confirmation='"looks fine at normal speed" (user, chat, 2026-10-31 16:02)')
        self.assertEqual(self.status(record)['overall_visual_status'], 'visual_pass')

    def test_10_visual_pass_gates(self):
        record, ev = self.review_setup('rv3')
        self.assertEqual(self.all_pass(record, ev)['overall_visual_status'], 'visual_pass')
        good = record.read_text(encoding='utf-8')

        def variant(tag, change, expect='pending_visual_review'):
            data = json.loads(good); change(data)
            path = record.with_name(f'rv3-{tag}.visual-review.json')
            path.write_text(json.dumps(data), encoding='utf-8')
            res = self.status(path)
            self.assertEqual(res['overall_visual_status'], expect, (tag, res))
            return res

        def check(data, cid):
            return next(c for c in data['visual_checks'] if c['id'] == cid)
        variant('technical', lambda d: d['technical_validation'].update(status='not_run'))
        variant('missing', lambda d: d['visual_checks'].pop())
        variant('unknown', lambda d: d['visual_checks'].append(dict(d['visual_checks'][0], id='extra')))
        variant('na-reason', lambda d: check(d, 'native_player_color').update(notes=''))
        variant('user-unconfirmed', lambda d: check(d, 'all_repair_frames_contact_sheets').update(reviewer='user'))
        res = variant('coverage', lambda d: check(d, 'all_repair_frames_contact_sheets').update(
            source_intervals_checked=[[16, 24]]))
        self.assertIn('[24, 32]', ' '.join(res['blocking']))
        variant('join', lambda d: check(d, 'ghosting_and_join_windows_frame_by_frame').update(
            source_intervals_checked=[[16, 32]]))
        variant('no-evidence', lambda d: check(d, 'all_repair_frames_contact_sheets').update(evidence=[]))
        variant('report-hash', lambda d: d.update(technical_report='rv3.report-edited.json'))
        rep = json.loads((self.base/'rv3.report.json').read_text(encoding='utf-8'))
        rep['output_sha256'] = '1'*64
        (self.base/'rv3.report-edited.json').write_text(json.dumps(rep), encoding='utf-8')
        variant('fail-and-not-run', lambda d: (check(d, 'native_player_color').update(status='not_run'),
                                               check(d, 'same_scene_sharpness').update(status='fail')),
                expect='visual_fail')
        # Evidence changed or deleted after it was recorded.
        ev.write_bytes(b'other')
        self.assertIn('changed', ' '.join(self.status(record)['blocking']))
        ev.unlink()
        self.assertIn('missing evidence', ' '.join(self.status(record)['blocking']))

    def test_11_check_export_record_never_passes(self):
        cfg = self.base/'hand.json'
        run('flow_patch.py', 'merge', '--config', cfg, '--output', self.base/'pv.mp4', '--preview')
        self.assertFalse((self.base/'pv.visual-review.json').exists())
        # Hand-made record for the preview, with every check filled in.
        record, ev = self.review_setup('rv4')
        self.all_pass(record, ev)
        data = json.loads(record.read_text(encoding='utf-8'))
        data.update(output='pv.mp4', output_sha256=sha(self.base/'pv.mp4'), technical_report='pv.report.json')
        fake = self.base/'pv.visual-review.json'; fake.write_text(json.dumps(data), encoding='utf-8')
        res = self.status(fake)
        self.assertEqual(res['overall_visual_status'], 'pending_visual_review')
        self.assertIn('not a deliverable', ' '.join(res['blocking']))


if __name__ == '__main__':
    unittest.main(verbosity=2)
