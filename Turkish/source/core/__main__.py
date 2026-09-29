# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Arayüzsüz komut satırı: kaynak -> CTScene -> Radiance çevirisi, ürünler ve kamaşma komutları."""
import sys
sys.dont_write_bytecode = True

import argparse
import json
import math
import os
import re
from pathlib import Path
import shlex
import shutil
import signal
import tempfile
import warnings

os.environ['PYTHONDONTWRITEBYTECODE'] = '1'


DESTEK = {'.ls10', '.evo', '.obj', '.stl', '.3ds', '.usda', '.usdc', '.usdz', '.usd',
          '.ies', '.ldt', '.glb', '.gltf', '.dae', '.fbx'}
URUN_DESTEK = {'.evo', '.zip', '.gldf', '.ldt', '.ies'}
# fotometri dosyası yazılan her raporun ve ürün tablosunun başlığından hemen sonra gelir.
FOTOMETRI_NOTU = ['Fotometri verisi sahibine, çoğunlukla armatür üreticisine aittir.',
                  'Bu dosyalar yalnız bu projeyi hesap için yeniden kurmak amacıyla yazıldı.']


class _Iptal(BaseException):
    def __init__(self, signum):
        self.signum = signum


def _cikti_hazirla(girdi, cikti, destek):
    """Girdiyi kontrol et, yeni ya da boş çıktı klasörünü hazırla, bütün komutlar aynı kuralı kullanır.

    destek None ise girdi bir sahne klasörüdür.
    """
    source = Path(girdi).resolve(strict=True)
    if destek is None:
        if not source.is_dir():
            raise ValueError('Sahne klasörü değil: ' + source.name)
    elif not source.is_file() or source.suffix.lower() not in destek:
        raise ValueError('Desteklenmeyen girdi biçimi: ' + source.suffix)
    output = Path(cikti).absolute()
    if any(p.is_symlink() for p in (output, *output.parents)):
        raise ValueError('Çıktı yolu sembolik bağ içeremez')
    # absolute() '..' bileşenlerini korur. İç içe kontrolü gerçek konumları karşılaştırsın diye sembolik bağ kontrolünden sonra normalleştirilir.
    output = Path(os.path.normpath(os.path.abspath(output)))
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise FileExistsError('Çıktı klasörü yeni veya boş olmalı')
    if output == source or source in output.parents or output in source.parents:
        raise ValueError('Girdi ve çıktı iç içe olamaz')
    output.mkdir(exist_ok=True)
    return source, output


def _not_sinifla(message, partial, skipped, limits):
    # Türkçe I/İ harfleri lower() öncesi elle çevrilir, yoksa 'SINIR' 'sinir' olur.
    normalized = message.replace('I', 'ı').replace('İ', 'i').lower()
    target = limits if 'sınır' in normalized else skipped if any(
        word in normalized for word in ('atlandı', 'eklenmedi', 'uygulanmadı', 'taşınmadı')) else partial
    if message not in target:
        target.append(message)


def _arac_uyarilari(entries):
    """Aynı araç/metin için dosya sayısı, dosyasız bildirimler aynen korunur."""
    groups, order = {}, []
    for entry in entries:
        for line in str(entry).splitlines():
            match = re.fullmatch(r'(ies2rad|xform): (.+?): (.+)', line)
            if not match:
                if line not in order:
                    order.append(line)
                continue
            tool, filename, message = match.groups()
            key = (tool, message)
            if key not in groups:
                groups[key] = []
                order.append(key)
            if filename not in groups[key]:
                groups[key].append(filename)
    return [('%s: %s, %d dosyada, ilk dosyalar: %s' %
             (item[0], item[1], len(groups[item]), ', '.join(groups[item][:3])))
            if isinstance(item, tuple) else item for item in order]


def _evalglare_bildirimleri(text):
    # evalglare 3.06 metinleri, uzun cümle kısasından önce denenir.
    kucuk = '. DGP kamaşma kaynaklarını olduğundan küçük gösterebilir'
    replacements = {
        'Low brightness scene. Vertical illuminance less than 380 lux! dgp might underestimate glare sources':
            'Düşük parlaklıklı sahne. Dikey aydınlık 380 lux altında' + kucuk,
        'Low brightness scene. dgp below 0.2! dgp might underestimate glare sources':
            'Düşük parlaklıklı sahne. DGP 0.2 altında' + kucuk,
        'Vertical illuminance is below 100 lux !!': 'Dikey aydınlık 100 lux altında',
        'Vertical illuminance is below 100 lux': 'Dikey aydınlık 100 lux altında',
        'Low brightness scene. dgp below 0.2': 'Düşük parlaklıklı sahne. DGP 0.2 altında',
        'Notice:': 'Bildirim:', 'Warning:': 'Uyarı:',
    }
    result = []
    for line in text.splitlines():
        if line.strip():
            translated = line.strip()
            for source, target in replacements.items():
                translated = re.sub(re.escape(source), lambda _: target, translated, flags=re.IGNORECASE)
            result.append('evalglare bildirimi: ' + translated)
    return result


def _rapor_yaz(output, source, status, read, partial, skipped, limits, error=None, fotometri=False):
    sections = [('OKUNAN', read), ('KISMİ KALAN / VARSAYILAN', partial),
                ('ATLANAN', skipped), ('SINIRLAR', limits)]
    text = ['CTLux', *(FOTOMETRI_NOTU if fotometri else []), 'Durum: '+status, 'Girdi: '+source.name]
    for label, entries in sections:
        text += ['', label, *(['- '+str(x).rstrip().rstrip('.') for x in _arac_uyarilari(entries)] or ['- Yok'])]
    if error:
        text += ['', 'HATA', str(error)]
    (output/'RAPOR.txt').write_text('\n'.join(text)+'\n', encoding='utf-8')


def _geri_al(teslim):
    # sadece bu işin teslim ettiği dosyalar geri alınır. Silinemeyen dosya asıl hatayı
    # örtmesin diye exception yükseltmez, adı ve nedeni döner.
    kalan = []
    for item in teslim:
        try:
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink(missing_ok=True)
        except OSError as error:
            kalan.append('%s (%s)' % (item.name, error))
    return kalan


def _hata_metni(error, kalan):
    if not kalan:
        return error
    return '%s\nGeri alınamayan sonuç dosyası: %s' % (error, ', '.join(kalan))


def cevir(girdi, cikti, *, kaynak_siniri_mib=1024, ucgen_siniri=3_000_000):
    """Tek dosyayı yeni ya da boş çıktı klasörüne çevirir, kaynağa hiç yazmaz.

    Başarılı ya da kısmi sonuç için dict döner. Kaynak okunamazsa ya da yazarken
    hata çıkarsa exception yükselir. Bytecode yazılmasın istiyorsan çağıran
    process'i -B ile başlat.
    """
    from core.scene_model import _sinir
    kaynak_siniri_mib = _sinir(kaynak_siniri_mib, 1024)
    ucgen_siniri = _sinir(ucgen_siniri, 3_000_000)
    source, output = _cikti_hazirla(girdi, cikti, DESTEK)
    read, partial, skipped = [], [], []
    limits = ['CTScene: okunan kaynakların toplamı %d MiB, %s üçgen, yüz başına 4096 köşe, aşılırsa hata.'
              % (kaynak_siniri_mib, format(ucgen_siniri, ',').replace(',', '.'))]
    teslim = []
    caught = []
    def not_ekle(message):
        _not_sinifla(message, partial, skipped, limits)
    def rapor(status, error=None):
        for warning in caught:
            not_ekle(str(warning.message))
        # not yalnız teslim edilmiş bir fotometri dosyası varken yazılır.
        _rapor_yaz(output, source, status, read, partial, skipped, limits, error,
                   error is None and any((output/'isik').glob('*.ies')))
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            # converter'ların çalışma dosyaları sadece çıktı klasörünün altında durur.
            with tempfile.TemporaryDirectory(prefix='.ceviri-', dir=output) as tmp:
                work = Path(tmp)
                from core import engine as motor, format_detector as dedektif, scene_model as sahne_dili, scene_writers as sahne_yazicilar
                from core.tools.ies_analysis import ies_oku
                from core.file_safety import ic_yol
                for name in ('model', 'isik', '_cache'):
                    (work/name).mkdir()
                local = work/('girdi'+source.suffix.lower())
                # OBJ'nin yan dosyaları asıl klasörden salt okunur çözülür.
                if source.suffix.lower() != '.obj':
                    shutil.copyfile(source, local)
                project = {'ad': source.stem, 'geometri': [], 'armaturler': []}
                suffix = source.suffix.lower()
                root = work
                if suffix == '.obj':
                    root = source.parent
                    project['geometri'] = [{'dosya': source.name}]
                    partial.append('OBJ birimsizdir, koordinatlar metre ve Z-up kabul edildi.')
                elif suffix in ('.ies', '.ldt'):
                    ies = work/'isik/girdi.ies'
                    if suffix == '.ldt':
                        motor.ldt_ies(str(local), str(ies))
                        read.append('LDT simetrisi açıldı ve mutlak kandela IES tablosu yazıldı.')
                    else:
                        shutil.copyfile(local, ies)
                    photo = ies_oku(ies)
                    read.append('IES: %d dikey × %d yatay = %d kandela' %
                                (photo['n_dikey'], photo['n_yatay'], len(photo['kandela'])))
                    project['armaturler'] = [{'ad': source.stem, 'ies': 'isik/girdi.ies',
                        'x': 0, 'y': 0, 'z': 0, 'tip': 'point', 'yon': [0,0,-1], 'dimmer': 1}]
                    partial.append('Fotometri girdisi: konum [0,0,0], yön [0,0,-1], dimmer 1 varsayıldı, geometri yok.')
                elif suffix == '.evo':
                    models, arms = dedektif._evo_yerlestir(str(local), str(work),
                        str(work/'model/girdi.obj'), read, partial)
                    project['geometri'] = [{'dosya': p} for p in models]
                    project['armaturler'] = arms
                    partial.append('EVO okuması STEP oda/yerleşim alt kümesidir, kapalı M3D/GDMS ayrıntıları ve tam malzeme modeli desteklenmez.')
                elif suffix in motor.USD_UZ:
                    # USD/glTF yan dosyaları asıl kaynağa göre salt okunur çözülür.
                    geo, arms, note = motor.usd_kopru_calistir(str(source), str(work/'model/girdi.obj'))
                    if geo:
                        project['geometri'] = [{'dosya': 'model/girdi.obj'}]
                    project['armaturler'], notes = motor.usd_armaturler(arms)
                    partial.extend(notes)
                    if note:
                        partial.append(note)
                    partial.append('USD statik mesh/ışık alt kümesi, animasyon ve tam malzeme ağı aktarılmaz.')
                else:
                    src = local if suffix == '.ls10' else source
                    motor.cevir(str(src), str(work/'model/girdi.obj'))
                    project['geometri'] = [{'dosya': 'model/girdi.obj'}]
                    if suffix == '.ls10':
                        project['armaturler'] = motor.lumion_armaturler(str(local))
                        report = json.loads((work/'model/girdi.obj.aktarim.json').read_text())
                        partial.extend(report['uyarilar'])
                        if report['atlanan']:
                            skipped.append('Lumion geometri kayıtları: '+str(report['atlanan']))
                    else:
                        partial.append('Assimp geometri köprüsü, birim/eksen ve malzeme anlamı kaynak uygulamayla doğrulanmalı.')
                for arm in project['armaturler']:
                    if arm.get('urun_rad') and not arm.get('ies'):
                        ies = Path(arm['urun_rad']).with_suffix('.ies')
                        if (root/ies).is_file():
                            arm['ies'] = ies.as_posix()
                scene = sahne_dili.scene_from_project(str(root), project,
                    max_source_bytes=kaynak_siniri_mib * 1048576, max_triangles=ucgen_siniri)
                scene['camera'] = sahne_yazicilar.default_camera(scene)
                partial.append('Görünüm otomatik çerçevelendi, kaynak kamera aktarılmadı.')
                def asset(ref):
                    return {'data': Path(ic_yol(root, ref)).read_bytes()}
                result = sahne_yazicilar.export_radiance(scene, work/'sonuc', asset, max_triangles=ucgen_siniri)
                for message in result['warnings']:
                    not_ekle(message)
                read.append('%d köşe, %d üçgen, %d ışık CTScene ve Radiance sahnesine işlendi.' %
                            (result['stats']['vertices'], result['stats']['triangles'], result['stats']['lights']))
                if suffix == '.evo':
                    limits.append('EVO armatür sınırı: 4000, aşım ayrıca atlanan sayısıyla bildirilir.')
                # temp klasör silinmeden önce IES referanslarını teslim edilen path'lere bağla.
                for i, light in enumerate(scene['lights']):
                    delivered = work/'sonuc/isik'/('l%d.ies' % i)
                    if delivered.is_file():
                        ref = 'isik/l%d.ies' % i
                        light['source']['ies'] = ref
                        light['source'].pop('urun_rad', None)
                        light['original']['ies'] = ref
                        light['original'].pop('urun_rad', None)
                for warning in caught:
                    not_ekle(str(warning.message))
                scene['warnings'] = list(dict.fromkeys([*scene['warnings'], *partial, *skipped, *limits]))
                (work/'sonuc/scene.ctlux.json').write_text(json.dumps(scene, ensure_ascii=False,
                    indent=2, allow_nan=False)+'\n', encoding='utf-8')
                for item in (work/'sonuc').iterdir():
                    teslim.append(output/item.name)
                    shutil.move(str(item), str(output/item.name))
        rapor('kısmi' if partial or skipped or caught else 'başarılı')
        return {'output': str(output), 'stats': result['stats'], 'report': 'RAPOR.txt'}
    except (_Iptal, KeyboardInterrupt):
        rapor('iptal', _hata_metni('İptal edildi.', _geri_al(teslim)))
        raise
    except Exception as error:
        # teslim yarıda kaldıysa bu koşunun taşıdığı sonuçlar da geri alınır, RAPOR.txt kalır.
        rapor('hata', _hata_metni(error, _geri_al(teslim)))
        raise


def _bos_ad(klasor, taban, uz):
    # aynı adlı ikinci ürün öncekini ezmesin: _2, _3.
    ad, i = taban + uz, 2
    while (klasor/ad).exists():
        ad = '%s_%d%s' % (taban, i, uz)
        i += 1
    return ad


def _urun_satiri(ies, veri, ad, uretici, lumen, kullanim):
    from core.tools.ies_analysis import isin_acisi
    beam, _ = isin_acisi(veri['dikey_acilar'], veri['kandela'], veri['n_dikey'], veri['n_yatay'])
    return {'ad': ad, 'uretici': uretici, 'lumen': lumen,
            'isin_acisi': round(beam, 1) if beam else None,
            'tepe_kandela': round(max(veri['kandela']) * veri['carpan'], 1),
            'c_duzlemi': veri['n_yatay'], 'gama_acisi': veri['n_dikey'],
            'kullanim': kullanim, 'ies': ies.name}


def _urun_tablosu(source, satirlar, reddedilen):
    basliklar = [('ad', 'Ad'), ('uretici', 'Üretici'), ('lumen', 'Lümen (lm)'), ('isin_acisi', 'Işın açısı (°)'),
                 ('tepe_kandela', 'Tepe şiddet (cd)'), ('c_duzlemi', 'C düzlemi'), ('gama_acisi', 'Gama açısı'),
                 ('kullanim', 'Kullanım'), ('ies', 'IES dosyası')]
    def hucre(value):
        return '-' if value is None else '%g' % value if isinstance(value, float) else str(value)
    tablo = [[etiket for _, etiket in basliklar]]
    tablo += [[hucre(satir[anahtar]) for anahtar, _ in basliklar] for satir in satirlar]
    genislik = [max(len(r[i]) for r in tablo) for i in range(len(basliklar))]
    text = ['CTLux ürün tablosu', *FOTOMETRI_NOTU, 'Girdi: '+source.name, '']
    text += ['  '.join(h.ljust(genislik[i]) for i, h in enumerate(r)).rstrip() for r in tablo]
    text += ['', 'REDDEDİLEN']
    text += ['- %s: %s' % (r['ad'], str(r['neden']).rstrip().rstrip('.')) for r in reddedilen] or ['- Yok']
    return '\n'.join(text)+'\n'


def urunler(girdi, cikti):
    """DIALux evo ya da fotometri dosyasındaki aydınlatma ürünlerini yeni/boş klasöre çıkarır.

    Ürün başına bir IES, URUNLER.txt, URUNLER.json ve RAPOR.txt yazılır, kaynağa
    yazılmaz. Lümeni olmayan ya da kandela tablosu bozuk üründe sayı uydurmayız:
    IES yazılmaz, nedeni rapora girer, diğer ürünler devam eder. Hiç IES
    yazılamazsa exception yükselir.
    """
    source, output = _cikti_hazirla(girdi, cikti, URUN_DESTEK)
    read, partial, skipped, limits = [], [], [], []
    teslim = []
    caught = []
    def rapor(status, error=None):
        for warning in caught:
            _not_sinifla(str(warning.message), partial, skipped, limits)
        # hata ya da iptalde ürün dosyası kalmaz, not da yazılmaz.
        _rapor_yaz(output, source, status, read, partial, skipped, limits, error, error is None)
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            # ara dosyalar sadece çıktı klasörünün altında durur.
            with tempfile.TemporaryDirectory(prefix='.urunler-', dir=output) as tmp:
                work = Path(tmp)
                from core import engine as motor, importer, photometry as fotometri
                from core.tools.ies_analysis import ies_oku
                for name in ('girdi', 'ham', 'donusum', 'sonuc'):
                    (work/name).mkdir()
                # asıl ad korunur: GLDF ve EVO dosya adları girdinin adından türer.
                local = work/'girdi'/source.name
                shutil.copyfile(source, local)
                suffix = source.suffix.lower()
                sonuc = work/'sonuc'
                satirlar, reddedilen = [], []
                def reddet(ad, neden, kullanim=None):
                    reddedilen.append({'ad': ad, 'neden': neden, 'kullanim': kullanim})
                    skipped.append('%s: %s, IES yazılmadı.' % (ad, neden))
                if suffix == '.evo':
                    red = []
                    kayitlar = importer.evo_ies(str(local), str(work/'ham'), reddedilen=red)
                    adlar = importer.evo_fotometri_yaz(str(work/'ham'), str(sonuc), proje_ad=source.stem)
                    dosya = {int(ad.rsplit('_u', 1)[1][:-4]): ad for ad in adlar}
                    urun = {}
                    for kayit in kayitlar.values():
                        eq = int(kayit['urun_temsil_kayit'][1:])
                        urun.setdefault(eq, dict(kayit, kullanim=0))['kullanim'] += 1
                    def urun_adi(kayit):
                        return kayit.get('urun_ad') or kayit.get('urun_kod') or 'ürün ' + kayit['urun_temsil_kayit']
                    for eq in sorted(urun):
                        kayit = urun[eq]
                        if eq not in dosya:
                            skipped.append('%s: IES okunamadı, atlandı.' % urun_adi(kayit))
                            continue
                        ies = sonuc/dosya[eq]
                        satirlar.append(_urun_satiri(ies, ies_oku(ies), urun_adi(kayit), None,
                                                     kayit['lumen'], kayit['kullanim']))
                    for kayit in red:
                        reddet(urun_adi(kayit), kayit['neden'], kayit['kullanim'])
                    ents = motor._evo_step_ents(str(local)) or {}
                    toplam = sum(1 for tur, _ in ents.values() if tur == 'LuminaireElement')
                    bagli = sum(k['kullanim'] for k in urun.values()) + sum(k['kullanim'] for k in red)
                    read.append('EVO: %d armatür örneği, %d ürün okundu.' % (toplam, len(urun) + len(red)))
                    if toplam > bagli:
                        partial.append('%d EVO armatür örneğinin ürün fotometrisi yok, ürün tablosuna girmedi.'
                                       % (toplam - bagli))
                    limits.append('EVO ürün kaydından üretici adı okunmaz, IES içindeki [MANUFAC] '
                                  'satırı bunu belirtir.')
                else:
                    ham = work/'ham'
                    if suffix == '.zip':
                        adlar = importer.zip_kutuphane(str(local), str(ham))
                    elif suffix == '.gldf':
                        adlar = [Path(p).name for p in importer.gldf_ac(str(local), str(ham))]
                    else:
                        shutil.copyfile(local, ham/source.name)
                        adlar = [source.name]
                    read.append('%d fotometri dosyası bulundu.' % len(adlar))
                    gorulen = {}
                    for ad in adlar:
                        bilgi = {}
                        try:
                            if ad.lower().endswith('.ldt'):
                                ies = work/'donusum'/(Path(ad).stem + '.ies')
                                motor.ldt_ies(str(ham/ad), str(ies), bilgi)
                            else:
                                ies = ham/ad
                            veri = ies_oku(ies)
                        except (ValueError, RuntimeError) as error:
                            reddet(ad, str(error))
                            continue
                        if bilgi:
                            lumen = bilgi['lumen']
                        elif veri['lumen_lamba'] > 0:
                            lumen = veri['lamba'] * veri['lumen_lamba']
                        elif veri['lumen_lamba'] == -1 and '_LUMENS' in veri['anahtarlar']:
                            try:
                                lumen = float(veri['anahtarlar']['_LUMENS'])
                                if not math.isfinite(lumen) or lumen <= 0:
                                    raise ValueError
                            except ValueError:
                                reddet(ad, 'geçersiz [_LUMENS]: pozitif, sonlu lümen gerekli')
                                continue
                        else:
                            reddet(ad, 'lümen yok (IES lümen alanı %g), kandela tablosundan hesaplanmadı'
                                   % veri['lumen_lamba'])
                            continue
                        icerik = ies.read_bytes()
                        if icerik in gorulen:
                            partial.append('%s: %s ile aynı içerik, bir kez yazıldı.' % (ad, gorulen[icerik]))
                            continue
                        hedef = sonuc/_bos_ad(sonuc, ies.stem, '.ies')
                        hedef.write_bytes(fotometri.notlu_ies(icerik))
                        gorulen[icerik] = hedef.name
                        anahtar = veri['anahtarlar']
                        satirlar.append(_urun_satiri(hedef, veri,
                            anahtar.get('LUMINAIRE') or anahtar.get('LUMCAT') or Path(ad).stem,
                            anahtar.get('MANUFAC'), lumen, None))
                    limits.append('Mutlak fotometrili IES (lümen alanı -1), pozitif ve sonlu [_LUMENS] beyanı yoksa lümensiz sayılır, '
                                  'lümen kandela tablosundan hesaplanmaz.')
                    limits.append('Fotometri dosyası tek başına sahne değildir, kullanım sayısı yok.')
                read.append('%d ürün için IES yazıldı, %d ürün reddedildi.' % (len(satirlar), len(reddedilen)))
                limits.append('Işın açısı ilk C düzleminde tepe şiddetin yarısına göre hesaplanır.')
                if suffix in ('.evo', '.zip'):
                    limits.append('Dosya adındaki açı künyesi mevcut adlandırma kuralıyla yazılır, '
                                  'tablodaki ışın açısının iki katıdır.')
                if not satirlar:
                    raise RuntimeError('Yazılabilir aydınlatma ürünü yok.')
                (sonuc/'URUNLER.txt').write_text(_urun_tablosu(source, satirlar, reddedilen), encoding='utf-8')
                (sonuc/'URUNLER.json').write_text(json.dumps({'girdi': source.name, 'urunler': satirlar,
                    'reddedilen': reddedilen}, ensure_ascii=False, indent=2, allow_nan=False)+'\n',
                    encoding='utf-8')
                for item in sorted(sonuc.iterdir()):
                    teslim.append(output/item.name)
                    shutil.move(str(item), str(output/item.name))
        rapor('kısmi' if partial or skipped or caught else 'başarılı')
        return {'output': str(output), 'urunler': len(satirlar), 'reddedilen': len(reddedilen),
                'report': 'RAPOR.txt'}
    except (_Iptal, KeyboardInterrupt):
        rapor('iptal', _hata_metni('İptal edildi.', _geri_al(teslim)))
        raise
    except Exception as error:
        # teslim yarıda kaldıysa bu koşunun taşıdığı sonuçlar da geri alınır, RAPOR.txt kalır.
        rapor('hata', _hata_metni(error, _geri_al(teslim)))
        raise


KAMASMA_COZ = {'taslak': 400, 'orta': 800, 'final': 1200}


def _vektor(deger, ad, sifir_olmaz=False):
    v = [float(x) for x in deger]
    if len(v) != 3 or not all(math.isfinite(x) for x in v):
        raise ValueError(ad + ' üç sonlu sayı olmalı')
    if sifir_olmaz and not any(v):
        raise ValueError(ad + ' sıfır olamaz')
    return v


def _bakis(sahne, vp, vd):
    """Komut satırındaki bakış eksikse gorunum.vf dosyasından tamamlanır."""
    if vp is not None and vd is not None:
        return vp, vd, 'komut satırı'
    vf = sahne/'gorunum.vf'
    if not vf.is_file():
        raise FileNotFoundError('Sahne klasöründe gorunum.vf yok, --vp ve --vd verin')
    parca = shlex.split(vf.read_text(encoding='utf-8'))
    dosya = {}
    for anahtar in ('-vp', '-vd'):
        if anahtar not in parca:
            raise ValueError('gorunum.vf içinde %s yok' % anahtar)
        i = parca.index(anahtar)
        dosya[anahtar] = _vektor(parca[i+1:i+4], 'gorunum.vf ' + anahtar, anahtar == '-vd')
    kaynak = 'gorunum.vf' if vp is None and vd is None else 'komut satırı ve gorunum.vf'
    return vp or dosya['-vp'], vd or dosya['-vd'], kaynak


def kamasma(sahne, cikti, vp=None, vd=None, yas=75, kalite='orta', goz_rengi=0.5):
    """`cevir` çıktısındaki sahnede, bakış noktasından yaşa göre kamaşma analizi.

    Balıkgözü görüntü çizilir, evalglare kaynakları bulur, her kaynağın yönüne
    gözden atılan ışın cismin adını verir. Yaşa göre örtü parlaklığı bu kaynak
    listesinden hesaplanır. KAMASMA.txt, KAMASMA.json, goz.png, isaretli.png ve
    RAPOR.txt yazılır, sahne klasörüne yazılmaz. evalglare ya da eşleme
    başarısızsa değer uydurmayız, exception yükselir.
    """
    from core import glare as analiz
    if kalite not in KAMASMA_COZ:
        raise ValueError('Kalite taslak, orta ya da final olmalı')
    analiz.yas_carpani(yas)
    analiz.goz_rengi_kontrol(goz_rengi)
    vp = None if vp is None else _vektor(vp, 'Bakış noktası')
    vd = None if vd is None else _vektor(vd, 'Bakış yönü', True)
    source, output = _cikti_hazirla(sahne, cikti, None)
    read, partial, skipped, limits = [], [], [], list(analiz.SINIRLAR)
    if vp is None or vd is None:
        partial.append('Otomatik görünüm çizim içindir, kamaşma için göz konumu odanın içinde seçilmelidir. '
                       '--vp ve --vd seçeneklerini birlikte verin.')
    limits.append('Yüzeyde cisim adı CTScene malzeme adıdır, CTScene üçgen başına nesne adı tutmaz.')
    limits.append('Genel denklem 0.1 < açı < 100 derece içindir, 180 derece balıkgözü görüntü '
                  'bakıştan 90 derece ötesini içermez, genel toplam yalnız bulunan kaynakları kapsar.')
    read.append('Göz rengi katsayısı p: %g.' % goz_rengi)
    limits.append('Sahnede gök ve gün ışığı yoktur, yalnız sahnedeki ışıklar hesaba girer.')
    teslim = []
    caught = []
    def rapor(status, error=None):
        for warning in caught:
            _not_sinifla(str(warning.message), partial, skipped, limits)
        _rapor_yaz(output, source, status, read, partial, skipped, limits, error)
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            # octree, HDR ve ışın dosyası sadece çıktı klasörünün altında durur.
            with tempfile.TemporaryDirectory(prefix='.kamasma-', dir=output) as tmp:
                work = Path(tmp)
                from core import engine as motor
                rad, sahne_json = source/'scene.rad', source/'scene.ctlux.json'
                for dosya in (rad, sahne_json):
                    if not dosya.is_file():
                        raise FileNotFoundError('Sahne klasöründe %s yok' % dosya.name)
                metin = rad.read_text(encoding='utf-8')
                # Radiance, satır içinde ve kontrol karakterlerinden sonra da primitive arası ! kabul eder.
                # Statik çeviri çıktısı komut kaçışı gerektirmez. Radiance token/yorum sınırlarını tahmin
                # etmek yerine yorum ve dizelerde bile ! reddedilir. Bu bir Radiance sandbox'ı değildir.
                if '!' in metin:
                    raise ValueError('scene.rad dış komut işareti (!) içeriyor, yalnız statik çeviri çıktısı kabul edilir')
                scene = json.loads(sahne_json.read_text(encoding='utf-8'))
                vp, vd, bakis_kaynagi = _bakis(source, vp, vd)
                read.append('Bakış: %s, nokta (m) %s, yön %s.' % (bakis_kaynagi,
                            ' '.join('%g' % x for x in vp), ' '.join('%g' % x for x in vd)))
                calisma = work/'sahne'
                (calisma/'_cache').mkdir(parents=True)
                # Değişebilir kaynağı yeniden açmadan, denetlenen içeriğin aynısını kullan.
                (calisma/'scene.rad').write_text(metin, encoding='utf-8')
                isik = source/'isik'
                # isik/ altındaki bağ veya aygıt sahne dışını gösterebilir ya da okunurken bitmeyebilir.
                if isik.is_symlink() or isik.exists() and not isik.is_dir() or any(p.is_symlink() or not (p.is_file() or p.is_dir())
                                            for p in (isik.rglob('*') if isik.is_dir() else ())):
                    raise ValueError('isik/ yalnız normal dosya ve klasör içermeli')
                if isik.is_dir():
                    shutil.copytree(isik, calisma/'isik')
                motor.ortam_hazirla()
                kod, _, se = motor.sh('oconv scene.rad > _cache/sahne.oct', cwd=calisma)
                if kod:
                    raise RuntimeError('oconv başarısız: ' + se[-500:])
                yon = analiz._birim(vd)
                yukari = [0, 0, 1] if abs(yon[2]) < 0.999999 else [0, 1, 0]
                gorunum = '-vta -vp %s -vd %s -vu %s -vh 180 -vv 180' % tuple(
                    ' '.join('%.12g' % x for x in v) for v in (vp, vd, yukari))
                coz = KAMASMA_COZ[kalite]
                motor.render(str(calisma/'_cache/sahne.oct'), gorunum, kalite, coz, coz, str(work/'goz.hdr'))
                read.append('Balıkgözü görüntü: %d x %d piksel, kalite %s.' % (coz, coz, kalite))
                kod, so, se = motor.sh('evalglare -d -c isaretli.hdr goz.hdr', cwd=work)
                partial.extend(_evalglare_bildirimleri(se))
                partial.extend(_evalglare_bildirimleri('\n'.join(
                    line for line in so.splitlines() if re.match(r'\s*(notice|warning)\b', line, re.I))))
                if kod:
                    raise RuntimeError('evalglare başarısız (%d): %s' % (kod, se[-500:]))
                kaynaklar, ozet, ozet_ham = analiz.evalglare_coz(so)
                if ozet['ugr'] is None:
                    partial.append('evalglare UGR tanımsız, çünkü arka plan parlaklığı 0. '
                                   'Yazılan %s değeri ölçüm değildir.' % ozet_ham['ugr'])
                adlar = []
                if kaynaklar:
                    (work/'isinlar.txt').write_text(''.join('%s\n' % ' '.join('%.12g' % x for x in (*vp, *k['yon']))
                                                            for k in kaynaklar), encoding='utf-8')
                    kod, so, se = motor.sh('rtrace -h -ab 0 -oms _cache/sahne.oct < ../isinlar.txt', cwd=calisma)
                    if kod:
                        raise RuntimeError('rtrace başarısız (%d): %s' % (kod, se[-500:]))
                    adlar = analiz.cisim_adlari(so, len(kaynaklar), scene)
                for k, ad in zip(kaynaklar, adlar):
                    k.update(ad)
                toplam = analiz.ortu_parlakligi(kaynaklar, vd)
                kaynaklar.sort(key=lambda k: (not k['aralikta'], -(k['katki'] or 0), -k['aydinlik']))
                for i, k in enumerate(kaynaklar, 1):
                    k['sira'] = i
                disarida = sum(not k['aralikta'] for k in kaynaklar)
                read.append('evalglare: %d kaynak, hepsi cisim adıyla eşlendi.' % len(kaynaklar)
                            if kaynaklar else 'evalglare: kamaşma kaynağı bulunmadı.')
                if disarida:
                    skipped.append('%d kaynak 1 < açı < 30 derece koşulu dışında. Örtü parlaklığı toplamına girmedi.'
                                   % disarida)
                sonuc = {'sahne': source.name, 'yas': yas, 'kalite': kalite, 'cozunurluk': coz,
                         'bakis': {'vp': vp, 'vd': vd, 'kaynak': bakis_kaynagi},
                         'formul': analiz.FORMUL, 'gecerlilik_derece': [analiz.ALT_ACI, analiz.UST_ACI],
                         'evalglare': {'ham': ozet_ham, 'deger': ozet},
                         'kaynak_sayisi': len(kaynaklar), 'aralik_disi_sayisi': disarida,
                         'kaynaklar': [{a: k[a] for a in ('sira', 'no', 'cisim', 'radiance_adi', 'parlaklik',
                                        'kati_aci', 'aci', 'aydinlik', 'katki', 'aralikta', 'yon')}
                                       for k in kaynaklar],
                         'yas_carpani_oncesi_toplam': toplam,
                         'yas_tablosu': analiz.yas_tablosu(toplam, yas)}
                sonuc['goz_rengi'] = goz_rengi
                sonuc['genel_formul'] = analiz.GENEL_FORMUL
                sonuc['genel_gecerlilik_derece'] = [0.1, 100]
                sonuc['genel_aralik_disi_sayisi'] = sum(not 0.1 < k['aci'] < 100 for k in kaynaklar)
                for row in sonuc['yas_tablosu']:
                    row['genel_ortu_parlakligi'] = analiz.genel_toplam(kaynaklar, row['yas'], goz_rengi)
                if sonuc['genel_aralik_disi_sayisi']:
                    skipped.append('%d kaynak genel denklemin 0.1 < açı < 100 derece aralığı dışında.'
                                   % sonuc['genel_aralik_disi_sayisi'])
                teslimat = work/'sonuc'
                teslimat.mkdir()
                motor.hdr_to_png(str(work/'goz.hdr'), str(teslimat/'goz.png'), otomatik=True)
                motor.hdr_to_png(str(work/'isaretli.hdr'), str(teslimat/'isaretli.png'), otomatik=True)
                sonuc['uyarilar'] = list(dict.fromkeys([*partial, *skipped]))
                sonuc['sinirlar'] = list(dict.fromkeys(limits))
                (teslimat/'KAMASMA.txt').write_text(analiz.metin(sonuc), encoding='utf-8')
                (teslimat/'KAMASMA.json').write_text(json.dumps(sonuc, ensure_ascii=False, indent=2,
                    allow_nan=False)+'\n', encoding='utf-8')
                for item in sorted(teslimat.iterdir()):
                    teslim.append(output/item.name)
                    shutil.move(str(item), str(output/item.name))
        rapor('kısmi' if partial or skipped or caught else 'başarılı')
        return {'output': str(output), 'kaynaklar': len(kaynaklar), 'aralik_disi': disarida,
                'report': 'RAPOR.txt'}
    except (_Iptal, KeyboardInterrupt):
        rapor('iptal', _hata_metni('İptal edildi.', _geri_al(teslim)))
        raise
    except Exception as error:
        # teslim yarıda kaldıysa bu koşunun taşıdığı sonuçlar da geri alınır, RAPOR.txt kalır.
        rapor('hata', _hata_metni(error, _geri_al(teslim)))
        raise


def _pozitif_tamsayi(value):
    try:
        number = int(value)
        if number > 0:
            return number
    except ValueError:
        pass
    raise argparse.ArgumentTypeError('a positive integer is required')


def _kalite_secimi(value):
    aliases = {'draft': 'taslak', 'medium': 'orta', 'final': 'final',
               'taslak': 'taslak', 'orta': 'orta'}
    try:
        return aliases[value]
    except KeyError:
        raise argparse.ArgumentTypeError('quality must be draft, medium or final') from None


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    komut = cevir
    if argv and argv[0] in ('products', 'urunler'):
        komut, argv = urunler, argv[1:]
        parser = argparse.ArgumentParser(prog='python3 -m core products',
            description='Extract lighting products from DIALux evo or photometry into IES files and a product table.')
    elif argv and argv[0] in ('glare', 'kamasma'):
        komut, argv = kamasma, argv[1:]
        parser = argparse.ArgumentParser(prog='python3 -m core glare',
            description='Find glare sources in a converted scene, identify surfaces and calculate age adjusted veiling luminance.')
    else:
        parser = argparse.ArgumentParser(prog='python3 -m core',
            description='Convert an input file to CTScene and Radiance. '
                        'Products: products <input> <output_directory>. '
                        'Glare: glare <scene_directory> <output_directory>.')
    parser.add_argument('girdi', metavar='scene_directory' if komut is kamasma else 'input')
    parser.add_argument('cikti_klasoru', metavar='output_directory')
    secenek = {}
    if komut is kamasma:
        parser.add_argument('--vp', nargs=3, type=float, metavar=('X', 'Y', 'Z'),
                            help='view point, read from gorunum.vf when omitted')
        parser.add_argument('--vd', nargs=3, type=float, metavar=('DX', 'DY', 'DZ'),
                            help='view direction, read from gorunum.vf when omitted')
        parser.add_argument('--eye-pigmentation', '--goz-rengi', dest='goz_rengi',
                            type=float, default=0.5, metavar='P',
                            help='eye pigmentation factor from 0 to 1.2, default 0.5')
        parser.add_argument('--age', '--yas', dest='yas', type=int, default=75,
                            metavar='N', help='observer age, default 75')
        parser.add_argument('--quality', '--kalite', dest='kalite', default='orta',
                            type=_kalite_secimi, metavar='{draft,medium,final}',
                            help='render quality and fisheye size, default medium')
    if komut is cevir:
        parser.add_argument('--source-limit-mib', '--kaynak-siniri-mib', dest='kaynak_siniri_mib',
                            type=_pozitif_tamsayi, default=1024, metavar='N',
                            help='registered source data limit in MiB, default 1024')
        parser.add_argument('--triangle-limit', '--ucgen-siniri', dest='ucgen_siniri',
                            type=_pozitif_tamsayi, default=3_000_000, metavar='N',
                            help='triangle limit, default 3000000')
    args = parser.parse_args(argv)
    if komut is cevir:
        secenek = {'kaynak_siniri_mib': args.kaynak_siniri_mib, 'ucgen_siniri': args.ucgen_siniri}
    if komut is kamasma:
        secenek = {'vp': args.vp, 'vd': args.vd, 'yas': args.yas, 'kalite': args.kalite, 'goz_rengi': args.goz_rengi}
    onceki = {}
    def iptal(signum, frame):
        # ikinci sinyal, ilk sinyalin child process ve temp dosya temizliğini yarıda kesmesin.
        for sig in onceki:
            signal.signal(sig, signal.SIG_IGN)
        raise _Iptal(signum)
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            onceki[sig] = signal.getsignal(sig)
            signal.signal(sig, iptal)
        result = komut(args.girdi, args.cikti_klasoru, **secenek)
    except (_Iptal, KeyboardInterrupt) as error:
        print('İptal edildi.', file=sys.stderr)
        return 128 + getattr(error, 'signum', signal.SIGINT)
    except Exception as error:
        print('Hata: '+str(error), file=sys.stderr)
        return 1
    finally:
        for sig, handler in onceki.items():
            signal.signal(sig, handler)
    print('Çıktı: '+result['output']+' / RAPOR.txt')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
