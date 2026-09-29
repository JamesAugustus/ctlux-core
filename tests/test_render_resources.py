# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Test the OOM regression using small inputs without running a large user scene."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
from core import render_resources as K
from core import render_parallel as P
from core import processes as processes


class ResourceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cache = self.root / '_cache'
        self.cache.mkdir()

    def scene(self, size=900 << 20):
        octree = self.cache / 'scene_test.oct'
        octree.write_bytes(b'x' * 256)
        mesh = self.cache / 'mesh_test.oct'
        # Sparse file: test size accounting without allocating 900 MiB of RAM/disk space.
        with mesh.open('wb') as stream:
            stream.truncate(size)
        record = Path(str(octree) + '.json')
        record.write_text(json.dumps({'derived': [{'path': '_cache/mesh_test.oct'}] * 2}))
        return octree, mesh, record

    def test_referenced_geometry_counted_once_and_six_workers_remain_limited(self):
        octree, mesh, _ = self.scene()
        expected = 256 + (900 << 20)
        self.assertEqual(P._scene_size(octree), expected)
        response = subprocess.CompletedProcess([], 0, '-x 320 -y 233\n', '')
        with patch.object(P, '_cpu_count', return_value=16), \
             patch.object(P, '_available_memory', return_value=8 << 30), \
             patch.object(P.shutil, 'which', return_value='/bin/rpiece'), \
             patch.object(processes, 'run', return_value=response):
            plan = P.plan_render(320, 240, 'draft', octree, args=['-ab', '2'])
        self.assertEqual(plan['workers'], 2)
        self.assertEqual(plan['scene_bytes'], expected)
        self.assertLessEqual(plan['worker_memory'] * plan['workers'], 4 << 30)
        mesh.unlink()
        with self.assertRaises(OSError):
            P._scene_size(octree)

    def test_dependency_cannot_escape_project(self):
        octree, _, record = self.scene(256)
        record.write_text(json.dumps({'derived': [{'path': '../../other.oct'}]}))
        with self.assertRaisesRegex(ValueError, 'outside the project'):
            P._scene_size(octree)

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux address space limit')
    def test_native_child_cannot_exceed_memory_limit(self):
        code = ('import sys\ntry:\n x=bytearray(256*1024*1024)\n'
                'except MemoryError:\n print("bounded");sys.exit(77)\n')
        result = processes.run([sys.executable, '-c', code],
                                  memory_limit=128 << 20, timeout=5)
        self.assertEqual(result.returncode, 77)
        self.assertIn('bounded', result.stdout)
        self.assertFalse(processes._active)

    @unittest.skipIf(os.name == 'nt', 'POSIX interprocess lock')
    def test_second_program_cannot_overlap_preparation_and_rendering(self):
        code = ('import sys;sys.path.insert(0,sys.argv[1]);from core import processes as processes\n'
                'try:\n with processes.render_queue(): print("entered")\n'
                'except RuntimeError as e:\n print(str(e));sys.exit(42)\n')
        with processes.render_queue():
            with processes.render_queue():
                result = processes.run([sys.executable, '-c', code, str(ROOT)], timeout=5)
            self.assertEqual(result.returncode, 42)
            self.assertIn('Another CTLux/Radiance', result.stdout)
        result = processes.run([sys.executable, '-c', code, str(ROOT)], timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertIn('entered', result.stdout)

    def test_low_memory_terminates_running_job_and_releases_lock(self):
        with patch.object(K, 'available_memory', side_effect=[4 << 30, 4 << 30, 32 << 20]):
            with self.assertRaisesRegex(K.InsufficientMemory, 'available memory fell below the limit'):
                with processes.render_queue():
                    processes.run([sys.executable, '-c', 'import time;time.sleep(30)'], timeout=5)
        self.assertFalse(processes._active)
        with processes.render_queue():
            result = processes.run([sys.executable, '-c', 'print("ready")'], timeout=5)
        self.assertEqual(result.stdout.strip(), 'ready')

    def test_low_memory_starts_no_process(self):
        with patch.object(K, 'available_memory', return_value=32 << 20), \
             patch.object(processes.subprocess, 'Popen') as popen:
            with self.assertRaises(K.InsufficientMemory):
                with processes.render_queue():
                    processes.run(['rpict'])
        popen.assert_not_called()


if __name__ == '__main__':
    unittest.main()
