# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Sayısal Radiance yüzeylerinden sınırlı sayıda, kaynak biriminde mesh yüzü üretir.

Analitik yüzey sadece mesh görünümü için polygon'a çevrilir, Radiance kaynağı ve
render hesabı değişmez. Silindir/koni sadece yan yüzeydir, kendiliğinden kapak eklenmez.
Komut çalıştırmak, path ya da malzeme çözmek bu modülün işi değil.
"""
import math


CEVRE_BOLUM = 24
KURE_KATMAN = 12
DESTEKLENEN_TURLER = frozenset((
    'polygon', 'sphere', 'bubble', 'cylinder', 'tube', 'cone', 'cup', 'ring'))


def _cross(a, b):
    return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])


def _normal(v):
    uzunluk = math.hypot(*v)
    if not math.isfinite(uzunluk) or uzunluk == 0:
        raise ValueError('RAD yüzey ekseni/normal vektörü sıfır veya sonlu değil')
    return tuple(x/uzunluk for x in v)


def _taban(eksen):
    w = _normal(eksen)
    # en az paralel koordinat ekseni: çok küçük cross product'tan kaçınır.
    en_az = min(range(3), key=lambda j: abs(w[j]))
    ref = tuple(float(i == en_az) for i in range(3))
    u = _normal(_cross(w, ref))
    return u, _cross(w, u)


def _degerler(tur, degerler):
    if tur not in DESTEKLENEN_TURLER:
        return None
    try:
        d = tuple(float(x) for x in degerler)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError('RAD yüzey parametreleri sayısal değil') from error
    if not all(math.isfinite(x) for x in d):
        raise ValueError('RAD yüzey parametreleri sonlu değil')
    n = len(d)
    beklenen = {'sphere': 4, 'bubble': 4, 'cylinder': 7, 'tube': 7,
                'cone': 8, 'cup': 8, 'ring': 8}
    if (tur == 'polygon' and (n < 9 or n % 3)) or (tur != 'polygon' and n != beklenen[tur]):
        raise ValueError('RAD %s parametre sayısı geçersiz' % tur)
    if tur in ('sphere', 'bubble'):
        if d[3] <= 0:
            raise ValueError('RAD küre yarıçapı pozitif olmalı')
    elif tur in ('cylinder', 'tube', 'cone', 'cup'):
        _normal(tuple(d[i+3]-d[i] for i in range(3)))
        if tur in ('cylinder', 'tube'):
            if d[6] <= 0:
                raise ValueError('RAD silindir yarıçapı pozitif olmalı')
        elif min(d[6:8]) < 0 or max(d[6:8]) <= 0:
            raise ValueError('RAD koni yarıçapları negatif olamaz, ikisi birden sıfır olamaz')
    elif tur == 'ring':
        _normal(d[3:6])
        if not 0 <= d[6] < d[7]:
            raise ValueError('RAD halka yarıçapları 0 <= iç < dış koşulunu sağlamalı')
    if tur != 'polygon':
        radius = d[3] if tur in ('sphere', 'bubble') else max(d[6:])
        merkezler = (d[:3], d[3:6]) if tur in ('cylinder', 'tube', 'cone', 'cup') else (d[:3],)
        if not all(math.isfinite(x-radius) and math.isfinite(x+radius) for c in merkezler for x in c):
            raise ValueError('RAD yüzey sınırı sonlu aralığı aştı')
    return d


def primitif_ucgen_sayisi(tur, degerler):
    """Yüz akışındaki fan triangulation ile aynı sayı, desteklenmeyen tür için sıfır."""
    d = _degerler(tur, degerler)
    if d is None:
        return 0
    if tur == 'polygon':
        return len(d)//3-2
    if tur in ('sphere', 'bubble'):
        return 2*CEVRE_BOLUM*(KURE_KATMAN-1)
    if tur in ('cone', 'cup') and (d[6] == 0 or d[7] == 0):
        return CEVRE_BOLUM
    if tur == 'ring' and d[6] == 0:
        return CEVRE_BOLUM
    return 2*CEVRE_BOLUM


def primitif_yuzleri(tur, degerler):
    """Dış normal yönünde 3/4 köşeli yüzler, bubble/tube/cup içe bakar.

    Polygon köşeleri kaynaktaki sırayla korunur. Eğri başına en çok 528 üçgen
    çıkar, parametreler sample sayısını ya da ayrılan belleği büyütmez.
    Bozuk ama desteklenen kayıt ValueError verir, bilinmeyen tür yüz üretmez.
    """
    d = _degerler(tur, degerler)
    if d is None:
        return
    if tur == 'polygon':
        yield tuple(d[i:i+3] for i in range(0, len(d), 3))
        return
    ters = tur in ('bubble', 'tube', 'cup')
    daire = tuple((math.cos(2*math.pi*i/CEVRE_BOLUM),
                   math.sin(2*math.pi*i/CEVRE_BOLUM)) for i in range(CEVRE_BOLUM))

    def yuz(points):
        if not all(math.isfinite(x) for p in points for x in p):
            raise ValueError('RAD yüzey koordinatı sonlu aralığı aştı')
        return tuple(reversed(points)) if ters else tuple(points)

    if tur in ('sphere', 'bubble'):
        c, radius = d[:3], d[3]
        alt = (c[0], c[1], c[2]-radius)
        ust = (c[0], c[1], c[2]+radius)
        once = None
        for j in range(1, KURE_KATMAN):
            enlem = -math.pi/2 + math.pi*j/KURE_KATMAN
            yatay, z = radius*math.cos(enlem), c[2]+radius*math.sin(enlem)
            halka = tuple((c[0]+yatay*x, c[1]+yatay*y, z) for x, y in daire)
            for i in range(CEVRE_BOLUM):
                k = (i+1) % CEVRE_BOLUM
                if once is None:
                    yield yuz((alt, halka[k], halka[i]))
                else:
                    yield yuz((once[i], once[k], halka[k], halka[i]))
            once = halka
        for i in range(CEVRE_BOLUM):
            yield yuz((once[i], once[(i+1) % CEVRE_BOLUM], ust))
        return

    c = d[:3]
    eksen = d[3:6] if tur == 'ring' else tuple(d[i+3]-d[i] for i in range(3))
    u, v = _taban(eksen)

    def halka(center, radius):
        return tuple(tuple(center[a]+radius*(u[a]*x+v[a]*y) for a in range(3)) for x, y in daire)

    if tur == 'ring':
        ic, dis = halka(c, d[6]), halka(c, d[7])
        for i in range(CEVRE_BOLUM):
            k = (i+1) % CEVRE_BOLUM
            yield yuz((c, dis[i], dis[k]) if d[6] == 0 else (ic[i], dis[i], dis[k], ic[k]))
        return

    ust_merkez = d[3:6]
    alt_r, ust_r = d[6], d[6] if tur in ('cylinder', 'tube') else d[7]
    alt, ust = halka(c, alt_r), halka(ust_merkez, ust_r)
    for i in range(CEVRE_BOLUM):
        k = (i+1) % CEVRE_BOLUM
        if alt_r == 0:
            yield yuz((c, ust[k], ust[i]))
        elif ust_r == 0:
            yield yuz((alt[i], alt[k], ust_merkez))
        else:
            yield yuz((alt[i], alt[k], ust[k], ust[i]))
