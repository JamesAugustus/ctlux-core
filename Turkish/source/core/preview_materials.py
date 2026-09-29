# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Kaynak malzemelerin base color'larını toplar (CTScene aktarımı kullanıyor), dosyalara ve komutlara dokunmaz."""
from array import array
from collections import OrderedDict
import math
import os
import shlex
import struct
import threading

from core.file_safety import ic_yol
from core.render_cache import dosya_ozeti

_bilgiler = OrderedDict()
_kilit = threading.RLock()
_GRI = [0.55, 0.55, 0.55]


def _satirlar(yol):
    with open(yol, encoding='latin-1') as f:
        for ham in f:
            s = ham.rstrip('\r\n')
            while s.endswith('\\'):
                devam = next(f, '')
                if not devam:
                    break
                s = s[:-1] + ' ' + devam.rstrip('\r\n')
            yield s.strip()


class _ObjDurum:
    def __init__(self):
        self.grup = ''
        self.malzeme = 'white'
        self.acik_malzeme = False

    def guncelle(self, p):
        if p[0] in ('o', 'g'):
            self.grup = ' '.join(p[1:])
            if p[0] == 'g' and not self.acik_malzeme:
                self.malzeme = p[1] if len(p) > 1 else 'white'
        elif p[0] == 'usemtl' and len(p) > 1:
            self.malzeme = p[1]
            self.acik_malzeme = True
            self.grup = self.grup or ' '.join(p[1:])


def obj_bilgisi(yol, ozet):
    """Yüz sayıları ve mtllib, vertex tablosu tutmayan, content hash'li ilk tarama."""
    key = (os.path.abspath(yol), ozet)
    with _kilit:
        if key in _bilgiler:
            _bilgiler.move_to_end(key)
            return _bilgiler[key]
    durum = _ObjDurum()
    adetler, kutuphaneler = {}, []
    for s in _satirlar(yol):
        if not s or s[0] not in 'ogufm':
            continue
        p = s.split('#', 1)[0].split()
        if not p:
            continue
        durum.guncelle(p)
        if p[0] == 'f':
            adetler[durum.grup] = adetler.get(durum.grup, 0) + max(0, len(p)-3)
        elif p[0] == 'mtllib':
            kutuphaneler.append(s.split(None, 1)[1] if len(p) > 1 else '')
    bilgi = {'adetler': adetler, 'mtllib': kutuphaneler}
    with _kilit:
        _bilgiler[key] = bilgi
        while len(_bilgiler) > 64:
            _bilgiler.popitem(last=False)
    return bilgi


def obj_yuzleri(yol, uv_ile=False):
    """Tek kompakt vertex tablosundan (grup, malzeme, yüz) akışı."""
    v = array('d')
    uv = array('d')
    durum = _ObjDurum()
    for s in _satirlar(yol):
        if not s or s[0] not in 'voguf':
            continue
        p = s.split('#', 1)[0].split()
        if not p:
            continue
        durum.guncelle(p)
        if p[0] == 'v' and len(p) >= 4:
            v.extend(float(x) for x in p[1:4])
        elif p[0] == 'vt' and len(p) >= 2:
            try:
                uv.extend((float(p[1]), float(p[2]) if len(p) > 2 else 0.0))
            except ValueError:
                uv.extend((float('nan'), float('nan')))
        elif p[0] == 'f':
            pts, koordinatlar = [], []
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
                            koordinatlar.append(point if len(point) == 2 and all(math.isfinite(x) for x in point) else None)
                        except (IndexError, ValueError):
                            koordinatlar.append(None)
                except ValueError:
                    continue
            if len(pts) >= 2:
                yield (durum.grup, durum.malzeme, pts, koordinatlar) if uv_ile else (durum.grup, durum.malzeme, pts)


def rad_kayitlari(yol):
    """RAD'daki sayısal kayıtlar, ! komutları çalıştırılmaz."""
    def tokens():
        for s in _satirlar(yol):
            if s and not s.startswith(('!', '#')):
                yield from shlex.split(s, comments=True)
    it = tokens()
    try:
        while True:
            mod, tur, ad = next(it), next(it), next(it)
            if tur == 'alias':
                yield mod, tur, ad, [next(it)]
                continue
            for _ in range(2):
                for j in range(int(next(it))):
                    next(it)
            degerler = [float(next(it)) for _ in range(int(next(it)))]
            yield mod, tur, ad, degerler
    except (StopIteration, ValueError):
        return


def rad_bilgisi(yol, ozet=None):
    key = ('rad', os.path.abspath(yol), ozet) if ozet is not None else None
    if key:
        with _kilit:
            if key in _bilgiler:
                _bilgiler.move_to_end(key)
                return _bilgiler[key]
    from core.radiance_preview import DESTEKLENEN_TURLER, primitif_ucgen_sayisi
    adetler, malzemeler, uyarilar = {}, {}, []
    for mod, tur, ad, d in rad_kayitlari(yol):
        if tur in DESTEKLENEN_TURLER:
            try:
                adetler[mod] = adetler.get(mod, 0) + primitif_ucgen_sayisi(tur, d)
            except ValueError:
                uyarilar.append('Bozuk RAD yüzeyi önizlemede atlandı: ' + tur)
        elif tur in ('plastic', 'metal', 'trans', 'glass'):
            malzemeler[ad] = {'renk': _renk(d), 'tur': tur}
        elif tur == 'alias' and d[0] in malzemeler:
            malzemeler[ad] = malzemeler[d[0]]
    bilgi = {'adetler': adetler, 'malzemeler': malzemeler, 'uyarilar': list(dict.fromkeys(uyarilar))}
    if key:
        with _kilit:
            _bilgiler[key] = bilgi
            while len(_bilgiler) > 64:
                _bilgiler.popitem(last=False)
    return bilgi


def rad_yuzleri(yol):
    from core.radiance_preview import DESTEKLENEN_TURLER, primitif_yuzleri
    for mod, tur, ad, d in rad_kayitlari(yol):
        if tur in DESTEKLENEN_TURLER:
            try:
                yield from ((mod, mod, list(yuz)) for yuz in primitif_yuzleri(tur, d))
            except ValueError:
                continue  # rad_bilgisi bu kayıt için açık warning'i zaten verdi.


def _renk(d):
    if len(d) < 3 or not all(math.isfinite(x) for x in d[:3]):
        return None
    return [min(1.0, max(0.0, x)) for x in d[:3]]


def _yan_yol(proje_dir, ana, ad):
    ad = ad.replace('\\', '/')
    # eski makineye ait absolute path takip edilmez, aynı klasördeki kopyaya bakılır.
    if ad.startswith('/') or ':' in ad:
        ad = ad.rsplit('/', 1)[-1]
    rel = os.path.relpath(os.path.normpath(os.path.join(os.path.dirname(ana), ad)), proje_dir)
    return ic_yol(proje_dir, rel)


def _mtl_oku(yol):
    tablo, ad = {}, None
    for s in _satirlar(yol):
        p = s.split('#', 1)[0].split()
        if not p:
            continue
        if p[0].lower() == 'newmtl':
            ad = ' '.join(p[1:])
            tablo[ad] = {'renk': None, 'doku': None}
        elif ad and p[0].lower() == 'kd':
            try:
                tablo[ad]['renk'] = _renk([float(x) for x in p[1:4]])
            except ValueError:
                pass
        elif ad and p[0].lower() == 'map_kd':
            tablo[ad]['doku'] = s.split(None, 1)[1] if len(p) > 1 else ''
    return tablo


def _goruntu_boyutu(yol):
    """JPEG/PNG header'ını oku, görüntünün tamamını açma, yan dosya üretme."""
    with open(yol, 'rb') as f:
        head = f.read(24)
        if len(head) == 24 and head[:8] == b'\x89PNG\r\n\x1a\n' and head[12:16] == b'IHDR':
            return struct.unpack('>II', head[16:24])
        if head[:2] != b'\xff\xd8':
            raise ValueError('Canlı doku yalnız JPEG/PNG destekliyor')
        f.seek(2)
        while True:
            lead = f.read(1)
            if not lead:
                break
            if lead != b'\xff':
                raise ValueError('JPEG başlığı bozuk')
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
    raise ValueError('JPEG/PNG boyutu okunamadı')


class Malzemeler:
    def __init__(self, proje_dir, proje, girdiler):
        self.tablo, self.uyarilar, self.imza = [], [], []
        self._ids, self._kaynaklar, self._cozulen = {}, {}, {}
        rad = {}
        try:
            mat = ic_yol(proje_dir, proje.get('malzeme', 'malzeme/materials.rad'))
            ozet = dosya_ozeti(mat) if os.path.isfile(mat) else None
            self.imza.append((mat, ozet))
            if ozet:
                rad = rad_bilgisi(mat, ozet)['malzemeler']
                if any(s.startswith('!') for s in _satirlar(mat)):
                    self.uyar('Dinamik Radiance malzeme komutları GPU önizlemesinde çalıştırılmıyor.')
        except (OSError, ValueError):
            self.uyar('Radiance malzeme dosyası okunamadı, kaynak temel renkleri kullanılıyor.')
        for gi, g, gp, obj, bilgi in girdiler:
            yerel = {} if obj else bilgi['malzemeler']
            mtl = {}
            for satir in bilgi.get('mtllib', []):
                if not satir:
                    continue
                try:
                    tek = _yan_yol(proje_dir, gp, satir)
                    adlar = [satir] if os.path.isfile(tek) else shlex.split(satir, posix=False)
                    for ad in adlar:
                        mp = _yan_yol(proje_dir, gp, ad.strip('"\''))
                        ozet = dosya_ozeti(mp) if os.path.isfile(mp) else None
                        self.imza.append((mp, ozet))
                        if not ozet:
                            self.uyar('MTL malzeme dosyası bulunamadı: ' + os.path.basename(mp))
                            continue
                        yeni = _mtl_oku(mp)
                        # texture eksik ya da sınırı aşmış olsa da kaynaktaki Kd geçerli.
                        mtl.update(yeni)
                        for m in yeni.values():
                            if m.get('doku'):
                                try:
                                    if m['doku'].startswith('-'):
                                        raise ValueError('MTL doku seçenekleri canlı görünümde henüz desteklenmiyor')
                                    dp = _yan_yol(proje_dir, mp, m['doku'].strip('"\''))
                                    if not os.path.isfile(dp):
                                        self.imza.append((dp, None))
                                        self.uyar('Doku dosyası bulunamadı: ' + os.path.basename(dp))
                                        continue
                                    if os.path.getsize(dp) > 32*1024*1024:
                                        raise ValueError('Canlı doku dosyası 32 MiB sınırını aşıyor')
                                    digest = dosya_ozeti(dp)
                                    self.imza.append((dp, digest))
                                    w, h = _goruntu_boyutu(dp)
                                    if min(w, h) < 1 or max(w, h) > 8192 or w*h > 32*1024*1024:
                                        raise ValueError('Canlı doku çözünürlüğü sınırı aşıyor')
                                    studio = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                                    rel = os.path.relpath(dp, studio).replace(os.sep, '/')
                                    ic_yol(studio, rel)
                                    m['doku_bilgisi'] = {'yol': rel, 'imza': digest, 'boyut': [w, h]}
                                except (OSError, ValueError) as exc:
                                    self.uyar('Doku kullanılamıyor, temel renk korundu: ' + str(exc))
                except (OSError, ValueError):
                    self.uyar('MTL başvurusu proje içinde okunamadı.')
            self._kaynaklar[gi] = (rad, yerel, mtl)

    def uyar(self, metin):
        if metin not in self.uyarilar:
            self.uyarilar.append(metin)

    def id(self, gi, ad):
        if (gi, ad) in self._cozulen:
            return self._cozulen[(gi, ad)]
        rad, yerel, mtl = self._kaynaklar[gi]
        kaynak, renk, tur = 'varsayilan', None, None
        for tablo, kok in ((rad, 'rad'), (yerel, 'rad'), (mtl, 'mtl')):
            if ad in tablo:
                renk, tur = tablo[ad].get('renk'), tablo[ad].get('tur')
                if renk is not None:
                    kaynak = kok
                break
        if renk is None:
            renk = _GRI
            self.uyar("'%s' malzemesinin rengi bulunamadı, nötr gri gösteriliyor." % ad)
        if tur in ('glass', 'trans'):
            self.uyar('Cam ve geçirgen malzemeler GPU önizlemesinde yalnız temel rengiyle gösteriliyor.')
        # kullanıcının RAD malzeme rengi önde kalır, OBJ'nin UV texture'ı yanında gelir.
        doku = mtl.get(ad, {}).get('doku_bilgisi')
        key = (ad, tuple(renk), kaynak, (doku['yol'], doku['imza']) if doku else None)
        if key not in self._ids:
            self._ids[key] = len(self.tablo)
            kayit = {'ad': ad, 'renk': list(renk), 'kaynak': kaynak}
            if doku:
                kayit['doku'] = doku
            self.tablo.append(kayit)
        self._cozulen[(gi, ad)] = self._ids[key]
        return self._ids[key]
