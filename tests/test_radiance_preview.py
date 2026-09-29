# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Geometry properties of analytic RAD mesh generation that preserves source data."""
from collections import Counter
import math
import sys
import unittest

from core.radiance_preview import primitive_triangle_count, primitive_faces


def sub(a, b):
    return tuple(x-y for x, y in zip(a, b))


def dot(a, b):
    return sum(x*y for x, y in zip(a, b))


def cross(a, b):
    return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])


def center(face):
    return tuple(sum(p[i] for p in face)/len(face) for i in range(3))


def triangles(faces):
    for face in faces:
        for i in range(1, len(face)-1):
            yield face[0], face[i], face[i+1]


class RadPreviewTest(unittest.TestCase):
    def test_polygon_and_unknown_type_preserve_source(self):
        raw = [1, 2, 3, 4, 2, 3, 4, 6, 3, 1, 6, 3]
        source = raw[:]
        self.assertEqual(list(primitive_faces('polygon', raw)), [
            ((1, 2, 3), (4, 2, 3), (4, 6, 3), (1, 6, 3))])
        self.assertEqual(primitive_triangle_count('polygon', raw), 2)
        self.assertEqual(raw, source)
        self.assertEqual(list(primitive_faces('mesh', ['!a_command'])), [])
        self.assertEqual(primitive_triangle_count('mesh', []), 0)

    def test_sphere_is_closed_and_preserves_units_center_radius_and_outward_orientation(self):
        origin, radius = (7.5, -3.25, 2.0), 2.4
        faces = list(primitive_faces('sphere', (*origin, radius)))
        tris = list(triangles(faces))
        self.assertEqual(len(tris), primitive_triangle_count('sphere', (*origin, radius)))
        self.assertLessEqual(len(tris), 600)
        edges, volume = Counter(), 0
        for a, b, c in tris:
            n = cross(sub(b, a), sub(c, a))
            self.assertGreater(dot(n, sub(center((a, b, c)), origin)), 0)
            volume += dot(sub(a, origin), cross(sub(b, origin), sub(c, origin)))/6
            for p in (a, b, c):
                self.assertAlmostEqual(math.dist(p, origin), radius, places=12)
            for x, y in ((a, b), (b, c), (c, a)):
                edges[tuple(sorted((x, y)))] += 1
        self.assertTrue(all(n == 2 for n in edges.values()), 'Open or duplicate edge at a sphere seam')
        self.assertLess(abs(volume/(4*math.pi*radius**3/3)-1), .04)

    def test_reversed_surfaces_keep_positions_and_reverse_only_normals(self):
        for outer, inner, data in (
            ('sphere', 'bubble', [1, 2, 3, .5]),
            ('cylinder', 'tube', [1, 2, 3, 2, 4, 6, .7]),
            ('cone', 'cup', [1, 2, 3, 2, 4, 6, .7, .2]),
        ):
            with self.subTest(type=inner):
                normal = list(primitive_faces(outer, data))
                reverse = list(primitive_faces(inner, data))
                self.assertEqual(reverse, [tuple(reversed(f)) for f in normal])
                self.assertEqual(primitive_triangle_count(outer, data), primitive_triangle_count(inner, data))

    def test_tilted_cylinder_preserves_radius_axis_and_has_no_added_caps(self):
        start, end, radius = (1., 2., 3.), (3., 5., 7.), .5
        axis = sub(end, start)
        length = math.hypot(*axis)
        direction = tuple(x/length for x in axis)
        faces = list(primitive_faces('cylinder', (*start, *end, radius)))
        self.assertEqual(len(faces), 24)
        self.assertTrue(all(len(f) == 4 for f in faces))
        for face in faces:
            n = cross(sub(face[1], face[0]), sub(face[2], face[0]))
            self.assertAlmostEqual(dot(n, direction), 0, places=12)
            for p in face:
                v = sub(p, start)
                t = dot(v, direction)
                self.assertLess(min(abs(t), abs(t-length)), 1e-12)
                radial = sub(v, tuple(t*x for x in direction))
                self.assertAlmostEqual(math.hypot(*radial), radius, places=12)
                self.assertGreater(dot(n, radial), 0)

    def test_cone_zero_radius_tip_both_directions_and_frustum_yield_valid_oriented_faces(self):
        for r0, r1 in ((1, 0), (0, 1), (1, .4)):
            with self.subTest(radii=(r0, r1)):
                raw = [0, 0, 0, 0, 0, 3, r0, r1]
                faces = list(primitive_faces('cone', raw))
                tris = list(triangles(faces))
                self.assertEqual(len(tris), primitive_triangle_count('cone', raw))
                self.assertEqual(len(tris), 48 if r0 and r1 else 24)
                for a, b, c in tris:
                    n = cross(sub(b, a), sub(c, a))
                    x, y, _ = center((a, b, c))
                    analytic = (x, y, -(r1-r0)*math.hypot(x, y)/3)
                    self.assertGreater(dot(n, analytic), 0)
                    self.assertGreater(math.hypot(*n), 0)

    def test_ring_hole_and_flat_disk_preserve_normal_direction(self):
        origin, normal = (2., 3., 4.), (1., -2., 3.)
        length = math.hypot(*normal)
        for inner in (0, .4):
            with self.subTest(inner=inner):
                data = (*origin, *normal, inner, 1.2)
                faces = list(primitive_faces('ring', data))
                self.assertEqual(sum(len(f)-2 for f in faces), primitive_triangle_count('ring', data))
                area = 0
                for a, b, c in triangles(faces):
                    n = cross(sub(b, a), sub(c, a))
                    self.assertGreater(dot(n, normal), 0)
                    area += math.hypot(*n)/2
                    for p in (a, b, c):
                        self.assertAlmostEqual(dot(sub(p, origin), normal)/length, 0, places=12)
                        self.assertLess(min(abs(math.dist(p, origin)-inner), abs(math.dist(p, origin)-1.2)), 1e-12)
                self.assertLess(abs(area/(math.pi*(1.2**2-inner**2))-1), .02)

    def test_invalid_parameters_raise_in_counting_and_face_generation(self):
        cases = [
            ('polygon', [0]*10), ('sphere', [0, 0, 0, 0]), ('sphere', [0, 0, 0, -1]),
            ('sphere', [0, 0, math.nan, 1]), ('sphere', [1.7e308, 0, 0, 1.7e308]),
            ('cylinder', [0, 0, 0, 0, 0, 0, 1]), ('tube', [0, 0, 0, 0, 0, 1, -1]),
            ('cone', [0, 0, 0, 0, 0, 1, 0, 0]), ('cup', [0, 0, 0, 0, 0, 1, 1, -1]),
            ('ring', [0, 0, 0, 0, 0, 0, 0, 1]), ('ring', [0, 0, 0, 0, 0, 1, 2, 1]),
        ]
        for kind, raw in cases:
            with self.subTest(type=kind, values=raw):
                with self.assertRaises(ValueError):
                    primitive_triangle_count(kind, raw)
                with self.assertRaises(ValueError):
                    list(primitive_faces(kind, raw))


if __name__ == '__main__':
    unittest.main()
