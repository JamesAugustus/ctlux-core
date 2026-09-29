# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Real conversions in a separate process using installed OpenUSD, without mocked pxr."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
from core import engine as engine


@unittest.skipUnless(importlib.util.find_spec('pxr'), 'A local USD environment is required')
class USDTest(unittest.TestCase):
    def setUp(self):
        (ROOT/'tests/.tmp').mkdir(exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=ROOT/'tests/.tmp')
        self.root=Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def scene(self):
        from pxr import Usd,UsdGeom,UsdLux,Gf
        p=self.root/'scene.usda';stage=Usd.Stage.CreateNew(str(p))
        UsdGeom.SetStageMetersPerUnit(stage,0.01)
        UsdGeom.SetStageUpAxis(stage,UsdGeom.Tokens.y)
        parent=UsdGeom.Xform.Define(stage,'/Scene')
        parent.AddTranslateOp().Set(Gf.Vec3d(100,200,50))
        mesh=UsdGeom.Mesh.Define(stage,'/Scene/Triangle')
        mesh.CreatePointsAttr([(0,0,0),(100,0,0),(0,100,0)])
        mesh.CreateFaceVertexCountsAttr([3]);mesh.CreateFaceVertexIndicesAttr([0,1,2])
        light=UsdLux.RectLight.Define(stage,'/Scene/Light')
        light.CreateIntensityAttr(2);light.CreateExposureAttr(1)
        stage.GetRootLayer().Save()
        return p,stage

    def check(self,p):
        target=self.root/(p.name+'.obj')
        geo,lights,_=engine.run_usd_bridge(str(p),str(target))
        self.assertTrue(geo);self.assertEqual(len(lights),1)
        self.assertEqual(lights[0]['position'],[1.0,-0.5,2.0])
        self.assertEqual(lights[0]['direction'],[0.0,1.0,0.0])
        self.assertEqual(lights[0]['intensity'],4)
        vertices=[list(map(float,l.split()[1:])) for l in target.read_text().splitlines() if l.startswith('v ')]
        self.assertEqual(vertices,[[1.0,-0.5,2.0],[2.0,-0.5,2.0],[1.0,-0.5,3.0]])

    def test_usda_meters_axis_and_light(self):
        p,_=self.scene();self.check(p)

    def test_usdc_binary(self):
        _,stage=self.scene();p=self.root/'scene.usdc'
        stage.GetRootLayer().Export(str(p));self.check(p)

    def test_usdz_package(self):
        from pxr import UsdUtils,Sdf
        p,_=self.scene();z=self.root/'scene.usdz'
        self.assertTrue(UsdUtils.CreateNewUsdzPackage(Sdf.AssetPath(str(p)),str(z)))
        self.check(z)

    def test_only_disabled_light(self):
        from pxr import Usd,UsdLux
        p=self.root/'disabled.usda';s=Usd.Stage.CreateNew(str(p))
        UsdLux.SphereLight.Define(s,'/Disabled').CreateIntensityAttr(0)
        s.GetRootLayer().Save()
        geo,lights,_=engine.run_usd_bridge(str(p),str(self.root/'empty.obj'))
        self.assertFalse(geo);self.assertEqual(lights[0]['intensity'],0)
        luminaires,_=engine.usd_luminaires(lights)
        self.assertEqual(luminaires[0]['power'],0)

    def test_light_own_and_parent_visibility_are_preserved(self):
        from pxr import UsdGeom,UsdLux
        p,s=self.scene()
        hidden=UsdGeom.Xform.Define(s,'/Hidden');hidden.MakeInvisible()
        UsdLux.SphereLight.Define(s,'/Hidden/ChildLight').CreateIntensityAttr(10)
        self_hidden=UsdLux.RectLight.Define(s,'/SelfHidden')
        UsdGeom.Imageable(self_hidden).MakeInvisible()
        s.GetRootLayer().Save();before=p.read_bytes()
        geo,lights,_=engine.run_usd_bridge(str(p),str(self.root/'visible.obj'))
        self.assertTrue(geo);self.assertEqual([i['name'] for i in lights],['Light'])
        self.assertEqual(p.read_bytes(),before)

    def test_rect_world_dimensions_scale_rotation_and_meters(self):
        from pxr import UsdGeom,UsdLux,Gf
        p,s=self.scene()
        parent=UsdGeom.Xformable(s.GetPrimAtPath('/Scene'))
        parent.AddRotateZOp().Set(30)
        parent.AddScaleOp().Set(Gf.Vec3f(2,3,1))
        light=UsdLux.RectLight(s.GetPrimAtPath('/Scene/Light'))
        light.CreateWidthAttr(200);light.CreateHeightAttr(400)
        s.GetRootLayer().Save();before=p.read_bytes()
        _,lights,_=engine.run_usd_bridge(str(p),str(self.root/'scale.obj'))
        self.assertEqual((lights[0]['w'],lights[0]['h']),(4,12))
        self.assertEqual(p.read_bytes(),before)
        # A local 90° rotation changes which parent scale axis determines width/height.
        UsdGeom.Xformable(light).AddRotateZOp().Set(90)
        s.GetRootLayer().Save();before=p.read_bytes()
        _,lights,_=engine.run_usd_bridge(str(p),str(self.root/'rotated_scale.obj'))
        self.assertEqual((lights[0]['w'],lights[0]['h']),(6,8))
        self.assertEqual(p.read_bytes(),before)

    def fixture_scene(self,name='instances'):
        from pxr import Usd,UsdGeom
        p=self.root/(name+'.usda');s=Usd.Stage.CreateNew(str(p))
        UsdGeom.SetStageMetersPerUnit(s,1)
        UsdGeom.SetStageUpAxis(s,UsdGeom.Tokens.z)
        proto=UsdGeom.Xform.Define(s,'/Prototype')
        mesh=UsdGeom.Mesh.Define(s,'/Prototype/Triangle')
        mesh.CreatePointsAttr([(0,0,0),(1,0,0),(0,1,0)])
        mesh.CreateFaceVertexCountsAttr([3]);mesh.CreateFaceVertexIndicesAttr([0,1,2])
        pi=UsdGeom.PointInstancer.Define(s,'/Instances')
        pi.GetPrototypesRel().SetTargets([proto.GetPath()])
        pi.CreateProtoIndicesAttr([0,0,0])
        pi.CreatePositionsAttr([(0,0,0),(5,0,0),(10,0,0)])
        return p,s,proto,mesh,pi

    def convert_fixture(self,p,s):
        s.GetRootLayer().Save();before=p.read_bytes()
        target=self.root/'instances.obj'
        geo,_,_=engine.run_usd_bridge(str(p),str(target))
        self.assertTrue(geo);self.assertEqual(p.read_bytes(),before)
        lines=target.read_text().splitlines()
        vertices=[list(map(float,l.split()[1:])) for l in lines if l.startswith('v ')]
        faces=[l for l in lines if l.startswith('f ')]
        return vertices,faces

    def test_invisible_instance_id_preserves_array_mapping(self):
        p,s,_,_,pi=self.fixture_scene()
        pi.CreateIdsAttr([10,20,30]);pi.CreateInvisibleIdsAttr([20])
        v,f=self.convert_fixture(p,s)
        self.assertEqual(len(f),2)
        self.assertEqual([v[0],v[3]],[[0,0,0],[10,0,0]])

    def test_inactive_instance_and_index_mask_without_ids(self):
        p,s,_,_,pi=self.fixture_scene()
        pi.CreateInvisibleIdsAttr([1]);pi.DeactivateId(2)
        v,f=self.convert_fixture(p,s)
        self.assertEqual(len(f),1);self.assertEqual(v[0],[0,0,0])

    def test_instance_prototype_child_mesh_and_world_transforms(self):
        from pxr import Gf
        p,s,proto,mesh,pi=self.fixture_scene()
        proto.AddTranslateOp().Set(Gf.Vec3d(2,0,0))
        mesh.AddTranslateOp().Set(Gf.Vec3d(3,0,0))
        pi.AddTranslateOp().Set(Gf.Vec3d(11,0,0))
        pi.CreateProtoIndicesAttr([0]);pi.CreatePositionsAttr([(5,0,0)])
        v,f=self.convert_fixture(p,s)
        self.assertEqual(len(f),1);self.assertEqual(v[0],[21,0,0])
        pi.AddRotateZOp().Set(90)
        v,f=self.convert_fixture(p,s)
        self.assertEqual(len(f),1)
        for got,want in zip(v[0],[11,10,0]):self.assertAlmostEqual(got,want,places=6)

    def test_invalid_topology_is_rejected_directly_and_in_prototype(self):
        from pxr import Usd,UsdGeom
        for prototype in (False,True):
            for status,(index,numbers) in enumerate((([0,1,9],[3]),([0,1,2],[4]))):
                with self.subTest(prototype=prototype,index=index,numbers=numbers):
                    if prototype:
                        p,s,_,mesh,_=self.fixture_scene('invalid_prototype_'+str(status))
                    else:
                        p=self.root/('invalid_'+str(status)+'.usda');s=Usd.Stage.CreateNew(str(p))
                        mesh=UsdGeom.Mesh.Define(s,'/Triangle')
                        mesh.CreatePointsAttr([(0,0,0),(1,0,0),(0,1,0)])
                    mesh.CreateFaceVertexCountsAttr(numbers)
                    mesh.CreateFaceVertexIndicesAttr(index)
                    s.GetRootLayer().Save();before=p.read_bytes()
                    target=self.root/'invalid.obj'
                    with self.assertRaisesRegex(RuntimeError,'invalid mesh topology'):
                        engine.run_usd_bridge(str(p),str(target))
                    self.assertFalse(target.exists());self.assertEqual(p.read_bytes(),before)


if __name__=='__main__':unittest.main()
