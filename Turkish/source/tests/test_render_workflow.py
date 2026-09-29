# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Ölçülen hazırlık aşamaları, Radiance process'i ya da kullanıcı projesi gerekmez."""
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
from core import render_progress as ri
from core import render_mesh


class RenderAkisiTest(unittest.TestCase):
    def test_asama_ve_adim_saatleri_genel_yuzde_uydurmaz(self):
        with patch.object(ri.time, 'monotonic', side_effect=[10, 12, 15, 19, 20, 22]):
            with ri.is_akisi('a'):
                ri.adim('mesh_derle', 'obj2mesh', ayrinti='model.obj', onbellek={'mesh': False})
                d = ri.akis_durumu('a')
                self.assertEqual((d['gecen_sn'], d['asama_sn'], d['adim_sn']), (5, 5, 3))
                self.assertIsNone(d['genel_yuzde'])
                self.assertIsNone(d['eta_sn'])
                self.assertIsNone(d['toplam'])
                ri.adim('render', 'Radiance', asama='render')
                d = ri.akis_durumu('a')
                self.assertEqual((d['gecen_sn'], d['asama_sn'], d['adim_sn']), (10, 1, 1))
                self.assertEqual(d['onbellek'], {'mesh': False})
            self.assertIsNone(ri.akis_durumu('a'))

    def test_sayac_guncellemek_adim_saatini_sifirlamaz(self):
        with patch.object(ri.time, 'monotonic', side_effect=[0, 1, 3, 4]):
            with ri.is_akisi('a'):
                ri.adim('obj', 'copy', tamamlanan=0, toplam=20, birim='bayt')
                ri.adim('obj', 'copy', tamamlanan=10, toplam=20, birim='bayt')
                d = ri.akis_durumu('a')
                self.assertEqual(d['adim_sn'], 3)
                self.assertEqual((d['tamamlanan'], d['toplam'], d['birim']), (10, 20, 'bayt'))
                self.assertIsNone(d['genel_yuzde'])

    def test_eski_sahip_yeni_isin_kaydini_guncelleyemez_ve_silemez(self):
        basladi, devam, dur = threading.Event(), threading.Event(), threading.Event()
        hatalar = []
        def yeni():
            try:
                with ri.is_akisi('yeni'):
                    ri.adim('mesh', 'new job')
                    basladi.set()
                    if not devam.wait(3):
                        raise AssertionError('test timed out')
                    self.assertEqual(ri.akis_durumu('yeni')['adim'], 'mesh')
            except BaseException as e:
                hatalar.append(e)
            finally:
                dur.set()
        th = threading.Thread(target=yeni)
        with ri.is_akisi('eski'):
            th.start()
            self.assertTrue(basladi.wait(2))
            self.assertFalse(ri.adim('late', 'old job'))
            self.assertIsNone(ri.akis_durumu('eski'))
        self.assertEqual(ri.akis_durumu('yeni')['adim'], 'mesh')
        devam.set()
        self.assertTrue(dur.wait(2))
        th.join()
        if hatalar:
            raise hatalar[0]
        self.assertIsNone(ri.akis_durumu('yeni'))

    def test_baska_thread_sahibin_adimina_dokunamaz(self):
        results = []
        with ri.is_akisi('a'):
            th = threading.Thread(target=lambda: results.append(ri.adim('wrong', 'wrong')))
            th.start(); th.join()
            self.assertEqual(results, [False])
            self.assertEqual(ri.akis_durumu('a')['adim'], 'cerceve')

    def test_hata_kaydi_temizler_ve_sayac_dogrulanir(self):
        with self.assertRaisesRegex(RuntimeError, 'failure'):
            with ri.is_akisi('a'):
                for done, total in ((-1, 2), (3, 2), (True, 2), (1, None)):
                    with self.assertRaises(ValueError):
                        ri.adim('x', 'x', tamamlanan=done, toplam=total)
                raise RuntimeError('failure')
        self.assertIsNone(ri.akis_durumu('a'))
        self.assertFalse(ri.adim('x', 'x'))

    def test_obj_kopyasi_gercek_bayti_olcer_kaynak_degismaz(self):
        with tempfile.TemporaryDirectory() as td:
            src, dst = Path(td) / 'input.obj', Path(td) / 'output.obj'
            raw = b'g floor\nv 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n'
            src.write_bytes(raw)
            with ri.is_akisi('a'):
                render_mesh._obj_hazirla(src, dst)
                d = ri.akis_durumu('a')
                self.assertEqual((d['tamamlanan'], d['toplam'], d['birim']), (len(raw), len(raw), 'bayt'))
                self.assertIsNone(d['genel_yuzde'])
            self.assertEqual(src.read_bytes(), raw)
            self.assertIn(b'usemtl floor', dst.read_bytes())

    def test_obj_kopya_iptali_yeni_surec_baslatmadan_cikar(self):
        with tempfile.TemporaryDirectory() as td:
            src, dst = Path(td) / 'input.obj', Path(td) / 'output.obj'
            src.write_bytes(b'v 0 0 0\n' * 140000)
            with patch.object(render_mesh.surecler, 'iptal_kontrol',
                              side_effect=[None, render_mesh.surecler.IslemIptal('iptal')]):
                with self.assertRaises(render_mesh.surecler.IslemIptal):
                    render_mesh._obj_hazirla(src, dst)
            self.assertLess(dst.stat().st_size, src.stat().st_size)


if __name__ == '__main__':
    unittest.main()
