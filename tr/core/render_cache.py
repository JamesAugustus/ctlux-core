# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Radiance girdilerinin content hash'leri, tamamlanmış, değişmez build cache'i."""
from collections import OrderedDict
import hashlib
import json
import os
import threading

from core.file_safety import ic_yol

_ozetler = OrderedDict()
_komutlar = OrderedDict()
_kilit = threading.RLock()
derleme_kilidi = threading.RLock()


def dosya_ozeti(yol):
    """Content SHA-256, stat aynıysa process içinde büyük dosya tekrar okunmaz.

    ctime, mtime, inode ve boyut birlikte takip edilir. Kaynak değişirken
    üretilmiş hash cache'e alınmaz, sonraki çağrı dosyayı baştan okur.
    """
    yol = os.path.abspath(yol)
    st = os.stat(yol)
    anahtar = (yol, st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
    with _kilit:
        if anahtar in _ozetler:
            _ozetler.move_to_end(anahtar)
            return _ozetler[anahtar]
    with open(yol, 'rb') as f:
        h = hashlib.file_digest(f, 'sha256').hexdigest() if hasattr(hashlib, 'file_digest') else _akis_ozeti(f)
    son = os.stat(yol)
    if (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns) != (son.st_ino, son.st_size, son.st_mtime_ns, son.st_ctime_ns):
        raise RuntimeError('Render girdisi okunurken değişti: ' + os.path.basename(yol))
    with _kilit:
        _ozetler[anahtar] = h
        while len(_ozetler) > 4096:
            _ozetler.popitem(last=False)
    return h


def _akis_ozeti(f):
    h = hashlib.sha256()
    for b in iter(lambda: f.read(1 << 20), b''):
        h.update(b)
    return h.hexdigest()


def nesne_ozeti(veri):
    return hashlib.sha256(json.dumps(veri, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False, separators=(',', ':')).encode()).hexdigest()


def dinamik_rad_var(kaynaklar):
    """Kullanıcının RAD'ındaki shell komutunun bütün bağımlılıkları bilinemez.

    Böyle sahneler her seferinde yeniden build edilir. Bizim ürettiğimiz
    deterministik xform/gensky satırları bu listede değil, onların girdileri
    ayrıca takip edilir.
    """
    for yol, ozet in kaynaklar:
        if not yol.lower().endswith('.rad'):
            continue
        key = (yol, ozet)
        with _kilit:
            biliniyor = key in _komutlar
            var = _komutlar.get(key)
        if not biliniyor:
            with open(yol, 'rb') as f:
                # Radiance ! komutunu satır ortasında da çalıştırır. Her ! baytı dinamik sayılır.
                var = any(b'!' in chunk for chunk in iter(lambda: f.read(1 << 20), b''))
            with _kilit:
                _komutlar[key] = var
                while len(_komutlar) > 4096:
                    _komutlar.popitem(last=False)
        if var:
            return True
    return False


def kaynaklar(proje_dir, proje, kutuphane=None):
    """Proje girdileri + fotometri/texture yan dosyaları, render çıktıları hariç.

    Kapsamı biraz geniş tutmak, eksik bağımlılık yüzünden eski light hesabını
    kullanmaktan daha güvenli. Yedek/çıktı ve octree klasörleri taranmaz.
    """
    yollar = set()
    for alt in ('model', 'malzeme', 'isik', 'doku', 'gok', '_cache/ies', '_cache/doku'):
        klasor = ic_yol(proje_dir, alt)
        if os.path.isdir(klasor):
            for kok, dizinler, adlar in os.walk(klasor, followlinks=False):
                dizinler[:] = sorted(d for d in dizinler if not os.path.islink(os.path.join(kok, d)))
                for ad in adlar:
                    p = os.path.join(kok, ad)
                    if not ad.endswith(('.tmp', '.yeni')) and os.path.isfile(p):
                        # proje dışına çıkan link'ler girdi sayılmaz.
                        p = ic_yol(proje_dir, os.path.relpath(p, proje_dir))
                        yollar.add(p)
    for g in proje.get('geometri', []):
        p = ic_yol(proje_dir, g['dosya'])
        if os.path.isfile(p):
            yollar.add(p)
        if os.path.isfile(p + '.tam.obj'):
            yollar.add(p + '.tam.obj')
    for rel in proje.get('isiklar', []):
        p = ic_yol(proje_dir, rel)
        if os.path.isfile(p):
            yollar.add(p)
    mat = ic_yol(proje_dir, proje.get('malzeme', 'malzeme/materials.rad'))
    if os.path.isfile(mat):
        yollar.add(mat)
    for a in proje.get('armaturler', []):
        for alan in ('ies', 'urun_rad'):
            rel = a.get(alan)
            if not rel:
                continue
            try:
                p = ic_yol(proje_dir, rel)
            except ValueError:
                continue
            if os.path.isfile(p):
                yollar.add(p)
                # ürün .rad dosyasının yanındaki .dat/.cal değişiklikleri de takip edilir.
                if alan == 'urun_rad':
                    for uz in ('.dat', '.cal'):
                        yan = os.path.splitext(p)[0] + uz
                        if os.path.isfile(yan):
                            yollar.add(yan)
            elif kutuphane and alan == 'ies':
                from core.photometry import ies_kaynagi
                p = ies_kaynagi(proje_dir, a, kutuphane)
                if p:
                    yollar.add(p)
    return [(os.path.abspath(p), dosya_ozeti(p)) for p in sorted(yollar)]


def gecerli_cikti(yol, kayit_yolu):
    """Sadece başarı kaydı yazılmış ve boyutu doğru çıktı kullanılabilir."""
    try:
        with open(kayit_yolu) as f:
            kayit = json.load(f)
        return (os.path.getsize(yol) > 64 and kayit['sha256'] == dosya_ozeti(yol))
    except (OSError, ValueError, KeyError, TypeError):
        return False


def gecerli_sahne(proje_dir, oct_yolu, kayit_yolu):
    """Sahnenin kendisi ve referans verdiği türetilmiş geometri birlikte doğrulanır."""
    try:
        if not gecerli_cikti(oct_yolu, kayit_yolu):
            return False
        with open(kayit_yolu) as f:
            bagimliliklar = json.load(f)['turetilmis']
        if not isinstance(bagimliliklar, list):
            return False
        for kayit in bagimliliklar:
            if not isinstance(kayit, dict) or not isinstance(kayit.get('yol'), str) or not isinstance(kayit.get('sha256'), str):
                return False
            yol = ic_yol(proje_dir, kayit['yol'])
            if not os.path.isfile(yol) or dosya_ozeti(yol) != kayit['sha256']:
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        return False
