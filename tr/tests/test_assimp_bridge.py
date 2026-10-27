# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Küçük sentetik dönüşümler: güvenli argv, geometri ve atomik teslim."""
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import warnings

ROOT = Path(__file__).resolve().parents[1]
from core import assimp_bridge as assimp_kopru
from core import processes as surecler

V = 'v 0 0 0\nv 1 0 0\nv 0 1 0\n'
OBJ = V + 'f 1 2 3\n'
MTL = 'newmtl kaynak\nKd .2 .4 .6\n'


class AssimpKopruTest(unittest.TestCase):
    def setUp(self):
        (ROOT / 'tests/.tmp').mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / 'tests/.tmp', prefix='assimp kopru ')
        self.pd = Path(self.tmp.name)
        self.kaynak = self.pd / 'kaynak.fbx'
        self.kaynak.write_bytes(b'sentetik kaynak')
        self.hedef = self.pd / 'hedef.obj'
        self.hedef.write_bytes(b'onceki hedef')

    def tearDown(self):
        self.tmp.cleanup()

    def taklit(self, obj=OBJ, mtl=None, kod=0, extra=None):
        def run(cmd, **kw):
            self.assertIsInstance(cmd, list)
            self.assertEqual(cmd[1], 'export')
            self.assertEqual(cmd[2], str(self.kaynak))
            self.assertEqual(cmd[4:], ['-fobj'])
            self.assertNotIn('shell', kw)
            p = Path(cmd[3])
            self.assertEqual(p.parent.parent, self.hedef.parent)
            self.assertEqual(kw['cwd'], str(p.parent))
            if obj is not None:
                p.write_text(obj, encoding='utf-8')
            if mtl is not None:
                p.with_suffix('.mtl').write_text(mtl, encoding='utf-8')
            if extra:
                extra(p)
            return subprocess.CompletedProcess(cmd, kod, '', 'sentetik hata' if kod else '')
        return run

    def cevir(self, run, timeout=120):
        with patch.object(assimp_kopru.shutil, 'which', return_value='/sentetik/assimp'), \
             patch.object(surecler, 'calistir', side_effect=run) as cli:
            sonuc = assimp_kopru.cevir(self.kaynak, self.hedef, timeout)
        return sonuc, cli

    def mtl(self):
        ref = next(s[7:] for s in self.hedef.read_text().splitlines() if s.startswith('mtllib '))
        return self.hedef.parent / ref

    def eski_korundu(self):
        self.assertEqual(self.hedef.read_bytes(), b'onceki hedef')
        self.assertFalse(list(self.pd.glob('.assimp_*')))

    def test_argv_ozel_karakter_ve_yeni_hedef(self):
        self.kaynak = self.pd / 'girdi $(touch KACAK) `touch KACAK2` "\'.fbx'
        self.kaynak.write_bytes(b'sentetik')
        self.hedef = self.pd / 'alt dizin' / 'cikti $ ` "\'.obj'
        sonuc, cli = self.cevir(self.taklit(), timeout=300)
        self.assertEqual(sonuc, str(self.hedef))
        self.assertEqual(self.hedef.read_text(), OBJ)
        self.assertEqual(cli.call_args.kwargs['timeout'], 300)
        self.assertFalse((self.pd / 'KACAK').exists())
        self.assertFalse(list(self.hedef.parent.glob('.assimp_*')))

    def test_negatif_vertex_uv_normal_indisleri_o_anin_kayitlarina_bakar(self):
        obj = (V + 'vt 0 0\nvt 1 0\nvt 0 1\nvn 0 0 1\n'
               'f -3/-3/-1 -2/-2/-1 -1/-1/-1\nv 99 99 99\n')
        self.cevir(self.taklit(obj))
        self.assertEqual(self.hedef.read_text(), obj)

    def test_v_bolme_vn_ve_kucuk_gercek_ucgen_korunur(self):
        obj = 'v 0 0 0\nv 1e-200 0 0\nv 0 1e-200 0\nvn 0 0 1\nf 1//1 2//1 3//1\n'
        self.cevir(self.taklit(obj))
        self.assertEqual(self.hedef.read_text(), obj)

    def test_basarisiz_timeout_ve_yeni_cikti_yok_eski_hedefi_korur(self):
        def timeout(*args, **kw):
            raise RuntimeError('İşlem zaman aşımına uğradı')
        for run in (self.taklit(kod=1), self.taklit(None), timeout):
            with self.subTest(run=run), self.assertRaises(RuntimeError):
                self.cevir(run)
            self.eski_korundu()

    def test_bozuk_geometri_asla_teslim_edilmez(self):
        bozuk = [V, V+'f 1 2\n', V+'f 0 2 3\n', V+'f 1 2 4\n',
                 V+'f -4 -2 -1\n', V+'f 1 2 x\n', V+'f 1/ 2 3\n',
                 V+'f 1// 2 3\n', V+'f 1/1 2/1 3/1\n',
                 V+'f 1//1 2//1 3//1\n', V+'f 1/1/1/1 2 3\n',
                 V+'f 1 1 3\n', V.replace('1 0 0', 'nan 0 0')+'f 1 2 3\n',
                 V.replace('1 0 0', 'inf 0 0')+'f 1 2 3\n',
                 V.replace('v 0 1 0', 'v 2 0 0')+'f 1 2 3\n',
                 V.replace('v 0 1 0', 'v 0 1')+'f 1 2 3\n',
                 V+'f 1 2 3\\', V.replace('v 0 1 0', 'v 0 1 0 2')+'f 1 2 3\n']
        for obj in bozuk:
            with self.subTest(obj=obj), self.assertRaises(RuntimeError):
                self.cevir(self.taklit(obj))
            self.eski_korundu()

    def test_ayni_dosya_sert_ve_sembolik_baglanti_isleme_girmez(self):
        hard = self.pd / 'sert.obj'; os.link(self.kaynak, hard)
        link = self.pd / 'bag.obj'; link.symlink_to(self.kaynak.name)
        for hedef in (self.kaynak, hard, link):
            with self.subTest(hedef=hedef), patch.object(surecler, 'calistir') as cli:
                with self.assertRaises(ValueError):
                    assimp_kopru.cevir(self.kaynak, hedef)
                cli.assert_not_called()
        self.assertEqual(self.kaynak.read_bytes(), b'sentetik kaynak')

    def test_kaynak_ve_timeout_kontrolu(self):
        for timeout in (0, -1, float('inf'), float('nan'), '120'):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                assimp_kopru.cevir(self.kaynak, self.hedef, timeout)
        with self.assertRaisesRegex(RuntimeError, 'kaynak'):
            assimp_kopru.cevir(self.pd / 'yok.fbx', self.hedef)
        self.eski_korundu()

    def test_mtl_ve_unicode_doku_yollari_secenekleri_korur(self):
        doku = self.pd / 'ışıklı doku.png'; doku.write_bytes(b'sentetik doku baytlari')
        obj = 'mtllib model.mtl\nusemtl kaynak\n' + OBJ
        line = 'map_Kd -s 1 2 3 -o .1 .2 -bm .5 "ışıklı doku.png"\n'
        self.cevir(self.taklit(obj, MTL+line))
        mtl = self.mtl()
        self.assertEqual(mtl.parent.parent, self.pd)
        self.assertTrue(mtl.parent.name.startswith('assimp_assets_'))
        mapline = next(s for s in mtl.read_text().splitlines() if s.startswith('map_Kd'))
        self.assertTrue(mapline.startswith('map_Kd -s 1 2 3 -o .1 .2 -bm .5 texture_'))
        self.assertEqual((mtl.parent / mapline.split()[-1]).read_bytes(), doku.read_bytes())
        self.assertIn('Kd .2 .4 .6', mtl.read_text())

    def test_eksik_dis_yol_symlink_belirsiz_map_korunur_okunmaz(self):
        dis = '/assimp_test_outside_clone/texture.png'
        (self.pd / 'disbag').symlink_to('/assimp_test_outside_clone', target_is_directory=True)
        lines = ['map_Kd ' + dis, r'map_Ks C:\sample_machine\texture.jpg',
                 'map_bump disbag/texture.png', 'map_Kd yok.png',
                 'map_Kd -bilinmeyen 9 texture.png',
                 'map_Kd ../../../../../../assimp_test_outside_clone/texture.png']
        obj = 'mtllib model.mtl\nusemtl kaynak\n'+OBJ
        orig_stat = Path.stat
        def stat(p, *args, **kw):
            self.assertFalse(str(p).startswith('/assimp_test_outside_clone'))
            return orig_stat(p, *args, **kw)
        with patch.object(Path, 'stat', stat), warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            self.cevir(self.taklit(obj, MTL+'\n'.join(lines)+'\n'))
        text = self.mtl().read_text()
        for line in lines:
            self.assertIn(line+'\n', text)
        self.assertEqual(len(caught), 1)
        self.assertIn('6 doku', str(caught[0].message))
        self.assertEqual(text.count('original reference preserved'), 6)

    def test_uyari_hata_sayilsa_da_hedef_korunur(self):
        with warnings.catch_warnings():
            warnings.simplefilter('error', RuntimeWarning)
            with self.assertRaises(RuntimeWarning):
                self.cevir(self.taklit('mtllib model.mtl\n'+OBJ, MTL+'map_Kd yok.png\n'))
        self.eski_korundu()

    def test_mtl_eksik_dis_yol_ve_tanimsiz_malzeme_reddedilir(self):
        for obj, mtl in [('mtllib yok.mtl\n'+OBJ, None),
                         ('mtllib /sample/model.mtl\n'+OBJ, None),
                         ('mtllib ../model.mtl\n'+OBJ, None),
                         ('usemtl eksik\n'+OBJ, None),
                         ('mtllib model.mtl\nusemtl eksik\n'+OBJ, MTL),
                         ('mtllib model.mtl\n'+OBJ, 'newmtl\n')]:
            with self.subTest(obj=obj), self.assertRaises(RuntimeError):
                self.cevir(self.taklit(obj, mtl))
            self.eski_korundu()

    def test_symlink_obj_ve_mtl_ciktisi_reddedilir(self):
        for kind in ('obj', 'mtl'):
            def extra(p):
                q = p if kind == 'obj' else p.with_suffix('.mtl')
                q.unlink(missing_ok=True)
                q.symlink_to(self.kaynak)
            with self.subTest(kind=kind), self.assertRaises(RuntimeError):
                self.cevir(self.taklit('mtllib model.mtl\n'+OBJ, MTL, extra=extra))
            self.eski_korundu()

    def test_icerik_adresli_varlik_tekrar_kullanilir_eski_malzeme_degismez(self):
        obj = 'mtllib model.mtl\nusemtl kaynak\n'+OBJ
        self.cevir(self.taklit(obj, MTL))
        eski = self.mtl(); oldbytes = eski.read_bytes()
        self.cevir(self.taklit(obj, MTL))
        self.assertEqual(self.mtl(), eski)
        self.assertEqual(len(list(self.pd.glob('assimp_assets_*'))), 1)
        self.cevir(self.taklit(obj, MTL.replace('.2 .4 .6', '.8 .4 .1')))
        self.assertNotEqual(self.mtl(), eski)
        self.assertEqual(eski.read_bytes(), oldbytes)

    def test_bozulmus_varlik_dizini_sessizce_ezilmez(self):
        obj = 'mtllib model.mtl\nusemtl kaynak\n'+OBJ
        self.cevir(self.taklit(obj, MTL))
        mtl = self.mtl(); mtl.write_bytes(b'degistirilmis')
        self.hedef.write_bytes(b'onceki hedef')
        with self.assertRaisesRegex(RuntimeError, 'içeriği'):
            self.cevir(self.taklit(obj, MTL))
        self.eski_korundu()
        self.assertEqual(mtl.read_bytes(), b'degistirilmis')

    def test_atomik_obj_teslimi_basarisizsa_eski_obj_ve_mtl_korunur(self):
        obj = 'mtllib model.mtl\nusemtl kaynak\n'+OBJ
        self.cevir(self.taklit(obj, MTL))
        eski_mtl = self.mtl()
        eski_malzeme = eski_mtl.read_bytes()
        self.hedef.write_bytes(b'onceki hedef')
        with patch.object(assimp_kopru.os, 'replace', side_effect=OSError('sentetik teslim hatası')):
            with self.assertRaisesRegex(OSError, 'teslim'):
                self.cevir(self.taklit(obj, MTL.replace('.2 .4 .6', '.8 .8 .8')))
        self.eski_korundu()
        self.assertEqual(eski_mtl.read_bytes(), eski_malzeme)

    def test_birden_fazla_mtl_ve_satir_devami_korunur(self):
        obj = ('mtllib model.mtl "ikinci malzeme.mtl"\nusemtl ikinci\n'+V+
               'f 1 2 \\\n3\n')
        def extra(p):
            (p.parent / 'ikinci malzeme.mtl').write_text('newmtl ikinci\nKd .9 .8 .7\n')
        self.cevir(self.taklit(obj, MTL, extra=extra))
        text = self.hedef.read_text()
        self.assertIn('f 1 2  3\n', text)
        refs = [s[7:] for s in text.splitlines() if s.startswith('mtllib ')]
        self.assertEqual(len(refs), 2)
        self.assertTrue(all((self.pd / ref).is_file() for ref in refs))

    def test_iptal_yeni_ciktiyi_teslim_etmez(self):
        run = self.taklit()
        def iptal_hatasi():
            raise RuntimeError('İşlem iptal edildi')
        def iptal(cmd, **kw):
            sonuc = run(cmd, **kw)
            surecler.iptal_kontrol = iptal_hatasi
            return sonuc
        with patch.object(surecler, 'iptal_kontrol', wraps=surecler.iptal_kontrol):
            with self.assertRaisesRegex(RuntimeError, 'iptal'):
                self.cevir(iptal)
        self.eski_korundu()

    def test_gercek_surec_zaman_asimi_mevcut_hedefi_korur(self):
        cli = self.pd / 'geciken assimp'
        # kernel shebang'deki boşluklu venv path'ini böler, argv'yi güvenli geçir.
        cli.write_text('#!/bin/sh\nexec '+shlex.quote(sys.executable)+
                       " -c 'import time; time.sleep(10)'\n")
        cli.chmod(0o700)
        with patch.object(assimp_kopru.shutil, 'which', return_value=str(cli)):
            with self.assertRaisesRegex(RuntimeError, 'zaman aşımı'):
                assimp_kopru.cevir(self.kaynak, self.hedef, timeout=.05)
        self.eski_korundu()
        self.assertFalse(surecler._aktif)

    @unittest.skipUnless(shutil.which('assimp'), 'Yerel Assimp CLI gerekli')
    def test_gercek_assimp_kucuk_stl_ozel_ad_ile(self):
        self.kaynak = self.pd / 'ucgen $(touch KACAK) `touch KACAK2` "\'.stl'
        self.kaynak.write_text('solid test\nfacet normal 0 0 1\nouter loop\n'
                               'vertex 0 0 0\nvertex 1 0 0\nvertex 0 1 0\n'
                               'endloop\nendfacet\nendsolid test\n')
        sonuc = assimp_kopru.cevir(self.kaynak, self.hedef)
        self.assertEqual(sonuc, str(self.hedef))
        self.assertTrue(assimp_kopru._obj_dogrula(self.hedef))
        self.assertTrue(self.mtl().is_file())
        self.assertFalse((self.pd / 'KACAK').exists())
        self.assertFalse((self.pd / 'KACAK2').exists())


if __name__ == '__main__':
    unittest.main()
