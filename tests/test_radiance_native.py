# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Regression test for light energy and scene independence using real Radiance."""
import copy
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib

ROOT=Path(__file__).resolve().parents[1]
from core import engine as engine
engine.prepare_environment()

# The scene build receives sky text from its caller. A dark dome suffices for nighttime.
SKY=('void glow nightglow\n0\n0\n4 0.008 0.01 0.02 0\n'
     'nightglow source skydome\n0\n0\n4 0 0 1 180\n')


@unittest.skipUnless(all(shutil.which(x) for x in ('oconv','rtrace','ies2rad','xform')),
                     'Local Radiance installation required')
class RadianceTest(unittest.TestCase):
    def setUp(self):
        (ROOT/'tests/.tmp').mkdir(exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(prefix='light test ',dir=ROOT/'tests/.tmp')
        self.pd=Path(self.tmp.name)
        for d in ('model', 'material', 'texture', 'light', 'sky', 'view', '_cache', 'render', 'output'):(self.pd/d).mkdir()
        (self.pd/'material/materials.rad').write_text('void plastic mat\n0\n0\n5 .5 .5 .5 0 0\n')
        (self.pd/'model/floor.rad').write_text('mat polygon floor\n0\n0\n12 -4 -4 0 4 -4 0 4 4 0 -4 4 0\n')
        (self.pd/'light/test.ies').write_text('IESNA:LM-63-2002\n[TEST] Numerical regression\nTILT=NONE\n1 1256.637061 1 3 1 1 2 0 0 0\n1 1 0\n0 90 180\n0\n100 100 100\n')
        self.p={'geometry':[{'file':'model/floor.rad','scale':1,'rx':0}],
                'luminaires':[{'name':'test','x':0,'y':0,'z':2,'direction':[0,0,-1],
                              'ies':'light/test.ies','dimmer':1}]}

    def tearDown(self):
        self.tmp.cleanup()

    def build(self,d):
        p=copy.deepcopy(self.p);p['luminaires'][0]['dimmer']=d
        return engine.compile_scene(str(self.pd),p,SKY,label='d'+str(d))

    def lux(self,octp):
        r=subprocess.run(['rtrace','-I+','-h','-ab','0','-ov',octp],
                         input='0 0 0 0 0 1\n',text=True,capture_output=True,
                         cwd=self.pd,timeout=30,check=True,env=engine.radiance_environment())
        self.assertNotIn('cannot find',r.stderr)
        rgb=list(map(float,r.stdout.split()))
        return 179*sum(a*b for a,b in zip(rgb,(.265,.670,.065)))

    def test_dimmer_inverse_square_and_path_with_spaces(self):
        for d in (0,.5,1,2):
            with self.subTest(dimmer=d):
                self.assertAlmostEqual(self.lux(self.build(d)),25*d,delta=.1)

    def test_compiled_scene_does_not_receive_later_light(self):
        before=self.build(.5)
        self.build(2)
        self.assertAlmostEqual(self.lux(before),12.5,delta=.1)

    def test_single_luminaire_toggle_preserves_photometry_and_record(self):
        before=copy.deepcopy(self.p['luminaires'][0])
        self.p['luminaires'][0]['enabled']=False
        self.assertAlmostEqual(self.lux(self.build(1)),0,delta=.01)
        self.p['luminaires'][0]['enabled']=True
        self.assertAlmostEqual(self.lux(self.build(1)),25,delta=.1)
        suffix=copy.deepcopy(self.p['luminaires'][0]);suffix.pop('enabled')
        self.assertEqual(suffix,before)

    def test_obj_group_without_mtl_builds(self):
        (self.pd/'model/grouped.obj').write_text('g 1043_0\nv -4 -4 0\nv 4 -4 0\nv 0 4 0\nf 1 2 3\n')
        self.p['geometry']=[{'file':'model/grouped.obj'}]
        self.assertAlmostEqual(self.lux(self.build(1)),25,delta=.1)

    def test_black_and_sparse_hdr_to_png(self):
        for bright in (0,1):
            hdr=self.pd/'synthetic.hdr'
            hdr.write_bytes(b'#?RADIANCE\nFORMAT=32-bit_rle_rgbe\n\n-Y 16 +X 16\n'+
                            bytes([128,128,128,129])*bright+bytes((256-bright)*4))
            for auto in (False,True):
                with self.subTest(bright=bright,automatic=auto):
                    png=self.pd/'synthetic.png'
                    engine.hdr_to_png(str(hdr),str(png),automatic=auto)
                    data=png.read_bytes()
                    self.assertEqual(data[:8],b'\x89PNG\r\n\x1a\n')
                    self.assertEqual(struct.unpack('>II',data[16:24]),(16,16))
                    offset=8;compressed=b''
                    while offset<len(data):
                        length=struct.unpack('>I',data[offset:offset+4])[0]
                        if data[offset+4:offset+8]==b'IDAT':compressed+=data[offset+8:offset+8+length]
                        offset+=length+12
                    rows=zlib.decompress(compressed)
                    self.assertEqual(len(rows),16*(1+16*3))
                    rgb=b''.join(rows[y*49+1:(y+1)*49] for y in range(16))
                    if bright==0:self.assertEqual(rgb,bytes(16*16*3))
                    elif not auto:self.assertGreater(max(rgb),0)
                    self.assertFalse(png.with_suffix('.ppm').exists())

    def test_invalid_hdr_is_still_rejected(self):
        hdr=self.pd/'invalid.hdr';hdr.write_bytes(b'not an HDR image')
        for auto in (False,True):
            with self.subTest(automatic=auto),self.assertRaises(RuntimeError):
                engine.hdr_to_png(str(hdr),str(self.pd/'invalid.png'),automatic=auto)
        self.assertFalse((self.pd/'invalid.png').exists())

    @unittest.skipUnless(all(shutil.which(x) for x in ('rpict','pvalue')),
                         'Local rpict/pvalue installation required')
    def test_native_small_hdr_has_expected_dimensions_and_nonzero_pixels(self):
        scene=self.build(1)
        output=engine.render(scene,
            '-vtv -vp 0 0 3 -vd 0 0 -1 -vu 0 1 0 -vh 45 -vv 45',
            quality='preview',W=16,H=16,out_hdr=str(self.pd/'render/native.hdr'))
        resolution=Path(output).read_bytes().split(b'\n\n',1)[1].splitlines()[0].split()
        self.assertEqual(resolution,[b'-Y',b'16',b'+X',b'16'])
        pixels=subprocess.run(['pvalue','-h','-H','-d',output],check=True,
            capture_output=True,text=True,timeout=30,env=engine.radiance_environment())
        values=[float(value) for value in pixels.stdout.split()]
        self.assertEqual(len(values),16*16*3)
        self.assertGreater(max(values),0)


if __name__=='__main__':unittest.main()
