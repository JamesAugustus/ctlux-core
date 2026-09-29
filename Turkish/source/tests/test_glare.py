# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Yaşa göre kamaşma komutu: formül, evalglare çözümü, cisim eşlemesi ve uçtan uca akış."""
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import shlex
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
from core import engine as motor
from core import glare as K
from core.__main__ import cevir, kamasma
import synthetic as sentetik

motor.ortam_hazirla()
RADIANCE = all(shutil.which(x) for x in ('oconv', 'rpict', 'evalglare', 'rtrace', 'pcond', 'ra_ppm', 'pextrem'))
BASLIK = ('No pixels x-pos y-pos L_s Omega_s Posindx L_b L_t E_v Edir Max_Lum Sigma xdir ydir zdir '
          'Eglare Lveil_cie teta glare_zone r_contrast r_dgp')
OZET = ('dgp,av_lum,E_v,lum_backg,E_v_dir,dgi,ugr,vcp,cgi,lum_sources,omega_sources,Lveil,Lveil_cie,'
        'dgr,ugp,ugr_exp,dgi_mod,av_lum_pos,av_lum_pos2,med_lum,med_lum_pos,med_lum_pos2,ugp2: '
        '0.412000 ' + ' '.join(['1.0'] * 5) + ' 23.500000 ' + ' '.join(['1.0'] * 16))
SINIR_SATIRLARI = (
    'Malzemeler mat varsayılır, parlak yüzeyden aynasal yansıma bu sahnede oluşmaz.',
    'EVO kaynaklı armatürlerde ışık çıkış açıklığının boyutu yer tutucudur, armatürün kendi parlaklığı '
    've ondan türetilen DGP, UGR ve örtü parlaklığı bu yüzden güvenilir değildir.',
    'Formül 1 < açı < 30 derece koşulunda geçerlidir. Dışarıdaki kaynaklar toplama girmez.')


def kaynak_satiri(no, L, omega, yon):
    sayilar = [no, 10, 1, 1, L, omega, 1, 1, 1, 1, 1, L, 1, *yon, 1, 1, 1, 0, 0, 0]
    return ' '.join('%.6f' % x for x in sayilar)


def evalglare_metni(satirlar, sayi=None):
    return '\n'.join(['%d %s' % (len(satirlar) if sayi is None else sayi, BASLIK), *satirlar, OZET]) + '\n'


def yon(derece):
    """Bakış yönü +X, kaynak XZ düzleminde verilen açıda."""
    return [math.cos(math.radians(derece)), 0, math.sin(math.radians(derece))]


class FormulTest(unittest.TestCase):
    def test_tek_kaynak_elle_hesap(self):
        kaynaklar = [{'parlaklik': 1000.0, 'kati_aci': 0.01, 'yon': yon(10)}]
        toplam = K.ortu_parlakligi(kaynaklar, [2, 0, 0])
        E = 1000 * 0.01 * math.cos(math.radians(10))
        self.assertAlmostEqual(kaynaklar[0]['aci'], 10, places=9)
        self.assertAlmostEqual(kaynaklar[0]['aydinlik'], E, places=9)
        self.assertAlmostEqual(kaynaklar[0]['katki'], 10 * E / 100, places=9)
        self.assertAlmostEqual(toplam, 0.984807753, places=9)
        tablo = {y['yas']: y for y in K.yas_tablosu(toplam, 75)}
        self.assertAlmostEqual(tablo[70]['ortu_parlakligi'], 2 * toplam, places=12)
        self.assertAlmostEqual(tablo[75]['ortu_parlakligi'], toplam * (1 + (75 / 70) ** 4), places=12)
        self.assertEqual(sorted(tablo), [20, 40, 50, 60, 70, 75, 80])

    def test_genel_formul_bagimsiz_sayisal_ornek(self):
        # A=62.5, p=.5, açı=10: 0.01 + (0.05+0.005)*2 + .00125 = .12125.
        self.assertAlmostEqual(K.genel_toplam([{'aci': 10, 'aydinlik': 2}], 62.5), .2425)
        # Eski toplamda dışlanan 60 derece kaynak genel toplama girer.
        self.assertGreater(K.genel_toplam([{'aci': 60, 'aydinlik': 2}], 75), 0)
        self.assertEqual(K.genel_toplam([], 75), 0)

    def test_genel_aralik_ve_goz_rengi(self):
        for angle in (0, .1, 100, 101):
            self.assertEqual(K.genel_toplam([{'aci': angle, 'aydinlik': 2}], 75), 0)
        for angle in (.10001, 99.999):
            self.assertGreater(K.genel_toplam([{'aci': angle, 'aydinlik': 2}], 75), 0)
        for p in (-1, 1.3, float('nan'), float('inf'), True):
            with self.assertRaises(ValueError):
                K.genel_toplam([], 75, p)
        for p in (0, .5, 1, 1.2):
            self.assertEqual(K.goz_rengi_kontrol(p), p)
        with self.assertRaises(ValueError):
            K.genel_toplam([{'aci': 95, 'aydinlik': -1}], 75)

    def test_yas_carpanlari(self):
        for yas, beklenen in ((20, 1.01), (50, 1.26), (60, 1.54), (70, 2.00), (80, 2.71)):
            with self.subTest(yas=yas):
                self.assertEqual(round(K.yas_carpani(yas), 2), beklenen)
        for kotu in (0, -5, 121, float('nan'), '70', True, False):
            with self.subTest(yas=kotu), self.assertRaises(ValueError):
                K.yas_carpani(kotu)

    def test_aralik_disi_sayilir_ve_toplama_girmez(self):
        acilar = (0.5, 1, 1.001, 15, 29.999, 30, 31, 90)
        kaynaklar = [{'parlaklik': 500.0, 'kati_aci': 0.002, 'yon': yon(a)} for a in acilar]
        toplam = K.ortu_parlakligi(kaynaklar, [1, 0, 0])
        self.assertEqual([k['aralikta'] for k in kaynaklar], [False, False, True, True, True, False, False, False])
        self.assertEqual(sum(not k['aralikta'] for k in kaynaklar), 5)
        self.assertEqual([k['katki'] is None for k in kaynaklar], [True, True, False, False, False, True, True, True])
        beklenen = sum(10 * 500 * .002 * math.cos(math.radians(a)) / a ** 2 for a in (1.001, 15, 29.999))
        self.assertAlmostEqual(toplam, beklenen, places=9)

    def test_sifir_kaynak(self):
        self.assertEqual(K.ortu_parlakligi([], [0, 1, 0]), 0)
        self.assertTrue(all(y['ortu_parlakligi'] == 0 for y in K.yas_tablosu(0.0, 75)))
        sonuc = {'sahne': 's', 'yas': 75, 'bakis': {'vp': [0, 0, 1], 'vd': [1, 0, 0], 'kaynak': 'komut satırı'},
                 'evalglare': {'ham': {'dgp': '0.159000', 'ugr': '0.000000'}}, 'kaynak_sayisi': 0,
                 'aralik_disi_sayisi': 0, 'kaynaklar': [], 'yas_tablosu': K.yas_tablosu(0.0, 75)}
        text = K.metin(sonuc)
        self.assertIn('- Kaynak yok.', text)
        self.assertIn('Aralık dışında kalan kaynak (1 < açı < 30 derece): 0', text)
        self.assertIn('75 (istenen)', text)
        for birim in ('Bakış noktası (m): 0 0 1', 'Yaş (yıl)', 'Çarpan (birimsiz)', K.SIMGELER):
            self.assertIn(birim, text)


class EvalglareTest(unittest.TestCase):
    def test_sonlu_olmayan_istege_bagli_ozet_none_ve_json_uyumlu(self):
        adlar, degerler = OZET.split(': ')
        degerler = degerler.split()
        degerler[adlar.split(',').index('ugp')] = 'inf'
        metin = evalglare_metni([kaynak_satiri(1, 2000, .05, [0, 0, 1])]).replace(OZET, adlar + ': ' + ' '.join(degerler))
        _, ozet, ham = K.evalglare_coz(metin)
        self.assertIsNone(ozet['ugp'])
        self.assertEqual(ham['ugp'], 'inf')
        self.assertEqual((ozet['dgp'], ozet['ugr']), (.412, 23.5))
        json.dumps(ozet, allow_nan=False)

    def test_karanlik_arka_planda_ugr_isareti_deger_sayilmaz(self):
        adlar, degerler = OZET.split(': ')
        adlar, degerler = adlar.split(','), degerler.split()
        degerler[adlar.index('ugr')], degerler[adlar.index('lum_backg')] = '-99.000000', '0.000000'
        metin = evalglare_metni([kaynak_satiri(1, 2000, .05, [0, 0, 1])]).replace(OZET, ','.join(adlar) + ': ' + ' '.join(degerler))
        _, ozet, ham = K.evalglare_coz(metin)
        self.assertIsNone(ozet['ugr'])
        self.assertEqual(ham['ugr'], '-99.000000')
        self.assertEqual(ozet['dgp'], .412)

    def test_sentetik_cikti_cozulur(self):
        metin = evalglare_metni([kaynak_satiri(1, 2000, .05, [0, 0, 2]), kaynak_satiri(2, 30, .5, [1, 0, 0])])
        kaynaklar, ozet, ham = K.evalglare_coz('Notice: dgp below 0.2\n' + metin)
        self.assertEqual([k['no'] for k in kaynaklar], [1, 2])
        self.assertEqual(kaynaklar[0]['yon'], [0, 0, 1])
        self.assertEqual((kaynaklar[0]['parlaklik'], kaynaklar[0]['kati_aci']), (2000, .05))
        self.assertEqual((ozet['dgp'], ozet['ugr']), (.412, 23.5))
        self.assertEqual((ham['dgp'], ham['ugr']), ('0.412000', '23.500000'))

    def test_kaynak_yoksa_yer_tutucu_satir_sayilmaz(self):
        kaynaklar, ozet, _ = K.evalglare_coz(evalglare_metni([' '.join(['0.000000'] * 23)], sayi=0))
        self.assertEqual(kaynaklar, [])
        self.assertEqual(ozet['dgp'], .412)

    def test_sifir_baslik_dolu_arka_plan_ve_23_sutun(self):
        values = ('0 0.000000 0.000000 0.000000 0.000000 0.0000000000 0.000000 '
                  '0.000185 0.000093 0.000582 0.000000 5.479233 ' + '0 ' * 11)
        self.assertEqual(len(values.split()), 23)
        self.assertEqual(K.evalglare_coz(evalglare_metni([values], sayi=0))[0], [])
        with self.assertRaisesRegex(ValueError, 'özet'):
            K.evalglare_coz(evalglare_metni([values], sayi=0).replace(OZET, ''))

    def test_kesik_satir_hata(self):
        satir = kaynak_satiri(1, 2000, .05, [0, 0, 1]).rsplit(' ', 1)[0]
        with self.assertRaisesRegex(ValueError, 'kesik'):
            K.evalglare_coz(evalglare_metni([satir]))
        with self.assertRaisesRegex(ValueError, 'özet satırı kesik'):
            K.evalglare_coz(evalglare_metni([kaynak_satiri(1, 2000, .05, [0, 0, 1])]).replace(' 23.500000', ''))

    def test_sayisi_tutmayan_liste_hata(self):
        iki = [kaynak_satiri(1, 2000, .05, [0, 0, 1]), kaynak_satiri(2, 30, .5, [1, 0, 0])]
        with self.assertRaisesRegex(ValueError, 'tutmuyor'):
            K.evalglare_coz(evalglare_metni(iki, sayi=3))
        with self.assertRaisesRegex(ValueError, 'tutmuyor'):
            K.evalglare_coz(evalglare_metni(iki[:1], sayi=0))

    def test_eksik_ya_da_gecersiz_cikti_hata(self):
        dogru = evalglare_metni([kaynak_satiri(1, 2000, .05, [0, 0, 1])])
        bozuk = {'başlık yok': dogru.split('\n', 1)[1],
                 'özet yok': dogru.replace(OZET, ''),
                 'dgp aralık dışı': dogru.replace('0.412000', '1.500000'),
                 'yön sıfır': evalglare_metni([kaynak_satiri(1, 2000, .05, [0, 0, 0])]),
                 'katı açı sıfır': evalglare_metni([kaynak_satiri(1, 2000, 0, [0, 0, 1])]),
                 'sonsuz': dogru.replace('2000.000000', 'inf', 1),
                 'boş': ''}
        for ad, metin in bozuk.items():
            with self.subTest(ad), self.assertRaises(ValueError):
                K.evalglare_coz(metin)


class CisimTest(unittest.TestCase):
    SAHNE = {'mesh': {'f': [0, 1, 2, 2, 3, 0], 'm': [0, 0, 1, 1],
                      'materials': [{'name': 'zemin_mat'}, {'name': 'duvar_mat'}]},
             'lights': [{'name': 'tavan lambası'}, {'name': ''}]}

    def test_yuz_ve_isik_adlari(self):
        cikti = 'm0\tyuz0\t\nm1\tyuz1\t\nl0_m\tl0\t\nl0_light\tl0.l0.s\t\nl1_m\tl1\t\n'
        self.assertEqual(K.cisim_adlari(cikti, 5, self.SAHNE), [
            {'cisim': 'zemin_mat', 'radiance_adi': 'yuz0'}, {'cisim': 'duvar_mat', 'radiance_adi': 'yuz1'},
            {'cisim': 'tavan lambası', 'radiance_adi': 'l0'}, {'cisim': 'tavan lambası', 'radiance_adi': 'l0.l0.s'},
            {'cisim': 'l1', 'radiance_adi': 'l1'}])

    def test_eslenmeyen_kaynak_hata(self):
        for ad, cikti, sayi in (('boşluk', '*\t*\t\n', 1), ('bilinmeyen ad', 'm0\tkutu\t\n', 1),
                                ('olmayan üçgen', 'm0\tyuz9\t\n', 1), ('olmayan ışık', 'l_m\tl7\t\n', 1),
                                ('eksik satır', 'm0\tyuz0\t\n', 2), ('boş çıktı', '', 1)):
            with self.subTest(ad), self.assertRaises(ValueError):
                K.cisim_adlari(cikti, sayi, self.SAHNE)


class KomutKuralTest(unittest.TestCase):
    """Radiance gerektirmeyen giriş ve çıktı kuralları."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sahne = self.root/'sahne'
        self.sahne.mkdir()
        (self.sahne/'scene.rad').write_text('void plastic m0\n0\n0\n5 .5 .5 .5 0 0\n')
        (self.sahne/'scene.ctlux.json').write_text('{}')

    def test_cikti_kurallari_cevirle_ayni(self):
        dolu = self.root/'dolu'; dolu.mkdir(); (dolu/'keep').write_text('original')
        with self.assertRaises(FileExistsError):
            kamasma(self.sahne, dolu)
        self.assertEqual((dolu/'keep').read_text(), 'original')
        with self.assertRaisesRegex(ValueError, 'iç içe'):
            kamasma(self.sahne, self.sahne/'icinde')
        with self.assertRaisesRegex(ValueError, 'Sahne klasörü değil'):
            kamasma(self.sahne/'scene.rad', self.root/'dosya')
        with self.assertRaises(FileNotFoundError):
            kamasma(self.sahne, self.root/'yok'/'icinde')
        for ad, secenek in (('yaş', {'yas': 0}), ('kalite', {'kalite': 'rapor'}),
                            ('yön', {'vp': [0, 0, 1], 'vd': [0, 0, 0]}), ('nokta', {'vp': [0, float('nan'), 1]})):
            with self.subTest(ad), self.assertRaises(ValueError):
                kamasma(self.sahne, self.root/('arg-' + ad), **secenek)
            self.assertFalse((self.root/('arg-' + ad)).exists())
        self.assertFalse(any(p.name.startswith(('.kamasma-', 'yok', 'dosya')) for p in self.root.iterdir()))

    def test_eksik_sahne_ve_dis_komut_rapor_ile_hata(self):
        (self.sahne/'scene.rad').write_text('!echo bir komut\n')
        with self.assertRaisesRegex(ValueError, 'dış komut'):
            kamasma(self.sahne, self.root/'komut', vp=[0, 0, 1], vd=[1, 0, 0])
        (self.sahne/'scene.rad').unlink()
        with self.assertRaisesRegex(FileNotFoundError, 'scene.rad'):
            kamasma(self.sahne, self.root/'eksik', vp=[0, 0, 1], vd=[1, 0, 0])
        for ad in ('komut', 'eksik'):
            self.assertEqual([p.name for p in (self.root/ad).iterdir()], ['RAPOR.txt'])
            rapor = (self.root/ad/'RAPOR.txt').read_text()
            self.assertIn('Durum: hata', rapor)
            for satir in SINIR_SATIRLARI:
                self.assertIn('- ' + satir.rstrip('.') + '\n', rapor)

    def test_dis_komut_isaretleri_yerel_araclardan_once_reddedilir(self):
        marker = self.root/'command-marker'
        command = '!echo verified > ' + shlex.quote(str(marker)) + '\n'
        primitive = 'void plastic m0 0 0 5 .5 .5 .5 0 0'
        prefixes = ('', primitive + ' ', primitive + '\t', primitive + '\v', primitive + '\f',
                    primitive + '\r', primitive + '\x00 ', primitive + ' # comment ',
                    primitive + ' # comment\r', primitive.replace('m0 ', 'm#0 ') + ' ')
        for i, prefix in enumerate(prefixes):
            with self.subTest(prefix=repr(prefix)):
                # Tanımlayıcı içindeki # yorum değildir. Zararsız yorumdaki ! bile reddedilir.
                payload = prefix + command + '\nm0 sphere object 0 0 4 0 0 0 1\n'
                (self.sahne/'scene.rad').write_text(payload, encoding='utf-8')
                out = self.root/('command-token-%d' % i)
                with patch.object(motor, 'sh') as native:
                    with self.assertRaisesRegex(ValueError, 'dış komut'):
                        kamasma(self.sahne, out, vp=[0, 0, 1], vd=[1, 0, 0])
                native.assert_not_called()
                self.assertFalse(marker.exists())
                self.assertEqual([p.name for p in out.iterdir()], ['RAPOR.txt'])
                self.assertIn('Durum: hata', (out/'RAPOR.txt').read_text())

    @unittest.skipUnless(os.name == 'posix', 'POSIX bağ ve FIFO gerekli')
    def test_isik_bag_ve_aygitlari_yerel_araclardan_once_reddedilir(self):
        disari = self.root/'disari.dat'; disari.write_text('0\n')
        durumlar = {'dosya-bagi': lambda d: (d/'a.dat').symlink_to(disari),
                    'klasor-bagi': lambda d: (d/'alt').symlink_to(self.root),
                    'fifo': lambda d: os.mkfifo(d/'a.dat')}
        for ad, yap in durumlar.items():
            with self.subTest(ad):
                isik = self.sahne/'isik'
                shutil.rmtree(isik, ignore_errors=True); isik.mkdir()
                yap(isik)
                out = self.root/('isik-' + ad)
                with patch.object(motor, 'sh') as native:
                    with self.assertRaisesRegex(ValueError, 'normal dosya'):
                        kamasma(self.sahne, out, vp=[0, 0, 1], vd=[1, 0, 0])
                native.assert_not_called()
                self.assertIn('Durum: hata', (out/'RAPOR.txt').read_text())
        shutil.rmtree(self.sahne/'isik')
        (self.sahne/'isik').symlink_to(self.root)
        with patch.object(motor, 'sh') as native, self.assertRaisesRegex(ValueError, 'normal dosya'):
            kamasma(self.sahne, self.root/'isik-kok-bagi', vp=[0, 0, 1], vd=[1, 0, 0])
        native.assert_not_called()
        (self.sahne/'isik').unlink()
        os.mkfifo(self.sahne/'isik')
        with patch.object(motor, 'sh') as native, self.assertRaisesRegex(ValueError, 'normal dosya'):
            kamasma(self.sahne, self.root/'isik-yolu-fifo', vp=[0, 0, 1], vd=[1, 0, 0])
        native.assert_not_called()

    @unittest.skipUnless(RADIANCE and os.name == 'posix', 'Local Radiance with a POSIX shell required')
    def test_yerel_satir_ici_komut_kacisi_engellenir(self):
        marker = self.root/'inline-command-marker'
        command = '!echo verified > ' + shlex.quote(str(marker)) + '\n'
        payload = ('void plastic m0 0 0 5 .5 .5 .5 0 0 ' + command +
                   'm0 sphere object 0 0 4 0 0 0 1\n')
        # Kurulu oconv satır içi kaçışı çalıştırıyor mu, yalnız geçici dizinde doğrula.
        result = subprocess.run([shutil.which('oconv'), '-'], input=payload, text=True,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                cwd=self.root, env=motor.radiance_ortami(), timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(marker.read_text().strip(), 'verified')
        marker.unlink()
        (self.sahne/'scene.rad').write_text(payload, encoding='utf-8')
        out = self.root/'inline-command'
        # Gerçek kamaşma girişi ve yerel kabuk çağrısı. Eski denetim render öncesinde testi düşürür.
        with patch.object(motor, 'render', side_effect=AssertionError('Command must be rejected before rendering')):
            with self.assertRaisesRegex(ValueError, 'dış komut'):
                kamasma(self.sahne, out, vp=[0, 0, 1], vd=[1, 0, 0])
        self.assertFalse(marker.exists())
        self.assertEqual([p.name for p in out.iterdir()], ['RAPOR.txt'])
        self.assertIn('Durum: hata', (out/'RAPOR.txt').read_text())

    def test_bildirim_cevirisi_ve_kayip_yok(self):
        from core.__main__ import _evalglare_bildirimleri
        lines = ['Notice: Vertical illuminance is below 100 lux',
                 'Notice: Low brightness scene. dgp below 0.2'] + ['unknown %d' % i for i in range(12)]
        result = _evalglare_bildirimleri('\n'.join(lines))
        self.assertEqual(len(result), 14)
        self.assertIn('Dikey aydınlık 100 lux altında', result[0])
        self.assertIn('DGP 0.2 altında', result[1])
        self.assertIn('unknown 11', result[-1])

    def test_bildirim_tam_cumle_cevirisi(self):
        from core.__main__ import _evalglare_bildirimleri
        result = _evalglare_bildirimleri(
            'Notice: Vertical illuminance is below 100 lux !!\n'
            'Notice: Low brightness scene. dgp below 0.2! dgp might underestimate glare sources\n'
            'Notice: Low brightness scene. Vertical illuminance less than 380 lux! '
            'dgp might underestimate glare sources\n')
        kucuk = '. DGP kamaşma kaynaklarını olduğundan küçük gösterebilir'
        self.assertEqual(result, [
            'evalglare bildirimi: Bildirim: Dikey aydınlık 100 lux altında',
            'evalglare bildirimi: Bildirim: Düşük parlaklıklı sahne. DGP 0.2 altında' + kucuk,
            'evalglare bildirimi: Bildirim: Düşük parlaklıklı sahne. Dikey aydınlık 380 lux altında' + kucuk])

    def test_arac_uyarilari_dosya_sayisi_ve_ilk_uc(self):
        from core.__main__ import _arac_uyarilari
        warnings = ['ies2rad: isik/l%d.ies: warning - no lamp type' % i for i in range(235)]
        warnings += [warnings[0], 'ies2rad: isik/l0.ies: başka uyarı', 'bilinmeyen bildirim']
        rows = _arac_uyarilari(warnings)
        self.assertEqual(len(rows), 3)
        self.assertIn('235 dosyada, ilk dosyalar: isik/l0.ies, isik/l1.ies, isik/l2.ies', rows[0])
        self.assertIn('başka uyarı, 1 dosyada', rows[1])
        self.assertEqual(rows[2], 'bilinmeyen bildirim')

    def test_sifir_baslik_dolu_satir_komutta_ve_bildirimler(self):
        from core.__main__ import main
        placeholder = ('0 0 0 0 0 0 0 .000185 .000093 .000582 0 5.479233 ' + '0 ' * 11)
        def sahte(cmd, cwd=None):
            if cmd.startswith('evalglare'):
                return (0, 'Notice: Low brightness scene. dgp below 0.2\n' +
                        evalglare_metni([placeholder], sayi=0),
                        'WARNING: Vertical illuminance is below 100 lux\n' +
                        '\n'.join('bildirim %d' % i for i in range(12)))
            self.assertFalse(cmd.startswith('rtrace'))
            return (0, '', '')
        out = self.root/'dolu-yer-tutucu'
        (self.sahne/'gorunum.vf').write_text('VIEW= -vp 0 0 1 -vd 1 0 0\n')
        def png(src, dst, **kwargs):
            Path(dst).write_bytes(b'synthetic image placeholder')
        with patch.object(motor, 'sh', side_effect=sahte), patch.object(motor, 'render'), \
                patch.object(motor, 'hdr_to_png', side_effect=png):
            self.assertEqual(main(['kamasma', str(self.sahne), str(out), '--kalite', 'taslak']), 0)
        text = (out/'KAMASMA.txt').read_text()
        report = (out/'RAPOR.txt').read_text()
        data = json.loads((out/'KAMASMA.json').read_text())
        self.assertIn('Kaynak sayısı: 0', text)
        self.assertTrue(all(y['ortu_parlakligi'] == y['genel_ortu_parlakligi'] == 0
                            for y in data['yas_tablosu']))
        for expected in ('Dikey aydınlık 100 lux altında', 'DGP 0.2 altında', 'bildirim 0',
                         'bildirim 11', 'Otomatik görünüm çizim içindir'):
            self.assertIn(expected, report)
        # aralık dışında kaynak yoksa rapora sıfırlı satır düşmez.
        self.assertNotIn('kaynak genel denklemin', report)

    def test_gorunum_dosyasi_yoksa_hata(self):
        with self.assertRaisesRegex(FileNotFoundError, 'gorunum.vf'):
            kamasma(self.sahne, self.root/'gorunumsuz')
        self.assertIn('Durum: hata', (self.root/'gorunumsuz/RAPOR.txt').read_text())


def klasor_ozeti(klasor):
    return {p.relative_to(klasor).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(klasor.rglob('*')) if p.is_file()}


@unittest.skipUnless(RADIANCE, 'Yerel Radiance ve evalglare kurulumu gerekli')
class KamasmaKomutTest(unittest.TestCase):
    BAKIS = ['--vp', '0.8', '2.5', '1.2', '--vd', '1', '0', '0.6', '--kalite', 'taslak']

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sahne = self.root/'sahne'
        cevir(sentetik.kamasma_odasi(self.root/'oda.evo'), self.sahne)
        self.env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}

    def test_uctan_uca_cisim_formul_ve_yazma_siniri(self):
        once = klasor_ozeti(self.sahne)
        cikti = self.root/'cikti $(touch UNWANTED)'
        kilit = self.root/'render.lock'
        # yazma denetimi: Python tarafında her yazım çıktı klasöründe ya da çizim kilidinde olmalı.
        code = '''import sys,os,runpy
from pathlib import Path
root=Path(sys.argv[-1]).absolute();lock=Path(os.environ['CTLUX_RENDER_LOCK'])
def inside(path,fd=None):
 if isinstance(path,int): return
 p=Path(os.fsdecode(path))
 if not p.is_absolute() and fd is not None and fd>=0:
  info=os.fstat(fd)
  bases=[root,*[q for q in root.rglob('*') if q.is_dir()]]
  base=next(q for q in bases if (q.stat().st_dev,q.stat().st_ino)==(info.st_dev,info.st_ino))
  p=base/p
 p=p.resolve()
 if p!=root and root not in p.parents and p!=lock: raise AssertionError('Outside write: '+str(p))
def guard(event,args):
 if event=='open':
  path,mode,flags=args
  if flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND): inside(path)
 elif event=='os.mkdir': inside(args[0],args[2])
 elif event in ('os.remove','os.rmdir'): inside(args[0],args[1])
 elif event=='os.rename': inside(args[0],args[2]);inside(args[1],args[3])
sys.addaudithook(guard)
sys.argv=['core',*sys.argv[1:]]
runpy.run_module('core',run_name='__main__')
'''
        r = subprocess.run([sys.executable, '-B', '-c', code, 'kamasma', str(self.sahne), *self.BAKIS,
                            '--yas', '80', str(cikti)], cwd=ROOT, capture_output=True, text=True,
                           timeout=300, env={**self.env, 'CTLUX_RENDER_LOCK': str(kilit)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((ROOT/'UNWANTED').exists())
        self.assertFalse(list(self.root.rglob('UNWANTED')))
        self.assertEqual(sorted(p.name for p in cikti.iterdir()),
                         ['KAMASMA.json', 'KAMASMA.txt', 'RAPOR.txt', 'goz.png', 'isaretli.png'])
        for png in ('goz.png', 'isaretli.png'):
            self.assertEqual((cikti/png).read_bytes()[:8], b'\x89PNG\r\n\x1a\n')
        self.assertEqual(once, klasor_ozeti(self.sahne))
        sonuc = json.loads((cikti/'KAMASMA.json').read_text())
        kaynaklar = sonuc['kaynaklar']
        self.assertEqual(sonuc['kaynak_sayisi'], len(kaynaklar))
        self.assertGreaterEqual(len(kaynaklar), 1)
        lamba = kaynaklar[0]
        self.assertEqual((lamba['cisim'], lamba['radiance_adi'], lamba['aralikta']), ('tavan lambası', 'l0', True))
        # lamba (2, 2.5, 2.8), gözden bakış yönüne açı 21.8 derece.
        beklenen_aci = math.degrees(math.acos((1.2 + 1.6 * .6) / (2 * math.sqrt(1.36))))
        self.assertAlmostEqual(lamba['aci'], beklenen_aci, delta=1)
        self.assertTrue(all(k['cisim'] and k['radiance_adi'] for k in kaynaklar))
        # formül JSON alanlarından bağımsız yeniden hesaplanır.
        toplam = 0
        for k in kaynaklar:
            E = k['parlaklik'] * k['kati_aci'] * math.cos(math.radians(k['aci']))
            self.assertAlmostEqual(k['aydinlik'], E, delta=1e-6 * max(1, E))
            self.assertEqual(k['aralikta'], 1 < k['aci'] < 30)
            if k['aralikta']:
                toplam += 10 * E / k['aci'] ** 2
            else:
                self.assertIsNone(k['katki'])
        self.assertAlmostEqual(sonuc['yas_carpani_oncesi_toplam'], toplam, delta=1e-9 * toplam)
        seksen = next(y for y in sonuc['yas_tablosu'] if y['yas'] == 80)
        self.assertAlmostEqual(seksen['ortu_parlakligi'], toplam * (1 + (80 / 70) ** 4), delta=1e-9 * toplam)
        disarida = sum(not k['aralikta'] for k in kaynaklar)
        self.assertEqual(sonuc['aralik_disi_sayisi'], disarida)
        text = (cikti/'KAMASMA.txt').read_text()
        self.assertIn('Aralık dışında kalan kaynak (1 < açı < 30 derece): %d' % disarida, text)
        self.assertIn('evalglare DGP (0 ile 1 arası, birimsiz): ' + sonuc['evalglare']['ham']['dgp'], text)
        self.assertIn('tavan lambası', text)
        for satir in SINIR_SATIRLARI:
            self.assertIn(satir, sonuc['sinirlar'])
        self.assertIn('SINIRLAR', text)
        for satir in SINIR_SATIRLARI:
            self.assertIn(satir.rstrip('.'), text)
        rapor = (cikti/'RAPOR.txt').read_text()
        for satir in SINIR_SATIRLARI:
            self.assertIn('- ' + satir.rstrip('.') + '\n', rapor)
        if disarida:
            self.assertIn('%d kaynak 1 < açı < 30 derece koşulu dışında' % disarida, rapor)
        self.assertFalse(list(cikti.glob('.kamasma-*')))

    def test_gorunum_dosyasi_ve_kaynaksiz_bakis(self):
        # otomatik kamera odanın dışından bakar, kaynak yoksa değer uydurulmaz, sonuç sıfırdır.
        r = subprocess.run([sys.executable, '-B', '-m', 'core', 'kamasma', str(self.sahne),
                            str(self.root/'dis'), '--kalite', 'taslak'],
                           cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=300)
        self.assertEqual(r.returncode, 0, r.stderr)
        sonuc = json.loads((self.root/'dis/KAMASMA.json').read_text())
        self.assertEqual(sonuc['bakis']['kaynak'], 'gorunum.vf')
        self.assertEqual((sonuc['kaynak_sayisi'], sonuc['kaynaklar']), (0, []))
        self.assertTrue(all(y['ortu_parlakligi'] == 0 for y in sonuc['yas_tablosu']))
        report = (self.root/'dis/RAPOR.txt').read_text()
        self.assertIn('kamaşma kaynağı bulunmadı', report)
        self.assertIn('Otomatik görünüm çizim içindir', report)
        self.assertTrue(all(y['genel_ortu_parlakligi'] == 0 for y in sonuc['yas_tablosu']))

    def test_evalglare_ya_da_esleme_basarisizsa_uydurma_yok(self):
        gercek = motor.sh
        for ad, bozan in (('evalglare', lambda cmd: (1, '', 'evalglare çöktü') if cmd.startswith('evalglare') else None),
                          ('rtrace', lambda cmd: (0, '*\t*\t\n' * 20, '') if cmd.startswith('rtrace') else None),
                          ('eksik rtrace', lambda cmd: (0, '', '') if cmd.startswith('rtrace') else None)):
            with self.subTest(ad):
                def sahte(cmd, cwd=None):
                    return bozan(cmd) or gercek(cmd, cwd=cwd)
                cikti = self.root/('bozuk ' + ad)
                with patch.object(motor, 'sh', side_effect=sahte), self.assertRaises((RuntimeError, ValueError)):
                    kamasma(self.sahne, cikti, vp=[0.8, 2.5, 1.2], vd=[1, 0, 0.6], kalite='taslak')
                self.assertEqual([p.name for p in cikti.iterdir()], ['RAPOR.txt'])
                self.assertIn('Durum: hata', (cikti/'RAPOR.txt').read_text())

    def test_teslimde_hata_sonucu_geri_alir(self):
        from core.__main__ import main
        from contextlib import redirect_stderr
        import io
        move = shutil.move
        tasinan = []
        def bozuk(src, dst):
            tasinan.append(dst)
            if len(tasinan) == 2:
                raise OSError('taşıma bozuldu')
            return move(src, dst)
        cikti = self.root/'teslim-hatasi'
        stderr = io.StringIO()
        with patch.object(shutil, 'move', side_effect=bozuk), redirect_stderr(stderr):
            self.assertEqual(main(['kamasma', str(self.sahne), str(cikti), *self.BAKIS]), 1)
        self.assertEqual(stderr.getvalue().strip(), 'Hata: taşıma bozuldu')
        self.assertEqual([p.name for p in cikti.iterdir()], ['RAPOR.txt'])
        rapor = (cikti/'RAPOR.txt').read_text()
        self.assertIn('Durum: hata', rapor)
        self.assertIn('taşıma bozuldu', rapor)

    @unittest.skipUnless(os.name == 'posix', 'POSIX sinyal testi')
    def test_teslimde_iptal_sonucu_geri_alir(self):
        from core.__main__ import main
        from contextlib import redirect_stderr
        import io
        move = shutil.move
        handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        for sig in handlers:
            with self.subTest(signal=sig):
                cikti = self.root/('iptal-' + str(sig))
                def kesilen(src, dst):
                    move(src, dst)
                    os.kill(os.getpid(), sig)
                stderr = io.StringIO()
                with patch.object(shutil, 'move', side_effect=kesilen), redirect_stderr(stderr):
                    self.assertEqual(main(['kamasma', str(self.sahne), str(cikti), *self.BAKIS]), 128 + sig)
                self.assertEqual(stderr.getvalue().strip(), 'İptal edildi.')
                self.assertEqual([p.name for p in cikti.iterdir()], ['RAPOR.txt'])
                self.assertIn('Durum: iptal', (cikti/'RAPOR.txt').read_text())
                self.assertEqual({s: signal.getsignal(s) for s in handlers}, handlers)


class EnglishGlareCLITest(unittest.TestCase):
    def test_english_flags_and_quality_reach_backend(self):
        from core import __main__ as cli
        for quality, internal in (('draft', 'taslak'), ('medium', 'orta'), ('final', 'final')):
            with self.subTest(quality=quality), patch.object(cli, 'kamasma', return_value={'output': 'out'}) as glare:
                self.assertEqual(cli.main(['glare', 'scene', 'out', '--age', '60', '--quality', quality,
                                           '--eye-pigmentation', '1.2', '--vp', '1', '2', '3', '--vd', '0', '1', '0']), 0)
                glare.assert_called_once_with('scene', 'out', vp=[1.0, 2.0, 3.0], vd=[0.0, 1.0, 0.0],
                                              yas=60, kalite=internal, goz_rengi=1.2)

    def test_invalid_quality_reports_english_choices(self):
        from core import __main__ as cli
        import contextlib
        import io
        error_text = io.StringIO()
        with patch.object(cli, 'kamasma') as glare, contextlib.redirect_stderr(error_text):
            with self.assertRaises(SystemExit) as error:
                cli.main(['glare', 'scene', 'out', '--quality', 'invalid'])
        self.assertEqual(error.exception.code, 2)
        self.assertIn('quality must be draft, medium or final', error_text.getvalue())
        glare.assert_not_called()

    def test_existing_flags_preserve_backend_contract(self):
        from core import __main__ as cli
        with patch.object(cli, 'kamasma', return_value={'output': 'out'}) as glare:
            self.assertEqual(cli.main(['kamasma', 'scene', 'out', '--yas', '80', '--kalite', 'taslak',
                                       '--goz-rengi', '0']), 0)
            glare.assert_called_once_with('scene', 'out', vp=None, vd=None, yas=80, kalite='taslak', goz_rengi=0.0)


if __name__ == '__main__':
    unittest.main()
