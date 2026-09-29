# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Small synthetic Radiance scenes and process groups owned only by the test.

No confidential scenes or user renders are used.
"""
import array
import json
import math
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
from core import engine as engine
from core import render_parallel as P
from core import render_progress as render_progress
from core import processes as processes

BIN = Path(engine.find_radiance() or '/radiance-unavailable')


class ParallelTest(unittest.TestCase):
    def setUp(self):
        (ROOT / 'tests/.tmp').mkdir(exist_ok=True)
        # Retain Unicode and spaces to exercise native tool path handling.
        self.tmp = tempfile.TemporaryDirectory(prefix='parallel space café Ω ', dir=ROOT / 'tests/.tmp')
        self.work = Path(self.tmp.name)
        self.env = os.environ.copy()
        self.env.update(PATH=str(BIN) + os.pathsep + self.env.get('PATH', ''),
                        RAYPATH='.:' + str(BIN.parent / 'lib'), TMPDIR=str(self.work))
        self.addCleanup(self.tmp.cleanup)

    def test_nested_cpu_quota_and_parent_limit_are_respected(self):
        groups = [self.work / 'root', self.work / 'root/a', self.work / 'root/a/b']
        for group, quota in zip(groups, ('max 100000', '350000 100000', '200000 100000')):
            group.mkdir(parents=True, exist_ok=True)
            (group / 'cpu.max').write_text(quota)
        with patch.object(P.os, 'sched_getaffinity', return_value=set(range(8))), \
             patch.object(P.render_resources, 'cgroup_paths', return_value=groups):
            self.assertEqual(P._cpu_count(), 2)
            (groups[1] / 'cpu.max').write_text('150000 100000')
            self.assertEqual(P._cpu_count(), 1)
            (groups[1] / 'cpu.max').write_text('max 100000')
            (groups[2] / 'cpu.max').write_text('broken')
            self.assertEqual(P._cpu_count(), 8)

    def plan(self, workers=2):
        return {'engine': 'rpiece', 'workers': workers, 'xdiv': 2, 'ydiv': 2, 'total_tiles': 4,
                'width': 64, 'height': 64, 'rpiece': str(BIN / 'rpiece')}

    def test_plan_cpu_memory_small_image_and_custom_argument_checks(self):
        response = subprocess.CompletedProcess([], 0, '-x 512 -y 512\n', '')
        octree = self.work / 'scene.oct'; octree.write_bytes(b'abc')
        with patch.object(P, '_cpu_count', return_value=8), patch.object(P, '_available_memory', return_value=4 << 30), \
                patch.object(P.shutil, 'which', return_value='/local/rpiece'), patch.object(processes, 'run', return_value=response):
            plan = P.plan_render(512, 512, 'draft', octree, args=['-ab', '2'], env={})
            self.assertEqual(plan['workers'], 6)
            self.assertEqual(512 % plan['xdiv'], 0); self.assertEqual(512 % plan['ydiv'], 0)
            self.assertEqual(P.plan_render(512, 512, 'draft', octree, env={'CTLUX_RENDER_WORKERS': '1'})['engine'], 'rpict')
            self.assertEqual(P.plan_render(512, 512, 'draft', octree, env={'CTLUX_RENDER_WORKERS': '16'})['workers'], 6)
            self.assertEqual(P.plan_render(32, 32, 'final', octree)['engine'], 'rpict')
            self.assertEqual(P.plan_render(512, 512, 'preview', octree)['engine'], 'rpiece')
            for arg in ('-x', '-y', '-o', '-F', '-R', '-t', '-e', '-af', '-S', '-unknown'):
                with self.subTest(arg=arg):
                    self.assertEqual(P.plan_render(512, 512, 'draft', octree, args=[arg, 'foo'])['engine'], 'rpict')
            with patch.object(P, '_available_memory', return_value=128 << 20):
                self.assertEqual(P.plan_render(512, 512, 'draft', octree)['engine'], 'rpict')
            with patch.object(P, '_cpu_count', return_value=1):
                self.assertEqual(P.plan_render(512, 512, 'draft', octree)['engine'], 'rpict')
        self.assertIsNone(P._divisor_grid(503, 509, 6))

    def test_preview_size_and_safety_checks_preserve_quality(self):
        octree = self.work / 'scene.oct'; octree.write_bytes(b'abc')
        args = shlex.split('-vtv -vh 50 -vv 37.5 ' + engine.QUALITY['preview'])
        original = list(args)
        response = subprocess.CompletedProcess([], 0, '-x 320 -y 233\n', '')
        with patch.object(P, '_cpu_count', return_value=8), \
                patch.object(P, '_available_memory', return_value=4 << 30), \
                patch.object(P.shutil, 'which', return_value='/local/rpiece'), \
                patch.object(processes, 'run', return_value=response) as native:
            plan = P.plan_render(320, 240, 'preview', octree, args=args, env={})
            self.assertEqual((plan['engine'], plan['workers']), ('rpiece', 6))
            self.assertEqual((plan['width'], plan['height']), (320, 233))
            self.assertEqual(320 % plan['xdiv'], 0); self.assertEqual(233 % plan['ydiv'], 0)
            self.assertEqual(args, original)
            native.reset_mock()
            for width, height in ((160, 120), (255, 256)):
                with self.subTest(width=width, height=height):
                    self.assertEqual(P.plan_render(width, height, 'preview', octree, args=args, env={})['engine'], 'rpict')
            self.assertEqual(P.plan_render(320, 240, 'preview', octree, args=args,
                                     env={'CTLUX_RENDER_WORKERS': '1'})['engine'], 'rpict')
            for missing in ('rpiece', 'vwrays'):
                with self.subTest(missing=missing), patch.object(P.shutil, 'which',
                        side_effect=lambda name, path=None: None if name == missing else '/local/' + name):
                    self.assertEqual(P.plan_render(320, 240, 'preview', octree, args=args, env={})['engine'], 'rpict')
            for option in ('-F', '-o', '-unknown'):
                with self.subTest(option=option):
                    self.assertEqual(P.plan_render(320, 240, 'preview', octree,
                        args=args + [option, 'external'], env={})['engine'], 'rpict')
            native.assert_not_called()

    def test_sync_counts_only_unique_completed_tiles(self):
        sync = self.work / 'sync'
        sync.write_text('2 2\n1 1\n\n')
        self.assertEqual(P._sync_completed(sync, 2, 2), set())
        sync.write_text('2 2\n0 0\n\n1 1\n1 1\n0 1\n0 ')
        self.assertEqual(P._sync_completed(sync, 2, 2), {(1, 1), (0, 1)})
        sync.write_text('2 2\n0 0\n\n2 1\n')
        with self.assertRaises(RuntimeError): P._sync_completed(sync, 2, 2)

    @unittest.skipIf(os.name == 'nt', 'POSIX record lock')
    def test_sync_read_waits_for_native_posix_writer_lock(self):
        sync = self.work / 'sync'; sync.write_text('2 2\n0 0\n\n')
        code = ('import fcntl,sys,time; f=open(sys.argv[1],"a+"); '
                'fcntl.lockf(f,fcntl.LOCK_EX); print("locked",flush=True); '
                'time.sleep(.3); f.write("0 0\\n"); f.flush(); fcntl.lockf(f,fcntl.LOCK_UN)')
        proc = subprocess.Popen([sys.executable, '-c', code, str(sync)], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(proc.stdout.readline().strip(), 'locked')
            start = time.monotonic(); done = P._sync_completed(sync, 2, 2)
            self.assertGreater(time.monotonic() - start, .15)
            self.assertEqual(done, {(0, 0)})
            self.assertEqual(proc.wait(timeout=2), 0)
        finally:
            if proc.poll() is None: proc.kill(); proc.wait()
            proc.stdout.close()

    def test_supervisor_cannot_start_in_main_application_group(self):
        with patch.object(P.os, 'getpgrp', return_value=900), patch.object(P.os, 'getpid', return_value=901):
            with self.assertRaisesRegex(RuntimeError, 'separate process group'): P._supervise({})

    def fake(self, mode):
        script = self.work / ('fake_' + mode)
        if mode == 'partial':
            body = '''import json,os,pathlib
d=pathlib.Path(os.environ['TMPDIR']);j=json.loads((d/'job.json').read_text());p=j['plan']
(d/'pieces.sync').write_text('2 2\\n0 0\\n\\n0 0\\n')
pathlib.Path(j['output']).write_bytes(b'#?RADIANCE\\nFORMAT=32-bit_rle_rgbe\\n\\n-Y 64 +X 64\\n'+bytes(64*64*4))
'''
        elif mode == 'earlyexit':
            body = '''import os,pathlib,time,sys
d=pathlib.Path(os.environ['TMPDIR'])
try:
 fd=os.open(d/'leader',os.O_WRONLY|os.O_CREAT|os.O_EXCL);os.close(fd);short=True
except FileExistsError:short=False
if short:
 (d/'sync.tmp').write_text('2 2\\n1 0\\n\\n0 0\\n');os.replace(d/'sync.tmp',d/'pieces.sync')
 print('0 0 begun',flush=True);print('0 0 done',flush=True);time.sleep(.05);sys.exit(0)
print('1 0 begun',flush=True);time.sleep(30)
'''
        else:
            # Each worker creates its own child and grandchild. None escape into a new group.
            leaf = "import os,pathlib,time;pathlib.Path(os.environ['TMPDIR'],'leaf_'+str(os.getpid())+'.pid').write_text(str(os.getpid()));time.sleep(30)"
            child = ("import os,pathlib,subprocess,sys,time;pathlib.Path(os.environ['TMPDIR'],'child_'+str(os.getpid())+'.pid').write_text(str(os.getpid()));"
                     "subprocess.Popen([sys.executable,'-c'," + repr(leaf) + "]);time.sleep(30)")
            body = ('import os,pathlib,subprocess,sys,time\n'
                    "d=pathlib.Path(os.environ['TMPDIR']);(d/('worker_'+str(os.getpid())+'.pid')).write_text(str(os.getpid()))\n"
                    'subprocess.Popen([sys.executable,"-c",' + repr(child) + '])\n'
                    "limit=time.monotonic()+3\nwhile len(list(d.glob('leaf_*.pid')))<2 and time.monotonic()<limit:time.sleep(.01)\n"
                    + ("sys.exit(3)\n" if mode == 'failure' else "print('0 0 begun',flush=True);time.sleep(30)\n"))
        # The script path may contain spaces, which the kernel splits in a shebang.
        # Therefore use env to select Python from PATH.
        script.write_text('#!/usr/bin/env python3\n' + body)
        script.chmod(0o700)
        return script

    def assert_children_stopped(self, directory):
        paths = list(directory.glob('*.pid'))
        self.assertGreaterEqual(len(paths), 3)
        def running():
            live = []
            for path in paths:
                pid = int(path.read_text())
                try:
                    state = Path('/proc', str(pid), 'stat').read_text().rsplit(')', 1)[1].split()[0]
                    if state != 'Z': live.append(pid)
                except FileNotFoundError: pass
            return live
        until = time.monotonic() + 2
        while running() and time.monotonic() < until: time.sleep(.02)
        self.assertEqual(running(), [], 'A child/descendant owned by the test continued running')

    @unittest.skipIf(os.name == 'nt', 'Local POSIX process group')
    def test_full_hdr_size_and_exit_zero_do_not_accept_missing_tile(self):
        plan = self.plan(); plan['rpiece'] = str(self.fake('partial'))
        job = self.work / 'partial'; output = self.work / 'partial.hdr'
        result = P.run(plan, [], self.work / 'unused.oct', self.work, output, None, job, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('did not complete all tiles', result.stderr)
        P._validate_hdr(output, 64, 64)  # Matching dimensions are deliberately not sufficient.
        self.assertEqual(json.loads((job / 'progress.json').read_text())['completed'], 1)

    @unittest.skipUnless(Path('/proc').is_dir(), 'Linux process inspection')
    def test_worker_failure_and_timeout_also_terminate_descendants(self):
        for mode in ('failure', 'timeout'):
            with self.subTest(mode=mode):
                plan = self.plan(); plan['rpiece'] = str(self.fake(mode)); job = self.work / mode
                if mode == 'timeout':
                    with self.assertRaisesRegex(RuntimeError, 'timed out'):
                        P.run(plan, [], self.work / 'unused.oct', self.work, self.work / (mode + '.hdr'), None, job, timeout=1)
                    snapshot = json.loads((job/'progress.json').read_text())
                    tile = next(p for p in snapshot['chunks'] if (p['x'], p['y']) == (0, 0))
                    self.assertEqual(tile['status'], 'calculation'); self.assertEqual(snapshot['completed'], 0)
                    owner = next(p for p in snapshot['processes'] if p['worker'] == tile['worker'])
                    self.assertTrue((job/('worker_' + str(owner['pid']) + '.pid')).is_file())
                else:
                    result = P.run(plan, [], self.work / 'unused.oct', self.work, self.work / (mode + '.hdr'), None, job, timeout=5)
                    self.assertNotEqual(result.returncode, 0)
                self.assert_children_stopped(job)

    @unittest.skipIf(os.name == 'nt', 'Local POSIX process group')
    def test_finished_worker_is_not_active_while_another_tile_runs(self):
        plan = self.plan(); plan['rpiece'] = str(self.fake('earlyexit')); job = self.work/'earlyexit'
        results = []
        def run():
            try:
                P.run(plan, [], self.work/'unused.oct', self.work, self.work/'early.hdr', None, job, timeout=2)
            except RuntimeError as error:
                results.append(str(error))
        thread = threading.Thread(target=run); thread.start()
        matched = None
        try:
            until = time.monotonic() + 1.5
            while time.monotonic() < until:
                try:
                    snapshot = json.loads((job/'progress.json').read_text())
                    active = [p['enabled'] for p in snapshot['processes']]
                    if snapshot['completed'] == 1 and sorted(active) == [False, True]:
                        matched = snapshot; break
                except (OSError, ValueError): pass
                time.sleep(.02)
        finally:
            thread.join(3)
        self.assertFalse(thread.is_alive()); self.assertIsNotNone(matched)
        self.assertTrue(any('timed out' in error for error in results))

    @unittest.skipUnless(Path('/proc').is_dir(), 'Linux process inspection')
    def test_disk_full_report_error_cannot_bypass_process_cleanup(self):
        job = self.work / 'diskfull'; job.mkdir(); plan = self.plan(); plan['rpiece'] = str(self.fake('diskfull'))
        data = {'version': 1, 'plan': plan, 'args': [], 'directory': str(job), 'cwd': str(self.work),
                'output': str(self.work / 'failed.hdr'), 'octree': str(self.work / 'unused.oct'), 'ambient': None}
        path = job / 'job.json'; path.write_text(json.dumps(data))
        wrapper = self.work / 'controller_failure.py'
        wrapper.write_text('import sys,json,errno\nsys.path.insert(0,' + repr(str(ROOT)) + ')\n'
            'from core import render_parallel as p\noriginal=p._atomic_json\n'
            'def fail(path,data):\n'
            ' if data.get("started",0)>0:raise OSError(errno.ENOSPC,"synthetic disk full")\n'
            ' return original(path,data)\n'
            'p._atomic_json=fail\np._supervise(json.load(open(sys.argv[1])))\n')
        env = {**self.env, 'TMPDIR': str(job)}
        result = processes.run([sys.executable, str(wrapper), str(path)], cwd=self.work, env=env, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('synthetic disk full', result.stderr)
        self.assert_children_stopped(job)

    def test_serial_error_preserves_previous_hdr_and_progress_ownership(self):
        cache = self.work / '_cache'; cache.mkdir(); octree = cache / 'scene.oct'; octree.write_bytes(b'octree')
        output = self.work / 'sample.hdr'; output.write_bytes(b'old')
        def failed(cmd, **kw):
            self.assertIn('-t', cmd); self.assertEqual(cmd[cmd.index('-t') + 1], '5')
            self.assertIn('-e', cmd); self.assertEqual(render_progress.status()['backend'], 'rpict')
            Path(kw['stdout_path']).write_bytes(b'partial')
            return subprocess.CompletedProcess(cmd, 1, '', 'failure')
        with patch.object(processes, 'run', side_effect=failed):
            with self.assertRaisesRegex(RuntimeError, 'rpict'):
                engine.render(str(octree), '-vtv', 'preview', 32, 32, str(output))
        self.assertEqual(output.read_bytes(), b'old'); self.assertIsNone(render_progress.status())
        self.assertFalse(list(self.work.glob('*.tmp')))


@unittest.skipUnless(all((BIN / name).is_file() for name in ('rpiece', 'rpict', 'oconv', 'vwrays', 'pvalue')), 'Local Radiance tools unavailable')
class RealRadianceTest(unittest.TestCase):
    setUp = ParallelTest.setUp
    plan = ParallelTest.plan
    def scene(self):
        source = self.work / 'synthetic.rad'
        source.write_text('void glow bg\n0\n0\n4 .03 .04 .05 0\nbg source sky\n0\n0\n4 0 0 1 360\n'
            'void light lamp\n0\n0\n3 15 15 15\nlamp sphere light\n0\n0\n4 -2 1 3 .3\n'
            'void plastic gray\n0\n0\n5 .5 .3 .2 0 0\ngray sphere ball\n0\n0\n4 0 3 0 1\n')
        octree = self.work / 'synthetic.oct'
        with octree.open('wb') as stream:
            subprocess.run([str(BIN / 'oconv'), '-f', str(source)], cwd=self.work, env=self.env, stdout=stream, check=True, timeout=5)
        return octree

    def values(self, hdr):
        result = subprocess.run([str(BIN / 'pvalue'), '-h', '-H', '-df', str(hdr)], env=self.env, capture_output=True, check=True, timeout=5)
        values = array.array('f'); values.frombytes(result.stdout)
        return values

    def test_native_camera_and_full_coverage_perspective_fisheye_irradiance(self):
        octree = self.scene()
        for name, view, irradiance in (('perspective', ['-vtv', '-vh', '50', '-vv', '37.5'], False),
                                      ('fisheye', ['-vta', '-vh', '180', '-vv', '180'], False),
                                      ('irradiance', ['-vtv', '-vh', '50', '-vv', '37.5'], True)):
            with self.subTest(name=name), patch.dict(os.environ, self.env):
                args = [*view, '-vp', '0', '0', '0', '-vd', '0', '1', '0', '-vu', '0', '0', '1',
                        '-ab', '0', '-ps', '1', '-pt', '0', '-pj', '0', '-dj', '0', '-ds', '0']
                if irradiance: args += ['-i']
                native_size = subprocess.run([str(BIN / 'vwrays'), '-d', *P._view_args(args), '-x', '130', '-y', '99'],
                    env=self.env, cwd=self.work, capture_output=True, text=True, check=True, timeout=5).stdout.split()
                width, height = int(native_size[1]), int(native_size[3]); grid = P._divisor_grid(width, height, 2)
                self.assertIsNotNone(grid)
                plan = self.plan(); plan.update(width=width, height=height, xdiv=grid[0], ydiv=grid[1], total_tiles=grid[0]*grid[1])
                output = self.work / (name + '.hdr'); job = self.work / name
                result = P.run(plan, args, octree, self.work, output, None, job, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                P._validate_hdr(output, width, height)
                status = json.loads((job / 'progress.json').read_text())
                self.assertEqual(status['phase'], 'done'); self.assertEqual(status['completed'], grid[0]*grid[1])
                self.assertEqual(status['grid'], {'x':grid[0], 'y':grid[1]})
                self.assertLessEqual((job/'progress.json').stat().st_size, 65536)
                self.assertEqual(len(status['chunks']), grid[0]*grid[1])
                self.assertTrue(all(tile['status'] == 'done' for tile in status['chunks']))
                self.assertTrue(all(tile['worker'] in (1, 2) for tile in status['chunks']))
                self.assertEqual([p['worker'] for p in status['processes']], [1, 2])
                self.assertTrue(all(p['pid'] > 0 and not p['enabled'] for p in status['processes']))
                native = self.work / (name + '_native.hdr')
                with native.open('wb') as stream:
                    subprocess.run([str(BIN / 'rpict'), *args, '-x', '130', '-y', '99', str(octree)],
                        env=self.env, cwd=self.work, stdout=stream, check=True, timeout=10)
                before, after = self.values(native), self.values(output)
                self.assertEqual(len(before), width*height*3); self.assertEqual(len(before), len(after))
                differences = [abs(a-b) for a,b in zip(before, after)]
                self.assertLess(sum(differences)/len(differences), 2e-5)
                self.assertLessEqual(max(differences), .008)
                self.assertEqual([v == 0 for v in before], [v == 0 for v in after])

    def test_workers_share_ambient_path_and_quality(self):
        octree = self.scene(); args = ['-vp','0','0','0','-vd','0','1','0','-vu','0','0','1','-vh','50','-vv','50',
            '-ab','1','-ad','16','-as','0','-ps','1','-pt','0','-pj','0']
        ambient = self.work / 'shared cache.amb'; job = self.work / 'ambient'
        with patch.dict(os.environ, self.env):
            result = P.run(self.plan(), args, octree, self.work, self.work/'ambient.hdr', ambient, job, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        recorded = json.loads((job/'job.json').read_text()); self.assertEqual(recorded['args'], args)
        self.assertEqual(recorded['ambient'], str(ambient)); self.assertTrue(ambient.is_file())
        self.assertEqual(len(list(job.glob('worker_*.log'))), 2)
        self.assertEqual(json.loads((job/'progress.json').read_text())['completed'], 4)

    def test_engine_parallel_path_publishes_completed_hdr(self):
        octree = self.scene(); cache = self.work/'_cache'; cache.mkdir()
        cached = cache/'scene.oct'; shutil.copy2(octree, cached)
        output = self.work/'final.hdr'; output.write_bytes(b'previous output')
        env = {**self.env, 'CTLUX_RENDER_WORKERS': '2'}
        with patch.dict(os.environ, env), patch.object(P, '_cpu_count', return_value=4), \
                patch.object(P, '_available_memory', return_value=2 << 30):
            result = engine.render(str(cached), '-vp 0 0 0 -vd 0 1 0 -vu 0 0 1 -vh 50 -vv 50',
                '-ab 0 -ps 1 -pt 0 -pj 0 -dj 0 -ds 0', 256, 256, str(output))
        self.assertEqual(result, str(output)); P._validate_hdr(output, 256, 256)
        jobs = list((cache/'render_jobs').glob('*/job.json')); self.assertEqual(len(jobs), 1)
        data = json.loads(jobs[0].read_text()); self.assertEqual(data['plan']['engine'], 'rpiece')
        self.assertEqual(data['plan']['workers'], 2)
        self.assertIsNone(render_progress.status()); self.assertFalse(list(self.work.glob('*.tmp')))

    def test_engine_preview_320_publishes_complete_native_size_with_same_settings(self):
        octree = self.scene(); cache = self.work / '_cache'; cache.mkdir()
        cached = cache / 'scene.oct'; shutil.copy2(octree, cached)
        output = self.work / 'preview.hdr'; output.write_bytes(b'previous output')
        view = '-vtv -vp 0 0 0 -vd 0 1 0 -vu 0 0 1 -vh 50 -vv 37.5'
        quality = engine.QUALITY['preview']
        env = {**self.env, 'CTLUX_RENDER_WORKERS': '2'}
        with patch.dict(os.environ, env), patch.object(P, '_cpu_count', return_value=4), \
                patch.object(P, '_available_memory', return_value=2 << 30):
            result = engine.render(str(cached), view, 'preview', 320, 240, str(output))
        self.assertEqual(result, str(output)); P._validate_hdr(output, 320, 233)
        jobs = list((cache / 'render_jobs').glob('*/job.json')); self.assertEqual(len(jobs), 1)
        data = json.loads(jobs[0].read_text()); plan = data['plan']
        self.assertEqual((plan['engine'], plan['workers']), ('rpiece', 2))
        self.assertEqual(data['args'], shlex.split(view) + shlex.split(quality))
        self.assertIsNone(data['ambient']); self.assertFalse(list(cache.glob('*.amb')))
        job_dir = jobs[0].parent
        done = P._sync_completed(job_dir / 'pieces.sync', plan['xdiv'], plan['ydiv'])
        self.assertEqual(done, {(x, y) for y in range(plan['ydiv']) for x in range(plan['xdiv'])})
        status = json.loads((job_dir / 'progress.json').read_text())
        self.assertEqual((status['phase'], status['completed']), ('done', plan['total_tiles']))
        self.assertTrue(all(not worker['enabled'] for worker in status['processes']))
        values = self.values(output)
        self.assertEqual(len(values), 320 * 233 * 3)
        self.assertTrue(all(math.isfinite(value) and value >= 0 for value in values))
        self.assertGreater(max(values), .05)
        self.assertIsNone(render_progress.status()); self.assertFalse(list(self.work.glob('*.tmp')))


if __name__ == '__main__': unittest.main()
