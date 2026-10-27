# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Assimp -> doğrulanmış OBJ, iş başarısız olursa var olan hedefe dokunulmaz."""
from array import array
import hashlib
import math
import os
from pathlib import Path, PureWindowsPath
import shlex
import shutil
import tempfile
import threading
import warnings

from core import processes as surecler


_KOK = Path(__file__).resolve().parent.parent
_TESLIM_KILIDI = threading.Lock()
_MAP = {'bump', 'disp', 'decal', 'refl', 'norm'}
_TEK = {'-blendu', '-blendv', '-boost', '-bm', '-clamp', '-texres',
        '-imfchan', '-type', '-colorspace'}


def _kelimeler(s):
    lexer = shlex.shlex(s, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ''
    lexer.escape = ''  # Windows texture path'lerindeki backslash'ı yutmasın.
    return list(lexer)


def _yerel_yol(base, ad, kok):
    """Absolute, eski makineye ait ya da kök dışına çıkan path'i stat/read yapmadan reddet, symlink takip etmez."""
    if not ad or Path(ad).is_absolute() or PureWindowsPath(ad).drive:
        return None
    aday = Path(os.path.abspath(base / ad.replace('\\', '/')))
    if not aday.is_relative_to(kok):
        return None
    yol = kok
    for parca in aday.relative_to(kok).parts:
        yol = yol / parca
        if yol.is_symlink():
            return None
    return aday


def _mantiksal(fp):
    for ham in fp:
        s = ham.rstrip('\r\n')
        while s.endswith('\\'):
            devam = next(fp, None)
            if devam is None:
                raise RuntimeError('OBJ/MTL satır devamı yarım kaldı')
            s = s[:-1] + ' ' + devam.rstrip('\r\n')
        yield s


def _indis(s, adet):
    try:
        n = int(s)
    except ValueError as exc:
        raise RuntimeError('OBJ yüz indeksi sayı değil') from exc
    i = n-1 if n > 0 else adet+n
    if n == 0 or not 0 <= i < adet:
        raise RuntimeError('OBJ yüz indeksi aralık dışında')
    return i


def _yuz_alani_var(v, ids):
    """Threshold kullanmadan collinear yüzü yakala, küçük ama gerçek üçgeni silme."""
    a = v[3*ids[0]:3*ids[0]+3]
    kenar = None
    for i in ids[1:]:
        e = [v[3*i+j]-a[j] for j in range(3)]
        boy = max(map(abs, e))
        if not math.isfinite(boy):
            raise RuntimeError('OBJ koordinat farkı sonlu değil')
        if not boy:
            continue
        e = [x/boy for x in e]
        if kenar is None:
            kenar = e
        elif any(kenar[j]*e[(j+1) % 3] != kenar[(j+1) % 3]*e[j] for j in range(3)):
            return True
    return False


def _obj_dogrula(obj):
    """Kayıt başına O(1) okuma, kompakt double position tablosu, negatif index o ana göre çözülür."""
    v = array('d')
    uv = normal = yuz = 0
    libraries, kullanilan = [], set()
    with obj.open(encoding='utf-8', errors='surrogateescape') as fp:
        for no, satir in enumerate(_mantiksal(fp), 1):
            if no % 4096 == 0:
                surecler.iptal_kontrol()
            temiz = satir.split('#', 1)[0].strip()
            p = temiz.split()
            if not p:
                continue
            if p[0] in ('v', 'vt', 'vn'):
                try:
                    sayilar = [float(x) for x in p[1:]]
                except ValueError as exc:
                    raise RuntimeError('OBJ koordinatı sayı değil') from exc
                uygun = (len(sayilar) in (3, 4, 6, 7) if p[0] == 'v'
                         else 1 <= len(sayilar) <= 3 if p[0] == 'vt'
                         else len(sayilar) == 3)
                if not uygun or not all(map(math.isfinite, sayilar)):
                    raise RuntimeError('OBJ koordinatı eksik veya sonlu değil')
                if p[0] == 'v':
                    if len(sayilar) == 4 and sayilar[3] != 1:
                        raise RuntimeError('OBJ homojen konumu bu köprüde desteklenmiyor')
                    v.extend(sayilar[:3])
                elif p[0] == 'vt':
                    uv += 1
                else:
                    normal += 1
            elif p[0] == 'f':
                if len(p) < 4:
                    raise RuntimeError('OBJ yüzü en az üç köşe içermeli')
                ids = []
                bos_uv = all(token.count('/') == 1 and token.endswith('/') for token in p[1:])
                for token in p[1:]:
                    q = token.split('/')
                    if len(q) > 3 or not q[0] or (len(q) > 1 and not q[-1] and not bos_uv):
                        raise RuntimeError('OBJ yüz köşesi bozuk')
                    ids.append(_indis(q[0], len(v)//3))
                    if len(q) > 1 and q[1]:
                        _indis(q[1], uv)
                    if len(q) == 3:
                        _indis(q[2], normal)
                if len(set(ids)) != len(ids) or not _yuz_alani_var(v, ids):
                    raise RuntimeError('OBJ yüzü yinelenmiş veya sıfır alanlı')
                yuz += 1
            elif p[0] == 'mtllib':
                adlar = _kelimeler(temiz[len('mtllib'):].strip())
                if not adlar:
                    raise RuntimeError('OBJ malzeme başvurusu boş')
                libraries.extend(adlar)
            elif p[0] == 'usemtl':
                if len(p) < 2:
                    raise RuntimeError('OBJ malzeme adı boş')
                kullanilan.add(' '.join(p[1:]))
    if not yuz:
        raise RuntimeError('Assimp çıktısı geçerli yüz içermiyor')
    return list(dict.fromkeys(libraries)), kullanilan


def _kopyala_ozet(kaynak, hedef):
    h = hashlib.sha256()
    with kaynak.open('rb') as gir, hedef.open('wb') as cik:
        while parca := gir.read(1024*1024):
            surecler.iptal_kontrol()
            h.update(parca)
            cik.write(parca)
    return h.hexdigest()


def _map_coz(s):
    """Bilinen MTL option'larını koru, belirsiz syntax'ta tahmin yürütme."""
    p = _kelimeler(s)
    i = 1
    while i < len(p) and p[i].startswith('-'):
        secenek = p[i]
        i += 1
        if secenek in ('-s', '-o', '-t'):
            ilk = i
            while i < len(p) and i-ilk < 3:
                try:
                    if not math.isfinite(float(p[i])):
                        break
                except ValueError:
                    break
                i += 1
            if i == ilk:
                return None
        elif secenek in _TEK or secenek == '-mm':
            i += 2 if secenek == '-mm' else 1
        else:
            return None
    if i >= len(p) or any(any(c.isspace() for c in x) for x in p[:i]):
        return None
    return ' '.join(p[:i]), ' '.join(p[i:])


def _mtl_hazirla(kaynak, hedef, assets, kaynak_dizini):
    adlar, uyari = set(), 0
    with kaynak.open(encoding='utf-8', errors='surrogateescape') as gir, hedef.open(
            'w', encoding='utf-8', errors='surrogateescape', newline='\n') as cik:
        for no, s in enumerate(_mantiksal(gir), 1):
            if no % 4096 == 0:
                surecler.iptal_kontrol()
            p = s.split()
            if p and p[0] == 'newmtl':
                if len(p) < 2:
                    raise RuntimeError('MTL malzeme adı boş')
                adlar.add(' '.join(p[1:]))
            if p and (p[0].startswith('map_') or p[0] in _MAP):
                try:
                    cozum = _map_coz(s)
                except ValueError:
                    cozum = None
                doku = None
                if cozum:
                    for base in (kaynak.parent, kaynak_dizini):
                        aday = _yerel_yol(base, cozum[1], _KOK)
                        if aday is not None and aday.is_file():
                            doku = aday
                            break
                if doku is not None:
                    gecici = assets / 'texture.tmp'
                    imza = _kopyala_ozet(doku, gecici)
                    son = doku.suffix.lower()
                    if len(son) > 10 or not son[1:].isalnum():
                        son = '.bin'
                    yeni = 'texture_' + imza + son
                    os.replace(gecici, assets / yeni)
                    s = cozum[0] + ' ' + yeni
                else:
                    uyari += 1
                    cik.write('# assimp_kopru: unresolved texture, original reference preserved\n')
            cik.write(s + '\n')
    return adlar, uyari


def _varlik_imzasi(dizin):
    h = hashlib.sha256()
    for p in sorted(dizin.iterdir()):
        if p.is_symlink() or not p.is_file():
            raise RuntimeError('Assimp varlık dizini beklenmeyen kayıt içeriyor')
        h.update(p.name.encode('utf-8') + b'\0')
        h.update(str(p.stat().st_size).encode('ascii') + b'\0')
        with p.open('rb') as fp:
            while parca := fp.read(1024*1024):
                surecler.iptal_kontrol()
                h.update(parca)
    return h.hexdigest()


def cevir(kaynak, hedef, timeout=120):
    """OBJ'yi ve content-addressed MTL/texture'ları teslim eder, hedef sadece başarıda değişir.

    Çözülemeyen texture MTL'de yorum satırı olarak kalır ve RuntimeWarning ile
    bildirilir, absolute ya da eski makineye ait texture path'leri okunmaz.
    Kaynaktaki MTL referansı korunur.
    """
    kaynak = Path(kaynak).absolute()
    hedef = Path(hedef).absolute()
    if not kaynak.is_file():
        raise RuntimeError('Assimp kaynak dosyası bulunamadı')
    if kaynak.resolve() == hedef.resolve() or (hedef.exists() and os.path.samefile(kaynak, hedef)):
        raise ValueError('Assimp kaynak ve hedef aynı dosya olamaz')
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('Assimp zaman aşımı pozitif ve sonlu olmalı')
    arac = shutil.which('assimp')
    if not arac:
        raise RuntimeError('assimp kurulu değil')
    hedef.parent.mkdir(parents=True, exist_ok=True)
    with surecler.islem_oturumu(), tempfile.TemporaryDirectory(prefix='.assimp_', dir=hedef.parent) as td:
        gecici = Path(td)
        obj = gecici / 'model.obj'
        r = surecler.calistir([arac, 'export', str(kaynak), str(obj), '-fobj'],
                              cwd=str(gecici), timeout=timeout)
        if r.returncode:
            raise RuntimeError('Assimp dönüşümü başarısız: ' + (r.stderr or r.stdout or '')[-800:])
        if obj.is_symlink() or not obj.is_file():
            raise RuntimeError('Assimp yeni OBJ çıktısı üretmedi')
        libraries, kullanilan = _obj_dogrula(obj)
        assets = gecici / 'assets'
        assets.mkdir()
        esleme, tanimlar, uyari = {}, set(), 0
        for i, ad in enumerate(libraries):
            mtl = _yerel_yol(gecici, ad, gecici)
            if mtl is None or not mtl.is_file():
                raise RuntimeError('Assimp MTL çıktısı eksik veya geçici dizinin dışında')
            yeni = 'material_%d.mtl' % i
            isimler, say = _mtl_hazirla(mtl, assets / yeni, assets, kaynak.parent)
            tanimlar.update(isimler)
            uyari += say
            esleme[ad] = yeni
        if kullanilan - tanimlar:
            raise RuntimeError('OBJ tanımsız MTL malzemesi kullanıyor')
        imza = _varlik_imzasi(assets)
        varlik = hedef.parent / ('assimp_assets_' + imza)
        teslim = gecici / 'teslim.obj'
        with obj.open(encoding='utf-8', errors='surrogateescape') as gir, teslim.open(
                'w', encoding='utf-8', errors='surrogateescape', newline='\n') as cik:
            for s in _mantiksal(gir):
                if s.lstrip().split(None, 1)[:1] == ['mtllib']:
                    for ad in _kelimeler(s.split('#', 1)[0].split(None, 1)[1]):
                        cik.write('mtllib ' + varlik.name + '/' + esleme[ad] + '\n')
                else:
                    cik.write(s + '\n')
            cik.flush()
            os.fsync(cik.fileno())
        if uyari:
            # warning'i error sayan çağıran da teslimden önce durmalı.
            warnings.warn('%d doku başvurusu çözülemedi, kaynak MTL satırları korundu' % uyari,
                          RuntimeWarning, stacklevel=2)
        with _TESLIM_KILIDI:
            surecler.iptal_kontrol()
            if libraries:
                if varlik.is_symlink():
                    raise RuntimeError('Assimp varlık hedefi sembolik bağlantı olamaz')
                if varlik.exists():
                    if not varlik.is_dir() or _varlik_imzasi(varlik) != imza:
                        raise RuntimeError('Assimp varlık hedefinin içeriği beklenenden farklı')
                else:
                    os.rename(assets, varlik)
            surecler.iptal_kontrol()
            os.replace(teslim, hedef)
    return str(hedef)
