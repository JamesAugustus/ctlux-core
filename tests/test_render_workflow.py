# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Measured preparation stages without a Radiance process or user project."""
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
from core import render_progress as ri
from core import render_mesh


class RenderWorkflowTest(unittest.TestCase):
    def test_stage_and_step_timers_do_not_invent_overall_percentage(self):
        with patch.object(ri.time, 'monotonic', side_effect=[10, 12, 15, 19, 20, 22]):
            with ri.workflow('a'):
                ri.step('mesh_build', 'obj2mesh', detail='model.obj', cache={'mesh': False})
                d = ri.workflow_status('a')
                self.assertEqual((d['elapsed_seconds'], d['stage_seconds'], d['step_seconds']), (5, 5, 3))
                self.assertIsNone(d['overall_percent'])
                self.assertIsNone(d['eta_seconds'])
                self.assertIsNone(d['total'])
                ri.step('render', 'Radiance', stage='render')
                d = ri.workflow_status('a')
                self.assertEqual((d['elapsed_seconds'], d['stage_seconds'], d['step_seconds']), (10, 1, 1))
                self.assertEqual(d['cache'], {'mesh': False})
            self.assertIsNone(ri.workflow_status('a'))

    def test_counter_update_does_not_reset_step_timer(self):
        with patch.object(ri.time, 'monotonic', side_effect=[0, 1, 3, 4]):
            with ri.workflow('a'):
                ri.step('obj', 'copy', completed=0, total=20, unit='bytes')
                ri.step('obj', 'copy', completed=10, total=20, unit='bytes')
                d = ri.workflow_status('a')
                self.assertEqual(d['step_seconds'], 3)
                self.assertEqual((d['completed'], d['total'], d['unit']), (10, 20, 'bytes'))
                self.assertIsNone(d['overall_percent'])

    def test_previous_owner_cannot_update_or_delete_new_job_record(self):
        started, continuation, stop_event = threading.Event(), threading.Event(), threading.Event()
        errors = []
        def new():
            try:
                with ri.workflow('new'):
                    ri.step('mesh', 'new job')
                    started.set()
                    if not continuation.wait(3):
                        raise AssertionError('test timed out')
                    self.assertEqual(ri.workflow_status('new')['step'], 'mesh')
            except BaseException as e:
                errors.append(e)
            finally:
                stop_event.set()
        th = threading.Thread(target=new)
        with ri.workflow('previous'):
            th.start()
            self.assertTrue(started.wait(2))
            self.assertFalse(ri.step('late', 'old job'))
            self.assertIsNone(ri.workflow_status('previous'))
        self.assertEqual(ri.workflow_status('new')['step'], 'mesh')
        continuation.set()
        self.assertTrue(stop_event.wait(2))
        th.join()
        if errors:
            raise errors[0]
        self.assertIsNone(ri.workflow_status('new'))

    def test_another_thread_cannot_modify_owner_step(self):
        results = []
        with ri.workflow('a'):
            th = threading.Thread(target=lambda: results.append(ri.step('wrong', 'wrong')))
            th.start(); th.join()
            self.assertEqual(results, [False])
            self.assertEqual(ri.workflow_status('a')['step'], 'bounds')

    def test_error_clears_record_and_counter_is_validated(self):
        with self.assertRaisesRegex(RuntimeError, 'failure'):
            with ri.workflow('a'):
                for done, total in ((-1, 2), (3, 2), (True, 2), (1, None)):
                    with self.assertRaises(ValueError):
                        ri.step('x', 'x', completed=done, total=total)
                raise RuntimeError('failure')
        self.assertIsNone(ri.workflow_status('a'))
        self.assertFalse(ri.step('x', 'x'))

    def test_obj_copy_measures_actual_bytes_and_preserves_source(self):
        with tempfile.TemporaryDirectory() as td:
            src, dst = Path(td) / 'input.obj', Path(td) / 'output.obj'
            raw = b'g floor\nv 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n'
            src.write_bytes(raw)
            with ri.workflow('a'):
                render_mesh._prepare_obj(src, dst)
                d = ri.workflow_status('a')
                self.assertEqual((d['completed'], d['total'], d['unit']), (len(raw), len(raw), 'bytes'))
                self.assertIsNone(d['overall_percent'])
            self.assertEqual(src.read_bytes(), raw)
            self.assertIn(b'usemtl floor', dst.read_bytes())

    def test_obj_copy_cancellation_exits_without_starting_new_process(self):
        with tempfile.TemporaryDirectory() as td:
            src, dst = Path(td) / 'input.obj', Path(td) / 'output.obj'
            src.write_bytes(b'v 0 0 0\n' * 140000)
            with patch.object(render_mesh.processes, 'check_cancellation',
                              side_effect=[None, render_mesh.processes.OperationCancelled('cancelled')]):
                with self.assertRaises(render_mesh.processes.OperationCancelled):
                    render_mesh._prepare_obj(src, dst)
            self.assertLess(dst.stat().st_size, src.stat().st_size)


if __name__ == '__main__':
    unittest.main()
