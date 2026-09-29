# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Collect source material base colors for CTScene export without modifying files or running commands."""
from array import array
from collections import OrderedDict
import math
import os
import shlex
import struct
import threading

from core.file_safety import internal_path
from core.render_cache import file_digest

_metadata = OrderedDict()
_lock = threading.RLock()
_GRAY = [0.55, 0.55, 0.55]


def _lines(path):
    with open(path, encoding='latin-1') as f:
        for raw in f:
            s = raw.rstrip('\r\n')
            while s.endswith('\\'):
                continuation = next(f, '')
                if not continuation:
                    break
                s = s[:-1] + ' ' + continuation.rstrip('\r\n')
            yield s.strip()


class _ObjState:
    def __init__(self):
        self.group = ''
        self.material = 'white'
        self.explicit_material = False

    def update(self, p):
        if p[0] in ('o', 'g'):
            self.group = ' '.join(p[1:])
            if p[0] == 'g' and not self.explicit_material:
                self.material = p[1] if len(p) > 1 else 'white'
        elif p[0] == 'usemtl' and len(p) > 1:
            self.material = p[1]
            self.explicit_material = True
            self.group = self.group or ' '.join(p[1:])


def obj_info(path, material_digest):
    """Initial content-hash-keyed scan of face counts and mtllib without retaining a vertex table."""
    key = (os.path.abspath(path), material_digest)
    with _lock:
        if key in _metadata:
            _metadata.move_to_end(key)
            return _metadata[key]
    status = _ObjState()
    counts, libraries = {}, []
    for s in _lines(path):
        if not s or s[0] not in 'ogufm':
            continue
        p = s.split('#', 1)[0].split()
        if not p:
            continue
        status.update(p)
        if p[0] == 'f':
            counts[status.group] = counts.get(status.group, 0) + max(0, len(p)-3)
        elif p[0] == 'mtllib':
            libraries.append(s.split(None, 1)[1] if len(p) > 1 else '')
    info = {'counts': counts, 'mtllib': libraries}
    with _lock:
        _metadata[key] = info
        while len(_metadata) > 64:
            _metadata.popitem(last=False)
    return info


def obj_faces(path, with_uv=False):
    """Stream (group, material, face) from a single compact vertex table."""
    v = array('d')
    uv = array('d')
    status = _ObjState()
    for s in _lines(path):
        if not s or s[0] not in 'voguf':
            continue
        p = s.split('#', 1)[0].split()
        if not p:
            continue
        status.update(p)
        if p[0] == 'v' and len(p) >= 4:
            v.extend(float(x) for x in p[1:4])
        elif p[0] == 'vt' and len(p) >= 2:
            try:
                uv.extend((float(p[1]), float(p[2]) if len(p) > 2 else 0.0))
            except ValueError:
                uv.extend((float('nan'), float('nan')))
        elif p[0] == 'f':
            pts, coordinates = [], []
            for token in p[1:]:
                try:
                    n = int(token.split('/', 1)[0])
                    i = n-1 if n > 0 else len(v)//3+n
                    if 0 <= i < len(v)//3:
                        pts.append((v[3*i], v[3*i+1], v[3*i+2]))
                        t = token.split('/')
                        try:
                            j = int(t[1]); j = j-1 if j > 0 else len(uv)//2+j
                            point = tuple(uv[2*j:2*j+2]) if 0 <= j < len(uv)//2 else ()
                            coordinates.append(point if len(point) == 2 and all(math.isfinite(x) for x in point) else None)
                        except (IndexError, ValueError):
                            coordinates.append(None)
                except ValueError:
                    continue
            if len(pts) >= 2:
                yield (status.group, status.material, pts, coordinates) if with_uv else (status.group, status.material, pts)


def rad_records(path):
    """Numeric RAD records. The ! commands are not executed."""
    def tokens():
        for s in _lines(path):
            if s and not s.startswith(('!', '#')):
                yield from shlex.split(s, comments=True)
    it = tokens()
    try:
        while True:
            mod, record_type, name = next(it), next(it), next(it)
            if record_type == 'alias':
                yield mod, record_type, name, [next(it)]
                continue
            for _ in range(2):
                for j in range(int(next(it))):
                    next(it)
            values = [float(next(it)) for _ in range(int(next(it)))]
            yield mod, record_type, name, values
    except (StopIteration, ValueError):
        return


def rad_info(path, material_digest=None):
    key = ('rad', os.path.abspath(path), material_digest) if material_digest is not None else None
    if key:
        with _lock:
            if key in _metadata:
                _metadata.move_to_end(key)
                return _metadata[key]
    from core.radiance_preview import SUPPORTED_TYPES, primitive_triangle_count
    counts, materials, warnings = {}, {}, []
    for mod, record_type, name, d in rad_records(path):
        if record_type in SUPPORTED_TYPES:
            try:
                counts[mod] = counts.get(mod, 0) + primitive_triangle_count(record_type, d)
            except ValueError:
                warnings.append('Invalid RAD surface skipped in preview: ' + record_type)
        elif record_type in ('plastic', 'metal', 'trans', 'glass'):
            materials[name] = {'color': _color(d), 'kind': record_type}
        elif record_type == 'alias' and d[0] in materials:
            materials[name] = materials[d[0]]
    info = {'counts': counts, 'materials': materials, 'warnings': list(dict.fromkeys(warnings))}
    if key:
        with _lock:
            _metadata[key] = info
            while len(_metadata) > 64:
                _metadata.popitem(last=False)
    return info


def rad_faces(path):
    from core.radiance_preview import SUPPORTED_TYPES, primitive_faces
    for mod, record_type, name, d in rad_records(path):
        if record_type in SUPPORTED_TYPES:
            try:
                yield from ((mod, mod, list(face)) for face in primitive_faces(record_type, d))
            except ValueError:
                continue  # rad_info already reported an explicit warning for this record.


def _color(d):
    if len(d) < 3 or not all(math.isfinite(x) for x in d[:3]):
        return None
    return [min(1.0, max(0.0, x)) for x in d[:3]]


def _sidecar_path(project_dir, main, name):
    name = name.replace('\\', '/')
    # Look for the copy in the same directory instead of following an old machine's absolute path.
    if name.startswith('/') or ':' in name:
        name = name.rsplit('/', 1)[-1]
    rel = os.path.relpath(os.path.normpath(os.path.join(os.path.dirname(main), name)), project_dir)
    return internal_path(project_dir, rel)


def _read_mtl(path):
    table, name = {}, None
    for s in _lines(path):
        p = s.split('#', 1)[0].split()
        if not p:
            continue
        if p[0].lower() == 'newmtl':
            name = ' '.join(p[1:])
            table[name] = {'color': None, 'texture': None}
        elif name and p[0].lower() == 'kd':
            try:
                table[name]['color'] = _color([float(x) for x in p[1:4]])
            except ValueError:
                pass
        elif name and p[0].lower() == 'map_kd':
            table[name]['texture'] = s.split(None, 1)[1] if len(p) > 1 else ''
    return table


def _image_dimensions(path):
    """Read the JPEG/PNG header without decoding the full image or creating sidecar files."""
    with open(path, 'rb') as f:
        head = f.read(24)
        if len(head) == 24 and head[:8] == b'\x89PNG\r\n\x1a\n' and head[12:16] == b'IHDR':
            return struct.unpack('>II', head[16:24])
        if head[:2] != b'\xff\xd8':
            raise ValueError('Live textures support only JPEG/PNG')
        f.seek(2)
        while True:
            lead = f.read(1)
            if not lead:
                break
            if lead != b'\xff':
                raise ValueError('Invalid JPEG header')
            marker = f.read(1)
            while marker == b'\xff':
                marker = f.read(1)
            if not marker or marker in (b'\xda', b'\xd9'):
                break
            if marker[0] in (0x01, *range(0xd0, 0xd8)):
                continue
            raw = f.read(2)
            if len(raw) != 2:
                break
            length = struct.unpack('>H', raw)[0]
            if length < 2:
                break
            if marker[0] in (0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf):
                raw = f.read(5)
                if len(raw) == 5:
                    height, width = struct.unpack('>HH', raw[1:])
                    return width, height
                break
            f.seek(length-2, 1)
    raise ValueError('Could not read JPEG/PNG dimensions')


class Materials:
    def __init__(self, project_dir, project, inputs):
        self.table, self.warnings, self.signature = [], [], []
        self._ids, self._sources, self._resolved = {}, {}, {}
        rad = {}
        try:
            mat = internal_path(project_dir, project.get('material', 'material/materials.rad'))
            material_digest = file_digest(mat) if os.path.isfile(mat) else None
            self.signature.append((mat, material_digest))
            if material_digest:
                rad = rad_info(mat, material_digest)['materials']
                if any(s.startswith('!') for s in _lines(mat)):
                    self.warn('Dynamic Radiance material commands are not executed in the GPU preview.')
        except (OSError, ValueError):
            self.warn('Could not read the Radiance material file. Using source base colors.')
        for gi, g, gp, obj, info in inputs:
            local = {} if obj else info['materials']
            mtl = {}
            for line in info.get('mtllib', []):
                if not line:
                    continue
                try:
                    single = _sidecar_path(project_dir, gp, line)
                    names = [line] if os.path.isfile(single) else shlex.split(line, posix=False)
                    for name in names:
                        mp = _sidecar_path(project_dir, gp, name.strip('"\''))
                        material_digest = file_digest(mp) if os.path.isfile(mp) else None
                        self.signature.append((mp, material_digest))
                        if not material_digest:
                            self.warn('MTL material file not found: ' + os.path.basename(mp))
                            continue
                        new = _read_mtl(mp)
                        # The source Kd remains valid even when the texture is missing or exceeds a limit.
                        mtl.update(new)
                        for m in new.values():
                            if m.get('texture'):
                                try:
                                    if m['texture'].startswith('-'):
                                        raise ValueError('MTL texture options are not yet supported in the live view')
                                    dp = _sidecar_path(project_dir, mp, m['texture'].strip('"\''))
                                    if not os.path.isfile(dp):
                                        self.signature.append((dp, None))
                                        self.warn('Texture file not found: ' + os.path.basename(dp))
                                        continue
                                    if os.path.getsize(dp) > 32*1024*1024:
                                        raise ValueError('Live texture file exceeds the 32 MiB limit')
                                    digest = file_digest(dp)
                                    self.signature.append((dp, digest))
                                    w, h = _image_dimensions(dp)
                                    if min(w, h) < 1 or max(w, h) > 8192 or w*h > 32*1024*1024:
                                        raise ValueError('Live texture resolution exceeds the limit')
                                    studio = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                                    rel = os.path.relpath(dp, studio).replace(os.sep, '/')
                                    internal_path(studio, rel)
                                    m['texture_info'] = {'path': rel, 'signature': digest, 'dimensions': [w, h]}
                                except (OSError, ValueError) as exc:
                                    self.warn('Texture unavailable, base color preserved: ' + str(exc))
                except (OSError, ValueError):
                    self.warn('Could not read the MTL reference within the project.')
            self._sources[gi] = (rad, local, mtl)

    def warn(self, text):
        if text not in self.warnings:
            self.warnings.append(text)

    def id(self, gi, name):
        if (gi, name) in self._resolved:
            return self._resolved[(gi, name)]
        rad, local, mtl = self._sources[gi]
        source, color, record_type = 'default', None, None
        for table, root in ((rad, 'rad'), (local, 'rad'), (mtl, 'mtl')):
            if name in table:
                color, record_type = table[name].get('color'), table[name].get('kind')
                if color is not None:
                    source = root
                break
        if color is None:
            color = _GRAY
            self.warn("Color not found for material '%s'. Displaying neutral gray." % name)
        if record_type in ('glass', 'trans'):
            self.warn('Glass and transmissive materials are displayed using only their base color in the GPU preview.')
        # The user's RAD material color takes priority, alongside the OBJ UV texture.
        texture = mtl.get(name, {}).get('texture_info')
        key = (name, tuple(color), source, (texture['path'], texture['signature']) if texture else None)
        if key not in self._ids:
            self._ids[key] = len(self.table)
            record = {'name': name, 'color': list(color), 'source': source}
            if texture:
                record['texture'] = texture
            self.table.append(record)
        self._resolved[(gi, name)] = self._ids[key]
        return self._ids[key]
