# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""OBJ material, caching and atomic completion behavior during mesh conversion."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
from core import engine as engine
engine.prepare_environment()
from core import render_mesh
from core import processes as processes


class _MeshEnvironment:
    def setUp(self):
        (ROOT / "tests/.tmp").mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix="mesh test ", dir=ROOT / "tests/.tmp")
        self.project = Path(self.tmp.name)
        self.obj = self.project / "model with spaces.obj"
        self.mat = self.project / "material with spaces.rad"
        self.mat.write_text("""void plastic white
0
0
5 .55 .55 .55 0 0
void plastic red
0
0
5 .8 .1 .1 0 0
void plastic green
0
0
5 .1 .7 .1 0 0
""")

    def tearDown(self):
        self.tmp.cleanup()

    def write_obj(self, selections):
        text = ""
        for i, selection in enumerate(selections):
            x = 2 * i
            text += (selection + f"v {x} 0 0\nv {x+1} 0 0\nv {x} 1 0\n"
                      f"f {3*i+1} {3*i+2} {3*i+3}\n")
        self.obj.write_text(text)

    def build(self):
        return render_mesh.build(self.project, self.obj, self.mat)


class MeshFileTest(_MeshEnvironment, unittest.TestCase):
    def test_numeric_records_and_source_are_preserved(self):
        raw = (b"# untouched\r\no sample\r\nv 1e-4 0 0\r\nv 1 0 0\r\n"
               b"v 0 1 0\r\nvt .1 .2\r\nvn 0 0 1\r\ng red\r\n"
               b"f -3/1/1 -2/1/1 -1/1/1\r\n")
        self.obj.write_bytes(raw)
        out = self.project / "temporary.obj"
        self.assertEqual(render_mesh._prepare_obj(self.obj, out), hashlib.sha256(raw).hexdigest())
        self.assertEqual(self.obj.read_bytes(), raw)
        remaining = b"".join(l for l in out.read_bytes().splitlines(keepends=True)
                         if not l.startswith(b"usemtl "))
        self.assertEqual(remaining, raw)

    def test_failed_build_leaves_no_partial_cache(self):
        self.write_obj([""])
        def error(cmd, **kwargs):
            (Path(kwargs["cwd"]) / cmd[-1]).write_bytes(b"half_peak mesh")
            return subprocess.CompletedProcess(cmd, 1, "", "synthetic compiler failure")
        with patch.object(processes, "run", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "synthetic compiler failure"):
                self.build()
        self.assertEqual(list((self.project / "_cache").iterdir()), [])

    def test_invalid_output_with_zero_exit_code_is_rejected(self):
        self.write_obj([""])
        def fake(cmd, **kwargs):
            (Path(kwargs["cwd"]) / cmd[-1]).write_bytes(b"not a mesh" * 100)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        with patch.object(processes, "run", side_effect=fake):
            with self.assertRaisesRegex(RuntimeError, "not in Radiance mesh format"):
                self.build()
        self.assertEqual(list((self.project / "_cache").iterdir()), [])


@unittest.skipUnless(all(shutil.which(t) for t in ("obj2mesh", "obj2rad", "oconv", "rtrace")),
                     "Local Radiance installation required")
class RealMeshTest(_MeshEnvironment, unittest.TestCase):
    def observe(self, rel, quantity, field="M"):
        scene = self.project / "scene.rad"
        kind = "instance" if rel.endswith(".oct") else "mesh"
        scene.write_text("void " + kind + " model\n1 " + rel + "\n0\n0\n")
        p = subprocess.run(["oconv", "-f", "scene.rad"], cwd=self.project,
                           capture_output=True, check=True, timeout=2, env=engine.radiance_environment())
        octree = self.project / "scene.oct"
        octree.write_bytes(p.stdout)
        rays = "".join(f"{2*i+.2} .2 1 0 0 -1\n" for i in range(quantity))
        r = subprocess.run(["rtrace", "-h", "-ab", "0", "-av", "1", "1", "1",
                            "-o" + field, "scene.oct"], cwd=self.project, input=rays,
                           text=True, capture_output=True, check=True, timeout=2, env=engine.radiance_environment())
        return [ln.strip() for ln in r.stdout.splitlines()]

    def obj2rad_materials(self):
        r = subprocess.run(["obj2rad", str(self.obj)], cwd=self.project,
                           text=True, capture_output=True, check=True, timeout=2, env=engine.radiance_environment())
        return [ln.split()[0] for ln in r.stdout.splitlines() if " polygon " in ln]

    def validate_material(self, selections, expected):
        self.write_obj(selections)
        before = self.obj.read_bytes()
        self.assertEqual(self.obj2rad_materials(), expected)
        self.assertEqual(self.observe(self.build(), len(selections)), expected)
        self.assertEqual(self.obj.read_bytes(), before)

    def test_object_only_and_unassigned_surface_use_white(self):
        self.validate_material(["", "o part\n"], ["white", "white"])

    def test_first_group_name_and_empty_group(self):
        self.validate_material(["g red green\n", "g green\n", "g\n"],
                            ["red", "green", "white"])

    def test_usemtl_unaffected_by_later_group(self):
        self.validate_material(["usemtl red\n", "g green\n", "usemtl\n"],
                            ["red", "red", "red"])

    def test_mixed_unassigned_group_and_explicit_material(self):
        self.validate_material(["", "g red\n", "g\n", "usemtl green\n", "g red\n"],
                            ["white", "red", "white", "green", "green"])

    def test_group_with_line_continuation(self):
        self.validate_material(["g \\\nred green\n", "g\n"], ["red", "white"])

    def test_cache_hit_and_source_material_changes(self):
        self.write_obj([""])
        with engine.render_progress.workflow('mesh-test'):
            first = self.build()
            self.assertFalse(engine.render_progress.workflow_status('mesh-test')['cache']['mesh'])
        with patch.object(processes, "run", side_effect=AssertionError("Must not be rebuilt")):
            with engine.render_progress.workflow('mesh-test'):
                self.assertEqual(self.build(), first)
                d = engine.render_progress.workflow_status('mesh-test')
                self.assertTrue(d['cache']['mesh'])
                self.assertEqual(d['step'], 'mesh_ready')
        self.mat.write_text(self.mat.read_text().replace(".55 .55 .55", ".2 .3 .4"))
        colored = self.build()
        self.assertNotEqual(first, colored)
        self.assertEqual([float(x) for x in self.observe(first, 1, "v")[0].split()], [.55, .55, .55])
        self.assertEqual([float(x) for x in self.observe(colored, 1, "v")[0].split()], [.2, .3, .4])
        self.obj.write_text(self.obj.read_text().replace("v 1 0 0", "v 2 0 0"))
        self.assertNotEqual(self.build(), colored)

    def test_dynamic_material_command_does_not_reuse_mesh_after_external_data_changes(self):
        self.write_obj([''])
        external_data = self.project / 'external_material.rad'
        self.mat.write_text('!cat external_material.rad\n')
        material_source = self.mat.read_bytes()
        external_data.write_text('void plastic white\n0\n0\n5 .2 .3 .4 0 0\n')
        first = self.build()
        external_data.write_text('void plastic white\n0\n0\n5 .7 .6 .5 0 0\n')
        suffix = self.build()
        self.assertEqual(self.mat.read_bytes(), material_source)
        self.assertNotEqual(first, suffix)
        self.assertEqual([float(x) for x in self.observe(first, 1, 'v')[0].split()], [.2, .3, .4])
        self.assertEqual([float(x) for x in self.observe(suffix, 1, 'v')[0].split()], [.7, .6, .5])

    def test_partial_unrecorded_and_same_size_corrupt_cache_is_rebuilt(self):
        self.write_obj([""])
        rel = self.build()
        p = self.project / rel
        for record_type in ("truncated", "unrecorded", "same_size"):
            with self.subTest(record_type=record_type):
                if record_type == "truncated":
                    p.write_bytes(p.read_bytes()[:140])
                elif record_type == "unrecorded":
                    p.with_suffix(".json").unlink()
                else:
                    raw = bytearray(p.read_bytes())
                    raw[-1] ^= 1
                    p.write_bytes(raw)
                with patch.object(processes, "run", wraps=processes.run) as run:
                    self.assertEqual(self.build(), rel)
                    self.assertEqual(run.call_count, 1)
                self.assertEqual(self.observe(rel, 1), ["white"])

    def test_concurrent_requests_build_once(self):
        self.write_obj([""])
        with patch.object(processes, "run", wraps=processes.run) as run:
            with ThreadPoolExecutor(max_workers=3) as pool:
                paths = list(pool.map(lambda _: self.build(), range(3)))
            self.assertEqual(len(set(paths)), 1)
            self.assertEqual(run.call_count, 1)

    def test_frozen_oct_preserves_geometry_and_material_at_patch_limit(self):
        self.write_obj(["", "g red\n", "usemtl green\n"])
        # Even zero-area, duplicate and very thin faces pass preparation without a threshold.
        with self.obj.open('a') as f:
            f.write('usemtl white\nf 1 2 3\nf 1 1 1\nv 0 10 0\nv 1e-8 10 0\nv 0 11 0\nf 10 11 12\n')
        source = self.obj.read_bytes()
        run_original = processes.run
        input_faces = []
        def patch_limit(cmd, **kw):
            if cmd[0] == 'obj2mesh':
                return subprocess.CompletedProcess(cmd, 1, '', 'obj2mesh: internal - too many patch triangles in addmeshtri')
            if cmd[0] == 'obj2rad':
                input_faces.extend(l for l in (Path(kw['cwd']) / cmd[1]).read_bytes().splitlines() if l.startswith(b'f '))
            return run_original(cmd, **kw)
        with patch.object(processes, 'run', side_effect=patch_limit) as run:
            rel = self.build()
            self.assertEqual(run.call_count, 3)
        self.assertTrue(rel.endswith('.oct'))
        self.assertEqual(self.obj.read_bytes(), source)
        self.assertEqual(input_faces, [l for l in source.splitlines() if l.startswith(b'f ')])
        self.assertEqual(self.observe(rel, 3), ['white', 'red', 'green'])
        with patch.object(processes, 'run', side_effect=AssertionError('A successfully built OCT must not be rebuilt')):
            self.assertEqual(self.build(), rel)
        self.assertFalse(list((self.project / '_cache').glob('.mesh_*')))

    def test_patch_limit_does_not_silently_accept_uv_loss(self):
        self.write_obj(['usemtl red\n'])
        with self.obj.open('a') as f:
            f.write('vt .1 .2\n')
        with patch.object(processes, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'too many patch triangles')) as run:
            with self.assertRaises(render_mesh.MeshUVSupportError):
                self.build()
        self.assertEqual(run.call_count, 1)
        self.assertFalse(list((self.project / '_cache').iterdir()))

    def test_frozen_oct_failure_leaves_no_partial_cache(self):
        self.write_obj([''])
        def failed(cmd, **kw):
            error = 'too many patch triangles' if cmd[0] == 'obj2mesh' else 'synthetic polygon failure'
            return subprocess.CompletedProcess(cmd, 1, '', error)
        with patch.object(processes, 'run', side_effect=failed):
            with self.assertRaisesRegex(RuntimeError, 'synthetic polygon failure'):
                self.build()
        self.assertFalse(list((self.project / '_cache').iterdir()))


@unittest.skipUnless(all(shutil.which(t) for t in ("obj2mesh", "oconv", "rtrace")),
                     "Local Radiance installation required")
class ChunkedMeshTest(RealMeshTest):
    def setUp(self):
        super().setUp()
        self.threshold = patch.object(render_mesh, '_CHUNK_THRESHOLD', 0)
        self.chunk = patch.object(render_mesh, '_CHUNK_FACES', 2)
        self.threshold.start(); self.chunk.start()
        self.addCleanup(self.threshold.stop); self.addCleanup(self.chunk.stop)

    # Inherited single-file cache/capacity tests cover the legacy small-input path.
    # This class runs only the new bounded-path cases below.
    def test_chunks_preserve_color_uv_normals_and_negative_indices(self):
        self.write_obj(['usemtl white\n', 'usemtl red\n', 'usemtl green\n'])
        with self.obj.open('a') as stream:
            stream.write('vt .1 .2\nvt .8 .2\nvt .1 .8\nvn 0 0 1\n'
                         'usemtl red\nf -3/1/1 -2/2/1 -1/3/1\nf 1 1 1\n')
        original = self.obj.read_bytes()
        calls = []
        run = processes.run
        def inspect(cmd, **kw):
            if cmd[0] == 'obj2mesh':
                calls.append((Path(kw['cwd']) / cmd[-2]).read_bytes())
                self.assertLessEqual(kw['memory_limit'], 768 << 20)
            return run(cmd, **kw)
        with patch.object(processes, 'run', side_effect=inspect):
            rel = self.build()
        self.assertTrue(rel.endswith('.oct'))
        self.assertEqual(self.obj.read_bytes(), original)
        self.assertEqual(self.observe(rel, 2), ['white', 'red'])
        import json
        meta = json.loads((self.project / rel).with_suffix('.json').read_text())
        self.assertEqual(meta['counts'], {'face': 5, 'zero_area': 1, 'chunk': 2})
        self.assertEqual(len(render_mesh.chunk_dependencies(self.project, rel)), 2)
        self.assertTrue(any(b'vt .1 .2' in x and b'vn 0 0 1' in x and b'/1' in x for x in calls))
        with patch.object(processes, 'run', side_effect=AssertionError('Cache should be reused')):
            self.assertEqual(self.build(), rel)
        piece = self.project / meta['chunks'][0]['path']
        piece.write_bytes(piece.read_bytes()[:-1] + b'X')
        self.assertEqual(self.build(), rel)
        self.assertEqual(self.observe(rel, 2), ['white', 'red'])

    def test_chunk_memory_failure_splits_only_affected_chunk(self):
        self.write_obj(['', 'g red\n', 'usemtl green\n'])
        run = processes.run
        seen = []
        def fail_large(cmd, **kw):
            if cmd[0] == 'obj2mesh':
                faces = sum(s.startswith(b'f ') for s in (Path(kw['cwd']) / cmd[-2]).read_bytes().splitlines())
                seen.append(faces)
                if faces > 1:
                    return subprocess.CompletedProcess(cmd, 1, '', 'system - out of memory')
            return run(cmd, **kw)
        with patch.object(processes, 'run', side_effect=fail_large):
            rel = self.build()
        self.assertEqual(seen, [2, 1, 1, 1])
        self.assertEqual(self.observe(rel, 3), ['white', 'red', 'green'])

    def test_invalid_index_leaves_no_chunk_or_partial_cache(self):
        self.write_obj([''])
        with self.obj.open('a') as stream: stream.write('f 0 2 3\n')
        with self.assertRaisesRegex(RuntimeError, 'out of range'):
            self.build()
        self.assertEqual(list((self.project / '_cache').iterdir()), [])

    def test_product_underflow_does_not_discard_small_finite_face(self):
        self.obj.write_text('v 0 0 0\nv 1e-200 0 0\nv 0 1e-200 0\nf 1 2 3\n')
        captured = []
        def compiler(cmd, **kw):
            if cmd[0] == 'obj2mesh':
                captured.append((Path(kw['cwd']) / cmd[-2]).read_text())
                (Path(kw['cwd']) / cmd[-1]).write_bytes(b'#?RADIANCE\nFORMAT=Radiance_tmesh\n\n' + b'0'*160)
            else:
                Path(kw['stdout_path']).write_bytes(b'#?RADIANCE\nFORMAT=Radiance_octree\n\n' + b'0'*160)
            return subprocess.CompletedProcess(cmd, 0, '', '')
        with patch.object(processes, 'run', side_effect=compiler):
            rel = self.build()
        import json
        meta = json.loads((self.project / rel).with_suffix('.json').read_text())
        self.assertEqual(meta['counts']['zero_area'], 0)
        self.assertIn('f 1 2 3', captured[0])

    def test_large_mesh_failure_does_not_fall_back_to_polygons(self):
        self.write_obj([''])
        with patch.object(render_mesh, 'build', side_effect=RuntimeError('out of memory')):
            with self.assertRaisesRegex(RuntimeError, 'polygon fallback was not started'):
                engine._obj_rtm(str(self.project), str(self.obj), self.mat.name)

# Do not rerun inherited small-input tests with forced chunking.
for _name in list(RealMeshTest.__dict__):
    if _name.startswith('test_'):
        setattr(ChunkedMeshTest, _name, None)


if __name__ == "__main__":
    unittest.main()
