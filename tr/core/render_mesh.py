# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""OBJ'yi değiştirmeden, malzemeleri koruyan Radiance geometri cache'i.

obj2mesh, obj2rad'ın atanmamış yüzeyler için yaptığı ``white`` varsayımını
yapmaz. Temp OBJ'de bu varsayımı açıkça yazıyoruz, vertex/face/UV/normal
kayıtları kaynakla aynı kalır. Render sahnesi dönen dosyayı ``void mesh`` ile
kullanmalı: malzemeler mesh'in içinde tanımlı. RTM patch kapasitesine
sığmayan, UV'siz geometri frozen OCT'ye çevrilir ve ``void instance`` kullanır.
"""
import hashlib
import json
import os
import shutil
from pathlib import Path
import tempfile
import threading
import uuid

from core.render_cache import dinamik_rad_var, dosya_ozeti, nesne_ozeti
from core import processes as surecler
from core import render_progress as render_ilerleme
from core import engine as motor


_SURUM = 3
_PARCA_ESIK = 8 << 20
_PARCA_YUZ = 50000
_kilit = threading.RLock()


class MeshDerlemeHatasi(RuntimeError):
    """Build başarısız, daha ağır polygon yolu güvenli bir fallback değil."""


class MeshUVDestekHatasi(RuntimeError):
    """RTM yerine polygon kullanmak local texture koordinatlarını kaybettirir."""


def _obj_hazirla(kaynak, hedef):
    """obj2rad: usemtl > ilk g adı > white. Kaynak hash'i de akış içinde hesaplanır."""
    ozet = hashlib.sha256()
    acik_malzeme = False
    etkin_malzeme = b"white"
    toplam = os.path.getsize(kaynak)
    okunan, bildirilen = 0, 0
    def bildir():
        surecler.iptal_kontrol()
        render_ilerleme.adim('obj_kopya', 'OBJ malzeme kayıtları hazırlanıyor',
                            tamamlanan=min(okunan, toplam), toplam=toplam, birim='bayt',
                            ayrinti=os.path.basename(kaynak))
    bildir()
    with open(kaynak, "rb") as gir, open(hedef, "wb") as cik:
        cik.write(b"usemtl white\n")
        for ham in gir:
            okunan += len(ham)
            if okunan - bildirilen >= 1024 * 1024:
                bildir()
                bildirilen = okunan
            ozet.update(ham)
            cik.write(ham)
            satir = ham.lstrip()
            # büyük OBJ'nin milyonlarca sayısal kaydını parse etmeye gerek yok.
            if satir[:1] not in (b"g", b"u"):
                continue
            parcalar = satir.split()
            if not parcalar or parcalar[0] not in (b"g", b"usemtl"):
                continue
            # grup/malzeme satırında Wavefront satır devamı varsa birlikte oku,
            # türetilen dosyada asıl fiziksel satırlar olduğu gibi kalır.
            mantiksal = satir.rstrip(b"\r\n")
            son_satir = ham
            while mantiksal.endswith(b"\\"):
                devam = next(gir, b"")
                if not devam:
                    break
                okunan += len(devam)
                ozet.update(devam)
                cik.write(devam)
                son_satir = devam
                mantiksal = mantiksal[:-1] + b" " + devam.rstrip(b"\r\n")
            parcalar = mantiksal.split()
            yazilacak = None
            if parcalar[0] == b"usemtl":
                # boş usemtl, obj2rad'da önceki seçimi sıfırlamaz.
                # obj2mesh ise sıfırlar, önceki seçimi tekrar yazmak gerekiyor.
                if len(parcalar) > 1:
                    acik_malzeme = True
                    etkin_malzeme = parcalar[1]
                else:
                    yazilacak = etkin_malzeme
            elif not acik_malzeme:
                etkin_malzeme = parcalar[1] if len(parcalar) > 1 else b"white"
                yazilacak = etkin_malzeme
            if yazilacak is not None:
                if not son_satir.endswith(b"\n"):
                    cik.write(b"\n")
                cik.write(b"usemtl " + yazilacak + b"\n")
    bildir()
    return ozet.hexdigest()


def _gecerli(rtm, kayit, imza, bicim="rtm"):
    """Sadece başarıyla bitmiş ve kaydına uyan dosya tekrar kullanılır."""
    try:
        bilgi = json.loads(kayit.read_text(encoding="utf-8"))
        for entry in bilgi.get('parcalar', []):
            part = (rtm.parent.parent / entry['yol']).resolve()
            if (not part.is_relative_to(rtm.parent.resolve()) or not part.is_file()
                    or part.stat().st_size != entry['boyut'] or dosya_ozeti(part) != entry['sha256']):
                return False
        return (bilgi.get("imza") == imza
                and bilgi.get("bicim") == bicim
                and bilgi.get("boyut") == rtm.stat().st_size
                and bilgi.get("boyut", 0) > 128
                and bilgi.get("sha256") == dosya_ozeti(rtm))
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        return False


def _rtm_kontrol(rtm, bicim="rtm"):
    """Process başarılı olsa bile boş ya da text çıktı bıraktıysa cache'e alma."""
    if not rtm.is_file() or rtm.stat().st_size <= 128:
        raise RuntimeError("obj2mesh boş veya eksik mesh üretti")
    with rtm.open("rb") as f:
        baslik = f.read(16384).split(b"\n\n", 1)[0]
    format_satiri = b"FORMAT=Radiance_octree" if bicim == "oct" else b"FORMAT=Radiance_tmesh"
    if not baslik.startswith(b"#?RADIANCE\n") or format_satiri not in baslik.splitlines():
        raise RuntimeError("obj2mesh çıktısı Radiance mesh biçiminde değil")


def _uv_var(obj):
    """UV içeren girdide polygon fallback'ine sessizce geçilmez."""
    with open(obj, "rb") as f:
        return any(s.lstrip().split(None, 1)[:1] == [b"vt"] for s in f)


def _frozen_oct(proje, obj_tmp, mat, hedef, geciciler):
    """RTM kapasite sınırı: kaynaktaki bütün yüzleri frozen OCT içinde sakla."""
    fd, ad = tempfile.mkstemp(prefix=".mesh_", suffix=".rad", dir=proje / "_cache")
    os.close(fd)
    rad = Path(ad)
    geciciler.append(rad)
    for komut, cikti in ((["obj2rad", os.path.relpath(obj_tmp, proje)], rad),
                         (["oconv", "-f", os.path.relpath(mat, proje), os.path.relpath(rad, proje)], hedef)):
        render_ilerleme.adim('mesh_' + komut[0], 'Radiance frozen geometri hazırlanıyor (%s)' % komut[0])
        sonuc = surecler.calistir(komut, cwd=str(proje), stdout_yolu=str(cikti), timeout=3600,
                                 env=motor.radiance_ortami())
        if sonuc.returncode:
            raise RuntimeError("Frozen geometri hazırlanamadı (%s): %s"
                               % (komut[0], (sonuc.stderr or "bilinmeyen hata")[-1500:]))



def _parcali_derle(proje, obj, mat, hedef):
    """Sıralı ve sınırlı RTM chunk'ları, vertex/UV/normal ve malzeme korunur.

    Sadece matematiksel olarak tam sıfır alanlı üçgenler atlanır, tolerans,
    sadeleştirme ya da çakışan yüzleri merge etme yok. Kaynak değişmez.
    """
    from array import array
    import math
    folder = Path(tempfile.mkdtemp(prefix='mesh_parts_', dir=proje / '_cache'))
    records = [[], [], []]
    xyz = array('d')
    faces = []
    material = b'white'
    counts = {'yuz': 0, 'sifir_alan': 0, 'parca': 0}
    entries = []
    read_bytes = 0
    total_bytes = obj.stat().st_size
    succeeded = False

    def emit(batch):
        if not batch:
            return
        surecler.iptal_kontrol()
        serial = len(entries)
        source = folder / ('%06d.obj' % serial)
        compiled = folder / ('%06d.rtm' % serial)
        maps = [{}, {}, {}]
        for corners, _ in batch:
            for corner in corners:
                for k, index in enumerate(corner):
                    if index is not None and index not in maps[k]:
                        maps[k][index] = len(maps[k]) + 1
        with source.open('wb') as stream:
            for k, mapping in enumerate(maps):
                for index in mapping:
                    stream.write(records[k][index] + b'\n')
            last_material = None
            for corners, selected in batch:
                if selected != last_material:
                    stream.write(b'usemtl ' + selected + b'\n')
                    last_material = selected
                values = []
                for corner in corners:
                    components = [str(maps[k][index]).encode() if index is not None else b''
                                  for k, index in enumerate(corner)]
                    while len(components) > 1 and not components[-1]:
                        components.pop()
                    values.append(b'/'.join(components))
                stream.write(b'f ' + b' '.join(values) + b'\n')
        render_ilerleme.adim('mesh_parca', 'Radiance geometrisi parça parça derleniyor',
                            tamamlanan=min(read_bytes, total_bytes), toplam=total_bytes,
                            birim='bayt', ayrinti='%d parça tamamlandı' % len(entries))
        try:
            result = surecler.calistir(['obj2mesh', '-a', os.path.relpath(mat, proje),
                                       os.path.relpath(source, proje), os.path.relpath(compiled, proje)],
                                      cwd=str(proje), bellek_siniri=768 << 20, env=motor.radiance_ortami())
            if result.returncode:
                reason = (result.stderr or result.stdout or 'bilinmeyen hata')[-1500:]
                if len(batch) > 1 and any(token in reason.lower() for token in (
                        'out of memory', 'cannot allocate memory', 'too many patch triangles')):
                    compiled.unlink(missing_ok=True)
                    middle = len(batch) // 2
                    emit(batch[:middle])
                    emit(batch[middle:])
                    return
                raise MeshDerlemeHatasi('Geometri parçası derlenemedi: ' + reason)
            _rtm_kontrol(compiled)
            entries.append({'yol': os.path.relpath(compiled, proje),
                            'boyut': compiled.stat().st_size, 'sha256': dosya_ozeti(compiled)})
        finally:
            source.unlink(missing_ok=True)

    try:
        with obj.open('rb') as stream:
            pending = b''
            for raw in stream:
                read_bytes += len(raw)
                line = raw.split(b'#', 1)[0].strip()
                pending += line[:-1] + b' ' if line.endswith(b'\\') else line
                if line.endswith(b'\\'):
                    continue
                line, pending = pending, b''
                parts = line.split()
                if not parts:
                    continue
                tag = parts[0]
                if tag in (b'v', b'vt', b'vn'):
                    k = (b'v', b'vt', b'vn').index(tag)
                    records[k].append(line)
                    if k == 0:
                        # homojen koordinatta ya da özel kayıtta alana bakıp eleme yapma.
                        coords = [float(x) for x in parts[1:4]] if len(parts) == 4 else [math.nan]*3
                        if len(coords) != 3:
                            raise MeshDerlemeHatasi('Geçersiz OBJ köşe kaydı')
                        xyz.extend(coords)
                elif tag == b'usemtl':
                    if len(parts) > 1:
                        material = parts[1]
                elif tag == b'f':
                    counts['yuz'] += 1
                    corners = []
                    for value in parts[1:]:
                        components = value.split(b'/')
                        if len(components) > 3:
                            raise MeshDerlemeHatasi('Geçersiz OBJ köşe başvurusu')
                        corner = []
                        for k in range(3):
                            component = components[k] if k < len(components) else b''
                            if not component:
                                if k == 0:
                                    raise MeshDerlemeHatasi('OBJ yüzünde köşe eksik')
                                corner.append(None)
                                continue
                            index = int(component)
                            resolved = index-1 if index > 0 else len(records[k])+index
                            if index == 0 or not 0 <= resolved < len(records[k]):
                                raise MeshDerlemeHatasi('OBJ yüz başvurusu sınır dışında')
                            corner.append(resolved)
                        corners.append(tuple(corner))
                    if len(corners) < 3:
                        raise MeshDerlemeHatasi('OBJ yüzünde en az üç köşe olmalı')
                    if len(corners) == 3:
                        a, b, c = (corner[0]*3 for corner in corners)
                        ux, uy, uz = (xyz[b+j]-xyz[a+j] for j in range(3))
                        vx, vy, vz = (xyz[c+j]-xyz[a+j] for j in range(3))
                        if (uy*vz-uz*vy, uz*vx-ux*vz, ux*vy-uy*vx) == (0, 0, 0):
                            # sadece float underflow/rounding yüzünden yüz atılmasın.
                            # sıfır olduğunu asıl decimal koordinatlarla teyit et.
                            from fractions import Fraction
                            exact = [[Fraction(x.decode('ascii')) for x in records[0][corner[0]].split()[1:4]]
                                     for corner in corners]
                            eu = [exact[1][j]-exact[0][j] for j in range(3)]
                            ev = [exact[2][j]-exact[0][j] for j in range(3)]
                            if (eu[1]*ev[2]-eu[2]*ev[1], eu[2]*ev[0]-eu[0]*ev[2], eu[0]*ev[1]-eu[1]*ev[0]) == (0, 0, 0):
                                counts['sifir_alan'] += 1
                                continue
                    faces.append((corners, material))
                    if len(faces) >= _PARCA_YUZ:
                        emit(faces)
                        faces.clear()
                if counts['yuz'] % 8192 == 0:
                    surecler.iptal_kontrol()
            if pending:
                raise MeshDerlemeHatasi('Tamamlanmamış OBJ satır devamı')
        emit(faces)
        if not entries:
            raise MeshDerlemeHatasi('Render edilebilir yüz bulunamadı, kaynak korundu')
        records.clear()
        del xyz
        faces.clear()
        wrapper = folder / 'parts.rad'
        wrapper.write_text(''.join('void mesh part_%d\n1 %s\n0\n0\n' % (i, item['yol'])
                                   for i, item in enumerate(entries)))
        result = surecler.calistir(['oconv', '-f', '-n', str(max(8, len(entries)+1)), os.path.relpath(wrapper, proje)],
                                  cwd=str(proje), stdout_yolu=str(hedef), bellek_siniri=768 << 20,
                                  env=motor.radiance_ortami())
        if result.returncode:
            raise MeshDerlemeHatasi('Geometri parçaları birleştirilemedi: ' + (result.stderr or '')[-1500:])
        _rtm_kontrol(hedef, 'oct')
        counts['parca'] = len(entries)
        succeeded = True
        return entries, counts, folder
    finally:
        if not succeeded:
            shutil.rmtree(folder)


def parca_bagimliliklari(proje, rel):
    """Dışarıdaki RTM chunk'larını sahne doğrulamasına ve bellek hesabına dahil et."""
    root = Path(proje).resolve()
    record = (root / rel).with_suffix('.json')
    data = json.loads(record.read_text())
    result = []
    for entry in data.get('parcalar', []):
        path = (root / entry['yol']).resolve()
        if not path.is_relative_to(root / '_cache'):
            raise MeshDerlemeHatasi('Mesh parçası proje önbelleği dışında')
        result.append(os.path.relpath(path, root))
    return result


def derle(proje_dir, obj_yol, mat_yol):
    """Content hash'li .rtm'in, RTM kapasite sınırında da .oct'un göreli path'ini döner.

    Build hatası RuntimeError, obj2rad fallback'ine geçip geçmemeye çağıran
    karar verir. Lock, aynı process'te iki isteğin aynı cache'i yarım yarım
    yazmasını önler. Tamamlanma kaydı en son, atomik olarak yerleştirilir.
    """
    proje = Path(proje_dir).resolve()
    obj = Path(obj_yol)
    mat = Path(mat_yol)
    obj = obj if obj.is_absolute() else proje / obj
    mat = mat if mat.is_absolute() else proje / mat
    cache = proje / "_cache"
    cache.mkdir(parents=True, exist_ok=True)
    with surecler.render_sirasi(), _kilit:
        geciciler = []
        part_folder = None
        published = False
        try:
            surecler.iptal_kontrol()
            render_ilerleme.adim('mesh_imza', 'OBJ ve malzeme içerik kimlikleri doğrulanıyor',
                                ayrinti=os.path.relpath(obj, proje))
            obj_ozet, mat_ozet = dosya_ozeti(obj), dosya_ozeti(mat)
            araclar = {ad: shutil.which(ad) for ad in ('obj2mesh', 'obj2rad', 'oconv')}
            imza = nesne_ozeti({"surum": _SURUM, "obj": obj_ozet, "mat": mat_ozet,
                               "araclar": {ad: dosya_ozeti(yol) if yol else None
                                            for ad, yol in araclar.items()}})
            if dinamik_rad_var([(str(mat), mat_ozet)]):
                imza = nesne_ozeti([imza, uuid.uuid4().hex])
            for bicim in ("rtm", "oct"):
                hedef = cache / ("mesh_" + imza + "." + bicim)
                kayit = hedef.with_suffix(".json")
                render_ilerleme.adim('mesh_onbellek', 'Hazır Radiance geometrisi doğrulanıyor',
                                    ayrinti=os.path.relpath(obj, proje))
                if _gecerli(hedef, kayit, imza, bicim):
                    render_ilerleme.adim('mesh_hazir', 'Doğrulanmış Radiance geometrisi önbellekten alındı',
                                        onbellek={'mesh': True})
                    return os.path.relpath(hedef, proje)
            render_ilerleme.adim('obj_kopya', 'OBJ geometri hazırlığı başlıyor', onbellek={'mesh': False})
            for son in (".obj", ".rtm", ".json"):
                fd, ad = tempfile.mkstemp(prefix=".mesh_", suffix=son, dir=cache)
                os.close(fd)
                geciciler.append(Path(ad))
            obj_tmp, rtm_tmp, kayit_tmp = geciciler
            if _obj_hazirla(obj, obj_tmp) != obj_ozet:
                raise RuntimeError("OBJ hazırlanırken kaynak değişti, yeniden deneyin")
            parcalar, parca_sayim = [], {}
            if obj.stat().st_size >= _PARCA_ESIK:
                parcalar, parca_sayim, part_folder = _parcali_derle(proje, obj_tmp, mat, rtm_tmp)
                bicim = 'oct'
            else:
                komut = ["obj2mesh", "-a", os.path.relpath(mat, proje),
                         os.path.relpath(obj_tmp, proje), os.path.relpath(rtm_tmp, proje)]
                render_ilerleme.adim('mesh_derle', 'OBJ geometri derleniyor (obj2mesh)',
                                    ayrinti=os.path.relpath(obj, proje))
                sonuc = surecler.calistir(komut, cwd=str(proje), env=motor.radiance_ortami())
                bicim = "rtm"
                if sonuc.returncode:
                    neden = (sonuc.stderr or sonuc.stdout or "bilinmeyen hata")[-1500:]
                    if "too many patch triangles" not in neden:
                        raise RuntimeError("obj2mesh başarısız: " + neden)
                    if _uv_var(obj_tmp):
                        raise MeshUVDestekHatasi("RTM patch sınırı aşıldı. OBJ doku koordinatları içeriyor, "
                                                "UV kaybına yol açan polygon/OCT alternatifi uygulanmadı.")
                    _frozen_oct(proje, obj_tmp, mat, rtm_tmp, geciciler)
                    bicim = "oct"
            render_ilerleme.adim('mesh_dogrula', 'Derlenen Radiance geometrisi doğrulanıyor',
                                ayrinti=os.path.relpath(obj, proje))
            _rtm_kontrol(rtm_tmp, bicim)
            if dosya_ozeti(obj) != obj_ozet or dosya_ozeti(mat) != mat_ozet:
                raise RuntimeError("Mesh derlenirken kaynak veya malzeme değişti, yeniden deneyin")
            hedef = cache / ("mesh_" + imza + "." + bicim)
            kayit = hedef.with_suffix(".json")
            bilgi = {"imza": imza, "bicim": bicim, "boyut": rtm_tmp.stat().st_size,
                     "sha256": dosya_ozeti(rtm_tmp), "parcalar": parcalar, "sayim": parca_sayim}
            kayit_tmp.write_text(json.dumps(bilgi, sort_keys=True) + "\n", encoding="utf-8")
            os.replace(rtm_tmp, hedef)
            os.replace(kayit_tmp, kayit)
            published = True
            return os.path.relpath(hedef, proje)
        except OSError as e:
            raise RuntimeError("Radiance mesh hazırlanamadı: " + str(e)) from e
        finally:
            if part_folder is not None and not published:
                shutil.rmtree(part_folder)
            for gecici in geciciler:
                try:
                    gecici.unlink(missing_ok=True)
                except OSError:
                    pass
