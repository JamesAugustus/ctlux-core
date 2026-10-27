# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Sahne bağımlılıkları, yarım çıktı ve iptal için gerçek, küçük regression'lar."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
from core import engine as motor
from core import render_cache as render_onbellek
from core import processes as surecler
motor.ortam_hazirla()


class DosyaliTest(unittest.TestCase):
    def setUp(self):
        (ROOT / 'tests/.tmp').mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / 'tests/.tmp', prefix='cache deneyi ')
        self.pd = Path(self.tmp.name)
        for d in ('model', 'malzeme', 'doku', 'isik', 'gok', 'gorunum', '_cache', 'render', 'cikti'):
            (self.pd / d).mkdir(exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()


class SurecTest(DosyaliTest):
    def test_sahne_turetilmis_kayitlari_eksik_bozuk_ve_sinir_disi_olamaz(self):
        octree = self.pd / '_cache/sahne.oct'; octree.write_bytes(b'scene' * 30)
        mesh = self.pd / '_cache/mesh.rtm'; mesh.write_bytes(b'mesh' * 30)
        kayit = self.pd / '_cache/sahne.oct.json'
        sha = render_onbellek.dosya_ozeti(octree)
        dep = {'yol': '_cache/mesh.rtm', 'sha256': render_onbellek.dosya_ozeti(mesh)}
        for deps in (None, {}, 'bozuk', [None], [{}], [{'yol': '../mesh.rtm', 'sha256': dep['sha256']}],
                     [{'yol': str(mesh), 'sha256': dep['sha256']}], [{'yol': None, 'sha256': dep['sha256']}]):
            with self.subTest(deps=deps):
                kayit.write_text(json.dumps({'sha256': sha, 'turetilmis': deps}))
                self.assertTrue(render_onbellek.gecerli_cikti(octree, kayit))
                self.assertFalse(render_onbellek.gecerli_sahne(self.pd, octree, kayit))
        kayit.write_text(json.dumps({'sha256': sha}))
        self.assertFalse(render_onbellek.gecerli_sahne(self.pd, octree, kayit))
        kayit.write_text(json.dumps({'sha256': sha, 'turetilmis': [dep]}))
        self.assertTrue(render_onbellek.gecerli_sahne(self.pd, octree, kayit))
        mesh.write_bytes(b'fake' * 30)
        self.assertFalse(render_onbellek.gecerli_sahne(self.pd, octree, kayit))
        mesh.unlink()
        self.assertFalse(render_onbellek.gecerli_sahne(self.pd, octree, kayit))
        kayit.write_text(json.dumps({'sha256': sha, 'turetilmis': []}))
        self.assertTrue(render_onbellek.gecerli_sahne(self.pd, octree, kayit))

    def test_yanlis_tur_kayit_bozuk_onbellektir(self):
        p = self.pd / 'cikti.bin'; p.write_bytes(b'x' * 100)
        kayit = self.pd / 'kayit.json'
        for veri in (None, [], 123, 'yanlis', {}):
            kayit.write_text(json.dumps(veri))
            self.assertFalse(render_onbellek.gecerli_cikti(p, kayit))

    def test_ikili_stdout_baytlari_degismez(self):
        p = self.pd / 'ikili'
        r = surecler.calistir([sys.executable, '-c',
            'import sys;sys.stdout.buffer.write(bytes(range(256)))'], stdout_yolu=p)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(p.read_bytes(), bytes(range(256)))
        self.assertEqual(r.stdout, '')

    def test_satir_ortasindaki_komut_rad_dosyasini_dinamik_yapar(self):
        rad = self.pd / 'model/orta.rad'
        for metin, dinamik in (('void plastic m 0 0 5 .5 .5 .5 0 0\n', False),
                               ('void plastic m 0 0 5 .5 .5 .5 0 0 !echo x\n', True),
                               ('\tm polygon p !cat parca.rad\n', True),
                               # 1 MiB parçalarla okunur. İlk parçadan sonraki işaret de bulunur.
                               ('#' * (1 << 20) + '\n!echo x\n', True)):
            with self.subTest(metin=metin):
                rad.write_text(metin)
                self.assertEqual(render_onbellek.dinamik_rad_var([(str(rad), render_onbellek.dosya_ozeti(rad))]), dinamik)

    def test_baslatma_hatasinda_aktif_surec_kalmaz(self):
        with self.assertRaises(FileNotFoundError):
            surecler.calistir([str(self.pd / 'yok')], stdout_yolu=self.pd / 'out')
        self.assertFalse(surecler._aktif)


@unittest.skipUnless(all(shutil.which(x) for x in ('oconv', 'obj2rad', 'obj2mesh', 'rpict')),
                     'Yerel Radiance gerekli')
class SahneTest(DosyaliTest):
    def setUp(self):
        super().setUp()
        self.mat = self.pd / 'malzeme/ozel.rad'
        self.mat.write_text('void plastic beyaz\n0\n0\n5 .5 .5 .5 0 0\n')
        self.obj = self.pd / 'model/a.obj'
        self.obj.write_text('usemtl beyaz\nv -2 -2 0\nv 2 -2 0\nv 0 2 0\nf 1 2 3\n')
        self.p = {'geometri': [{'dosya': 'model/a.obj', 'mesh_render': True}],
                  'malzeme': 'malzeme/ozel.rad', 'armaturler': []}
        self.gok = 'void glow gok\n0\n0\n4 1 1 1 0\ngok source dome\n0\n0\n4 0 0 1 180\n'

    def derle(self, p=None):
        return motor.sahne_derle(str(self.pd), p or self.p, self.gok)

    def test_kamera_ve_cozunurluk_sahneyi_tekrar_derlemez(self):
        with patch.object(surecler, 'calistir', wraps=surecler.calistir) as run:
            ilk = self.derle()
            p = copy.deepcopy(self.p); p.update(hedef=[1, 2, 3], coz='320x240')
            self.assertEqual(ilk, self.derle(p))
        self.assertEqual(sum(c.args[0][0] == 'oconv' for c in run.call_args_list), 1)
        self.assertEqual(len(list((self.pd / '_cache').glob('*.rtm'))), 1)

    def test_parcali_sahne_alt_meshleri_dogrular_ve_bellege_sayar(self):
        import json; from core import render_mesh; from core import render_parallel as render_paralel
        with patch.object(render_mesh, '_PARCA_ESIK', 0):
            octree = self.derle()
            meta = json.loads(Path(octree + '.json').read_text())
            self.assertGreaterEqual(len(meta['turetilmis']), 2)
            expected = Path(octree).stat().st_size + sum((self.pd / x['yol']).stat().st_size for x in meta['turetilmis'])
            self.assertEqual(render_paralel._sahne_boyutu(octree), expected)
            part = next(self.pd / x['yol'] for x in meta['turetilmis'] if x['yol'].endswith('.rtm'))
            part.unlink()
            self.assertFalse(render_onbellek.gecerli_sahne(str(self.pd), octree, octree + '.json'))
            self.assertEqual(self.derle(), octree)
            self.assertTrue(render_onbellek.gecerli_sahne(str(self.pd), octree, octree + '.json'))

    def test_sahne_gecerliyken_mesh_silinmesi_ve_bozulmasi_onarilir(self):
        ilk = self.derle()
        mesh = next((self.pd / '_cache').glob('mesh_*.rtm'))
        for tur in ('bozuk', 'eksik'):
            with self.subTest(tur=tur):
                if tur == 'bozuk':
                    ham = bytearray(mesh.read_bytes()); ham[-1] ^= 1
                    mesh.write_bytes(ham)
                else:
                    mesh.unlink()
                self.assertTrue(render_onbellek.gecerli_cikti(ilk, ilk + '.json'))
                self.assertFalse(render_onbellek.gecerli_sahne(self.pd, ilk, ilk + '.json'))
                with patch.object(surecler, 'calistir', wraps=surecler.calistir) as run:
                    self.assertEqual(self.derle(), ilk)
                self.assertTrue(any(c.args[0][0] == 'obj2mesh' for c in run.call_args_list))
                self.assertTrue(any(c.args[0][0] == 'oconv' for c in run.call_args_list))
                self.assertTrue(render_onbellek.gecerli_sahne(self.pd, ilk, ilk + '.json'))
                hdr = self.pd / 'render/onarim.hdr'
                motor.render(ilk, '-vp 0 0 1 -vd 0 0 -1 -vu 0 1 0', 'onizleme', 8, 8, str(hdr))
                self.assertGreater(hdr.stat().st_size, 64)

    def test_malzeme_ve_geometri_degisiminde_yeni_sahne(self):
        ilk = self.derle(); eski = Path(ilk).read_bytes()
        self.mat.write_text(self.mat.read_text().replace('.5 .5 .5', '.2 .2 .2'))
        ikinci = self.derle()
        self.assertNotEqual(ilk, ikinci)
        self.obj.write_text(self.obj.read_text().replace('2 -2', '3 -2'))
        self.assertNotEqual(ikinci, self.derle())
        self.assertEqual(Path(ilk).read_bytes(), eski)

    def test_harici_veri_ambient_anahtarini_degistirir(self):
        yan = self.pd / 'isik/dagilim.dat'; yan.write_text('1')
        ilk = self.derle()
        yan.write_text('2')
        yeni = self.derle()
        self.assertNotEqual(ilk, yeni)
        self.assertNotEqual(motor._ambient_dosyasi(ilk), motor._ambient_dosyasi(yeni))

    def test_bozuk_octree_ve_basarisiz_derleme_kullanilmaz(self):
        ilk = self.derle()
        Path(ilk).write_bytes(b'yarim')
        with patch.object(surecler, 'calistir', wraps=surecler.calistir) as run:
            self.assertEqual(ilk, self.derle())
        self.assertTrue(any(c.args[0][0] == 'oconv' for c in run.call_args_list))
        self.assertTrue(render_onbellek.gecerli_cikti(ilk, ilk + '.json'))
        self.mat.write_text(self.mat.read_text() + '\n# Yeni girdi\n')
        def bozuk(cmd, **kw):
            Path(kw['stdout_yolu']).write_bytes(b'yarim')
            return subprocess.CompletedProcess(cmd, 1, '', 'sentetik derleme hatasi')
        with patch.object(motor, '_geo_satiri', return_value=''), patch.object(surecler, 'calistir', side_effect=bozuk):
            with self.assertRaisesRegex(RuntimeError, 'oconv'):
                self.derle()
        self.assertEqual(len(list((self.pd / '_cache').glob('sahne_*.oct'))), 1)
        self.assertFalse(list((self.pd / '_cache').glob('*.tmp')))

    def test_kucuk_obj_ayni_mtime_ile_degisse_de_eski_rad_kullanilmaz(self):
        ilk = motor._obj_rad(str(self.pd), str(self.obj))
        st = self.obj.stat()
        self.obj.write_text(self.obj.read_text().replace('2 -2', '3 -2'))
        os.utime(self.obj, ns=(st.st_atime_ns, st.st_mtime_ns))
        yeni = motor._obj_rad(str(self.pd), str(self.obj))
        self.assertNotEqual(ilk, yeni)

    def test_dinamik_rad_komutu_eski_octree_kullanmaz(self):
        kaynak = self.pd / 'model/dinamik.rad'
        kaynak.write_text("!printf '# Degisken kaynak deneyi\\n'\n")
        self.p['geometri'].append({'dosya': 'model/dinamik.rad'})
        self.assertNotEqual(self.derle(), self.derle())

    def test_satir_ortasindaki_rad_komutu_eski_octree_kullanmaz(self):
        kaynak = self.pd / 'model/dinamik.rad'
        kaynak.write_text("void plastic gri 0 0 5 .5 .5 .5 0 0 !printf '# Degisken kaynak deneyi\\n'\n")
        self.p['geometri'].append({'dosya': 'model/dinamik.rad'})
        self.assertNotEqual(self.derle(), self.derle())

    def test_ozel_klasordeki_isik_degisiminde_sahne_yenilenir(self):
        ozel = self.pd / 'ozel'; ozel.mkdir()
        rad = ozel / 'isik.rad'
        rad.write_text('void light lamba\n0\n0\n3 1 1 1\nlamba sphere kure\n0\n0\n4 0 0 2 .1\n')
        self.p['isiklar'] = ['ozel/isik.rad']
        ilk = motor.sahne_derle(str(self.pd), self.p, self.gok, gece_isiklari=True)
        rad.write_text(rad.read_text().replace('3 1 1 1', '3 2 2 2'))
        yeni = motor.sahne_derle(str(self.pd), self.p, self.gok, gece_isiklari=True)
        self.assertNotEqual(ilk, yeni)

    def test_obj_donusum_sirasinda_degismisse_yayinlanmaz(self):
        def degistir(cmd, **kw):
            self.obj.write_text(self.obj.read_text() + '# Degisti\n')
            Path(kw['stdout_yolu']).write_bytes(b'x' * 100)
            return subprocess.CompletedProcess(cmd, 0, '', '')
        with patch.object(surecler, 'calistir', side_effect=degistir):
            with self.assertRaisesRegex(RuntimeError, 'kaynak değişti'):
                motor._obj_rad(str(self.pd), str(self.obj))
        self.assertFalse(list((self.pd / '_cache').glob('geo_*')))

    def test_rpict_hatasi_eski_hdr_korur(self):
        octp = self.derle(); hdr = self.pd / 'render/sonuc.hdr'; hdr.write_bytes(b'eski')
        def bozuk(cmd, **kw):
            Path(kw['stdout_yolu']).write_bytes(b'yarim')
            return subprocess.CompletedProcess(cmd, 1, '', 'sentetik rpict hatasi')
        with patch.object(surecler, 'calistir', side_effect=bozuk):
            with self.assertRaisesRegex(RuntimeError, 'rpict'):
                motor.render(octp, '-vp 0 0 1 -vd 0 0 -1 -vu 0 1 0', 'onizleme', 16, 16, str(hdr))
        self.assertEqual(hdr.read_bytes(), b'eski')
        self.assertFalse(list(hdr.parent.glob('*.tmp')))

    def test_onizleme_dolayli_aydinlatmayi_kapatir_analizi_reddeder(self):
        q = motor.KALITE['onizleme'].split()
        self.assertEqual(q[q.index('-ab') + 1], '0')
        with self.assertRaisesRegex(ValueError, 'analiz'):
            motor.render('unused', '', 'onizleme', irradiance=True)


if __name__ == '__main__':
    unittest.main()
