# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
import os,shlex,shutil,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from core import engine as motor
from core import render_parallel as P
from core import processes as surecler
import subprocess
class PathTest(unittest.TestCase):
 def test_child_raypath_is_completed_without_changing_caller(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);selected=root/'bin';lib=root/'lib';selected.mkdir();lib.mkdir()
   with patch.object(motor,'RADIANCE_BIN',str(selected)),patch.object(motor,'RAYPATH','configured'):
    for raypath in (None,'',str(lib),'custom'+os.pathsep+'.'+os.pathsep+'custom'):
     with self.subTest(raypath=raypath),patch.dict(os.environ,{'PATH':str(selected)}):
      if raypath is None:os.environ.pop('RAYPATH',None)
      else:os.environ['RAYPATH']=raypath
      before=os.environ.copy()
      env=motor.radiance_ortami()
      self.assertEqual(os.environ,before)
      paths=env['RAYPATH'].split(os.pathsep)
      self.assertEqual(paths[0],'.');self.assertEqual(paths.count('.'),1)
      self.assertIn(str(lib),paths)
      if raypath is None:self.assertIn('configured',paths)
      if raypath and 'custom' in raypath:self.assertEqual(paths.count('custom'),1)
      motor.ortam_hazirla()
      self.assertEqual(os.environ.get('RAYPATH'),raypath)
    supplied={'PATH':str(selected),'RAYPATH':'custom','TMPDIR':'scratch'}
    env=motor.radiance_ortami(supplied)
    self.assertEqual(supplied['RAYPATH'],'custom');self.assertEqual(env['TMPDIR'],'scratch')
 def test_selected_bin_is_first_even_when_already_in_path(self):
  with tempfile.TemporaryDirectory() as d:
   selected=Path(d)/'selected';other=Path(d)/'other';selected.mkdir();other.mkdir()
   with patch.object(motor,'RADIANCE_BIN',str(selected)),patch.dict(os.environ,{'PATH':str(other)+os.pathsep+str(selected)}):
    motor.ortam_hazirla();paths=os.environ['PATH'].split(os.pathsep)
    self.assertEqual(paths[0],str(selected));self.assertEqual(paths.count(str(selected)),1)
 def test_rpiece_symlink_uses_sibling_rpict_and_rejects_mixed_suite(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);real=root/'real';wrong=root/'wrong';real.mkdir();wrong.mkdir()
   for folder,name in [(real,'rpiece'),(real,'rpict'),(real,'vwrays'),(wrong,'rpict')]:
    f=folder/name;f.write_text('#!/bin/sh\nexit 0\n');f.chmod(0o755)
   link=root/'rpiece';link.symlink_to(real/'rpiece');octree=root/'scene.oct';octree.write_bytes(b'abc')
   paths={'rpiece':str(link),'vwrays':str(real/'vwrays'),'rpict':str(wrong/'rpict')}
   with patch.object(P,'_cpu_sayisi',return_value=8),patch.object(P,'_bos_bellek',return_value=4<<30),patch.object(P.shutil,'which',side_effect=lambda name,path=None:paths.get(name)),patch.object(surecler,'calistir',return_value=subprocess.CompletedProcess([],0,'-x 512 -y 512\n','')):
    plan=P.planla(512,512,'taslak',octree,env={})
    self.assertEqual(plan['rpiece'],str(real/'rpiece'));self.assertEqual(plan['radiance_bin'],str(real))
    (real/'rpict').unlink()
    self.assertEqual(P.planla(512,512,'taslak',octree,env={})['motor'],'rpict')
KOTU='a $(touch isaret) `touch isaret2` "q'
RADIANCE=all(shutil.which(x) for x in ('cat','xform','getbbox','pcomb','pextrem','falsecolor','ra_ppm'))
class ShellTirnakTest(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.taban=Path(self.tmp.name);self.kok=self.taban/KOTU
  for d in ('model','isik','_cache'):(self.kok/d).mkdir(parents=True)
  cwd=os.getcwd();os.chdir(self.taban);self.addCleanup(os.chdir,cwd);self.addCleanup(self.tmp.cleanup)
 def kacti(self):return any(self.taban.rglob('isaret*'))
 def test_proje_disindaki_geometri_okunmaz(self):
  (self.taban/'disari.rad').write_text('void plastic m\n0\n0\n5 0 0 0 0 0\n')
  with patch.object(motor,'sh') as sh:
   bb=motor.armatur_bbox(str(self.kok),{'geometri':[{'dosya':'../disari.rad'},{'dosya':str(self.taban/'disari.rad')}]})
  sh.assert_not_called();self.assertEqual(bb,motor.armatur_bbox(str(self.kok),{}))
 def test_gecersiz_falsecolor_birimi_falsecolordan_once_reddedilir(self):
  with patch.object(motor,'sh',return_value=(0,'','')) as sh,self.assertRaises(ValueError):
   motor.falsecolor_png(str(self.kok/'x.hdr'),str(self.kok/'x.png'),'Lux;touch isaret')
  self.assertEqual(len(sh.call_args_list),1);self.assertTrue(sh.call_args[0][0].startswith('pextrem '))
 def test_armatur_urun_yolu_rad_icinde_shell_tirnaklidir(self):
  (self.kok/'isik'/(KOTU+'.rad')).write_text('void light lamba\n0\n0\n3 1 1 1\nlamba sphere s\n0\n0\n4 0 0 0 .1\n')
  yol=motor.armatur_uret(str(self.kok),{'armaturler':[{'ad':'l','urun_rad':'isik/'+KOTU+'.rad'}]})
  satir=[x for x in Path(yol).read_text().splitlines() if x.startswith('!')][0]
  self.assertEqual(shlex.split(satir[1:])[-1],os.path.join('..','isik',KOTU+'.rad'))
  if shutil.which('xform'):
   r=subprocess.run(['xform','_cache/'+Path(yol).name],cwd=self.kok,capture_output=True,text=True,env=motor.radiance_ortami())
   self.assertEqual(r.returncode,0,r.stderr);self.assertIn('lamba',r.stdout)
  self.assertFalse(self.kacti())
 @unittest.skipUnless(RADIANCE,'Yerel Radiance gerekli')
 def test_zararli_pathler_radiancea_aynen_ulasir(self):
  (self.kok/'model'/(KOTU+'.rad')).write_text('void plastic m\n0\n0\n5 0 0 0 0 0\nm polygon p\n0\n0\n9 0 0 0 2 0 0 2 3 1\n')
  bb=motor.armatur_bbox(str(self.kok),{'geometri':[{'dosya':'model/'+KOTU+'.rad'}]})
  self.assertEqual(bb,[0,2,0,3,0,1])
  hdr=self.kok/(KOTU+'.hdr')
  with open(hdr,'wb') as f:subprocess.run(['pcomb','-x','8','-y','8','-e','ro=1;go=1;bo=1'],stdout=f,check=True,env=motor.radiance_ortami())
  self.assertTrue(motor.pextrem(str(hdr)))
  cikti=self.kok/(KOTU+'.png');motor.falsecolor_png(str(hdr),str(cikti))
  self.assertTrue(cikti.is_file());self.assertFalse(self.kacti())
 @unittest.skipUnless(RADIANCE,'Yerel Radiance gerekli')
 def test_bosluklu_tmpdir_ile_falsecolor_calisir(self):
  # falsecolor File::Temp klasörünü kendi shell komutlarına tırnaksız yapıştırır.
  bosluklu=self.kok/'gecici klasor';bosluklu.mkdir()
  hdr=self.kok/'duz.hdr'
  with open(hdr,'wb') as f:subprocess.run(['pcomb','-x','8','-y','8','-e','ro=1;go=1;bo=1'],stdout=f,check=True,env=motor.radiance_ortami())
  with patch.dict(os.environ,{'TMPDIR':str(bosluklu)}):
   cikti=self.kok/'duz.png';motor.falsecolor_png(str(hdr),str(cikti))
  self.assertTrue(cikti.is_file());self.assertFalse(self.kacti())
if __name__=='__main__':unittest.main()
