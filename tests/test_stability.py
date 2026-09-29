# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Small synthetic regression cases based on real user behavior."""
import json
import math
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
from core import engine as engine
from core import importer as importer
from core.tools import ls10_reader as read_lumion
from core import processes as processes
from core.file_safety import atomic_json, internal_path
from core.step_reader import parse_fields, parse_records


class FileBackedTest(unittest.TestCase):
    def setUp(self):
        (ROOT / "tests/.tmp").mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / "tests/.tmp")
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()


class ReaderTest(FileBackedTest):
    def test_truncated_mesh_header(self):
        for b in (b"VPPI", b"VPPI\x01", b"VPPI"+struct.pack('<I',12)+b'\0'*12+b'PO32'):
            self.assertEqual(read_lumion.extract_mesh(b, self.root/'x.obj')['mesh'], 0)

    def test_valid_and_nan_mesh(self):
        for x, expected in ((0.0, 1), (float('nan'), 0)):
            v = struct.pack('<9f', x,0,0, 1,0,0, 0,1,0)
            b = b'VPPI'+struct.pack('<I',len(v))+v+b'PO32'+struct.pack('<I',12)+struct.pack('<3I',0,1,2)
            self.assertEqual(read_lumion.extract_mesh(b,self.root/'x.obj')['mesh'], expected)

    def test_step_quotes(self):
        self.assertEqual(parse_fields("42,'Lamp, (A) and ''B''',#7,(1,2,3)"),
                         ['42', "'Lamp, (A) and ''B'''", '#7', '(1,2,3)'])

    def test_step_semicolon_in_record(self):
        d=parse_records("#1=Thing('A);B',(#2,#3));\n#2=Other(7);")
        self.assertEqual(len(d),2)
        self.assertEqual(parse_fields(d[1][1])[0],"'A);B'")

    def test_step_truncated_field(self):
        with self.assertRaises(ValueError): parse_fields("1,'abc")

    def test_usd_disabled_light(self):
        a,_=importer.usd_luminaires([{'kind':'omni','intensity':0}])
        self.assertEqual(a[0]['power'],0)
        self.assertEqual(a[0]['photometry_status'],'approximate')


class FileTest(FileBackedTest):
    def test_atomic_json_failure_preserves_old_file(self):
        p=self.root/'p.json';atomic_json(p,{'x':1})
        with self.assertRaises(ValueError):atomic_json(p,{'x':float('nan')})
        self.assertEqual(json.loads(p.read_text()),{'x':1})

    def test_path_escape(self):
        for rel in ('../x','/x','a/../../x','C:\\x','..\\x'):
            with self.assertRaises(ValueError):internal_path(self.root,rel)

    def test_symlink_escape(self):
        d=self.root/'root';d.mkdir();(self.root/'else').mkdir()
        (d/'link').symlink_to(self.root/'else',target_is_directory=True)
        with self.assertRaises(ValueError):internal_path(d,'link/x')

    def test_textures_with_same_name(self):
        from core import format_detector as format_detector
        for n in ('a/tex.jpg','b/tex.jpg'):
            p=self.root/n;p.parent.mkdir();p.write_bytes(b'fixture')
        ds,_=format_detector.analyze(str(self.root))
        self.assertEqual({d['file'] for d in ds},{'a/tex.jpg','b/tex.jpg'})


class PhotometryTest(FileBackedTest):
    def setUp(self):
        super().setUp()
        for n in ('model', 'material', 'texture', 'light', 'sky', 'view', '_cache', 'render', 'output'):(self.root/n).mkdir()
        self.ies=self.root/'light/lamp.ies';self.ies.write_text('test photometry')

    def converter(self,cmd,**kw):
        out=cmd[cmd.index('-o')+1]
        Path(kw['cwd'],out+'.rad').write_text('void light l\n0\n0\n3 1 1 1\n')
        Path(kw['cwd'],out+'.dat').write_text('fixture')
        return subprocess.CompletedProcess(cmd,0,'','')

    def test_dimmer_and_source_changes(self):
        with patch.object(processes,'run',side_effect=self.converter) as run:
            a=engine._ies_product_rad(str(self.root),'light/lamp.ies',.5)
            b=engine._ies_product_rad(str(self.root),'light/lamp.ies',1)
            self.assertNotEqual(a,b)
            self.assertEqual(run.call_args_list[0].args[0][2],'0.5')
            self.ies.write_text('changed photometry')
            c=engine._ies_product_rad(str(self.root),'light/lamp.ies',1)
            self.assertNotEqual(b,c)

    def test_disabled_photometry_produces_no_emission(self):
        a={'name':'l','ies':'light/lamp.ies','dimmer':0}
        with patch.object(processes,'run') as run:
            p=engine.generate_luminaire(str(self.root),{'luminaires':[a]})
            self.assertNotIn('!xform',Path(p).read_text());run.assert_not_called()

    def test_missing_ies_does_not_silently_use_generic_light(self):
        with self.assertRaises(ValueError):
            engine.generate_luminaire(str(self.root),{'luminaires':[{'name':'l','ies':'missing.ies'}]})

    def test_evo_photometry_converter_error_is_not_hidden(self):
        ents={1:('LuminaireElement','17'),2:('LuminairePrototype','#3'),
              3:('PrototypeGeometricRepresentation','#4'),
              4:('LuminairePrototypeRepresentationData',''),
              5:('LightDistributionConnection','#4,#6'),6:('LightDistribution','#7'),
              7:('LightDistributionData','(),(0),(0,90,180),(100,100,100)'),
              8:('LampTypeChannel','#4,$,$,1256'),9:('RelDefinesByPrototype','#1,#2')}
        with patch.object(importer,'_evo_step_ents',return_value=ents),patch.object(engine,'sh',return_value=(127,'','ies2rad is unavailable')):
            with self.assertRaisesRegex(RuntimeError,'EVO photometry'):
                engine.evo_ies('test.evo',str(self.root))

    def test_cache_calculation_settings(self):
        p=self.root/'scene.oct';p.write_bytes(b'same scene')
        self.assertNotEqual(engine._ambient_file(str(p),'-ab 2'),engine._ambient_file(str(p),'-ab 6'))

    def test_texture_paths_are_preserved(self):
        p=self.root/'model/a.mtl';p.write_text('newmtl A\nmap_Kd a/tex.jpg\nnewmtl B\nmap_Kd b/tex.jpg\n')
        d=engine._read_mtl([str(p)])
        self.assertEqual(d['A']['map'],'a/tex.jpg');self.assertEqual(d['B']['map'],'b/tex.jpg')

    def test_nested_mtl_relative_texture(self):
        for house in ('A','B'):
            mtl=self.root/'model'/house/'room.mtl';mtl.parent.mkdir()
            mtl.write_text('newmtl test\nmap_Kd textures/a.png\n')
            tex=self.root/'texture'/house/'textures/a.png';tex.parent.mkdir(parents=True)
            tex.write_bytes(house.encode())
            d=engine._read_mtl([str(mtl)])['test']
            found=engine._find_texture(str(self.root),d['map'],d['_mtl_path'])
            self.assertEqual(Path(found),tex)
        self.assertIsNone(engine._find_texture(str(self.root),'../../outside'))


class ProcessTest(unittest.TestCase):
    def test_timeout(self):
        with self.assertRaises(RuntimeError):
            processes.run([sys.executable,'-c','import time;time.sleep(10)'],timeout=.1)
        self.assertFalse(processes._active)


if __name__=='__main__':unittest.main()
