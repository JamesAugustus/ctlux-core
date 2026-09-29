# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Command contracts, silent limits and real conversions of synthetic formats."""
import ast
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import warnings
import zipfile
from unittest.mock import patch

from core import engine as engine, importer as importer, step_reader as step_reader, scene_model as scene_model, scene_writers as scene_writers, assimp_bridge as assimp_bridge
from core.tools import ies_analysis as ies_analysis, ls10_reader as read_lumion
from core.__main__ import convert, products, PHOTOMETRY_NOTICE
from core.photometry import FILE_NOTICE, PROJECT_NOTICE
import synthetic as synthetic

ROOT = Path(__file__).resolve().parents[1]
IES2RAD = shutil.which('ies2rad', path=(engine.find_radiance() or '')+os.pathsep+os.environ.get('PATH',''))


def ies2rad_light(data):
    """Pass IES bytes through ies2rad. Return its distribution table and .rad lines without comments."""
    with tempfile.TemporaryDirectory() as work:
        (Path(work)/'x.ies').write_bytes(data)
        r = subprocess.run([IES2RAD,'-o','x','x.ies'],cwd=work,capture_output=True,text=True,
                           env=engine.radiance_environment())
        if r.returncode:
            raise AssertionError(r.stderr)
        rad = [x for x in (Path(work)/'x.rad').read_text().splitlines() if not x.startswith('#')]
        return (Path(work)/'x.dat').read_bytes(), rad


def ies_header(data):
    lines = data.decode('ascii').splitlines()
    return lines[:next(i for i, x in enumerate(lines) if x.startswith('TILT='))+1]


class LimitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_report_warning_categories_are_case_insensitive(self):
        from core.__main__ import _classify_notice
        partial, skipped, limits = [], [], []
        messages = ['Skipped: synthetic light', 'LIMIT exceeded', 'Assumed unit']
        for message in [*messages, messages[0]]:
            _classify_notice(message, partial, skipped, limits)
        self.assertEqual(skipped, [messages[0]])
        self.assertEqual(limits, [messages[1]])
        self.assertEqual(partial, [messages[2]])

    def test_evo_limit_is_reported(self):
        path = synthetic.evo(self.root/'a.evo', count=3)
        with warnings.catch_warnings(record=True) as records:
            warnings.simplefilter('always')
            luminaires = importer.evo_luminaires(path, max_luminaires=1)
        self.assertEqual(len(luminaires), 1)
        self.assertTrue(any('read=3, limit=1, skipped=2' in str(w.message) for w in records))
        with self.assertRaises(ValueError):
            importer.evo_luminaires(path, max_luminaires=-1)
        with warnings.catch_warnings(record=True):
            self.assertEqual(importer.evo_luminaires(path, max_luminaires=0), [])

    def test_evo_furniture_limit_is_reported(self):
        from test_evo_components import STEP
        path=self.root/'furniture.evo'
        text=STEP+"\n#80=FurnitureElement(80,'a',(),#2,$,$);\n#81=FurnitureElement(81,'b',(),#2,$,$);"
        with zipfile.ZipFile(path,'w') as z:
            z.writestr('Project/ProjectData/ProjectData.dat',text)
        with warnings.catch_warnings(record=True) as records:
            warnings.simplefilter('always')
            _,count=importer.evo_furniture(path,str(self.root/'furniture.rad'),max_furniture=1)
        self.assertEqual(count,1)
        self.assertTrue(any('read=2, limit=1, skipped=1' in str(w.message) for w in records))

    def test_step_cut_tail_is_reported_but_quoted_text_is_data(self):
        with warnings.catch_warnings(record=True) as records:
            warnings.simplefilter('always')
            got = step_reader.parse_records("#1=A('x; #9=foo'); #2=B('cut")
        self.assertEqual(list(got), [1])
        self.assertEqual(len(records), 1)
        self.assertIn('#2', str(records[0].message))

    def test_ies_incomplete_and_extra_candela_are_errors(self):
        path = self.root/'a.ies'
        for text in (synthetic.IES.rsplit('100 100 100',1)[0]+'100\n', synthetic.IES+'100\n'):
            path.write_text(text)
            with self.assertRaisesRegex(ValueError, 'table size mismatch'):
                ies_analysis.read_ies(path)
        path.write_text(synthetic.IES)
        self.assertEqual(ies_analysis.read_ies(path)['candela'], [100]*3)

    def test_ies_bad_counts_and_tilt_rejected(self):
        path = self.root/'a.ies'
        for text in (synthetic.IES.replace('1 3 1 1 2','1 3.5 1 1 2'),
                     synthetic.IES.replace('TILT=NONE','TILT=external'),
                     'IESNA\nTILT=INCLUDE\n1\n'):
            path.write_text(text)
            with self.assertRaises(ValueError):
                ies_analysis.read_ies(path)

    def test_lumion_defaults_preserve_values_and_name_each_field(self):
        with warnings.catch_warnings(record=True) as records:
            warnings.simplefilter('always')
            lights = read_lumion.decode_lights(synthetic.lumion(True))
        light = lights[0]
        self.assertEqual((light['type'], light['cone'], light['w'], light['h']), (0,1,1,1))
        self.assertEqual(light['rgb'], (1,1,1))
        self.assertEqual(set(light['defaults']), {'rgb','type','cone','w','h'})
        for field in light['defaults']:
            self.assertTrue(any(light['source_id'] in str(w.message) and field+' default=' in str(w.message) for w in records))

    def test_lumion_missing_relative_field_and_incomplete_light_reported(self):
        from test_ls10_preservation import light, tlv
        data = light(1)
        start = data.index(b'IIM1')
        # Shift the matrix one record to the right, keeping 37 records in total: relative #37 is missing.
        shifted = data[:start]+tlv(b'IIVE',[1])+data[start:-12]
        with warnings.catch_warnings(record=True) as records:
            warnings.simplefilter('always')
            lights = read_lumion.decode_lights(shifted)
        self.assertEqual(lights[0]['type'],0)
        self.assertTrue(any('type default=0' in str(w.message) for w in records))
        with warnings.catch_warnings(record=True) as records:
            warnings.simplefilter('always')
            self.assertEqual(read_lumion.decode_lights(data[:-24]),[])
        self.assertTrue(any('skipped' in str(w.message) for w in records))

    def test_assimp_empty_uv_notation_and_bad_indices(self):
        path = self.root/'a.obj'
        path.write_bytes(synthetic.OBJ.replace(b'f 1 2 3', b'f  1/ 2/ 3/'))
        assimp_bridge._validate_obj(path)
        for face in (b'f 1// 2// 3//', b'f 0/ 2/ 3/', b'f 1/ 2/ 4/'):
            path.write_bytes(synthetic.OBJ.replace(b'f 1 2 3', face))
            with self.assertRaises(RuntimeError):
                assimp_bridge._validate_obj(path)

    def test_public_names_without_legacy_aliases(self):
        self.assertIs(engine.ldt_to_ies, importer.ldt_to_ies)
        self.assertTrue(callable(engine.auto_frame))
        for name in ('_ldt_to_ies', '_auto_frame', 'LOG', 'log', 'LIBRARY', 'INBOX',
                     'PROJECTS_DIR', 'create_project', 'package_project', 'open_project'):
            self.assertFalse(hasattr(engine, name), name)
        self.assertTrue(callable(importer.write_evo_photometry))
        with self.assertRaises(AttributeError):
            importer.harvest_evo_products

    def test_language_packages_use_one_source_root(self):
        roots = [ROOT, ROOT/'Turkish'/'source']
        code = """from pathlib import Path
import sys
import core
import core.engine
expected = Path(sys.argv[1]).resolve() / 'core'
assert Path(core.__file__).resolve().parent == expected
assert Path(core.engine.__file__).resolve().parent == expected
assert len(core.__path__) == 1
"""
        for selected in roots:
            other = next(path for path in roots if path != selected)
            env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1',
                   'PYTHONPATH': os.pathsep.join(map(str, (selected, other)))}
            result = subprocess.run([sys.executable, '-B', '-c', code, str(selected)],
                                    cwd=self.root, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_core_import_guard_all_subdirectories(self):
        blocked = ('studio','server','providers','project_vault','http.server','webbrowser')
        modules = []
        for path in (ROOT/'core').rglob('*.py'):
            modules.append('.'.join(path.relative_to(ROOT).with_suffix('').parts))
            for node in ast.walk(ast.parse(path.read_text())):
                names = [a.name for a in node.names] if isinstance(node,ast.Import) else [node.module or ''] if isinstance(node,ast.ImportFrom) else []
                for name in names:
                    stripped = name.removeprefix('core.')
                    self.assertFalse(any(stripped==b or stripped.startswith(b+'.') for b in blocked), (path,name))
        code = '''import sys, importlib, importlib.abc, json
class Guard(importlib.abc.MetaPathFinder):
 def find_spec(self, name, path=None, target=None):
  if any(name==b or name.startswith(b+'.') for b in json.loads(sys.argv[1])):
   raise AssertionError('Forbidden import: '+name)
sys.meta_path.insert(0, Guard())
for name in json.loads(sys.argv[2]): importlib.import_module(name)
'''
        result = subprocess.run([sys.executable,'-B','-c',code,json.dumps(blocked),json.dumps(modules)],cwd=ROOT,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)


class CommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.inputs=self.root/'input'
        synthetic.generate(self.inputs)
        self.env={**os.environ, 'PYTHONDONTWRITEBYTECODE':'1'}

    def run_cli(self,name):
        output=self.root/(name.replace('.','_')+'_out')
        result=subprocess.run([sys.executable,'-B','-m','core',str(self.inputs/name),str(output)],
                              cwd=ROOT,env=self.env,capture_output=True,text=True,timeout=60)
        self.assertEqual(result.returncode,0,result.stderr)
        scene=json.loads((output/'scene.ctlux.json').read_text())
        scene_model.validate_scene(scene)
        self.assertTrue((output/'scene.rad').is_file())
        self.assertTrue((output/'view.vf').is_file())
        self.assertFalse(list(output.glob('.conversion-*')))
        for section in ('READ','PARTIAL','SKIPPED','LIMITS'):
            self.assertIn(section,(output/'REPORT.txt').read_text())
        return output,scene

    def test_cli_obj(self):
        # Conversions that write no photometry file have no ownership notice.
        output,scene=self.run_cli('triangle.obj')
        self.assertEqual((output/'REPORT.txt').read_text().splitlines()[:2], ['CTLux', 'Status: partial'])
        self.assertEqual(len(scene['mesh']['f'])//3,1)

    def test_cli_ls10(self):
        _,scene=self.run_cli('scene.ls10')
        self.assertEqual(len(scene['lights']),2)

    def test_cli_evo(self):
        _,scene=self.run_cli('room.evo')
        self.assertEqual(len(scene['mesh']['f'])//3,12)
        self.assertEqual(len(scene['lights']),1)

    def test_cli_lumion_defaults_in_report(self):
        output,_=self.run_cli('default.ls10')
        report=(output/'REPORT.txt').read_text()
        for field in ('rgb','type','cone','w','h'):
            self.assertIn(field+' default=',report)

    def test_cli_evo_truncation_in_report(self):
        synthetic.evo(self.inputs/'cut.evo',truncated=True)
        output,_=self.run_cli('cut.evo')
        self.assertIn('STEP truncated/invalid record skipped: #999999',(output/'REPORT.txt').read_text())

    def test_cli_evo_default_4000_limit_in_report(self):
        synthetic.evo(self.inputs/'many.evo',count=4001)
        output,scene=self.run_cli('many.evo')
        self.assertEqual(len(scene['lights']),4000)
        self.assertIn('read=4001, limit=4000, skipped=1',(output/'REPORT.txt').read_text())

    @unittest.skipUnless(shutil.which('assimp'),'Assimp required')
    def test_evo_embedded_fbx_skips_reported_and_special_output_path(self):
        path=self.inputs/'fbx.evo'
        synthetic.evo(path)
        with zipfile.ZipFile(path,'a') as z:
            z.writestr('mesh/valid.fbx',synthetic.FBX_ASCII)
            z.writestr('mesh/broken.fbx',b'broken')
        output=self.root/'output $(touch UNWANTED)'
        convert(path,output)
        self.assertIn('EVO FBX part skipped: mesh/broken.fbx',(output/'REPORT.txt').read_text())
        self.assertFalse((ROOT/'UNWANTED').exists())
        self.assertFalse((self.root/'UNWANTED').exists())

    @unittest.skipUnless(shutil.which('assimp'),'Assimp required')
    def test_cli_assimp_formats(self):
        for name in ('ascii.stl','binary.stl','triangle.3ds','triangle.glb','triangle.gltf','triangle.dae','ascii.fbx','binary.fbx'):
            with self.subTest(name=name):
                _,scene=self.run_cli(name)
                self.assertEqual(len(scene['mesh']['f'])//3,1)

    @unittest.skipUnless(importlib.util.find_spec('pxr'),'usd-core required')
    def test_cli_usda(self):
        _,scene=self.run_cli('triangle.usda')
        self.assertEqual(len(scene['mesh']['f'])//3,1)

    @unittest.skipUnless(all(shutil.which(x,path=(engine.find_radiance() or '')+os.pathsep+os.environ.get('PATH',''))
                            for x in ('ies2rad','xform','oconv','rtrace')),'Radiance required')
    def test_cli_ies_ldt_use_real_photometry(self):
        for name in ('light.ies','light.ldt'):
            with self.subTest(name=name):
                output,scene=self.run_cli(name)
                self.assertEqual(scene['mesh']['f'],[])
                self.assertTrue((output/'light/l0.dat').is_file())
                self.assertTrue((output/scene['lights'][0]['source']['ies']).is_file())
                self.assertEqual((output/'REPORT.txt').read_text().splitlines()[:3], ['CTLux', *PHOTOMETRY_NOTICE])
                delivery = (output/'light/l0.ies').read_bytes()
                self.assertEqual(ies_header(delivery)[-len(FILE_NOTICE)-1:], [*FILE_NOTICE, 'TILT=NONE'])
                if name == 'light.ies' and IES2RAD:
                    self.assertIn('[TEST] Synthetic', ies_header(delivery))
                    self.assertEqual(ies2rad_light(delivery), ies2rad_light((self.inputs/name).read_bytes()))
                octree=subprocess.run(['oconv','scene.rad'],cwd=output,capture_output=True,env=engine.radiance_environment())
                self.assertEqual(octree.returncode,0,octree.stderr)
                (output/'scene.oct').write_bytes(octree.stdout)
                traced=subprocess.run(['rtrace','-h','-I+','-ab','0','scene.oct'],cwd=output,
                    input='0 0 -2 0 0 1\n',text=True,capture_output=True,env=engine.radiance_environment())
                self.assertEqual(traced.returncode,0,traced.stderr)
                self.assertTrue(all(float(x)>0 for x in traced.stdout.split()))

    @unittest.skipUnless(all(shutil.which(x,path=(engine.find_radiance() or '')+os.pathsep+os.environ.get('PATH',''))
                            for x in ('oconv','rpict')),'Radiance required')
    def test_exported_view_is_readable_by_rpict(self):
        output,_=self.run_cli('triangle.obj')
        octree=subprocess.run(['oconv','scene.rad'],cwd=output,capture_output=True,env=engine.radiance_environment())
        self.assertEqual(octree.returncode,0,octree.stderr)
        (output/'scene.oct').write_bytes(octree.stdout)
        result=subprocess.run(['rpict','-vf','view.vf','-x','16','-y','12','-pa','0',
            '-av','.2','.2','.2','scene.oct'],cwd=output,capture_output=True,timeout=30,env=engine.radiance_environment())
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn(b'FORMAT=32-bit_rle_rgbe',result.stdout)

    @unittest.skipUnless(engine.find_radiance(),'Radiance required')
    def test_cli_photometry_with_lib_only_raypath(self):
        from test_evo_identity import STEP
        with zipfile.ZipFile(self.inputs/'photo.evo','w') as z:
            z.writestr('Project/ProjectData/ProjectData.dat',STEP)
        self.env['RAYPATH']=str(Path(engine.RADIANCE_BIN).parent/'lib')
        self.env['RADIANCE_BIN']=engine.RADIANCE_BIN
        for name,count in (('light.ies',1),('light.ldt',1),('photo.evo',3)):
            with self.subTest(name=name):
                output,scene=self.run_cli(name)
                self.assertEqual(len(scene['lights']),count)
                self.assertEqual(len(list((output/'light').glob('*.dat'))),count)
                self.assertNotIn('Without photometry',(output/'REPORT.txt').read_text())

    @unittest.skipUnless(os.name=='posix','POSIX signal test')
    def test_cli_sigterm_cleans_assimp_and_temporary_files(self):
        self.check_cli_signal(signal.SIGTERM)

    @unittest.skipUnless(os.name=='posix','POSIX signal test')
    def test_cli_sigint_cleans_assimp_without_traceback(self):
        self.check_cli_signal(signal.SIGINT)

    def check_cli_signal(self,signum):
        # Synthetic Assimp with a duration independent of data size: real child and descendant processes.
        tools=self.root/'tools';tools.mkdir()
        marker=self.root/'ready.json'
        script=tools/'assimp'
        script.write_text('#!'+sys.executable+'''\nimport json,os,subprocess,sys,time
from pathlib import Path
child=subprocess.Popen([sys.executable,'-B','-c','import time;time.sleep(60)'])
Path(os.environ['SIGNAL_READY']).write_text(json.dumps([os.getpid(),child.pid]))
time.sleep(60)
''')
        script.chmod(0o755)
        output=self.root/'cancelled'
        env={**self.env,'PATH':str(tools)+os.pathsep+self.env.get('PATH',''),
             'SIGNAL_READY':str(marker)}
        before=(self.inputs/'ascii.stl').read_bytes()
        process=subprocess.Popen([sys.executable,'-B','-m','core',str(self.inputs/'ascii.stl'),str(output)],
            cwd=ROOT,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        pids=[]
        def alive(pid):
            try:
                if sys.platform.startswith('linux'):
                    return Path('/proc',str(pid),'stat').read_text().rsplit(')',1)[1].split()[0]!='Z'
                os.kill(pid,0)
                return True
            except (FileNotFoundError,ProcessLookupError):
                return False
        try:
            until=time.monotonic()+10
            while time.monotonic()<until and process.poll() is None:
                try:
                    pids=json.loads(marker.read_text())
                    break
                except (FileNotFoundError,json.JSONDecodeError):
                    time.sleep(.01)
            self.assertEqual(len(pids),2,'Assimp did not reach its waiting point')
            self.assertTrue(list(output.glob('.conversion-*/model/.assimp_*')))
            process.send_signal(signum)
            stdout,stderr=process.communicate(timeout=10)
            until=time.monotonic()+2
            while any(alive(pid) for pid in pids) and time.monotonic()<until:time.sleep(.01)
            self.assertFalse(any(alive(pid) for pid in pids),'Child or descendant process remains')
            self.assertEqual(process.returncode,128+signum,stderr)
            self.assertEqual(stderr.strip(),'Cancelled.')
            self.assertEqual(stdout,'')
            self.assertEqual([p.name for p in output.iterdir()],['REPORT.txt'])
            self.assertIn('Status: cancelled',(output/'REPORT.txt').read_text())
            self.assertEqual((self.inputs/'ascii.stl').read_bytes(),before)
        finally:
            if process.poll() is None:process.kill()
            process.communicate(timeout=5)
            for pid in pids:
                if alive(pid):os.kill(pid,signal.SIGKILL)

    @unittest.skipUnless(os.name=='posix','POSIX signal test')
    def test_cancel_during_delivery_removes_partial_result_and_restores_handlers(self):
        from core.__main__ import main
        move=shutil.move
        handlers={sig:signal.getsignal(sig) for sig in (signal.SIGINT,signal.SIGTERM)}
        for sig in handlers:
            with self.subTest(signal=sig):
                output=self.root/('delivery-'+str(sig))
                def interrupted_move(src,dst):
                    move(src,dst)
                    os.kill(os.getpid(),sig)
                from contextlib import redirect_stderr
                import io
                stderr=io.StringIO()
                with patch.object(shutil,'move',side_effect=interrupted_move),redirect_stderr(stderr):
                    self.assertEqual(main([str(self.inputs/'triangle.obj'),str(output)]),128+sig)
                self.assertEqual(stderr.getvalue().strip(),'Cancelled.')
                self.assertEqual([p.name for p in output.iterdir()],['REPORT.txt'])
                self.assertIn('Status: cancelled',(output/'REPORT.txt').read_text())
                self.assertEqual({s:signal.getsignal(s) for s in handlers},handlers)

    def test_delivery_failure_removes_partial_result(self):
        from core.__main__ import main
        from contextlib import ExitStack,redirect_stderr
        import io
        move=shutil.move
        for cleanup_fails in (False,True):
            with self.subTest(cleanup_fails=cleanup_fails):
                output=self.root/('delivery-error-'+str(cleanup_fails))
                calls=[]
                def broken_move(src,dst):
                    calls.append(dst)
                    if len(calls)==2:
                        raise OSError('move failed')
                    return move(src,dst)
                stderr=io.StringIO()
                with ExitStack() as stack:
                    stack.enter_context(redirect_stderr(stderr))
                    stack.enter_context(patch.object(shutil,'move',side_effect=broken_move))
                    if cleanup_fails:
                        stack.enter_context(patch.object(Path,'unlink',side_effect=PermissionError('locked')))
                    self.assertEqual(main([str(self.inputs/'triangle.obj'),str(output)]),1)
                # Cleanup errors do not hide the original error. Its message remains on screen and in the report.
                self.assertEqual(stderr.getvalue().strip(),'Error: move failed')
                report=(output/'REPORT.txt').read_text()
                self.assertIn('Status: error',report)
                self.assertIn('move failed',report)
                if cleanup_fails:
                    self.assertIn('Output file that could not be rolled back: '+Path(calls[0]).name,report)
                    self.assertEqual(sorted(p.name for p in output.iterdir()),sorted(['REPORT.txt',Path(calls[0]).name]))
                else:
                    self.assertEqual([p.name for p in output.iterdir()],['REPORT.txt'])

    def test_bad_input_exit_and_failure_report(self):
        bad=self.inputs/'bad.ies';bad.write_text(synthetic.IES.rsplit('100 100 100',1)[0]+'100')
        out=self.root/'bad-out'
        r=subprocess.run([sys.executable,'-B','-m','core',str(bad),str(out)],cwd=ROOT,env=self.env,capture_output=True,text=True)
        self.assertNotEqual(r.returncode,0)
        self.assertIn('Error:',r.stderr)
        self.assertIn('Status: error',(out/'REPORT.txt').read_text())
        self.assertFalse(list(out.glob('.conversion-*')))
        unsupported=self.inputs/'bad.xyz';unsupported.write_text('test')
        with self.assertRaisesRegex(ValueError,'Unsupported'):
            convert(unsupported,self.root/'unsupported')

    def test_existing_output_and_symlink_rejected(self):
        out=self.root/'occupied';out.mkdir();(out/'keep').write_text('original')
        with self.assertRaises(FileExistsError):
            convert(self.inputs/'triangle.obj',out)
        self.assertEqual((out/'keep').read_text(),'original')
        link=self.root/'link';link.symlink_to(out,target_is_directory=True)
        with self.assertRaises(ValueError):
            convert(self.inputs/'triangle.obj',link/'nested')
        with self.assertRaises(FileNotFoundError):
            convert(self.inputs/'triangle.obj',self.root/'missing'/'nested')
        self.assertFalse((self.root/'missing').exists())

    def test_writes_confined_and_inputs_unchanged(self):
        synthetic.photometry_zip(self.inputs/'package.zip')
        before={p.name:p.read_bytes() for p in self.inputs.iterdir()}
        code='''import sys,os,runpy
from pathlib import Path
root=Path(sys.argv[-1]).resolve()
def inside(path,fd=None):
 if isinstance(path,int): return
 p=Path(os.fsdecode(path))
 if not p.is_absolute() and fd is not None and fd>=0:
  info=os.fstat(fd)
  bases=[root,*[q for q in root.rglob('*') if q.is_dir()]]
  base=next(q for q in bases if (q.stat().st_dev,q.stat().st_ino)==(info.st_dev,info.st_ino))
  p=base/p
 p=p.resolve()
 if p!=root and root not in p.parents: raise AssertionError('Outside write: '+str(p))
def guard(event,args):
 if event=='open':
  path,mode,flags=args
  if flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND): inside(path)
 elif event=='os.mkdir': inside(args[0],args[2])
 elif event in ('os.remove','os.rmdir'): inside(args[0],args[1])
 elif event=='os.rename': inside(args[0],args[2]);inside(args[1],args[3])
sys.addaudithook(guard)
sys.argv=['core',*sys.argv[1:]]
runpy.run_module('core',run_name='__main__')
'''
        for args in (['triangle.obj'],['scene.ls10'],['room.evo'],['products','package.zip']):
            out=self.root/(args[-1]+'_audit')
            r=subprocess.run([sys.executable,'-B','-c',code,*args[:-1],str(self.inputs/args[-1]),str(out)],
                             cwd=ROOT,env=self.env,capture_output=True,text=True)
            self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.inputs.iterdir()})

    def test_program_does_not_create_default_folders(self):
        # The program must not create directories automatically under HOME or the source root.
        home=self.root/'home';home.mkdir()
        env={k:v for k,v in self.env.items() if k not in ('CTLUX_DATA_DIR','XDG_DATA_HOME')}
        env['HOME']=str(home)
        before=sorted(p.name for p in ROOT.iterdir())
        synthetic.photometry_zip(self.inputs/'package.zip')
        for args in ([str(self.inputs/'room.evo'),str(self.root/'k1')],
                     ['products',str(self.inputs/'package.zip'),str(self.root/'k2')]):
            r=subprocess.run([sys.executable,'-B','-m','core',*args],cwd=ROOT,env=env,capture_output=True,text=True)
            self.assertEqual(r.returncode,0,r.stderr)
        r=subprocess.run([sys.executable,'-B','-c','import core.engine, core.format_detector, core.photometry'],
                         cwd=ROOT,env=env,capture_output=True,text=True)
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(list(home.iterdir()),[])
        self.assertEqual(sorted(p.name for p in ROOT.iterdir()),before)

    def test_radiance_writer_camera_and_disabled_light(self):
        out,scene=self.run_cli('scene.ls10')
        scene['lights'][0]['enabled']=False
        result=scene_writers.export_scene('radiance',scene,self.root/'other')
        self.assertTrue(any('Disabled light' in w for w in result['warnings']))
        self.assertNotIn('l0_m',(self.root/'other/lights.rad').read_text())
        with self.assertRaises(FileExistsError):
            scene_writers.export_scene('radiance',scene,self.root/'other')


class ProductsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}

    def run_cli(self, source, code=0):
        output = self.root/(source.name+'_products')
        before = source.read_bytes()
        result = subprocess.run([sys.executable,'-B','-m','core','products',str(source),str(output)],
                                cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, code, result.stderr)
        self.assertEqual(source.read_bytes(), before)
        self.assertFalse(list(output.glob('.products-*')))
        for section in ('READ','PARTIAL','SKIPPED','LIMITS'):
            self.assertIn(section, (output/'REPORT.txt').read_text())
        return output

    def table(self, output):
        data = json.loads((output/'PRODUCTS.json').read_text())
        text = (output/'PRODUCTS.txt').read_text()
        # The table follows the heading, two-line photometry notice, input and blank line.
        self.assertEqual(text.splitlines()[:3], ['CTLux product table', *PHOTOMETRY_NOTICE])
        rows = text.split('\n\nREJECTED\n')[0].splitlines()[5:]
        # One header and one row per product. Rejected products have a separate section.
        self.assertEqual(len(rows), 1+len(data['products']))
        for unit in ('Lumens (lm)', 'Beam angle (°)', 'Peak intensity (cd)'):
            self.assertIn(unit, rows[0])
        for item, row in zip(data['products'], rows[1:]):
            self.assertTrue(row.endswith(item['ies']))
        for item in data['rejected']:
            self.assertIn('- %s: %s' % (item['name'], item['reason'].rstrip().rstrip('.')), text)
        return data

    def assert_ies2rad_reads(self, output):
        files = sorted(output.glob('*.ies'))
        self.assertTrue(files)
        for ies in files:
            with tempfile.TemporaryDirectory() as work:
                r = subprocess.run([IES2RAD,'-o',str(Path(work)/'x'),str(ies)],capture_output=True,text=True,
                                   env=engine.radiance_environment())
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertTrue((Path(work)/'x.rad').is_file())
                self.assertTrue((Path(work)/'x.dat').stat().st_size > 0)

    @unittest.skipUnless(IES2RAD, 'Radiance ies2rad required')
    def test_evo_products_once_with_usage_and_rejections(self):
        output = self.run_cli(synthetic.evo_with_products(self.root/'Example Office.evo'))
        self.assertEqual(sorted(p.name for p in output.iterdir()),
            ['PRODUCTS.json','PRODUCTS.txt','REPORT.txt',
             'project_example_office_downlight_72d_1200lm_u202.ies','project_example_office_wide_283d_3000lm_u302.ies'])
        data = self.table(output)
        self.assertEqual([(u['name'],u['lumens'],u['usage'],u['manufacturer']) for u in data['products']],
                         [('Synthetic spot A',1200,2,None),('Synthetic wide B',3000,1,None)])
        self.assertEqual([(u['c_planes'],u['gamma_angles'],u['peak_candela']) for u in data['products']],
                         [(1,6,1200),(4,3,1560)])
        self.assertAlmostEqual(data['products'][0]['beam_angle'],36)
        rejected = {u['name']:u for u in data['rejected']}
        self.assertEqual(set(rejected), {'Synthetic no-lumens C','Synthetic invalid D'})
        self.assertIn('has no lumens', rejected['Synthetic no-lumens C']['reason'])
        self.assertIn('candela', rejected['Synthetic invalid D']['reason'])
        self.assertEqual([u['usage'] for u in data['rejected']], [1,1])
        report = (output/'REPORT.txt').read_text()
        self.assertIn('Status: partial', report)
        self.assertIn('EVO: 6 luminaire instances and 4 products read', report)
        self.assertIn('1 EVO luminaire instances have no product photometry', report)
        self.assertIn('1 1200 1 6 1', (output/'project_example_office_downlight_72d_1200lm_u202.ies').read_text())
        self.assert_ies2rad_reads(output)

    @unittest.skipUnless(IES2RAD, 'Radiance ies2rad required')
    def test_evo_ies_header_is_neutral_with_notice_and_same_light(self):
        output = self.run_cli(synthetic.evo_with_products(self.root/'office.evo'))
        files = sorted(output.glob('*.ies'))
        self.assertEqual(len(files), 2)
        for ies in files:
            with self.subTest(ies=ies.name):
                data = ies.read_bytes()
                self.assertTrue(ies.name.startswith('project_office_'))
                self.assertNotIn(b'dialux', data.lower())
                self.assertTrue(all(b < 128 for b in data))
                self.assertTrue(all(len(x) <= 80 for x in data.decode('ascii').splitlines()))
                self.assertEqual(ies_header(data), ['IESNA:LM-63-2002', '[TEST] not available',
                    '[TESTLAB] not available', '[ISSUEDATE] not available',
                    '[MANUFAC] not read from the project record', *PROJECT_NOTICE, 'TILT=NONE'])
                # With the old header, the same body produces the same distribution and light in ies2rad.
                body = data.split(b'TILT=NONE\n', 1)[1]
                old = b'IESNA:LM-63-2002\n[TEST] DIALux evo\n[MANUFAC] DIALux\nTILT=NONE\n' + body
                self.assertEqual(ies2rad_light(data), ies2rad_light(old))
        report = (output/'REPORT.txt').read_text()
        self.assertEqual(report.splitlines()[:3], ['CTLux', *PHOTOMETRY_NOTICE])
        self.assertNotIn('DIALux', report)

    def test_package_ies_keep_source_keywords_and_get_notice(self):
        from test_ldt_translation import LDTTest
        ldt = self.root/'single.ldt'; ldt.write_text('\n'.join(LDTTest().fixture()))
        single = self.root/'single.ies'; single.write_bytes(synthetic.product_ies(800, (0, 90), (300, 0)).encode()
                                                   .replace(b'\n', b'\r\n'))
        for source in (synthetic.photometry_zip(self.root/'package.zip'), synthetic.gldf(self.root/'luminaire.gldf'),
                       single, ldt):
            output = self.run_cli(source)
            self.assertEqual((output/'REPORT.txt').read_text().splitlines()[:3], ['CTLux', *PHOTOMETRY_NOTICE])
            for ies in sorted(output.glob('*.ies')):
                with self.subTest(source=source.name, ies=ies.name):
                    header = ies_header(ies.read_bytes().replace(b'\r\n', b'\n'))
                    self.assertEqual(header[-len(FILE_NOTICE)-1:], [*FILE_NOTICE, 'TILT=NONE'])
                    self.assertEqual(header[0], 'IESNA:LM-63-2002')
                    if source.suffix == '.ldt' or ies.name in ('wide.ies', 'luminaire_product.ies'):
                        self.assertIn('[LUMINAIRE] fixture', header)
                    else:
                        self.assertIn('[MANUFAC] Example Manufacturer', header)
                    if IES2RAD and source is single:
                        # Line endings match the source. The notice does not change the photometry.
                        self.assertEqual(ies.read_bytes().count(b'\n'), ies.read_bytes().count(b'\r\n'))
                        self.assertEqual(ies2rad_light(ies.read_bytes()), ies2rad_light(single.read_bytes()))
        package = self.root/'package.zip_products'
        if IES2RAD:
            with zipfile.ZipFile(self.root/'package.zip') as z:
                source_bytes = z.read('Manufacturer/spot_a.ies')
            self.assertEqual(ies2rad_light((package/'spot_a_62d_1500lm.ies').read_bytes()), ies2rad_light(source_bytes))

    def test_notice_is_added_once(self):
        from core.photometry import annotated_ies
        data = synthetic.product_ies(800, (0, 90), (300, 0)).encode()
        first = annotated_ies(data)
        self.assertEqual(annotated_ies(first), first)
        self.assertEqual(first.count(FILE_NOTICE[0].encode()), 1)
        self.assertEqual(annotated_ies(b'No TILT line\n'), b'No TILT line\n')

    def test_settings_path_has_one_rule(self):
        code = 'from core import paths as y; print(y.SETTINGS_FILE)'
        env = {k: v for k, v in self.env.items() if k != 'CTLUX_DATA_DIR'}
        r = subprocess.run([sys.executable,'-B','-c',code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(Path(r.stdout.strip()), Path(ROOT).resolve()/'settings'/'settings.json')
        env['CTLUX_DATA_DIR'] = str(self.root)
        r = subprocess.run([sys.executable,'-B','-c',code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(Path(r.stdout.strip()), Path(self.root).resolve()/'settings'/'settings.json')
        self.assertFalse((self.root/'settings').exists())

    def test_single_input_rule(self):
        single = self.root/'single.ies'; single.write_text(synthetic.product_ies(800, (0, 90), (300, 0)))
        second = self.root/'second.ies'; second.write_bytes(single.read_bytes())
        directory = self.root/'folder'; directory.mkdir(); shutil.copy(single, directory/'single.ies')
        for command in (['products'], []):
            with self.subTest(command=command):
                output = self.root/('folder_output' + ''.join(command))
                r = subprocess.run([sys.executable,'-B','-m','core',*command,str(directory),str(output)],
                                   cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=60)
                self.assertEqual(r.returncode, 1)
                self.assertIn('Unsupported input', r.stderr)
                self.assertFalse(output.exists())
                output = self.root/('two_inputs_output' + ''.join(command))
                r = subprocess.run([sys.executable,'-B','-m','core',*command,str(single),str(second),str(output)],
                                   cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=60)
                self.assertEqual(r.returncode, 2)
                self.assertIn('unrecognized arguments', r.stderr)
                self.assertFalse(output.exists())
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['folder', 'second.ies', 'single.ies'])

    def test_photometry_zip_names_lumen_rejections_and_duplicate(self):
        output = self.run_cli(synthetic.photometry_zip(self.root/'package.zip'))
        self.assertEqual(sorted(p.name for p in output.iterdir()),
            ['PRODUCTS.json','PRODUCTS.txt','REPORT.txt','spot_a_62d_1500lm.ies','wide.ies'])
        data = self.table(output)
        self.assertEqual([(u['ies'],u['name'],u['manufacturer'],u['lumens'],u['usage']) for u in data['products']],
            [('spot_a_62d_1500lm.ies','Package spot','Example Manufacturer',1500,None),('wide.ies','fixture','0',2000,None)])
        rejected = {u['name']:u['reason'] for u in data['rejected']}
        self.assertEqual(set(rejected), {'absolute_180d.ies','invalid.ies'})
        self.assertIn('no lumens', rejected['absolute_180d.ies'])
        self.assertIn('negative', rejected['invalid.ies'])
        report = (output/'REPORT.txt').read_text()
        self.assertIn('Written once', report)
        self.assertIn('Status: partial', report)
        if IES2RAD:
            self.assert_ies2rad_reads(output)

    def test_single_ldt_and_gldf(self):
        from test_ldt_translation import LDTTest
        ldt = self.root/'single.ldt'; ldt.write_text('\n'.join(LDTTest().fixture()))
        for source, name in ((ldt,'single.ies'), (synthetic.gldf(self.root/'luminaire.gldf'),'luminaire_product.ies')):
            with self.subTest(source=source.name):
                output = self.run_cli(source)
                self.assertEqual(sorted(p.name for p in output.iterdir()),
                                 ['PRODUCTS.json','PRODUCTS.txt','REPORT.txt',name])
                data = self.table(output)
                self.assertEqual([(u['name'],u['lumens'],u['peak_candela']) for u in data['products']],
                                 [('fixture',2000,120)])
                self.assertEqual(data['rejected'], [])
                self.assertIn('Status: success', (output/'REPORT.txt').read_text())
                if IES2RAD:
                    self.assert_ies2rad_reads(output)

    def test_ldt_header_source_and_unknown_cct(self):
        from test_ldt_translation import LDTTest
        for manufacturer, date, cct in (('', '', '0'), ('Café Works', '2026-09-28', '3000'),
                                         ('', '', '0.0'), ('', '', 'NaN'), ('', '', 'unknown')):
            with self.subTest(manufacturer=manufacturer, date=date, cct=cct):
                lines = LDTTest().fixture()
                lines[0], lines[11], lines[29] = manufacturer, date, cct
                lines[8] = 'Café light'
                source, target = self.root/'header.ldt', self.root/'header.ies'
                source.write_bytes('\n'.join(lines).encode('latin-1'))
                engine.ldt_to_ies(source, target)
                text = target.read_text(encoding='ascii')
                header = ies_header(target.read_bytes())
                self.assertEqual(header[:5], ['IESNA:LM-63-2002', '[TEST] not available',
                    '[TESTLAB] not available', '[ISSUEDATE] '+(date or 'not available'),
                    '[MANUFAC] '+('Cafe Works' if manufacturer else 'not available')])
                self.assertIn('[LUMINAIRE] Cafe light', header)
                self.assertIn('[_SOURCE] Converted from EULUMDAT, Isym=1', header)
                self.assertIn('[_LUMENS] 2000', header)
                self.assertEqual(header[-3:], [*FILE_NOTICE, 'TILT=NONE'])
                self.assertEqual([line for line in header if line.startswith('[LAMP]')],
                                 ['[LAMP] CCT 3000'] if cct == '3000' else [])
                self.assertTrue(all(len(line) <= 80 for line in header))
                self.assertNotIn('from_source', text)

    @unittest.skipUnless(IES2RAD, 'Radiance ies2rad required')
    def test_ldt_roundtrip_preserves_flux_and_ies2rad_light(self):
        from test_ldt_translation import LDTTest
        source = self.root/'roundtrip.ldt'
        source.write_text('\n'.join(LDTTest().fixture()))
        output = self.run_cli(source)
        ies = next(output.glob('*.ies'))
        second = self.run_cli(ies)
        self.assertEqual(self.table(second)['products'][0]['lumens'], 2000)
        self.assertEqual(self.table(second)['rejected'], [])
        self.assertEqual(ies.read_bytes(), next(second.glob('*.ies')).read_bytes())
        self.assert_ies2rad_reads(output)
        self.assert_ies2rad_reads(second)
        body = ies.read_bytes().split(b'TILT=NONE\n', 1)[1]
        self.assertEqual(body, b'1 -1 1 3 1 1 2 0 0 0\n1 1 10\n0 90 180\n0\n40 80 120\n')
        old = b'IESNA:LM-63-2002\n[TEST] LDT->IES conversion (CTLux)\nTILT=NONE\n'+body
        self.assertEqual(ies2rad_light(ies.read_bytes()), ies2rad_light(old))

    def test_absolute_ies_lumen_declaration_validation(self):
        for i, value in enumerate(('0', '-1', 'NaN', 'inf', '1e999', 'bad', '', '2000')):
            with self.subTest(value=value):
                source = self.root/('absolute%d.ies' % i)
                text = synthetic.product_ies(-1, (0,90), (100,0))
                source.write_text(text.replace('TILT=NONE', '[_LUMENS] '+value+'\nTILT=NONE'))
                output = self.run_cli(source, code=0 if value == '2000' else 1)
                if value == '2000':
                    self.assertEqual(self.table(output)['products'][0]['lumens'], 2000)
                else:
                    self.assertIn('invalid [_LUMENS]', (output/'REPORT.txt').read_text())

    def test_no_writable_product_is_error_with_report(self):
        path = self.root/'invalid.zip'
        with zipfile.ZipFile(path,'w') as z:
            z.writestr('invalid.ies', synthetic.product_ies(900,(0,90),(100,-1)))
        output = self.run_cli(path, code=1)
        self.assertEqual([p.name for p in output.iterdir()], ['REPORT.txt'])
        report = (output/'REPORT.txt').read_text()
        # Reports with no remaining product files have no photometry notice.
        self.assertEqual(report.splitlines()[:2], ['CTLux', 'Status: error'])
        self.assertIn('invalid.ies: IES candela/multiplier cannot be negative, IES not written', report)

    def test_output_rules_match_conversion(self):
        source = synthetic.photometry_zip(self.root/'package.zip')
        occupied = self.root/'occupied'; occupied.mkdir(); (occupied/'keep').write_text('original')
        with self.assertRaises(FileExistsError):
            products(source, occupied)
        self.assertEqual((occupied/'keep').read_text(), 'original')
        with self.assertRaisesRegex(ValueError, 'nested'):
            products(source, source/'inside')
        unsupported = self.root/'model.obj'; unsupported.write_bytes(synthetic.OBJ)
        with self.assertRaisesRegex(ValueError, 'Unsupported'):
            products(unsupported, self.root/'obj-out')

    def test_delivery_failure_removes_products(self):
        from core.__main__ import main
        from contextlib import redirect_stderr
        import io
        source = synthetic.photometry_zip(self.root/'package.zip')
        output = self.root/'delivery-failure'
        move = shutil.move
        calls = []
        def broken_move(src, dst):
            calls.append(dst)
            if len(calls) == 2:
                raise OSError('move failed')
            return move(src, dst)
        stderr = io.StringIO()
        with patch.object(shutil,'move',side_effect=broken_move),redirect_stderr(stderr):
            self.assertEqual(main(['products',str(source),str(output)]), 1)
        self.assertEqual(stderr.getvalue().strip(), 'Error: move failed')
        self.assertEqual([p.name for p in output.iterdir()], ['REPORT.txt'])
        report = (output/'REPORT.txt').read_text()
        self.assertIn('Status: error', report)
        self.assertIn('move failed', report)

    def gldf_bytes(self, *members):
        import io
        data = io.BytesIO()
        with zipfile.ZipFile(data,'w') as archive:
            archive.writestr('product.xml', '<Root/>')
            for name, text in members:
                archive.writestr(name, text)
        return data.getvalue()

    def spot(self, lumens, name='Synthetic spot'):
        return synthetic.product_ies(lumens, (0,10,20,30,90), (2000,1500,600,50,0), name=name)

    def test_same_named_gldf_in_zip(self):
        different = self.root/'different.zip'
        with zipfile.ZipFile(different,'w') as z:
            z.writestr('a/same.gldf', self.gldf_bytes(('ldc/product.ies', self.spot(1000))))
            z.writestr('b/same.gldf', self.gldf_bytes(('ldc/product.ies', self.spot(2000))))
        output = self.run_cli(different)
        self.assertEqual(sorted(p.name for p in output.iterdir()),
                         ['PRODUCTS.json','PRODUCTS.txt','REPORT.txt','same_product.ies','same_product_2.ies'])
        data = self.table(output)
        self.assertEqual([(u['ies'],u['lumens']) for u in data['products']],
                         [('same_product.ies',1000),('same_product_2.ies',2000)])
        self.assertIn('2 photometry files found', (output/'REPORT.txt').read_text())
        same = self.root/'same.zip'
        with zipfile.ZipFile(same,'w') as z:
            for folder in ('a','b'):
                z.writestr(folder+'/same.gldf', self.gldf_bytes(('ldc/product.ies', self.spot(1000))))
        output = self.run_cli(same)
        self.assertEqual(sorted(p.name for p in output.iterdir()),
                         ['PRODUCTS.json','PRODUCTS.txt','REPORT.txt','same_product.ies'])
        self.assertEqual([u['lumens'] for u in self.table(output)['products']], [1000])
        report = (output/'REPORT.txt').read_text()
        self.assertIn('same_product_2.ies: same content as same_product.ies. Written once', report)
        self.assertIn('Status: partial', report)

    def test_same_base_name_members_in_one_gldf(self):
        source = self.root/'luminaire.gldf'
        source.write_bytes(self.gldf_bytes(('ldc/a/product.ies', self.spot(1000)), ('ldc/b/product.ies', self.spot(2000))))
        output = self.run_cli(source)
        data = self.table(output)
        self.assertEqual([(u['ies'],u['lumens']) for u in data['products']],
                         [('luminaire_product.ies',1000),('luminaire_product_2.ies',2000)])

    def test_same_named_ies_in_zip(self):
        # Both members get the same filename metadata. Their content differs in the LUMINAIRE line.
        source = self.root/'ies.zip'
        with zipfile.ZipFile(source,'w') as z:
            z.writestr('a/x.ies', self.spot(1500, 'Spot A'))
            z.writestr('b/x.ies', self.spot(1500, 'Spot B'))
        output = self.run_cli(source)
        data = self.table(output)
        self.assertEqual([(u['ies'],u['name']) for u in data['products']],
                         [('x_62d_1500lm.ies','Spot A'),('x_62d_1500lm_2.ies','Spot B')])

    def test_extraction_never_overwrites_existing_file(self):
        target = self.root/'target'; target.mkdir()
        (target/'package_product.ies').write_text('previous')
        (target/'x_62d_1500lm.ies').write_text('previous')
        gldf = self.root/'package.gldf'
        gldf.write_bytes(self.gldf_bytes(('ldc/product.ies', self.spot(1000))))
        self.assertEqual([Path(p).name for p in importer.extract_gldf(str(gldf), str(target))], ['package_product_2.ies'])
        source = self.root/'x.zip'
        with zipfile.ZipFile(source,'w') as z:
            z.writestr('x.ies', self.spot(1500))
        self.assertEqual(importer.extract_photometry_zip(str(source), str(target)), ['x_62d_1500lm_2.ies'])
        self.assertEqual((target/'package_product.ies').read_text(), 'previous')
        self.assertEqual((target/'x_62d_1500lm.ies').read_text(), 'previous')
        self.assertEqual(sorted(p.name for p in target.iterdir()),
                         ['package_product.ies','package_product_2.ies','x_62d_1500lm.ies','x_62d_1500lm_2.ies'])

    @unittest.skipUnless(os.name=='posix','POSIX signal test')
    def test_cancel_during_delivery_removes_products(self):
        from core.__main__ import main
        from contextlib import redirect_stderr
        import io
        source = synthetic.photometry_zip(self.root/'package.zip')
        move = shutil.move
        handlers = {sig:signal.getsignal(sig) for sig in (signal.SIGINT,signal.SIGTERM)}
        for sig in handlers:
            with self.subTest(signal=sig):
                output = self.root/('cancelled-'+str(sig))
                def interrupted_move(src, dst):
                    move(src, dst)
                    os.kill(os.getpid(), sig)
                stderr = io.StringIO()
                with patch.object(shutil,'move',side_effect=interrupted_move),redirect_stderr(stderr):
                    self.assertEqual(main(['products',str(source),str(output)]), 128+sig)
                self.assertEqual(stderr.getvalue().strip(), 'Cancelled.')
                self.assertEqual([p.name for p in output.iterdir()], ['REPORT.txt'])
                self.assertIn('Status: cancelled', (output/'REPORT.txt').read_text())
                self.assertEqual({s:signal.getsignal(s) for s in handlers}, handlers)


class EnglishCLITest(unittest.TestCase):
    def test_conversion_limit_flags_reach_backend(self):
        from core import __main__ as cli
        for flags in (['--source-limit-mib', '2048', '--triangle-limit', '6000000'],
                      ['--triangle-limit', '6000000', '--source-limit-mib', '2048']):
            with self.subTest(flags=flags), patch.object(cli, 'convert', return_value={'output': 'out'}) as convert:
                self.assertEqual(cli.main(['input.obj', 'out', *flags]), 0)
                convert.assert_called_once_with('input.obj', 'out', source_limit_mib=2048, triangle_limit=6000000)

    def test_invalid_limits_do_not_call_backend(self):
        from core import __main__ as cli
        for flag, value in (('--source-limit-mib', '0'), ('--triangle-limit', '-1')):
            with self.subTest(flag=flag), patch.object(cli, 'convert') as convert:
                with self.assertRaises(SystemExit) as error:
                    cli.main(['input.obj', 'out', flag, value])
                self.assertEqual(error.exception.code, 2)
                convert.assert_not_called()

    def test_products_command_writes_real_photometry(self):
        with tempfile.TemporaryDirectory() as work:
            work = Path(work)
            source = synthetic.photometry_zip(work/'package.zip')
            for command, folder in (('products', 'products'), ('products', 'products-repeat')):
                output = work/folder
                result = subprocess.run([sys.executable, '-B', '-m', 'core', command, str(source), str(output)],
                                        cwd=ROOT, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                products = json.loads((output/'PRODUCTS.json').read_text())
                self.assertTrue(products['products'])
                self.assertTrue(list(output.glob('*.ies')))
                self.assertTrue((output/'REPORT.txt').is_file())


class PathAndReportRegressionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def test_evo_partial_notices_stay_out_of_read_section(self):
        from core import format_detector as format_detector
        source = synthetic.evo(self.root/'room.evo')
        with zipfile.ZipFile(source, 'a') as z:
            z.writestr('mesh/detail.m3d', b'')
        project = self.root/'project'; (project/'model').mkdir(parents=True)
        read, partial = [], []
        format_detector._place_evo(str(source), str(project), str(project/'model/input.obj'), read, partial)
        self.assertTrue(any('Embedded EVO .m3d' in x for x in partial))
        self.assertFalse(set(read) & set(partial))
        output = self.root/'out'
        convert(source, output)
        report = (output/'REPORT.txt').read_text()
        read_section = report.split('\nREAD\n', 1)[1].split('\nPARTIAL / ASSUMED\n', 1)[0]
        self.assertNotIn('Embedded EVO .m3d', read_section)
        self.assertEqual(report.count('Embedded EVO .m3d'), 1)

    def test_output_with_dotdot_into_scene_is_nested(self):
        from core.__main__ import _prepare_output
        scene = self.root/'a/scene'; scene.mkdir(parents=True); (self.root/'a/x').mkdir()
        with self.assertRaisesRegex(ValueError, 'nested'):
            _prepare_output(scene, str(self.root/'a/x') + '/../scene/out', None)
        self.assertFalse((scene/'out').exists())
        _, output = _prepare_output(scene, str(self.root/'a/x') + '/../result', None)
        self.assertEqual(output, self.root/'a/result')
        self.assertTrue(output.is_dir())

    def test_colon_is_a_file_name_character_outside_windows(self):
        from core.file_safety import internal_path
        for rel in ('C:x', 'c:/x', 'C:\\x', '//server/share/x', '\\\\server\\share\\x'):
            with self.assertRaises(ValueError):
                internal_path(self.root, rel)
        if os.name == 'nt':
            with self.assertRaises(ValueError):
                internal_path(self.root, 'a.obj:stream')
            return
        source = self.root/'scan 12:30.obj'; source.write_bytes(synthetic.OBJ)
        self.assertEqual(internal_path(self.root, source.name), str(source))
        convert(source, self.root/'out')
        self.assertTrue((self.root/'out/scene.rad').is_file())

    @unittest.skipIf(os.name == 'nt', 'POSIX file modes')
    def test_atomic_json_keeps_file_mode(self):
        from core.file_safety import atomic_json
        path = self.root/'project.json'; path.write_text('{}'); os.chmod(path, 0o640)
        atomic_json(path, {'x': 1})
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o640)
        self.assertEqual(json.loads(path.read_text()), {'x': 1})
        umask = os.umask(0o022); os.umask(umask)
        atomic_json(self.root/'new.json', {})
        self.assertEqual(os.stat(self.root/'new.json').st_mode & 0o777, 0o666 & ~umask)


if __name__=='__main__':
    unittest.main()
