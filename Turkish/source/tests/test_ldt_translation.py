# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
import sys, tempfile, unittest
from pathlib import Path
from core.engine import ldt_ies

class LDTTest(unittest.TestCase):
 def fixture(self):
  lines=['0']*42
  for i,v in {2:1,3:4,5:3,8:'fixture',23:2,25:1,26:1,28:2000,31:10}.items():lines[i]=str(v)
  return lines+['0','90','180','270','0','90','180','10','20','30']
 def test_absolute_candela_and_factor(self):
  with tempfile.TemporaryDirectory() as d:
   source=Path(d)/'a.ldt';target=Path(d)/'a.ies';source.write_text('\n'.join(self.fixture()))
   ldt_ies(source,target)
   self.assertEqual(target.read_text().splitlines()[-1],'40 80 120')
 def test_bad_data_keeps_existing_target(self):
  cases=[]
  cases.append(self.fixture()[:-1])
  for index,value in [(2,'3'),(3,'0'),(5,'3.5'),(23,'0'),(24,'15'),(25,'2'),(28,'0'),(-1,'NaN'),(-1,'-1'),(-1,'bad')]:
   lines=self.fixture();lines[index]=value;cases.append(lines)
  for lines in cases:
   with self.subTest(lines=lines),tempfile.TemporaryDirectory() as d:
    source=Path(d)/'a.ldt';target=Path(d)/'a.ies';source.write_text('\n'.join(lines));target.write_text('original')
    with self.assertRaises(RuntimeError):ldt_ies(source,target)
    self.assertEqual(target.read_text(),'original')
if __name__=='__main__':unittest.main()
