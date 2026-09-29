# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Generate a bounded number of mesh faces in source units from numeric Radiance surfaces.

Analytic surfaces are converted to polygons only for the mesh view. Radiance source and
rendering calculations are unchanged. Cylinders/cones have side surfaces only. No caps are added.
Command execution, path resolution and material resolution are outside this module's scope.
"""
import math


CIRCUMFERENCE_SEGMENTS = 24
SPHERE_LAYERS = 12
SUPPORTED_TYPES = frozenset((
    'polygon', 'sphere', 'bubble', 'cylinder', 'tube', 'cone', 'cup', 'ring'))


def _cross(a, b):
    return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])


def _normal(v):
    length = math.hypot(*v)
    if not math.isfinite(length) or length == 0:
        raise ValueError('RAD surface axis/normal vector is zero or nonfinite')
    return tuple(x/length for x in v)


def _basis(axis):
    w = _normal(axis)
    # Choose the least parallel coordinate axis to avoid a very small cross product.
    least_parallel = min(range(3), key=lambda j: abs(w[j]))
    ref = tuple(float(i == least_parallel) for i in range(3))
    u = _normal(_cross(w, ref))
    return u, _cross(w, u)


def _values(record_type, values):
    if record_type not in SUPPORTED_TYPES:
        return None
    try:
        d = tuple(float(x) for x in values)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError('RAD surface parameters are not numeric') from error
    if not all(math.isfinite(x) for x in d):
        raise ValueError('RAD surface parameters are not finite')
    n = len(d)
    expected = {'sphere': 4, 'bubble': 4, 'cylinder': 7, 'tube': 7,
                'cone': 8, 'cup': 8, 'ring': 8}
    if (record_type == 'polygon' and (n < 9 or n % 3)) or (record_type != 'polygon' and n != expected[record_type]):
        raise ValueError('Invalid RAD %s parameter count' % record_type)
    if record_type in ('sphere', 'bubble'):
        if d[3] <= 0:
            raise ValueError('RAD sphere radius must be positive')
    elif record_type in ('cylinder', 'tube', 'cone', 'cup'):
        _normal(tuple(d[i+3]-d[i] for i in range(3)))
        if record_type in ('cylinder', 'tube'):
            if d[6] <= 0:
                raise ValueError('RAD cylinder radius must be positive')
        elif min(d[6:8]) < 0 or max(d[6:8]) <= 0:
            raise ValueError('RAD cone radii must be nonnegative and cannot both be zero')
    elif record_type == 'ring':
        _normal(d[3:6])
        if not 0 <= d[6] < d[7]:
            raise ValueError('RAD ring radii must satisfy 0 <= inner < outer')
    if record_type != 'polygon':
        radius = d[3] if record_type in ('sphere', 'bubble') else max(d[6:])
        centers = (d[:3], d[3:6]) if record_type in ('cylinder', 'tube', 'cone', 'cup') else (d[:3],)
        if not all(math.isfinite(x-radius) and math.isfinite(x+radius) for c in centers for x in c):
            raise ValueError('RAD surface bounds exceeded the finite range')
    return d


def primitive_triangle_count(record_type, values):
    """Match the face stream's fan triangulation count and return zero for unsupported types."""
    d = _values(record_type, values)
    if d is None:
        return 0
    if record_type == 'polygon':
        return len(d)//3-2
    if record_type in ('sphere', 'bubble'):
        return 2*CIRCUMFERENCE_SEGMENTS*(SPHERE_LAYERS-1)
    if record_type in ('cone', 'cup') and (d[6] == 0 or d[7] == 0):
        return CIRCUMFERENCE_SEGMENTS
    if record_type == 'ring' and d[6] == 0:
        return CIRCUMFERENCE_SEGMENTS
    return 2*CIRCUMFERENCE_SEGMENTS


def primitive_faces(record_type, values):
    """Three/four-vertex faces with outward normals. Bubble/tube/cup face inward.

    Polygon vertices retain their source order. Each curved surface yields at most 528
    triangles. Parameters do not increase the sample count or memory allocation.
    Invalid records of a supported type raise ValueError. Unknown types yield no faces.
    """
    d = _values(record_type, values)
    if d is None:
        return
    if record_type == 'polygon':
        yield tuple(d[i:i+3] for i in range(0, len(d), 3))
        return
    reversed_orientation = record_type in ('bubble', 'tube', 'cup')
    circle = tuple((math.cos(2*math.pi*i/CIRCUMFERENCE_SEGMENTS),
                   math.sin(2*math.pi*i/CIRCUMFERENCE_SEGMENTS)) for i in range(CIRCUMFERENCE_SEGMENTS))

    def face(points):
        if not all(math.isfinite(x) for p in points for x in p):
            raise ValueError('RAD surface coordinate exceeded the finite range')
        return tuple(reversed(points)) if reversed_orientation else tuple(points)

    if record_type in ('sphere', 'bubble'):
        c, radius = d[:3], d[3]
        lower = (c[0], c[1], c[2]-radius)
        upper = (c[0], c[1], c[2]+radius)
        before = None
        for j in range(1, SPHERE_LAYERS):
            latitude = -math.pi/2 + math.pi*j/SPHERE_LAYERS
            horizontal, z = radius*math.cos(latitude), c[2]+radius*math.sin(latitude)
            ring = tuple((c[0]+horizontal*x, c[1]+horizontal*y, z) for x, y in circle)
            for i in range(CIRCUMFERENCE_SEGMENTS):
                k = (i+1) % CIRCUMFERENCE_SEGMENTS
                if before is None:
                    yield face((lower, ring[k], ring[i]))
                else:
                    yield face((before[i], before[k], ring[k], ring[i]))
            before = ring
        for i in range(CIRCUMFERENCE_SEGMENTS):
            yield face((before[i], before[(i+1) % CIRCUMFERENCE_SEGMENTS], upper))
        return

    c = d[:3]
    axis = d[3:6] if record_type == 'ring' else tuple(d[i+3]-d[i] for i in range(3))
    u, v = _basis(axis)

    def ring(center, radius):
        return tuple(tuple(center[a]+radius*(u[a]*x+v[a]*y) for a in range(3)) for x, y in circle)

    if record_type == 'ring':
        inner, outside = ring(c, d[6]), ring(c, d[7])
        for i in range(CIRCUMFERENCE_SEGMENTS):
            k = (i+1) % CIRCUMFERENCE_SEGMENTS
            yield face((c, outside[i], outside[k]) if d[6] == 0 else (inner[i], outside[i], outside[k], inner[k]))
        return

    upper_center = d[3:6]
    lower_radius, upper_radius = d[6], d[6] if record_type in ('cylinder', 'tube') else d[7]
    lower, upper = ring(c, lower_radius), ring(upper_center, upper_radius)
    for i in range(CIRCUMFERENCE_SEGMENTS):
        k = (i+1) % CIRCUMFERENCE_SEGMENTS
        if lower_radius == 0:
            yield face((c, upper[k], upper[i]))
        elif upper_radius == 0:
            yield face((lower[i], lower[k], upper_center))
        else:
            yield face((lower[i], lower[k], upper[k], upper[i]))
