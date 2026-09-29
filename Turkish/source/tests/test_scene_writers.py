# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Sentetik CTScene: gerçek USDA/Assimp reader'ı + GLB binary sözleşmesi."""
import copy
import importlib.util
import json
import math
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib

ROOT = Path(__file__).resolve().parents[1]
from core.scene_writers import export_scene


def checker_png():
    def chunk(kind, payload):
        return struct.pack('>I', len(payload))+kind+payload+struct.pack('>I', zlib.crc32(kind+payload))
    # üst kırmızı/yeşil, alt mavi/beyaz, tek renk olsa UV yönü hatası gizlenirdi.
    pixels = b'\0\xff\0\0\0\xff\0\0\0\0\xff\xff\xff\xff'
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR', struct.pack('>IIBBBBB', 2, 2, 8, 2, 0, 0, 0))+chunk(b'IDAT', zlib.compress(pixels))+chunk(b'IEND', b'')


def scene_fixture():
    return {'schema': 'ctlux.scene', 'version': 1, 'name': 'Kafe "ışık" / 測試', 'units': 'm', 'up_axis': 'Z',
            'mesh': {'v': [0, 0, 0, 1, 0, 0, 1, 0, 1, 0, 0, 1], 'f': [0, 1, 2, 0, 2, 3],
                     'm': [0]*4, 'uv': [0, 0, 1, 0, 1, 1, 0, 1], 'uv_ok': [1]*4,
                     'normals': [0, -1, 0]*4,
                     'materials': [{'name': 'Ahşap / özel', 'color': [.5, .25, 1], 'texture': 'doku/checker.png'}]},
            'lights': [{'id': 'spot_1', 'name': 'Lamba "A" / 測試', 'type': 'spot', 'enabled': True,
                        'position': [1, 2, 3], 'direction': [0, 1, -1], 'color_linear': [1, .5, .25],
                        'cone_angle_degrees': 60, 'radius_m': .1, 'size_m': [.5, .4],
                        'intensity': {'value': 120, 'unit': 'cd', 'provenance': 'sentetik'},
                        'source': {'kind': 'test'}, 'original': {'ies': 'isik/deney.ies', 'guc': 10, 'renk': [1, .5, .25]}}],
            'warnings': []}


def read_glb(path):
    raw = path.read_bytes()
    magic, version, length = struct.unpack_from('<III', raw)
    if (magic, version, length) != (0x46546c67, 2, len(raw)):
        raise AssertionError('GLB başlığı geçersiz')
    chunks = []; offset = 12
    while offset < len(raw):
        size, kind = struct.unpack_from('<II', raw, offset)
        if size % 4 or offset+8+size > len(raw):
            raise AssertionError('GLB chunk boyutu/hizalaması geçersiz')
        chunks.append((kind, raw[offset+8:offset+8+size])); offset += 8+size
    if not chunks or chunks[0][0] != 0x4e4f534a:
        raise AssertionError('GLB ilk chunk JSON olmalı')
    return json.loads(chunks[0][1]), chunks[1][1] if len(chunks) > 1 else b''


def read_accessor(doc, binary, index):
    a = doc['accessors'][index]; view = doc['bufferViews'][a['bufferView']]
    component = {5126: 'f', 5125: 'I', 5123: 'H'}[a['componentType']]
    width = {'SCALAR': 1, 'VEC2': 2, 'VEC3': 3}[a['type']]
    offset = view.get('byteOffset', 0)+a.get('byteOffset', 0)
    count = width*a['count']; size = struct.calcsize('<'+component)*count
    if offset+size > view.get('byteOffset', 0)+view['byteLength'] or offset+size > len(binary):
        raise AssertionError('Accessor buffer dışına çıkıyor')
    return struct.unpack_from('<'+str(count)+component, binary, offset)


def rotate(q, v):
    x, y, z, w = q
    # quaternion matrisini bağımsız uygula, üretimdeki helper testten çağrılmaz.
    return [(1-2*(y*y+z*z))*v[0]+2*(x*y-z*w)*v[1]+2*(x*z+y*w)*v[2],
            2*(x*y+z*w)*v[0]+(1-2*(x*x+z*z))*v[1]+2*(y*z-x*w)*v[2],
            2*(x*z-y*w)*v[0]+2*(y*z+x*w)*v[1]+(1-2*(x*x+y*y))*v[2]]


class WriterTest(unittest.TestCase):
    def setUp(self):
        (ROOT/'tests/.tmp').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT/'tests/.tmp')
        self.root = Path(self.temp.name)
        self.scene = scene_fixture()
        self.png = checker_png()
        self.calls = []

    def tearDown(self):
        self.temp.cleanup()

    def resolver(self, ref):
        self.calls.append(ref)
        self.assertEqual(ref, 'doku/checker.png')
        return {'data': self.png, 'mime_type': 'image/png'}

    def write(self, fmt, scene=None, resolver=True, directory=None):
        return export_scene(fmt, self.scene if scene is None else scene, directory or self.root/fmt,
                            self.resolver if resolver is True else resolver if callable(resolver) else None)

    def test_all_formats_preserve_input_and_output_contract(self):
        before = copy.deepcopy(self.scene)
        for fmt in ('glb', 'obj', 'usda'):
            with self.subTest(fmt=fmt):
                result = self.write(fmt)
                self.assertEqual(result['main'], 'scene.'+fmt)
                self.assertIn(result['main'], result['files'])
                self.assertEqual(result['stats']['triangles'], 2)
                self.assertEqual(result['stats']['lights'], 1)
                self.assertEqual(result['stats']['bytes'], sum((self.root/fmt/p).stat().st_size for p in result['files']))
                self.assertFalse((self.root/fmt/'scene.ctlux.json').exists())
        self.assertEqual(self.scene, before)

    def test_glb_geometry_uv_texture_and_lights_are_standard_fields(self):
        self.write('glb'); doc, binary = read_glb(self.root/'glb/scene.glb')
        prim = doc['meshes'][0]['primitives'][0]
        self.assertEqual(read_accessor(doc, binary, prim['attributes']['POSITION']), (0,0,0, 1,0,0, 1,1,0, 0,1,0))
        self.assertEqual(read_accessor(doc, binary, prim['attributes']['TEXCOORD_0']), (0,1, 1,1, 1,0, 0,0))
        self.assertEqual(read_accessor(doc, binary, prim['attributes']['NORMAL']), (0,0,1)*4)
        self.assertEqual(read_accessor(doc, binary, prim['indices']), tuple(self.scene['mesh']['f']))
        pbr = doc['materials'][prim['material']]['pbrMetallicRoughness']
        self.assertEqual(pbr['baseColorFactor'], [.5,.25,1,1]); self.assertIn('baseColorTexture', pbr)
        image = doc['images'][0]; view = doc['bufferViews'][image['bufferView']]
        self.assertEqual(binary[view['byteOffset']:view['byteOffset']+view['byteLength']], self.png)
        self.assertEqual(image['mimeType'], 'image/png')
        light = doc['extensions']['KHR_lights_punctual']['lights'][0]
        self.assertEqual(light['type'], 'spot'); self.assertEqual(light['intensity'], 120)
        self.assertEqual(light['color'], [1,.5,.25]); self.assertAlmostEqual(light['spot']['outerConeAngle'], math.pi/6)
        self.assertEqual(light['extras']['ctlux'], self.scene['lights'][0])
        node = doc['nodes'][1]; self.assertEqual(node['translation'], [1,3,-2])
        self.assertEqual(node['name'], self.scene['lights'][0]['name'])
        expected = [0, -math.sqrt(.5), -math.sqrt(.5)]
        for a,b in zip(rotate(node['rotation'], [0,0,-1]), expected): self.assertAlmostEqual(a,b)
        for view in doc['bufferViews']:
            self.assertEqual(view['byteOffset'] % 4, 0)
            self.assertLessEqual(view['byteOffset']+view['byteLength'], doc['buffers'][0]['byteLength'])

    def test_orientation_axes_and_general_direction(self):
        directions = [[0,0,-1],[0,0,1],[1,0,0],[-1,0,0],[0,1,0],[0,-1,0],[1,2,3]]
        for i,d in enumerate(directions):
            scene = copy.deepcopy(self.scene); scene['lights'][0]['direction'] = d
            self.write('glb', scene, directory=self.root/str(i))
            doc,_ = read_glb(self.root/str(i)/'scene.glb'); q = doc['nodes'][1]['rotation']
            self.assertAlmostEqual(sum(x*x for x in q), 1)
            length = math.hypot(*d); want = [d[0]/length,d[2]/length,-d[1]/length]
            for a,b in zip(rotate(q,[0,0,-1]),want): self.assertAlmostEqual(a,b)

    def test_missing_uv_triangle_gets_color_only_material(self):
        self.scene['mesh']['uv_ok'][3] = 0
        result = self.write('glb'); doc,binary = read_glb(self.root/'glb/scene.glb')
        first,second = doc['meshes'][0]['primitives']
        self.assertEqual(read_accessor(doc,binary,first['indices']), (0,1,2))
        self.assertEqual(read_accessor(doc,binary,second['indices']), (0,2,3))
        self.assertIn('baseColorTexture',doc['materials'][first['material']]['pbrMetallicRoughness'])
        self.assertNotIn('baseColorTexture',doc['materials'][second['material']]['pbrMetallicRoughness'])
        self.assertNotIn('TEXCOORD_0',second['attributes'])
        self.assertTrue(any('UV' in x for x in result['warnings']))

    def test_valid_uv_without_texture_is_not_lost(self):
        del self.scene['mesh']['materials'][0]['texture']
        self.scene['mesh']['uv_ok'][3]=0
        self.write('glb');doc,binary=read_glb(self.root/'glb/scene.glb')
        first,second=doc['meshes'][0]['primitives']
        self.assertEqual(read_accessor(doc,binary,first['attributes']['TEXCOORD_0']),(0,1,1,1,1,0,0,0))
        self.assertNotIn('TEXCOORD_0',second['attributes']);self.assertNotIn('images',doc)
        self.assertEqual(self.calls,[])

    def test_bad_texture_preserves_valid_base_color(self):
        for i,resolver in enumerate([False, lambda _:None, lambda _:{'data':self.png,'mime_type':'image/jpeg'}]):
            self.write('glb',resolver=resolver,directory=self.root/str(i))
            doc,_ = read_glb(self.root/str(i)/'scene.glb')
            self.assertNotIn('images',doc)
            self.assertEqual(doc['materials'][0]['pbrMetallicRoughness']['baseColorFactor'],[.5,.25,1,1])
            self.assertTrue(any('temel renk korundu' in x for x in doc['extras']['ctlux']['warnings']))

    def test_relative_area_closed_light_warns_without_source_mutation(self):
        light = self.scene['lights'][0]; light['type']='area'; light['enabled']=False
        light['intensity']={'value':52,'unit':'relative','provenance':'deney'}
        original=copy.deepcopy(light); result=self.write('glb');doc,_=read_glb(self.root/'glb/scene.glb')
        target=doc['extensions']['KHR_lights_punctual']['lights'][0]
        self.assertEqual(target['type'],'point');self.assertEqual(target['intensity'],0)
        self.assertEqual(target['extras']['ctlux'],original);self.assertEqual(light,original)
        self.assertTrue(any('alan ışığı' in w for w in result['warnings']))
        self.assertTrue(any('kalibre edilmemiş' in w for w in result['warnings']))
        self.assertTrue(any('IES' in w for w in result['warnings']))

    def test_obj_preserves_coordinate_uv_normals_and_asset_bytes(self):
        result=self.write('obj');obj=(self.root/'obj/scene.obj').read_text();mtl=(self.root/'obj/scene.mtl').read_text()
        self.assertIn('v 1 0 1',obj);self.assertIn('vt 0 0',obj);self.assertIn('f 1/1/1 2/2/2 3/3/3',obj)
        self.assertIn('illum 1',mtl);self.assertNotIn('illum 2',mtl);self.assertIn('Kd 0.5 0.25 1',mtl);self.assertTrue(any('OBJ standart ışık taşımaz' in w for w in result['warnings']))
        asset=next(p for p in result['files'] if p.startswith('assets/'))
        self.assertIn('map_Kd '+asset,mtl);self.assertEqual((self.root/'obj'/asset).read_bytes(),self.png)

    def test_light_only_glb_is_valid_and_warns_geometry_requirement(self):
        self.scene['mesh']={};result=self.write('glb');doc,binary=read_glb(self.root/'glb/scene.glb')
        self.assertNotIn('meshes',doc);self.assertEqual(binary,b'');self.assertEqual(len(doc['nodes']),1)
        self.assertTrue(any('yüzey geometrisi yok' in w for w in result['warnings']))

    def test_empty_glb_has_no_invalid_empty_optional_node_arrays(self):
        self.scene['mesh']={};self.scene['lights']=[];self.write('glb')
        doc,binary=read_glb(self.root/'glb/scene.glb')
        self.assertNotIn('nodes',doc);self.assertNotIn('nodes',doc['scenes'][0]);self.assertEqual(binary,b'')

    def test_invalid_topology_fails_before_creating_outputs(self):
        for key,value in [('v',[0,0]),('v',[float('nan'),0,0]),('f',[0,1,9]),('f',[0,1]),
                          ('f',[0,0,1]),('m',[0,0,0,3]),('uv',[0,1]),('normals',[0]*12)]:
            with self.subTest(key=key,value=value):
                scene=copy.deepcopy(self.scene);scene['mesh'][key]=value
                with self.assertRaises(ValueError):self.write('glb',scene)
                self.assertFalse((self.root/'glb').exists())

    def test_output_collision_and_asset_symlink_are_rejected(self):
        self.write('obj');before=(self.root/'obj/scene.obj').read_bytes()
        with self.assertRaises(FileExistsError):self.write('obj')
        self.assertEqual((self.root/'obj/scene.obj').read_bytes(),before)
        outside=self.root/'outside';outside.mkdir();directory=self.root/'linked';directory.mkdir()
        (directory/'assets').symlink_to(outside,target_is_directory=True)
        with self.assertRaises(ValueError):self.write('obj',directory=directory)
        self.assertEqual(list(outside.iterdir()),[])

    @unittest.skipUnless(importlib.util.find_spec('pxr'),'Yerel usd-core gerekli')
    def test_usda_real_parser_mesh_material_binding_texture_and_light(self):
        from pxr import Usd,UsdGeom,UsdLux,UsdShade,Gf
        self.scene['mesh']['uv_ok'][3]=0;result=self.write('usda')
        stage=Usd.Stage.Open(str(self.root/'usda/scene.usda'));self.assertTrue(stage)
        self.assertEqual(UsdGeom.GetStageUpAxis(stage),'Z');self.assertEqual(UsdGeom.GetStageMetersPerUnit(stage),1)
        self.assertEqual(stage.GetDefaultPrim().GetDisplayName(),self.scene['name'])
        mesh=UsdGeom.Mesh(stage.GetPrimAtPath('/Scene/Geometry'))
        self.assertEqual(list(mesh.GetFaceVertexIndicesAttr().Get()),self.scene['mesh']['f'])
        self.assertEqual(list(mesh.GetFaceVertexCountsAttr().Get()),[3,3])
        self.assertEqual(tuple(mesh.GetPointsAttr().Get()[2]),(1,0,1))
        uv=UsdGeom.PrimvarsAPI(mesh).GetPrimvar('st');self.assertEqual(uv.GetInterpolation(),'vertex')
        self.assertEqual(tuple(uv.Get()[0]),(0,0))
        subset=stage.GetPrimAtPath('/Scene/Geometry/M0');mat,_=UsdShade.MaterialBindingAPI(subset).ComputeBoundMaterial()
        self.assertEqual(str(mat.GetPath()),'/Scene/Materials/M0')
        shader=UsdShade.Shader(stage.GetPrimAtPath('/Scene/Materials/M0/Texture'))
        self.assertEqual(shader.GetIdAttr().Get(),'UsdUVTexture')
        self.assertEqual(shader.GetInput('sourceColorSpace').Get(),'sRGB')
        file=shader.GetInput('file').Get();self.assertEqual(Path(file.resolvedPath).read_bytes(),self.png)
        fallback=UsdShade.Shader(stage.GetPrimAtPath('/Scene/Materials/M1/Preview'))
        self.assertFalse(fallback.GetInput('diffuseColor').HasConnectedSource())
        self.assertEqual(tuple(fallback.GetInput('diffuseColor').Get()),(.5,.25,1))
        prim=stage.GetPrimAtPath('/Scene/Light0');self.assertTrue(prim.IsA(UsdLux.SphereLight))
        shaping=UsdLux.ShapingAPI(prim);self.assertTrue(shaping);self.assertEqual(shaping.GetShapingConeAngleAttr().Get(),30)
        light=UsdLux.LightAPI(prim);self.assertEqual(light.GetIntensityAttr().Get(),120)
        self.assertEqual(json.loads(prim.GetCustomDataByKey('ctlux')),self.scene['lights'][0])
        matrix=UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(Usd.TimeCode.Default())
        self.assertEqual(tuple(matrix.Transform(Gf.Vec3d(0,0,0))),(1,2,3))
        direction=matrix.TransformDir(Gf.Vec3d(0,0,-1)).GetNormalized()
        for a,b in zip(direction,[0,math.sqrt(.5),-math.sqrt(.5)]):self.assertAlmostEqual(a,b,places=6)
        self.assertTrue(any('fotometrik eşdeğerlik' in w for w in result['warnings']))

    @unittest.skipUnless(importlib.util.find_spec('pxr'),'Yerel usd-core gerekli')
    def test_usda_types_duplicate_names_and_disabled_intensity(self):
        from pxr import Usd,UsdLux
        self.scene['lights']=[dict(copy.deepcopy(self.scene['lights'][0]),type=t,enabled=t!='point') for t in ('point','area','distant')]
        self.write('usda');stage=Usd.Stage.Open(str(self.root/'usda/scene.usda'))
        for i,kind in enumerate([UsdLux.SphereLight,UsdLux.RectLight,UsdLux.DistantLight]):
            prim=stage.GetPrimAtPath('/Scene/Light'+str(i));self.assertTrue(prim.IsA(kind))
            self.assertEqual(prim.GetDisplayName(),self.scene['lights'][i]['name'])
        self.assertEqual(UsdLux.LightAPI(stage.GetPrimAtPath('/Scene/Light0')).GetIntensityAttr().Get(),0)
        area=UsdLux.RectLight(stage.GetPrimAtPath('/Scene/Light1'))
        self.assertAlmostEqual(area.GetWidthAttr().Get(),.5);self.assertAlmostEqual(area.GetHeightAttr().Get(),.4)

    @unittest.skipUnless(shutil.which('assimp'),'Assimp gerekli')
    def test_glb_and_obj_open_in_independent_assimp_reader(self):
        for fmt in ('glb','obj'):
            self.write(fmt)
            run=subprocess.run(['assimp','info',str(self.root/fmt/('scene.'+fmt))],capture_output=True,text=True,timeout=20)
            self.assertEqual(run.returncode,0,run.stdout+'\n'+run.stderr)
            self.assertIn('Faces:              2',run.stdout)
            if fmt=='glb':self.assertRegex(run.stdout,r'Lights:\s+1')


class CommentBangTest(unittest.TestCase):
    def test_ies_header_bang_leaves_comments_only(self):
        from core.scene_writers import _yorum_unlemlerini_sil
        text = '# xform -n a\n#<[LUMINAIRE] Super bright!\n  # note!\nvoid brightdata a_dist\n'
        self.assertEqual(_yorum_unlemlerini_sil(text), '# xform -n a\n#<[LUMINAIRE] Super bright\n  # note\nvoid brightdata a_dist\n')
        self.assertEqual(_yorum_unlemlerini_sil('void light m!\n'), 'void light m!\n')



# C0 1000 cd, C180 hiç ışık vermez. Aydınlık, C0 düzleminin hangi yana baktığını gösterir.
ASIMETRIK_IES = ('IESNA:LM-63-2002\n[TEST] asymmetric\nTILT=NONE\n1 -1 1 3 5 1 2 0.1 0.1 0\n1 1 100\n'
                  '0 45 90\n0 90 180 270 360\n1000 1000 1000\n500 500 500\n0 0 0\n500 500 500\n1000 1000 1000\n')
YONELIM_ARACLARI = all(shutil.which(x) for x in ('ies2rad', 'xform', 'oconv', 'rtrace'))


class FotometriYonelimTest(unittest.TestCase):
    def rotate(self, angles, v):
        a, b, c = (math.radians(x) for x in angles)
        x, y, z = v
        y, z = y*math.cos(a) - z*math.sin(a), y*math.sin(a) + z*math.cos(a)
        x, z = x*math.cos(b) + z*math.sin(b), -x*math.sin(b) + z*math.cos(b)
        return [x*math.cos(c) - y*math.sin(c), x*math.sin(c) + y*math.cos(c), z]

    def test_nadir_ve_c0_yon_ve_eksene_uyar(self):
        from core.engine import fotometri_donusu
        s = math.sqrt(.5)
        for direction, c0, expected_c0 in (([0, 0, -1], None, [1, 0, 0]), ([0, 0, -1], [0, 1, 0], [0, 1, 0]),
                                           ([0, 0, -1], [-1, 0, 0], [-1, 0, 0]), ([0, 0, 1], None, [1, 0, 0]),
                                           ([1, 0, 0], None, [0, 1, 0]), ([s, 0, -s], None, [s, 0, s]),
                                           ([0, s, -s], [1, 0, 0], [1, 0, 0]), ([0, 0, -2], [3, 0, 1], [1, 0, 0])):
            with self.subTest(direction=direction, c0=c0):
                angles = fotometri_donusu(direction, c0)
                length = math.hypot(*direction)
                for got, want in zip(self.rotate(angles, [0, 0, -1]), [v/length for v in direction]):
                    self.assertAlmostEqual(got, want, places=12)
                for got, want in zip(self.rotate(angles, [1, 0, 0]), expected_c0):
                    self.assertAlmostEqual(got, want, places=12)
        with self.assertRaises(ValueError):
            fotometri_donusu([0, 0, 0])

    @unittest.skipUnless(YONELIM_ARACLARI, 'Yerel Radiance gerekli')
    def test_asimetrik_ies_c0_yani_radiancede(self):
        from core import engine as motor
        from core.engine import fotometri_donusu
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root/'a.ies').write_text(ASIMETRIK_IES)
            env = motor.radiance_ortami()
            subprocess.run(['ies2rad', '-m', '1', '-o', 'a', 'a.ies'], cwd=root, env=env, check=True, capture_output=True)

            def lit(c0):
                angles = fotometri_donusu([0, 0, -1], c0)
                placed = subprocess.run(['xform', '-rx', repr(angles[0]), '-ry', repr(angles[1]), '-rz', repr(angles[2]),
                                         'a.rad'], cwd=root, env=env, check=True, capture_output=True, text=True).stdout
                octree = subprocess.run(['oconv', '-'], input=placed.encode(), cwd=root, env=env,
                                        check=True, capture_output=True).stdout
                (root/'s.oct').write_bytes(octree)
                probes = '1 0 -1 0 0 1\n-1 0 -1 0 0 1\n0 1 -1 0 0 1\n0 -1 -1 0 0 1\n'
                out = subprocess.run(['rtrace', '-h', '-I', '-ab', '0', '-w', 's.oct'], input=probes, cwd=root,
                                     env=env, check=True, capture_output=True, text=True).stdout
                return dict(zip(('+x', '-x', '+y', '-y'), (float(line.split()[0]) for line in out.splitlines())))

            default, turned = lit(None), lit([0, 1, 0])
            self.assertGreater(default['+x'], 0)
            self.assertEqual(default['-x'], 0)
            self.assertGreater(turned['+y'], 0)
            self.assertEqual(turned['-y'], 0)

if __name__=='__main__':
    unittest.main()
