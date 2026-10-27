# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Gerçek kullanıcı davranışlarından küçük, sentetik regression örnekleri."""
import json
import math
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
from core import engine as motor
from core import importer as ice_aktarma
from core.tools import ls10_reader as lumion_oku
from core import processes as surecler
from core.file_safety import atomik_json, ic_yol
from core.step_reader import alanlar, kayitlar


class DosyaliTest(unittest.TestCase):
    def setUp(self):
        (ROOT / "tests/.tmp").mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / "tests/.tmp")
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()


class OkuyucuTest(DosyaliTest):
    def test_kesik_mesh_basligi(self):
        for b in (b"VPPI", b"VPPI\x01", b"VPPI"+struct.pack('<I',12)+b'\0'*12+b'PO32'):
            self.assertEqual(lumion_oku.mesh_cikar(b, self.root/'x.obj')['mesh'], 0)

    def test_gecerli_ve_nan_mesh(self):
        for x, beklenen in ((0.0, 1), (float('nan'), 0)):
            v = struct.pack('<9f', x,0,0, 1,0,0, 0,1,0)
            b = b'VPPI'+struct.pack('<I',len(v))+v+b'PO32'+struct.pack('<I',12)+struct.pack('<3I',0,1,2)
            self.assertEqual(lumion_oku.mesh_cikar(b,self.root/'x.obj')['mesh'], beklenen)

    def test_step_tirnaklar(self):
        self.assertEqual(alanlar("42,'Lamp, (A) and ''B''',#7,(1,2,3)"),
                         ['42', "'Lamp, (A) and ''B'''", '#7', '(1,2,3)'])

    def test_step_kayitta_noktali_virgul(self):
        d=kayitlar("#1=Thing('A);B',(#2,#3));\n#2=Other(7);")
        self.assertEqual(len(d),2)
        self.assertEqual(alanlar(d[1][1])[0],"'A);B'")

    def test_step_kesik_alan(self):
        with self.assertRaises(ValueError): alanlar("1,'abc")

    def test_usd_kapali_isik(self):
        a,_=ice_aktarma.usd_armaturler([{'tur':'omni','siddet':0}])
        self.assertEqual(a[0]['guc'],0)
        self.assertEqual(a[0]['fotometri_durumu'],'yaklasik')


class DosyaTest(DosyaliTest):
    def test_atomik_json_hatasi_eskiyi_korur(self):
        p=self.root/'p.json';atomik_json(p,{'x':1})
        with self.assertRaises(ValueError):atomik_json(p,{'x':float('nan')})
        self.assertEqual(json.loads(p.read_text()),{'x':1})

    def test_yol_kacisi(self):
        for rel in ('../x','/x','a/../../x','C:\\x','..\\x'):
            with self.assertRaises(ValueError):ic_yol(self.root,rel)

    def test_symlink_kacisi(self):
        d=self.root/'root';d.mkdir();(self.root/'else').mkdir()
        (d/'link').symlink_to(self.root/'else',target_is_directory=True)
        with self.assertRaises(ValueError):ic_yol(d,'link/x')

    def test_ayni_adli_dokular(self):
        from core import format_detector as dedektif
        for n in ('a/tex.jpg','b/tex.jpg'):
            p=self.root/n;p.parent.mkdir();p.write_bytes(b'fixture')
        ds,_=dedektif.analiz(str(self.root))
        self.assertEqual({d['dosya'] for d in ds},{'a/tex.jpg','b/tex.jpg'})


class FotometriTest(DosyaliTest):
    def setUp(self):
        super().setUp()
        for n in ('model', 'malzeme', 'doku', 'isik', 'gok', 'gorunum', '_cache', 'render', 'cikti'):(self.root/n).mkdir()
        self.ies=self.root/'isik/lamp.ies';self.ies.write_text('test photometry')

    def converter(self,cmd,**kw):
        out=cmd[cmd.index('-o')+1]
        Path(kw['cwd'],out+'.rad').write_text('void light l\n0\n0\n3 1 1 1\n')
        Path(kw['cwd'],out+'.dat').write_text('fixture')
        return subprocess.CompletedProcess(cmd,0,'','')

    def test_dimmer_ve_kaynak_degisimi(self):
        with patch.object(surecler,'calistir',side_effect=self.converter) as run:
            a=motor._ies_urun_rad(str(self.root),'isik/lamp.ies',.5)
            b=motor._ies_urun_rad(str(self.root),'isik/lamp.ies',1)
            self.assertNotEqual(a,b)
            self.assertEqual(run.call_args_list[0].args[0][2],'0.5')
            self.ies.write_text('changed photometry')
            c=motor._ies_urun_rad(str(self.root),'isik/lamp.ies',1)
            self.assertNotEqual(b,c)

    def test_kapali_fotometri_emisyon_uretmez(self):
        a={'ad':'l','ies':'isik/lamp.ies','dimmer':0}
        with patch.object(surecler,'calistir') as run:
            p=motor.armatur_uret(str(self.root),{'armaturler':[a]})
            self.assertNotIn('!xform',Path(p).read_text());run.assert_not_called()

    def test_eksik_ies_sessiz_jenerik_olmaz(self):
        with self.assertRaises(ValueError):
            motor.armatur_uret(str(self.root),{'armaturler':[{'ad':'l','ies':'missing.ies'}]})

    def test_evo_fotometri_cevirici_hatasi_gizlenmez(self):
        ents={1:('LuminaireElement','17'),2:('LuminairePrototype','#3'),
              3:('PrototypeGeometricRepresentation','#4'),
              4:('LuminairePrototypeRepresentationData',''),
              5:('LightDistributionConnection','#4,#6'),6:('LightDistribution','#7'),
              7:('LightDistributionData','(),(0),(0,90,180),(100,100,100)'),
              8:('LampTypeChannel','#4,$,$,1256'),9:('RelDefinesByPrototype','#1,#2')}
        with patch.object(ice_aktarma,'_evo_step_ents',return_value=ents),patch.object(motor,'sh',return_value=(127,'','ies2rad yok')):
            with self.assertRaisesRegex(RuntimeError,'EVO fotometrisi'):
                motor.evo_ies('test.evo',str(self.root))

    def test_onbellek_hesap_ayari(self):
        p=self.root/'scene.oct';p.write_bytes(b'same scene')
        self.assertNotEqual(motor._ambient_dosyasi(str(p),'-ab 2'),motor._ambient_dosyasi(str(p),'-ab 6'))

    def test_doku_yollari_korunur(self):
        p=self.root/'model/a.mtl';p.write_text('newmtl A\nmap_Kd a/tex.jpg\nnewmtl B\nmap_Kd b/tex.jpg\n')
        d=motor._mtl_oku([str(p)])
        self.assertEqual(d['A']['harita'],'a/tex.jpg');self.assertEqual(d['B']['harita'],'b/tex.jpg')

    def test_ic_ice_mtl_goreli_dokusu(self):
        for ev in ('A','B'):
            mtl=self.root/'model'/ev/'room.mtl';mtl.parent.mkdir()
            mtl.write_text('newmtl test\nmap_Kd textures/a.png\n')
            tex=self.root/'doku'/ev/'textures/a.png';tex.parent.mkdir(parents=True)
            tex.write_bytes(ev.encode())
            d=motor._mtl_oku([str(mtl)])['test']
            bulundu=motor._doku_bul(str(self.root),d['harita'],d['_mtl_yolu'])
            self.assertEqual(Path(bulundu),tex)
        self.assertIsNone(motor._doku_bul(str(self.root),'../../disari'))


class SurecTest(unittest.TestCase):
    def test_zaman_asimi(self):
        with self.assertRaises(RuntimeError):
            surecler.calistir([sys.executable,'-c','import time;time.sleep(10)'],timeout=.1)
        self.assertFalse(surecler._aktif)


if __name__=='__main__':unittest.main()
