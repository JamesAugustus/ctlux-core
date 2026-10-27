# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Mesh dönüşümünde OBJ malzemesi, cache ve atomik tamamlanma davranışı."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
from core import engine as motor
motor.ortam_hazirla()
from core import render_mesh
from core import processes as surecler


class _MeshOrtam:
    def setUp(self):
        (ROOT / "tests/.tmp").mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix="mesh deneyi ", dir=ROOT / "tests/.tmp")
        self.proje = Path(self.tmp.name)
        self.obj = self.proje / "model bosluklu.obj"
        self.mat = self.proje / "malzeme bosluklu.rad"
        self.mat.write_text("""void plastic white
0
0
5 .55 .55 .55 0 0
void plastic red
0
0
5 .8 .1 .1 0 0
void plastic green
0
0
5 .1 .7 .1 0 0
""")

    def tearDown(self):
        self.tmp.cleanup()

    def obj_yaz(self, secimler):
        metin = ""
        for i, secim in enumerate(secimler):
            x = 2 * i
            metin += (secim + f"v {x} 0 0\nv {x+1} 0 0\nv {x} 1 0\n"
                      f"f {3*i+1} {3*i+2} {3*i+3}\n")
        self.obj.write_text(metin)

    def derle(self):
        return render_mesh.derle(self.proje, self.obj, self.mat)


class MeshDosyaTest(_MeshOrtam, unittest.TestCase):
    def test_sayisal_kayitlar_ve_kaynak_korunur(self):
        ham = (b"# untouched\r\no sample\r\nv 1e-4 0 0\r\nv 1 0 0\r\n"
               b"v 0 1 0\r\nvt .1 .2\r\nvn 0 0 1\r\ng red\r\n"
               b"f -3/1/1 -2/1/1 -1/1/1\r\n")
        self.obj.write_bytes(ham)
        out = self.proje / "temporary.obj"
        self.assertEqual(render_mesh._obj_hazirla(self.obj, out), hashlib.sha256(ham).hexdigest())
        self.assertEqual(self.obj.read_bytes(), ham)
        kalan = b"".join(l for l in out.read_bytes().splitlines(keepends=True)
                         if not l.startswith(b"usemtl "))
        self.assertEqual(kalan, ham)

    def test_basarisiz_derleme_yarim_cache_birakmaz(self):
        self.obj_yaz([""])
        def hata(cmd, **kwargs):
            (Path(kwargs["cwd"]) / cmd[-1]).write_bytes(b"yarim mesh")
            return subprocess.CompletedProcess(cmd, 1, "", "synthetic compiler failure")
        with patch.object(surecler, "calistir", side_effect=hata):
            with self.assertRaisesRegex(RuntimeError, "synthetic compiler failure"):
                self.derle()
        self.assertEqual(list((self.proje / "_cache").iterdir()), [])

    def test_sifir_kodlu_gecersiz_cikti_kabul_edilmez(self):
        self.obj_yaz([""])
        def sahte(cmd, **kwargs):
            (Path(kwargs["cwd"]) / cmd[-1]).write_bytes(b"not a mesh" * 100)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        with patch.object(surecler, "calistir", side_effect=sahte):
            with self.assertRaisesRegex(RuntimeError, "biçiminde değil"):
                self.derle()
        self.assertEqual(list((self.proje / "_cache").iterdir()), [])


@unittest.skipUnless(all(shutil.which(t) for t in ("obj2mesh", "obj2rad", "oconv", "rtrace")),
                     "Yerel Radiance kurulumu gerekli")
class GercekMeshTest(_MeshOrtam, unittest.TestCase):
    def izle(self, rel, adet, alan="M"):
        scene = self.proje / "scene.rad"
        tip = "instance" if rel.endswith(".oct") else "mesh"
        scene.write_text("void " + tip + " model\n1 " + rel + "\n0\n0\n")
        p = subprocess.run(["oconv", "-f", "scene.rad"], cwd=self.proje,
                           capture_output=True, check=True, timeout=2, env=motor.radiance_ortami())
        octree = self.proje / "scene.oct"
        octree.write_bytes(p.stdout)
        rays = "".join(f"{2*i+.2} .2 1 0 0 -1\n" for i in range(adet))
        r = subprocess.run(["rtrace", "-h", "-ab", "0", "-av", "1", "1", "1",
                            "-o" + alan, "scene.oct"], cwd=self.proje, input=rays,
                           text=True, capture_output=True, check=True, timeout=2, env=motor.radiance_ortami())
        return [ln.strip() for ln in r.stdout.splitlines()]

    def obj2rad_malzemeleri(self):
        r = subprocess.run(["obj2rad", str(self.obj)], cwd=self.proje,
                           text=True, capture_output=True, check=True, timeout=2, env=motor.radiance_ortami())
        return [ln.split()[0] for ln in r.stdout.splitlines() if " polygon " in ln]

    def malzeme_dogrula(self, secimler, beklenen):
        self.obj_yaz(secimler)
        once = self.obj.read_bytes()
        self.assertEqual(self.obj2rad_malzemeleri(), beklenen)
        self.assertEqual(self.izle(self.derle(), len(secimler)), beklenen)
        self.assertEqual(self.obj.read_bytes(), once)

    def test_yalniz_o_ve_atanmamis_yuzey_white(self):
        self.malzeme_dogrula(["", "o part\n"], ["white", "white"])

    def test_grup_ilk_adi_ve_bos_grup(self):
        self.malzeme_dogrula(["g red green\n", "g green\n", "g\n"],
                            ["red", "green", "white"])

    def test_usemtl_sonraki_gruptan_etkilenmez(self):
        self.malzeme_dogrula(["usemtl red\n", "g green\n", "usemtl\n"],
                            ["red", "red", "red"])

    def test_karisik_atanmamis_grup_ve_acik_malzeme(self):
        self.malzeme_dogrula(["", "g red\n", "g\n", "usemtl green\n", "g red\n"],
                            ["white", "red", "white", "green", "green"])

    def test_satir_devamli_grup(self):
        self.malzeme_dogrula(["g \\\nred green\n", "g\n"], ["red", "white"])

    def test_cache_hit_ve_kaynak_malzeme_degisimi(self):
        self.obj_yaz([""])
        with motor.render_ilerleme.is_akisi('mesh-test'):
            ilk = self.derle()
            self.assertFalse(motor.render_ilerleme.akis_durumu('mesh-test')['onbellek']['mesh'])
        with patch.object(surecler, "calistir", side_effect=AssertionError("Tekrar derlenmemeli")):
            with motor.render_ilerleme.is_akisi('mesh-test'):
                self.assertEqual(self.derle(), ilk)
                d = motor.render_ilerleme.akis_durumu('mesh-test')
                self.assertTrue(d['onbellek']['mesh'])
                self.assertEqual(d['adim'], 'mesh_hazir')
        self.mat.write_text(self.mat.read_text().replace(".55 .55 .55", ".2 .3 .4"))
        renkli = self.derle()
        self.assertNotEqual(ilk, renkli)
        self.assertEqual([float(x) for x in self.izle(ilk, 1, "v")[0].split()], [.55, .55, .55])
        self.assertEqual([float(x) for x in self.izle(renkli, 1, "v")[0].split()], [.2, .3, .4])
        self.obj.write_text(self.obj.read_text().replace("v 1 0 0", "v 2 0 0"))
        self.assertNotEqual(self.derle(), renkli)

    def test_dinamik_malzeme_komutu_dis_veri_degisince_eski_mesh_kullanmaz(self):
        self.obj_yaz([''])
        dis_veri = self.proje / 'external_material.rad'
        self.mat.write_text('!cat external_material.rad\n')
        mat_kaynagi = self.mat.read_bytes()
        dis_veri.write_text('void plastic white\n0\n0\n5 .2 .3 .4 0 0\n')
        ilk = self.derle()
        dis_veri.write_text('void plastic white\n0\n0\n5 .7 .6 .5 0 0\n')
        son = self.derle()
        self.assertEqual(self.mat.read_bytes(), mat_kaynagi)
        self.assertNotEqual(ilk, son)
        self.assertEqual([float(x) for x in self.izle(ilk, 1, 'v')[0].split()], [.2, .3, .4])
        self.assertEqual([float(x) for x in self.izle(son, 1, 'v')[0].split()], [.7, .6, .5])

    def test_yarim_kayitsiz_ve_ayni_boyda_bozuk_cache_yenilenir(self):
        self.obj_yaz([""])
        rel = self.derle()
        p = self.proje / rel
        for tur in ("kesik", "kayitsiz", "ayniboy"):
            with self.subTest(tur=tur):
                if tur == "kesik":
                    p.write_bytes(p.read_bytes()[:140])
                elif tur == "kayitsiz":
                    p.with_suffix(".json").unlink()
                else:
                    ham = bytearray(p.read_bytes())
                    ham[-1] ^= 1
                    p.write_bytes(ham)
                with patch.object(surecler, "calistir", wraps=surecler.calistir) as run:
                    self.assertEqual(self.derle(), rel)
                    self.assertEqual(run.call_count, 1)
                self.assertEqual(self.izle(rel, 1), ["white"])

    def test_eszamanli_istekler_bir_kere_derler(self):
        self.obj_yaz([""])
        with patch.object(surecler, "calistir", wraps=surecler.calistir) as run:
            with ThreadPoolExecutor(max_workers=3) as pool:
                yollar = list(pool.map(lambda _: self.derle(), range(3)))
            self.assertEqual(len(set(yollar)), 1)
            self.assertEqual(run.call_count, 1)

    def test_patch_sinirinda_frozen_oct_geometri_ve_malzeme_korur(self):
        self.obj_yaz(["", "g red\n", "usemtl green\n"])
        # sıfır alan, tekrar ve çok ince yüzler bile hazırlıkta threshold'a takılmaz.
        with self.obj.open('a') as f:
            f.write('usemtl white\nf 1 2 3\nf 1 1 1\nv 0 10 0\nv 1e-8 10 0\nv 0 11 0\nf 10 11 12\n')
        kaynak = self.obj.read_bytes()
        calistir = surecler.calistir
        gelen_yuzler = []
        def patch_siniri(cmd, **kw):
            if cmd[0] == 'obj2mesh':
                return subprocess.CompletedProcess(cmd, 1, '', 'obj2mesh: internal - too many patch triangles in addmeshtri')
            if cmd[0] == 'obj2rad':
                gelen_yuzler.extend(l for l in (Path(kw['cwd']) / cmd[1]).read_bytes().splitlines() if l.startswith(b'f '))
            return calistir(cmd, **kw)
        with patch.object(surecler, 'calistir', side_effect=patch_siniri) as run:
            rel = self.derle()
            self.assertEqual(run.call_count, 3)
        self.assertTrue(rel.endswith('.oct'))
        self.assertEqual(self.obj.read_bytes(), kaynak)
        self.assertEqual(gelen_yuzler, [l for l in kaynak.splitlines() if l.startswith(b'f ')])
        self.assertEqual(self.izle(rel, 3), ['white', 'red', 'green'])
        with patch.object(surecler, 'calistir', side_effect=AssertionError('Başarılı OCT tekrar derlenmemeli')):
            self.assertEqual(self.derle(), rel)
        self.assertFalse(list((self.proje / '_cache').glob('.mesh_*')))

    def test_patch_sinirinda_uv_kaybi_sessizce_kabul_edilmez(self):
        self.obj_yaz(['usemtl red\n'])
        with self.obj.open('a') as f:
            f.write('vt .1 .2\n')
        with patch.object(surecler, 'calistir', return_value=subprocess.CompletedProcess([], 1, '', 'too many patch triangles')) as run:
            with self.assertRaises(render_mesh.MeshUVDestekHatasi):
                self.derle()
        self.assertEqual(run.call_count, 1)
        self.assertFalse(list((self.proje / '_cache').iterdir()))

    def test_frozen_oct_hatasinda_yarim_onbellek_yok(self):
        self.obj_yaz([''])
        def basarisiz(cmd, **kw):
            hata = 'too many patch triangles' if cmd[0] == 'obj2mesh' else 'synthetic polygon failure'
            return subprocess.CompletedProcess(cmd, 1, '', hata)
        with patch.object(surecler, 'calistir', side_effect=basarisiz):
            with self.assertRaisesRegex(RuntimeError, 'synthetic polygon failure'):
                self.derle()
        self.assertFalse(list((self.proje / '_cache').iterdir()))


@unittest.skipUnless(all(shutil.which(t) for t in ("obj2mesh", "oconv", "rtrace")),
                     "Yerel Radiance kurulumu gerekli")
class ParcaliMeshTest(GercekMeshTest):
    def setUp(self):
        super().setUp()
        self.threshold = patch.object(render_mesh, '_PARCA_ESIK', 0)
        self.chunk = patch.object(render_mesh, '_PARCA_YUZ', 2)
        self.threshold.start(); self.chunk.start()
        self.addCleanup(self.threshold.stop); self.addCleanup(self.chunk.stop)

    # miras gelen tek dosyalık cache/kapasite testleri eski küçük yol için.
    # bu class'ta sadece aşağıdaki yeni, sınırlı yol case'leri koşar.
    def test_parcalar_renk_uv_normal_ve_negatif_indeks_korur(self):
        self.obj_yaz(['usemtl white\n', 'usemtl red\n', 'usemtl green\n'])
        with self.obj.open('a') as stream:
            stream.write('vt .1 .2\nvt .8 .2\nvt .1 .8\nvn 0 0 1\n'
                         'usemtl red\nf -3/1/1 -2/2/1 -1/3/1\nf 1 1 1\n')
        original = self.obj.read_bytes()
        calls = []
        run = surecler.calistir
        def inspect(cmd, **kw):
            if cmd[0] == 'obj2mesh':
                calls.append((Path(kw['cwd']) / cmd[-2]).read_bytes())
                self.assertLessEqual(kw['bellek_siniri'], 768 << 20)
            return run(cmd, **kw)
        with patch.object(surecler, 'calistir', side_effect=inspect):
            rel = self.derle()
        self.assertTrue(rel.endswith('.oct'))
        self.assertEqual(self.obj.read_bytes(), original)
        self.assertEqual(self.izle(rel, 2), ['white', 'red'])
        import json
        meta = json.loads((self.proje / rel).with_suffix('.json').read_text())
        self.assertEqual(meta['sayim'], {'yuz': 5, 'sifir_alan': 1, 'parca': 2})
        self.assertEqual(len(render_mesh.parca_bagimliliklari(self.proje, rel)), 2)
        self.assertTrue(any(b'vt .1 .2' in x and b'vn 0 0 1' in x and b'/1' in x for x in calls))
        with patch.object(surecler, 'calistir', side_effect=AssertionError('Cache should be reused')):
            self.assertEqual(self.derle(), rel)
        piece = self.proje / meta['parcalar'][0]['yol']
        piece.write_bytes(piece.read_bytes()[:-1] + b'X')
        self.assertEqual(self.derle(), rel)
        self.assertEqual(self.izle(rel, 2), ['white', 'red'])

    def test_parca_bellek_hatasi_yalniz_o_parcayi_boler(self):
        self.obj_yaz(['', 'g red\n', 'usemtl green\n'])
        run = surecler.calistir
        seen = []
        def fail_large(cmd, **kw):
            if cmd[0] == 'obj2mesh':
                faces = sum(s.startswith(b'f ') for s in (Path(kw['cwd']) / cmd[-2]).read_bytes().splitlines())
                seen.append(faces)
                if faces > 1:
                    return subprocess.CompletedProcess(cmd, 1, '', 'system - out of memory')
            return run(cmd, **kw)
        with patch.object(surecler, 'calistir', side_effect=fail_large):
            rel = self.derle()
        self.assertEqual(seen, [2, 1, 1, 1])
        self.assertEqual(self.izle(rel, 3), ['white', 'red', 'green'])

    def test_gecersiz_indeks_parca_ve_yarim_cache_birakmaz(self):
        self.obj_yaz([''])
        with self.obj.open('a') as stream: stream.write('f 0 2 3\n')
        with self.assertRaisesRegex(RuntimeError, 'sınır dışında'):
            self.derle()
        self.assertEqual(list((self.proje / '_cache').iterdir()), [])

    def test_carpim_alt_tasmasi_sonlu_kucuk_yuzu_silmez(self):
        self.obj.write_text('v 0 0 0\nv 1e-200 0 0\nv 0 1e-200 0\nf 1 2 3\n')
        captured = []
        def compiler(cmd, **kw):
            if cmd[0] == 'obj2mesh':
                captured.append((Path(kw['cwd']) / cmd[-2]).read_text())
                (Path(kw['cwd']) / cmd[-1]).write_bytes(b'#?RADIANCE\nFORMAT=Radiance_tmesh\n\n' + b'0'*160)
            else:
                Path(kw['stdout_yolu']).write_bytes(b'#?RADIANCE\nFORMAT=Radiance_octree\n\n' + b'0'*160)
            return subprocess.CompletedProcess(cmd, 0, '', '')
        with patch.object(surecler, 'calistir', side_effect=compiler):
            rel = self.derle()
        import json
        meta = json.loads((self.proje / rel).with_suffix('.json').read_text())
        self.assertEqual(meta['sayim']['sifir_alan'], 0)
        self.assertIn('f 1 2 3', captured[0])

    def test_buyuk_mesh_hatasi_polygon_yoluna_gecmez(self):
        self.obj_yaz([''])
        with patch.object(render_mesh, 'derle', side_effect=RuntimeError('out of memory')):
            with self.assertRaisesRegex(RuntimeError, 'polygon yolu başlatılmadı'):
                motor._obj_rtm(str(self.proje), str(self.obj), self.mat.name)

# miras gelen küçük yol testleri zorla chunk'lanmış halde tekrar koşmasın.
for _name in list(GercekMeshTest.__dict__):
    if _name.startswith('test_'):
        setattr(ParcaliMeshTest, _name, None)


if __name__ == "__main__":
    unittest.main()
