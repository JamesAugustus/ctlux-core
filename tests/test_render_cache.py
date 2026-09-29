# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Small real regression tests for scene dependencies, partial output and cancellation."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
from core import engine as engine
from core import render_cache as render_cache
from core import processes as processes
engine.prepare_environment()


class FileBackedTest(unittest.TestCase):
    def setUp(self):
        (ROOT / 'tests/.tmp').mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / 'tests/.tmp', prefix='cache test ')
        self.pd = Path(self.tmp.name)
        for d in ('model', 'material', 'texture', 'light', 'sky', 'view', '_cache', 'render', 'output'):
            (self.pd / d).mkdir(exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()


class ProcessTest(FileBackedTest):
    def test_scene_derived_records_cannot_be_missing_invalid_or_out_of_bounds(self):
        octree = self.pd / '_cache/scene.oct'; octree.write_bytes(b'scene' * 30)
        mesh = self.pd / '_cache/mesh.rtm'; mesh.write_bytes(b'mesh' * 30)
        record = self.pd / '_cache/scene.oct.json'
        sha = render_cache.file_digest(octree)
        dep = {'path': '_cache/mesh.rtm', 'sha256': render_cache.file_digest(mesh)}
        for deps in (None, {}, 'invalid', [None], [{}], [{'path': '../mesh.rtm', 'sha256': dep['sha256']}],
                     [{'path': str(mesh), 'sha256': dep['sha256']}], [{'path': None, 'sha256': dep['sha256']}]):
            with self.subTest(deps=deps):
                record.write_text(json.dumps({'sha256': sha, 'derived': deps}))
                self.assertTrue(render_cache.valid_output(octree, record))
                self.assertFalse(render_cache.valid_scene(self.pd, octree, record))
        record.write_text(json.dumps({'sha256': sha}))
        self.assertFalse(render_cache.valid_scene(self.pd, octree, record))
        record.write_text(json.dumps({'sha256': sha, 'derived': [dep]}))
        self.assertTrue(render_cache.valid_scene(self.pd, octree, record))
        mesh.write_bytes(b'fake' * 30)
        self.assertFalse(render_cache.valid_scene(self.pd, octree, record))
        mesh.unlink()
        self.assertFalse(render_cache.valid_scene(self.pd, octree, record))
        record.write_text(json.dumps({'sha256': sha, 'derived': []}))
        self.assertTrue(render_cache.valid_scene(self.pd, octree, record))

    def test_record_of_wrong_type_is_invalid_cache(self):
        p = self.pd / 'output.bin'; p.write_bytes(b'x' * 100)
        record = self.pd / 'record.json'
        for data in (None, [], 123, 'wrong', {}):
            record.write_text(json.dumps(data))
            self.assertFalse(render_cache.valid_output(p, record))

    def test_binary_stdout_bytes_unchanged(self):
        p = self.pd / 'binary'
        r = processes.run([sys.executable, '-c',
            'import sys;sys.stdout.buffer.write(bytes(range(256)))'], stdout_path=p)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(p.read_bytes(), bytes(range(256)))
        self.assertEqual(r.stdout, '')

    def test_mid_line_command_marks_rad_dynamic(self):
        rad = self.pd / 'model/mid.rad'
        for text, dynamic in (('void plastic m 0 0 5 .5 .5 .5 0 0\n', False),
                              ('void plastic m 0 0 5 .5 .5 .5 0 0 !echo x\n', True),
                              ('\tm polygon p !cat part.rad\n', True),
                              # Read in 1 MiB chunks. A marker after the first chunk is still found.
                              ('#' * (1 << 20) + '\n!echo x\n', True)):
            with self.subTest(text=text):
                rad.write_text(text)
                self.assertEqual(render_cache.has_dynamic_rad([(str(rad), render_cache.file_digest(rad))]), dynamic)

    def test_start_failure_leaves_no_active_process(self):
        with self.assertRaises(FileNotFoundError):
            processes.run([str(self.pd / 'missing')], stdout_path=self.pd / 'out')
        self.assertFalse(processes._active)


@unittest.skipUnless(all(shutil.which(x) for x in ('oconv', 'obj2rad', 'obj2mesh', 'rpict')),
                     'Local Radiance required')
class SceneTest(FileBackedTest):
    def setUp(self):
        super().setUp()
        self.mat = self.pd / 'material/custom.rad'
        self.mat.write_text('void plastic white\n0\n0\n5 .5 .5 .5 0 0\n')
        self.obj = self.pd / 'model/a.obj'
        self.obj.write_text('usemtl white\nv -2 -2 0\nv 2 -2 0\nv 0 2 0\nf 1 2 3\n')
        self.p = {'geometry': [{'file': 'model/a.obj', 'render_mesh': True}],
                  'material': 'material/custom.rad', 'luminaires': []}
        self.sky = 'void glow sky\n0\n0\n4 1 1 1 0\nsky source dome\n0\n0\n4 0 0 1 180\n'

    def build(self, p=None):
        return engine.compile_scene(str(self.pd), p or self.p, self.sky)

    def test_camera_and_resolution_do_not_rebuild_scene(self):
        with patch.object(processes, 'run', wraps=processes.run) as run:
            first = self.build()
            p = copy.deepcopy(self.p); p.update(target=[1, 2, 3], parse='320x240')
            self.assertEqual(first, self.build(p))
        self.assertEqual(sum(c.args[0][0] == 'oconv' for c in run.call_args_list), 1)
        self.assertEqual(len(list((self.pd / '_cache').glob('*.rtm'))), 1)

    def test_chunked_scene_validates_submeshes_and_counts_memory(self):
        import json; from core import render_mesh; from core import render_parallel as render_parallel
        with patch.object(render_mesh, '_CHUNK_THRESHOLD', 0):
            octree = self.build()
            meta = json.loads(Path(octree + '.json').read_text())
            self.assertGreaterEqual(len(meta['derived']), 2)
            expected = Path(octree).stat().st_size + sum((self.pd / x['path']).stat().st_size for x in meta['derived'])
            self.assertEqual(render_parallel._scene_size(octree), expected)
            part = next(self.pd / x['path'] for x in meta['derived'] if x['path'].endswith('.rtm'))
            part.unlink()
            self.assertFalse(render_cache.valid_scene(str(self.pd), octree, octree + '.json'))
            self.assertEqual(self.build(), octree)
            self.assertTrue(render_cache.valid_scene(str(self.pd), octree, octree + '.json'))

    def test_missing_or_corrupt_mesh_is_repaired_when_scene_is_valid(self):
        first = self.build()
        mesh = next((self.pd / '_cache').glob('mesh_*.rtm'))
        for record_type in ('invalid', 'missing'):
            with self.subTest(record_type=record_type):
                if record_type == 'invalid':
                    raw = bytearray(mesh.read_bytes()); raw[-1] ^= 1
                    mesh.write_bytes(raw)
                else:
                    mesh.unlink()
                self.assertTrue(render_cache.valid_output(first, first + '.json'))
                self.assertFalse(render_cache.valid_scene(self.pd, first, first + '.json'))
                with patch.object(processes, 'run', wraps=processes.run) as run:
                    self.assertEqual(self.build(), first)
                self.assertTrue(any(c.args[0][0] == 'obj2mesh' for c in run.call_args_list))
                self.assertTrue(any(c.args[0][0] == 'oconv' for c in run.call_args_list))
                self.assertTrue(render_cache.valid_scene(self.pd, first, first + '.json'))
                hdr = self.pd / 'render/repair.hdr'
                engine.render(first, '-vp 0 0 1 -vd 0 0 -1 -vu 0 1 0', 'preview', 8, 8, str(hdr))
                self.assertGreater(hdr.stat().st_size, 64)

    def test_material_and_geometry_changes_build_new_scene(self):
        first = self.build(); old = Path(first).read_bytes()
        self.mat.write_text(self.mat.read_text().replace('.5 .5 .5', '.2 .2 .2'))
        second = self.build()
        self.assertNotEqual(first, second)
        self.obj.write_text(self.obj.read_text().replace('2 -2', '3 -2'))
        self.assertNotEqual(second, self.build())
        self.assertEqual(Path(first).read_bytes(), old)

    def test_external_data_changes_ambient_key(self):
        sidecar = self.pd / 'light/distribution.dat'; sidecar.write_text('1')
        first = self.build()
        sidecar.write_text('2')
        new = self.build()
        self.assertNotEqual(first, new)
        self.assertNotEqual(engine._ambient_file(first), engine._ambient_file(new))

    def test_invalid_octree_and_failed_build_are_not_reused(self):
        first = self.build()
        Path(first).write_bytes(b'partial')
        with patch.object(processes, 'run', wraps=processes.run) as run:
            self.assertEqual(first, self.build())
        self.assertTrue(any(c.args[0][0] == 'oconv' for c in run.call_args_list))
        self.assertTrue(render_cache.valid_output(first, first + '.json'))
        self.mat.write_text(self.mat.read_text() + '\n# New input\n')
        def invalid(cmd, **kw):
            Path(kw['stdout_path']).write_bytes(b'partial')
            return subprocess.CompletedProcess(cmd, 1, '', 'synthetic build failure')
        with patch.object(engine, '_geometry_line', return_value=''), patch.object(processes, 'run', side_effect=invalid):
            with self.assertRaisesRegex(RuntimeError, 'oconv'):
                self.build()
        self.assertEqual(len(list((self.pd / '_cache').glob('scene_*.oct'))), 1)
        self.assertFalse(list((self.pd / '_cache').glob('*.tmp')))

    def test_small_obj_change_with_same_mtime_does_not_reuse_stale_rad(self):
        first = engine._obj_rad(str(self.pd), str(self.obj))
        st = self.obj.stat()
        self.obj.write_text(self.obj.read_text().replace('2 -2', '3 -2'))
        os.utime(self.obj, ns=(st.st_atime_ns, st.st_mtime_ns))
        new = engine._obj_rad(str(self.pd), str(self.obj))
        self.assertNotEqual(first, new)

    def test_dynamic_rad_command_does_not_reuse_stale_octree(self):
        source = self.pd / 'model/dynamic.rad'
        source.write_text("!printf '# Variable source test\\n'\n")
        self.p['geometry'].append({'file': 'model/dynamic.rad'})
        self.assertNotEqual(self.build(), self.build())

    def test_mid_line_rad_command_does_not_reuse_stale_octree(self):
        source = self.pd / 'model/dynamic.rad'
        source.write_text("void plastic grey 0 0 5 .5 .5 .5 0 0 !printf '# Variable source test\\n'\n")
        self.p['geometry'].append({'file': 'model/dynamic.rad'})
        self.assertNotEqual(self.build(), self.build())

    def test_light_change_in_custom_directory_rebuilds_scene(self):
        custom = self.pd / 'custom'; custom.mkdir()
        rad = custom / 'light.rad'
        rad.write_text('void light lamp\n0\n0\n3 1 1 1\nlamp sphere sphere\n0\n0\n4 0 0 2 .1\n')
        self.p['lights'] = ['custom/light.rad']
        first = engine.compile_scene(str(self.pd), self.p, self.sky, night_lights=True)
        rad.write_text(rad.read_text().replace('3 1 1 1', '3 2 2 2'))
        new = engine.compile_scene(str(self.pd), self.p, self.sky, night_lights=True)
        self.assertNotEqual(first, new)

    def test_obj_changed_during_conversion_is_not_published(self):
        def modify(cmd, **kw):
            self.obj.write_text(self.obj.read_text() + '# Changed\n')
            Path(kw['stdout_path']).write_bytes(b'x' * 100)
            return subprocess.CompletedProcess(cmd, 0, '', '')
        with patch.object(processes, 'run', side_effect=modify):
            with self.assertRaisesRegex(RuntimeError, 'source changed'):
                engine._obj_rad(str(self.pd), str(self.obj))
        self.assertFalse(list((self.pd / '_cache').glob('geo_*')))

    def test_rpict_error_preserves_previous_hdr(self):
        octp = self.build(); hdr = self.pd / 'render/result.hdr'; hdr.write_bytes(b'previous')
        def invalid(cmd, **kw):
            Path(kw['stdout_path']).write_bytes(b'partial')
            return subprocess.CompletedProcess(cmd, 1, '', 'synthetic rpict failure')
        with patch.object(processes, 'run', side_effect=invalid):
            with self.assertRaisesRegex(RuntimeError, 'rpict'):
                engine.render(octp, '-vp 0 0 1 -vd 0 0 -1 -vu 0 1 0', 'preview', 16, 16, str(hdr))
        self.assertEqual(hdr.read_bytes(), b'previous')
        self.assertFalse(list(hdr.parent.glob('*.tmp')))

    def test_preview_disables_indirect_lighting_and_rejects_analysis(self):
        q = engine.QUALITY['preview'].split()
        self.assertEqual(q[q.index('-ab') + 1], '0')
        with self.assertRaisesRegex(ValueError, 'analysis'):
            engine.render('unused', '', 'preview', irradiance=True)


if __name__ == '__main__':
    unittest.main()
