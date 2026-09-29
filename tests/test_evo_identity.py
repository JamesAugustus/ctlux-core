# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Synthetic EVO identity and read-only metadata; no private projects or data required."""
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
from core.step_reader import parse_fields, parse_records
from core import engine as engine
from core import importer as importer
from core import format_detector as format_detector
from core import evo_identity as evo_identity

STEP = """
#1=CoordSys3D((10.0000000001,20.2,4.125),(0,1,0),(-1,0,0),(0,0,1));
#4=CoordSys3D((2.125,0.125,1.5),(1,0,0),(0,1,0),(0,0,1));
#5=CoordSys3D((0.123456789,-0.987654321,0.000009876),(0.6,0,-0.8),(0,1,0),(0.8,0,0.6));
#6=CoordSys3D((1,2,3),(1.2,0,-1.6),(0,2,0),(1.6,0,1.2));
#10=Storey(10,'storey',(),#1,$,$);
#50=LuminaireArrangement(500,'11111111-1111-1111-1111-111111111111',(#502),#4,$,$);
#502=CommonInformationPropertySet(502,'',#50,'Array A',$,$);
#60=LuminaireElement(77,'22222222-2222-2222-2222-222222222222',(#602),#5,$,$);
#61=LuminaireElement(78,'33333333-3333-3333-3333-333333333333',(#612),#6,$,$);
#62=LuminaireElement(79,'44444444-4444-4444-4444-444444444444',(),$,$,$);
#602=CommonInformationPropertySet(602,'',#60,'Same ''Light α'' #999',$,$);
#612=CommonInformationPropertySet(612,'',#61,'Same ''Light α'' #999',$,$);
#70=RelContainedInSpatialStructure(70,'text #999',#10,(#50,#60,#61,#62));
#73=RelAggregates(73,'',#50,(#60,#61,#62));
#100=LuminairePrototype(901,'55555555-5555-5555-5555-555555555555',(#110),#101,$);
#101=PrototypeGeometricRepresentation(#100,#102);
#102=LuminairePrototypeRepresentationData(#100,0,(),(),False,(1,1,1));
#110=ProductDataPropertySet(110,'',#100,#111);
#111=ProductData('DEMO-01','',#112,$);
#112=LanguageDependentTextContainer(((1055,((.ArticleName.,'Synthetic downlight α'),(.FileName.,'not a filename'))),(1033,((.ArticleName.,'Demo downlight')))));
#120=RelDefinesByPrototype(120,'text #999',(#60,#61,#62),#100);
#130=LightDistributionConnection(#102,0,#131,#132);
#131=LightEmittingPart(#102,0);
#132=LightDistribution(#133);
#133=LightDistributionData((),(0),(0,90,180),(100,100,100));
#134=LampTypeChannel(#102,0,1,1256);
"""


class StepReaderTest(unittest.TestCase):
    def test_quoted_separators_escapes_and_multiline_records(self):
        body = "\n'first ''quoted; #9=Fake();'' line\nlast',(#2,(3,4)),''\n"
        text = ("HEADER; FILE_DESCRIPTION(('ignore #98=Fake();'),'2;1'); ENDSEC; DATA;\n"
                "#001\n=\nThing_α\n(" + body + ") \n; #2=Empty(); #٣=نوع_٣(,1,,); ENDSEC;")
        with warnings.catch_warnings(record=True) as notes:
            warnings.simplefilter('always')
            result = parse_records(text)
        self.assertEqual(result, {1: ('Thing_α', body), 2: ('Empty', ''), 3: ('نوع_٣', ',1,,')})
        self.assertEqual(parse_fields(body), ["'first ''quoted; #9=Fake();'' line\nlast'", '(#2,(3,4))', "''"])
        self.assertEqual(parse_fields(result[3][1]), ['', '1', ''])
        self.assertFalse(notes)

    def test_invalid_record_recovers_after_unquoted_semicolon(self):
        for malformed in ('#1=A(1;', '#1=A(1));', '#1=A((1);', '#1=garbage;', '#1=(1);'):
            with self.subTest(malformed=malformed), warnings.catch_warnings(record=True) as notes:
                warnings.simplefilter('always')
                result = parse_records(malformed + "\n#2=B('ok; #99=Data()');")
            self.assertEqual(result, {2: ('B', "'ok; #99=Data()'")})
            self.assertEqual(len(notes), 1)
            self.assertIs(notes[0].category, RuntimeWarning)
            self.assertEqual(str(notes[0].message), 'STEP truncated/invalid record skipped: #1')

    def test_eof_warns_once_without_resyncing_inside_quoted_text(self):
        for tail in ("#2=B('cut", '#2=B(1)', '#2=B(', '#2=', "#2=B('cut\n#9=Fake();"):
            with self.subTest(tail=tail), warnings.catch_warnings(record=True) as notes:
                warnings.simplefilter('always')
                result = parse_records("#1=A('x; #8=Fake();'); " + tail)
            self.assertEqual(result, {1: ('A', "'x; #8=Fake();'")})
            self.assertEqual(len(notes), 1)
            self.assertIs(notes[0].category, RuntimeWarning)
            self.assertEqual(str(notes[0].message), 'STEP truncated/invalid record skipped: #2')

    def test_duplicate_identity_error_contract(self):
        for second in ('#1=B();', '#1=B((1);'):
            with self.subTest(second=second), self.assertRaisesRegex(ValueError, 'Duplicate STEP record identity: 1'):
                parse_records('#01=A(); ' + second)
        with warnings.catch_warnings(record=True) as notes:
            warnings.simplefilter('always')
            self.assertEqual(parse_records('#1=A(); #1=garbage;'), {1: ('A', '')})
        self.assertEqual(len(notes), 1)
        self.assertEqual(str(notes[0].message), 'STEP truncated/invalid record skipped: #1')

    def test_comments_do_not_toggle_quotes_or_add_records(self):
        text = ("HEADER;\n/* exported by O'Brien tool */\nFILE_NAME('a');\nENDSEC;\nDATA;\n"
                "/* #5=Z(9); */ #1=A('a' /* ' */ ,2);\n#2=B('x /* kept */');\nENDSEC;")
        with warnings.catch_warnings(record=True) as notes:
            warnings.simplefilter('always')
            result = parse_records(text)
        self.assertEqual(sorted(result), [1, 2])
        self.assertEqual(parse_fields(result[1][1]), ["'a'", '2'])
        self.assertEqual(parse_fields(result[2][1]), ["'x /* kept */'"])
        self.assertFalse(notes)
        with warnings.catch_warnings(record=True) as notes:
            warnings.simplefilter('always')
            self.assertEqual(parse_records("#1=A(1); /* open"), {1: ('A', '1')})
        self.assertFalse(notes)
        with self.assertRaises(ValueError):
            parse_fields("1, /* open")

    def test_oversized_identity_is_skipped_with_warning(self):
        with warnings.catch_warnings(record=True) as notes:
            warnings.simplefilter('always')
            result = parse_records('#' + '1' * 5000 + '=A(1); #2=B(2);')
        self.assertEqual(result, {2: ('B', '2')})
        self.assertEqual([str(n.message) for n in notes], ['STEP truncated/invalid record skipped: #' + '1' * 18 + '...'])

    def test_field_validation_error_contract(self):
        for body, message in [("1,'cut", 'Truncated STEP field'), ('1,(2,3', 'Truncated STEP field'),
                              ('1),2', 'Unmatched STEP parenthesis')]:
            with self.subTest(body=body), self.assertRaisesRegex(ValueError, message):
                parse_fields(body)

    def test_long_malformed_records_finish_in_bounded_subprocess(self):
        # A subprocess timeout detects runaway parsing without timing a fast machine.
        script = """
import warnings
from core.step_reader import parse_records
for text in ["#1=A(" + chr(39) * 200000,
             "#1=A('" + "x" * 200000 + "#99=Hidden();",
             "#1=A(" + "(" * 100000 + ");",
             "#1=" + "A" * 200000]:
    with warnings.catch_warnings(record=True) as notes:
        warnings.simplefilter('always')
        assert parse_records(text) == {}
    assert len(notes) == 1
    assert notes[0].category is RuntimeWarning
    assert str(notes[0].message) == 'STEP truncated/invalid record skipped: #1'
body = chr(39) + chr(39) * 200000 + chr(39)
assert parse_records('#2=A(' + body + ');') == {2: ('A', body)}
"""
        result = subprocess.run([sys.executable, '-c', script], cwd=ROOT, capture_output=True,
                                text=True, timeout=10,
                                env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class EvoIdentityTest(unittest.TestCase):
    def setUp(self):
        (ROOT / 'tests/.tmp').mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / 'tests/.tmp')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.pd = self.root / 'project'
        (self.pd / 'model').mkdir(parents=True)
        self.src = self.pd / 'model' / 'synthetic.evo'
        self.write_evo()
        self.luminaires = [{'name': 'luminaire_77', 'source': 'evo/STEP', 'x': 99.25,
                      'y': -7.0, 'z': 2.0, 'direction': [1, 0, 0], 'color': [.2, .3, .4],
                      'power': 123.0, 'lumens': 321.0, 'product_rad': '_cache/sample.rad'}]
        self.project = {'geometry': [], 'luminaires': deepcopy(self.luminaires)}
        (self.pd / 'project.json').write_text(json.dumps(self.project), encoding='utf-8')
        with evo_identity._lock:
            evo_identity._cache.clear()

    def write_evo(self, text=STEP, path=None):
        with zipfile.ZipFile(path or self.src, 'w') as z:
            z.writestr('Project/ProjectData/ProjectData.dat', text)

    def fake_ies2rad(self, command, cwd):
        args = shlex.split(command)
        target = Path(cwd) / args[args.index('-o') + 1]
        target.with_suffix('.rad').write_text('# Synthetic converter output\n')
        target.with_suffix('.dat').write_text('0\n')
        return 0, '', ''

    def test_instance_guid_product_and_array_remain_distinct(self):
        records = engine.evo_luminaires(str(self.src))
        self.assertEqual(len(records), 3)
        self.assertEqual([a['source_id'] for a in records], ['77', '78', '79'])
        self.assertEqual(records[0]['name'], 'luminaire_77')
        self.assertEqual(records[0]['source_record'], '#60')
        self.assertEqual(len({a['source_guid'] for a in records}), 3)
        self.assertEqual({a['product_id'] for a in records}, {'901'})
        self.assertEqual({a['array_id'] for a in records}, {'500'})
        self.assertEqual(records[0]['array_name'], 'Array A')
        self.assertEqual(records[0]['source_name'], "Same 'Light α' #999")
        self.assertEqual(records[0]['source_name'], records[1]['source_name'])
        self.assertNotEqual(records[0]['name'], records[1]['name'])
        self.assertNotIn('source_name', records[2])  # The GUID does not become a display name.

    def test_multilingual_product_names_and_code_preserved(self):
        record = engine.evo_luminaires(str(self.src))[0]
        self.assertEqual(record['product_name'], 'Synthetic downlight α')
        self.assertEqual(record['product_names'], [
            {'language': '1055', 'name': 'Synthetic downlight α'}, {'language': '1033', 'name': 'Demo downlight'}])
        self.assertEqual(record['product_code'], 'DEMO-01')
        self.assertNotIn('not a filename', str(record['product_names']))
        self.assertEqual(importer._evo_text(r"'\X2\004C0069006700680074002003B1\X0\'"), 'Light α')

    def test_record_renumbering_preserves_persistent_matching(self):
        before = engine.evo_luminaires(str(self.src))
        # The #999 inside the source name is data; only file references shift.
        from core.step_reader import parse_records
        ents = parse_records(STEP)
        rebuilt = []
        for i, (kind, body) in ents.items():
            parts = re.split(r"('(?:[^']|'')*')", body)
            body = ''.join(part if n % 2 else re.sub(r'#(\d+)', lambda m: '#' + str(int(m[1]) + 1000), part)
                           for n, part in enumerate(parts))
            rebuilt.append('#%d=%s(%s);' % (i + 1000, kind, body))
        self.write_evo('\n'.join(rebuilt))
        after = engine.evo_luminaires(str(self.src))
        for old, new in zip(before, after):
            for field in ('name', 'source_id', 'source_guid', 'source_name', 'product_id', 'product_name', 'array_id', 'x', 'y', 'z', 'direction'):
                self.assertEqual(old.get(field), new.get(field))
            self.assertNotEqual(old['source_record'], new['source_record'])

    def test_storey_array_local_composition_precision_and_normalization(self):
        records = engine.evo_luminaires(str(self.src))
        a = records[0]
        expected = (10.8626543211, 22.448456789, 5.625009876)
        for field, value in zip(('x', 'y', 'z'), expected):
            self.assertAlmostEqual(a[field], value, places=11)
        self.assertEqual(a['source_world_matrix'][12:15], [a['x'], a['y'], a['z']])
        self.assertEqual(a['source_world_matrix'][15], 1.0)
        for record in records[:2]:
            self.assertAlmostEqual(math.hypot(*record['direction']), 1.0)
            for actual, target in zip(record['direction'], (0, -.8, -.6)):
                self.assertAlmostEqual(actual, target)

    def test_duplicate_persistent_identity_and_conflicting_prototype_rejected(self):
        self.write_evo(STEP.replace('LuminaireElement(78,', 'LuminaireElement(77,'))
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            engine.evo_luminaires(str(self.src))
        self.write_evo(STEP + "#200=LuminairePrototype(902,'second',(),$,$);#201=RelDefinesByPrototype(201,'',(#60),#200);")
        with self.assertRaisesRegex(ValueError, 'conflicts'):
            engine.evo_luminaires(str(self.src))

    def test_nonfinite_values_and_zero_direction_are_not_inferred(self):
        for value in ('NaN', '1e999'):
            with self.subTest(value=value):
                self.write_evo(STEP.replace('0.123456789', value))
                with self.assertRaisesRegex(ValueError, 'finite'):
                    engine.evo_luminaires(str(self.src))
        self.write_evo(STEP.replace('(0.8,0,0.6)', '(0,0,0)'))
        with self.assertRaisesRegex(ValueError, 'direction is invalid'):
            engine.evo_luminaires(str(self.src))

    def test_photometry_matches_same_product_to_distinct_instance_identities(self):
        with patch.object(engine, 'sh', side_effect=self.fake_ies2rad) as run:
            products = engine.evo_ies(str(self.src), str(self.pd))
        self.assertEqual(set(products), {'77', '78', '79'})
        self.assertEqual(len({p['rad'] for p in products.values()}), 1)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(products['77']['source_id'], '77')
        self.assertEqual(products['77']['product_id'], '901')
        self.assertEqual(products['77']['product_name'], 'Synthetic downlight α')

    def test_placement_does_not_match_trailing_number_in_name(self):
        samples = [({'name': 'user name_999', 'source_id': '77'}, True),
                   ({'name': 'luminaire_77'}, True),
                   ({'name': 'user name_77'}, False),
                   ({'name': 'luminaire_77', 'source_id': '999'}, False)]
        for record, matched in samples:
            with self.subTest(record=record):
                errors = []
                with patch.object(engine, 'evo_luminaires', return_value=[deepcopy(record)]), \
                     patch.object(engine, 'evo_ies', return_value={'77': {'rad': '_cache/toy.rad', 'lumens': 1000}}):
                    _, luminaires = format_detector._place_evo(str(self.src), str(self.pd),
                        str(self.pd / 'model/toy.obj'), [], errors)
                self.assertEqual('product_rad' in luminaires[0], matched)

    def test_read_only_enrichment_preserves_physical_values(self):
        source_before = self.src.read_bytes()
        project_before = (self.pd / 'project.json').read_bytes()
        arms_before = deepcopy(self.luminaires)
        records, notes = evo_identity.enrich(str(self.pd), self.project, self.luminaires)
        self.assertFalse(notes)
        for key, value in arms_before[0].items():
            self.assertEqual(records[0][key], value)
        self.assertEqual(records[0]['source_id'], '77')
        self.assertEqual(records[0]['product_name'], 'Synthetic downlight α')
        self.assertEqual(self.luminaires, arms_before)
        self.assertEqual(self.src.read_bytes(), source_before)
        self.assertEqual((self.pd / 'project.json').read_bytes(), project_before)
        self.assertFalse((self.pd / '_cache').exists())

    def test_existing_metadata_and_cached_copy_unchanged(self):
        self.luminaires[0].update({'product_name': 'user preference', 'source_name': None})
        first, _ = evo_identity.enrich(str(self.pd), self.project, self.luminaires)
        self.assertEqual(first[0]['product_name'], 'user preference')
        self.assertIsNone(first[0]['source_name'])
        first[0]['product_names'][0]['name'] = 'only the output changed'
        first[0]['direction'][0] = 99
        second, _ = evo_identity.enrich(str(self.pd), self.project, self.luminaires)
        self.assertEqual(second[0]['product_names'][0]['name'], 'Synthetic downlight α')
        self.assertEqual(second[0]['direction'], [1, 0, 0])

    def test_explicit_source_takes_precedence_over_single_candidate_search(self):
        self.write_evo(path=self.pd / 'model/second.evo')
        for project in ({'source_evo': 'model/synthetic.evo'},
                        {'importer': {'source_evo': 'model/synthetic.evo'}}):
            records, notes = evo_identity.enrich(str(self.pd), project, self.luminaires)
            self.assertFalse(notes)
            self.assertIn('product_name', records[0])

    def test_missing_multiple_or_broken_sources_preserve_original(self):
        self.src.unlink()
        for situation in ('missing', 'multiple', 'broken'):
            if situation == 'multiple':
                self.write_evo(); self.write_evo(path=self.pd / 'model/second.evo')
            elif situation == 'broken':
                (self.pd / 'model/second.evo').unlink(); self.src.write_bytes(b'not a zip')
            records, notes = evo_identity.enrich(str(self.pd), self.project, self.luminaires)
            self.assertEqual(records, self.luminaires)
            self.assertTrue(notes)
            self.assertNotIn('synthetic.evo', str(notes))

    def test_absolute_escaping_and_symlink_sources_rejected(self):
        outside = self.root / 'outside.evo'; self.write_evo(path=outside)
        link = self.pd / 'model/outside.evo'; link.symlink_to(outside)
        for ref in (str(outside), '../outside.evo', 'model/outside.evo', 'model/no.evo'):
            with patch.object(engine, 'evo_luminaires') as parse:
                records, notes = evo_identity.enrich(str(self.pd), {'source_evo': ref}, self.luminaires)
                self.assertEqual(records, self.luminaires); self.assertTrue(notes); parse.assert_not_called()

    def test_foreign_format_and_arbitrary_name_not_inferred_as_evo(self):
        records = [{'name': 'luminaire_77', 'source': 'usd', 'source_id': '77'},
                   {'name': 'table_77'}, {'name': 'user', 'source': 'evo/STEP'}]
        with patch.object(engine, 'evo_luminaires') as parse:
            after, notes = evo_identity.enrich(str(self.pd), self.project, records)
            self.assertEqual(after, records); self.assertFalse(notes); parse.assert_not_called()

    def test_renamed_identity_matches_and_legacy_name_does_not_override_identity(self):
        records = [{'name': 'renamed', 'source': 'evo/STEP', 'source_id': '77'},
                   {'name': 'luminaire_77', 'source': 'evo/STEP', 'source_id': '999'},
                   {'name': 'luminaire_78'}]
        after, notes = evo_identity.enrich(str(self.pd), self.project, records)
        self.assertEqual(after[0]['product_id'], '901')
        self.assertNotIn('product_id', after[1]); self.assertTrue(notes)
        self.assertEqual(after[2]['source_id'], '78')

    def test_cache_size_mtime_and_at_most_eight_sources(self):
        with patch.object(engine, 'evo_luminaires', wraps=engine.evo_luminaires) as parse:
            for _ in range(2):
                evo_identity.enrich(str(self.pd), self.project, self.luminaires)
            self.assertEqual(parse.call_count, 1)
            stat = self.src.stat(); os.utime(self.src, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1000000))
            evo_identity.enrich(str(self.pd), self.project, self.luminaires)
            self.assertEqual(parse.call_count, 2)
            self.write_evo(STEP + '\n')
            evo_identity.enrich(str(self.pd), self.project, self.luminaires)
            self.assertEqual(parse.call_count, 3)
            for index in range(9):
                source = self.pd / ('source%d.evo' % index); self.write_evo(path=source)
                evo_identity.enrich(str(self.pd), {'source_evo': source.name}, self.luminaires)
            self.assertEqual(len(evo_identity._cache), 8)
            self.assertTrue(all(Path(key[0]).is_absolute() for key in evo_identity._cache))


if __name__ == '__main__':
    unittest.main()
