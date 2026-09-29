# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
import os,shlex,shutil,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from core import engine as engine
from core import render_parallel as P
from core import processes as processes
import subprocess
class PathTest(unittest.TestCase):
 def test_child_raypath_is_completed_without_changing_caller(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);selected=root/'bin';lib=root/'lib';selected.mkdir();lib.mkdir()
   with patch.object(engine,'RADIANCE_BIN',str(selected)),patch.object(engine,'RAYPATH','configured'):
    for raypath in (None,'',str(lib),'custom'+os.pathsep+'.'+os.pathsep+'custom'):
     with self.subTest(raypath=raypath),patch.dict(os.environ,{'PATH':str(selected)}):
      if raypath is None:os.environ.pop('RAYPATH',None)
      else:os.environ['RAYPATH']=raypath
      before=os.environ.copy()
      env=engine.radiance_environment()
      self.assertEqual(os.environ,before)
      paths=env['RAYPATH'].split(os.pathsep)
      self.assertEqual(paths[0],'.');self.assertEqual(paths.count('.'),1)
      self.assertIn(str(lib),paths)
      if raypath is None:self.assertIn('configured',paths)
      if raypath and 'custom' in raypath:self.assertEqual(paths.count('custom'),1)
      engine.prepare_environment()
      self.assertEqual(os.environ.get('RAYPATH'),raypath)
    supplied={'PATH':str(selected),'RAYPATH':'custom','TMPDIR':'scratch'}
    env=engine.radiance_environment(supplied)
    self.assertEqual(supplied['RAYPATH'],'custom');self.assertEqual(env['TMPDIR'],'scratch')
 def test_selected_bin_is_first_even_when_already_in_path(self):
  with tempfile.TemporaryDirectory() as d:
   selected=Path(d)/'selected';other=Path(d)/'other';selected.mkdir();other.mkdir()
   with patch.object(engine,'RADIANCE_BIN',str(selected)),patch.dict(os.environ,{'PATH':str(other)+os.pathsep+str(selected)}):
    engine.prepare_environment();paths=os.environ['PATH'].split(os.pathsep)
    self.assertEqual(paths[0],str(selected));self.assertEqual(paths.count(str(selected)),1)
 def test_rpiece_symlink_uses_sibling_rpict_and_rejects_mixed_suite(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);real=root/'real';wrong=root/'wrong';real.mkdir();wrong.mkdir()
   for folder,name in [(real,'rpiece'),(real,'rpict'),(real,'vwrays'),(wrong,'rpict')]:
    f=folder/name;f.write_text('#!/bin/sh\nexit 0\n');f.chmod(0o755)
   link=root/'rpiece';link.symlink_to(real/'rpiece');octree=root/'scene.oct';octree.write_bytes(b'abc')
   paths={'rpiece':str(link),'vwrays':str(real/'vwrays'),'rpict':str(wrong/'rpict')}
   with patch.object(P,'_cpu_count',return_value=8),patch.object(P,'_available_memory',return_value=4<<30),patch.object(P.shutil,'which',side_effect=lambda name,path=None:paths.get(name)),patch.object(processes,'run',return_value=subprocess.CompletedProcess([],0,'-x 512 -y 512\n','')):
    plan=P.plan_render(512,512,'draft',octree,env={})
    self.assertEqual(plan['rpiece'],str(real/'rpiece'));self.assertEqual(plan['radiance_bin'],str(real))
    (real/'rpict').unlink()
    self.assertEqual(P.plan_render(512,512,'draft',octree,env={})['engine'],'rpict')
EVIL='a $(touch marker) `touch marker2` "q'
RADIANCE=all(shutil.which(x) for x in ('cat','xform','getbbox','pcomb','pextrem','falsecolor','ra_ppm'))
class ShellQuoteTest(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name);self.root=self.base/EVIL
  for d in ('model','light','_cache'):(self.root/d).mkdir(parents=True)
  cwd=os.getcwd();os.chdir(self.base);self.addCleanup(os.chdir,cwd);self.addCleanup(self.tmp.cleanup)
 def escaped(self):return any(self.base.rglob('marker*'))
 def test_geometry_outside_project_is_not_read(self):
  (self.base/'outside.rad').write_text('void plastic m\n0\n0\n5 0 0 0 0 0\n')
  with patch.object(engine,'sh') as sh:
   bb=engine.luminaire_bbox(str(self.root),{'geometry':[{'file':'../outside.rad'},{'file':str(self.base/'outside.rad')}]})
  sh.assert_not_called();self.assertEqual(bb,engine.luminaire_bbox(str(self.root),{}))
 def test_invalid_falsecolor_unit_is_rejected_before_falsecolor(self):
  with patch.object(engine,'sh',return_value=(0,'','')) as sh,self.assertRaises(ValueError):
   engine.falsecolor_png(str(self.root/'x.hdr'),str(self.root/'x.png'),'Lux;touch marker')
  self.assertEqual(len(sh.call_args_list),1);self.assertTrue(sh.call_args[0][0].startswith('pextrem '))
 def test_luminaire_product_path_is_shell_quoted_in_rad(self):
  (self.root/'light'/(EVIL+'.rad')).write_text('void light lamp\n0\n0\n3 1 1 1\nlamp sphere s\n0\n0\n4 0 0 0 .1\n')
  path=engine.generate_luminaire(str(self.root),{'luminaires':[{'name':'l','product_rad':'light/'+EVIL+'.rad'}]})
  line=[x for x in Path(path).read_text().splitlines() if x.startswith('!')][0]
  self.assertEqual(shlex.split(line[1:])[-1],os.path.join('..','light',EVIL+'.rad'))
  if shutil.which('xform'):
   r=subprocess.run(['xform','_cache/_luminaires.rad'],cwd=self.root,capture_output=True,text=True,env=engine.radiance_environment())
   self.assertEqual(r.returncode,0,r.stderr);self.assertIn('lamp',r.stdout)
  self.assertFalse(self.escaped())
 @unittest.skipUnless(RADIANCE,'Local Radiance required')
 def test_hostile_paths_reach_radiance_literally(self):
  (self.root/'model'/(EVIL+'.rad')).write_text('void plastic m\n0\n0\n5 0 0 0 0 0\nm polygon p\n0\n0\n9 0 0 0 2 0 0 2 3 1\n')
  bb=engine.luminaire_bbox(str(self.root),{'geometry':[{'file':'model/'+EVIL+'.rad'}]})
  self.assertEqual(bb,[0,2,0,3,0,1])
  hdr=self.root/(EVIL+'.hdr')
  with open(hdr,'wb') as f:subprocess.run(['pcomb','-x','8','-y','8','-e','ro=1;go=1;bo=1'],stdout=f,check=True,env=engine.radiance_environment())
  self.assertTrue(engine.pextrem(str(hdr)))
  out=self.root/(EVIL+'.png');engine.falsecolor_png(str(hdr),str(out))
  self.assertTrue(out.is_file());self.assertFalse(self.escaped())
 @unittest.skipUnless(RADIANCE,'Local Radiance required')
 def test_falsecolor_works_with_spaced_tmpdir(self):
  # falsecolor pastes its File::Temp directory into its own shell commands unquoted.
  spaced=self.root/'temp dir';spaced.mkdir()
  hdr=self.root/'plain.hdr'
  with open(hdr,'wb') as f:subprocess.run(['pcomb','-x','8','-y','8','-e','ro=1;go=1;bo=1'],stdout=f,check=True,env=engine.radiance_environment())
  with patch.dict(os.environ,{'TMPDIR':str(spaced)}):
   out=self.root/'plain.png';engine.falsecolor_png(str(hdr),str(out))
  self.assertTrue(out.is_file());self.assertFalse(self.escaped())
if __name__=='__main__':unittest.main()
