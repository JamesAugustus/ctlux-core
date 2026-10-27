# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Tam CTScene aktarımı, sentetik kaynaklarla, önizleme/GPU ya da native writer testi değil."""
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
from core import scene_model as scene
try:
    from jsonschema import Draft202012Validator
except ImportError:
    Draft202012Validator = None

SCHEMA_FILE = ROOT / 'tests/CTSCENE_V1.schema.json'


class SahneDiliTest(unittest.TestCase):
    def setUp(self):
        (ROOT / 'tests/.tmp').mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / 'tests/.tmp', prefix='ctscene_')
        self.root = Path(self.tmp.name)
        self.pd = self.root / 'project'
        (self.pd / 'model').mkdir(parents=True)
        (self.pd / 'malzeme').mkdir()
        self.obj = self.pd / 'model/model.obj'
        self.obj.write_text('mtllib colors.mtl\no full\nv 0 0 0\nv 1 0 0\nv 0 1 0\nusemtl red\nf 1 2 3\n')
        (self.pd / 'model/colors.mtl').write_text('newmtl red\nKd .2 .3 .4\n')
        self.project = {'ad': 'Sentetik sahne', 'geometri': [{'dosya': 'model/model.obj'}]}

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, lights=None):
        return scene.scene_from_project(self.pd, self.project, lights)

    def digest(self):
        return {p.relative_to(self.pd).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.pd.rglob('*') if p.is_file()}

    def test_full_geometry_no_preview_quota_no_cache_or_source_write(self):
        self.obj.write_text(self.obj.read_text() + 'o second\nf 1 2 3\n')
        self.project['onizleme_ucgen'] = 1
        before, project = self.digest(), deepcopy(self.project)
        out = self.build()
        self.assertEqual(len(out['mesh']['f'])//3, 2)
        self.assertEqual(out['mesh']['materials'], [{'name': 'red', 'color': [.2, .3, .4]}])
        self.assertEqual((out['units'], out['up_axis']), ('m', 'Z'))
        self.assertIs(scene.validate_scene(out), out)
        self.assertEqual(json.loads(json.dumps(out, allow_nan=False)), out)
        self.assertEqual(self.digest(), before)
        self.assertEqual(self.project, project)
        self.assertFalse((self.pd / '_cache').exists())

    def test_tekrarli_koseli_ucgen_atlanir_ve_sayilir(self):
        self.obj.write_text(self.obj.read_text() + 'f 1 1 2\nf 1 2 3\n')
        out = self.build()
        self.assertEqual(len(out['mesh']['f'])//3, 2)
        self.assertTrue(any('1 tekrarlı köşeli' in w and 'kaynak dosya değiştirilmedi' in w
                            for w in out['warnings']))
        self.assertEqual(self.obj.read_text().count('f 1 1 2'), 1)

    def test_obj_scale_x_rotation_applied_once_and_uv_seams_preserved(self):
        self.obj.write_text('mtllib colors.mtl\nv 0 0 0\nv 1 0 0\nv 0 1 0\n'
                            'vt 0 0\nvt 1 0\nvt 0 1\nvt .5 .5\nusemtl red\n'
                            'f -3/-4 -2/-3 -1/-2\nf 1/4 2/2 3/3\n')
        self.project['geometri'][0].update(olcek=2, rx=90)
        out = self.build()['mesh']
        pts = list(zip(*[iter(out['v'])]*3))
        self.assertEqual(len(pts), 4, 'UV seam ayrı köşe gerektirir')
        self.assertTrue(any(abs(x) < 1e-12 and abs(y) < 1e-12 and abs(z-2) < 1e-12 for x, y, z in pts))
        self.assertIn((2, 0, 0), pts)
        self.assertEqual(out['uv_ok'], [1]*4)
        self.assertIn(.5, out['uv'])

    def test_material_boundary_and_full_texture_relative_link(self):
        # texture bağlantısı, PNG/JPEG okuyucusunun filtresinden bağımsız aktarılır.
        texture = self.pd / 'model/albedo.webp'
        texture.write_bytes(b'RIFF-synthetic-unread-texture')
        (self.pd / 'model/colors.mtl').write_text('newmtl red\nKd .2 .3 .4\nmap_Kd albedo.webp\nnewmtl blue\nKd .1 .2 .9\n')
        self.obj.write_text(self.obj.read_text() + 'usemtl blue\nf 1 2 3\n')
        out = self.build()['mesh']
        self.assertEqual(len(out['v'])//3, 6)
        self.assertEqual(out['materials'][0]['texture'], 'model/albedo.webp')
        self.assertNotIn('texture', out['materials'][1])
        self.assertEqual(out['materials'][1]['color'], [.1, .2, .9])
        self.assertNotIn(str(self.pd), json.dumps(out))

    def test_concave_polygon_not_incorrect_triangle_fan(self):
        points = [(0, 0), (3, 0), (3, 3), (2, 3), (2, 1), (1, 1), (1, 3), (0, 3)]
        self.obj.write_text(''.join('v %s %s 0\n' % p for p in points) + 'f 1 2 3 4 5 6 7 8\n')
        mesh = self.build()['mesh']
        v = list(zip(*[iter(mesh['v'])]*3))
        area = 0
        for i in range(0, len(mesh['f']), 3):
            a, b, c = [v[k] for k in mesh['f'][i:i+3]]
            signed = ((b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0]))/2
            self.assertGreater(signed, 0)
            area += signed
            cx, cy = sum(p[0] for p in (a, b, c))/3, sum(p[1] for p in (a, b, c))/3
            self.assertFalse(1 < cx < 2 and cy > 1, 'İçbükey boşluk doldurulmamalı')
        self.assertEqual(area, 7)
        self.assertEqual(len(mesh['f'])//3, 6)

    def test_rad_polygon_and_sphere_world_scale_without_primitive_loss(self):
        rad = self.pd / 'model/test.rad'
        rad.write_text('void plastic red\n0\n0\n5 .4 .5 .6 0 0\n'
                       'red polygon floor\n0\n0\n12 -1 -1 0 1 -1 0 1 1 0 -1 1 0\n'
                       'red sphere body\n0\n0\n4 0 2 0 1\n')
        self.project['geometri'] = [{'dosya': 'model/test.rad', 'olcek': 2, 'rx': 90}]
        out = self.build()
        self.assertEqual(len(out['mesh']['f'])//3, 530)
        self.assertEqual(out['mesh']['materials'][0]['color'], [.4, .5, .6])
        self.assertTrue(any('sphere' in w and 'yaklaşık' in w for w in out['warnings']))
        zs = out['mesh']['v'][2::3]
        self.assertAlmostEqual(max(zs), 6)
        self.assertAlmostEqual(min(zs), -2)

    def test_rtm_uses_original_obj_and_rechecks_symlink_boundary(self):
        self.project['geometri'][0]['dosya'] = 'model/model.rtm'
        (self.pd / 'model/model.rtm').write_bytes(b'not a text mesh')
        self.assertEqual(len(self.build()['mesh']['f']), 3)
        self.obj.unlink()
        outside = self.root / 'outside.obj'
        outside.write_text('v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n')
        self.obj.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'proje dışında'):
            self.build()

    def test_missing_unsupported_and_empty_geometry_fail_explicitly(self):
        for name, body in [('missing.obj', None), ('binary.rtm', b'binary'), ('empty.obj', b'v 0 0 0\n')]:
            with self.subTest(name=name):
                if body is not None:
                    (self.pd / 'model' / name).write_bytes(body)
                self.project['geometri'] = [{'dosya': 'model/'+name}]
                with self.assertRaises(ValueError):
                    self.build()
        self.project['geometri'] = []
        with self.assertRaisesRegex(ValueError, 'geometrisi yok'):
            self.build()

    def test_rad_commands_instances_mesh_and_truncated_record_rejected(self):
        marker = self.pd / 'executed'
        for body in ['!touch '+str(marker), 'void instance a\n1 model.oct\n0\n0\n',
                     'void mesh a\n1 model.rtm\n0\n0\n', 'void sphere a\n0\n0\n4 0 0']:
            with self.subTest(body=body):
                (self.pd / 'model/test.rad').write_text(body)
                self.project['geometri'] = [{'dosya': 'model/test.rad'}]
                with self.assertRaises(ValueError):
                    self.build()
                self.assertFalse(marker.exists())

    def test_obj_invalid_index_nan_and_freeform_are_not_silently_skipped(self):
        base = 'v 0 0 0\nv 1 0 0\nv 0 1 0\n'
        for body in [base+'f 1 2 99\n', base+'f 0 1 2\n', base+'f 1/a 2 3\n',
                     base+'v nan 2 3\nf 1 2 3\n', base+'curv 0 1 1 2 3\n']:
            with self.subTest(body=body):
                self.obj.write_text(body)
                with self.assertRaises(ValueError):
                    self.build()

    def test_budget_errors_not_preview_sampling(self):
        self.obj.write_text(self.obj.read_text() + 'f 1 2 3\n')
        with self.assertRaisesRegex(ValueError, '2 üçgen.*--ucgen-siniri'):
            scene.scene_from_project(self.pd, self.project, max_triangles=1)
        with self.assertRaisesRegex(ValueError, 'MiB.*--kaynak-siniri-mib'):
            scene.scene_from_project(self.pd, self.project, max_source_bytes=8)
        result = scene.scene_from_project(self.pd, self.project, max_triangles=2, max_source_bytes=4096)
        self.assertEqual(len(result['mesh']['f']), 6)
        self.assertEqual(scene.MAX_TRIANGLES, 3_000_000)
        self.assertEqual(scene.MAX_SOURCE_BYTES, 1024 * 1048576)
        self.assertFalse((self.pd / '_cache').exists())

    def test_komut_kaynak_ve_ucgen_sinirlari(self):
        from core.__main__ import main
        from contextlib import redirect_stdout, redirect_stderr
        import io
        # Büyük yorum dosyayı 1 MiB üzerine çıkarır, geometriyi değiştirmez.
        self.obj.write_text(self.obj.read_text() + '#' + 'x' * 1048576 + '\nf 1 2 3\n')
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main([str(self.obj), str(self.root/'varsayilan')]), 0)
        self.assertIn('1024 MiB', (self.root/'varsayilan/RAPOR.txt').read_text())
        for name, options, code in (
            ('dar-kaynak', ['--kaynak-siniri-mib', '1'], 1),
            ('dar-ucgen', ['--kaynak-siniri-mib', '2', '--ucgen-siniri', '1'], 1),
            ('genis', ['--kaynak-siniri-mib', '2', '--ucgen-siniri', '2'], 0)):
            out = self.root/name
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(main([str(self.obj), str(out), *options]), code)
            report = (out/'RAPOR.txt').read_text()
            self.assertIn('MiB', report)
            if code:
                self.assertIn('--kaynak-siniri-mib' if name == 'dar-kaynak' else '--ucgen-siniri', report)
            else:
                self.assertEqual(len(json.loads((out/'scene.ctlux.json').read_text())['mesh']['f']), 6)
        for option in ('--kaynak-siniri-mib', '--ucgen-siniri'):
            for value in ('0', '-1', 'nan', 'abc', '1.5'):
                with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                    main([str(self.obj), str(self.root/'invalid'), option, value])
                self.assertEqual(caught.exception.code, 2)
        self.assertFalse((self.root/'invalid').exists())

    def test_external_material_or_texture_symlink_rejected(self):
        outside = self.root / 'outside.mtl'
        outside.write_text('newmtl red\nKd 1 0 0\n')
        local = self.pd / 'model/colors.mtl'
        local.unlink(); local.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'MTL/doku'):
            self.build()

    def test_light_metadata_override_zero_relative_power_and_stable_ids(self):
        lights = [{'ad': 'Düzenlenen ad', 'urun_ad': 'Özgün ürün', 'kaynak_id': 'EVO#77',
                   'urun_id': 'P42', 'dizi_id': 'D8', 'kaynak': 'evo/STEP', 'x': 4, 'y': -5, 'z': 6,
                   'yon': [0, 0, -3], 'renk': [.8, .3, .1], 'guc': 0, 'etkin': False,
                   'tip': 'spot', 'aci': 90, 'extra': {'bilinmeyen': ['öğe', None, 7]}},
                  {'ad': 'Nokta', 'tip': 'omni', 'guc': 12345, 'renk': [0, 0, 0]}]
        original = deepcopy(lights)
        self.project['armaturler'] = [{'ad': 'Disk kaydı', 'tip': 'omni'}]
        out = self.build(lights)
        a, b = out['lights']
        self.assertEqual(a['name'], 'Düzenlenen ad')
        self.assertEqual(a['position'], [4, -5, 6])
        self.assertEqual(a['direction'], [0, 0, -1])
        self.assertEqual(a['intensity']['value'], 0)
        self.assertFalse(a['enabled'])
        self.assertEqual(a['source']['dizi_id'], 'D8')
        self.assertEqual(a['source']['urun_id'], 'P42')
        self.assertEqual(a['original'], lights[0])
        self.assertEqual(b['color_linear'], [0, 0, 0])
        self.assertEqual(b['intensity']['unit'], 'relative')
        self.assertEqual(b['intensity']['value'], 12345)
        lights[0]['x'] = 7
        self.assertEqual(self.build(lights)['lights'][0]['id'], a['id'])
        lights[0]['extra']['bilinmeyen'].append(1)
        self.assertEqual(a['original'], original[0], 'original derin kopya olmalı')
        self.assertEqual(self.build([])['lights'], [], 'Boş override disk ışıklarını geri getirmez')

    def test_light_display_name_matches_editor_priority_and_zero_suffix(self):
        cases = [
            ({'ad': 'Özel ad', 'kaynak_ad': 'Kaynak adı', 'urun_ad': 'Ürün'}, 'Özel ad'),
            ({'ad': 'armatur_12_kopya', 'kaynak_ad': 'Kaynak adı'}, 'armatur_12_kopya'),
            ({'ad': 'armatur_12', 'kaynak_ad': 'Kaynak adı', 'urun_ad': 'Ürün'}, 'Kaynak adı'),
            ({'ad': 'armatur_12', 'urun_ad': 'Ürün', 'kaynak_id': 0}, 'Ürün · 0'),
            ({'ad': 'armatur_12', 'urun_ad': 'Ürün', 'kaynak_id': 'E77'}, 'Ürün · E77'),
            ({'ad': 'armatur_12', 'urun_ad': 'Ürün'}, 'Ürün · armatur_12'),
            ({'ad': 'armatur_12'}, 'armatur_12'), ({'urun_ad': 'Ürün'}, 'Ürün'),
            ({}, 'Adsız ışık')]
        records = [case[0] for case in cases]
        original = deepcopy(records)
        out = self.build(records)
        self.assertEqual([light['name'] for light in out['lights']], [case[1] for case in cases])
        self.assertEqual([light['original'] for light in out['lights']], original)
        self.assertEqual(records, original)

    def test_source_guid_namespace_and_zero_id_survive_reordering_and_rename(self):
        records = [
            {'kaynak': 'evo/STEP', 'kaynak_id': 0, 'ad': 'Sıfır'},
            {'kaynak': 'evo/STEP', 'kaynak_id': 1, 'ad': 'Bir'},
            {'kaynak': 'lumion', 'kaynak_id': 0, 'ad': 'Başka kaynak'},
            {'kaynak': 'evo/STEP', 'kaynak_guid': 'abc-guid', 'kaynak_id': 88, 'id': 7, 'ad': 'GUID'},
            {'kaynak': 'evo/STEP', 'id': 0, 'ad': 'Yerel sıfır'}]
        first = self.build(records)['lights']
        self.assertEqual(len({light['id'] for light in first}), len(records))
        again = self.build(list(reversed(records)))['lights']
        self.assertEqual([light['id'] for light in again], [light['id'] for light in reversed(first)])
        changed = deepcopy(records)
        for r in changed:
            r.update(ad='Yeni görünen ad', x=123)
        changed[3].update(kaynak_id=999, id=888)
        last = self.build(changed)['lights']
        self.assertEqual([light['id'] for light in last], [light['id'] for light in first])
        self.assertEqual(last[3]['source']['kaynak_guid'], 'abc-guid')
        self.assertEqual(last[0]['source']['kaynak_id'], 0)

    def test_duplicate_source_identity_is_explicit_and_unrelated_order_does_not_change_suffix(self):
        a = {'kaynak': 'evo/STEP', 'kaynak_id': 77}
        out = self.build([a, deepcopy(a)])
        self.assertNotEqual(out['lights'][0]['id'], out['lights'][1]['id'])
        self.assertTrue(any('Tekrarlanan kaynak' in w for w in out['warnings']))
        again = self.build([{'kaynak': 'other', 'id': 'other'}, a, deepcopy(a)])
        self.assertEqual([light['id'] for light in again['lights'][1:]], [light['id'] for light in out['lights']])
        no_ids = self.build([{'ad': 'A'}, {'ad': 'B'}])
        self.assertNotEqual(no_ids['lights'][0]['id'], no_ids['lights'][1]['id'])

    def test_ies_dimmer_not_mislabeled_cd_and_original_paths_are_separate(self):
        raw = {'ad': 'IES', 'tip': 'spot', 'ies': 'isik/product.ies', 'guc': 99999, 'dimmer': 0,
               'urun_rad': 'C:\\sample\\product.rad', 'custom': {'path': '/sample/project/source'}}
        out = self.build([raw])
        light = out['lights'][0]
        self.assertEqual(light['intensity'], {'value': 0, 'unit': 'relative', 'provenance': 'fotometri_dimmer'})
        self.assertEqual(light['source']['ies'], 'isik/product.ies')
        self.assertNotIn('urun_rad', light['source'])
        self.assertEqual(light['original'], raw)
        self.assertTrue(any('original' in w for w in out['warnings']))
        self.assertTrue(any('fotometri' in w for w in out['warnings']))

    def test_validator_rejects_nonfinite_indices_metadata_and_wrong_contract(self):
        good = self.build([{'tip': 'spot'}])
        changes = [lambda s: s.update(version=True), lambda s: s.update(units='cm'),
                   lambda s: s['mesh']['v'].__setitem__(0, float('nan')),
                   lambda s: s['mesh']['f'].__setitem__(0, True),
                   lambda s: s['mesh']['f'].__setitem__(0, 999),
                   lambda s: s['mesh']['m'].__setitem__(0, -1),
                   lambda s: s['mesh']['uv'].pop(),
                   lambda s: s['mesh']['materials'][0].update(texture='../escape.png'),
                   lambda s: s['lights'][0].update(direction=[0, 0, 0]),
                   lambda s: s['lights'][0]['intensity'].update(unit='watt'),
                   lambda s: s['lights'][0]['original'].update(bad=float('inf')),
                   lambda s: s['lights'].append(deepcopy(s['lights'][0]))]
        for change in changes:
            bad = deepcopy(good); change(bad)
            with self.subTest(change=changes.index(change)):
                with self.assertRaises(ValueError):
                    scene.validate_scene(bad)
        self.assertIs(scene.validate_scene(good), good)

    def test_formal_schema_parses_has_only_local_refs_and_valid_path_pattern(self):
        schema = json.loads(SCHEMA_FILE.read_text())
        self.assertEqual(schema['$schema'], 'https://json-schema.org/draft/2020-12/schema')
        self.assertNotIn('$id', schema)
        self.assertIn('validate_scene', schema['$comment'])
        refs = []
        def walk(value):
            if isinstance(value, dict):
                if '$ref' in value:
                    refs.append(value['$ref'])
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)
        walk(schema)
        self.assertTrue(refs)
        for ref in refs:
            self.assertTrue(ref.startswith('#/'))
            target = schema
            for part in ref[2:].split('/'):
                target = target[part.replace('~1', '/').replace('~0', '~')]
            self.assertIsInstance(target, dict)
        path = re.compile(schema['$defs']['relativePath']['pattern'])
        for value in ['doku/a.png', 'model/my texture.webp', 'model\\texture.png', 'a..b.png']:
            self.assertIsNotNone(path.fullmatch(value), value)
        for value in ['', '/etc/file', '\\outside', 'C:\\sample.png', '../a', 'a/../b', 'a\\..\\b', 'a/..', '..']:
            self.assertIsNone(path.fullmatch(value), value)

    @unittest.skipIf(Draft202012Validator is None, 'jsonschema kurulu değil, yeni bağımlılık kurulmadı')
    def test_formal_schema_semantics_when_jsonschema_is_available(self):
        schema = json.loads(SCHEMA_FILE.read_text())
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        good = self.build([{'tip': 'spot', 'kaynak_id': 0}])
        validator.validate(good)
        for mutate in [lambda s: s.update(version=2), lambda s: s.update(units='cm'),
                       lambda s: s['mesh']['uv_ok'].__setitem__(0, 2),
                       lambda s: s['mesh']['materials'][0].update(texture='../escape'),
                       lambda s: s['lights'][0].update(direction=[0, 1]),
                       lambda s: s['lights'][0]['intensity'].update(unit='watts')]:
            bad = deepcopy(good); mutate(bad)
            self.assertTrue(list(validator.iter_errors(bad)))
        # dizilerin çapraz limitleri JSON Schema'nın değil runtime validator'ın işi.
        bad = deepcopy(good); bad['mesh']['f'][0] = 999999
        self.assertFalse(list(validator.iter_errors(bad)))
        with self.assertRaises(ValueError):
            scene.validate_scene(bad)


if __name__ == '__main__':
    unittest.main()
