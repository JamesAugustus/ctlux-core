# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Sentetik EVO kimliği ve salt okunur metadata, özel proje ya da veri gerekmez."""
from copy import deepcopy
from pathlib import Path
import json
import math
import os
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
import warnings
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
from core.step_reader import alanlar, kayitlar
from core import engine as motor
from core import importer as ice_aktarma
from core import format_detector as dedektif
from core import evo_identity as evo_kimlik

STEP = """
#1=CoordSys3D((10.0000000001,20.2,4.125),(0,1,0),(-1,0,0),(0,0,1));
#4=CoordSys3D((2.125,0.125,1.5),(1,0,0),(0,1,0),(0,0,1));
#5=CoordSys3D((0.123456789,-0.987654321,0.000009876),(0.6,0,-0.8),(0,1,0),(0.8,0,0.6));
#6=CoordSys3D((1,2,3),(1.2,0,-1.6),(0,2,0),(1.6,0,1.2));
#10=Storey(10,'kat',(),#1,$,$);
#50=LuminaireArrangement(500,'11111111-1111-1111-1111-111111111111',(#502),#4,$,$);
#502=CommonInformationPropertySet(502,'',#50,'Dizi A',$,$);
#60=LuminaireElement(77,'22222222-2222-2222-2222-222222222222',(#602),#5,$,$);
#61=LuminaireElement(78,'33333333-3333-3333-3333-333333333333',(#612),#6,$,$);
#62=LuminaireElement(79,'44444444-4444-4444-4444-444444444444',(),$,$,$);
#602=CommonInformationPropertySet(602,'',#60,'Aynı ''ışık'' #999',$,$);
#612=CommonInformationPropertySet(612,'',#61,'Aynı ''ışık'' #999',$,$);
#70=RelContainedInSpatialStructure(70,'metin #999',#10,(#50,#60,#61,#62));
#73=RelAggregates(73,'',#50,(#60,#61,#62));
#100=LuminairePrototype(901,'55555555-5555-5555-5555-555555555555',(#110),#101,$);
#101=PrototypeGeometricRepresentation(#100,#102);
#102=LuminairePrototypeRepresentationData(#100,0,(),(),False,(1,1,1));
#110=ProductDataPropertySet(110,'',#100,#111);
#111=ProductData('DEMO-01','',#112,$);
#112=LanguageDependentTextContainer(((1055,((.ArticleName.,'Sentetik gömme'),(.FileName.,'dosya adı değil'))),(1033,((.ArticleName.,'Demo downlight')))));
#120=RelDefinesByPrototype(120,'metin #999',(#60,#61,#62),#100);
#130=LightDistributionConnection(#102,0,#131,#132);
#131=LightEmittingPart(#102,0);
#132=LightDistribution(#133);
#133=LightDistributionData((),(0),(0,90,180),(100,100,100));
#134=LampTypeChannel(#102,0,1,1256);
"""


class StepOkuyucuTest(unittest.TestCase):
    def test_tirnakli_ayraclar_kacislar_ve_cok_satirli_kayitlar(self):
        body = "\n'first ''quoted; #9=Fake();'' line\nlast',(#2,(3,4)),''\n"
        text = ("HEADER; FILE_DESCRIPTION(('ignore #98=Fake();'),'2;1'); ENDSEC; DATA;\n"
                "#001\n=\nThing_α\n(" + body + ") \n; #2=Empty(); #٣=نوع_٣(,1,,); ENDSEC;")
        with warnings.catch_warnings(record=True) as notes:
            warnings.simplefilter('always')
            result = kayitlar(text)
        self.assertEqual(result, {1: ('Thing_α', body), 2: ('Empty', ''), 3: ('نوع_٣', ',1,,')})
        self.assertEqual(alanlar(body), ["'first ''quoted; #9=Fake();'' line\nlast'", '(#2,(3,4))', "''"])
        self.assertEqual(alanlar(result[3][1]), ['', '1', ''])
        self.assertFalse(notes)

    def test_bozuk_kayittan_tirnak_disindaki_noktali_virgulle_cikis(self):
        for malformed in ('#1=A(1;', '#1=A(1));', '#1=A((1);', '#1=garbage;', '#1=(1);'):
            with self.subTest(malformed=malformed), warnings.catch_warnings(record=True) as notes:
                warnings.simplefilter('always')
                result = kayitlar(malformed + "\n#2=B('ok; #99=Data()');")
            self.assertEqual(result, {2: ('B', "'ok; #99=Data()'")})
            self.assertEqual(len(notes), 1)
            self.assertIs(notes[0].category, RuntimeWarning)
            self.assertEqual(str(notes[0].message), 'STEP kesik/bozuk kayıt atlandı: #1')

    def test_dosya_sonu_tirnakli_metni_kayit_saymadan_bir_uyari_verir(self):
        for tail in ("#2=B('cut", '#2=B(1)', '#2=B(', '#2=', "#2=B('cut\n#9=Fake();"):
            with self.subTest(tail=tail), warnings.catch_warnings(record=True) as notes:
                warnings.simplefilter('always')
                result = kayitlar("#1=A('x; #8=Fake();'); " + tail)
            self.assertEqual(result, {1: ('A', "'x; #8=Fake();'")})
            self.assertEqual(len(notes), 1)
            self.assertIs(notes[0].category, RuntimeWarning)
            self.assertEqual(str(notes[0].message), 'STEP kesik/bozuk kayıt atlandı: #2')

    def test_tekrar_kimlik_hata_sozlesmesi(self):
        for second in ('#1=B();', '#1=B((1);'):
            with self.subTest(second=second), self.assertRaisesRegex(ValueError, 'Tekrarlanan STEP kayıt kimliği: 1'):
                kayitlar('#01=A(); ' + second)
        with warnings.catch_warnings(record=True) as notes:
            warnings.simplefilter('always')
            self.assertEqual(kayitlar('#1=A(); #1=garbage;'), {1: ('A', '')})
        self.assertEqual(len(notes), 1)
        self.assertEqual(str(notes[0].message), 'STEP kesik/bozuk kayıt atlandı: #1')

    def test_yorum_tirnak_durumunu_degistirmez_kayit_eklemez(self):
        text = ("HEADER;\n/* exported by O'Brien tool */\nFILE_NAME('a');\nENDSEC;\nDATA;\n"
                "/* #5=Z(9); */ #1=A('a' /* ' */ ,2);\n#2=B('x /* kept */');\nENDSEC;")
        with warnings.catch_warnings(record=True) as notes:
            warnings.simplefilter('always')
            result = kayitlar(text)
        self.assertEqual(sorted(result), [1, 2])
        self.assertEqual(alanlar(result[1][1]), ["'a'", '2'])
        self.assertEqual(alanlar(result[2][1]), ["'x /* kept */'"])
        self.assertFalse(notes)
        with warnings.catch_warnings(record=True) as notes:
            warnings.simplefilter('always')
            self.assertEqual(kayitlar("#1=A(1); /* open"), {1: ('A', '1')})
        self.assertFalse(notes)
        with self.assertRaises(ValueError):
            alanlar("1, /* open")

    def test_asiri_uzun_kimlik_uyariyla_atlanir(self):
        with warnings.catch_warnings(record=True) as notes:
            warnings.simplefilter('always')
            result = kayitlar('#' + '1' * 5000 + '=A(1); #2=B(2);')
        self.assertEqual(result, {2: ('B', '2')})
        self.assertEqual([str(n.message) for n in notes], ['STEP kesik/bozuk kayıt atlandı: #' + '1' * 18 + '...'])

    def test_alan_dogrulama_hata_sozlesmesi(self):
        for body, message in [("1,'cut", 'Kesilmiş STEP alanı'), ('1,(2,3', 'Kesilmiş STEP alanı'),
                              ('1),2', 'STEP parantezi eşleşmiyor')]:
            with self.subTest(body=body), self.assertRaisesRegex(ValueError, message):
                alanlar(body)

    def test_uzun_bozuk_kayitlar_sure_sinirli_alt_surecte_biter(self):
        # Alt süreç zaman aşımı, makine hızını ölçmeden aşırı ayrıştırma süresini yakalar.
        script = """
import warnings
from core.step_reader import kayitlar
for text in ["#1=A(" + chr(39) * 200000,
             "#1=A('" + "x" * 200000 + "#99=Hidden();",
             "#1=A(" + "(" * 100000 + ");",
             "#1=" + "A" * 200000]:
    with warnings.catch_warnings(record=True) as notes:
        warnings.simplefilter('always')
        assert kayitlar(text) == {}
    assert len(notes) == 1
    assert notes[0].category is RuntimeWarning
    assert str(notes[0].message) == 'STEP kesik/bozuk kayıt atlandı: #1'
body = chr(39) + chr(39) * 200000 + chr(39)
assert kayitlar('#2=A(' + body + ');') == {2: ('A', body)}
"""
        result = subprocess.run([sys.executable, '-c', script], cwd=ROOT, capture_output=True,
                                text=True, timeout=10,
                                env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class EvoKimlikTest(unittest.TestCase):
    def setUp(self):
        (ROOT / 'tests/.tmp').mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / 'tests/.tmp')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.pd = self.root / 'proje'
        (self.pd / 'model').mkdir(parents=True)
        self.src = self.pd / 'model' / 'sentetik.evo'
        self.write_evo()
        self.arms = [{'ad': 'armatur_77', 'kaynak': 'evo/STEP', 'x': 99.25,
                      'y': -7.0, 'z': 2.0, 'yon': [1, 0, 0], 'renk': [.2, .3, .4],
                      'guc': 123.0, 'lumen': 321.0, 'urun_rad': '_cache/sample.rad'}]
        self.project = {'geometri': [], 'armaturler': deepcopy(self.arms)}
        (self.pd / 'proje.json').write_text(json.dumps(self.project), encoding='utf-8')
        with evo_kimlik._lock:
            evo_kimlik._cache.clear()

    def write_evo(self, text=STEP, path=None):
        with zipfile.ZipFile(path or self.src, 'w') as z:
            z.writestr('Project/ProjectData/ProjectData.dat', text)

    def fake_ies2rad(self, command, cwd):
        args = shlex.split(command)
        target = Path(cwd) / args[args.index('-o') + 1]
        target.with_suffix('.rad').write_text('# Sentetik dönüştürücü çıktısı\n')
        target.with_suffix('.dat').write_text('0\n')
        return 0, '', ''

    def test_instance_guid_urun_ve_dizi_ayri_kalir(self):
        records = motor.evo_armaturler(str(self.src))
        self.assertEqual(len(records), 3)
        self.assertEqual([a['kaynak_id'] for a in records], ['77', '78', '79'])
        self.assertEqual(records[0]['ad'], 'armatur_77')
        self.assertEqual(records[0]['kaynak_kayit'], '#60')
        self.assertEqual(len({a['kaynak_guid'] for a in records}), 3)
        self.assertEqual({a['urun_id'] for a in records}, {'901'})
        self.assertEqual({a['dizi_id'] for a in records}, {'500'})
        self.assertEqual(records[0]['dizi_ad'], 'Dizi A')
        self.assertEqual(records[0]['kaynak_ad'], "Aynı 'ışık' #999")
        self.assertEqual(records[0]['kaynak_ad'], records[1]['kaynak_ad'])
        self.assertNotEqual(records[0]['ad'], records[1]['ad'])
        self.assertNotIn('kaynak_ad', records[2])  # GUID görünen ada dönüşmez.

    def test_urun_adi_cokdil_ve_kod_korunur(self):
        record = motor.evo_armaturler(str(self.src))[0]
        self.assertEqual(record['urun_ad'], 'Sentetik gömme')
        self.assertEqual(record['urun_adlari'], [
            {'dil': '1055', 'ad': 'Sentetik gömme'}, {'dil': '1033', 'ad': 'Demo downlight'}])
        self.assertEqual(record['urun_kod'], 'DEMO-01')
        self.assertNotIn('dosya adı değil', str(record['urun_adlari']))
        self.assertEqual(ice_aktarma._evo_metin(r"'\X2\0130015F0131006B\X0\'"), 'İşık')

    def test_sira_numarasi_degisse_kalici_esleme_degismez(self):
        before = motor.evo_armaturler(str(self.src))
        # kaynak adının içindeki #999 veri, sadece dosya referansları kayar.
        from core.step_reader import kayitlar
        ents = kayitlar(STEP)
        rebuilt = []
        for i, (kind, body) in ents.items():
            parts = re.split(r"('(?:[^']|'')*')", body)
            body = ''.join(part if n % 2 else re.sub(r'#(\d+)', lambda m: '#' + str(int(m[1]) + 1000), part)
                           for n, part in enumerate(parts))
            rebuilt.append('#%d=%s(%s);' % (i + 1000, kind, body))
        self.write_evo('\n'.join(rebuilt))
        after = motor.evo_armaturler(str(self.src))
        for old, new in zip(before, after):
            for field in ('ad', 'kaynak_id', 'kaynak_guid', 'kaynak_ad', 'urun_id', 'urun_ad', 'dizi_id', 'x', 'y', 'z', 'yon'):
                self.assertEqual(old.get(field), new.get(field))
            self.assertNotEqual(old['kaynak_kayit'], new['kaynak_kayit'])

    def test_kat_dizi_yerel_carpimi_hassasiyet_ve_normalizasyon(self):
        records = motor.evo_armaturler(str(self.src))
        a = records[0]
        expected = (10.8626543211, 22.448456789, 5.625009876)
        for field, value in zip(('x', 'y', 'z'), expected):
            self.assertAlmostEqual(a[field], value, places=11)
        self.assertEqual(a['kaynak_dunya_matrisi'][12:15], [a['x'], a['y'], a['z']])
        self.assertEqual(a['kaynak_dunya_matrisi'][15], 1.0)
        for record in records[:2]:
            self.assertAlmostEqual(math.hypot(*record['yon']), 1.0)
            for actual, target in zip(record['yon'], (0, -.8, -.6)):
                self.assertAlmostEqual(actual, target)

    def test_ayni_kalici_id_ve_celisik_prototip_reddedilir(self):
        self.write_evo(STEP.replace('LuminaireElement(78,', 'LuminaireElement(77,'))
        with self.assertRaisesRegex(ValueError, 'Tekrarlanan'):
            motor.evo_armaturler(str(self.src))
        self.write_evo(STEP + "#200=LuminairePrototype(902,'ikinci',(),$,$);#201=RelDefinesByPrototype(201,'',(#60),#200);")
        with self.assertRaisesRegex(ValueError, 'çelişiyor'):
            motor.evo_armaturler(str(self.src))

    def test_sonlu_olmayan_ve_sifir_yon_tahmin_edilmez(self):
        for value in ('NaN', '1e999'):
            with self.subTest(value=value):
                self.write_evo(STEP.replace('0.123456789', value))
                with self.assertRaisesRegex(ValueError, 'sonlu'):
                    motor.evo_armaturler(str(self.src))
        self.write_evo(STEP.replace('(0.8,0,0.6)', '(0,0,0)'))
        with self.assertRaisesRegex(ValueError, 'yönü geçersiz'):
            motor.evo_armaturler(str(self.src))

    def test_fotometri_ayni_urun_farkli_instance_kimlikleriyle_eslesir(self):
        with patch.object(motor, 'sh', side_effect=self.fake_ies2rad) as run:
            products = motor.evo_ies(str(self.src), str(self.pd))
        self.assertEqual(set(products), {'77', '78', '79'})
        self.assertEqual(len({p['rad'] for p in products.values()}), 1)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(products['77']['kaynak_id'], '77')
        self.assertEqual(products['77']['urun_id'], '901')
        self.assertEqual(products['77']['urun_ad'], 'Sentetik gömme')

    def test_yerlestirme_adin_sonundaki_sayiya_baglanmaz(self):
        samples = [({'ad': 'kullanıcı adı_999', 'kaynak_id': '77'}, True),
                   ({'ad': 'armatur_77'}, True),
                   ({'ad': 'kullanıcı adı_77'}, False),
                   ({'ad': 'armatur_77', 'kaynak_id': '999'}, False)]
        for record, matched in samples:
            with self.subTest(record=record):
                errors = []
                with patch.object(motor, 'evo_armaturler', return_value=[deepcopy(record)]), \
                     patch.object(motor, 'evo_ies', return_value={'77': {'rad': '_cache/toy.rad', 'lumen': 1000}}):
                    _, arms = dedektif._evo_yerlestir(str(self.src), str(self.pd),
                        str(self.pd / 'model/toy.obj'), [], errors)
                self.assertEqual('urun_rad' in arms[0], matched)

    def test_salt_okunur_zenginlestirme_fiziksel_degerleri_korumali(self):
        source_before = self.src.read_bytes()
        project_before = (self.pd / 'proje.json').read_bytes()
        arms_before = deepcopy(self.arms)
        records, notes = evo_kimlik.zenginlestir(str(self.pd), self.project, self.arms)
        self.assertFalse(notes)
        for key, value in arms_before[0].items():
            self.assertEqual(records[0][key], value)
        self.assertEqual(records[0]['kaynak_id'], '77')
        self.assertEqual(records[0]['urun_ad'], 'Sentetik gömme')
        self.assertEqual(self.arms, arms_before)
        self.assertEqual(self.src.read_bytes(), source_before)
        self.assertEqual((self.pd / 'proje.json').read_bytes(), project_before)
        self.assertFalse((self.pd / '_cache').exists())

    def test_mevcut_metadata_ve_cache_kopyasi_degismez(self):
        self.arms[0].update({'urun_ad': 'kullanıcı tercihi', 'kaynak_ad': None})
        first, _ = evo_kimlik.zenginlestir(str(self.pd), self.project, self.arms)
        self.assertEqual(first[0]['urun_ad'], 'kullanıcı tercihi')
        self.assertIsNone(first[0]['kaynak_ad'])
        first[0]['urun_adlari'][0]['ad'] = 'yalnız çıktı değişti'
        first[0]['yon'][0] = 99
        second, _ = evo_kimlik.zenginlestir(str(self.pd), self.project, self.arms)
        self.assertEqual(second[0]['urun_adlari'][0]['ad'], 'Sentetik gömme')
        self.assertEqual(second[0]['yon'], [1, 0, 0])

    def test_explicit_kaynak_tek_aday_aramasina_ustundur(self):
        self.write_evo(path=self.pd / 'model/ikinci.evo')
        for project in ({'kaynak_evo': 'model/sentetik.evo'},
                        {'ice_aktarma': {'kaynak_evo': 'model/sentetik.evo'}}):
            records, notes = evo_kimlik.zenginlestir(str(self.pd), project, self.arms)
            self.assertFalse(notes)
            self.assertIn('urun_ad', records[0])

    def test_eksik_coklu_bozuk_kaynak_orijinali_korur(self):
        self.src.unlink()
        for situation in ('missing', 'multiple', 'broken'):
            if situation == 'multiple':
                self.write_evo(); self.write_evo(path=self.pd / 'model/ikinci.evo')
            elif situation == 'broken':
                (self.pd / 'model/ikinci.evo').unlink(); self.src.write_bytes(b'not a zip')
            records, notes = evo_kimlik.zenginlestir(str(self.pd), self.project, self.arms)
            self.assertEqual(records, self.arms)
            self.assertTrue(notes)
            self.assertNotIn('sentetik.evo', str(notes))

    def test_mutlak_disari_tasan_ve_symlink_kaynak_reddedilir(self):
        outside = self.root / 'disarida.evo'; self.write_evo(path=outside)
        link = self.pd / 'model/disari.evo'; link.symlink_to(outside)
        for ref in (str(outside), '../disarida.evo', 'model/disari.evo', 'model/no.evo'):
            with patch.object(motor, 'evo_armaturler') as parse:
                records, notes = evo_kimlik.zenginlestir(str(self.pd), {'kaynak_evo': ref}, self.arms)
                self.assertEqual(records, self.arms); self.assertTrue(notes); parse.assert_not_called()

    def test_yabanci_format_ve_keyfi_ad_evo_olarak_tahmin_edilmez(self):
        records = [{'ad': 'armatur_77', 'kaynak': 'usd', 'kaynak_id': '77'},
                   {'ad': 'masa_77'}, {'ad': 'kullanıcı', 'kaynak': 'evo/STEP'}]
        with patch.object(motor, 'evo_armaturler') as parse:
            after, notes = evo_kimlik.zenginlestir(str(self.pd), self.project, records)
            self.assertEqual(after, records); self.assertFalse(notes); parse.assert_not_called()

    def test_yeniden_adlandirilmis_id_eslesir_eski_ad_idyi_ezmez(self):
        records = [{'ad': 'değişmiş', 'kaynak': 'evo/STEP', 'kaynak_id': '77'},
                   {'ad': 'armatur_77', 'kaynak': 'evo/STEP', 'kaynak_id': '999'},
                   {'ad': 'armatur_78'}]
        after, notes = evo_kimlik.zenginlestir(str(self.pd), self.project, records)
        self.assertEqual(after[0]['urun_id'], '901')
        self.assertNotIn('urun_id', after[1]); self.assertTrue(notes)
        self.assertEqual(after[2]['kaynak_id'], '78')

    def test_cache_size_mtime_ve_en_fazla_sekiz_kaynak(self):
        with patch.object(motor, 'evo_armaturler', wraps=motor.evo_armaturler) as parse:
            for _ in range(2):
                evo_kimlik.zenginlestir(str(self.pd), self.project, self.arms)
            self.assertEqual(parse.call_count, 1)
            stat = self.src.stat(); os.utime(self.src, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1000000))
            evo_kimlik.zenginlestir(str(self.pd), self.project, self.arms)
            self.assertEqual(parse.call_count, 2)
            self.write_evo(STEP + '\n')
            evo_kimlik.zenginlestir(str(self.pd), self.project, self.arms)
            self.assertEqual(parse.call_count, 3)
            for index in range(9):
                source = self.pd / ('source%d.evo' % index); self.write_evo(path=source)
                evo_kimlik.zenginlestir(str(self.pd), {'kaynak_evo': source.name}, self.arms)
            self.assertEqual(len(evo_kimlik._cache), 8)
            self.assertTrue(all(Path(key[0]).is_absolute() for key in evo_kimlik._cache))


if __name__ == '__main__':
    unittest.main()
