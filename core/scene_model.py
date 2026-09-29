# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Read all static project geometry into CTScene without writing to sources or cache.

CTScene uses meters and Z-up. It provides neither native EVO/LSF writing nor physical power
calibration. The original light record is also stored in ``original``.
"""
from copy import deepcopy
import hashlib
from itertools import chain
import json
import math
import os
from pathlib import Path
import re
import shlex

from core.file_safety import internal_path
from core.preview_materials import (Materials, _lines, _sidecar_path, _read_mtl,
                             obj_info, obj_faces, rad_info, rad_faces)
from core.radiance_preview import SUPPORTED_TYPES, primitive_triangle_count
from core.render_cache import file_digest
from core.scene_camera import camera_normalize

MAX_TRIANGLES = 3_000_000
MAX_SOURCE_BYTES = 1024 * 1024 * 1024
MAX_FACE_VERTICES = 4096


def _limit(value, default):
    value = default if value is None else value
    if type(value) is not int or value <= 0:
        raise ValueError('Limit must be a positive integer')
    return value


def _check_triangles(count, limit):
    if count > limit:
        raise ValueError('Full geometry: %d triangles read, limit %d, '
                         'raise with --triangle-limit. No sampling was performed' % (count, limit))


def _number(value, where, minimum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(where + ': expected a number')
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite or (minimum is not None and value < minimum):
        raise ValueError(where + ': invalid/nonfinite number')
    return value


def _vector(value, size, where, minimum=None):
    if not isinstance(value, list) or len(value) != size:
        raise ValueError(where + ': invalid vector size')
    for v in value:
        _number(v, where, minimum)
    return value


def _relative(value):
    return (isinstance(value, str) and bool(value) and not value.startswith(('/', '\\'))
            and ':' not in value and '..' not in value.replace('\\', '/').split('/'))


def _json_value(value, depth=0):
    """Reject NaN, Python objects, and unbounded nesting in metadata."""
    if depth > 64:
        raise ValueError('Metadata exceeds the nesting limit')
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, (int, float)):
        _number(value, 'Metadata')
    elif isinstance(value, list):
        for v in value:
            _json_value(v, depth+1)
    elif isinstance(value, dict) and all(isinstance(k, str) for k in value):
        for v in value.values():
            _json_value(v, depth+1)
    else:
        raise ValueError('Metadata is not a JSON value')


def validate_scene(scene, *, max_triangles=None):
    """Validate the CTScene v1 contract and return the same dictionary unchanged."""
    max_triangles = _limit(max_triangles, MAX_TRIANGLES)
    if not isinstance(scene, dict) or scene.get('schema') != 'ctlux.scene' or type(scene.get('version')) is not int or scene['version'] != 1:
        raise ValueError('Unsupported CTScene schema/version')
    if scene.get('units') != 'm' or scene.get('up_axis') != 'Z' or not isinstance(scene.get('name'), str):
        raise ValueError('CTScene requires meters/Z-up and a string name')
    mesh = scene.get('mesh')
    if not isinstance(mesh, dict):
        raise ValueError('CTScene mesh is missing')
    for key in ('v', 'f', 'm', 'uv', 'uv_ok', 'materials'):
        if not isinstance(mesh.get(key), list):
            raise ValueError('Missing mesh array: ' + key)
    v, f, materials = mesh['v'], mesh['f'], mesh['materials']
    n = len(v)//3
    only_lights = not v and not f and bool(scene.get('lights'))
    if (not n or not f) and not only_lights or len(v) % 3 or len(f) % 3:
        raise ValueError('Geometry is empty or the xyz/triangle array is malformed')
    _check_triangles(len(f)//3, max_triangles)
    if len(mesh['m']) != n or len(mesh['uv']) != n*2 or len(mesh['uv_ok']) != n:
        raise ValueError('Mesh material/UV arrays do not match the vertex count')
    for x in chain(v, mesh['uv']):
        _number(x, 'Mesh coordinate')
    if not materials:
        raise ValueError('Material table is empty')
    for mat in materials:
        if not isinstance(mat, dict) or not isinstance(mat.get('name'), str):
            raise ValueError('Invalid material name')
        _vector(mat.get('color'), 3, 'Material color', 0)
        if 'texture' in mat and not _relative(mat['texture']):
            raise ValueError('Texture path must be relative within the project')
        _json_value(mat)
    for i in f:
        if type(i) is not int or not 0 <= i < n:
            raise ValueError('Triangle index is outside the vertex table')
    for i in mesh['m']:
        if type(i) is not int or not 0 <= i < len(materials):
            raise ValueError('Material index is outside the table')
    if any(type(i) is not int or i not in (0, 1) for i in mesh['uv_ok']):
        raise ValueError('uv_ok may only contain 0/1')
    if 'normals' in mesh:
        _vector(mesh['normals'], n*3, 'Mesh normal array')
    lights, warnings = scene.get('lights'), scene.get('warnings')
    if not isinstance(lights, list) or not isinstance(warnings, list) or not all(isinstance(w, str) for w in warnings):
        raise ValueError('Invalid light/warning list')
    if 'camera' in scene:
        camera_normalize(scene['camera'])
    ids = set()
    for light in lights:
        if not isinstance(light, dict) or not isinstance(light.get('id'), str) or not light['id'] or light['id'] in ids:
            raise ValueError('Light ID is empty or duplicated')
        ids.add(light['id'])
        if not isinstance(light.get('name'), str) or light.get('type') not in ('point', 'spot', 'area', 'distant') or type(light.get('enabled')) is not bool:
            raise ValueError('Invalid light name/type/enabled state')
        _vector(light.get('position'), 3, 'Light position')
        direction = _vector(light.get('direction'), 3, 'Light direction')
        if not math.isclose(math.hypot(*direction), 1, rel_tol=1e-6, abs_tol=1e-6):
            raise ValueError('Light direction must be a unit vector')
        _vector(light.get('color_linear'), 3, 'Light color', 0)
        _vector(light.get('size_m'), 2, 'Light size', 0)
        _number(light.get('radius_m'), 'Light radius', 0)
        if not 0 <= _number(light.get('cone_angle_degrees'), 'Light angle') <= 180:
            raise ValueError('Light angle must be in the 0..180 range')
        intensity = light.get('intensity')
        if not isinstance(intensity, dict) or intensity.get('unit') not in ('relative', 'cd') or not isinstance(intensity.get('provenance'), str):
            raise ValueError('Invalid light intensity unit/provenance')
        _number(intensity.get('value'), 'Light intensity', 0)
        for key in ('source', 'original'):
            if not isinstance(light.get(key), dict):
                raise ValueError('Light source metadata is missing')
            _json_value(light[key])
        for key, value in light.items():
            if key not in ('source', 'original'):
                _json_value(value)
    for key, value in mesh.items():
        if key not in ('v', 'f', 'm', 'uv', 'uv_ok', 'materials', 'normals'):
            _json_value(value)
    for key, value in scene.items():
        if key not in ('schema', 'version', 'units', 'up_axis', 'name', 'mesh', 'lights', 'warnings'):
            _json_value(value)
    return scene


class _Sources:
    def __init__(self, root, max_source_bytes=None):
        self.limit = _limit(max_source_bytes, MAX_SOURCE_BYTES)
        self.root, self.files, self.total = Path(root).resolve(), {}, 0

    def add(self, path):
        # RTM fallback and sidecar files must also remain within the resolved root.
        p = Path(path).resolve()
        try:
            rel = p.relative_to(self.root).as_posix()
            safe = internal_path(self.root, rel)
        except ValueError as exc:
            raise ValueError('Scene source is outside the project') from exc
        if not p.is_file():
            raise ValueError('Scene source not found: ' + rel)
        if safe not in self.files:
            st = p.stat()
            self.total += st.st_size
            if self.total > self.limit:
                raise ValueError('Full scene sources: %.6f MiB of registered sources, limit %.6f MiB, '
                                 'raise with --source-limit-mib. No sampling was performed'
                                 % (self.total / 1048576, self.limit / 1048576))
            self.files[safe] = (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        return safe

    def verify(self):
        for path, stamp in self.files.items():
            st = os.stat(path)
            if (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns) != stamp:
                raise ValueError('Source changed while reading the scene. Try again')


def _obj_check(path, warn):
    counts = {'v': 0, 'vt': 0, 'vn': 0}
    triangles = 0
    allowed = {'o', 'g', 's', 'mtllib', 'usemtl'}
    for line, raw in enumerate(_lines(path), 1):
        p = raw.split('#', 1)[0].split()
        if not p:
            continue
        op = p[0]
        if op in counts:
            need = 1 if op == 'vt' else 3
            if len(p) < need+1:
                raise ValueError('Missing OBJ vertex/UV/normal record at line %d' % line)
            vals = [float(x) for x in p[1:]]
            if not all(math.isfinite(x) for x in vals):
                raise ValueError('OBJ contains a nonfinite coordinate')
            if op == 'v' and len(vals) == 4 and vals[3] != 1:
                raise ValueError('Homogeneous OBJ vertex weights are not yet supported')
            if op == 'v' and len(vals) > 4:
                warn('OBJ vertex color extension was not transferred. MTL base color was preserved.')
            if op == 'vt' and len(vals) > 2:
                warn('The third OBJ UV component was not transferred. Two-dimensional UVs were preserved.')
            if op == 'vn':
                warn('Custom OBJ vertex normals were not transferred. The target will compute normals from faces.')
            counts[op] += 1
        elif op == 'f':
            if not 3 <= len(p)-1 <= MAX_FACE_VERTICES:
                raise ValueError('OBJ face vertex count is outside the supported limits')
            for token in p[1:]:
                fields = token.split('/')
                if len(fields) > 3 or not fields[0]:
                    raise ValueError('Malformed OBJ face index')
                for j, value in enumerate(fields):
                    if not value and j:
                        continue
                    i = int(value)
                    size = counts[('v', 'vt', 'vn')[j]]
                    if i == 0 or not -size <= i <= size:
                        raise ValueError('OBJ face index is outside the current table')
            triangles += len(p)-3
        elif op in ('l', 'p'):
            warn('OBJ line/point elements are not represented in the triangle mesh.')
        elif op not in allowed:
            raise ValueError('Unsupported OBJ record: ' + op)
    return triangles


def _rad_check(path, warn):
    def tokens():
        for line in _lines(path):
            if line.startswith('!'):
                raise ValueError('Dynamic !RAD commands are not executed for export')
            yield from shlex.split(line, comments=True)
    it, triangles = iter(tokens()), 0
    while True:
        mod = next(it, None)
        if mod is None:
            return triangles
        try:
            kind, name = next(it), next(it)
            if kind == 'alias':
                next(it)
                continue
            groups = []
            for group in range(3):
                n = int(next(it))
                if not 0 <= n <= MAX_FACE_VERTICES*3:
                    raise ValueError('Invalid RAD argument count')
                groups.append([next(it) for _ in range(n)])
            values = [float(v) for v in groups[2]]
            if not all(math.isfinite(v) for v in values):
                raise ValueError('RAD contains a nonfinite parameter')
            if kind in ('instance', 'mesh'):
                raise ValueError('RAD ' + kind + ' records are not yet supported for full scene export')
            if kind in SUPPORTED_TYPES:
                triangles += primitive_triangle_count(kind, values)
                if kind != 'polygon':
                    warn('Analytic RAD ' + kind + ' surface was approximated with a finite triangle subdivision. Preserve the original source.')
            elif kind not in ('plastic', 'metal', 'trans', 'glass'):
                warn('RAD ' + kind + ' record is not fully represented in the shared scene. Preserve the original source.')
        except StopIteration as exc:
            raise ValueError('RAD record is truncated') from exc


def _triangles(points, warn):
    """Preserve vertex order. Avoid incorrectly filling concave planar faces with a triangle fan."""
    n = len(points)
    if n == 3:
        yield (0, 1, 2)
        return
    if not 3 <= n <= MAX_FACE_VERTICES:
        raise ValueError('Face vertex count is out of range')
    # Move the projection to a local origin for large world coordinates.
    p = [tuple(a-b for a, b in zip(v, points[0])) for v in points]
    normal = [sum(p[i][(j+1)%3]*p[(i+1)%n][(j+2)%3] - p[i][(j+2)%3]*p[(i+1)%n][(j+1)%3] for i in range(n)) for j in range(3)]
    length = math.hypot(*normal)
    if not math.isfinite(length) or length == 0:
        raise ValueError('Face normal could not be computed reliably')
    extent = max(math.hypot(*v) for v in p)
    if any(abs(sum(v[i]*(normal[i]/length) for i in range(3))) > extent*1e-7 for v in p):
        warn('A nonplanar polygon was triangulated. The face interpretation may be approximate.')
    axis = max(range(3), key=lambda i: abs(normal[i]))
    axes = [i for i in range(3) if i != axis]
    q = [(v[axes[0]], v[axes[1]]) for v in p]
    area = sum(q[i][0]*q[(i+1)%n][1]-q[(i+1)%n][0]*q[i][1] for i in range(n))
    if not math.isfinite(area) or area == 0:
        raise ValueError('Face has no triangulatable area')
    sign = 1 if area > 0 else -1
    eps = max(abs(x) for v in q for x in v)**2 * 1e-12
    def turn(a, b, c):
        return sign*((q[b][0]-q[a][0])*(q[c][1]-q[a][1])-(q[b][1]-q[a][1])*(q[c][0]-q[a][0]))
    if all(turn(i-1, i, (i+1)%n) > eps for i in range(n)):
        yield from ((0, i, i+1) for i in range(1, n-1))
        return
    remaining = list(range(n))
    while len(remaining) > 3:
        for j, b in enumerate(remaining):
            a, c = remaining[j-1], remaining[(j+1)%len(remaining)]
            if turn(a, b, c) <= eps:
                continue
            if any(turn(a, b, k) >= -eps and turn(b, c, k) >= -eps and turn(c, a, k) >= -eps for k in remaining if k not in (a, b, c)):
                continue
            yield a, b, c
            remaining.pop(j)
            break
        else:
            raise ValueError('Concave/intersecting face could not be triangulated reliably')
    yield tuple(remaining)


def _light_name(record):
    """Apply light display name priority. Source ID 0 is also a valid suffix."""
    name = str(record.get('name') or '')
    if name and not re.fullmatch(r'luminaire_[0-9]+', name):
        return name
    if record.get('source_name'):
        return str(record['source_name'])
    if record.get('product_name'):
        source_id = record.get('source_id')
        suffix = name if source_id is None or source_id == '' else str(source_id)
        return str(record['product_name']) + (' · '+suffix if suffix else '')
    return name or 'Unnamed light'


def _light_identity(record, index):
    """Identity survives position/name changes. Distinct source namespaces remain separate."""
    namespace = record.get('source', '')
    for key in ('source_guid', 'id', 'source_id'):
        if record.get(key) is not None and record[key] != '':
            identity = (namespace, key, record[key])
            break
    else:
        identity = (namespace, 'index', index)
    raw = json.dumps(identity, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    return 'light-'+hashlib.sha256(raw.encode()).hexdigest()[:20]


def _lights(records, root, warn):
    if not isinstance(records, list):
        raise ValueError('Project lights must be a list')
    result, ids = [], {}
    kinds = {'light': 'point', 'illum': 'point', 'omni': 'point', 'point': 'point',
             'spot': 'spot', 'area': 'area', 'sun': 'distant', 'distant': 'distant'}
    for i, record in enumerate(records):
        if not isinstance(record, dict):
            raise ValueError('Light record must be an object')
        _json_value(record)
        original = deepcopy(record)
        raw_kind = record.get('type', 'light')
        if raw_kind not in kinds:
            raise ValueError('Unsupported light type: ' + str(raw_kind))
        ident = _light_identity(record, i)
        ids[ident] = ids.get(ident, 0)+1
        if ids[ident] > 1:
            warn('Duplicate source light IDs were distinguished by their order within the same ID. Stable unique source IDs are recommended.')
            ident += '-'+str(ids[ident])
        direction = list(record.get('direction') or [0, 0, -1])
        _vector(direction, 3, 'Source light direction')
        length = math.hypot(*direction)
        if not math.isfinite(length) or length == 0:
            raise ValueError('Source light direction is zero/nonfinite')
        source = {k: deepcopy(record[k]) for k in ('source', 'source_id', 'source_guid', 'source_name', 'product_name', 'product_id', 'array_id', 'color_source', 'color_note', 'photometry_status') if k in record}
        for key in ('ies', 'product_rad'):
            value = record.get(key)
            if value:
                try:
                    source[key] = Path(internal_path(root, value)).relative_to(root).as_posix()
                except (ValueError, TypeError):
                    warn('Light photometry reference is outside the project. It was preserved only in the original record.')
        # Having an IES file does not make light power a physical candela value. Dimmer is only a multiplier.
        photometric = bool(record.get('ies') or record.get('product_rad'))
        value = record.get('dimmer', 1) if photometric else record.get('power', 1)
        intensity = {'value': value, 'unit': 'relative', 'provenance': 'photometry_dimmer' if photometric else 'project.power, no physical candela calibration'}
        if photometric:
            warn('IES/product light intensity was transferred as a relative dimmer. The target reader must apply photometry separately.')
        if 'color' not in record or record['color'] is None:
            warn('Source light color is missing. The neutral white default was explicitly used.')
        result.append({'id': ident, 'name': _light_name(record),
                       'position': [record.get('x', 0), record.get('y', 0), record.get('z', 3)],
                       'direction': [v/length for v in direction], 'type': kinds[raw_kind],
                       'color_linear': deepcopy([1, 1, 1] if record.get('color') is None else record['color']),
                       'enabled': record.get('enabled') is not False,
                       'cone_angle_degrees': record.get('angle', 60), 'radius_m': record.get('radius', .15),
                       'size_m': [record.get('w', .5), record.get('h', .5)],
                       'intensity': intensity, 'source': source, 'original': original})
    return result


def scene_from_project(project_dir, project, lights=None, *, max_source_bytes=None, max_triangles=None):
    """Full OBJ/static RAD -> CTScene. Exceeding a limit raises an error. No sampling is performed."""
    if not isinstance(project, dict):
        raise ValueError('Project record must be an object')
    # Use the shared transform without generating a preview or cache.
    from core.engine import _wire_source, _transform_point
    max_triangles = _limit(max_triangles, MAX_TRIANGLES)
    sources = _Sources(project_dir, max_source_bytes)
    warnings = []
    def warn(message):
        if message not in warnings:
            warnings.append(message)
    inputs, total, textures = [], 0, {}
    geometry = project.get('geometry')
    if not isinstance(geometry, list) or not geometry and not (project.get('luminaires') if lights is None else lights):
        raise ValueError('Project has no geometry source')
    for gi, g in enumerate(geometry):
        if not isinstance(g, dict):
            raise ValueError('Geometry record must be an object')
        path = sources.add(_wire_source(internal_path(sources.root, g.get('file', ''))))
        suffix = Path(path).suffix.lower()
        if suffix not in ('.obj', '.rad'):
            raise ValueError('Full scene requires a static OBJ/RAD source: ' + suffix)
        scale = _number(g.get('scale', 1), 'Geometry scale')
        _number(g.get('rx', 0), 'Geometry X rotation')
        if scale <= 0:
            raise ValueError('Geometry scale must be positive')
        obj = suffix == '.obj'
        total += _obj_check(path, warn) if obj else _rad_check(path, warn)
        _check_triangles(total, max_triangles)
        info = obj_info(path, file_digest(path)) if obj else rad_info(path, file_digest(path))
        inputs.append((gi, g, path, obj, info))
        # Preserve MTL texture paths regardless of the size limit. Do not open the file.
        # The upper layer handles copying/packaging.
        for line in info.get('mtllib', []):
            try:
                one = _sidecar_path(sources.root, path, line)
                names = [line] if os.path.isfile(one) else shlex.split(line, posix=False)
                for name in names:
                    mp = _sidecar_path(sources.root, path, name.strip('\"\''))
                    if not os.path.isfile(mp):
                        warn('MTL file is missing. The material will use its fallback base color.')
                        continue
                    mp = sources.add(mp)
                    for name, mat in _read_mtl(mp).items():
                        if not mat.get('texture'):
                            continue
                        if mat['texture'].startswith('-'):
                            warn('MTL map_Kd options were not resolved. The texture reference was not transferred.')
                            continue
                        dp = _sidecar_path(sources.root, mp, mat['texture'].strip('\"\''))
                        if os.path.isfile(dp):
                            sources.add(dp)
                            textures[(gi, name)] = Path(dp).relative_to(sources.root).as_posix()
                        else:
                            warn('MTL texture is missing. Source base color was preserved.')
            except ValueError as exc:
                raise ValueError('MTL/texture reference is invalid or exceeds the source quota: ' + str(exc)) from exc
    material_path = internal_path(sources.root, project.get('material', 'material/materials.rad'))
    if os.path.isfile(material_path):
        sources.add(material_path)
        _rad_check(material_path, warn)
    mats = Materials(str(sources.root), project, inputs)
    mesh = {key: [] for key in ('v', 'f', 'm', 'uv', 'uv_ok', 'materials')}
    vertices, material_ids = {}, {}
    skipped_repeated = 0
    for gi, g, path, obj, info in inputs:
        faces = obj_faces(path, with_uv=True) if obj else ((a, b, p, [None]*len(p)) for a, b, p in rad_faces(path))
        for group, name, points, uvs in faces:
            mi0 = mats.id(gi, name)
            texture = textures.get((gi, name))
            material_key = (mi0, texture)
            if material_key not in material_ids:
                m = mats.table[mi0]
                mi = material_ids[material_key] = len(mesh['materials'])
                material = {'name': m['name'], 'color': list(m['color'])}
                if texture:
                    material['texture'] = texture
                mesh['materials'].append(material)
            mi = material_ids[material_key]
            for triangle in _triangles(points, warn):
                if len({tuple(points[k]) for k in triangle}) != 3:
                    skipped_repeated += 1
                    continue
                for k in triangle:
                    point, uv = tuple(points[k]), uvs[k]
                    key = (gi, group, mi, point, uv)
                    if key not in vertices:
                        vertices[key] = len(mesh['v'])//3
                        mesh['v'].extend(_transform_point(point, g.get('scale', 1), g.get('rx', 0)))
                        mesh['m'].append(mi)
                        mesh['uv'].extend(uv if uv is not None else (0, 0))
                        mesh['uv_ok'].append(int(uv is not None))
                    mesh['f'].append(vertices[key])
                _check_triangles(len(mesh['f'])//3, max_triangles)
            if texture and any(uv is None for uv in uvs):
                warn('Some UV coordinates are missing from a textured face. The uv_ok mask was preserved.')
    if skipped_repeated:
        warn('%d zero-area triangles with repeated vertices were skipped. The source file was not changed.' % skipped_repeated)
    for w in mats.warnings:
        # The helper texture size limit does not apply here. Base color warnings remain.
        if not w.startswith('Texture unavailable,'):
            warn(w.replace('in the GPU preview', 'in the shared scene').replace('displayed', 'represented'))
    if not mesh['materials']:
        mesh['materials'].append({'name': 'Default', 'color': [.55, .55, .55]})
    scene = {'schema': 'ctlux.scene', 'version': 1, 'units': 'm', 'up_axis': 'Z',
             'name': str(project.get('name') or sources.root.name), 'mesh': mesh,
             'lights': _lights(project.get('luminaires') or [] if lights is None else lights, sources.root, warn),
             'warnings': warnings}
    if project.get('camera') is not None:
        scene['camera'] = camera_normalize(project['camera'])
    sources.verify()
    return validate_scene(scene, max_triangles=max_triangles)
