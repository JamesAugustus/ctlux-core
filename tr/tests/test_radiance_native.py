# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Gerçek Radiance ile light enerjisi ve sahne bağımsızlığı regression testi."""
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
from core import engine as motor
motor.ortam_hazirla()

# sahne build'i gökyüzü metnini çağırandan alıyor, gece için koyu bir kubbe yeter.
GOK=('void glow nightglow\n0\n0\n4 0.008 0.01 0.02 0\n'
     'nightglow source skydome\n0\n0\n4 0 0 1 180\n')


@unittest.skipUnless(all(shutil.which(x) for x in ('oconv','rtrace','ies2rad','xform')),
                     'Yerel Radiance kurulumu gerekli')
class RadianceTest(unittest.TestCase):
    def setUp(self):
        (ROOT/'tests/.tmp').mkdir(exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(prefix='isik deneyi ',dir=ROOT/'tests/.tmp')
        self.pd=Path(self.tmp.name)
        for d in ('model', 'malzeme', 'doku', 'isik', 'gok', 'gorunum', '_cache', 'render', 'cikti'):(self.pd/d).mkdir()
        (self.pd/'malzeme/materials.rad').write_text('void plastic mat\n0\n0\n5 .5 .5 .5 0 0\n')
        (self.pd/'model/zemin.rad').write_text('mat polygon zemin\n0\n0\n12 -4 -4 0 4 -4 0 4 4 0 -4 4 0\n')
        (self.pd/'isik/test.ies').write_text('IESNA:LM-63-2002\n[TEST] Numerical regression\nTILT=NONE\n1 1256.637061 1 3 1 1 2 0 0 0\n1 1 0\n0 90 180\n0\n100 100 100\n')
        self.p={'geometri':[{'dosya':'model/zemin.rad','olcek':1,'rx':0}],
                'armaturler':[{'ad':'test','x':0,'y':0,'z':2,'yon':[0,0,-1],
                              'ies':'isik/test.ies','dimmer':1}]}

    def tearDown(self):
        self.tmp.cleanup()

    def derle(self,d):
        p=copy.deepcopy(self.p);p['armaturler'][0]['dimmer']=d
        return motor.sahne_derle(str(self.pd),p,GOK,etiket='d'+str(d))

    def lux(self,octp):
        r=subprocess.run(['rtrace','-I+','-h','-ab','0','-ov',octp],
                         input='0 0 0 0 0 1\n',text=True,capture_output=True,
                         cwd=self.pd,timeout=30,check=True,env=motor.radiance_ortami())
        self.assertNotIn('cannot find',r.stderr)
        rgb=list(map(float,r.stdout.split()))
        return 179*sum(a*b for a,b in zip(rgb,(.265,.670,.065)))

    def test_dimmer_ters_kare_ve_bosluklu_yol(self):
        for d in (0,.5,1,2):
            with self.subTest(dimmer=d):
                self.assertAlmostEqual(self.lux(self.derle(d)),25*d,delta=.1)

    def test_derlenmis_sahne_sonraki_isigi_almaz(self):
        once=self.derle(.5)
        self.derle(2)
        self.assertAlmostEqual(self.lux(once),12.5,delta=.1)

    def test_tek_armatur_ac_kapat_fotometriyi_ve_kaydi_korur(self):
        once=copy.deepcopy(self.p['armaturler'][0])
        self.p['armaturler'][0]['etkin']=False
        self.assertAlmostEqual(self.lux(self.derle(1)),0,delta=.01)
        self.p['armaturler'][0]['etkin']=True
        self.assertAlmostEqual(self.lux(self.derle(1)),25,delta=.1)
        son=copy.deepcopy(self.p['armaturler'][0]);son.pop('etkin')
        self.assertEqual(son,once)

    def test_mtl_olmayan_obj_grubu_derlenir(self):
        (self.pd/'model/gruplu.obj').write_text('g 1043_0\nv -4 -4 0\nv 4 -4 0\nv 0 4 0\nf 1 2 3\n')
        self.p['geometri']=[{'dosya':'model/gruplu.obj'}]
        self.assertAlmostEqual(self.lux(self.derle(1)),25,delta=.1)

    def test_black_and_sparse_hdr_to_png(self):
        for bright in (0,1):
            hdr=self.pd/'synthetic.hdr'
            hdr.write_bytes(b'#?RADIANCE\nFORMAT=32-bit_rle_rgbe\n\n-Y 16 +X 16\n'+
                            bytes([128,128,128,129])*bright+bytes((256-bright)*4))
            for auto in (False,True):
                with self.subTest(bright=bright,automatic=auto):
                    png=self.pd/'synthetic.png'
                    motor.hdr_to_png(str(hdr),str(png),otomatik=auto)
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
                motor.hdr_to_png(str(hdr),str(self.pd/'invalid.png'),otomatik=auto)
        self.assertFalse((self.pd/'invalid.png').exists())

    @unittest.skipUnless(all(shutil.which(x) for x in ('rpict','pvalue')),
                         'Yerel rpict/pvalue kurulumu gerekli')
    def test_native_small_hdr_has_expected_dimensions_and_nonzero_pixels(self):
        scene=self.derle(1)
        output=motor.render(scene,
            '-vtv -vp 0 0 3 -vd 0 0 -1 -vu 0 1 0 -vh 45 -vv 45',
            kalite='onizleme',W=16,H=16,out_hdr=str(self.pd/'render/native.hdr'))
        resolution=Path(output).read_bytes().split(b'\n\n',1)[1].splitlines()[0].split()
        self.assertEqual(resolution,[b'-Y',b'16',b'+X',b'16'])
        pixels=subprocess.run(['pvalue','-h','-H','-d',output],check=True,
            capture_output=True,text=True,timeout=30,env=motor.radiance_ortami())
        values=[float(value) for value in pixels.stdout.split()]
        self.assertEqual(len(values),16*16*3)
        self.assertGreater(max(values),0)


if __name__=='__main__':unittest.main()
