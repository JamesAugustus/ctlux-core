# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""OOM tekrarını küçük girdilerle test eder, büyük kullanıcı sahnesi çalıştırmaz."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
from core import render_resources as K
from core import render_parallel as P
from core import processes as surecler


class KaynakTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cache = self.root / '_cache'
        self.cache.mkdir()

    def scene(self, size=900 << 20):
        octree = self.cache / 'sahne_test.oct'
        octree.write_bytes(b'x' * 256)
        mesh = self.cache / 'mesh_test.oct'
        # sparse dosya: 900 MiB RAM/disk ayırmadan boyut hesabını test et.
        with mesh.open('wb') as stream:
            stream.truncate(size)
        record = Path(str(octree) + '.json')
        record.write_text(json.dumps({'turetilmis': [{'yol': '_cache/mesh_test.oct'}] * 2}))
        return octree, mesh, record

    def test_bagli_geometri_bir_kez_sayilir_ve_alti_isci_sinirsiz_acilmaz(self):
        octree, mesh, _ = self.scene()
        expected = 256 + (900 << 20)
        self.assertEqual(P._sahne_boyutu(octree), expected)
        response = subprocess.CompletedProcess([], 0, '-x 320 -y 233\n', '')
        with patch.object(P, '_cpu_sayisi', return_value=16), \
             patch.object(P, '_bos_bellek', return_value=8 << 30), \
             patch.object(P.shutil, 'which', return_value='/bin/rpiece'), \
             patch.object(surecler, 'calistir', return_value=response):
            plan = P.planla(320, 240, 'taslak', octree, args=['-ab', '2'])
        self.assertEqual(plan['isciler'], 2)
        self.assertEqual(plan['sahne_bayt'], expected)
        self.assertLessEqual(plan['bellek_isci'] * plan['isciler'], 4 << 30)
        mesh.unlink()
        with self.assertRaises(OSError):
            P._sahne_boyutu(octree)

    def test_bagimlilik_proje_disina_cikamaz(self):
        octree, _, record = self.scene(256)
        record.write_text(json.dumps({'turetilmis': [{'yol': '../../other.oct'}]}))
        with self.assertRaisesRegex(ValueError, 'proje dışında'):
            P._sahne_boyutu(octree)

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux adres alanı sınırı')
    def test_native_cocuk_bellek_sinirini_asamaz(self):
        code = ('import sys\ntry:\n x=bytearray(256*1024*1024)\n'
                'except MemoryError:\n print("bounded");sys.exit(77)\n')
        result = surecler.calistir([sys.executable, '-c', code],
                                  bellek_siniri=128 << 20, timeout=5)
        self.assertEqual(result.returncode, 77)
        self.assertIn('bounded', result.stdout)
        self.assertFalse(surecler._aktif)

    @unittest.skipIf(os.name == 'nt', 'POSIX süreçler arası kilit')
    def test_ikinci_program_hazirlik_ve_render_ile_cakisamaz(self):
        code = ('import sys;sys.path.insert(0,sys.argv[1]);from core import processes as surecler\n'
                'try:\n with surecler.render_sirasi(): print("entered")\n'
                'except RuntimeError as e:\n print(str(e));sys.exit(42)\n')
        with surecler.render_sirasi():
            with surecler.render_sirasi():
                result = surecler.calistir([sys.executable, '-c', code, str(ROOT)], timeout=5)
            self.assertEqual(result.returncode, 42)
            self.assertIn('Başka bir CTLux/Radiance', result.stdout)
        result = surecler.calistir([sys.executable, '-c', code, str(ROOT)], timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertIn('entered', result.stdout)

    def test_dusuk_bellekte_calisan_is_kapatilir_ve_kilit_serbest_kalir(self):
        with patch.object(K, 'bos_bellek', side_effect=[4 << 30, 4 << 30, 32 << 20]):
            with self.assertRaisesRegex(K.BellekYetersiz, 'bellek azaldı'):
                with surecler.render_sirasi():
                    surecler.calistir([sys.executable, '-c', 'import time;time.sleep(30)'], timeout=5)
        self.assertFalse(surecler._aktif)
        with surecler.render_sirasi():
            result = surecler.calistir([sys.executable, '-c', 'print("ready")'], timeout=5)
        self.assertEqual(result.stdout.strip(), 'ready')

    def test_dusuk_bellekte_hic_surec_baslamaz(self):
        with patch.object(K, 'bos_bellek', return_value=32 << 20), \
             patch.object(surecler.subprocess, 'Popen') as popen:
            with self.assertRaises(K.BellekYetersiz):
                with surecler.render_sirasi():
                    surecler.calistir(['rpict'])
        popen.assert_not_called()


if __name__ == '__main__':
    unittest.main()
