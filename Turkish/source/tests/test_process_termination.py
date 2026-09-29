# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Senkron komut bitince pipe'larını kapatmış alt process de sonlanmalı."""
import os
from pathlib import Path
import signal
import sys
import time
import unittest

from core import processes as surecler


@unittest.skipUnless(sys.platform.startswith('linux'), 'Linux süreç durumu kanıtı')
class SurecSonlanmaTest(unittest.TestCase):
    def test_lider_bitisinde_sessiz_torun_kalmaz(self):
        script = """import os, subprocess, sys
p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print(p.pid, flush=True)
sys.exit(int(sys.argv[1]))
"""
        for exit_code in (0, 3):
            with self.subTest(exit_code=exit_code):
                result = surecler.calistir([sys.executable, '-c', script, str(exit_code)], timeout=3)
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
                    self.assertFalse(alive(), 'Lider bitti ama sahipli torun yaşıyor')
                    self.assertFalse(surecler._aktif)
                finally:
                    if alive():
                        os.kill(pid, signal.SIGKILL)


if __name__ == '__main__':
    unittest.main()
