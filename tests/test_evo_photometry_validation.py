# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Generated STEP data only; native ies2rad is mocked in this unit test."""
import tempfile, unittest, zipfile
from pathlib import Path
from unittest.mock import patch
from test_evo_identity import STEP, importer

class PhotometryValidationTest(unittest.TestCase):
    def test_invalid_product_is_not_sent_to_native_tool(self):
        bad = [STEP.replace('#134=LampTypeChannel(#102,0,1,1256);',''),
               STEP.replace('0,1,1256','0,1,1e999'), STEP.replace('0,1,1256','0,1,0'),
               STEP.replace('(100,100,100)','(-100,100,100)'),
               STEP.replace('(100,100,100)','(100,100,100,999)'),
               STEP.replace('(100,100,100)','(100,NaN,100)'),
               STEP.replace('(100,100,100)','(100,1e999,100)'),
               STEP.replace('(0,90,180)','(0,180,90)'),
               STEP.replace('(0,90,180)','(0,90,181)')]
        for text in bad:
            with self.subTest(text=text[-75:]),tempfile.TemporaryDirectory() as d:
                src=Path(d,'bad.evo')
                with zipfile.ZipFile(src,'w') as z:z.writestr('Project/ProjectData/ProjectData.dat',text)
                with patch.object(importer.engine,'sh') as run:
                    with self.assertRaises(ValueError):importer.evo_ies(str(src),d)
                    run.assert_not_called()
                self.assertFalse(list(Path(d).glob('_cache/ies/*.ies')))

if __name__=='__main__':unittest.main()
