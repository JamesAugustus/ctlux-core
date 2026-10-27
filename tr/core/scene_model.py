# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Proje içindeki statik geometrinin tamamını CTScene'e okur, kaynağa ya da cache'e yazmaz.

CTScene metre ve Z-up kullanır. Bu native bir EVO/LSF writer'ı ya da fiziksel güç
kalibrasyonu değil. Asıl light kaydı ``original`` içinde ayrıca saklanır.
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

from core.file_safety import ic_yol
from core.preview_materials import (Malzemeler, _satirlar, _yan_yol, _mtl_oku,
                             obj_bilgisi, obj_yuzleri, rad_bilgisi, rad_yuzleri)
from core.radiance_preview import DESTEKLENEN_TURLER, primitif_ucgen_sayisi
from core.render_cache import dosya_ozeti
from core.scene_camera import camera_normalize

MAX_TRIANGLES = 3_000_000
MAX_SOURCE_BYTES = 1024 * 1024 * 1024
MAX_FACE_VERTICES = 4096


def _sinir(value, default):
    value = default if value is None else value
    if type(value) is not int or value <= 0:
        raise ValueError('Sınır pozitif tam sayı olmalı')
    return value


def _ucgen_kontrol(count, limit):
    if count > limit:
        raise ValueError('Tam geometri: %d üçgen okundu, sınır %d, '
                         '--ucgen-siniri ile yükseltin, örnekleme yapılmadı' % (count, limit))


def _number(value, where, minimum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(where + ': sayı bekleniyor')
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite or (minimum is not None and value < minimum):
        raise ValueError(where + ': geçersiz/sonlu olmayan sayı')
    return value


def _vector(value, size, where, minimum=None):
    if not isinstance(value, list) or len(value) != size:
        raise ValueError(where + ': vektör boyutu geçersiz')
    for v in value:
        _number(v, where, minimum)
    return value


def _relative(value):
    return (isinstance(value, str) and bool(value) and not value.startswith(('/', '\\'))
            and ':' not in value and '..' not in value.replace('\\', '/').split('/'))


def _json_value(value, depth=0):
    """Metadata'da NaN, Python nesnesi ya da sınırsız iç içelik saklama."""
    if depth > 64:
        raise ValueError('Metadata iç içelik sınırını aşıyor')
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
        raise ValueError('Metadata JSON değeri değil')


def validate_scene(scene, *, max_triangles=None):
    """CTScene v1 sözleşmesini doğrula, değiştirmeden aynı dict'i döner."""
    max_triangles = _sinir(max_triangles, MAX_TRIANGLES)
    if not isinstance(scene, dict) or scene.get('schema') != 'ctlux.scene' or type(scene.get('version')) is not int or scene['version'] != 1:
        raise ValueError('Desteklenmeyen CTScene şeması/sürümü')
    if scene.get('units') != 'm' or scene.get('up_axis') != 'Z' or not isinstance(scene.get('name'), str):
        raise ValueError('CTScene metre/Z-up ve metin ad gerektirir')
    mesh = scene.get('mesh')
    if not isinstance(mesh, dict):
        raise ValueError('CTScene mesh eksik')
    for key in ('v', 'f', 'm', 'uv', 'uv_ok', 'materials'):
        if not isinstance(mesh.get(key), list):
            raise ValueError('Mesh dizisi eksik: ' + key)
    v, f, materials = mesh['v'], mesh['f'], mesh['materials']
    n = len(v)//3
    only_lights = not v and not f and bool(scene.get('lights'))
    if (not n or not f) and not only_lights or len(v) % 3 or len(f) % 3:
        raise ValueError('Geometri boş veya xyz/üçgen dizisi bozuk')
    _ucgen_kontrol(len(f)//3, max_triangles)
    if len(mesh['m']) != n or len(mesh['uv']) != n*2 or len(mesh['uv_ok']) != n:
        raise ValueError('Mesh malzeme/UV dizileri köşe sayısıyla uyuşmuyor')
    for x in chain(v, mesh['uv']):
        _number(x, 'Mesh koordinatı')
    if not materials:
        raise ValueError('Malzeme tablosu boş')
    for mat in materials:
        if not isinstance(mat, dict) or not isinstance(mat.get('name'), str):
            raise ValueError('Malzeme adı geçersiz')
        _vector(mat.get('color'), 3, 'Malzeme rengi', 0)
        if 'texture' in mat and not _relative(mat['texture']):
            raise ValueError('Doku yolu proje içinde göreli olmalı')
        _json_value(mat)
    for i in f:
        if type(i) is not int or not 0 <= i < n:
            raise ValueError('Üçgen indisi köşe tablosu dışında')
    for i in mesh['m']:
        if type(i) is not int or not 0 <= i < len(materials):
            raise ValueError('Malzeme indisi tablo dışında')
    if any(type(i) is not int or i not in (0, 1) for i in mesh['uv_ok']):
        raise ValueError('uv_ok yalnız 0/1 içerebilir')
    if 'normals' in mesh:
        _vector(mesh['normals'], n*3, 'Mesh normal dizisi')
    lights, warnings = scene.get('lights'), scene.get('warnings')
    if not isinstance(lights, list) or not isinstance(warnings, list) or not all(isinstance(w, str) for w in warnings):
        raise ValueError('Işık/uyarı listesi geçersiz')
    if 'camera' in scene:
        camera_normalize(scene['camera'])
    ids = set()
    for light in lights:
        if not isinstance(light, dict) or not isinstance(light.get('id'), str) or not light['id'] or light['id'] in ids:
            raise ValueError('Işık kimliği boş veya tekrarlı')
        ids.add(light['id'])
        if not isinstance(light.get('name'), str) or light.get('type') not in ('point', 'spot', 'area', 'distant') or type(light.get('enabled')) is not bool:
            raise ValueError('Işık adı/türü/etkinliği geçersiz')
        _vector(light.get('position'), 3, 'Işık konumu')
        direction = _vector(light.get('direction'), 3, 'Işık yönü')
        if not math.isclose(math.hypot(*direction), 1, rel_tol=1e-6, abs_tol=1e-6):
            raise ValueError('Işık yönü birim vektör olmalı')
        _vector(light.get('color_linear'), 3, 'Işık rengi', 0)
        _vector(light.get('size_m'), 2, 'Işık boyutu', 0)
        _number(light.get('radius_m'), 'Işık yarıçapı', 0)
        if not 0 <= _number(light.get('cone_angle_degrees'), 'Işık açısı') <= 180:
            raise ValueError('Işık açısı 0..180 aralığında olmalı')
        intensity = light.get('intensity')
        if not isinstance(intensity, dict) or intensity.get('unit') not in ('relative', 'cd') or not isinstance(intensity.get('provenance'), str):
            raise ValueError('Işık yoğunluğunun birimi/kaynağı geçersiz')
        _number(intensity.get('value'), 'Işık yoğunluğu', 0)
        for key in ('source', 'original'):
            if not isinstance(light.get(key), dict):
                raise ValueError('Işık kaynak metadata eksik')
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
        self.limit = _sinir(max_source_bytes, MAX_SOURCE_BYTES)
        self.root, self.files, self.total = Path(root).resolve(), {}, 0

    def add(self, path):
        # RTM fallback'i ve yan dosyalar da resolve edilmiş kökün içinde kalmalı.
        p = Path(path).resolve()
        try:
            rel = p.relative_to(self.root).as_posix()
            safe = ic_yol(self.root, rel)
        except ValueError as exc:
            raise ValueError('Sahne kaynağı proje dışında') from exc
        if not p.is_file():
            raise ValueError('Sahne kaynağı bulunamadı: ' + rel)
        if safe not in self.files:
            st = p.stat()
            self.total += st.st_size
            if self.total > self.limit:
                raise ValueError('Tam sahne kaynakları: %.6f MiB kayıtlı kaynak, sınır %.6f MiB, '
                                 '--kaynak-siniri-mib ile yükseltin, örnekleme yapılmadı'
                                 % (self.total / 1048576, self.limit / 1048576))
            self.files[safe] = (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        return safe

    def verify(self):
        for path, stamp in self.files.items():
            st = os.stat(path)
            if (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns) != stamp:
                raise ValueError('Sahne okunurken kaynak değişti, tekrar deneyin')


def _obj_check(path, warn):
    counts = {'v': 0, 'vt': 0, 'vn': 0}
    triangles = 0
    allowed = {'o', 'g', 's', 'mtllib', 'usemtl'}
    for line, raw in enumerate(_satirlar(path), 1):
        p = raw.split('#', 1)[0].split()
        if not p:
            continue
        op = p[0]
        if op in counts:
            need = 1 if op == 'vt' else 3
            if len(p) < need+1:
                raise ValueError('OBJ köşe/UV/normal kaydı eksik, satır %d' % line)
            vals = [float(x) for x in p[1:]]
            if not all(math.isfinite(x) for x in vals):
                raise ValueError('OBJ sonlu olmayan koordinat içeriyor')
            if op == 'v' and len(vals) == 4 and vals[3] != 1:
                raise ValueError('Homojen OBJ köşe ağırlığı henüz desteklenmiyor')
            if op == 'v' and len(vals) > 4:
                warn('OBJ köşe rengi uzantısı taşınmadı, MTL temel rengi korundu.')
            if op == 'vt' and len(vals) > 2:
                warn('OBJ üçüncü UV bileşeni taşınmadı, iki boyutlu UV korundu.')
            if op == 'vn':
                warn('OBJ özel köşe normalleri taşınmadı, hedef yüzlerden normal hesaplayacak.')
            counts[op] += 1
        elif op == 'f':
            if not 3 <= len(p)-1 <= MAX_FACE_VERTICES:
                raise ValueError('OBJ yüz köşe sayısı desteklenen sınır dışında')
            for token in p[1:]:
                fields = token.split('/')
                if len(fields) > 3 or not fields[0]:
                    raise ValueError('OBJ yüz indisi bozuk')
                for j, value in enumerate(fields):
                    if not value and j:
                        continue
                    i = int(value)
                    size = counts[('v', 'vt', 'vn')[j]]
                    if i == 0 or not -size <= i <= size:
                        raise ValueError('OBJ yüz indisi mevcut tablo dışında')
            triangles += len(p)-3
        elif op in ('l', 'p'):
            warn('OBJ çizgi/nokta öğeleri üçgen mesh içinde temsil edilmedi.')
        elif op not in allowed:
            raise ValueError('Desteklenmeyen OBJ kaydı: ' + op)
    return triangles


def _rad_check(path, warn):
    def tokens():
        for line in _satirlar(path):
            if line.startswith('!'):
                raise ValueError('Dinamik !RAD komutu dışa aktarım için çalıştırılmaz')
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
                    raise ValueError('RAD argüman sayısı geçersiz')
                groups.append([next(it) for _ in range(n)])
            values = [float(v) for v in groups[2]]
            if not all(math.isfinite(v) for v in values):
                raise ValueError('RAD sonlu olmayan parametre içeriyor')
            if kind in ('instance', 'mesh'):
                raise ValueError('RAD ' + kind + ' kaydı tam sahne aktarımında henüz desteklenmiyor')
            if kind in DESTEKLENEN_TURLER:
                triangles += primitif_ucgen_sayisi(kind, values)
                if kind != 'polygon':
                    warn('Analitik RAD ' + kind + ' yüzeyi sınırlı bölümlü üçgenlerle yaklaşık temsil edildi, özgün kaynak korunmalı.')
            elif kind not in ('plastic', 'metal', 'trans', 'glass'):
                warn('RAD ' + kind + ' kaydı ortak sahnede tam temsil edilmedi, özgün kaynak korunmalı.')
        except StopIteration as exc:
            raise ValueError('RAD kaydı yarıda kesilmiş') from exc


def _triangles(points, warn):
    """Köşe sırasını koru, içbükey düzlemsel yüzleri fan ile yanlış doldurma."""
    n = len(points)
    if n == 3:
        yield (0, 1, 2)
        return
    if not 3 <= n <= MAX_FACE_VERTICES:
        raise ValueError('Yüz köşe sayısı sınır dışında')
    # büyük world koordinatlarında projeksiyonu local origin'e taşı.
    p = [tuple(a-b for a, b in zip(v, points[0])) for v in points]
    normal = [sum(p[i][(j+1)%3]*p[(i+1)%n][(j+2)%3] - p[i][(j+2)%3]*p[(i+1)%n][(j+1)%3] for i in range(n)) for j in range(3)]
    length = math.hypot(*normal)
    if not math.isfinite(length) or length == 0:
        raise ValueError('Yüz normali güvenilir biçimde hesaplanamadı')
    extent = max(math.hypot(*v) for v in p)
    if any(abs(sum(v[i]*(normal[i]/length) for i in range(3))) > extent*1e-7 for v in p):
        warn('Düzlemsel olmayan çokgen üçgenlere ayrıldı, yüz yorumu yaklaşık olabilir.')
    axis = max(range(3), key=lambda i: abs(normal[i]))
    axes = [i for i in range(3) if i != axis]
    q = [(v[axes[0]], v[axes[1]]) for v in p]
    area = sum(q[i][0]*q[(i+1)%n][1]-q[(i+1)%n][0]*q[i][1] for i in range(n))
    if not math.isfinite(area) or area == 0:
        raise ValueError('Yüzün üçgenleştirilebilir alanı yok')
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
            raise ValueError('İçbükey/kesişen yüz güvenilir biçimde üçgenleştirilemedi')
    yield tuple(remaining)


def _light_name(record):
    """Light'ın görünen adı için öncelik sırası, kaynak kimliği 0 da geçerli bir sonek."""
    name = str(record.get('ad') or '')
    if name and not re.fullmatch(r'armatur_[0-9]+', name):
        return name
    if record.get('kaynak_ad'):
        return str(record['kaynak_ad'])
    if record.get('urun_ad'):
        source_id = record.get('kaynak_id')
        suffix = name if source_id is None or source_id == '' else str(source_id)
        return str(record['urun_ad']) + (' · '+suffix if suffix else '')
    return name or 'Adsız ışık'


def _light_identity(record, index):
    """Konum ya da ad değişince kimlik değişmez, farklı kaynak alanları ayrı tutulur."""
    namespace = record.get('kaynak', '')
    for key in ('kaynak_guid', 'id', 'kaynak_id'):
        if record.get(key) is not None and record[key] != '':
            identity = (namespace, key, record[key])
            break
    else:
        identity = (namespace, 'index', index)
    raw = json.dumps(identity, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    return 'light-'+hashlib.sha256(raw.encode()).hexdigest()[:20]


def _lights(records, root, warn):
    if not isinstance(records, list):
        raise ValueError('Proje ışıkları liste olmalı')
    result, ids = [], {}
    kinds = {'light': 'point', 'illum': 'point', 'omni': 'point', 'point': 'point',
             'spot': 'spot', 'alan': 'area', 'area': 'area', 'gunes': 'distant', 'distant': 'distant'}
    for i, record in enumerate(records):
        if not isinstance(record, dict):
            raise ValueError('Işık kaydı nesne olmalı')
        _json_value(record)
        original = deepcopy(record)
        raw_kind = record.get('tip', 'light')
        if raw_kind not in kinds:
            raise ValueError('Desteklenmeyen ışık türü: ' + str(raw_kind))
        ident = _light_identity(record, i)
        ids[ident] = ids.get(ident, 0)+1
        if ids[ident] > 1:
            warn('Tekrarlanan kaynak ışık kimliği aynı kimlik içindeki sıra ile ayrıldı, kalıcı tekil kaynak kimliği önerilir.')
            ident += '-'+str(ids[ident])
        direction = list(record.get('yon') or [0, 0, -1])
        _vector(direction, 3, 'Kaynak ışık yönü')
        length = math.hypot(*direction)
        if not math.isfinite(length) or length == 0:
            raise ValueError('Kaynak ışık yönü sıfır/sonlu değil')
        source = {k: deepcopy(record[k]) for k in ('kaynak', 'kaynak_id', 'kaynak_guid', 'kaynak_ad', 'urun_ad', 'urun_id', 'dizi_id', 'renk_kaynagi', 'renk_notu', 'fotometri_durumu') if k in record}
        for key in ('ies', 'urun_rad'):
            value = record.get(key)
            if value:
                try:
                    source[key] = Path(ic_yol(root, value)).relative_to(root).as_posix()
                except (ValueError, TypeError):
                    warn('Işık fotometri başvurusu proje içinde değil, yalnız original kaydında korundu.')
        # IES olması guc'u fiziksel kandela yapmaz. Dimmer sadece çarpan.
        photometric = bool(record.get('ies') or record.get('urun_rad'))
        value = record.get('dimmer', 1) if photometric else record.get('guc', 1)
        intensity = {'value': value, 'unit': 'relative', 'provenance': 'fotometri_dimmer' if photometric else 'proje.guc, fiziksel kandela kalibrasyonu yok'}
        if photometric:
            warn('IES/ürün ışığının yoğunluğu göreli dimmer olarak taşındı, fotometri hedef okuyucuda ayrıca uygulanmalı.')
        if 'renk' not in record or record['renk'] is None:
            warn('Kaynak ışık rengi yok, nötr beyaz varsayılanı açıkça kullanıldı.')
        result.append({'id': ident, 'name': _light_name(record),
                       'position': [record.get('x', 0), record.get('y', 0), record.get('z', 3)],
                       'direction': [v/length for v in direction], 'type': kinds[raw_kind],
                       'color_linear': deepcopy([1, 1, 1] if record.get('renk') is None else record['renk']),
                       'enabled': record.get('etkin') is not False,
                       'cone_angle_degrees': record.get('aci', 60), 'radius_m': record.get('yaricap', .15),
                       'size_m': [record.get('w', .5), record.get('h', .5)],
                       'intensity': intensity, 'source': source, 'original': original})
    return result


def scene_from_project(project_dir, project, lights=None, *, max_source_bytes=None, max_triangles=None):
    """Tam OBJ/statik RAD -> CTScene. Limit aşılırsa hata, asla sampling yok."""
    if not isinstance(project, dict):
        raise ValueError('Proje kaydı nesne olmalı')
    # ortak transform'u kullan, önizleme ya da cache üretme.
    from core.engine import _wire_kaynagi, _don_nokta
    max_triangles = _sinir(max_triangles, MAX_TRIANGLES)
    sources = _Sources(project_dir, max_source_bytes)
    warnings = []
    def warn(message):
        if message not in warnings:
            warnings.append(message)
    girdiler, total, textures = [], 0, {}
    geometry = project.get('geometri')
    if not isinstance(geometry, list) or not geometry and not (project.get('armaturler') if lights is None else lights):
        raise ValueError('Projenin kaynak geometrisi yok')
    for gi, g in enumerate(geometry):
        if not isinstance(g, dict):
            raise ValueError('Geometri kaydı nesne olmalı')
        path = sources.add(_wire_kaynagi(ic_yol(sources.root, g.get('dosya', ''))))
        suffix = Path(path).suffix.lower()
        if suffix not in ('.obj', '.rad'):
            raise ValueError('Tam sahne için statik OBJ/RAD kaynağı gerekli: ' + suffix)
        scale = _number(g.get('olcek', 1), 'Geometri ölçeği')
        _number(g.get('rx', 0), 'Geometri X dönüşü')
        if scale <= 0:
            raise ValueError('Geometri ölçeği pozitif olmalı')
        obj = suffix == '.obj'
        total += _obj_check(path, warn) if obj else _rad_check(path, warn)
        _ucgen_kontrol(total, max_triangles)
        bilgi = obj_bilgisi(path, dosya_ozeti(path)) if obj else rad_bilgisi(path, dosya_ozeti(path))
        girdiler.append((gi, g, path, obj, bilgi))
        # MTL texture path'i boyut limitinden bağımsız korunur. Dosya açılmaz,
        # kopyalama/paketleme üst katmanın işi.
        for line in bilgi.get('mtllib', []):
            try:
                one = _yan_yol(sources.root, path, line)
                names = [line] if os.path.isfile(one) else shlex.split(line, posix=False)
                for name in names:
                    mp = _yan_yol(sources.root, path, name.strip('\"\''))
                    if not os.path.isfile(mp):
                        warn('MTL dosyası eksik, malzeme için temel renk yedeği kullanılacak.')
                        continue
                    mp = sources.add(mp)
                    for name, mat in _mtl_oku(mp).items():
                        if not mat.get('doku'):
                            continue
                        if mat['doku'].startswith('-'):
                            warn('MTL map_Kd seçenekleri çözümlenmedi, doku başvurusu taşınmadı.')
                            continue
                        dp = _yan_yol(sources.root, mp, mat['doku'].strip('\"\''))
                        if os.path.isfile(dp):
                            sources.add(dp)
                            textures[(gi, name)] = Path(dp).relative_to(sources.root).as_posix()
                        else:
                            warn('MTL dokusu eksik, kaynak temel renk korundu.')
            except ValueError as exc:
                raise ValueError('MTL/doku başvurusu geçersiz veya kaynak kotasını aşıyor: ' + str(exc)) from exc
    material_path = ic_yol(sources.root, project.get('malzeme', 'malzeme/materials.rad'))
    if os.path.isfile(material_path):
        sources.add(material_path)
        _rad_check(material_path, warn)
    mats = Malzemeler(str(sources.root), project, girdiler)
    mesh = {key: [] for key in ('v', 'f', 'm', 'uv', 'uv_ok', 'materials')}
    vertices, material_ids = {}, {}
    atlanan_tekrarli = 0
    for gi, g, path, obj, bilgi in girdiler:
        faces = obj_yuzleri(path, uv_ile=True) if obj else ((a, b, p, [None]*len(p)) for a, b, p in rad_yuzleri(path))
        for group, name, points, uvs in faces:
            mi0 = mats.id(gi, name)
            texture = textures.get((gi, name))
            material_key = (mi0, texture)
            if material_key not in material_ids:
                m = mats.tablo[mi0]
                mi = material_ids[material_key] = len(mesh['materials'])
                material = {'name': m['ad'], 'color': list(m['renk'])}
                if texture:
                    material['texture'] = texture
                mesh['materials'].append(material)
            mi = material_ids[material_key]
            for triangle in _triangles(points, warn):
                if len({tuple(points[k]) for k in triangle}) != 3:
                    atlanan_tekrarli += 1
                    continue
                for k in triangle:
                    point, uv = tuple(points[k]), uvs[k]
                    key = (gi, group, mi, point, uv)
                    if key not in vertices:
                        vertices[key] = len(mesh['v'])//3
                        mesh['v'].extend(_don_nokta(point, g.get('olcek', 1), g.get('rx', 0)))
                        mesh['m'].append(mi)
                        mesh['uv'].extend(uv if uv is not None else (0, 0))
                        mesh['uv_ok'].append(int(uv is not None))
                    mesh['f'].append(vertices[key])
                _ucgen_kontrol(len(mesh['f'])//3, max_triangles)
            if texture and any(uv is None for uv in uvs):
                warn('Dokulu yüzün bazı UV koordinatları eksik, uv_ok maskesi korundu.')
    if atlanan_tekrarli:
        warn('%d tekrarlı köşeli, sıfır alanlı üçgen atlandı, kaynak dosya değiştirilmedi.' % atlanan_tekrarli)
    for w in mats.uyarilar:
        # helper'ın texture boyut sınırı burada uygulanmaz, base color warning'leri kalır.
        if not w.startswith('Doku kullanılamıyor,'):
            warn(w.replace('GPU önizlemesinde', 'ortak sahnede').replace('gösteriliyor', 'temsil ediliyor'))
    if not mesh['materials']:
        mesh['materials'].append({'name': 'Varsayılan', 'color': [.55, .55, .55]})
    scene = {'schema': 'ctlux.scene', 'version': 1, 'units': 'm', 'up_axis': 'Z',
             'name': str(project.get('ad') or sources.root.name), 'mesh': mesh,
             'lights': _lights(project.get('armaturler') or [] if lights is None else lights, sources.root, warn),
             'warnings': warnings}
    if project.get('kamera') is not None:
        scene['camera'] = camera_normalize(project['kamera'])
    sources.verify()
    return validate_scene(scene, max_triangles=max_triangles)
