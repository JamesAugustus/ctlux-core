# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Lumion aktarımında doğrulanan kayıp türleri, sadece sentetik binary/OBJ."""
from pathlib import Path
import json
import os
import struct
import sys
import tempfile
import unittest
import warnings

ROOT = Path(__file__).resolve().parents[1]
from core import importer as ice_aktarma
from core.tools import ls10_reader as L


def tlv(tag, vals, fmt='f'):
    data = struct.pack('<'+str(len(vals))+fmt, *vals)
    return tag + struct.pack('<I', len(data)) + data


def matris(y=0):
    return [1,0,0,0, 0,1,0,0, 0,0,1,0, 0,y,0,1]


def isik(tip, x=0, alanlar=None):
    m = matris(); m[12] = x
    out = 'ClassType->cLightObject'.encode('utf-16le')
    for n in range(1, 38):
        if alanlar and n in alanlar:
            out += tlv(b'IIV1' if n == 28 else b'IIVE', alanlar[n])
        elif n == 3:
            out += tlv(b'IIM1', m)
        elif n == 28:
            out += tlv(b'IIV1', [100,80,60,0])
        else:
            out += tlv(b'IIVE', [tip if n == 37 else 1])
    return out


def model_kaydi(y=20):
    return ('ClassType->cCustomObject'.encode('utf-16le')
            + 'world'.encode('utf-16le') + tlv(b'IIM1', matris(y))
            + 'ClassInstance->cImportObject'.encode('utf-16le'))


def mesh():
    # 20 cm grid cell'inde tamamen çöken üçgen + birebir aynı tekrar eden vertex.
    return (tlv(b'VPPI', [0,0,0, .001,0,0, 0,.001,0, 0,0,0])
            + tlv(b'PO32', [3,1,2], 'I'))


class LumionKayipTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ctlux-ls10-koruma-')
        self.p = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_yakin_isiklar_varsayilan_olarak_silinmez(self):
        raw = isik(3) + isik(0, .1)
        self.assertEqual(len(L.isik_coz(raw)), 2)
        self.assertEqual(len(L.isik_armatur(raw)), 2)
        self.assertEqual(len(L.isik_coz(raw, birlestir=True)), 1)

    def test_kaynak_kimligi_filtre_sirasindan_bagimsiz(self):
        raw = isik(0, .1) + isik(3)
        self.assertEqual(L.isik_coz(raw)[1]['kaynak_id'],
                         L.isik_coz(raw, birlestir=True)[0]['kaynak_id'])
        self.assertEqual(L.isik_armatur(raw)[1]['fotometri_durumu'], 'yaklasik')

    def test_dogrulanmayan_isik_rengi_notr_ve_acikca_varsayilandir(self):
        raw = isik(0, .1)+isik(1)+isik(3)
        before = bytes(raw)
        decoded = L.isik_coz(raw)
        arms = L.isik_armatur(raw)
        self.assertEqual(raw, before)
        for arm, source in zip(arms, decoded):
            self.assertEqual(arm['renk'], [1, 1, 1])
            self.assertEqual(arm['renk_kaynagi'], 'varsayilan_notr')
            self.assertIn('doğrulanamadı', arm['renk_notu'])
            self.assertEqual(arm['kaynak_id'], source['kaynak_id'])
            self.assertEqual(arm['guc'], max(1, int(round(source['sidd']/200*1000))))
            self.assertEqual(arm['yon'], None if source['tip'] == 1 else [0, -1, 0])

    def test_tek_model_donusumu_ve_zup_sirasi(self):
        data = model_kaydi() + mesh()
        m = L.tek_model_matrisi(data)
        self.assertEqual(m[13], 20)
        out = self.p/'model.obj'
        r = L.mesh_cikar(data, out, zup=True, matris=m, tekil=True)
        self.assertEqual((r['vertex'], r['ucgen']), (3, 1))
        coords = [tuple(map(float, s.split()[1:])) for s in out.read_text().splitlines() if s.startswith('v ')]
        self.assertAlmostEqual(coords[2][2], 20.001, places=6)
        self.assertIn('f 1 2 3', out.read_text())

    def test_yedek_mesh_yonu_aynalama_ve_zup_ile_korunur(self):
        # Yüzleri dışarı bakan kapalı dört yüzlü ve birebir tekrarlanan bir köşe.
        raw = (tlv(b'VPPI', [0,0,0, 2,0,0, 0,3,0, 0,0,4, 0,0,0])
               + tlv(b'PO32', [4,2,1, 0,1,3, 0,3,2, 1,2,3], 'I'))
        for sign in (1, -1):
            # Döndürme, farklı eksen ölçekleri, kaykılma ve öteleme; determinant 24 * sign.
            world = [0,2*sign,0,0, -3,0,0,0, 1,0,4,0, 7,-11,13,1]
            data = model_kaydi().replace(tlv(b'IIM1', matris(20)), tlv(b'IIM1', world)) + raw
            resolved = L.tek_model_matrisi(data)
            self.assertIsNotNone(resolved)
            for zup in (False, True):
                for unique in (False, True):
                    with self.subTest(sign=sign, zup=zup, unique=unique):
                        out = self.p / 'winding.obj'
                        result = L.mesh_cikar(data, out, zup=zup, matris=resolved, tekil=unique)
                        self.assertEqual((result['mesh'], result['vertex'], result['ucgen']),
                                         (1, 4 if unique else 5, 4))
                        lines = out.read_text().splitlines()
                        vertices = [tuple(map(float, line.split()[1:])) for line in lines if line.startswith('v ')]
                        faces = [tuple(int(i)-1 for i in line.split()[1:]) for line in lines if line.startswith('f ')]
                        self.assertEqual(len(faces), 4)
                        expected = (7, -11 + 4*sign, 13)
                        if zup:
                            expected = (expected[0], -expected[2], expected[1])
                        self.assertEqual(vertices[1], expected)
                        center = [sum(v[i] for v in vertices[:4])/4 for i in range(3)]
                        for face in faces:
                            a, b, c = [vertices[i] for i in face]
                            ab = [b[i]-a[i] for i in range(3)]
                            ac = [c[i]-a[i] for i in range(3)]
                            normal = (ab[1]*ac[2]-ab[2]*ac[1], ab[2]*ac[0]-ab[0]*ac[2],
                                      ab[0]*ac[1]-ab[1]*ac[0])
                            midpoint = [(a[i]+b[i]+c[i])/3 for i in range(3)]
                            self.assertGreater(sum(normal[i]*(midpoint[i]-center[i]) for i in range(3)),
                                               0, face)

    def test_coklu_modelde_matris_tahmin_edilmez(self):
        self.assertIsNone(L.tek_model_matrisi(model_kaydi()+model_kaydi(40)))
        self.assertIsNone(L.tek_model_matrisi(mesh()))

    def test_sifir_olcekli_matris_reddedilir(self):
        data = model_kaydi().replace(tlv(b'IIM1', matris(20)), tlv(b'IIM1', [0]*15+[1]))
        self.assertIsNone(L.tek_model_matrisi(data))

    def test_dunya_matrisi_determinant_esigi(self):
        # Float32 olarak tam temsil edilen bu ölçekler 1e-20 determinant eşiğinin iki yanındadır.
        for sign in (1, -1):
            for scale, accepted in ((2.0**-67, False), (2.0**-66, True)):
                with self.subTest(sign=sign, scale=scale):
                    world = matris(20)
                    world[0] = sign * scale
                    data = model_kaydi().replace(tlv(b'IIM1', matris(20)), tlv(b'IIM1', world))
                    resolved = L.tek_model_matrisi(data)
                    if accepted:
                        self.assertIsNotNone(resolved)
                        self.assertEqual(resolved[0], sign * scale)
                    else:
                        self.assertIsNone(resolved)

    def test_varsayilan_aktarim_ince_yuzu_ve_gruplari_korur(self):
        src = self.p/'input.ls10'; out = self.p/'out.obj'
        src.write_bytes(model_kaydi()+mesh()+mesh())
        stale = Path(str(out)+'.tam.obj'); stale.write_text('old geometry')
        ice_aktarma._lumion_obj(str(src), str(out))
        self.assertEqual(stale.read_bytes(),out.read_bytes())
        lines = out.read_text().splitlines()
        self.assertEqual(sum(x.startswith('f ') for x in lines), 2)
        self.assertEqual(sum(x.startswith('o ') for x in lines), 2)
        info = json.loads(Path(str(out)+'.aktarim.json').read_text())
        self.assertEqual(info['ucgen'], 2)

    def test_sonlu_olmayan_isik_alani_yalniz_o_isigi_atlar(self):
        # Alan numaraları: 28 rgb, 37 tip, 26 cone, 35 w, 36 h.
        durumlar = {28: ('rgb', [float('nan'), 0, 0, 0]), 37: ('tip', [float('nan')]),
                    26: ('cone', [float('inf')]), 35: ('w', [float('-inf')]), 36: ('h', [float('nan')])}
        for n, (ad, deger) in durumlar.items():
            with self.subTest(alan=ad):
                raw = isik(1) + isik(3, 5, {n: deger})
                with warnings.catch_warnings(record=True) as yakalanan:
                    warnings.simplefilter('always')
                    cozulen = L.isik_coz(raw)
                    armaturler = L.isik_armatur(raw)
                self.assertEqual([c['tip'] for c in cozulen], [1])
                self.assertEqual(len(armaturler), 1)
                self.assertTrue(any('atlandı: sonlu olmayan' in str(w.message) and ad in str(w.message)
                                    for w in yakalanan))

    def test_sonlu_olmayan_guc_isigi_atlar(self):
        raw = isik(1)
        self.assertEqual(len(L.isik_armatur(raw)), 1)
        with warnings.catch_warnings(record=True) as yakalanan:
            warnings.simplefilter('always')
            self.assertEqual(L.isik_armatur(raw, k=1e-320), [])
        self.assertTrue(any('güç sonlu değil' in str(w.message) for w in yakalanan))

    def test_bos_dosya_acik_hata_verir_ve_dosyayi_kapatir(self):
        src = self.p/'bos.ls10'; src.write_bytes(b'')
        once = len(os.listdir('/proc/self/fd')) if os.path.isdir('/proc/self/fd') else None
        for cagri in (lambda: ice_aktarma._lumion_obj(str(src), str(self.p/'out.obj')),
                      lambda: ice_aktarma.lumion_armaturler(str(src))):
            with self.assertRaisesRegex(RuntimeError, 'Lumion dosyası boş'):
                cagri()
        if once is not None:
            self.assertEqual(len(os.listdir('/proc/self/fd')), once)
        self.assertFalse((self.p/'out.obj').exists())


if __name__ == '__main__':
    unittest.main()
