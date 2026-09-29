# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Verified loss cases in Lumion import, using only synthetic binary/OBJ."""
from pathlib import Path
import json
import os
import struct
import sys
import tempfile
import unittest
import warnings

ROOT = Path(__file__).resolve().parents[1]
from core import importer as importer
from core.tools import ls10_reader as L


def tlv(tag, vals, fmt='f'):
    data = struct.pack('<'+str(len(vals))+fmt, *vals)
    return tag + struct.pack('<I', len(data)) + data


def matrix(y=0):
    return [1,0,0,0, 0,1,0,0, 0,0,1,0, 0,y,0,1]


def light(kind, x=0, fields=None):
    m = matrix(); m[12] = x
    out = 'ClassType->cLightObject'.encode('utf-16le')
    for n in range(1, 38):
        if fields and n in fields:
            out += tlv(b'IIV1' if n == 28 else b'IIVE', fields[n])
        elif n == 3:
            out += tlv(b'IIM1', m)
        elif n == 28:
            out += tlv(b'IIV1', [100,80,60,0])
        else:
            out += tlv(b'IIVE', [kind if n == 37 else 1])
    return out


def model_record(y=20):
    return ('ClassType->cCustomObject'.encode('utf-16le')
            + 'world'.encode('utf-16le') + tlv(b'IIM1', matrix(y))
            + 'ClassInstance->cImportObject'.encode('utf-16le'))


def mesh():
    # A triangle that fully collapses in a 20 cm grid cell, plus an exactly repeated vertex.
    return (tlv(b'VPPI', [0,0,0, .001,0,0, 0,.001,0, 0,0,0])
            + tlv(b'PO32', [3,1,2], 'I'))


class LumionLossTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ctlux-ls10-preservation-')
        self.p = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_nearby_lights_are_not_deleted_by_default(self):
        raw = light(3) + light(0, .1)
        self.assertEqual(len(L.decode_lights(raw)), 2)
        self.assertEqual(len(L.light_luminaires(raw)), 2)
        self.assertEqual(len(L.decode_lights(raw, merge=True)), 1)

    def test_source_identity_is_independent_of_filter_order(self):
        raw = light(0, .1) + light(3)
        self.assertEqual(L.decode_lights(raw)[1]['source_id'],
                         L.decode_lights(raw, merge=True)[0]['source_id'])
        self.assertEqual(L.light_luminaires(raw)[1]['photometry_status'], 'approximate')

    def test_unverified_light_color_is_neutral_and_explicitly_defaulted(self):
        raw = light(0, .1)+light(1)+light(3)
        before = bytes(raw)
        decoded = L.decode_lights(raw)
        luminaires = L.light_luminaires(raw)
        self.assertEqual(raw, before)
        for luminaire, source in zip(luminaires, decoded):
            self.assertEqual(luminaire['color'], [1, 1, 1])
            self.assertEqual(luminaire['color_source'], 'default_neutral')
            self.assertIn('could not be verified', luminaire['color_note'])
            self.assertEqual(luminaire['source_id'], source['source_id'])
            self.assertEqual(luminaire['power'], max(1, int(round(source['intensity']/200*1000))))
            self.assertEqual(luminaire['direction'], None if source['type'] == 1 else [0, -1, 0])

    def test_single_model_transform_and_zup_order(self):
        data = model_record() + mesh()
        m = L.single_model_matrix(data)
        self.assertEqual(m[13], 20)
        out = self.p/'model.obj'
        r = L.extract_mesh(data, out, zup=True, matrix=m, unique=True)
        self.assertEqual((r['vertex'], r['triangle']), (3, 1))
        coords = [tuple(map(float, s.split()[1:])) for s in out.read_text().splitlines() if s.startswith('v ')]
        self.assertAlmostEqual(coords[2][2], 20.001, places=6)
        self.assertIn('f 1 2 3', out.read_text())

    def test_fallback_mesh_winding_survives_mirrors_and_zup(self):
        # A closed tetrahedron with outward faces and one exactly duplicated vertex.
        raw = (tlv(b'VPPI', [0,0,0, 2,0,0, 0,3,0, 0,0,4, 0,0,0])
               + tlv(b'PO32', [4,2,1, 0,1,3, 0,3,2, 1,2,3], 'I'))
        for sign in (1, -1):
            # Rotation, nonuniform scale, shear and translation; determinant is 24 * sign.
            world = [0,2*sign,0,0, -3,0,0,0, 1,0,4,0, 7,-11,13,1]
            data = model_record().replace(tlv(b'IIM1', matrix(20)), tlv(b'IIM1', world)) + raw
            resolved = L.single_model_matrix(data)
            self.assertIsNotNone(resolved)
            for zup in (False, True):
                for unique in (False, True):
                    with self.subTest(sign=sign, zup=zup, unique=unique):
                        out = self.p / 'winding.obj'
                        result = L.extract_mesh(data, out, zup=zup, matrix=resolved, unique=unique)
                        self.assertEqual((result['mesh'], result['vertex'], result['triangle']),
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

    def test_matrix_is_not_guessed_for_multiple_models(self):
        self.assertIsNone(L.single_model_matrix(model_record()+model_record(40)))
        self.assertIsNone(L.single_model_matrix(mesh()))

    def test_zero_scale_matrix_is_rejected(self):
        data = model_record().replace(tlv(b'IIM1', matrix(20)), tlv(b'IIM1', [0]*15+[1]))
        self.assertIsNone(L.single_model_matrix(data))

    def test_world_matrix_determinant_threshold(self):
        # These exactly representable float32 scales bracket the 1e-20 determinant threshold.
        for sign in (1, -1):
            for scale, accepted in ((2.0**-67, False), (2.0**-66, True)):
                with self.subTest(sign=sign, scale=scale):
                    world = matrix(20)
                    world[0] = sign * scale
                    data = model_record().replace(tlv(b'IIM1', matrix(20)), tlv(b'IIM1', world))
                    resolved = L.single_model_matrix(data)
                    if accepted:
                        self.assertIsNotNone(resolved)
                        self.assertEqual(resolved[0], sign * scale)
                    else:
                        self.assertIsNone(resolved)

    def test_default_import_preserves_thin_face_and_groups(self):
        src = self.p/'input.ls10'; out = self.p/'out.obj'
        src.write_bytes(model_record()+mesh()+mesh())
        stale = Path(str(out)+'.full.obj'); stale.write_text('old geometry')
        importer._lumion_obj(str(src), str(out))
        self.assertEqual(stale.read_bytes(),out.read_bytes())
        lines = out.read_text().splitlines()
        self.assertEqual(sum(x.startswith('f ') for x in lines), 2)
        self.assertEqual(sum(x.startswith('o ') for x in lines), 2)
        info = json.loads(Path(str(out)+'.import.json').read_text())
        self.assertEqual(info['triangle'], 2)

    def test_nonfinite_light_fields_skip_only_that_light(self):
        # Field numbers: 28 rgb, 37 type, 26 cone, 35 w, 36 h.
        cases = {28: ('rgb', [float('nan'), 0, 0, 0]), 37: ('type', [float('nan')]),
                 26: ('cone', [float('inf')]), 35: ('w', [float('-inf')]), 36: ('h', [float('nan')])}
        for n, (name, value) in cases.items():
            with self.subTest(field=name):
                raw = light(1) + light(3, 5, {n: value})
                with warnings.catch_warnings(record=True) as caught:
                    warnings.simplefilter('always')
                    decoded = L.decode_lights(raw)
                    luminaires = L.light_luminaires(raw)
                self.assertEqual([d['type'] for d in decoded], [1])
                self.assertEqual(len(luminaires), 1)
                self.assertTrue(any('skipped: nonfinite' in str(w.message) and name in str(w.message)
                                    for w in caught))

    def test_nonfinite_power_skips_light(self):
        raw = light(1)
        self.assertEqual(len(L.light_luminaires(raw)), 1)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            self.assertEqual(L.light_luminaires(raw, k=1e-320), [])
        self.assertTrue(any('nonfinite power' in str(w.message) for w in caught))

    def test_empty_file_is_clear_error_and_closes_file(self):
        src = self.p/'empty.ls10'; src.write_bytes(b'')
        before = len(os.listdir('/proc/self/fd')) if os.path.isdir('/proc/self/fd') else None
        for call in (lambda: importer._lumion_obj(str(src), str(self.p/'out.obj')),
                     lambda: importer.lumion_luminaires(str(src))):
            with self.assertRaisesRegex(RuntimeError, 'Lumion file is empty'):
                call()
        if before is not None:
            self.assertEqual(len(os.listdir('/proc/self/fd')), before)
        self.assertFalse((self.p/'out.obj').exists())


if __name__ == '__main__':
    unittest.main()
