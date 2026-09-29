# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Small synthetic conversions: safe argv, geometry, and atomic delivery."""
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import warnings

ROOT = Path(__file__).resolve().parents[1]
from core import assimp_bridge as assimp_bridge
from core import processes as processes

V = 'v 0 0 0\nv 1 0 0\nv 0 1 0\n'
OBJ = V + 'f 1 2 3\n'
MTL = 'newmtl source\nKd .2 .4 .6\n'


class AssimpBridgeTest(unittest.TestCase):
    def setUp(self):
        (ROOT / 'tests/.tmp').mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / 'tests/.tmp', prefix='assimp bridge ')
        self.pd = Path(self.tmp.name)
        self.source = self.pd / 'source.fbx'
        self.source.write_bytes(b'synthetic source')
        self.target = self.pd / 'target.obj'
        self.target.write_bytes(b'previous target')

    def tearDown(self):
        self.tmp.cleanup()

    def mock_run(self, obj=OBJ, mtl=None, code=0, extra=None):
        def run(cmd, **kw):
            self.assertIsInstance(cmd, list)
            self.assertEqual(cmd[1], 'export')
            self.assertEqual(cmd[2], str(self.source))
            self.assertEqual(cmd[4:], ['-fobj'])
            self.assertNotIn('shell', kw)
            p = Path(cmd[3])
            self.assertEqual(p.parent.parent, self.target.parent)
            self.assertEqual(kw['cwd'], str(p.parent))
            if obj is not None:
                p.write_text(obj, encoding='utf-8')
            if mtl is not None:
                p.with_suffix('.mtl').write_text(mtl, encoding='utf-8')
            if extra:
                extra(p)
            return subprocess.CompletedProcess(cmd, code, '', 'synthetic error' if code else '')
        return run

    def convert(self, run, timeout=120):
        with patch.object(assimp_bridge.shutil, 'which', return_value='/synthetic/assimp'), \
             patch.object(processes, 'run', side_effect=run) as cli:
            result = assimp_bridge.convert(self.source, self.target, timeout)
        return result, cli

    def mtl(self):
        ref = next(s[7:] for s in self.target.read_text().splitlines() if s.startswith('mtllib '))
        return self.target.parent / ref

    def assert_old_preserved(self):
        self.assertEqual(self.target.read_bytes(), b'previous target')
        self.assertFalse(list(self.pd.glob('.assimp_*')))

    def test_argv_special_characters_and_new_target(self):
        self.source = self.pd / 'input $(touch ESCAPED) `touch ESCAPED2` "\'.fbx'
        self.source.write_bytes(b'synthetic')
        self.target = self.pd / 'subdirectory' / 'output $ ` "\'.obj'
        result, cli = self.convert(self.mock_run(), timeout=300)
        self.assertEqual(result, str(self.target))
        self.assertEqual(self.target.read_text(), OBJ)
        self.assertEqual(cli.call_args.kwargs['timeout'], 300)
        self.assertFalse((self.pd / 'ESCAPED').exists())
        self.assertFalse(list(self.target.parent.glob('.assimp_*')))

    def test_negative_vertex_uv_normal_indices_use_current_records(self):
        obj = (V + 'vt 0 0\nvt 1 0\nvt 0 1\nvn 0 0 1\n'
               'f -3/-3/-1 -2/-2/-1 -1/-1/-1\nv 99 99 99\n')
        self.convert(self.mock_run(obj))
        self.assertEqual(self.target.read_text(), obj)

    def test_vertex_normal_slash_and_small_nondegenerate_triangle_are_preserved(self):
        obj = 'v 0 0 0\nv 1e-200 0 0\nv 0 1e-200 0\nvn 0 0 1\nf 1//1 2//1 3//1\n'
        self.convert(self.mock_run(obj))
        self.assertEqual(self.target.read_text(), obj)

    def test_failure_timeout_and_missing_new_output_preserve_old_target(self):
        def timeout(*args, **kw):
            raise RuntimeError('Process timed out')
        for run in (self.mock_run(code=1), self.mock_run(None), timeout):
            with self.subTest(run=run), self.assertRaises(RuntimeError):
                self.convert(run)
            self.assert_old_preserved()

    def test_invalid_geometry_is_never_delivered(self):
        invalid = [V, V+'f 1 2\n', V+'f 0 2 3\n', V+'f 1 2 4\n',
                 V+'f -4 -2 -1\n', V+'f 1 2 x\n', V+'f 1/ 2 3\n',
                 V+'f 1// 2 3\n', V+'f 1/1 2/1 3/1\n',
                 V+'f 1//1 2//1 3//1\n', V+'f 1/1/1/1 2 3\n',
                 V+'f 1 1 3\n', V.replace('1 0 0', 'nan 0 0')+'f 1 2 3\n',
                 V.replace('1 0 0', 'inf 0 0')+'f 1 2 3\n',
                 V.replace('v 0 1 0', 'v 2 0 0')+'f 1 2 3\n',
                 V.replace('v 0 1 0', 'v 0 1')+'f 1 2 3\n',
                 V+'f 1 2 3\\', V.replace('v 0 1 0', 'v 0 1 0 2')+'f 1 2 3\n']
        for obj in invalid:
            with self.subTest(obj=obj), self.assertRaises(RuntimeError):
                self.convert(self.mock_run(obj))
            self.assert_old_preserved()

    def test_same_file_hard_link_and_symlink_are_not_processed(self):
        hard = self.pd / 'hard.obj'; os.link(self.source, hard)
        link = self.pd / 'link.obj'; link.symlink_to(self.source.name)
        for target in (self.source, hard, link):
            with self.subTest(target=target), patch.object(processes, 'run') as cli:
                with self.assertRaises(ValueError):
                    assimp_bridge.convert(self.source, target)
                cli.assert_not_called()
        self.assertEqual(self.source.read_bytes(), b'synthetic source')

    def test_source_and_timeout_validation(self):
        for timeout in (0, -1, float('inf'), float('nan'), '120'):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                assimp_bridge.convert(self.source, self.target, timeout)
        with self.assertRaisesRegex(RuntimeError, 'source'):
            assimp_bridge.convert(self.pd / 'missing.fbx', self.target)
        self.assert_old_preserved()

    def test_mtl_and_unicode_texture_paths_preserve_options(self):
        # Accented and Greek characters plus spaces exercise Unicode texture paths.
        texture = self.pd / 'Café texture α.png'; texture.write_bytes(b'synthetic texture bytes')
        obj = 'mtllib model.mtl\nusemtl source\n' + OBJ
        line = 'map_Kd -s 1 2 3 -o .1 .2 -bm .5 "Café texture α.png"\n'
        self.convert(self.mock_run(obj, MTL+line))
        mtl = self.mtl()
        self.assertEqual(mtl.parent.parent, self.pd)
        self.assertTrue(mtl.parent.name.startswith('assimp_assets_'))
        mapline = next(s for s in mtl.read_text().splitlines() if s.startswith('map_Kd'))
        self.assertTrue(mapline.startswith('map_Kd -s 1 2 3 -o .1 .2 -bm .5 texture_'))
        self.assertEqual((mtl.parent / mapline.split()[-1]).read_bytes(), texture.read_bytes())
        self.assertIn('Kd .2 .4 .6', mtl.read_text())

    def test_missing_external_symlink_and_ambiguous_map_references_are_preserved_not_read(self):
        outside = '/assimp_test_outside_clone/texture.png'
        (self.pd / 'external_link').symlink_to('/assimp_test_outside_clone', target_is_directory=True)
        lines = ['map_Kd ' + outside, r'map_Ks C:\sample_machine\texture.jpg',
                 'map_bump external_link/texture.png', 'map_Kd missing.png',
                 'map_Kd -unknown 9 texture.png',
                 'map_Kd ../../../../../../assimp_test_outside_clone/texture.png']
        obj = 'mtllib model.mtl\nusemtl source\n'+OBJ
        orig_stat = Path.stat
        def stat(p, *args, **kw):
            self.assertFalse(str(p).startswith('/assimp_test_outside_clone'))
            return orig_stat(p, *args, **kw)
        with patch.object(Path, 'stat', stat), warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            self.convert(self.mock_run(obj, MTL+'\n'.join(lines)+'\n'))
        text = self.mtl().read_text()
        for line in lines:
            self.assertIn(line+'\n', text)
        self.assertEqual(len(caught), 1)
        self.assertIn('6 texture', str(caught[0].message))
        self.assertEqual(text.count('original reference preserved'), 6)

    def test_target_preserved_when_warning_is_treated_as_error(self):
        with warnings.catch_warnings():
            warnings.simplefilter('error', RuntimeWarning)
            with self.assertRaises(RuntimeWarning):
                self.convert(self.mock_run('mtllib model.mtl\n'+OBJ, MTL+'map_Kd missing.png\n'))
        self.assert_old_preserved()

    def test_missing_external_mtl_and_undefined_material_are_rejected(self):
        for obj, mtl in [('mtllib missing.mtl\n'+OBJ, None),
                         ('mtllib /sample/model.mtl\n'+OBJ, None),
                         ('mtllib ../model.mtl\n'+OBJ, None),
                         ('usemtl missing\n'+OBJ, None),
                         ('mtllib model.mtl\nusemtl missing\n'+OBJ, MTL),
                         ('mtllib model.mtl\n'+OBJ, 'newmtl\n')]:
            with self.subTest(obj=obj), self.assertRaises(RuntimeError):
                self.convert(self.mock_run(obj, mtl))
            self.assert_old_preserved()

    def test_symlink_obj_and_mtl_outputs_are_rejected(self):
        for kind in ('obj', 'mtl'):
            def extra(p):
                q = p if kind == 'obj' else p.with_suffix('.mtl')
                q.unlink(missing_ok=True)
                q.symlink_to(self.source)
            with self.subTest(kind=kind), self.assertRaises(RuntimeError):
                self.convert(self.mock_run('mtllib model.mtl\n'+OBJ, MTL, extra=extra))
            self.assert_old_preserved()

    def test_content_addressed_asset_reused_without_changing_old_material(self):
        obj = 'mtllib model.mtl\nusemtl source\n'+OBJ
        self.convert(self.mock_run(obj, MTL))
        old = self.mtl(); oldbytes = old.read_bytes()
        self.convert(self.mock_run(obj, MTL))
        self.assertEqual(self.mtl(), old)
        self.assertEqual(len(list(self.pd.glob('assimp_assets_*'))), 1)
        self.convert(self.mock_run(obj, MTL.replace('.2 .4 .6', '.8 .4 .1')))
        self.assertNotEqual(self.mtl(), old)
        self.assertEqual(old.read_bytes(), oldbytes)

    def test_corrupt_asset_directory_is_not_silently_overwritten(self):
        obj = 'mtllib model.mtl\nusemtl source\n'+OBJ
        self.convert(self.mock_run(obj, MTL))
        mtl = self.mtl(); mtl.write_bytes(b'modified')
        self.target.write_bytes(b'previous target')
        with self.assertRaisesRegex(RuntimeError, 'content'):
            self.convert(self.mock_run(obj, MTL))
        self.assert_old_preserved()
        self.assertEqual(mtl.read_bytes(), b'modified')

    def test_atomic_obj_delivery_failure_preserves_old_obj_and_mtl(self):
        obj = 'mtllib model.mtl\nusemtl source\n'+OBJ
        self.convert(self.mock_run(obj, MTL))
        old_mtl = self.mtl()
        old_material = old_mtl.read_bytes()
        self.target.write_bytes(b'previous target')
        with patch.object(assimp_bridge.os, 'replace', side_effect=OSError('synthetic delivery error')):
            with self.assertRaisesRegex(OSError, 'delivery'):
                self.convert(self.mock_run(obj, MTL.replace('.2 .4 .6', '.8 .8 .8')))
        self.assert_old_preserved()
        self.assertEqual(old_mtl.read_bytes(), old_material)

    def test_multiple_mtl_files_and_line_continuations_are_preserved(self):
        obj = ('mtllib model.mtl "second material.mtl"\nusemtl second\n'+V+
               'f 1 2 \\\n3\n')
        def extra(p):
            (p.parent / 'second material.mtl').write_text('newmtl second\nKd .9 .8 .7\n')
        self.convert(self.mock_run(obj, MTL, extra=extra))
        text = self.target.read_text()
        self.assertIn('f 1 2  3\n', text)
        refs = [s[7:] for s in text.splitlines() if s.startswith('mtllib ')]
        self.assertEqual(len(refs), 2)
        self.assertTrue(all((self.pd / ref).is_file() for ref in refs))

    def test_cancellation_does_not_deliver_new_output(self):
        run = self.mock_run()
        def cancel_error():
            raise RuntimeError('Process canceled')
        def cancel(cmd, **kw):
            result = run(cmd, **kw)
            processes.check_cancellation = cancel_error
            return result
        with patch.object(processes, 'check_cancellation', wraps=processes.check_cancellation):
            with self.assertRaisesRegex(RuntimeError, 'canceled'):
                self.convert(cancel)
        self.assert_old_preserved()

    def test_real_process_timeout_preserves_existing_target(self):
        cli = self.pd / 'delayed assimp'
        # The kernel splits venv paths containing spaces in a shebang. Pass argv safely.
        cli.write_text('#!/bin/sh\nexec '+shlex.quote(sys.executable)+
                       " -c 'import time; time.sleep(10)'\n")
        cli.chmod(0o700)
        with patch.object(assimp_bridge.shutil, 'which', return_value=str(cli)):
            with self.assertRaisesRegex(RuntimeError, 'timed out'):
                assimp_bridge.convert(self.source, self.target, timeout=.05)
        self.assert_old_preserved()
        self.assertFalse(processes._active)

    @unittest.skipUnless(shutil.which('assimp'), 'A local Assimp CLI is required')
    def test_real_assimp_small_stl_with_special_name(self):
        self.source = self.pd / 'triangle $(touch ESCAPED) `touch ESCAPED2` "\'.stl'
        self.source.write_text('solid test\nfacet normal 0 0 1\nouter loop\n'
                               'vertex 0 0 0\nvertex 1 0 0\nvertex 0 1 0\n'
                               'endloop\nendfacet\nendsolid test\n')
        result = assimp_bridge.convert(self.source, self.target)
        self.assertEqual(result, str(self.target))
        self.assertTrue(assimp_bridge._validate_obj(self.target))
        self.assertTrue(self.mtl().is_file())
        self.assertFalse((self.pd / 'ESCAPED').exists())
        self.assertFalse((self.pd / 'ESCAPED2').exists())


if __name__ == '__main__':
    unittest.main()
