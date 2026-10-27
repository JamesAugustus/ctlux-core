# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Küçük sentetik Radiance sahneleri ve sadece teste ait process group'ları.

NDA'lı sahne ya da kullanıcının render'ı kullanılmaz.
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
from core import engine as motor
from core import render_parallel as P
from core import render_progress as render_ilerleme
from core import processes as surecler

BIN = Path(motor.radiance_bul() or '/radiance-unavailable')


class ParalelTest(unittest.TestCase):
    def setUp(self):
        (ROOT / 'tests/.tmp').mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix='paralel boşluk ', dir=ROOT / 'tests/.tmp')
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
             patch.object(P.render_kaynak, 'cgroup_yollari', return_value=groups):
            self.assertEqual(P._cpu_sayisi(), 2)
            (groups[1] / 'cpu.max').write_text('150000 100000')
            self.assertEqual(P._cpu_sayisi(), 1)
            (groups[1] / 'cpu.max').write_text('max 100000')
            (groups[2] / 'cpu.max').write_text('broken')
            self.assertEqual(P._cpu_sayisi(), 8)

    def plan(self, workers=2):
        return {'motor': 'rpiece', 'isciler': workers, 'xdiv': 2, 'ydiv': 2, 'toplam_parca': 4,
                'width': 64, 'height': 64, 'rpiece': str(BIN / 'rpiece')}

    def test_plan_cpu_bellek_kucuk_goruntu_ve_ozel_arguman_kapilari(self):
        response = subprocess.CompletedProcess([], 0, '-x 512 -y 512\n', '')
        octree = self.work / 'scene.oct'; octree.write_bytes(b'abc')
        with patch.object(P, '_cpu_sayisi', return_value=8), patch.object(P, '_bos_bellek', return_value=4 << 30), \
                patch.object(P.shutil, 'which', return_value='/yerel/rpiece'), patch.object(surecler, 'calistir', return_value=response):
            plan = P.planla(512, 512, 'taslak', octree, args=['-ab', '2'], env={})
            self.assertEqual(plan['isciler'], 6)
            self.assertEqual(512 % plan['xdiv'], 0); self.assertEqual(512 % plan['ydiv'], 0)
            self.assertEqual(P.planla(512, 512, 'taslak', octree, env={'CTLUX_RENDER_WORKERS': '1'})['motor'], 'rpict')
            self.assertEqual(P.planla(512, 512, 'taslak', octree, env={'CTLUX_RENDER_WORKERS': '16'})['isciler'], 6)
            self.assertEqual(P.planla(32, 32, 'final', octree)['motor'], 'rpict')
            self.assertEqual(P.planla(512, 512, 'onizleme', octree)['motor'], 'rpiece')
            for arg in ('-x', '-y', '-o', '-F', '-R', '-t', '-e', '-af', '-S', '-unknown'):
                with self.subTest(arg=arg):
                    self.assertEqual(P.planla(512, 512, 'taslak', octree, args=[arg, 'foo'])['motor'], 'rpict')
            with patch.object(P, '_bos_bellek', return_value=128 << 20):
                self.assertEqual(P.planla(512, 512, 'taslak', octree)['motor'], 'rpict')
            with patch.object(P, '_cpu_sayisi', return_value=1):
                self.assertEqual(P.planla(512, 512, 'taslak', octree)['motor'], 'rpict')
        self.assertIsNone(P._bolen_grid(503, 509, 6))

    def test_onizleme_boyut_ve_guvenlik_kapilari_kaliteyi_degistirmez(self):
        octree = self.work / 'scene.oct'; octree.write_bytes(b'abc')
        args = shlex.split('-vtv -vh 50 -vv 37.5 ' + motor.KALITE['onizleme'])
        original = list(args)
        response = subprocess.CompletedProcess([], 0, '-x 320 -y 233\n', '')
        with patch.object(P, '_cpu_sayisi', return_value=8), \
                patch.object(P, '_bos_bellek', return_value=4 << 30), \
                patch.object(P.shutil, 'which', return_value='/yerel/rpiece'), \
                patch.object(surecler, 'calistir', return_value=response) as native:
            plan = P.planla(320, 240, 'onizleme', octree, args=args, env={})
            self.assertEqual((plan['motor'], plan['isciler']), ('rpiece', 6))
            self.assertEqual((plan['width'], plan['height']), (320, 233))
            self.assertEqual(320 % plan['xdiv'], 0); self.assertEqual(233 % plan['ydiv'], 0)
            self.assertEqual(args, original)
            native.reset_mock()
            for width, height in ((160, 120), (255, 256)):
                with self.subTest(width=width, height=height):
                    self.assertEqual(P.planla(width, height, 'onizleme', octree, args=args, env={})['motor'], 'rpict')
            self.assertEqual(P.planla(320, 240, 'onizleme', octree, args=args,
                                     env={'CTLUX_RENDER_WORKERS': '1'})['motor'], 'rpict')
            for missing in ('rpiece', 'vwrays'):
                with self.subTest(missing=missing), patch.object(P.shutil, 'which',
                        side_effect=lambda name, path=None: None if name == missing else '/yerel/' + name):
                    self.assertEqual(P.planla(320, 240, 'onizleme', octree, args=args, env={})['motor'], 'rpict')
            for option in ('-F', '-o', '-unknown'):
                with self.subTest(option=option):
                    self.assertEqual(P.planla(320, 240, 'onizleme', octree,
                        args=args + [option, 'external'], env={})['motor'], 'rpict')
            native.assert_not_called()

    def test_sync_yalniz_benzersiz_biten_parcalari_sayar(self):
        sync = self.work / 'sync'
        sync.write_text('2 2\n1 1\n\n')
        self.assertEqual(P._sync_tamamlanan(sync, 2, 2), set())
        sync.write_text('2 2\n0 0\n\n1 1\n1 1\n0 1\n0 ')
        self.assertEqual(P._sync_tamamlanan(sync, 2, 2), {(1, 1), (0, 1)})
        sync.write_text('2 2\n0 0\n\n2 1\n')
        with self.assertRaises(RuntimeError): P._sync_tamamlanan(sync, 2, 2)

    @unittest.skipIf(os.name == 'nt', 'POSIX kayıt kilidi')
    def test_sync_okuma_native_posix_yazici_kilidini_bekler(self):
        sync = self.work / 'sync'; sync.write_text('2 2\n0 0\n\n')
        code = ('import fcntl,sys,time; f=open(sys.argv[1],"a+"); '
                'fcntl.lockf(f,fcntl.LOCK_EX); print("locked",flush=True); '
                'time.sleep(.3); f.write("0 0\\n"); f.flush(); fcntl.lockf(f,fcntl.LOCK_UN)')
        proc = subprocess.Popen([sys.executable, '-c', code, str(sync)], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(proc.stdout.readline().strip(), 'locked')
            start = time.monotonic(); done = P._sync_tamamlanan(sync, 2, 2)
            self.assertGreater(time.monotonic() - start, .15)
            self.assertEqual(done, {(0, 0)})
            self.assertEqual(proc.wait(timeout=2), 0)
        finally:
            if proc.poll() is None: proc.kill(); proc.wait()
            proc.stdout.close()

    def test_denetci_ana_uygulamanin_grubunda_baslatilamaz(self):
        with patch.object(P.os, 'getpgrp', return_value=900), patch.object(P.os, 'getpid', return_value=901):
            with self.assertRaisesRegex(RuntimeError, 'ayrı süreç grubunda'): P._denetle({})

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
            # her worker kendi child'ını ve onun altını üretir. Hiçbiri yeni gruba kaçmaz.
            leaf = "import os,pathlib,time;pathlib.Path(os.environ['TMPDIR'],'leaf_'+str(os.getpid())+'.pid').write_text(str(os.getpid()));time.sleep(30)"
            child = ("import os,pathlib,subprocess,sys,time;pathlib.Path(os.environ['TMPDIR'],'child_'+str(os.getpid())+'.pid').write_text(str(os.getpid()));"
                     "subprocess.Popen([sys.executable,'-c'," + repr(leaf) + "]);time.sleep(30)")
            body = ('import os,pathlib,subprocess,sys,time\n'
                    "d=pathlib.Path(os.environ['TMPDIR']);(d/('worker_'+str(os.getpid())+'.pid')).write_text(str(os.getpid()))\n"
                    'subprocess.Popen([sys.executable,"-c",' + repr(child) + '])\n'
                    "limit=time.monotonic()+3\nwhile len(list(d.glob('leaf_*.pid')))<2 and time.monotonic()<limit:time.sleep(.01)\n"
                    + ("sys.exit(3)\n" if mode == 'failure' else "print('0 0 begun',flush=True);time.sleep(30)\n"))
        # script'in path'i boşluk içerebilir, kernel shebang'deki path'i
        # boşluktan böler. O yüzden env ile PATH'teki Python seçiliyor.
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
        self.assertEqual(running(), [], 'Teste ait çocuk/torun süreç çalışmayı sürdürdü')

    @unittest.skipIf(os.name == 'nt', 'Yerel POSIX süreç grubu')
    def test_tam_hdr_boyutu_ve_exit0_eksik_parcayi_basarili_saymaz(self):
        plan = self.plan(); plan['rpiece'] = str(self.fake('partial'))
        job = self.work / 'partial'; output = self.work / 'partial.hdr'
        result = P.calistir(plan, [], self.work / 'unused.oct', self.work, output, None, job, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('bütün parçaları tamamlamadı', result.stderr)
        P._hdr_dogrula(output, 64, 64)  # boyutun tutması bilerek yeterli sayılmıyor.
        self.assertEqual(json.loads((job / 'ilerleme.json').read_text())['tamamlanan'], 1)

    @unittest.skipUnless(Path('/proc').is_dir(), 'Linux süreç denetimi')
    def test_worker_hatasi_ve_timeout_torun_surecleri_de_kapatir(self):
        for mode in ('failure', 'timeout'):
            with self.subTest(mode=mode):
                plan = self.plan(); plan['rpiece'] = str(self.fake(mode)); job = self.work / mode
                if mode == 'timeout':
                    with self.assertRaisesRegex(RuntimeError, 'zaman aşımı'):
                        P.calistir(plan, [], self.work / 'unused.oct', self.work, self.work / (mode + '.hdr'), None, job, timeout=1)
                    snapshot = json.loads((job/'ilerleme.json').read_text())
                    tile = next(p for p in snapshot['parcalar'] if (p['x'], p['y']) == (0, 0))
                    self.assertEqual(tile['durum'], 'hesap'); self.assertEqual(snapshot['tamamlanan'], 0)
                    owner = next(p for p in snapshot['calisanlar'] if p['isci'] == tile['isci'])
                    self.assertTrue((job/('worker_' + str(owner['pid']) + '.pid')).is_file())
                else:
                    result = P.calistir(plan, [], self.work / 'unused.oct', self.work, self.work / (mode + '.hdr'), None, job, timeout=5)
                    self.assertNotEqual(result.returncode, 0)
                self.assert_children_stopped(job)

    @unittest.skipIf(os.name == 'nt', 'Yerel POSIX süreç grubu')
    def test_biten_isci_diger_uzun_parca_surerken_etkin_gosterilmez(self):
        plan = self.plan(); plan['rpiece'] = str(self.fake('earlyexit')); job = self.work/'earlyexit'
        results = []
        def run():
            try:
                P.calistir(plan, [], self.work/'unused.oct', self.work, self.work/'early.hdr', None, job, timeout=2)
            except RuntimeError as error:
                results.append(str(error))
        thread = threading.Thread(target=run); thread.start()
        matched = None
        try:
            until = time.monotonic() + 1.5
            while time.monotonic() < until:
                try:
                    snapshot = json.loads((job/'ilerleme.json').read_text())
                    active = [p['etkin'] for p in snapshot['calisanlar']]
                    if snapshot['tamamlanan'] == 1 and sorted(active) == [False, True]:
                        matched = snapshot; break
                except (OSError, ValueError): pass
                time.sleep(.02)
        finally:
            thread.join(3)
        self.assertFalse(thread.is_alive()); self.assertIsNotNone(matched)
        self.assertTrue(any('zaman aşımı' in error for error in results))

    @unittest.skipUnless(Path('/proc').is_dir(), 'Linux süreç denetimi')
    def test_disk_dolu_rapor_hatasi_surec_temizligini_atlayamaz(self):
        job = self.work / 'diskfull'; job.mkdir(); plan = self.plan(); plan['rpiece'] = str(self.fake('diskfull'))
        data = {'version': 1, 'plan': plan, 'args': [], 'directory': str(job), 'cwd': str(self.work),
                'output': str(self.work / 'failed.hdr'), 'octree': str(self.work / 'unused.oct'), 'ambient': None}
        path = job / 'job.json'; path.write_text(json.dumps(data))
        wrapper = self.work / 'controller_failure.py'
        wrapper.write_text('import sys,json,errno\nsys.path.insert(0,' + repr(str(ROOT)) + ')\n'
            'from core import render_parallel as p\noriginal=p._atomik_json\n'
            'def fail(path,data):\n'
            ' if data.get("baslanan",0)>0:raise OSError(errno.ENOSPC,"sentetik disk dolu")\n'
            ' return original(path,data)\n'
            'p._atomik_json=fail\np._denetle(json.load(open(sys.argv[1])))\n')
        env = {**self.env, 'TMPDIR': str(job)}
        result = surecler.calistir([sys.executable, str(wrapper), str(path)], cwd=self.work, env=env, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('sentetik disk dolu', result.stderr)
        self.assert_children_stopped(job)

    def test_seri_hata_eski_hdr_ve_ilerleme_sahipligini_korur(self):
        cache = self.work / '_cache'; cache.mkdir(); octree = cache / 'scene.oct'; octree.write_bytes(b'octree')
        output = self.work / 'sample.hdr'; output.write_bytes(b'old')
        def failed(cmd, **kw):
            self.assertIn('-t', cmd); self.assertEqual(cmd[cmd.index('-t') + 1], '5')
            self.assertIn('-e', cmd); self.assertEqual(render_ilerleme.durum()['backend'], 'rpict')
            Path(kw['stdout_yolu']).write_bytes(b'partial')
            return subprocess.CompletedProcess(cmd, 1, '', 'failure')
        with patch.object(surecler, 'calistir', side_effect=failed):
            with self.assertRaisesRegex(RuntimeError, 'rpict'):
                motor.render(str(octree), '-vtv', 'onizleme', 32, 32, str(output))
        self.assertEqual(output.read_bytes(), b'old'); self.assertIsNone(render_ilerleme.durum())
        self.assertFalse(list(self.work.glob('*.tmp')))


@unittest.skipUnless(all((BIN / name).is_file() for name in ('rpiece', 'rpict', 'oconv', 'vwrays', 'pvalue')), 'Yerel Radiance araçları yok')
class GercekRadianceTest(unittest.TestCase):
    setUp = ParalelTest.setUp
    plan = ParalelTest.plan
    def scene(self):
        source = self.work / 'sentetik.rad'
        source.write_text('void glow bg\n0\n0\n4 .03 .04 .05 0\nbg source sky\n0\n0\n4 0 0 1 360\n'
            'void light lamp\n0\n0\n3 15 15 15\nlamp sphere light\n0\n0\n4 -2 1 3 .3\n'
            'void plastic gray\n0\n0\n5 .5 .3 .2 0 0\ngray sphere ball\n0\n0\n4 0 3 0 1\n')
        octree = self.work / 'sentetik.oct'
        with octree.open('wb') as stream:
            subprocess.run([str(BIN / 'oconv'), '-f', str(source)], cwd=self.work, env=self.env, stdout=stream, check=True, timeout=5)
        return octree

    def values(self, hdr):
        result = subprocess.run([str(BIN / 'pvalue'), '-h', '-H', '-df', str(hdr)], env=self.env, capture_output=True, check=True, timeout=5)
        values = array.array('f'); values.frombytes(result.stdout)
        return values

    def test_native_kamera_ve_tam_kapsam_perspektif_fisheye_irradiance(self):
        octree = self.scene()
        for name, view, irradiance in (('perspektif', ['-vtv', '-vh', '50', '-vv', '37.5'], False),
                                      ('fisheye', ['-vta', '-vh', '180', '-vv', '180'], False),
                                      ('irradiance', ['-vtv', '-vh', '50', '-vv', '37.5'], True)):
            with self.subTest(name=name), patch.dict(os.environ, self.env):
                args = [*view, '-vp', '0', '0', '0', '-vd', '0', '1', '0', '-vu', '0', '0', '1',
                        '-ab', '0', '-ps', '1', '-pt', '0', '-pj', '0', '-dj', '0', '-ds', '0']
                if irradiance: args += ['-i']
                native_size = subprocess.run([str(BIN / 'vwrays'), '-d', *P._gorunum_args(args), '-x', '130', '-y', '99'],
                    env=self.env, cwd=self.work, capture_output=True, text=True, check=True, timeout=5).stdout.split()
                width, height = int(native_size[1]), int(native_size[3]); grid = P._bolen_grid(width, height, 2)
                self.assertIsNotNone(grid)
                plan = self.plan(); plan.update(width=width, height=height, xdiv=grid[0], ydiv=grid[1], toplam_parca=grid[0]*grid[1])
                output = self.work / (name + '.hdr'); job = self.work / name
                result = P.calistir(plan, args, octree, self.work, output, None, job, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                P._hdr_dogrula(output, width, height)
                status = json.loads((job / 'ilerleme.json').read_text())
                self.assertEqual(status['asama'], 'tamam'); self.assertEqual(status['tamamlanan'], grid[0]*grid[1])
                self.assertEqual(status['grid'], {'x':grid[0], 'y':grid[1]})
                self.assertLessEqual((job/'ilerleme.json').stat().st_size, 65536)
                self.assertEqual(len(status['parcalar']), grid[0]*grid[1])
                self.assertTrue(all(tile['durum'] == 'tamam' for tile in status['parcalar']))
                self.assertTrue(all(tile['isci'] in (1, 2) for tile in status['parcalar']))
                self.assertEqual([p['isci'] for p in status['calisanlar']], [1, 2])
                self.assertTrue(all(p['pid'] > 0 and not p['etkin'] for p in status['calisanlar']))
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

    def test_ambient_yolu_ve_kalite_iscilerde_ortaktir(self):
        octree = self.scene(); args = ['-vp','0','0','0','-vd','0','1','0','-vu','0','0','1','-vh','50','-vv','50',
            '-ab','1','-ad','16','-as','0','-ps','1','-pt','0','-pj','0']
        ambient = self.work / 'ortak cache.amb'; job = self.work / 'ambient'
        with patch.dict(os.environ, self.env):
            result = P.calistir(self.plan(), args, octree, self.work, self.work/'ambient.hdr', ambient, job, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        recorded = json.loads((job/'job.json').read_text()); self.assertEqual(recorded['args'], args)
        self.assertEqual(recorded['ambient'], str(ambient)); self.assertTrue(ambient.is_file())
        self.assertEqual(len(list(job.glob('worker_*.log'))), 2)
        self.assertEqual(json.loads((job/'ilerleme.json').read_text())['tamamlanan'], 4)

    def test_motor_paralel_yolu_tamamlanan_hdr_yayimlar(self):
        octree = self.scene(); cache = self.work/'_cache'; cache.mkdir()
        cached = cache/'scene.oct'; shutil.copy2(octree, cached)
        output = self.work/'final.hdr'; output.write_bytes(b'previous output')
        env = {**self.env, 'CTLUX_RENDER_WORKERS': '2'}
        with patch.dict(os.environ, env), patch.object(P, '_cpu_sayisi', return_value=4), \
                patch.object(P, '_bos_bellek', return_value=2 << 30):
            result = motor.render(str(cached), '-vp 0 0 0 -vd 0 1 0 -vu 0 0 1 -vh 50 -vv 50',
                '-ab 0 -ps 1 -pt 0 -pj 0 -dj 0 -ds 0', 256, 256, str(output))
        self.assertEqual(result, str(output)); P._hdr_dogrula(output, 256, 256)
        jobs = list((cache/'render_jobs').glob('*/job.json')); self.assertEqual(len(jobs), 1)
        data = json.loads(jobs[0].read_text()); self.assertEqual(data['plan']['motor'], 'rpiece')
        self.assertEqual(data['plan']['isciler'], 2)
        self.assertIsNone(render_ilerleme.durum()); self.assertFalse(list(self.work.glob('*.tmp')))

    def test_motor_onizleme_320_native_boyutta_tam_ve_ayni_ayarlarla_yayimlanir(self):
        octree = self.scene(); cache = self.work / '_cache'; cache.mkdir()
        cached = cache / 'scene.oct'; shutil.copy2(octree, cached)
        output = self.work / 'onizleme.hdr'; output.write_bytes(b'previous output')
        view = '-vtv -vp 0 0 0 -vd 0 1 0 -vu 0 0 1 -vh 50 -vv 37.5'
        quality = motor.KALITE['onizleme']
        env = {**self.env, 'CTLUX_RENDER_WORKERS': '2'}
        with patch.dict(os.environ, env), patch.object(P, '_cpu_sayisi', return_value=4), \
                patch.object(P, '_bos_bellek', return_value=2 << 30):
            result = motor.render(str(cached), view, 'onizleme', 320, 240, str(output))
        self.assertEqual(result, str(output)); P._hdr_dogrula(output, 320, 233)
        jobs = list((cache / 'render_jobs').glob('*/job.json')); self.assertEqual(len(jobs), 1)
        data = json.loads(jobs[0].read_text()); plan = data['plan']
        self.assertEqual((plan['motor'], plan['isciler']), ('rpiece', 2))
        self.assertEqual(data['args'], shlex.split(view) + shlex.split(quality))
        self.assertIsNone(data['ambient']); self.assertFalse(list(cache.glob('*.amb')))
        job_dir = jobs[0].parent
        done = P._sync_tamamlanan(job_dir / 'pieces.sync', plan['xdiv'], plan['ydiv'])
        self.assertEqual(done, {(x, y) for y in range(plan['ydiv']) for x in range(plan['xdiv'])})
        status = json.loads((job_dir / 'ilerleme.json').read_text())
        self.assertEqual((status['asama'], status['tamamlanan']), ('tamam', plan['toplam_parca']))
        self.assertTrue(all(not worker['etkin'] for worker in status['calisanlar']))
        values = self.values(output)
        self.assertEqual(len(values), 320 * 233 * 3)
        self.assertTrue(all(math.isfinite(value) and value >= 0 for value in values))
        self.assertGreater(max(values), .05)
        self.assertIsNone(render_ilerleme.durum()); self.assertFalse(list(self.work.glob('*.tmp')))


if __name__ == '__main__': unittest.main()
