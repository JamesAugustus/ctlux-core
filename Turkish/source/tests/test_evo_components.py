# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Tamamen sentetik EVO kayıtları, kullanıcı dosyası ya da proje ölçüsü yok."""
from pathlib import Path
import json
import sys
import tempfile
import unittest
import warnings
from unittest.mock import patch
import zipfile

ROOT=Path(__file__).resolve().parents[1]
from core import engine as motor
from core import importer as ice_aktarma
from core import format_detector as dedektif

# kat dönük ve kaydırılmış, kontur ve armatür onun local koordinatlarında.
STEP='''
#1=CoordSys3D((10,20,4),(0,1,0),(-1,0,0),(0,0,1));
#2=CoordSys3D((0,0,0),(1,0,0),(0,1,0),(0,0,1));
#3=CoordSys3D((1,0,0),(1,0,0),(0,1,0),(0,0,1));
#4=CoordSys3D((2,0,1),(1,0,0),(0,1,0),(0,0,1));
#5=CoordSys3D((1,0,0),(0,0,-1),(0,1,0),(1,0,0));
#10=Storey(10,'kat',(),#1,#11,$);
#11=InstanceGeometricRepresentation(#10,#12);
#12=StoreyRepresentationData(#10,0,(),(),False,(1,1,1),3);
#20=StoreyElement(20,'eleman',(),#2,$,$);
#30=StoreyContour(30,'kontur',(),#3,#31,$);
#31=InstanceGeometricRepresentation(#30,#32);
#32=StoreyContourRepresentationData(#30,0,(),(),False,(1,1,1),((0,0),(4,0),(4,5),(0,5)),$,(),.Inner.,False,$,$);
#40=Space(40,'oda',(),#2,#41,$);
#41=SpaceGeometricRepresentation(#40,#42);
#42=SpaceRepresentationData(#40,0,(#43),(),False,(1,1,1));
#43=StoreyContourBasedSpaceRepresentationDataPart(#42,1,#2,.Storey.,2.5);
#50=LuminaireArrangement(50,'dizi',(),#4,$,$);
#60=LuminaireElement(77,'ışık',(),#5,$,$);
#70=RelContainedInSpatialStructure(70,'',#10,(#20,#50,#60));
#71=RelAggregates(71,'',#20,(#30));
#72=RelAggregates(72,'',#10,(#40));
#73=RelAggregates(73,'',#50,(#60));
#74=RelAssociatesStoreyContourBasedSpace(74,'',#30,#40,True);
'''

POLYGON_STEP=STEP.replace('#43=StoreyContourBasedSpaceRepresentationDataPart(#42,1,#2,.Storey.,2.5);',
          '#43=PolygonBasedSpaceRepresentationDataPart(#42,3,(#81,#82,#83,#84));\n'
          '#81=PolyPoint2D($,(0,0));#82=PolyPoint2D($,(4,0));'
          '#83=PolyPoint2D($,(4,5));#84=PolyPoint2D($,(0,5));')

class EvoBilesenTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='ctlux-evo-bilesenler-')
        self.root=Path(self.tmp.name);self.src=self.root/'ornek.evo'
        self.pd=self.root/'proje';(self.pd/'model').mkdir(parents=True)
        motor.proje_kaydet(str(self.pd),{'geometri':[],'armaturler':[]})
        self.zip_yaz()

    def tearDown(self): self.tmp.cleanup()

    def zip_yaz(self,step=STEP,ekler=None):
        with zipfile.ZipFile(self.src,'w') as z:
            z.writestr('Project/ProjectData/ProjectData.dat',step)
            for n,b in (ekler or {}).items():z.writestr(n,b)

    def test_kat_dizi_eleman_bilesimi_ve_yon(self):
        a=motor.evo_armaturler(str(self.src))[0]
        self.assertEqual((a['x'],a['y'],a['z']),(10,23,5))
        self.assertEqual(a['yon'],[0,-1,0])

    def test_dogrulanmayan_evo_rengi_notr_ve_diger_isik_verileri_aynen_kalir(self):
        before = self.src.read_bytes()
        a = motor.evo_armaturler(str(self.src))[0]
        self.assertEqual(a['renk'], [1, 1, 1])
        self.assertEqual(a['renk_kaynagi'], 'varsayilan_notr')
        self.assertIn('doğrulanamadı', a['renk_notu'])
        self.assertEqual((a['ad'], a['kaynak']), ('armatur_77', 'evo/STEP'))
        self.assertEqual((a['guc'], a['yaricap'], a['tip'], a['aci']), (25000, .18, 'spot', 90))
        self.assertEqual(a['yon'], [0, -1, 0])
        self.assertEqual(self.src.read_bytes(), before)

    def test_kat_konturu_oda_ve_kat_yuksekligi(self):
        hedef=self.pd/'model/oda.rad';_,n=motor.evo_odalar(str(self.src),str(hedef))
        self.assertEqual(n,6)
        s=hedef.read_text();self.assertIn(' 10 21 4\n',s);self.assertIn(' 10 21 7\n',s)
        self.assertNotIn('6.5',s)  # kullanılmayan bireysel yükseklik 2.5'i seçme.

    def test_hiyerarsi_dongusu_reddedilir(self):
        self.zip_yaz(STEP+'\n#80=RelAggregates(80,\'\',#60,(#10));')
        with self.assertRaisesRegex(ValueError,'döngü'):motor.evo_armaturler(str(self.src))

    def test_bozuk_kontur_reddedilir(self):
        self.zip_yaz(STEP.replace('((0,0),(4,0),(4,5),(0,5))','((0,0),(4,0))'))
        with self.assertRaisesRegex(ValueError,'kontur'):motor.evo_odalar(str(self.src),str(self.pd/'model/oda.rad'))

    def test_fbx_hatasi_step_verilerini_engellemez(self):
        self.zip_yaz(ekler={'Mesh/toy.fbx':b'bad'})
        hatalar=[]
        with patch.object(motor,'cevir',side_effect=RuntimeError('çevirici yok')),patch.object(motor,'evo_ies',return_value={}):
            gs,ls=dedektif._evo_yerlestir(str(self.src),str(self.pd),str(self.pd/'model/toy.obj'),[],hatalar)
        self.assertEqual(len(gs),1);self.assertEqual(len(ls),1)
        self.assertTrue(any('çevirici yok' in h for h in hatalar))

    def test_m3d_ayrintisi_kismi_bildirilir(self):
        self.zip_yaz(ekler={'Mesh/toy.m3d':b'opaque','Mesh/toy.gdms':b'opaque'})
        hatalar=[]
        with patch.object(motor,'evo_ies',return_value={}):
            gs,ls=dedektif._evo_yerlestir(str(self.src),str(self.pd),str(self.pd/'model/toy.obj'),[],hatalar)
        self.assertEqual(len(gs),1);self.assertEqual(len(ls),1)
        self.assertTrue(any('henüz desteklenmiyor' in h for h in hatalar))

    def test_evo_step_icin_assimp_sarti_yok(self):
        with patch.object(ice_aktarma.shutil,'which',return_value=None):self.assertTrue(motor.cevrilebilir('.evo'))

    def test_eski_poligonlu_oda_da_kat_cercevesinde(self):
        step=POLYGON_STEP
        self.zip_yaz(step);hedef=self.pd/'model/oda.rad'
        _,n=motor.evo_odalar(str(self.src),str(hedef));self.assertEqual(n,6)
        self.assertIn(' 10 20 4\n',hedef.read_text())

    def test_kapali_poligon_alti_sifir_olmayan_ice_bakan_yuz_uretir(self):
        for refs in ('(#81,#82,#83,#84,#81)', '(#81,#84,#83,#82,#81)'):
            with self.subTest(refs=refs):
                step = POLYGON_STEP.replace('(#81,#82,#83,#84)', refs)
                self.assertEqual(self.oda_normalleri(step), {'zemin': 1, 'tavan': 1, 'duvar': 4})

    def test_uc_gecerli_koseden_az_poligon_yukseklik_uyarisi_olmadan_reddedilir(self):
        for refs in ('(#81,#82)', '(#81,#82,#81)', '(#81,#82,#999)', '(#81,#82,#85)'):
            with self.subTest(refs=refs):
                step = POLYGON_STEP.replace('(#81,#82,#83,#84)', refs)
                self.zip_yaz(step + '\n#85=PolyPoint2D($,(1e309,5));')
                hedef = self.pd / 'model/oda.rad'
                with warnings.catch_warnings(record=True) as caught:
                    warnings.simplefilter('always')
                    with self.assertRaisesRegex(ValueError, 'poligonu üç geçerli köşeden az'):
                        motor.evo_odalar(str(self.src), str(hedef))
                self.assertEqual(caught, [])
                self.assertFalse(hedef.exists())

    def test_poligon_yuksekligi_ilk_uygun_skalerin_dogrulanmadigini_bildirir(self):
        self.zip_yaz(POLYGON_STEP.replace('#42,3,(', '#42,1.5,4.25,9,('))
        hedef = self.pd / 'model/oda.rad'
        with self.assertWarnsRegex(RuntimeWarning,
                r'doğrulanmamıştır.*1\.5 < değer < 30.*ilk skalerden 4\.25 m.*alanın yükseklik olduğu belirlenmemiştir') as caught:
            _, adet = motor.evo_odalar(str(self.src), str(hedef))
        self.assertEqual(len(caught.warnings), 1)
        self.assertEqual(adet, 6)
        self.assertIn(' 10 20 8.25\n', hedef.read_text())
        self.assertNotIn(' 10 20 13\n', hedef.read_text())

    def test_poligon_yuksekligi_varsayilan_uc_metrenin_dogrulanmadigini_bildirir(self):
        self.zip_yaz(POLYGON_STEP.replace('#42,3,(', '#42,1.5,30,0,('))
        hedef = self.pd / 'model/oda.rad'
        with self.assertWarnsRegex(RuntimeWarning,
                r'doğrulanmamıştır.*1\.5 < değer < 30.*skaler yok.*varsayılan 3 m') as caught:
            _, adet = motor.evo_odalar(str(self.src), str(hedef))
        self.assertEqual(len(caught.warnings), 1)
        self.assertEqual(adet, 6)
        self.assertIn(' 10 20 7\n', hedef.read_text())

    def oda_normalleri(self,step):
        # her çokgenin Newell normali oda merkezine gitmeli, zemin yukarı, tavan aşağı bakar.
        import re
        self.zip_yaz(step);hedef=self.pd/'model/oda.rad'
        _, adet = motor.evo_odalar(str(self.src),str(hedef))
        bloklar=[(tip,[tuple(map(float,l.split())) for l in b.strip().split('\n')]) for tip,b in
                 re.findall(r'(\w+)_mat polygon \S+\n0\n0\n\d+\n((?: \S+ \S+ \S+\n)+)',hedef.read_text())]
        self.assertEqual(adet, len(bloklar))
        koseler=[v for _,p in bloklar for v in p]
        merkez=[sum(v[i] for v in koseler)/len(koseler) for i in range(3)]
        sonuc={}
        for tip,p in bloklar:
            n=[0.,0.,0.]
            for k,(x,y,z) in enumerate(p):
                x2,y2,z2=p[(k+1)%len(p)]
                n[0]+=(y-y2)*(z+z2);n[1]+=(z-z2)*(x+x2);n[2]+=(x-x2)*(y+y2)
            self.assertGreater(sum(v * v for v in n), 0, (tip, p))
            orta=[sum(v[i] for v in p)/len(p) for i in range(3)]
            self.assertGreater(sum(n[i]*(merkez[i]-orta[i]) for i in range(3)),0,(tip,p))
            if tip=='zemin':self.assertGreater(n[2],0)
            if tip=='tavan':self.assertLess(n[2],0)
            if tip=='duvar':self.assertAlmostEqual(n[2],0)
            sonuc[tip]=sonuc.get(tip,0)+1
        return sonuc

    def test_oda_yuzleri_ice_bakar_iki_yonde_iki_oda_turunde(self):
        ccw,cw='((0,0),(4,0),(4,5),(0,5))','((0,0),(0,5),(4,5),(4,0))'
        kontur={'saat yönü tersi':STEP,'saat yönü':STEP.replace(ccw,cw)}
        poligon=POLYGON_STEP
        kontur['poligon saat yönü tersi']=poligon
        kontur['poligon saat yönü']=poligon.replace('(#81,#82,#83,#84)','(#84,#83,#82,#81)')
        for ad,step in kontur.items():
            with self.subTest(ad):
                self.assertEqual(self.oda_normalleri(step),{'zemin':1,'tavan':1,'duvar':4})

if __name__=='__main__':unittest.main()
