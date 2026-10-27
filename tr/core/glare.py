# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Yaşa göre kamaşma: evalglare kaynak listesinden örtü parlaklığı (CIE 146, Stiles-Holladay).

    Lv = toplam(10 * E_i / teta_i^2) * (1 + (yas / 70)^4)
    E_i = L_i * omega_i * cos(teta_i)

Formül 1 dereceden büyük, 30 dereceden küçük açılarda geçerlidir.
Dışarıda kalan kaynak toplama girmez
ama sayılır. Kaynağın armatür ya da ışığı yansıtan yüzey olması fark etmez.
Buradaki işlevler Radiance çağırmaz, komut akışı `core.__main__.kamasma` içindedir.
"""
import math
import re

ALT_ACI, UST_ACI = 1.0, 30.0
GENEL_FORMUL = 'Lv = toplam(E * (10/teta^3 + (5/teta^2 + 0.1*p/teta)*(1+(yas/62.5)^4) + 0.0025*p))'
YASLAR = (20, 40, 50, 60, 70, 80)
FORMUL = 'Lv = toplam(10 * E / teta^2) * (1 + (yas / 70)^4), E = L * omega * cos(teta)'
SIMGELER = ('Simgeler: Lv örtü parlaklığı (cd/m²), E gözdeki aydınlık düzeyi (lx), L parlaklık (cd/m²), omega katı açı (sr), '
            'teta bakış yönü ile kaynak yönü arasındaki açı (derece), yas yıl cinsinden yaş')
GENEL_SIMGELER = ('Simgeler: Lv örtü parlaklığı (cd/m²), E gözdeki aydınlık düzeyi (lx), teta bakış yönü ile kaynak yönü arasındaki açı (derece), '
                  'yas yıl cinsinden yaş, p göz rengi katsayısı (birimsiz, 0 ile 1.2 arası)')
KAYNAK_SUTUNLARI = ('L_s', 'Omega_s', 'xdir', 'ydir', 'zdir')
OZET_ZORUNLU = ('dgp', 'ugr')
SINIRLAR = (
    'Malzemeler mat varsayılır, parlak yüzeyden aynasal yansıma bu sahnede oluşmaz.',
    'EVO kaynaklı armatürlerde ışık çıkış açıklığının boyutu yer tutucudur, armatürün kendi '
    'parlaklığı ve ondan türetilen DGP, UGR ve örtü parlaklığı bu yüzden güvenilir değildir.',
    'Formül 1 < açı < 30 derece koşulunda geçerlidir. Dışarıdaki kaynaklar toplama girmez.',
)


def yas_carpani(yas):
    """CIE 146 yaş çarpanı: 1 + (yaş / 70)^4."""
    if isinstance(yas, bool) or not (isinstance(yas, (int, float)) and 0 < yas <= 120):
        raise ValueError('Yaş 0 ile 120 arasında olmalı')
    return 1 + (yas / 70.0) ** 4


def _birim(v):
    boy = math.sqrt(sum(x * x for x in v))
    if not math.isfinite(boy) or boy < 1e-12:
        raise ValueError('Yön vektörü sıfır ya da geçersiz')
    return [x / boy for x in v]


def evalglare_coz(metin):
    """`evalglare -d` stdout'u -> (kaynaklar, ozet, ozet_ham).

    Sütunlar başlık satırındaki adlardan bulunur. Kesik satır, başlıktaki sayıyla
    tutmayan kaynak listesi, eksik özet ya da geçersiz sayı ValueError verir.
    """
    adlar, beklenen, satirlar, ozet_ham = None, None, [], None
    for satir in metin.splitlines():
        parca = satir.split()
        if not parca:
            continue
        if len(parca) > 2 and parca[0].isdigit() and parca[1:3] == ['No', 'pixels']:
            if adlar is not None:
                raise ValueError('evalglare çıktısında iki kaynak başlığı var')
            beklenen, adlar = int(parca[0]), parca[1:]
            continue
        if satir.lstrip().startswith('dgp,') and ':' in satir:
            etiket, deger = satir.split(':', 1)
            etiketler, degerler = [x.strip() for x in etiket.split(',')], deger.split()
            if len(etiketler) != len(degerler):
                raise ValueError('evalglare özet satırı kesik')
            ozet_ham = dict(zip(etiketler, degerler))
            continue
        try:
            sayilar = [float(x) for x in parca]
        except ValueError:
            continue
        if adlar is None:
            raise ValueError('evalglare kaynak satırı başlıktan önce geldi')
        # Kaynak sayısı sıfırken yer tutucu arka plan değerleri sıfır olmayabilir.
        # Ancak No sütununda gerçek kaynak numarası varsa başlıkla çelişir.
        if beklenen == 0:
            if sayilar[0] != 0:
                raise ValueError('evalglare kaynak sayısı tutmuyor: başlıkta 0, listede kaynak var')
            continue
        if len(sayilar) != len(adlar):
            raise ValueError('evalglare kaynak satırı kesik: %d sütun, %d bekleniyordu'
                             % (len(sayilar), len(adlar)))
        satirlar.append(sayilar)
    if adlar is None:
        raise ValueError('evalglare kaynak başlığı yok')
    eksik = [ad for ad in ('No', *KAYNAK_SUTUNLARI) if ad not in adlar]
    if eksik:
        raise ValueError('evalglare kaynak sütunu yok: ' + ', '.join(eksik))
    if beklenen != len(satirlar):
        raise ValueError('evalglare kaynak sayısı tutmuyor: başlıkta %d, listede %d'
                         % (beklenen, len(satirlar)))
    if ozet_ham is None:
        raise ValueError('evalglare özet satırı yok')
    try:
        ozet = {k: float(v) for k, v in ozet_ham.items()}
    except ValueError as error:
        raise ValueError('evalglare özet değeri sayı değil') from error
    if any(k not in ozet or not math.isfinite(ozet[k]) for k in OZET_ZORUNLU):
        raise ValueError('evalglare DGP ya da UGR değeri eksik veya geçersiz')
    if not 0 <= ozet['dgp'] <= 1:
        raise ValueError('evalglare DGP değeri 0 ile 1 arasında değil')
    # ugp gibi isteğe bağlı alanlar karanlık görünümde inf olabilir. Ham metin kalır, değer None saklanır.
    ozet = {k: v if math.isfinite(v) else None for k, v in ozet.items()}
    # Arka plan parlaklığı 0 iken evalglare UGR için -99 yazar. UGR bu değere böler, bu yüzden tanımsızdır.
    if ozet['ugr'] == -99 or ozet.get('lum_backg') == 0:
        ozet['ugr'] = None
    kaynaklar = []
    for sayilar in satirlar:
        k = dict(zip(adlar, sayilar))
        if not all(math.isfinite(x) for x in sayilar) or k['L_s'] < 0 or k['Omega_s'] <= 0:
            raise ValueError('evalglare kaynak %g: parlaklık ya da katı açı geçersiz' % k['No'])
        kaynaklar.append({'no': int(k['No']), 'parlaklik': k['L_s'], 'kati_aci': k['Omega_s'],
                          'yon': _birim([k['xdir'], k['ydir'], k['zdir']])})
    return kaynaklar, ozet, ozet_ham


def ortu_parlakligi(kaynaklar, vd):
    """Her kaynağa açı, gözdeki aydınlık ve katkı ekler, yaş çarpanı öncesi toplamı döner.

    Açı, bakış yönü ile kaynak yönü arasındadır. Aralık dışındaki kaynağın katkısı
    None olur ve toplama girmez.
    """
    bakis = _birim(vd)
    toplam = 0.0
    for k in kaynaklar:
        kos = max(-1.0, min(1.0, sum(a * b for a, b in zip(bakis, _birim(k['yon'])))))
        teta = math.degrees(math.acos(kos))
        k['aci'] = teta
        k['aydinlik'] = k['parlaklik'] * k['kati_aci'] * kos
        # CIE 146 uçları dışlar. acos yuvarlaması uçları içeri sokmasın.
        k['aralikta'] = ALT_ACI + 1e-9 < teta < UST_ACI - 1e-9
        k['katki'] = 10 * k['aydinlik'] / teta ** 2 if k['aralikta'] else None
        if k['aralikta']:
            toplam += k['katki']
    return toplam


def yas_tablosu(toplam, yas):
    """20, 40, 50, 60, 70, 80 ve istenen yaş için çarpan ve örtü parlaklığı."""
    yaslar = sorted(set(YASLAR) | {yas})
    return [{'yas': y, 'carpan': yas_carpani(y), 'ortu_parlakligi': toplam * yas_carpani(y)}
            for y in yaslar]


def goz_rengi_kontrol(p):
    if isinstance(p, bool) or not isinstance(p, (int, float)) or not math.isfinite(p) or not 0 <= p <= 1.2:
        raise ValueError('Göz rengi katsayısı 0 ile 1.2 arasında sonlu sayı olmalı')
    return p


def genel_toplam(kaynaklar, yas, p=0.5):
    """CIE genel denklemi, açı derece, E göz düzleminde negatif olmayan lux.

    NIST pub_id=917534 denklem 4, NODD (Applied Optics 54, 1564), denklem 1.
    Geçerlilik açık aralık: 0.1 < açı < 100. Görüntü kapsamını genişletmez.
    """
    yas_carpani(yas)
    goz_rengi_kontrol(p)
    toplam = 0.0
    for k in kaynaklar:
        t, e = k['aci'], k['aydinlik']
        if not math.isfinite(t) or not math.isfinite(e):
            raise ValueError('Genel denklem için sonlu açı ve aydınlık gerekli')
        if 0.1 < t < 100:
            if e < 0:
                raise ValueError('Genel denklem için negatif göz aydınlığı kullanılamaz')
            toplam += e * (10 / t**3 + (5 / t**2 + 0.1*p / t) * (1 + (yas/62.5)**4) + 0.0025*p)
    return toplam


def cisim_adlari(rtrace_cikti, sayi, sahne):
    """`rtrace -oms` satırlarını CTScene adlarına çevirir.

    `yuzN` N'inci üçgendir, adı CTScene malzeme adıdır. `lN` ya da `lN.` ile
    başlayan ad N'inci ışıktır. Işın cisme çarpmazsa ya da ad çözülemezse
    ValueError verir, yerine ad uydurulmaz.
    """
    satirlar = rtrace_cikti.splitlines()
    if len(satirlar) != sayi:
        raise ValueError('Kaynak ve cisim eşlemesi eksik: %d kaynak, %d rtrace satırı'
                         % (sayi, len(satirlar)))
    mesh, isiklar = sahne.get('mesh') or {}, sahne.get('lights') or []
    sonuc = []
    for i, satir in enumerate(satirlar, 1):
        parca = satir.split('\t')
        radiance = parca[1].strip() if len(parca) > 1 else ''
        if radiance in ('', '*'):
            raise ValueError('Kaynak %d yönünde cisim bulunamadı' % i)
        yuz, isik = re.fullmatch(r'yuz(\d+)', radiance), re.match(r'l(\d+)(\.|$)', radiance)
        try:
            if yuz:
                n = int(yuz.group(1))
                malzeme = mesh['materials'][mesh['m'][mesh['f'][3 * n]]]
                ad = malzeme.get('name')
            elif isik:
                ad = isiklar[int(isik.group(1))].get('name')
            else:
                raise LookupError
        except (LookupError, TypeError):
            raise ValueError('Kaynak %d cismi sahnede çözülemedi: %s' % (i, radiance)) from None
        sonuc.append({'cisim': ad or radiance, 'radiance_adi': radiance})
    return sonuc


def _tablo(basliklar, satirlar):
    genislik = [max(len(r[i]) for r in [basliklar, *satirlar]) for i in range(len(basliklar))]
    return ['  '.join(h.ljust(genislik[i]) for i, h in enumerate(r)).rstrip()
            for r in [basliklar, *satirlar]]


def metin(sonuc):
    """KAMASMA.txt: özet, kaynak tablosu, yaş tablosu."""
    def vek(v):
        return ' '.join('%g' % round(x, 4) for x in v)
    b, e = sonuc['bakis'], sonuc['evalglare']
    text = ['CTLux kamaşma analizi', 'Sahne: ' + sonuc['sahne'], '', 'ÖZET',
            'Bakış noktası (m): ' + vek(b['vp']), 'Bakış yönü (yön vektörü, birimsiz): ' + vek(b['vd']),
            'Bakış kaynağı: ' + b['kaynak'],
            'Kaynak sayısı: %d' % sonuc['kaynak_sayisi'],
            'Aralık dışında kalan kaynak (1 < açı < 30 derece): %d' % sonuc['aralik_disi_sayisi'],
            'evalglare DGP (0 ile 1 arası, birimsiz): ' + e['ham']['dgp'], 'evalglare UGR (indis, birimsiz): ' + (e['ham']['ugr'] if e.get('deger', {}).get('ugr', 0) is not None else
                                'tanımsız, arka plan parlaklığı 0 (evalglare %s yazdı)' % e['ham']['ugr']),
            'Formül: ' + FORMUL, SIMGELER, '', 'KAYNAKLAR']
    satirlar = [[str(k['sira']), k['cisim'], k['radiance_adi'], '%.1f' % k['parlaklik'],
                 '%.6f' % k['kati_aci'], '%.2f' % k['aci'], '%.2f' % k['aydinlik'],
                 '-' if k['katki'] is None else '%.3f' % k['katki'],
                 'evet' if k['aralikta'] else 'hayır'] for k in sonuc['kaynaklar']]
    if satirlar:
        text += _tablo(['Sıra', 'Cisim', 'Radiance adı', 'L (cd/m²)', 'Katı açı (sr)', 'Açı (°)',
                        'E göz (lx)', 'Katkı (cd/m²)', 'Aralıkta'], satirlar)
        text.append('Katkı yaş çarpanı öncesidir: 10 * E / teta^2.')
    else:
        text.append('- Kaynak yok.')
    if 'goz_rengi' in sonuc:
        text += ['', 'Genel formül: ' + GENEL_FORMUL, GENEL_SIMGELER,
                 'Göz rengi katsayısı p: %g' % sonuc['goz_rengi'],
                 'Aralık dışında kalan kaynak (0.1 < açı < 100 derece): %d' % sonuc['genel_aralik_disi_sayisi']]
    text += ['', 'YAŞ TABLOSU']
    basliklar = ['Yaş (yıl)', 'Çarpan (birimsiz)', 'Örtü parlaklığı (cd/m²)']
    satirlar = [[str(y['yas']) + (' (istenen)' if y['yas'] == sonuc['yas'] else ''),
                 '%.2f' % y['carpan'], '%.3f' % y['ortu_parlakligi']] for y in sonuc['yas_tablosu']]
    if 'goz_rengi' in sonuc:
        basliklar.append('Genel örtü parlaklığı (cd/m²)')
        for row, y in zip(satirlar, sonuc['yas_tablosu']):
            row.append('%.3f' % y['genel_ortu_parlakligi'])
    text += _tablo(basliklar, satirlar)
    if sonuc.get('uyarilar'):
        text += ['', 'UYARILAR', *('- ' + item.rstrip('.') for item in sonuc['uyarilar'])]
    if sonuc.get('sinirlar'):
        text += ['', 'SINIRLAR', *('- ' + item.rstrip('.') for item in sonuc['sinirlar'])]
    return '\n'.join(text) + '\n'
