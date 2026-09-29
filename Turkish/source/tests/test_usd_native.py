# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Kurulu OpenUSD ile gerçek, ayrı process'te dönüşümler, sahte pxr kullanılmaz."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
from core import engine as motor


@unittest.skipUnless(importlib.util.find_spec('pxr'), 'Yerel USD ortamı gerekli')
class USDTest(unittest.TestCase):
    def setUp(self):
        (ROOT/'tests/.tmp').mkdir(exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=ROOT/'tests/.tmp')
        self.root=Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def sahne(self):
        from pxr import Usd,UsdGeom,UsdLux,Gf
        p=self.root/'sahne.usda';stage=Usd.Stage.CreateNew(str(p))
        UsdGeom.SetStageMetersPerUnit(stage,0.01)
        UsdGeom.SetStageUpAxis(stage,UsdGeom.Tokens.y)
        parent=UsdGeom.Xform.Define(stage,'/Sahne')
        parent.AddTranslateOp().Set(Gf.Vec3d(100,200,50))
        mesh=UsdGeom.Mesh.Define(stage,'/Sahne/Ucgen')
        mesh.CreatePointsAttr([(0,0,0),(100,0,0),(0,100,0)])
        mesh.CreateFaceVertexCountsAttr([3]);mesh.CreateFaceVertexIndicesAttr([0,1,2])
        light=UsdLux.RectLight.Define(stage,'/Sahne/Isik')
        light.CreateIntensityAttr(2);light.CreateExposureAttr(1)
        stage.GetRootLayer().Save()
        return p,stage

    def kontrol(self,p):
        hedef=self.root/(p.name+'.obj')
        geo,lights,_=motor.usd_kopru_calistir(str(p),str(hedef))
        self.assertTrue(geo);self.assertEqual(len(lights),1)
        self.assertEqual(lights[0]['konum'],[1.0,-0.5,2.0])
        self.assertEqual(lights[0]['yon'],[0.0,1.0,0.0])
        self.assertEqual(lights[0]['siddet'],4)
        vertices=[list(map(float,l.split()[1:])) for l in hedef.read_text().splitlines() if l.startswith('v ')]
        self.assertEqual(vertices,[[1.0,-0.5,2.0],[2.0,-0.5,2.0],[1.0,-0.5,3.0]])

    def test_usda_metre_eksen_ve_isik(self):
        p,_=self.sahne();self.kontrol(p)

    def test_usdc_binary(self):
        _,stage=self.sahne();p=self.root/'sahne.usdc'
        stage.GetRootLayer().Export(str(p));self.kontrol(p)

    def test_usdz_paket(self):
        from pxr import UsdUtils,Sdf
        p,_=self.sahne();z=self.root/'sahne.usdz'
        self.assertTrue(UsdUtils.CreateNewUsdzPackage(Sdf.AssetPath(str(p)),str(z)))
        self.kontrol(z)

    def test_yalniz_kapali_isik(self):
        from pxr import Usd,UsdLux
        p=self.root/'kapali.usda';s=Usd.Stage.CreateNew(str(p))
        UsdLux.SphereLight.Define(s,'/Kapali').CreateIntensityAttr(0)
        s.GetRootLayer().Save()
        geo,lights,_=motor.usd_kopru_calistir(str(p),str(self.root/'bos.obj'))
        self.assertFalse(geo);self.assertEqual(lights[0]['siddet'],0)
        arms,_=motor.usd_armaturler(lights)
        self.assertEqual(arms[0]['guc'],0)

    def test_isik_kendi_ve_ust_gorunurlugu_korunur(self):
        from pxr import UsdGeom,UsdLux
        p,s=self.sahne()
        gizli=UsdGeom.Xform.Define(s,'/Gizli');gizli.MakeInvisible()
        UsdLux.SphereLight.Define(s,'/Gizli/AltIsik').CreateIntensityAttr(10)
        kendisi=UsdLux.RectLight.Define(s,'/KendisiGizli')
        UsdGeom.Imageable(kendisi).MakeInvisible()
        s.GetRootLayer().Save();once=p.read_bytes()
        geo,lights,_=motor.usd_kopru_calistir(str(p),str(self.root/'gorunur.obj'))
        self.assertTrue(geo);self.assertEqual([i['ad'] for i in lights],['Isik'])
        self.assertEqual(p.read_bytes(),once)

    def test_rect_dunya_boyutu_olcek_donus_ve_metre(self):
        from pxr import UsdGeom,UsdLux,Gf
        p,s=self.sahne()
        parent=UsdGeom.Xformable(s.GetPrimAtPath('/Sahne'))
        parent.AddRotateZOp().Set(30)
        parent.AddScaleOp().Set(Gf.Vec3f(2,3,1))
        light=UsdLux.RectLight(s.GetPrimAtPath('/Sahne/Isik'))
        light.CreateWidthAttr(200);light.CreateHeightAttr(400)
        s.GetRootLayer().Save();once=p.read_bytes()
        _,lights,_=motor.usd_kopru_calistir(str(p),str(self.root/'olcek.obj'))
        self.assertEqual((lights[0]['w'],lights[0]['h']),(4,12))
        self.assertEqual(p.read_bytes(),once)
        # local 90° dönüş, genişlik/uzunluğun hangi üst scale eksenine düştüğünü değiştirir.
        UsdGeom.Xformable(light).AddRotateZOp().Set(90)
        s.GetRootLayer().Save();once=p.read_bytes()
        _,lights,_=motor.usd_kopru_calistir(str(p),str(self.root/'olcek_donuk.obj'))
        self.assertEqual((lights[0]['w'],lights[0]['h']),(6,8))
        self.assertEqual(p.read_bytes(),once)

    def ornek_sahne(self,ad='ornekler'):
        from pxr import Usd,UsdGeom
        p=self.root/(ad+'.usda');s=Usd.Stage.CreateNew(str(p))
        UsdGeom.SetStageMetersPerUnit(s,1)
        UsdGeom.SetStageUpAxis(s,UsdGeom.Tokens.z)
        proto=UsdGeom.Xform.Define(s,'/Prototip')
        mesh=UsdGeom.Mesh.Define(s,'/Prototip/Ucgen')
        mesh.CreatePointsAttr([(0,0,0),(1,0,0),(0,1,0)])
        mesh.CreateFaceVertexCountsAttr([3]);mesh.CreateFaceVertexIndicesAttr([0,1,2])
        pi=UsdGeom.PointInstancer.Define(s,'/Ornekler')
        pi.GetPrototypesRel().SetTargets([proto.GetPath()])
        pi.CreateProtoIndicesAttr([0,0,0])
        pi.CreatePositionsAttr([(0,0,0),(5,0,0),(10,0,0)])
        return p,s,proto,mesh,pi

    def ornek_cevir(self,p,s):
        s.GetRootLayer().Save();once=p.read_bytes()
        hedef=self.root/'ornekler.obj'
        geo,_,_=motor.usd_kopru_calistir(str(p),str(hedef))
        self.assertTrue(geo);self.assertEqual(p.read_bytes(),once)
        satirlar=hedef.read_text().splitlines()
        vertices=[list(map(float,l.split()[1:])) for l in satirlar if l.startswith('v ')]
        faces=[l for l in satirlar if l.startswith('f ')]
        return vertices,faces

    def test_instance_gorunmez_id_dizi_eslesmesini_bozmaz(self):
        p,s,_,_,pi=self.ornek_sahne()
        pi.CreateIdsAttr([10,20,30]);pi.CreateInvisibleIdsAttr([20])
        v,f=self.ornek_cevir(p,s)
        self.assertEqual(len(f),2)
        self.assertEqual([v[0],v[3]],[[0,0,0],[10,0,0]])

    def test_instance_inactive_ve_id_yokken_indeks_maskesi(self):
        p,s,_,_,pi=self.ornek_sahne()
        pi.CreateInvisibleIdsAttr([1]);pi.DeactivateId(2)
        v,f=self.ornek_cevir(p,s)
        self.assertEqual(len(f),1);self.assertEqual(v[0],[0,0,0])

    def test_instance_prototip_alt_mesh_ve_dunya_donusumleri(self):
        from pxr import Gf
        p,s,proto,mesh,pi=self.ornek_sahne()
        proto.AddTranslateOp().Set(Gf.Vec3d(2,0,0))
        mesh.AddTranslateOp().Set(Gf.Vec3d(3,0,0))
        pi.AddTranslateOp().Set(Gf.Vec3d(11,0,0))
        pi.CreateProtoIndicesAttr([0]);pi.CreatePositionsAttr([(5,0,0)])
        v,f=self.ornek_cevir(p,s)
        self.assertEqual(len(f),1);self.assertEqual(v[0],[21,0,0])
        pi.AddRotateZOp().Set(90)
        v,f=self.ornek_cevir(p,s)
        self.assertEqual(len(f),1)
        for got,want in zip(v[0],[11,10,0]):self.assertAlmostEqual(got,want,places=6)

    def test_gecersiz_topoloji_dogrudan_ve_prototipte_reddedilir(self):
        from pxr import Usd,UsdGeom
        for prototip in (False,True):
            for durum,(indeks,sayilar) in enumerate((([0,1,9],[3]),([0,1,2],[4]))):
                with self.subTest(prototip=prototip,indeks=indeks,sayilar=sayilar):
                    if prototip:
                        p,s,_,mesh,_=self.ornek_sahne('gecersiz_prototip_'+str(durum))
                    else:
                        p=self.root/('gecersiz_'+str(durum)+'.usda');s=Usd.Stage.CreateNew(str(p))
                        mesh=UsdGeom.Mesh.Define(s,'/Ucgen')
                        mesh.CreatePointsAttr([(0,0,0),(1,0,0),(0,1,0)])
                    mesh.CreateFaceVertexCountsAttr(sayilar)
                    mesh.CreateFaceVertexIndicesAttr(indeks)
                    s.GetRootLayer().Save();once=p.read_bytes()
                    hedef=self.root/'gecersiz.obj'
                    with self.assertRaisesRegex(RuntimeError,'geçersiz mesh topolojisi'):
                        motor.usd_kopru_calistir(str(p),str(hedef))
                    self.assertFalse(hedef.exists());self.assertEqual(p.read_bytes(),once)


if __name__=='__main__':unittest.main()
