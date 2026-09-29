# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Descendants that closed their pipes must also terminate when the synchronous command finishes."""
import os
from pathlib import Path
import signal
import sys
import time
import unittest

from core import processes as processes


@unittest.skipUnless(sys.platform.startswith('linux'), 'Linux process status evidence')
class ProcessTerminationTest(unittest.TestCase):
    def test_leader_exit_leaves_no_silent_descendant(self):
        script = """import os, subprocess, sys
p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print(p.pid, flush=True)
sys.exit(int(sys.argv[1]))
"""
        for exit_code in (0, 3):
            with self.subTest(exit_code=exit_code):
                result = processes.run([sys.executable, '-c', script, str(exit_code)], timeout=3)
                pid = int(result.stdout.strip())
                def alive():
                    try:
                        return Path('/proc', str(pid), 'stat').read_text().rsplit(')', 1)[1].split()[0] != 'Z'
                    except (FileNotFoundError, ProcessLookupError):
                        return False
                try:
                    until = time.monotonic() + .5
                    while alive() and time.monotonic() < until:
                        time.sleep(.01)
                    self.assertEqual(result.returncode, exit_code)
                    self.assertFalse(alive(), 'Leader exited but an owned descendant is still alive')
                    self.assertFalse(processes._active)
                finally:
                    if alive():
                        os.kill(pid, signal.SIGKILL)


if __name__ == '__main__':
    unittest.main()
