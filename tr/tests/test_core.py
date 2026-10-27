# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Komut sözleşmesi, sessiz limitler ve gerçek sentetik format dönüşümleri."""
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

from core import engine as motor, importer as ice_aktarma, step_reader as step_oku, scene_model as sahne_dili, scene_writers as sahne_yazicilar, assimp_bridge as assimp_kopru
from core.tools import ies_analysis as ies_analiz, ls10_reader as lumion_oku
from core.__main__ import cevir, urunler, FOTOMETRI_NOTU
from core.photometry import NOT_DOSYA, NOT_PROJE
import synthetic as sentetik

ROOT = Path(__file__).resolve().parents[1]
IES2RAD = shutil.which('ies2rad', path=(motor.radiance_bul() or '')+os.pathsep+os.environ.get('PATH',''))


def ies2rad_isik(veri):
    """IES baytlarını ies2rad'dan geçirir, dağılım tablosu ve yorumsuz .rad satırlarını döner."""
    with tempfile.TemporaryDirectory() as work:
        (Path(work)/'x.ies').write_bytes(veri)
        r = subprocess.run([IES2RAD,'-o','x','x.ies'],cwd=work,capture_output=True,text=True,
                           env=motor.radiance_ortami())
        if r.returncode:
            raise AssertionError(r.stderr)
        rad = [x for x in (Path(work)/'x.rad').read_text().splitlines() if not x.startswith('#')]
        return (Path(work)/'x.dat').read_bytes(), rad


def ies_basligi(veri):
    satirlar = veri.decode('ascii').splitlines()
    return satirlar[:next(i for i, x in enumerate(satirlar) if x.startswith('TILT='))+1]


class SinirTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_report_warning_categories_are_case_insensitive(self):
        from core.__main__ import _not_sinifla
        partial, skipped, limits = [], [], []
        messages = ['Atlandı: sentetik ışık', 'SINIR aşıldı', 'Varsayılan birim']
        for message in [*messages, messages[0]]:
            _not_sinifla(message, partial, skipped, limits)
        self.assertEqual(skipped, [messages[0]])
        self.assertEqual(limits, [messages[1]])
        self.assertEqual(partial, [messages[2]])

    def test_evo_limit_is_reported(self):
        path = sentetik.evo(self.root/'a.evo', count=3)
        with warnings.catch_warnings(record=True) as records:
            warnings.simplefilter('always')
            arms = ice_aktarma.evo_armaturler(path, max_arm=1)
        self.assertEqual(len(arms), 1)
        self.assertTrue(any('okunan=3, sınır=1, atlanan=2' in str(w.message) for w in records))
        with self.assertRaises(ValueError):
            ice_aktarma.evo_armaturler(path, max_arm=-1)
        with warnings.catch_warnings(record=True):
            self.assertEqual(ice_aktarma.evo_armaturler(path, max_arm=0), [])

    def test_evo_furniture_limit_is_reported(self):
        from test_evo_components import STEP
        path=self.root/'furniture.evo'
        text=STEP+"\n#80=FurnitureElement(80,'a',(),#2,$,$);\n#81=FurnitureElement(81,'b',(),#2,$,$);"
        with zipfile.ZipFile(path,'w') as z:
            z.writestr('Project/ProjectData/ProjectData.dat',text)
        with warnings.catch_warnings(record=True) as records:
            warnings.simplefilter('always')
            _,count=ice_aktarma.evo_mobilyalar(path,str(self.root/'furniture.rad'),max_mob=1)
        self.assertEqual(count,1)
        self.assertTrue(any('okunan=2, sınır=1, atlanan=1' in str(w.message) for w in records))

    def test_step_cut_tail_is_reported_but_quoted_text_is_data(self):
        with warnings.catch_warnings(record=True) as records:
            warnings.simplefilter('always')
            got = step_oku.kayitlar("#1=A('x; #9=foo'); #2=B('cut")
        self.assertEqual(list(got), [1])
        self.assertEqual(len(records), 1)
        self.assertIn('#2', str(records[0].message))

    def test_ies_incomplete_and_extra_candela_are_errors(self):
        path = self.root/'a.ies'
        for text in (sentetik.IES.rsplit('100 100 100',1)[0]+'100\n', sentetik.IES+'100\n'):
            path.write_text(text)
            with self.assertRaisesRegex(ValueError, 'tablosu boyu'):
                ies_analiz.ies_oku(path)
        path.write_text(sentetik.IES)
        self.assertEqual(ies_analiz.ies_oku(path)['kandela'], [100]*3)

    def test_ies_bad_counts_and_tilt_rejected(self):
        path = self.root/'a.ies'
        for text in (sentetik.IES.replace('1 3 1 1 2','1 3.5 1 1 2'),
                     sentetik.IES.replace('TILT=NONE','TILT=external'),
                     'IESNA\nTILT=INCLUDE\n1\n'):
            path.write_text(text)
            with self.assertRaises(ValueError):
                ies_analiz.ies_oku(path)

    def test_lumion_defaults_preserve_values_and_name_each_field(self):
        with warnings.catch_warnings(record=True) as records:
            warnings.simplefilter('always')
            lights = lumion_oku.isik_coz(sentetik.lumion(True))
        light = lights[0]
        self.assertEqual((light['tip'], light['cone'], light['w'], light['h']), (0,1,1,1))
        self.assertEqual(light['rgb'], (1,1,1))
        self.assertEqual(set(light['varsayilanlar']), {'rgb','tip','cone','w','h'})
        for field in light['varsayilanlar']:
            self.assertTrue(any(light['kaynak_id'] in str(w.message) and field+' varsayılan=' in str(w.message) for w in records))

    def test_lumion_missing_relative_field_and_incomplete_light_reported(self):
        from test_ls10_preservation import isik, tlv
        data = isik(1)
        start = data.index(b'IIM1')
        # matrisi bir kayıt sağa kaydır, toplam 37 kaydı koru: göreli #37 kalmaz.
        shifted = data[:start]+tlv(b'IIVE',[1])+data[start:-12]
        with warnings.catch_warnings(record=True) as records:
            warnings.simplefilter('always')
            lights = lumion_oku.isik_coz(shifted)
        self.assertEqual(lights[0]['tip'],0)
        self.assertTrue(any('tip varsayılan=0' in str(w.message) for w in records))
        with warnings.catch_warnings(record=True) as records:
            warnings.simplefilter('always')
            self.assertEqual(lumion_oku.isik_coz(data[:-24]),[])
        self.assertTrue(any('atlandı' in str(w.message) for w in records))

    def test_assimp_empty_uv_notation_and_bad_indices(self):
        path = self.root/'a.obj'
        path.write_bytes(sentetik.OBJ.replace(b'f 1 2 3', b'f  1/ 2/ 3/'))
        assimp_kopru._obj_dogrula(path)
        for face in (b'f 1// 2// 3//', b'f 0/ 2/ 3/', b'f 1/ 2/ 4/'):
            path.write_bytes(sentetik.OBJ.replace(b'f 1 2 3', face))
            with self.assertRaises(RuntimeError):
                assimp_kopru._obj_dogrula(path)

    def test_public_names_without_legacy_aliases(self):
        self.assertIs(motor.ldt_ies, ice_aktarma.ldt_ies)
        self.assertTrue(callable(motor.oto_cerceve))
        for name in ('_ldt_ies', '_oto_cerceve', 'LOG', 'log', 'KUTUPHANE', 'GELEN_KUTUSU',
                     'PROJELER_DIR', 'proje_olustur', 'proje_paketle', 'proje_ac'):
            self.assertFalse(hasattr(motor, name), name)
        self.assertTrue(callable(ice_aktarma.evo_fotometri_yaz))
        with self.assertRaises(AttributeError):
            ice_aktarma.evo_urun_hasat

    def test_core_import_guard_all_subdirectories(self):
        blocked = ('studio','sunucu','saglayicilar','proje_kasasi','http.server','webbrowser')
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
        sentetik.uret(self.inputs)
        self.env={**os.environ, 'PYTHONDONTWRITEBYTECODE':'1'}

    def run_cli(self,name):
        output=self.root/(name.replace('.','_')+'_out')
        result=subprocess.run([sys.executable,'-B','-m','core',str(self.inputs/name),str(output)],
                              cwd=ROOT,env=self.env,capture_output=True,text=True,timeout=60)
        self.assertEqual(result.returncode,0,result.stderr)
        scene=json.loads((output/'scene.ctlux.json').read_text())
        sahne_dili.validate_scene(scene)
        self.assertTrue((output/'scene.rad').is_file())
        self.assertTrue((output/'gorunum.vf').is_file())
        self.assertFalse(list(output.glob('.ceviri-*')))
        for section in ('OKUNAN','KISMİ KALAN','ATLANAN','SINIRLAR'):
            self.assertIn(section,(output/'RAPOR.txt').read_text())
        return output,scene

    def test_cli_obj(self):
        # fotometri dosyası yazılmayan çeviride not yoktur.
        output,scene=self.run_cli('ucgen.obj')
        self.assertEqual((output/'RAPOR.txt').read_text().splitlines()[:2], ['CTLux', 'Durum: kısmi'])
        self.assertEqual(len(scene['mesh']['f'])//3,1)

    def test_cli_ls10(self):
        _,scene=self.run_cli('sahne.ls10')
        self.assertEqual(len(scene['lights']),2)

    def test_cli_evo(self):
        _,scene=self.run_cli('oda.evo')
        self.assertEqual(len(scene['mesh']['f'])//3,12)
        self.assertEqual(len(scene['lights']),1)

    def test_cli_lumion_defaults_in_report(self):
        output,_=self.run_cli('varsayilan.ls10')
        report=(output/'RAPOR.txt').read_text()
        for field in ('rgb','tip','cone','w','h'):
            self.assertIn(field+' varsayılan=',report)

    def test_cli_evo_truncation_in_report(self):
        sentetik.evo(self.inputs/'cut.evo',truncated=True)
        output,_=self.run_cli('cut.evo')
        self.assertIn('STEP kesik/bozuk kayıt atlandı: #999999',(output/'RAPOR.txt').read_text())

    def test_cli_evo_default_4000_limit_in_report(self):
        sentetik.evo(self.inputs/'many.evo',count=4001)
        output,scene=self.run_cli('many.evo')
        self.assertEqual(len(scene['lights']),4000)
        self.assertIn('okunan=4001, sınır=4000, atlanan=1',(output/'RAPOR.txt').read_text())

    @unittest.skipUnless(shutil.which('assimp'),'Assimp gerekli')
    def test_evo_embedded_fbx_skips_reported_and_special_output_path(self):
        path=self.inputs/'fbx.evo'
        sentetik.evo(path)
        with zipfile.ZipFile(path,'a') as z:
            z.writestr('mesh/valid.fbx',sentetik.FBX_ASCII)
            z.writestr('mesh/broken.fbx',b'broken')
        output=self.root/'output $(touch UNWANTED)'
        cevir(path,output)
        self.assertIn('EVO FBX parçası atlandı: mesh/broken.fbx',(output/'RAPOR.txt').read_text())
        self.assertFalse((ROOT/'UNWANTED').exists())
        self.assertFalse((self.root/'UNWANTED').exists())

    @unittest.skipUnless(shutil.which('assimp'),'Assimp gerekli')
    def test_cli_assimp_formats(self):
        for name in ('ascii.stl','binary.stl','ucgen.3ds','ucgen.glb','ucgen.gltf','ucgen.dae','ascii.fbx','binary.fbx'):
            with self.subTest(name=name):
                _,scene=self.run_cli(name)
                self.assertEqual(len(scene['mesh']['f'])//3,1)

    @unittest.skipUnless(importlib.util.find_spec('pxr'),'usd-core gerekli')
    def test_cli_usda(self):
        _,scene=self.run_cli('ucgen.usda')
        self.assertEqual(len(scene['mesh']['f'])//3,1)

    @unittest.skipUnless(all(shutil.which(x,path=(motor.radiance_bul() or '')+os.pathsep+os.environ.get('PATH',''))
                            for x in ('ies2rad','xform','oconv','rtrace')),'Radiance gerekli')
    def test_cli_ies_ldt_use_real_photometry(self):
        for name in ('isik.ies','isik.ldt'):
            with self.subTest(name=name):
                output,scene=self.run_cli(name)
                self.assertEqual(scene['mesh']['f'],[])
                self.assertTrue((output/'isik/l0.dat').is_file())
                self.assertTrue((output/scene['lights'][0]['source']['ies']).is_file())
                self.assertEqual((output/'RAPOR.txt').read_text().splitlines()[:3], ['CTLux', *FOTOMETRI_NOTU])
                teslim = (output/'isik/l0.ies').read_bytes()
                self.assertEqual(ies_basligi(teslim)[-len(NOT_DOSYA)-1:], [*NOT_DOSYA, 'TILT=NONE'])
                if name == 'isik.ies' and IES2RAD:
                    self.assertIn('[TEST] Synthetic', ies_basligi(teslim))
                    self.assertEqual(ies2rad_isik(teslim), ies2rad_isik((self.inputs/name).read_bytes()))
                octree=subprocess.run(['oconv','scene.rad'],cwd=output,capture_output=True,env=motor.radiance_ortami())
                self.assertEqual(octree.returncode,0,octree.stderr)
                (output/'scene.oct').write_bytes(octree.stdout)
                traced=subprocess.run(['rtrace','-h','-I+','-ab','0','scene.oct'],cwd=output,
                    input='0 0 -2 0 0 1\n',text=True,capture_output=True,env=motor.radiance_ortami())
                self.assertEqual(traced.returncode,0,traced.stderr)
                self.assertTrue(all(float(x)>0 for x in traced.stdout.split()))

    @unittest.skipUnless(all(shutil.which(x,path=(motor.radiance_bul() or '')+os.pathsep+os.environ.get('PATH',''))
                            for x in ('oconv','rpict')),'Radiance gerekli')
    def test_exported_view_is_readable_by_rpict(self):
        output,_=self.run_cli('ucgen.obj')
        octree=subprocess.run(['oconv','scene.rad'],cwd=output,capture_output=True,env=motor.radiance_ortami())
        self.assertEqual(octree.returncode,0,octree.stderr)
        (output/'scene.oct').write_bytes(octree.stdout)
        result=subprocess.run(['rpict','-vf','gorunum.vf','-x','16','-y','12','-pa','0',
            '-av','.2','.2','.2','scene.oct'],cwd=output,capture_output=True,timeout=30,env=motor.radiance_ortami())
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn(b'FORMAT=32-bit_rle_rgbe',result.stdout)

    @unittest.skipUnless(motor.radiance_bul(),'Radiance gerekli')
    def test_cli_photometry_with_lib_only_raypath(self):
        from test_evo_identity import STEP
        with zipfile.ZipFile(self.inputs/'photo.evo','w') as z:
            z.writestr('Project/ProjectData/ProjectData.dat',STEP)
        self.env['RAYPATH']=str(Path(motor.RADIANCE_BIN).parent/'lib')
        self.env['RADIANCE_BIN']=motor.RADIANCE_BIN
        for name,count in (('isik.ies',1),('isik.ldt',1),('photo.evo',3)):
            with self.subTest(name=name):
                output,scene=self.run_cli(name)
                self.assertEqual(len(scene['lights']),count)
                self.assertEqual(len(list((output/'isik').glob('*.dat'))),count)
                self.assertNotIn('Fotometrisiz',(output/'RAPOR.txt').read_text())

    @unittest.skipUnless(os.name=='posix','POSIX sinyal testi')
    def test_cli_sigterm_cleans_assimp_and_temporary_files(self):
        self.check_cli_signal(signal.SIGTERM)

    @unittest.skipUnless(os.name=='posix','POSIX sinyal testi')
    def test_cli_sigint_cleans_assimp_without_traceback(self):
        self.check_cli_signal(signal.SIGINT)

    def check_cli_signal(self,signum):
        # süresi veri boyuna bağlı olmayan sentetik Assimp: gerçek child ve alt process.
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
            self.assertEqual(len(pids),2,'Assimp bekleme noktasına ulaşılmadı')
            self.assertTrue(list(output.glob('.ceviri-*/model/.assimp_*')))
            process.send_signal(signum)
            stdout,stderr=process.communicate(timeout=10)
            until=time.monotonic()+2
            while any(alive(pid) for pid in pids) and time.monotonic()<until:time.sleep(.01)
            self.assertFalse(any(alive(pid) for pid in pids),'Çocuk/torun süreç kaldı')
            self.assertEqual(process.returncode,128+signum,stderr)
            self.assertEqual(stderr.strip(),'İptal edildi.')
            self.assertEqual(stdout,'')
            self.assertEqual([p.name for p in output.iterdir()],['RAPOR.txt'])
            self.assertIn('Durum: iptal',(output/'RAPOR.txt').read_text())
            self.assertEqual((self.inputs/'ascii.stl').read_bytes(),before)
        finally:
            if process.poll() is None:process.kill()
            process.communicate(timeout=5)
            for pid in pids:
                if alive(pid):os.kill(pid,signal.SIGKILL)

    @unittest.skipUnless(os.name=='posix','POSIX sinyal testi')
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
                    self.assertEqual(main([str(self.inputs/'ucgen.obj'),str(output)]),128+sig)
                self.assertEqual(stderr.getvalue().strip(),'İptal edildi.')
                self.assertEqual([p.name for p in output.iterdir()],['RAPOR.txt'])
                self.assertIn('Durum: iptal',(output/'RAPOR.txt').read_text())
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
                        raise OSError('taşıma bozuldu')
                    return move(src,dst)
                stderr=io.StringIO()
                with ExitStack() as stack:
                    stack.enter_context(redirect_stderr(stderr))
                    stack.enter_context(patch.object(shutil,'move',side_effect=broken_move))
                    if cleanup_fails:
                        stack.enter_context(patch.object(Path,'unlink',side_effect=PermissionError('kilitli')))
                    self.assertEqual(main([str(self.inputs/'ucgen.obj'),str(output)]),1)
                # temizlik hatası asıl hatayı örtmez: ekranda ve raporda asıl ileti kalır.
                self.assertEqual(stderr.getvalue().strip(),'Hata: taşıma bozuldu')
                report=(output/'RAPOR.txt').read_text()
                self.assertIn('Durum: hata',report)
                self.assertIn('taşıma bozuldu',report)
                if cleanup_fails:
                    self.assertIn('Geri alınamayan sonuç dosyası: '+Path(calls[0]).name,report)
                    self.assertEqual(sorted(p.name for p in output.iterdir()),sorted(['RAPOR.txt',Path(calls[0]).name]))
                else:
                    self.assertEqual([p.name for p in output.iterdir()],['RAPOR.txt'])

    def test_bad_input_exit_and_failure_report(self):
        bad=self.inputs/'bad.ies';bad.write_text(sentetik.IES.rsplit('100 100 100',1)[0]+'100')
        out=self.root/'bad-out'
        r=subprocess.run([sys.executable,'-B','-m','core',str(bad),str(out)],cwd=ROOT,env=self.env,capture_output=True,text=True)
        self.assertNotEqual(r.returncode,0)
        self.assertIn('Hata:',r.stderr)
        self.assertIn('Durum: hata',(out/'RAPOR.txt').read_text())
        self.assertFalse(list(out.glob('.ceviri-*')))
        unsupported=self.inputs/'bad.xyz';unsupported.write_text('test')
        with self.assertRaisesRegex(ValueError,'Desteklenmeyen'):
            cevir(unsupported,self.root/'unsupported')

    def test_existing_output_and_symlink_rejected(self):
        out=self.root/'occupied';out.mkdir();(out/'keep').write_text('original')
        with self.assertRaises(FileExistsError):
            cevir(self.inputs/'ucgen.obj',out)
        self.assertEqual((out/'keep').read_text(),'original')
        link=self.root/'link';link.symlink_to(out,target_is_directory=True)
        with self.assertRaises(ValueError):
            cevir(self.inputs/'ucgen.obj',link/'nested')
        with self.assertRaises(FileNotFoundError):
            cevir(self.inputs/'ucgen.obj',self.root/'missing'/'nested')
        self.assertFalse((self.root/'missing').exists())

    def test_writes_confined_and_inputs_unchanged(self):
        sentetik.fotometri_zip(self.inputs/'paket.zip')
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
        for args in (['ucgen.obj'],['sahne.ls10'],['oda.evo'],['urunler','paket.zip']):
            out=self.root/(args[-1]+'_audit')
            r=subprocess.run([sys.executable,'-B','-c',code,*args[:-1],str(self.inputs/args[-1]),str(out)],
                             cwd=ROOT,env=self.env,capture_output=True,text=True)
            self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.inputs.iterdir()})

    def test_program_does_not_create_default_folders(self):
        # program HOME altında ya da kaynak kökünde kendiliğinden klasör açmamalı.
        home=self.root/'home';home.mkdir()
        env={k:v for k,v in self.env.items() if k not in ('CTLUX_DATA_DIR','XDG_DATA_HOME')}
        env['HOME']=str(home)
        before=sorted(p.name for p in ROOT.iterdir())
        sentetik.fotometri_zip(self.inputs/'paket.zip')
        for args in ([str(self.inputs/'oda.evo'),str(self.root/'k1')],
                     ['urunler',str(self.inputs/'paket.zip'),str(self.root/'k2')]):
            r=subprocess.run([sys.executable,'-B','-m','core',*args],cwd=ROOT,env=env,capture_output=True,text=True)
            self.assertEqual(r.returncode,0,r.stderr)
        r=subprocess.run([sys.executable,'-B','-c','import core.engine, core.format_detector, core.photometry'],
                         cwd=ROOT,env=env,capture_output=True,text=True)
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(list(home.iterdir()),[])
        self.assertEqual(sorted(p.name for p in ROOT.iterdir()),before)

    def test_radiance_writer_camera_and_disabled_light(self):
        out,scene=self.run_cli('sahne.ls10')
        scene['lights'][0]['enabled']=False
        result=sahne_yazicilar.export_scene('radiance',scene,self.root/'other')
        self.assertTrue(any('Kapalı ışık' in w for w in result['warnings']))
        self.assertNotIn('l0_m',(self.root/'other/isiklar.rad').read_text())
        with self.assertRaises(FileExistsError):
            sahne_yazicilar.export_scene('radiance',scene,self.root/'other')


class UrunlerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}

    def run_cli(self, source, code=0):
        output = self.root/(source.name+'_urunler')
        before = source.read_bytes()
        result = subprocess.run([sys.executable,'-B','-m','core','urunler',str(source),str(output)],
                                cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, code, result.stderr)
        self.assertEqual(source.read_bytes(), before)
        self.assertFalse(list(output.glob('.urunler-*')))
        for section in ('OKUNAN','KISMİ KALAN','ATLANAN','SINIRLAR'):
            self.assertIn(section, (output/'RAPOR.txt').read_text())
        return output

    def table(self, output):
        data = json.loads((output/'URUNLER.json').read_text())
        text = (output/'URUNLER.txt').read_text()
        # başlık, iki satırlık fotometri notu, girdi ve boş satırdan sonra tablo gelir.
        self.assertEqual(text.splitlines()[:3], ['CTLux ürün tablosu', *FOTOMETRI_NOTU])
        rows = text.split('\n\nREDDEDİLEN\n')[0].splitlines()[5:]
        # header ve ürün başına bir satır, reddedilenler ayrı bölümde.
        self.assertEqual(len(rows), 1+len(data['urunler']))
        for birim in ('Lümen (lm)', 'Işın açısı (°)', 'Tepe şiddet (cd)'):
            self.assertIn(birim, rows[0])
        for item, row in zip(data['urunler'], rows[1:]):
            self.assertTrue(row.endswith(item['ies']))
        for item in data['reddedilen']:
            self.assertIn('- %s: %s' % (item['ad'], item['neden'].rstrip().rstrip('.')), text)
        return data

    def assert_ies2rad_reads(self, output):
        files = sorted(output.glob('*.ies'))
        self.assertTrue(files)
        for ies in files:
            with tempfile.TemporaryDirectory() as work:
                r = subprocess.run([IES2RAD,'-o',str(Path(work)/'x'),str(ies)],capture_output=True,text=True,
                                   env=motor.radiance_ortami())
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertTrue((Path(work)/'x.rad').is_file())
                self.assertTrue((Path(work)/'x.dat').stat().st_size > 0)

    @unittest.skipUnless(IES2RAD, 'Radiance ies2rad gerekli')
    def test_evo_products_once_with_usage_and_rejections(self):
        output = self.run_cli(sentetik.urunlu_evo(self.root/'Ornek Ofis.evo'))
        self.assertEqual(sorted(p.name for p in output.iterdir()),
            ['RAPOR.txt','URUNLER.json','URUNLER.txt',
             'proje_ornek_ofis_downlight_72d_1200lm_u202.ies','proje_ornek_ofis_genis_283d_3000lm_u302.ies'])
        data = self.table(output)
        self.assertEqual([(u['ad'],u['lumen'],u['kullanim'],u['uretici']) for u in data['urunler']],
                         [('Sentetik spot A',1200,2,None),('Sentetik geniş B',3000,1,None)])
        self.assertEqual([(u['c_duzlemi'],u['gama_acisi'],u['tepe_kandela']) for u in data['urunler']],
                         [(1,6,1200),(4,3,1560)])
        self.assertAlmostEqual(data['urunler'][0]['isin_acisi'],36)
        rejected = {u['ad']:u for u in data['reddedilen']}
        self.assertEqual(set(rejected), {'Sentetik lümensiz C','Sentetik bozuk D'})
        self.assertIn('lümeni yok', rejected['Sentetik lümensiz C']['neden'])
        self.assertIn('kandela', rejected['Sentetik bozuk D']['neden'])
        self.assertEqual([u['kullanim'] for u in data['reddedilen']], [1,1])
        report = (output/'RAPOR.txt').read_text()
        self.assertIn('Durum: kısmi', report)
        self.assertIn('EVO: 6 armatür örneği, 4 ürün okundu', report)
        self.assertIn('1 EVO armatür örneğinin ürün fotometrisi yok', report)
        self.assertIn('1 1200 1 6 1', (output/'proje_ornek_ofis_downlight_72d_1200lm_u202.ies').read_text())
        self.assert_ies2rad_reads(output)

    @unittest.skipUnless(IES2RAD, 'Radiance ies2rad gerekli')
    def test_evo_ies_header_is_neutral_with_notice_and_same_light(self):
        output = self.run_cli(sentetik.urunlu_evo(self.root/'ofis.evo'))
        files = sorted(output.glob('*.ies'))
        self.assertEqual(len(files), 2)
        for ies in files:
            with self.subTest(ies=ies.name):
                veri = ies.read_bytes()
                self.assertTrue(ies.name.startswith('proje_ofis_'))
                self.assertNotIn(b'dialux', veri.lower())
                self.assertTrue(all(b < 128 for b in veri))
                self.assertTrue(all(len(x) <= 80 for x in veri.decode('ascii').splitlines()))
                self.assertEqual(ies_basligi(veri), ['IESNA:LM-63-2002', '[TEST] not available',
                    '[TESTLAB] not available', '[ISSUEDATE] not available',
                    '[MANUFAC] not read from the project record', *NOT_PROJE, 'TILT=NONE'])
                # önceki başlıkla aynı gövde ies2rad'da aynı dağılımı ve aynı ışığı verir.
                govde = veri.split(b'TILT=NONE\n', 1)[1]
                eski = b'IESNA:LM-63-2002\n[TEST] DIALux evo\n[MANUFAC] DIALux\nTILT=NONE\n' + govde
                self.assertEqual(ies2rad_isik(veri), ies2rad_isik(eski))
        report = (output/'RAPOR.txt').read_text()
        self.assertEqual(report.splitlines()[:3], ['CTLux', *FOTOMETRI_NOTU])
        self.assertNotIn('DIALux', report)

    def test_package_ies_keep_source_keywords_and_get_notice(self):
        from test_ldt_translation import LDTTest
        ldt = self.root/'tek.ldt'; ldt.write_text('\n'.join(LDTTest().fixture()))
        tek = self.root/'tek.ies'; tek.write_bytes(sentetik.urun_ies(800, (0, 90), (300, 0)).encode()
                                                   .replace(b'\n', b'\r\n'))
        for source in (sentetik.fotometri_zip(self.root/'paket.zip'), sentetik.gldf(self.root/'armatur.gldf'),
                       tek, ldt):
            output = self.run_cli(source)
            self.assertEqual((output/'RAPOR.txt').read_text().splitlines()[:3], ['CTLux', *FOTOMETRI_NOTU])
            for ies in sorted(output.glob('*.ies')):
                with self.subTest(source=source.name, ies=ies.name):
                    baslik = ies_basligi(ies.read_bytes().replace(b'\r\n', b'\n'))
                    self.assertEqual(baslik[-len(NOT_DOSYA)-1:], [*NOT_DOSYA, 'TILT=NONE'])
                    self.assertEqual(baslik[0], 'IESNA:LM-63-2002')
                    if source.suffix == '.ldt' or ies.name in ('genis.ies', 'armatur_urun.ies'):
                        self.assertIn('[LUMINAIRE] fixture', baslik)
                    else:
                        self.assertIn('[MANUFAC] Ornek Uretici', baslik)
                    if IES2RAD and source is tek:
                        # satır sonu kaynağınkine uyar, not fotometriyi değiştirmez.
                        self.assertEqual(ies.read_bytes().count(b'\n'), ies.read_bytes().count(b'\r\n'))
                        self.assertEqual(ies2rad_isik(ies.read_bytes()), ies2rad_isik(tek.read_bytes()))
        paket = self.root/'paket.zip_urunler'
        if IES2RAD:
            with zipfile.ZipFile(self.root/'paket.zip') as z:
                kaynak = z.read('Uretici/spot_a.ies')
            self.assertEqual(ies2rad_isik((paket/'spot_a_62d_1500lm.ies').read_bytes()), ies2rad_isik(kaynak))

    def test_notice_is_added_once(self):
        from core.photometry import notlu_ies
        veri = sentetik.urun_ies(800, (0, 90), (300, 0)).encode()
        bir = notlu_ies(veri)
        self.assertEqual(notlu_ies(bir), bir)
        self.assertEqual(bir.count(NOT_DOSYA[0].encode()), 1)
        self.assertEqual(notlu_ies(b'TILT satiri yok\n'), b'TILT satiri yok\n')

    def test_settings_path_has_one_rule(self):
        kod = 'from core import paths as y; print(y.AYAR_DOSYASI)'
        env = {k: v for k, v in self.env.items() if k != 'CTLUX_DATA_DIR'}
        r = subprocess.run([sys.executable,'-B','-c',kod], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(Path(r.stdout.strip()), Path(ROOT).resolve()/'ayarlar'/'ayar.json')
        env['CTLUX_DATA_DIR'] = str(self.root)
        r = subprocess.run([sys.executable,'-B','-c',kod], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(Path(r.stdout.strip()), Path(self.root).resolve()/'ayarlar'/'ayar.json')
        self.assertFalse((self.root/'ayarlar').exists())

    def test_single_input_rule(self):
        tek = self.root/'tek.ies'; tek.write_text(sentetik.urun_ies(800, (0, 90), (300, 0)))
        ikinci = self.root/'ikinci.ies'; ikinci.write_bytes(tek.read_bytes())
        klasor = self.root/'klasor'; klasor.mkdir(); shutil.copy(tek, klasor/'tek.ies')
        for komut in (['urunler'], []):
            with self.subTest(komut=komut):
                cikti = self.root/('klasor_cikti' + ''.join(komut))
                r = subprocess.run([sys.executable,'-B','-m','core',*komut,str(klasor),str(cikti)],
                                   cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=60)
                self.assertEqual(r.returncode, 1)
                self.assertIn('Desteklenmeyen girdi', r.stderr)
                self.assertFalse(cikti.exists())
                cikti = self.root/('iki_cikti' + ''.join(komut))
                r = subprocess.run([sys.executable,'-B','-m','core',*komut,str(tek),str(ikinci),str(cikti)],
                                   cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=60)
                self.assertEqual(r.returncode, 2)
                self.assertIn('unrecognized arguments', r.stderr)
                self.assertFalse(cikti.exists())
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['ikinci.ies', 'klasor', 'tek.ies'])

    def test_photometry_zip_names_lumen_rejections_and_duplicate(self):
        output = self.run_cli(sentetik.fotometri_zip(self.root/'paket.zip'))
        self.assertEqual(sorted(p.name for p in output.iterdir()),
            ['RAPOR.txt','URUNLER.json','URUNLER.txt','genis.ies','spot_a_62d_1500lm.ies'])
        data = self.table(output)
        self.assertEqual([(u['ies'],u['ad'],u['uretici'],u['lumen'],u['kullanim']) for u in data['urunler']],
            [('spot_a_62d_1500lm.ies','Paket spot','Ornek Uretici',1500,None),('genis.ies','fixture','0',2000,None)])
        rejected = {u['ad']:u['neden'] for u in data['reddedilen']}
        self.assertEqual(set(rejected), {'mutlak_180d.ies','bozuk.ies'})
        self.assertIn('lümen yok', rejected['mutlak_180d.ies'])
        self.assertIn('negatif', rejected['bozuk.ies'])
        report = (output/'RAPOR.txt').read_text()
        self.assertIn('bir kez yazıldı', report)
        self.assertIn('Durum: kısmi', report)
        if IES2RAD:
            self.assert_ies2rad_reads(output)

    def test_single_ldt_and_gldf(self):
        from test_ldt_translation import LDTTest
        ldt = self.root/'tek.ldt'; ldt.write_text('\n'.join(LDTTest().fixture()))
        for source, name in ((ldt,'tek.ies'), (sentetik.gldf(self.root/'armatur.gldf'),'armatur_urun.ies')):
            with self.subTest(source=source.name):
                output = self.run_cli(source)
                self.assertEqual(sorted(p.name for p in output.iterdir()),
                                 ['RAPOR.txt','URUNLER.json','URUNLER.txt',name])
                data = self.table(output)
                self.assertEqual([(u['ad'],u['lumen'],u['tepe_kandela']) for u in data['urunler']],
                                 [('fixture',2000,120)])
                self.assertEqual(data['reddedilen'], [])
                self.assertIn('Durum: başarılı', (output/'RAPOR.txt').read_text())
                if IES2RAD:
                    self.assert_ies2rad_reads(output)

    def test_ldt_header_source_and_unknown_cct(self):
        from test_ldt_translation import LDTTest
        for manufacturer, date, cct in (('', '', '0'), ('Société', '2026-09-28', '3000'),
                                         ('', '', '0.0'), ('', '', 'NaN'), ('', '', 'unknown')):
            with self.subTest(manufacturer=manufacturer, date=date, cct=cct):
                lines = LDTTest().fixture()
                lines[0], lines[11], lines[29] = manufacturer, date, cct
                lines[8] = 'Luminaire été'
                source, target = self.root/'header.ldt', self.root/'header.ies'
                source.write_bytes('\n'.join(lines).encode('latin-1'))
                motor.ldt_ies(source, target)
                text = target.read_text(encoding='ascii')
                header = ies_basligi(target.read_bytes())
                self.assertEqual(header[:5], ['IESNA:LM-63-2002', '[TEST] not available',
                    '[TESTLAB] not available', '[ISSUEDATE] '+(date or 'not available'),
                    '[MANUFAC] '+('Societe' if manufacturer else 'not available')])
                self.assertIn('[LUMINAIRE] Luminaire ete', header)
                self.assertIn('[_SOURCE] Converted from EULUMDAT, Isym=1', header)
                self.assertIn('[_LUMENS] 2000', header)
                self.assertEqual(header[-3:], [*NOT_DOSYA, 'TILT=NONE'])
                self.assertEqual([line for line in header if line.startswith('[LAMP]')],
                                 ['[LAMP] CCT 3000'] if cct == '3000' else [])
                self.assertTrue(all(len(line) <= 80 for line in header))
                self.assertNotIn('kaynaktan', text)

    @unittest.skipUnless(IES2RAD, 'Radiance ies2rad gerekli')
    def test_ldt_roundtrip_preserves_flux_and_ies2rad_light(self):
        from test_ldt_translation import LDTTest
        source = self.root/'roundtrip.ldt'
        source.write_text('\n'.join(LDTTest().fixture()))
        output = self.run_cli(source)
        ies = next(output.glob('*.ies'))
        second = self.run_cli(ies)
        self.assertEqual(self.table(second)['urunler'][0]['lumen'], 2000)
        self.assertEqual(self.table(second)['reddedilen'], [])
        self.assertEqual(ies.read_bytes(), next(second.glob('*.ies')).read_bytes())
        self.assert_ies2rad_reads(output)
        self.assert_ies2rad_reads(second)
        body = ies.read_bytes().split(b'TILT=NONE\n', 1)[1]
        self.assertEqual(body, b'1 -1 1 3 1 1 2 0 0 0\n1 1 10\n0 90 180\n0\n40 80 120\n')
        old = b'IESNA:LM-63-2002\n[TEST] LDT->IES cevrimi (CTLux)\nTILT=NONE\n'+body
        self.assertEqual(ies2rad_isik(ies.read_bytes()), ies2rad_isik(old))

    def test_absolute_ies_lumen_declaration_validation(self):
        for i, value in enumerate(('0', '-1', 'NaN', 'inf', '1e999', 'bad', '', '2000')):
            with self.subTest(value=value):
                source = self.root/('absolute%d.ies' % i)
                text = sentetik.urun_ies(-1, (0,90), (100,0))
                source.write_text(text.replace('TILT=NONE', '[_LUMENS] '+value+'\nTILT=NONE'))
                output = self.run_cli(source, code=0 if value == '2000' else 1)
                if value == '2000':
                    self.assertEqual(self.table(output)['urunler'][0]['lumen'], 2000)
                else:
                    self.assertIn('geçersiz [_LUMENS]', (output/'RAPOR.txt').read_text())

    def test_no_writable_product_is_error_with_report(self):
        path = self.root/'bozuk.zip'
        with zipfile.ZipFile(path,'w') as z:
            z.writestr('bozuk.ies', sentetik.urun_ies(900,(0,90),(100,-1)))
        output = self.run_cli(path, code=1)
        self.assertEqual([p.name for p in output.iterdir()], ['RAPOR.txt'])
        report = (output/'RAPOR.txt').read_text()
        # ürün dosyası kalmayan raporda fotometri notu yoktur.
        self.assertEqual(report.splitlines()[:2], ['CTLux', 'Durum: hata'])
        self.assertIn('bozuk.ies: IES kandela/çarpan negatif olamaz, IES yazılmadı', report)

    def test_output_rules_match_conversion(self):
        source = sentetik.fotometri_zip(self.root/'paket.zip')
        occupied = self.root/'occupied'; occupied.mkdir(); (occupied/'keep').write_text('original')
        with self.assertRaises(FileExistsError):
            urunler(source, occupied)
        self.assertEqual((occupied/'keep').read_text(), 'original')
        with self.assertRaisesRegex(ValueError, 'iç içe'):
            urunler(source, source/'icinde')
        unsupported = self.root/'model.obj'; unsupported.write_bytes(sentetik.OBJ)
        with self.assertRaisesRegex(ValueError, 'Desteklenmeyen'):
            urunler(unsupported, self.root/'obj-out')

    def test_delivery_failure_removes_products(self):
        from core.__main__ import main
        from contextlib import redirect_stderr
        import io
        source = sentetik.fotometri_zip(self.root/'paket.zip')
        output = self.root/'teslim-hatasi'
        move = shutil.move
        calls = []
        def broken_move(src, dst):
            calls.append(dst)
            if len(calls) == 2:
                raise OSError('taşıma bozuldu')
            return move(src, dst)
        stderr = io.StringIO()
        with patch.object(shutil,'move',side_effect=broken_move),redirect_stderr(stderr):
            self.assertEqual(main(['urunler',str(source),str(output)]), 1)
        self.assertEqual(stderr.getvalue().strip(), 'Hata: taşıma bozuldu')
        self.assertEqual([p.name for p in output.iterdir()], ['RAPOR.txt'])
        report = (output/'RAPOR.txt').read_text()
        self.assertIn('Durum: hata', report)
        self.assertIn('taşıma bozuldu', report)

    def gldf_bytes(self, *members):
        import io
        data = io.BytesIO()
        with zipfile.ZipFile(data,'w') as archive:
            archive.writestr('product.xml', '<Root/>')
            for name, text in members:
                archive.writestr(name, text)
        return data.getvalue()

    def spot(self, lumen, ad='Sentetik spot'):
        return sentetik.urun_ies(lumen, (0,10,20,30,90), (2000,1500,600,50,0), ad=ad)

    def test_same_named_gldf_in_zip(self):
        farkli = self.root/'farkli.zip'
        with zipfile.ZipFile(farkli,'w') as z:
            z.writestr('a/same.gldf', self.gldf_bytes(('ldc/urun.ies', self.spot(1000))))
            z.writestr('b/same.gldf', self.gldf_bytes(('ldc/urun.ies', self.spot(2000))))
        output = self.run_cli(farkli)
        self.assertEqual(sorted(p.name for p in output.iterdir()),
                         ['RAPOR.txt','URUNLER.json','URUNLER.txt','same_urun.ies','same_urun_2.ies'])
        data = self.table(output)
        self.assertEqual([(u['ies'],u['lumen']) for u in data['urunler']],
                         [('same_urun.ies',1000),('same_urun_2.ies',2000)])
        self.assertIn('2 fotometri dosyası bulundu', (output/'RAPOR.txt').read_text())
        ayni = self.root/'ayni.zip'
        with zipfile.ZipFile(ayni,'w') as z:
            for folder in ('a','b'):
                z.writestr(folder+'/same.gldf', self.gldf_bytes(('ldc/urun.ies', self.spot(1000))))
        output = self.run_cli(ayni)
        self.assertEqual(sorted(p.name for p in output.iterdir()),
                         ['RAPOR.txt','URUNLER.json','URUNLER.txt','same_urun.ies'])
        self.assertEqual([u['lumen'] for u in self.table(output)['urunler']], [1000])
        report = (output/'RAPOR.txt').read_text()
        self.assertIn('same_urun_2.ies: same_urun.ies ile aynı içerik, bir kez yazıldı', report)
        self.assertIn('Durum: kısmi', report)

    def test_same_base_name_members_in_one_gldf(self):
        source = self.root/'armatur.gldf'
        source.write_bytes(self.gldf_bytes(('ldc/a/urun.ies', self.spot(1000)), ('ldc/b/urun.ies', self.spot(2000))))
        output = self.run_cli(source)
        data = self.table(output)
        self.assertEqual([(u['ies'],u['lumen']) for u in data['urunler']],
                         [('armatur_urun.ies',1000),('armatur_urun_2.ies',2000)])

    def test_same_named_ies_in_zip(self):
        # iki üyenin künyesi de aynı çıkar: ad, içerik değil, içerik LUMINAIRE satırında ayrılır.
        source = self.root/'ies.zip'
        with zipfile.ZipFile(source,'w') as z:
            z.writestr('a/x.ies', self.spot(1500, 'Spot A'))
            z.writestr('b/x.ies', self.spot(1500, 'Spot B'))
        output = self.run_cli(source)
        data = self.table(output)
        self.assertEqual([(u['ies'],u['ad']) for u in data['urunler']],
                         [('x_62d_1500lm.ies','Spot A'),('x_62d_1500lm_2.ies','Spot B')])

    def test_extraction_never_overwrites_existing_file(self):
        target = self.root/'hedef'; target.mkdir()
        (target/'paket_urun.ies').write_text('onceki')
        (target/'x_62d_1500lm.ies').write_text('onceki')
        gldf = self.root/'paket.gldf'
        gldf.write_bytes(self.gldf_bytes(('ldc/urun.ies', self.spot(1000))))
        self.assertEqual([Path(p).name for p in ice_aktarma.gldf_ac(str(gldf), str(target))], ['paket_urun_2.ies'])
        source = self.root/'x.zip'
        with zipfile.ZipFile(source,'w') as z:
            z.writestr('x.ies', self.spot(1500))
        self.assertEqual(ice_aktarma.zip_kutuphane(str(source), str(target)), ['x_62d_1500lm_2.ies'])
        self.assertEqual((target/'paket_urun.ies').read_text(), 'onceki')
        self.assertEqual((target/'x_62d_1500lm.ies').read_text(), 'onceki')
        self.assertEqual(sorted(p.name for p in target.iterdir()),
                         ['paket_urun.ies','paket_urun_2.ies','x_62d_1500lm.ies','x_62d_1500lm_2.ies'])

    @unittest.skipUnless(os.name=='posix','POSIX sinyal testi')
    def test_cancel_during_delivery_removes_products(self):
        from core.__main__ import main
        from contextlib import redirect_stderr
        import io
        source = sentetik.fotometri_zip(self.root/'paket.zip')
        move = shutil.move
        handlers = {sig:signal.getsignal(sig) for sig in (signal.SIGINT,signal.SIGTERM)}
        for sig in handlers:
            with self.subTest(signal=sig):
                output = self.root/('iptal-'+str(sig))
                def interrupted_move(src, dst):
                    move(src, dst)
                    os.kill(os.getpid(), sig)
                stderr = io.StringIO()
                with patch.object(shutil,'move',side_effect=interrupted_move),redirect_stderr(stderr):
                    self.assertEqual(main(['urunler',str(source),str(output)]), 128+sig)
                self.assertEqual(stderr.getvalue().strip(), 'İptal edildi.')
                self.assertEqual([p.name for p in output.iterdir()], ['RAPOR.txt'])
                self.assertIn('Durum: iptal', (output/'RAPOR.txt').read_text())
                self.assertEqual({s:signal.getsignal(s) for s in handlers}, handlers)


class EnglishCLITest(unittest.TestCase):
    def test_conversion_limit_flags_reach_backend(self):
        from core import __main__ as cli
        for flags in (['--source-limit-mib', '2048', '--triangle-limit', '6000000'],
                      ['--kaynak-siniri-mib', '2048', '--ucgen-siniri', '6000000']):
            with self.subTest(flags=flags), patch.object(cli, 'cevir', return_value={'output': 'out'}) as convert:
                self.assertEqual(cli.main(['input.obj', 'out', *flags]), 0)
                convert.assert_called_once_with('input.obj', 'out', kaynak_siniri_mib=2048, ucgen_siniri=6000000)

    def test_invalid_limits_do_not_call_backend(self):
        from core import __main__ as cli
        for flag, value in (('--source-limit-mib', '0'), ('--triangle-limit', '-1')):
            with self.subTest(flag=flag), patch.object(cli, 'cevir') as convert:
                with self.assertRaises(SystemExit) as error:
                    cli.main(['input.obj', 'out', flag, value])
                self.assertEqual(error.exception.code, 2)
                convert.assert_not_called()

    def test_products_command_writes_real_photometry(self):
        with tempfile.TemporaryDirectory() as work:
            work = Path(work)
            source = sentetik.fotometri_zip(work/'package.zip')
            for command in ('products', 'urunler'):
                output = work/command
                result = subprocess.run([sys.executable, '-B', '-m', 'core', command, str(source), str(output)],
                                        cwd=ROOT, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                products = json.loads((output/'URUNLER.json').read_text())
                self.assertTrue(products['urunler'])
                self.assertTrue(list(output.glob('*.ies')))
                self.assertTrue((output/'RAPOR.txt').is_file())


class YolVeRaporRegresyonTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def test_evo_kismi_notlari_okunan_bolumune_girmez(self):
        from core import format_detector as dedektif
        source = sentetik.evo(self.root/'oda.evo')
        with zipfile.ZipFile(source, 'a') as z:
            z.writestr('mesh/detail.m3d', b'')
        project = self.root/'proje'; (project/'model').mkdir(parents=True)
        read, partial = [], []
        dedektif._evo_yerlestir(str(source), str(project), str(project/'model/girdi.obj'), read, partial)
        self.assertTrue(any('EVO gömülü .m3d' in x for x in partial))
        self.assertFalse(set(read) & set(partial))
        output = self.root/'out'
        cevir(source, output)
        report = (output/'RAPOR.txt').read_text()
        read_section = report.split('\nOKUNAN\n', 1)[1].split('\nKISMİ KALAN / VARSAYILAN\n', 1)[0]
        self.assertNotIn('EVO gömülü .m3d', read_section)
        self.assertEqual(report.count('EVO gömülü .m3d'), 1)

    def test_dotdot_ile_sahneye_giren_cikti_ic_ice(self):
        from core.__main__ import _cikti_hazirla
        scene = self.root/'a/sahne'; scene.mkdir(parents=True); (self.root/'a/x').mkdir()
        with self.assertRaisesRegex(ValueError, 'iç içe'):
            _cikti_hazirla(scene, str(self.root/'a/x') + '/../sahne/out', None)
        self.assertFalse((scene/'out').exists())
        _, output = _cikti_hazirla(scene, str(self.root/'a/x') + '/../sonuc', None)
        self.assertEqual(output, self.root/'a/sonuc')
        self.assertTrue(output.is_dir())

    def test_iki_nokta_windows_disinda_dosya_adi_karakteri(self):
        from core.file_safety import ic_yol
        for rel in ('C:x', 'c:/x', 'C:\\x', '//server/share/x', '\\\\server\\share\\x'):
            with self.assertRaises(ValueError):
                ic_yol(self.root, rel)
        if os.name == 'nt':
            with self.assertRaises(ValueError):
                ic_yol(self.root, 'a.obj:stream')
            return
        source = self.root/'tarama 12:30.obj'; source.write_bytes(sentetik.OBJ)
        self.assertEqual(ic_yol(self.root, source.name), str(source))
        cevir(source, self.root/'out')
        self.assertTrue((self.root/'out/scene.rad').is_file())

    @unittest.skipIf(os.name == 'nt', 'POSIX dosya izinleri')
    def test_atomik_json_dosya_iznini_korur(self):
        from core.file_safety import atomik_json
        path = self.root/'proje.json'; path.write_text('{}'); os.chmod(path, 0o640)
        atomik_json(path, {'x': 1})
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o640)
        self.assertEqual(json.loads(path.read_text()), {'x': 1})
        umask = os.umask(0o022); os.umask(umask)
        atomik_json(self.root/'yeni.json', {})
        self.assertEqual(os.stat(self.root/'yeni.json').st_mode & 0o777, 0o666 & ~umask)


if __name__=='__main__':
    unittest.main()
