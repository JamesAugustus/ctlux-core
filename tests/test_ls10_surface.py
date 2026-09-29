# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
import importlib.util,unittest,struct,tempfile,json,hashlib
from pathlib import Path
from unittest.mock import patch
MODULE=Path(__file__).resolve().parents[1]/'core/tools/ls10_surface.py'
spec=importlib.util.spec_from_file_location('lumion_surface',MODULE);L=importlib.util.module_from_spec(spec);spec.loader.exec_module(L)
IDENTITY=[1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1]
def chunk(tag, payload):
 return tag+struct.pack('<I',len(payload))+payload

def packed_chunk(tag, fmt, values):
 return chunk(tag,struct.pack('<'+fmt*len(values),*values))

def schema_bytes(names):
 return (b'ICSI'+packed_chunk(b'INIC','I',[len(names)])+
         b''.join(b'INIT'+chunk(b'IINW',name.encode('utf-16le'))+b'IIIS'+chunk(b'IIOM',b'') for name in names)+b'ICIF')

def channel_bytes(name, payload, links=()):
 return (packed_chunk(b'CHIT','I',[1])+chunk(b'CHNW',name.encode('utf-16le'))+
         packed_chunk(b'CHIT','I',[1])+packed_chunk(b'CHLC','I',[len(links)])+
         b''.join(packed_chunk(b'CHLI','I',[li])+packed_chunk(b'CHUL','I',[ul]) for li,ul in links)+payload)

def property_bytes(payload):
 return b'IFSI'+chunk(b'IIOM',b'')+payload+b'ENDL'

def instance_bytes(names=(), values=None, children=None, kind=L.SURFACE, schema=None):
 if values is None:values=[b'IFNS']*len(names)
 if children is None:children=[b'IFNS']*len(names)
 return (chunk(b'ICUD',bytes(32))+packed_chunk(b'ICIC','I',[1])+chunk(b'ICTD',kind)+
         (schema_bytes(names) if schema is None else schema)+b''.join(values)+b''.join(children)+b'ENDI^EN^')

def documented_example():
 buffers=b''.join(packed_chunk(tag,fmt,values) for tag,fmt,values in [
  (b'VRCO','I',[3]),(b'VPPI','f',[0,0,0,1,0,0,0,1,0]),(b'VNNI','h',[0,0,32767]*3),
  (b'VTD0','f',[0,0,1,0,0,1]),(b'VTD1','f',[0,0,1,0,0,1]),(b'PO32','I',[0,1,2])])
 return instance_bytes(['a','g'],[property_bytes(packed_chunk(b'IIV1','f',[.5,.25,.125,1])),b'IFNS'],
                       [b'IFNS',b'IFSICHAC'+channel_bytes('Vertex Data',buffers,[(0,0)])],kind=bytes([0x11])*16)

class Fixture(L.Reader):
 def __init__(self,bad_index=False,nan=False,uv1_value=None,omit=None,short=None):
  buffers=packed_chunk(b'VRCO','I',[4])
  for name,fmt,values in [('VPPI','f',[0,0,0,1,0,0,0,1,0,0,0,0]),('VTD0','f',[0,0,1,0,0,1,1,1]),('VNNI','h',[0,0,32767]*3+[0,32767,0]),('VTD1','f',[.5]*8),('VTO0','f',[0,0,0,0,1,1]),('VTO1','f',[0,0,0,0,1,1]),('PO32','I',[0,1,2,3,1,99 if bad_index else 2])]:
   if nan and name=='VPPI':values[0]=float('nan')
   if uv1_value is not None and name=='VTD1':values[-1]=uv1_value
   if name==omit:continue
   if name==short:values=values[:-1]
   buffers+=packed_chunk(name.encode('ascii'),fmt,values)
  values=[property_bytes(packed_chunk(b'IIVE','f',[0])),
          property_bytes(channel_bytes('Text',chunk(b'STWA','test'.encode('utf-16le'))+chunk(b'STLR',b''))),
          property_bytes(packed_chunk(b'IIV1','f',[.2,.4,.6,1])),b'IFNS']
  children=[b'IFNS']*3+[b'IFSICHAC'+channel_bytes('Vertex Data',buffers)]
  super().__init__(instance_bytes(['surfaceNr','surfaceName','Diffuse','ObjectData'],values,children))
  self.surface=self.surfaces()[0]
class Tests(unittest.TestCase):
 def test_negative_chunk_offset(self):
  reader=L.Reader(chunk(b'TEST',b'data'))
  for offset in (-1,-4,-8,-len(reader.m),-len(reader.m)-1):
   with self.subTest(offset=offset),self.assertRaisesRegex(L.FormatError,'Negative chunk offset'):
    reader.chunk(offset)
  parsed,end=reader.chunk(0,b'TEST')
  self.assertEqual(reader.data(parsed),b'data');self.assertEqual(end,len(reader.m))
 def test_class_type_identifier_size(self):
  for size in (0,15,16,17,4096,4097):
   with self.subTest(size=size):
    raw=instance_bytes(kind=bytes([0x11])*size);reader=L.Reader(raw)
    if size==16:
     item,end=reader.instance(0)
     self.assertEqual(item['type'],'11'*16);self.assertEqual(end,len(raw))
    else:
     with self.assertRaises(L.FormatError):reader.instance(0)
 def test_property_channel_must_be_text(self):
  text='field text α'
  channels=[('Text',chunk(b'STWA',text.encode('utf-16le'))+chunk(b'STLR',b'')),
            ('OO Class Instances List',chunk(b'ICSD',b'')+packed_chunk(b'ICLL','I',[0])),
            ('OO ClassInstance',b'ISP2'+chunk(b'ICSD',b'')+b'NOIS')]
  for kind,payload in channels:
   with self.subTest(kind=kind):
    raw=instance_bytes(['a'],[property_bytes(channel_bytes(kind,payload))]);reader=L.Reader(raw)
    if kind=='Text':
     item,end=reader.instance(0)
     self.assertEqual(item['fields']['a'],text);self.assertEqual(end,len(raw))
    else:
     with self.assertRaisesRegex(L.FormatError,'Property channel must be Text'):reader.instance(0)
 def test_field_descriptor_chunk_limit(self):
  for count in (1,11,12,13):
   for name_last in (False,True):
    with self.subTest(count=count,name_last=name_last):
     parts=[chunk(b'META',b'')]*(count-1)
     parts.insert(len(parts) if name_last else 0,chunk(b'IINW','a'.encode('utf-16le')))
     schema=b'ICSI'+packed_chunk(b'INIC','I',[1])+b'INIT'+b''.join(parts)+b'IIIS'+chunk(b'IIOM',b'')+b'ICIF'
     raw=instance_bytes(['a'],schema=schema);reader=L.Reader(raw)
     if count<=12:
      item,end=reader.instance(0)
      self.assertEqual(item['fields'],{'a':None});self.assertEqual(end,len(raw))
     else:
      with self.assertRaisesRegex(L.FormatError,'Field descriptor limit'):reader.instance(0)
 def test_field_descriptor_metadata_limit(self):
  for tag in (b'IINW',b'META'):
   for size in (4096,4097):
    with self.subTest(tag=tag,size=size):
     large=chunk(tag,b'a\0'+bytes(size-2))
     parts=large if tag==b'IINW' else chunk(b'IINW',b'a\0')+large
     schema=b'ICSI'+packed_chunk(b'INIC','I',[1])+b'INIT'+parts+b'IIIS'+chunk(b'IIOM',b'')+b'ICIF'
     raw=instance_bytes(['a'],schema=schema);reader=L.Reader(raw)
     if size==4096:
      item,end=reader.instance(0)
      self.assertEqual(item['fields'],{'a':None});self.assertEqual(end,len(raw))
     else:
      with self.assertRaisesRegex(L.FormatError,'Metadata too large'):reader.instance(0)
 def test_documented_synthetic_instance(self):
  raw=documented_example()
  self.assertEqual(len(raw),468)
  self.assertEqual(hashlib.sha256(raw).hexdigest(),'5182482876d8004c734360c8b6d196e5ddef7265f56fcb3393b5fc722145465b')
  item,end=L.Reader(raw).instance(0)
  self.assertEqual(end,468);self.assertEqual(item['fields'],{'a':[.5,.25,.125,1.0],'g':None})
  self.assertIsNone(item['children']['a'])
  geo=item['children']['g'];self.assertEqual(geo['kind'],'Vertex Data')
  self.assertEqual(geo['vertices'],3);self.assertEqual(geo['links'],[(0,0)])
  self.assertEqual({tag:data['length'] for tag,data in geo['buffers'].items()},
                   {'VRCO':4,'VPPI':36,'VNNI':18,'VTD0':24,'VTD1':24,'PO32':12})
 def test_documented_synthetic_mutations(self):
  raw=documented_example()
  bad_length=raw[:4]+struct.pack('<I',468)+raw[8:]
  short_vertices=raw[:310]+struct.pack('<I',24)+raw[314:338]+raw[350:]
  for data,message in [(bad_length,'Chunk length outside source'),(raw[:-4],"expected b'\\^EN\\^'"),
                       (short_vertices,'Vertex attribute count mismatch')]:
   with self.subTest(message=message),self.assertRaisesRegex(L.FormatError,message):L.Reader(data).instance(0)
 def test_uv1_nonfinite_keeps_existing_target(self):
  for value in (float('nan'),float('inf'),float('-inf')):
   with self.subTest(value=value),tempfile.TemporaryDirectory() as d:
    p=Path(d)/'a.obj';p.write_text('original')
    with self.assertRaisesRegex(L.FormatError,'Nonfinite UV1 attribute'):L.export(Fixture(uv1_value=value),p,IDENTITY)
    self.assertEqual(p.read_text(),'original');self.assertEqual(list(Path(d).iterdir()),[p])
 def test_uv_and_hard_normal_seams(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'a.obj';r=L.export(Fixture(),p,IDENTITY);lines=p.read_text().splitlines();faces=[x.split()[1:] for x in lines if x.startswith('f ')]
   self.assertEqual(faces[0][0].split('/')[0],faces[1][0].split('/')[0]);self.assertNotEqual(faces[0][0].split('/')[1:],faces[1][0].split('/')[1:]);self.assertEqual(r['triangle'],2)
   manifest=json.loads(Path(r['manifest']).read_text());uv=Path(r['manifest']).parent/manifest['surfaces'][0]['uv1_file'];self.assertEqual(uv.read_bytes(),struct.pack('<8f',*[.5]*8))
 def test_uv_transform_partial_and_nonfinite(self):
  for value in [2.0,float('nan')]:
   fixture=Fixture();offset=fixture.surface['children']['ObjectData']['buffers']['VTO0']['offset']
   data=bytearray(fixture.m);struct.pack_into('<f',data,offset,value);fixture.m=bytes(data)
   with tempfile.TemporaryDirectory() as d:
    p=Path(d)/'a.obj'
    if value==2.0:
     result=L.export(fixture,p,IDENTITY)
     manifest=json.loads(Path(result['manifest']).read_text())
     self.assertTrue(any('VTO0' in warning and 'not baked' in warning for warning in manifest['warnings']))
    else:
     p.write_text('original')
     with self.assertRaises(L.FormatError):L.export(fixture,p,IDENTITY)
     self.assertEqual(p.read_text(),'original')
 def test_unicode_target_name(self):
  # Accented and Greek characters plus spaces exercise Unicode output paths.
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'Café Center color α.obj';r=L.export(Fixture(),p,IDENTITY)
   self.assertTrue(p.exists());p.read_text(encoding='ascii');self.assertTrue(Path(r['manifest']).exists())
 def test_different_sources_have_distinct_material_names(self):
  with tempfile.TemporaryDirectory() as d:
   a=Fixture();b=Fixture();b.m+=b'different source'
   ra=L.export(a,Path(d)/'a.obj',IDENTITY);rb=L.export(b,Path(d)/'b.obj',IDENTITY)
   self.assertNotEqual(ra['surfaces'][0]['material'],rb['surfaces'][0]['material'])
 def test_invalid_index_keeps_existing_target(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'a.obj';p.write_text('original')
   with self.assertRaises(L.FormatError):L.export(Fixture(bad_index=True),p,IDENTITY)
   self.assertEqual(p.read_text(),'original');self.assertEqual(list(Path(d).iterdir()),[p])
 def test_nonfinite_rejected(self):
  with tempfile.TemporaryDirectory() as d:
   with self.assertRaises(L.FormatError):L.export(Fixture(nan=True),Path(d)/'a.obj',IDENTITY)
 def test_bundle_rolls_back_when_obj_publish_fails(self):
  import os
  real=os.replace
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'a.obj';p.write_text('original')
   def replace(a,b):
    if Path(b)==p:raise OSError('injected publication failure')
    return real(a,b)
   with patch('os.replace',replace),self.assertRaises(OSError):L.export(Fixture(),p,IDENTITY)
   self.assertEqual(list(Path(d).iterdir()),[p]);self.assertEqual(p.read_text(),'original')
 def test_inverse_transpose(self):
  # A=[2,1,0,0,3,0,0,0,4]. Tangents transform by A. The normal must remain perpendicular.
  mat=[2,0,0,0,1,3,0,0,0,0,4,0,0,0,0,1];pos,norm,mirror=L.transform_matrix(mat)
  n=norm([1,1,1]);t=pos([1,-1,0]);self.assertAlmostEqual(sum(a*b for a,b in zip(n,t)),0);self.assertFalse(mirror)
 def test_mirror_winding(self):
  mat=IDENTITY.copy();mat[0]=-1
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'a.obj';L.export(Fixture(),p,mat);f=next(x for x in p.read_text().splitlines() if x.startswith('f '));self.assertEqual([int(x.split('/')[0]) for x in f.split()[1:]],[1,3,2])
 def test_invalid_matrices(self):
  for mat in [None,[0]*16,[float('nan')]*16]:
   with self.assertRaises(L.FormatError):L.transform_matrix(mat)
 def test_chunk_bounds(self):
  for raw in [b'X',b'VPPI'+struct.pack('<I',200)+b'xx']:
   with self.assertRaises(L.FormatError):L.Reader(raw).chunk(0)
 def test_override_color_w_not_opacity(self):
  override={'test':{'offset':123,'children':{'materialInfo':{'item':{'fields':{'color':[.7,.2,.1,0]}}}}}}
  with tempfile.TemporaryDirectory() as d,patch.object(L,'material_overrides',return_value=override):
   p=Path(d)/'a.obj';r=L.export(Fixture(),p,IDENTITY);mtl=(Path(r['manifest']).parent/'materials.mtl').read_text();self.assertIn('d 1\n',mtl);self.assertEqual(r['surfaces'][0]['display_color'],[.7,.2,.1])
 def test_missing_or_short_uv_transform_is_format_error(self):
  # VTO0/VTO1 are optional for the parser, the exporter rejects cleanly and keeps the target.
  for tag in ('VTO0','VTO1'):
   for kind,message in (('omit','Missing UV transform'),('short','UV transform size')):
    with self.subTest(tag=tag,kind=kind),tempfile.TemporaryDirectory() as d:
     fixture=Fixture(**{kind:tag});self.assertEqual(fixture.surface['children']['ObjectData']['vertices'],4)
     p=Path(d)/'a.obj';p.write_text('original')
     with self.assertRaisesRegex(L.FormatError,message+': '+tag):L.export(fixture,p,IDENTITY)
     self.assertEqual(p.read_text(),'original');self.assertEqual(list(Path(d).iterdir()),[p])
 def test_scalar_field_sizes_are_format_errors(self):
  vertex=lambda count:(chunk(b'VRCO',count)+b''.join(chunk(t,b'') for t in (b'VPPI',b'VNNI',b'VTD0',b'VTD1',b'PO32')))
  node,_=L.Reader(channel_bytes('Vertex Data',vertex(struct.pack('<I',0)))).channel(0);self.assertEqual(node['vertices'],0)
  for size in (0,2,8):
   with self.subTest(size=size),self.assertRaisesRegex(L.FormatError,'Vertex count size'):
    L.Reader(channel_bytes('Vertex Data',vertex(bytes(size)))).channel(0)
  fields={'TEXW':struct.pack('<I',1),'TEXH':struct.pack('<I',1),'TEXS':struct.pack('<I',3)}
  texture=lambda f:channel_bytes('Texture',b''.join(chunk(t.encode(),v) for t,v in f.items())+chunk(b'TEXT',b'abc'))
  node,_=L.Reader(texture(fields)).channel(0);self.assertEqual(node['kind'],'Texture')
  for tag in fields:
   for size in (0,3,8):
    with self.subTest(tag=tag,size=size),self.assertRaisesRegex(L.FormatError,'Texture field size'):
     L.Reader(texture({**fields,tag:bytes(size)})).channel(0)
if __name__=='__main__':unittest.main()
