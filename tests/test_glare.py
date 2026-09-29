# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Age-dependent glare command: formula, evalglare parsing, object mapping and end-to-end flow."""
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
from core import engine as engine
from core import glare as K
from core.__main__ import convert, glare
import synthetic as synthetic

engine.prepare_environment()
RADIANCE = all(shutil.which(x) for x in ('oconv', 'rpict', 'evalglare', 'rtrace', 'pcond', 'ra_ppm', 'pextrem'))
HEADER = ('No pixels x-pos y-pos L_s Omega_s Posindx L_b L_t E_v Edir Max_Lum Sigma xdir ydir zdir '
          'Eglare Lveil_cie teta glare_zone r_contrast r_dgp')
SUMMARY = ('dgp,av_lum,E_v,lum_backg,E_v_dir,dgi,ugr,vcp,cgi,lum_sources,omega_sources,Lveil,Lveil_cie,'
        'dgr,ugp,ugr_exp,dgi_mod,av_lum_pos,av_lum_pos2,med_lum,med_lum_pos,med_lum_pos2,ugp2: '
        '0.412000 ' + ' '.join(['1.0'] * 5) + ' 23.500000 ' + ' '.join(['1.0'] * 16))
LIMITATION_LINES = (
    'Materials are assumed to be matte. Glossy surfaces do not produce specular reflections in this scene.',
    'For luminaires imported from EVO, the emitting aperture size is a placeholder. The luminaire luminance '
    'and the resulting DGP, UGR and veiling luminance are therefore unreliable.',
    'The formula applies for 1 < angle < 30 degrees. Sources outside this range are excluded from the sum.')


def source_row(no, L, omega, direction):
    numbers = [no, 10, 1, 1, L, omega, 1, 1, 1, 1, 1, L, 1, *direction, 1, 1, 1, 0, 0, 0]
    return ' '.join('%.6f' % x for x in numbers)


def evalglare_text(lines, number=None):
    return '\n'.join(['%d %s' % (len(lines) if number is None else number, HEADER), *lines, SUMMARY]) + '\n'


def direction(degrees_value):
    """View direction +X. The source is at the given angle in the XZ plane."""
    return [math.cos(math.radians(degrees_value)), 0, math.sin(math.radians(degrees_value))]


class FormulaTest(unittest.TestCase):
    def test_single_source_manual_calculation(self):
        sources = [{'luminance': 1000.0, 'solid_angle': 0.01, 'direction': direction(10)}]
        total = K.veiling_luminance(sources, [2, 0, 0])
        E = 1000 * 0.01 * math.cos(math.radians(10))
        self.assertAlmostEqual(sources[0]['angle'], 10, places=9)
        self.assertAlmostEqual(sources[0]['illuminance'], E, places=9)
        self.assertAlmostEqual(sources[0]['contribution'], 10 * E / 100, places=9)
        self.assertAlmostEqual(total, 0.984807753, places=9)
        table = {y['age']: y for y in K.age_table(total, 75)}
        self.assertAlmostEqual(table[70]['veiling_luminance'], 2 * total, places=12)
        self.assertAlmostEqual(table[75]['veiling_luminance'], total * (1 + (75 / 70) ** 4), places=12)
        self.assertEqual(sorted(table), [20, 40, 50, 60, 70, 75, 80])

    def test_general_formula_independent_numeric_example(self):
        # A=62.5, p=.5, angle=10: 0.01 + (0.05+0.005)*2 + .00125 = .12125.
        self.assertAlmostEqual(K.general_sum([{'angle': 10, 'illuminance': 2}], 62.5), .2425)
        # The 60-degree source excluded from the old sum is included in the general sum.
        self.assertGreater(K.general_sum([{'angle': 60, 'illuminance': 2}], 75), 0)
        self.assertEqual(K.general_sum([], 75), 0)

    def test_general_range_and_eye_pigmentation(self):
        for angle in (0, .1, 100, 101):
            self.assertEqual(K.general_sum([{'angle': angle, 'illuminance': 2}], 75), 0)
        for angle in (.10001, 99.999):
            self.assertGreater(K.general_sum([{'angle': angle, 'illuminance': 2}], 75), 0)
        for p in (-1, 1.3, float('nan'), float('inf'), True):
            with self.assertRaises(ValueError):
                K.general_sum([], 75, p)
        for p in (0, .5, 1, 1.2):
            self.assertEqual(K.check_eye_pigmentation(p), p)
        with self.assertRaises(ValueError):
            K.general_sum([{'angle': 95, 'illuminance': -1}], 75)

    def test_age_factors(self):
        for age, expected in ((20, 1.01), (50, 1.26), (60, 1.54), (70, 2.00), (80, 2.71)):
            with self.subTest(age=age):
                self.assertEqual(round(K.age_factor(age), 2), expected)
        for invalid_value in (0, -5, 121, float('nan'), '70', True, False):
            with self.subTest(age=invalid_value), self.assertRaises(ValueError):
                K.age_factor(invalid_value)

    def test_out_of_range_sources_are_counted_and_excluded_from_sum(self):
        angles = (0.5, 1, 1.001, 15, 29.999, 30, 31, 90)
        sources = [{'luminance': 500.0, 'solid_angle': 0.002, 'direction': direction(a)} for a in angles]
        total = K.veiling_luminance(sources, [1, 0, 0])
        self.assertEqual([k['in_range'] for k in sources], [False, False, True, True, True, False, False, False])
        self.assertEqual(sum(not k['in_range'] for k in sources), 5)
        self.assertEqual([k['contribution'] is None for k in sources], [True, True, False, False, False, True, True, True])
        expected = sum(10 * 500 * .002 * math.cos(math.radians(a)) / a ** 2 for a in (1.001, 15, 29.999))
        self.assertAlmostEqual(total, expected, places=9)

    def test_zero_sources(self):
        self.assertEqual(K.veiling_luminance([], [0, 1, 0]), 0)
        self.assertTrue(all(y['veiling_luminance'] == 0 for y in K.age_table(0.0, 75)))
        result = {'scene': 's', 'age': 75, 'view': {'vp': [0, 0, 1], 'vd': [1, 0, 0], 'source': 'command line'},
                 'evalglare': {'raw': {'dgp': '0.159000', 'ugr': '0.000000'}}, 'source_count': 0,
                 'out_of_range_count': 0, 'sources': [], 'age_table': K.age_table(0.0, 75)}
        text = K.text(result)
        self.assertIn('- No sources\n', text)
        self.assertIn('Sources outside the range (1 < angle < 30 degrees): 0', text)
        self.assertIn('75 (requested)', text)
        for unit in ('Viewpoint (m): 0 0 1', 'Age (years)', 'Factor (no unit)', K.SYMBOLS):
            self.assertIn(unit, text)


class EvalglareTest(unittest.TestCase):
    def test_synthetic_output_is_parsed(self):
        text = evalglare_text([source_row(1, 2000, .05, [0, 0, 2]), source_row(2, 30, .5, [1, 0, 0])])
        sources, digest, raw = K.parse_evalglare('Notice: dgp below 0.2\n' + text)
        self.assertEqual([k['no'] for k in sources], [1, 2])
        self.assertEqual(sources[0]['direction'], [0, 0, 1])
        self.assertEqual((sources[0]['luminance'], sources[0]['solid_angle']), (2000, .05))
        self.assertEqual((digest['dgp'], digest['ugr']), (.412, 23.5))
        self.assertEqual((raw['dgp'], raw['ugr']), ('0.412000', '23.500000'))

    def test_nonfinite_optional_summary_is_none_and_json_safe(self):
        names, values = SUMMARY.split(': ')
        values = values.split()
        values[names.split(',').index('ugp')] = 'inf'
        text = evalglare_text([source_row(1, 2000, .05, [0, 0, 1])]).replace(SUMMARY, names + ': ' + ' '.join(values))
        _, digest, raw = K.parse_evalglare(text)
        self.assertIsNone(digest['ugp'])
        self.assertEqual(raw['ugp'], 'inf')
        self.assertEqual((digest['dgp'], digest['ugr']), (.412, 23.5))
        json.dumps(digest, allow_nan=False)

    def test_ugr_sentinel_with_dark_background_is_not_a_value(self):
        names, values = SUMMARY.split(': ')
        names, values = names.split(','), values.split()
        values[names.index('ugr')], values[names.index('lum_backg')] = '-99.000000', '0.000000'
        text = evalglare_text([source_row(1, 2000, .05, [0, 0, 1])]).replace(SUMMARY, ','.join(names) + ': ' + ' '.join(values))
        _, digest, raw = K.parse_evalglare(text)
        self.assertIsNone(digest['ugr'])
        self.assertEqual(raw['ugr'], '-99.000000')
        self.assertEqual(digest['dgp'], .412)

    def test_placeholder_row_not_counted_without_sources(self):
        sources, digest, _ = K.parse_evalglare(evalglare_text([' '.join(['0.000000'] * 23)], number=0))
        self.assertEqual(sources, [])
        self.assertEqual(digest['dgp'], .412)

    def test_zero_header_nonzero_background_and_23_columns(self):
        values = ('0 0.000000 0.000000 0.000000 0.000000 0.0000000000 0.000000 '
                  '0.000185 0.000093 0.000582 0.000000 5.479233 ' + '0 ' * 11)
        self.assertEqual(len(values.split()), 23)
        self.assertEqual(K.parse_evalglare(evalglare_text([values], number=0))[0], [])
        with self.assertRaisesRegex(ValueError, 'summary'):
            K.parse_evalglare(evalglare_text([values], number=0).replace(SUMMARY, ''))

    def test_truncated_row_raises(self):
        line = source_row(1, 2000, .05, [0, 0, 1]).rsplit(' ', 1)[0]
        with self.assertRaisesRegex(ValueError, 'truncated'):
            K.parse_evalglare(evalglare_text([line]))
        with self.assertRaisesRegex(ValueError, 'summary row is truncated'):
            K.parse_evalglare(evalglare_text([source_row(1, 2000, .05, [0, 0, 1])]).replace(' 23.500000', ''))

    def test_list_count_mismatch_raises(self):
        two = [source_row(1, 2000, .05, [0, 0, 1]), source_row(2, 30, .5, [1, 0, 0])]
        with self.assertRaisesRegex(ValueError, 'mismatch'):
            K.parse_evalglare(evalglare_text(two, number=3))
        with self.assertRaisesRegex(ValueError, 'mismatch'):
            K.parse_evalglare(evalglare_text(two[:1], number=0))

    def test_missing_or_invalid_output_raises(self):
        valid = evalglare_text([source_row(1, 2000, .05, [0, 0, 1])])
        invalid = {'missing header': valid.split('\n', 1)[1],
                 'missing summary': valid.replace(SUMMARY, ''),
                 'dgp out of range': valid.replace('0.412000', '1.500000'),
                 'zero direction': evalglare_text([source_row(1, 2000, .05, [0, 0, 0])]),
                 'zero solid angle': evalglare_text([source_row(1, 2000, 0, [0, 0, 1])]),
                 'infinite': valid.replace('2000.000000', 'inf', 1),
                 'empty': ''}
        for name, text in invalid.items():
            with self.subTest(name), self.assertRaises(ValueError):
                K.parse_evalglare(text)


class ObjectTest(unittest.TestCase):
    SCENE = {'mesh': {'f': [0, 1, 2, 2, 3, 0], 'm': [0, 0, 1, 1],
                      'materials': [{'name': 'floor_mat'}, {'name': 'wall_mat'}]},
             'lights': [{'name': 'ceiling light'}, {'name': ''}]}

    def test_face_and_light_names(self):
        output = 'm0\tface0\t\nm1\tface1\t\nl0_m\tl0\t\nl0_light\tl0.l0.s\t\nl1_m\tl1\t\n'
        self.assertEqual(K.object_names(output, 5, self.SCENE), [
            {'object': 'floor_mat', 'radiance_name': 'face0'}, {'object': 'wall_mat', 'radiance_name': 'face1'},
            {'object': 'ceiling light', 'radiance_name': 'l0'}, {'object': 'ceiling light', 'radiance_name': 'l0.l0.s'},
            {'object': 'l1', 'radiance_name': 'l1'}])

    def test_unmapped_source_raises(self):
        for name, output, number in (('empty space', '*\t*\t\n', 1), ('unknown name', 'm0\tbox\t\n', 1),
                                ('missing triangle', 'm0\tface9\t\n', 1), ('missing light', 'l_m\tl7\t\n', 1),
                                ('missing row', 'm0\tface0\t\n', 2), ('empty output', '', 1)):
            with self.subTest(name), self.assertRaises(ValueError):
                K.object_names(output, number, self.SCENE)


class CommandRulesTest(unittest.TestCase):
    """Input and output rules that do not require Radiance."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.scene = self.root/'scene'
        self.scene.mkdir()
        (self.scene/'scene.rad').write_text('void plastic m0\n0\n0\n5 .5 .5 .5 0 0\n')
        (self.scene/'scene.ctlux.json').write_text('{}')

    def test_output_rules_match_conversion(self):
        populated = self.root/'populated'; populated.mkdir(); (populated/'keep').write_text('original')
        with self.assertRaises(FileExistsError):
            glare(self.scene, populated)
        self.assertEqual((populated/'keep').read_text(), 'original')
        with self.assertRaisesRegex(ValueError, 'nested'):
            glare(self.scene, self.scene/'inside')
        with self.assertRaisesRegex(ValueError, 'Not a scene directory'):
            glare(self.scene/'scene.rad', self.root/'file')
        with self.assertRaises(FileNotFoundError):
            glare(self.scene, self.root/'missing'/'inside')
        for name, option in (('age', {'age': 0}), ('quality', {'quality': 'report'}),
                            ('direction', {'vp': [0, 0, 1], 'vd': [0, 0, 0]}), ('point', {'vp': [0, float('nan'), 1]})):
            with self.subTest(name), self.assertRaises(ValueError):
                glare(self.scene, self.root/('arg-' + name), **option)
            self.assertFalse((self.root/('arg-' + name)).exists())
        self.assertFalse(any(p.name.startswith(('.glare-', 'missing', 'file')) for p in self.root.iterdir()))

    def test_missing_scene_and_external_command_fail_with_report(self):
        (self.scene/'scene.rad').write_text('!echo a command\n')
        with self.assertRaisesRegex(ValueError, 'external command'):
            glare(self.scene, self.root/'command', vp=[0, 0, 1], vd=[1, 0, 0])
        (self.scene/'scene.rad').unlink()
        with self.assertRaisesRegex(FileNotFoundError, 'scene.rad'):
            glare(self.scene, self.root/'missing', vp=[0, 0, 1], vd=[1, 0, 0])
        for name in ('command', 'missing'):
            self.assertEqual([p.name for p in (self.root/name).iterdir()], ['REPORT.txt'])
            report = (self.root/name/'REPORT.txt').read_text()
            self.assertIn('Status: error', report)
            for line in LIMITATION_LINES:
                self.assertIn('- ' + line.rstrip('.') + '\n', report)

    def test_external_command_markers_rejected_before_native_tools(self):
        marker = self.root/'command-marker'
        command = '!echo verified > ' + shlex.quote(str(marker)) + '\n'
        primitive = 'void plastic m0 0 0 5 .5 .5 .5 0 0'
        prefixes = ('', primitive + ' ', primitive + '\t', primitive + '\v', primitive + '\f',
                    primitive + '\r', primitive + '\x00 ', primitive + ' # comment ',
                    primitive + ' # comment\r', primitive.replace('m0 ', 'm#0 ') + ' ')
        for i, prefix in enumerate(prefixes):
            with self.subTest(prefix=repr(prefix)):
                # A # inside an identifier is not a comment. Even harmless comment bangs are rejected.
                payload = prefix + command + '\nm0 sphere object 0 0 4 0 0 0 1\n'
                (self.scene/'scene.rad').write_text(payload, encoding='utf-8')
                out = self.root/('command-token-%d' % i)
                with patch.object(engine, 'sh') as native:
                    with self.assertRaisesRegex(ValueError, 'external command'):
                        glare(self.scene, out, vp=[0, 0, 1], vd=[1, 0, 0])
                native.assert_not_called()
                self.assertFalse(marker.exists())
                self.assertEqual([p.name for p in out.iterdir()], ['REPORT.txt'])
                self.assertIn('Status: error', (out/'REPORT.txt').read_text())

    @unittest.skipUnless(os.name == 'posix', 'POSIX links and FIFOs required')
    def test_light_links_and_devices_rejected_before_native_tools(self):
        outside = self.root/'outside.dat'; outside.write_text('0\n')
        cases = {'file-link': lambda d: (d/'a.dat').symlink_to(outside),
                 'folder-link': lambda d: (d/'sub').symlink_to(self.root),
                 'fifo': lambda d: os.mkfifo(d/'a.dat')}
        for name, make in cases.items():
            with self.subTest(name):
                light = self.scene/'light'
                shutil.rmtree(light, ignore_errors=True); light.mkdir()
                make(light)
                out = self.root/('light-' + name)
                with patch.object(engine, 'sh') as native:
                    with self.assertRaisesRegex(ValueError, 'regular files'):
                        glare(self.scene, out, vp=[0, 0, 1], vd=[1, 0, 0])
                native.assert_not_called()
                self.assertIn('Status: error', (out/'REPORT.txt').read_text())
        shutil.rmtree(self.scene/'light')
        (self.scene/'light').symlink_to(self.root)
        with patch.object(engine, 'sh') as native, self.assertRaisesRegex(ValueError, 'regular files'):
            glare(self.scene, self.root/'light-root-link', vp=[0, 0, 1], vd=[1, 0, 0])
        native.assert_not_called()
        (self.scene/'light').unlink()
        os.mkfifo(self.scene/'light')
        with patch.object(engine, 'sh') as native, self.assertRaisesRegex(ValueError, 'regular files'):
            glare(self.scene, self.root/'light-path-fifo', vp=[0, 0, 1], vd=[1, 0, 0])
        native.assert_not_called()

    @unittest.skipUnless(RADIANCE and os.name == 'posix', 'Local Radiance with a POSIX shell required')
    def test_native_inline_command_escape_is_blocked(self):
        marker = self.root/'inline-command-marker'
        command = '!echo verified > ' + shlex.quote(str(marker)) + '\n'
        payload = ('void plastic m0 0 0 5 .5 .5 .5 0 0 ' + command +
                   'm0 sphere object 0 0 4 0 0 0 1\n')
        # Establish that the installed oconv executes this inline escape, only inside the temp directory.
        result = subprocess.run([shutil.which('oconv'), '-'], input=payload, text=True,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                cwd=self.root, env=engine.radiance_environment(), timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(marker.read_text().strip(), 'verified')
        marker.unlink()
        (self.scene/'scene.rad').write_text(payload, encoding='utf-8')
        out = self.root/'inline-command'
        # Use the real glare entry point and native shell wrapper. An old guard fails before rendering.
        with patch.object(engine, 'render', side_effect=AssertionError('Command must be rejected before rendering')):
            with self.assertRaisesRegex(ValueError, 'external command'):
                glare(self.scene, out, vp=[0, 0, 1], vd=[1, 0, 0])
        self.assertFalse(marker.exists())
        self.assertEqual([p.name for p in out.iterdir()], ['REPORT.txt'])
        self.assertIn('Status: error', (out/'REPORT.txt').read_text())

    def test_notice_translation_loses_no_messages(self):
        from core.__main__ import _evalglare_notices
        lines = ['Notice: Vertical illuminance is below 100 lux',
                 'Notice: Low brightness scene. dgp below 0.2'] + ['unknown %d' % i for i in range(12)]
        result = _evalglare_notices('\n'.join(lines))
        self.assertEqual(len(result), 14)
        self.assertIn('Vertical illuminance is below 100 lux', result[0])
        self.assertIn('DGP is below 0.2', result[1])
        self.assertIn('unknown 11', result[-1])

    def test_full_sentence_notice_translation(self):
        from core.__main__ import _evalglare_notices
        result = _evalglare_notices(
            'Notice: Vertical illuminance is below 100 lux !!\n'
            'Notice: Low brightness scene. dgp below 0.2! dgp might underestimate glare sources\n'
            'Notice: Low brightness scene. Vertical illuminance less than 380 lux! '
            'dgp might underestimate glare sources\n')
        small = '. DGP may underestimate glare sources'
        self.assertEqual(result, [
            'evalglare notification: Notice: Vertical illuminance is below 100 lux',
            'evalglare notification: Notice: Low brightness scene. DGP is below 0.2' + small,
            'evalglare notification: Notice: Low brightness scene. Vertical illuminance is below 380 lux' + small])

    def test_tool_warnings_file_count_and_first_three(self):
        from core.__main__ import _tool_warnings
        warnings = ['ies2rad: light/l%d.ies: warning - no lamp type' % i for i in range(235)]
        warnings += [warnings[0], 'ies2rad: light/l0.ies: another warning', 'unknown notice']
        rows = _tool_warnings(warnings)
        self.assertEqual(len(rows), 3)
        self.assertIn('in 235 files, first files: light/l0.ies, light/l1.ies, light/l2.ies', rows[0])
        self.assertIn('another warning, in 1 file', rows[1])
        self.assertEqual(rows[2], 'unknown notice')

    def test_command_zero_header_nonzero_placeholder_row_and_notices(self):
        from core.__main__ import main
        placeholder = ('0 0 0 0 0 0 0 .000185 .000093 .000582 0 5.479233 ' + '0 ' * 11)
        def fake(cmd, cwd=None):
            if cmd.startswith('evalglare'):
                return (0, 'Notice: Low brightness scene. dgp below 0.2\n' +
                        evalglare_text([placeholder], number=0),
                        'WARNING: Vertical illuminance is below 100 lux\n' +
                        '\n'.join('notice %d' % i for i in range(12)))
            self.assertFalse(cmd.startswith('rtrace'))
            return (0, '', '')
        out = self.root/'nonzero-placeholder'
        (self.scene/'view.vf').write_text('VIEW= -vp 0 0 1 -vd 1 0 0\n')
        def png(src, dst, **kwargs):
            Path(dst).write_bytes(b'synthetic image placeholder')
        with patch.object(engine, 'sh', side_effect=fake), patch.object(engine, 'render'), \
                patch.object(engine, 'hdr_to_png', side_effect=png):
            self.assertEqual(main(['glare', str(self.scene), str(out), '--quality', 'draft']), 0)
        text = (out/'GLARE.txt').read_text()
        report = (out/'REPORT.txt').read_text()
        data = json.loads((out/'GLARE.json').read_text())
        self.assertIn('Source count: 0', text)
        self.assertTrue(all(y['veiling_luminance'] == y['general_veiling_luminance'] == 0
                            for y in data['age_table']))
        for expected in ('Vertical illuminance is below 100 lux', 'DGP is below 0.2', 'notice 0',
                         'notice 11', 'The automatic view is for rendering'):
            self.assertIn(expected, report)
        # Do not add a zero-count report line when no sources are outside the range.
        self.assertNotIn('sources are outside the general equation', report)

    def test_missing_view_file_raises(self):
        with self.assertRaisesRegex(FileNotFoundError, 'view.vf'):
            glare(self.scene, self.root/'without_view')
        self.assertIn('Status: error', (self.root/'without_view/REPORT.txt').read_text())


def directory_digest(directory):
    return {p.relative_to(directory).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(directory.rglob('*')) if p.is_file()}


@unittest.skipUnless(RADIANCE, 'Local Radiance and evalglare installation required')
class GlareCommandTest(unittest.TestCase):
    VIEW = ['--vp', '0.8', '2.5', '1.2', '--vd', '1', '0', '0.6', '--quality', 'draft']

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.scene = self.root/'scene'
        convert(synthetic.glare_room(self.root/'room.evo'), self.scene)
        self.env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}

    def test_end_to_end_objects_formula_and_write_boundary(self):
        before = directory_digest(self.scene)
        # Preserve shell metacharacters and spaces to exercise literal path handling.
        output = self.root/'output $(touch UNWANTED)'
        lock = self.root/'render.lock'
        # Write audit: every Python write must target the output directory or the render lock.
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
        r = subprocess.run([sys.executable, '-B', '-c', code, 'glare', str(self.scene), *self.VIEW,
                            '--age', '80', str(output)], cwd=ROOT, capture_output=True, text=True,
                           timeout=300, env={**self.env, 'CTLUX_RENDER_LOCK': str(lock)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((ROOT/'UNWANTED').exists())
        self.assertFalse(list(self.root.rglob('UNWANTED')))
        self.assertEqual(sorted(p.name for p in output.iterdir()),
                         ['GLARE.json', 'GLARE.txt', 'REPORT.txt', 'eye.png', 'marked.png'])
        for png in ('eye.png', 'marked.png'):
            self.assertEqual((output/png).read_bytes()[:8], b'\x89PNG\r\n\x1a\n')
        self.assertEqual(before, directory_digest(self.scene))
        result = json.loads((output/'GLARE.json').read_text())
        sources = result['sources']
        self.assertEqual(result['source_count'], len(sources))
        self.assertGreaterEqual(len(sources), 1)
        lamp = sources[0]
        self.assertEqual((lamp['object'], lamp['radiance_name'], lamp['in_range']), ('ceiling light', 'l0', True))
        # Lamp at (2, 2.5, 2.8), at 21.8 degrees from the eye's view direction.
        expected_angle = math.degrees(math.acos((1.2 + 1.6 * .6) / (2 * math.sqrt(1.36))))
        self.assertAlmostEqual(lamp['angle'], expected_angle, delta=1)
        self.assertTrue(all(k['object'] and k['radiance_name'] for k in sources))
        # Recompute the formula independently from the JSON fields.
        total = 0
        for k in sources:
            E = k['luminance'] * k['solid_angle'] * math.cos(math.radians(k['angle']))
            self.assertAlmostEqual(k['illuminance'], E, delta=1e-6 * max(1, E))
            self.assertEqual(k['in_range'], 1 < k['angle'] < 30)
            if k['in_range']:
                total += 10 * E / k['angle'] ** 2
            else:
                self.assertIsNone(k['contribution'])
        self.assertAlmostEqual(result['sum_before_age_factor'], total, delta=1e-9 * total)
        eighty = next(y for y in result['age_table'] if y['age'] == 80)
        self.assertAlmostEqual(eighty['veiling_luminance'], total * (1 + (80 / 70) ** 4), delta=1e-9 * total)
        outside = sum(not k['in_range'] for k in sources)
        self.assertEqual(result['out_of_range_count'], outside)
        text = (output/'GLARE.txt').read_text()
        self.assertIn('Sources outside the range (1 < angle < 30 degrees): %d' % outside, text)
        self.assertIn('evalglare DGP (0 to 1, no unit): ' + result['evalglare']['raw']['dgp'], text)
        self.assertIn('ceiling light', text)
        for line in LIMITATION_LINES:
            self.assertIn(line, result['limitations'])
        self.assertIn('LIMITATIONS', text)
        for line in LIMITATION_LINES:
            self.assertIn(line.rstrip('.'), text)
        report = (output/'REPORT.txt').read_text()
        for line in LIMITATION_LINES:
            self.assertIn('- ' + line.rstrip('.') + '\n', report)
        if outside:
            self.assertIn('%d sources are outside 1 < angle < 30 degrees' % outside, report)
        self.assertFalse(list(output.glob('.glare-*')))

    def test_view_file_and_view_without_sources(self):
        # The automatic camera looks from outside the room. With no sources, the result is zero.
        r = subprocess.run([sys.executable, '-B', '-m', 'core', 'glare', str(self.scene),
                            str(self.root/'outside'), '--quality', 'draft'],
                           cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=300)
        self.assertEqual(r.returncode, 0, r.stderr)
        result = json.loads((self.root/'outside/GLARE.json').read_text())
        self.assertEqual(result['view']['source'], 'view.vf')
        self.assertEqual((result['source_count'], result['sources']), (0, []))
        self.assertTrue(all(y['veiling_luminance'] == 0 for y in result['age_table']))
        report = (self.root/'outside/REPORT.txt').read_text()
        self.assertIn('no glare sources found', report)
        self.assertIn('The automatic view is for rendering', report)
        self.assertTrue(all(y['general_veiling_luminance'] == 0 for y in result['age_table']))

    def test_evalglare_or_mapping_failure_does_not_invent_results(self):
        real = engine.sh
        for name, corruptor in (('evalglare', lambda cmd: (1, '', 'evalglare crashed') if cmd.startswith('evalglare') else None),
                          ('rtrace', lambda cmd: (0, '*\t*\t\n' * 20, '') if cmd.startswith('rtrace') else None),
                          ('missing rtrace', lambda cmd: (0, '', '') if cmd.startswith('rtrace') else None)):
            with self.subTest(name):
                def fake(cmd, cwd=None):
                    return corruptor(cmd) or real(cmd, cwd=cwd)
                output = self.root/('invalid ' + name)
                with patch.object(engine, 'sh', side_effect=fake), self.assertRaises((RuntimeError, ValueError)):
                    glare(self.scene, output, vp=[0.8, 2.5, 1.2], vd=[1, 0, 0.6], quality='draft')
                self.assertEqual([p.name for p in output.iterdir()], ['REPORT.txt'])
                self.assertIn('Status: error', (output/'REPORT.txt').read_text())

    def test_delivery_failure_rolls_back_result(self):
        from core.__main__ import main
        from contextlib import redirect_stderr
        import io
        move = shutil.move
        moved = []
        def invalid(src, dst):
            moved.append(dst)
            if len(moved) == 2:
                raise OSError('move failed')
            return move(src, dst)
        output = self.root/'delivery-failure'
        stderr = io.StringIO()
        with patch.object(shutil, 'move', side_effect=invalid), redirect_stderr(stderr):
            self.assertEqual(main(['glare', str(self.scene), str(output), *self.VIEW]), 1)
        self.assertEqual(stderr.getvalue().strip(), 'Error: move failed')
        self.assertEqual([p.name for p in output.iterdir()], ['REPORT.txt'])
        report = (output/'REPORT.txt').read_text()
        self.assertIn('Status: error', report)
        self.assertIn('move failed', report)

    @unittest.skipUnless(os.name == 'posix', 'POSIX signal test')
    def test_delivery_cancellation_rolls_back_result(self):
        from core.__main__ import main
        from contextlib import redirect_stderr
        import io
        move = shutil.move
        handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        for sig in handlers:
            with self.subTest(signal=sig):
                output = self.root/('cancelled-' + str(sig))
                def interrupted(src, dst):
                    move(src, dst)
                    os.kill(os.getpid(), sig)
                stderr = io.StringIO()
                with patch.object(shutil, 'move', side_effect=interrupted), redirect_stderr(stderr):
                    self.assertEqual(main(['glare', str(self.scene), str(output), *self.VIEW]), 128 + sig)
                self.assertEqual(stderr.getvalue().strip(), 'Cancelled.')
                self.assertEqual([p.name for p in output.iterdir()], ['REPORT.txt'])
                self.assertIn('Status: cancelled', (output/'REPORT.txt').read_text())
                self.assertEqual({s: signal.getsignal(s) for s in handlers}, handlers)


class EnglishGlareCLITest(unittest.TestCase):
    def test_english_flags_and_quality_reach_backend(self):
        from core import __main__ as cli
        for quality, internal in (('draft', 'draft'), ('medium', 'medium'), ('final', 'final')):
            with self.subTest(quality=quality), patch.object(cli, 'glare', return_value={'output': 'out'}) as glare:
                self.assertEqual(cli.main(['glare', 'scene', 'out', '--age', '60', '--quality', quality,
                                           '--eye-pigmentation', '1.2', '--vp', '1', '2', '3', '--vd', '0', '1', '0']), 0)
                glare.assert_called_once_with('scene', 'out', vp=[1.0, 2.0, 3.0], vd=[0.0, 1.0, 0.0],
                                              age=60, quality=internal, eye_pigmentation=1.2)

    def test_invalid_quality_reports_english_choices(self):
        from core import __main__ as cli
        import contextlib
        import io
        error_text = io.StringIO()
        with patch.object(cli, 'glare') as glare, contextlib.redirect_stderr(error_text):
            with self.assertRaises(SystemExit) as error:
                cli.main(['glare', 'scene', 'out', '--quality', 'invalid'])
        self.assertEqual(error.exception.code, 2)
        self.assertIn('quality must be draft, medium or final', error_text.getvalue())
        glare.assert_not_called()

    def test_existing_flags_preserve_backend_contract(self):
        from core import __main__ as cli
        with patch.object(cli, 'glare', return_value={'output': 'out'}) as glare:
            self.assertEqual(cli.main(['glare', 'scene', 'out', '--age', '80', '--quality', 'draft',
                                       '--eye-pigmentation', '0']), 0)
            glare.assert_called_once_with('scene', 'out', vp=None, vd=None, age=80, quality='draft', eye_pigmentation=0.0)


if __name__ == '__main__':
    unittest.main()
