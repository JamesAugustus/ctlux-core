# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""CTScene v1 -> GLB 2.0 / OBJ+MTL / USDA, sadece Python standart kütüphanesi.

Kaynak dosya path'i açılmaz. Texture byte'larını çağıranın asset_resolver fonksiyonu verir.
Çıktı yeni iş dizinine yazılır, var olan dosyanın üzerine yazılmaz. Kaynak sahne
değişmez, geometri sample'lanmaz. CTLux yan dosyasını üst katman yazar.

Format sözleşmeleri:
https://registry.khronos.org/glTF/specs/2.0/glTF-2.0.html
https://github.com/KhronosGroup/glTF/tree/main/extensions/2.0/Khronos/KHR_lights_punctual
https://openusd.org/release/spec_usdpreviewsurface.html
https://openusd.org/release/api/class_usd_lux_shaping_a_p_i.html
"""
from array import array
from collections import OrderedDict
import hashlib
import json
import math
from pathlib import Path
import struct
import sys


from core.scene_camera import camera_normalize, camera_basis

WRITER_VERSION = 2
_FLOAT_MAX = 3.4028234663852886e38


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def _number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(label + ' sonlu bir sayı olmalı')
    return float(value)


def _vector(value, n, label, color=False):
    if not isinstance(value, (list, tuple)) or len(value) != n:
        raise ValueError(label + ' boyutu geçersiz')
    result = [_number(v, label) for v in value]
    if color and any(v < 0 or v > 1 for v in result):
        raise ValueError(label + ' lineer 0..1 aralığında olmalı')
    return result


def _unit(v):
    length = math.hypot(*v)
    if not math.isfinite(length) or length == 0:
        raise ValueError('Yön veya normal sıfır olamaz')
    return [x / length for x in v]


def _yup(v):
    return [v[0], v[2], -v[1]]


def _orientation(direction):
    """Local -Z'yi yön vektörüne çeviren birim quaternion: (x,y,z,w)."""
    x, y, z = _unit(direction)
    if z > 1 - 1e-12:  # ters paralel eksende cross product sıfır çıkar.
        return [1., 0., 0., 0.]
    return _unit([y, -x, 0., 1 - z])


def _validate(scene):
    if not isinstance(scene, dict) or scene.get('schema') != 'ctlux.scene' or scene.get('version') != 1:
        raise ValueError('CTScene v1 gerekli')
    if scene.get('units') != 'm' or scene.get('up_axis') != 'Z':
        raise ValueError('CTScene metre ve Z-up olmalı')
    if 'camera' in scene:
        camera_normalize(scene['camera'])
    _json(scene)  # metadata içinde de NaN, bytes ya da döngü yayımlanmasın.
    mesh = scene.get('mesh') or {}
    v, f = mesh.get('v', []), mesh.get('f', [])
    if not isinstance(v, (list, tuple)) or not isinstance(f, (list, tuple)) or len(v) % 3 or len(f) % 3:
        raise ValueError('Vertex veya üçgen dizisi üçlü olmalı')
    for x in v:
        if abs(_number(x, 'Vertex')) > _FLOAT_MAX:
            raise ValueError('Vertex float32 aralığını aşıyor')
    count = len(v) // 3
    for i in f:
        if isinstance(i, bool) or not isinstance(i, int) or not 0 <= i < count:
            raise ValueError('Üçgen vertex indeksi geçersiz')
    if any(len(set(f[i:i+3])) != 3 for i in range(0, len(f), 3)):
        raise ValueError('Üçgen tekrarlı vertex indeksi içeriyor')
    materials = mesh.get('materials') or [{'name': 'Varsayılan', 'color': [.55, .55, .55]}]
    for material in materials:
        _vector(material.get('color', [.55]*3), 3, 'Malzeme rengi', True)
        if material.get('texture') is not None and not isinstance(material['texture'], str):
            raise ValueError('Doku başvurusu metin olmalı')
    m = mesh.get('m') or [0]*count
    if len(m) != count or any(isinstance(i, bool) or not isinstance(i, int) or not 0 <= i < len(materials) for i in m):
        raise ValueError('Vertex malzeme indeksi geçersiz')
    for i in range(0, len(f), 3):
        if len({m[j] for j in f[i:i+3]}) != 1:
            raise ValueError('Bir üçgenin vertex malzeme indeksleri aynı olmalı')
    uv, ok = mesh.get('uv') or [], mesh.get('uv_ok') or []
    if uv and len(uv) != count*2 or ok and len(ok) != count:
        raise ValueError('UV veya UV geçerlik dizisi boyutu geçersiz')
    if ok and not uv:
        raise ValueError('UV geçerliği var ama UV dizisi yok')
    if any(x not in (0, 1, False, True) for x in ok):
        raise ValueError('UV geçerliği yalnız 0/1 olabilir')
    for x in uv:
        if abs(_number(x, 'UV')) > _FLOAT_MAX / 2:
            raise ValueError('UV float32 aralığını aşıyor')
    normals = mesh.get('normals') or []
    if normals and len(normals) != count*3:
        raise ValueError('Normal dizisi boyutu geçersiz')
    for i in range(0, len(normals), 3):
        _unit(_vector(normals[i:i+3], 3, 'Normal'))
    for light in scene.get('lights', []):
        if light.get('type') not in ('point', 'spot', 'area', 'distant', 'omni', 'alan', 'light', 'illum'):
            raise ValueError('Işık türü desteklenmiyor')
        _vector(light.get('position'), 3, 'Işık konumu')
        _vector(light.get('color_linear', [1, 1, 1]), 3, 'Işık rengi', True)
        _unit(_vector(light.get('direction') or [0, 0, -1], 3, 'Işık yönü'))
        intensity = light.get('intensity') or {}
        if _number(intensity.get('value', 1), 'Işık şiddeti') < 0 or intensity.get('unit', 'relative') not in ('relative', 'cd', 'lux'):
            raise ValueError('Işık şiddeti veya birimi geçersiz')
    return v, f, m, uv, ok or ([1]*count if uv else []), materials, normals


def _warn(warnings, message):
    if message not in warnings:
        warnings.append(message)


def _assets(materials, resolver, warnings):
    assets, seen = {}, {}
    for i, material in enumerate(materials):
        ref = material.get('texture')
        if not ref:
            continue
        if ref not in seen:
            try:
                if resolver is None:
                    raise ValueError('doku çözücü verilmedi')
                result = resolver(ref)
                if not isinstance(result, dict):
                    raise ValueError('doku baytları bulunamadı')
                data, mime = result.get('data'), result.get('mime_type')
                if not isinstance(data, (bytes, bytearray)):
                    raise ValueError('doku verisi bayt olmalı')
                valid = (mime == 'image/png' and data.startswith(b'\x89PNG\r\n\x1a\n') or
                         mime == 'image/jpeg' and data.startswith(b'\xff\xd8\xff'))
                if not valid:
                    raise ValueError('yalnız MIME ile eşleşen PNG/JPEG destekleniyor')
                data = bytes(data)
                name = 'assets/' + hashlib.sha256(data).hexdigest() + ('.png' if mime == 'image/png' else '.jpg')
                seen[ref] = {'data': data, 'mime': mime, 'name': name}
            except (OSError, ValueError, TypeError) as error:
                seen[ref] = None
                _warn(warnings, 'Doku aktarılamadı, temel renk korundu: ' + str(error))
        if seen[ref]:
            assets[i] = seen[ref]
    return assets


def _groups(f, m, uv, ok, assets, warnings):
    groups = OrderedDict()
    for n in range(0, len(f), 3):
        tri = f[n:n+3]
        mi = m[tri[0]]
        complete_uv = bool(uv) and all(ok[i] for i in tri)
        if mi in assets and not complete_uv:
            _warn(warnings, 'UV bilgisi eksik yüzlerde doku kullanılmadı, temel renk korundu.')
        # malzemede resim olmasa da geçerli kaynak UV'si kaybolmasın.
        groups.setdefault((mi, complete_uv), []).append(n//3)
    return groups


def _light(light, warnings, fmt):
    original = light.get('original') or {}
    kind = {'omni': 'point', 'light': 'point', 'illum': 'point', 'alan': 'area'}.get(light['type'], light['type'])
    unit = light.get('intensity', {}).get('unit', 'relative')
    value = float(light.get('intensity', {}).get('value', 1))
    if unit == 'relative':
        _warn(warnings, 'Göreli ışık gücü hedef biçimin şiddetine sayısal olarak eşlendi, candela/lüks karşılığı yaklaşık ve kalibre edilmemiştir.')
    if fmt == 'glb' and kind == 'distant' and unit != 'lux':
        _warn(warnings, 'GLB yönlü ışık lüks bekler, kaynak birimin sayısal eşlemesi yaklaşıktır.')
    if fmt == 'glb' and kind != 'distant' and unit == 'lux':
        _warn(warnings, 'GLB nokta/spot ışık candela bekler, kaynak lüks değerinin sayısal eşlemesi yaklaşıktır.')
    if fmt == 'usda':
        _warn(warnings, 'USD Lux intensity sayısal olarak eşlendi, kaynak candela veya IES dağılımıyla fotometrik eşdeğerlik garanti edilmez.')
    if original.get('ies') or original.get('urun_rad') or light.get('source', {}).get('ies'):
        _warn(warnings, 'IES/ürün fotometrisinin özgün başvurusu metadata içinde korundu, hedef ışık dağılımına dönüştürülmedi.')
    enabled = light.get('enabled', original.get('etkin', True)) is not False
    angle = _number(light.get('cone_angle_degrees', original.get('aci', 60)), 'Koni açısı')
    if not 0 < angle <= 180:
        raise ValueError('Tam koni açısı 0..180 derece aralığında olmalı')
    radius = _number(light.get('radius_m', original.get('yaricap', .15)), 'Işık yarıçapı')
    size = _vector(light.get('size_m', [original.get('w', .3), original.get('h', .3)]), 2, 'Alan ışığı boyutu')
    if radius < 0 or min(size) <= 0:
        raise ValueError('Işık boyutu geçersiz')
    return {'type': kind, 'position': light['position'], 'direction': light.get('direction') or [0, 0, -1],
            'color': light.get('color_linear', [1, 1, 1]), 'value': value if enabled else 0.,
            'angle': angle, 'radius': radius, 'size': size}


def _metadata(scene):
    return {'schema': scene['schema'], 'version': scene['version'], 'writer_version': WRITER_VERSION,
            'units': scene['units'], 'up_axis': scene['up_axis'], 'name': scene.get('name', '')}


def _packed(code, values):
    out = array(code, values)
    if sys.byteorder != 'little':
        out.byteswap()
    return out.tobytes()


def _glb(scene, data, groups, assets, warnings):
    v, f, m, uv, ok, materials, normals = data
    doc = {'asset': {'version': '2.0', 'generator': 'CTLux CTScene writer v1'}, 'scene': 0,
           'scenes': [{'name': scene.get('name', 'Sahne'), 'nodes': []}], 'nodes': [],
           'extras': {'ctlux': _metadata(scene)}}
    binary = bytearray()

    def view(raw, target=None):
        binary.extend(b'\0' * (-len(binary) % 4))
        item = {'buffer': 0, 'byteOffset': len(binary), 'byteLength': len(raw)}
        if target:
            item['target'] = target
        binary.extend(raw)
        doc.setdefault('bufferViews', []).append(item)
        return len(doc['bufferViews']) - 1

    def accessor(values, code, component, kind, components, bounds=False, target=34962):
        raw = _packed(code, values)
        item = {'bufferView': view(raw, target), 'componentType': component,
                'count': len(values)//components, 'type': kind}
        if bounds:
            values32 = array(code); values32.frombytes(raw)
            if sys.byteorder != 'little':
                values32.byteswap()
            item['min'] = [min(values32[i::components]) for i in range(components)]
            item['max'] = [max(values32[i::components]) for i in range(components)]
        doc.setdefault('accessors', []).append(item)
        return len(doc['accessors']) - 1

    textures = {}
    if groups:
        pos = [x for i in range(0, len(v), 3) for x in _yup(v[i:i+3])]
        attributes = {'POSITION': accessor(pos, 'f', 5126, 'VEC3', 3, True)}
        if normals:
            ns = [x for i in range(0, len(normals), 3) for x in _yup(_unit(normals[i:i+3]))]
            attributes['NORMAL'] = accessor(ns, 'f', 5126, 'VEC3', 3)
        texcoord = None
        if any(has_uv for _, has_uv in groups):
            # CTScene/OBJ/USD'de UV başlangıcı sol alt, glTF'de sol üst. Görüntü byte'ları değişmez.
            texcoord = accessor([x for i in range(0, len(uv), 2) for x in (uv[i], 1-uv[i+1])], 'f', 5126, 'VEC2', 2)
        primitives = []
        for (mi, has_uv), faces in groups.items():
            textured = has_uv and mi in assets
            material = materials[mi]
            pbr = {'baseColorFactor': list(material.get('color', [.55]*3)) + [1], 'metallicFactor': 0, 'roughnessFactor': .8}
            if textured:
                asset = assets[mi]
                if asset['name'] not in textures:
                    image_id = len(doc.setdefault('images', []))
                    doc['images'].append({'bufferView': view(asset['data']), 'mimeType': asset['mime']})
                    doc.setdefault('textures', []).append({'source': image_id, 'sampler': 0})
                    textures[asset['name']] = len(doc['textures']) - 1
                    doc['samplers'] = [{'magFilter': 9729, 'minFilter': 9987, 'wrapS': 10497, 'wrapT': 10497}]
                pbr['baseColorTexture'] = {'index': textures[asset['name']], 'texCoord': 0}
            mat_id = len(doc.setdefault('materials', []))
            doc['materials'].append({'name': material.get('name', str(mi)), 'pbrMetallicRoughness': pbr})
            ids = [f[n*3+j] for n in faces for j in range(3)]
            attrs = dict(attributes)
            if has_uv:
                attrs['TEXCOORD_0'] = texcoord
            primitives.append({'attributes': attrs, 'indices': accessor(ids, 'I', 5125, 'SCALAR', 1, target=34963),
                               'material': mat_id, 'mode': 4})
        doc['meshes'] = [{'name': scene.get('name', 'Sahne'), 'primitives': primitives}]
        doc['nodes'].append({'mesh': 0, 'name': scene.get('name', 'Sahne')})
        doc['scenes'][0]['nodes'].append(0)
    lights = []
    for item in scene.get('lights', []):
        light = _light(item, warnings, 'glb')
        kind = light['type']
        if kind == 'area':
            kind = 'point'
            _warn(warnings, 'GLB KHR_lights_punctual alan ışığı desteklemez, alan ışığı noktasal yaklaşık ışık olarak aktarıldı, boyutları metadata içinde korundu.')
        entry = {'name': item.get('name', item.get('id', 'Işık')), 'type': 'directional' if kind == 'distant' else kind,
                 'color': light['color'], 'intensity': light['value'], 'extras': {'ctlux': item}}
        if kind == 'spot':
            entry['spot'] = {'innerConeAngle': 0, 'outerConeAngle': math.radians(light['angle']/2)}
        doc['scenes'][0]['nodes'].append(len(doc['nodes']))
        doc['nodes'].append({'name': entry['name'], 'translation': _yup(light['position']),
                             'rotation': _orientation(_yup(light['direction'])),
                             'extensions': {'KHR_lights_punctual': {'light': len(lights)}}})
        lights.append(entry)
    if lights:
        doc['extensionsUsed'] = ['KHR_lights_punctual']
        doc['extensions'] = {'KHR_lights_punctual': {'lights': lights}}
    if 'camera' in scene:
        camera = camera_normalize(scene['camera'])
        basis = camera_basis(camera)
        matrix = [x for i, v in enumerate(basis) for x in (*_yup(v), 1 if i == 3 else 0)]
        doc['cameras'] = [{'name': 'CTLux Camera', 'type': 'perspective', 'perspective': {
            'yfov': math.radians(camera['yfov_degrees']), 'aspectRatio': camera['aspect_ratio'],
            'znear': camera['znear'], 'zfar': camera['zfar']}, 'extras': {'ctlux': camera}}]
        doc['scenes'][0]['nodes'].append(len(doc['nodes']))
        doc['nodes'].append({'name': 'CTLux Camera', 'camera': 0, 'matrix': matrix})
    if not doc['nodes']:
        del doc['nodes']
        del doc['scenes'][0]['nodes']
    if binary:
        doc['buffers'] = [{'byteLength': len(binary)}]
    doc['extras']['ctlux']['warnings'] = list(warnings)
    encoded = _json(doc).encode('utf-8')
    encoded += b' ' * (-len(encoded) % 4)
    binary.extend(b'\0' * (-len(binary) % 4))
    total = 12 + 8 + len(encoded) + (8 + len(binary) if binary else 0)
    if total > 0xffffffff:
        raise ValueError('GLB 4 GiB kapsayıcı sınırını aşıyor')
    result = struct.pack('<III', 0x46546c67, 2, total) + struct.pack('<II', len(encoded), 0x4e4f534a) + encoded
    if binary:
        result += struct.pack('<II', len(binary), 0x004e4942) + binary
    return {'scene.glb': result}


def _fmt(v):
    return format(float(v), '.17g')


def _tuple(v):
    return '(' + ', '.join(_fmt(x) for x in v) + ')'


def _usd_array(values, n=1):
    if n == 1:
        return '[' + ', '.join(str(v) for v in values) + ']'
    return '[' + ', '.join(_tuple(values[i:i+n]) for i in range(0, len(values), n)) + ']'


def _usda(scene, data, groups, assets, warnings):
    v, f, m, uv, ok, materials, normals = data
    lights = [(item, _light(item, warnings, 'usda')) for item in scene.get('lights', [])]
    files = {}
    meta = _metadata(scene); meta['warnings'] = list(warnings)
    out = ['#usda 1.0', '(', '    defaultPrim = "Scene"', '    metersPerUnit = 1', '    upAxis = "Z"', ')',
           'def Xform "Scene" (', '    displayName = ' + _json(scene.get('name', 'Sahne')),
           '    customData = { string ctlux = ' + _json(_json(meta)) + ' }', ')', '{']
    if groups:
        out += ['    def Scope "Materials"', '    {']
        for index, (mi, has_uv) in enumerate(groups):
            textured = has_uv and mi in assets
            material = materials[mi]; color = material.get('color', [.55]*3)
            path = '/Scene/Materials/M' + str(index)
            out += ['        def Material "M%d" (' % index, '            displayName = ' + _json(material.get('name', str(mi))),
                    '        )', '        {', '            token outputs:surface.connect = <%s/Preview.outputs:surface>' % path,
                    '            def Shader "Preview"', '            {', '                uniform token info:id = "UsdPreviewSurface"',
                    ('                color3f inputs:diffuseColor.connect = <%s/Texture.outputs:rgb>' % path if textured else
                     '                color3f inputs:diffuseColor = ' + _tuple(color)),
                    '                float inputs:metallic = 0', '                float inputs:roughness = 0.8',
                    '                token outputs:surface', '            }']
            if textured:
                asset = assets[mi]; files[asset['name']] = asset['data']
                out += ['            def Shader "UV"', '            {', '                uniform token info:id = "UsdPrimvarReader_float2"',
                        '                string inputs:varname = "st"', '                float2 outputs:result', '            }',
                        '            def Shader "Texture"', '            {', '                uniform token info:id = "UsdUVTexture"',
                        '                asset inputs:file = @%s@' % asset['name'], '                token inputs:sourceColorSpace = "sRGB"',
                        '                token inputs:wrapS = "repeat"', '                token inputs:wrapT = "repeat"',
                        '                float2 inputs:st.connect = <%s/UV.outputs:result>' % path,
                        '                float4 inputs:scale = ' + _tuple(list(color)+[1]),
                        '                float3 outputs:rgb', '            }']
            out += ['        }']
        out += ['    }', '    def Mesh "Geometry" (prepend apiSchemas = ["MaterialBindingAPI"])', '    {',
                '        point3f[] points = ' + _usd_array(v, 3),
                '        int[] faceVertexCounts = ' + _usd_array([3]*(len(f)//3)),
                '        int[] faceVertexIndices = ' + _usd_array(f), '        uniform token subdivisionScheme = "none"',
                '        uniform token subsetFamily:materialBind:familyType = "partition"']
        if uv:
            out += ['        texCoord2f[] primvars:st = ' + _usd_array(uv, 2) + ' (interpolation = "vertex")']
        if normals:
            ns = [x for i in range(0, len(normals), 3) for x in _unit(normals[i:i+3])]
            out += ['        normal3f[] normals = ' + _usd_array(ns, 3) + ' (interpolation = "vertex")']
        for index, faces in enumerate(groups.values()):
            out += ['        def GeomSubset "M%d" (prepend apiSchemas = ["MaterialBindingAPI"])' % index, '        {',
                    '            uniform token elementType = "face"', '            uniform token familyName = "materialBind"',
                    '            int[] indices = ' + _usd_array(faces),
                    '            rel material:binding = </Scene/Materials/M%d>' % index, '        }']
        out += ['    }']
    for index, (item, light) in enumerate(lights):
        kind = light['type']; schema = 'RectLight' if kind == 'area' else 'DistantLight' if kind == 'distant' else 'SphereLight'
        q = _orientation(light['direction'])
        out += ['    def %s "Light%d" (' % (schema, index), '        displayName = ' + _json(item.get('name', item.get('id', 'Işık'))),
                '        customData = { string ctlux = ' + _json(_json(item)) + ' }']
        if kind == 'spot':
            out += ['        prepend apiSchemas = ["ShapingAPI"]']
        out += ['    )', '    {', '        double3 xformOp:translate = ' + _tuple(light['position']),
                '        quatf xformOp:orient = ' + _tuple([q[3], q[0], q[1], q[2]]),
                '        uniform token[] xformOpOrder = ["xformOp:translate", "xformOp:orient"]',
                '        color3f inputs:color = ' + _tuple(light['color']),
                '        float inputs:intensity = ' + _fmt(light['value']), '        float inputs:exposure = 0']
        if kind == 'area':
            out += ['        float inputs:width = ' + _fmt(light['size'][0]), '        float inputs:height = ' + _fmt(light['size'][1])]
        elif kind != 'distant':
            out += ['        float inputs:radius = ' + _fmt(light['radius']), '        bool treatAsPoint = true']
        if kind == 'spot':
            out += ['        float inputs:shaping:cone:angle = ' + _fmt(light['angle']/2),
                    '        float inputs:shaping:cone:softness = 0']
        out += ['    }']
    if 'camera' in scene:
        camera = camera_normalize(scene['camera'])
        rows = [(*v, 1 if i == 3 else 0) for i, v in enumerate(camera_basis(camera))]
        matrix = '(' + ', '.join(_tuple(row) for row in rows) + ')'
        # USD optics: sahne biriminin onda biri, metersPerUnit=1 iken .5 = 50 mm.
        focal = .5
        aperture = 2*focal*math.tan(math.radians(camera['yfov_degrees'])/2)
        out += ['    def Camera "Camera"', '    {', '        token projection = "perspective"',
                '        matrix4d xformOp:transform = ' + matrix,
                '        uniform token[] xformOpOrder = ["xformOp:transform"]',
                '        float focalLength = ' + _fmt(focal),
                '        float verticalAperture = ' + _fmt(aperture),
                '        float horizontalAperture = ' + _fmt(aperture*camera['aspect_ratio']),
                '        float2 clippingRange = ' + _tuple([camera['znear'], camera['zfar']]),
                '    }']
    out += ['}', '']
    files['scene.usda'] = '\n'.join(out).encode('utf-8')
    return files


def _obj(scene, data, groups, assets, warnings):
    v, f, m, uv, ok, materials, normals = data
    files = {}
    if scene.get('lights'):
        _warn(warnings, 'OBJ standart ışık taşımaz, ışıklar üst katmanın scene.ctlux.json yan dosyasında korunur.')
    if 'camera' in scene:
        _warn(warnings, 'OBJ standart kamera taşımaz, kamera scene.ctlux.json yan dosyasında korunur.')
    obj = ['# CTLux: metre, Z-up, display name ' + _json(scene.get('name', 'Sahne')), 'mtllib scene.mtl', 'o CTScene']
    obj.extend('v ' + ' '.join(_fmt(x) for x in v[i:i+3]) for i in range(0, len(v), 3))
    obj.extend('vt ' + ' '.join(_fmt(x) for x in uv[i:i+2]) for i in range(0, len(uv), 2))
    obj.extend('vn ' + ' '.join(_fmt(x) for x in _unit(normals[i:i+3])) for i in range(0, len(normals), 3))
    mtl = ['# CTLux: Kd temel renk, gelişmiş Radiance malzeme modeli taşınmaz.']
    for index, ((mi, has_uv), faces) in enumerate(groups.items()):
        textured = has_uv and mi in assets
        material = materials[mi]
        mtl += ['# display name ' + _json(material.get('name', str(mi))), 'newmtl M%d' % index,
                'Kd ' + ' '.join(_fmt(x) for x in material.get('color', [.55]*3)), 'illum 1']
        if textured:
            asset = assets[mi]; files[asset['name']] = asset['data']
            mtl += ['map_Kd ' + asset['name']]
        obj += ['usemtl M%d' % index]
        for face in faces:
            ids = f[face*3:face*3+3]
            # texture'ı olmayan yüzün geçerli kaynak UV'si de korunur.
            with_uv = bool(uv) and all(ok[i] for i in ids)
            def corner(i):
                s = str(i+1)
                if normals:
                    return s + '/' + (s if with_uv else '') + '/' + s
                return s + '/' + s if with_uv else s
            obj.append('f ' + ' '.join(corner(i) for i in ids))
    files['scene.obj'] = ('\n'.join(obj)+'\n').encode('utf-8')
    files['scene.mtl'] = ('\n'.join(mtl)+'\n').encode('utf-8')
    return files


def export_scene(format_name, scene, output_dir, asset_resolver=None):
    """Yeni çıktı dizinine yazar, main/files göreli path'ler, warnings/stats döner.

    asset_resolver(ref) -> {'data': bytes, 'mime_type': 'image/png'|'image/jpeg'}.
    JSON yan dosyası, proje kaydı ve kaynak texture dosyası bu modülde yazılmaz.
    """
    if format_name in ('rad', 'radiance'):
        return export_radiance(scene, output_dir, asset_resolver)
    writers = {'glb': _glb, 'obj': _obj, 'usda': _usda}
    if format_name not in writers:
        raise ValueError('Yazım biçimi yalnız glb, obj, usda veya radiance olabilir')
    data = _validate(scene)
    warnings = list(dict.fromkeys(str(x) for x in scene.get('warnings', [])))
    assets = _assets(data[5], asset_resolver, warnings)
    groups = _groups(data[1], data[2], data[3], data[4], assets, warnings)
    if not data[1]:
        _warn(warnings, 'Sahnede yüzey geometrisi yok, bazı hedef uygulamalar yalnız ışık içeren dosyayı içe almayabilir.')
    payloads = writers[format_name](scene, data, groups, assets, warnings)
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    # isimler kodun sabitleri ya da hash'leri, kullanıcının verdiği ad path'e girmez.
    for name in payloads:
        target = directory / name
        if target.exists() or target.is_symlink():
            raise FileExistsError('Mevcut çıktı üzerine yazılmaz: ' + name)
        if target.parent.is_symlink():
            raise ValueError('Çıktı alt dizini sembolik bağ olamaz')
    for name, content in payloads.items():
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('xb') as output:
            output.write(content)
    return {'main': 'scene.' + format_name, 'files': list(payloads), 'warnings': warnings,
            'stats': {'vertices': len(data[0])//3, 'triangles': len(data[1])//3,
                      'lights': len(scene.get('lights', [])), 'bytes': sum(len(x) for x in payloads.values())}}


def default_camera(scene):
    """Kaynakta kamera yoksa bütün geometriyi ve light'ları kapsayan perspective kamera."""
    from itertools import chain
    v = scene['mesh']['v']
    points = chain((v[i:i+3] for i in range(0, len(v), 3)),
                   (light['position'] for light in scene.get('lights', [])))
    first = next(points, [0, 0, 0])
    low, high = list(first), list(first)
    for point in points:
        low = [min(a, b) for a, b in zip(low, point)]
        high = [max(a, b) for a, b in zip(high, point)]
    center = [(a+b)/2 for a, b in zip(low, high)]
    radius = max(math.dist(low, high)/2, .1)
    offset = [radius*2.4, -radius*3.2, radius*2.2]
    return camera_normalize({'type': 'perspective',
        'position': [a+b for a, b in zip(center, offset)],
        'direction': [-x for x in offset], 'up': [0, 0, 1],
        'yfov_degrees': 45, 'aspect_ratio': 4/3,
        'znear': .001, 'zfar': max(radius*100, 1000)})


def _yorum_unlemlerini_sil(text):
    # ies2rad IES başlığını # yorumlarına kopyalar. glare her ! işaretini reddettiği için orada silinir.
    return ''.join(line.replace('!', '') if line.lstrip().startswith('#') else line
                   for line in text.splitlines(True))


def export_radiance(scene, output_dir, asset_resolver=None, *, max_triangles=None):
    """CTScene -> statik Radiance, IES varsa gerçek ies2rad/xform kullanılır.

    asset_resolver(göreli IES path'i) -> {'data': bytes}. Eksik fotometri hatadır.
    Çıktı klasörü yeni ya da boş olmalı. Kaynaktaki isimler Radiance identifier'ı yapılmaz.
    """
    from core import engine as motor, processes as surecler
    from core.tools.ies_analysis import ies_oku
    from core.photometry import notlu_ies
    from core.scene_model import validate_scene
    from core.scene_camera import radiance_view
    validate_scene(scene, max_triangles=max_triangles)
    v, f, m, uv, ok, materials, normals = _validate(scene)
    directory = Path(output_dir)
    if any(p.is_symlink() for p in (directory, *directory.parents)):
        raise ValueError('Çıktı yolu sembolik bağ içeremez')
    if directory.exists() and any(directory.iterdir()):
        raise FileExistsError('Radiance çıktı klasörü boş olmalı')
    directory.mkdir(parents=True, exist_ok=True)
    warnings = list(scene.get('warnings', []))
    if f:
        _warn(warnings, 'Radiance malzemesi mat temel renktir, cam, metal ve özel shader parametreleri uygulanmadı.')
    mats, geo, lights = [], [], []
    def primitive(modifier, kind, name, numbers):
        return '%s %s %s\n0\n0\n%d %s\n' % (
            modifier, kind, name, len(numbers), ' '.join(format(x, '.12g') for x in numbers))
    for i, mat in enumerate(materials):
        color = mat['color']
        if any(x > 1 for x in color):
            _warn(warnings, 'Radiance yansıtıcılığı 0..1 aralığına sınırlandı: m%d' % i)
        mats.append(primitive('void', 'plastic', 'm%d' % i, [*map(lambda x: min(x, 1), color), 0, 0]))
        if mat.get('texture'):
            _warn(warnings, 'Radiance dokusu atlandı, temel renk kullanıldı: m%d' % i)
    if normals:
        _warn(warnings, 'Radiance yüzey normalleri üçgenlerden hesaplandı, yumuşak köşe normalleri uygulanmadı.')
    for i in range(0, len(f), 3):
        tri = f[i:i+3]
        geo.append(primitive('m%d' % m[tri[0]], 'polygon', 'yuz%d' % (i//3),
                             [x for j in tri for x in v[3*j:3*j+3]]))
    for i, light in enumerate(scene.get('lights', [])):
        if not light['enabled']:
            _warn(warnings, 'Kapalı ışık Radiance sahnesine eklenmedi: ' + light['id'])
            continue
        source = light.get('source', {})
        value = light['intensity']['value']
        if value == 0:
            _warn(warnings, 'Sıfır güçlü ışık Radiance sahnesine eklenmedi: ' + light['id'])
            continue
        name = 'l%d' % i
        position, direction = light['position'], light['direction']
        if source.get('ies') or source.get('urun_rad'):
            ref = source.get('ies')
            if not ref or asset_resolver is None:
                raise ValueError('Radiance için kaynak IES fotometrisi gerekli: ' + light['id'])
            data = asset_resolver(ref)['data']
            if not isinstance(data, bytes):
                raise ValueError('Fotometri çözücüsü bayt döndürmeli')
            folder = directory/'isik'; folder.mkdir(exist_ok=True)
            # teslim edilen IES sahiplik notunu taşır, not zaten varsa bayt eklenmez.
            ies = folder/(name+'.ies'); ies.write_bytes(notlu_ies(data))
            ies_oku(ies)
            motor.ortam_hazirla()
            result = surecler.calistir(['ies2rad', '-m', str(value), '-o', 'isik/'+name,
                                        'isik/'+name+'.ies'], cwd=str(directory), env=motor.radiance_ortami())
            if result.returncode:
                raise RuntimeError('ies2rad başarısız: ' + result.stderr[-500:])
            if result.stderr.strip():
                warnings.extend('ies2rad: ' + line.strip() for line in result.stderr.splitlines() if line.strip())
            rx, ry, rz = motor.fotometri_donusu(direction, light.get('c0_direction'))
            result = surecler.calistir(['xform', '-n', name, '-rx', repr(rx), '-ry', repr(ry), '-rz', repr(rz),
                '-t', *map(str, position), 'isik/'+name+'.rad'], cwd=str(directory), env=motor.radiance_ortami())
            if result.returncode:
                raise RuntimeError('xform başarısız: ' + result.stderr[-500:])
            lights.append(_yorum_unlemlerini_sil(result.stdout))
            if result.stderr.strip():
                _warn(warnings, 'xform: ' + result.stderr.strip())
            continue
        _warn(warnings, 'Fotometrisiz ışık gücü Radiance ışınımına sayısal eşlendi, fiziksel kalibrasyon yok.')
        color = [x*value for x in light['color_linear']]
        kind = light['type']
        if kind == 'spot':
            lights.append(primitive('void', 'spotlight', name+'_m',
                [*color, max(.01, light['cone_angle_degrees']), *direction]))
        else:
            lights.append(primitive('void', 'light', name+'_m', color))
        if kind == 'area':
            from core.tools.ls10_reader import _perp
            u, w = _perp(direction)
            a, b = [max(.001, x)/2 for x in light['size_m']]
            corners = [[position[j]+su*a*u[j]+sw*b*w[j] for j in range(3)]
                       for su, sw in [(-1,-1),(1,-1),(1,1),(-1,1)]]
            lights.append(primitive(name+'_m', 'polygon', name, [x for p in corners for x in p]))
        elif kind == 'distant':
            lights.append(primitive(name+'_m', 'source', name, [*[-x for x in direction], .533]))
            _warn(warnings, 'Uzak ışık açısal çapı 0.533 derece varsayıldı.')
        else:
            lights.append(primitive(name+'_m', 'sphere', name, [*position, max(.001, light['radius_m'])]))
    camera = scene.get('camera') or default_camera(scene)
    payloads = {'malzeme.rad': ''.join(mats), 'geometri.rad': ''.join(geo),
                'isiklar.rad': ''.join(lights), 'gorunum.vf': 'VIEW= '+radiance_view(camera, 320, 240)+'\n'}
    # tek statik dosyada dış shell komutu yok. IES .dat referansları çıktı köküne göre.
    payloads['scene.rad'] = payloads['malzeme.rad']+payloads['geometri.rad']+payloads['isiklar.rad']
    for name, content in payloads.items():
        (directory/name).write_text(content, encoding='utf-8')
    files = sorted(p.relative_to(directory).as_posix() for p in directory.rglob('*') if p.is_file())
    return {'main': 'scene.rad', 'files': files, 'warnings': warnings,
            'stats': {'vertices': len(v)//3, 'triangles': len(f)//3, 'lights': len(scene.get('lights', []))}}
