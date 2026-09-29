# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""LS10 içine gömülü surface instance/channel kayıtları için katı reader.

Offset'ler provenance için saklanır. Bilinmeyen şemada tahmin yürütmez, hata verir.
"""
import struct,re,math
SURFACE=bytes.fromhex('fd1c729ffb00b74ea24200c2ba2db89b')
INSTANCE=re.compile(rb'ICUD\x20\x00\x00\x00.{32}ICIC\x04\x00\x00\x00.{4}ICTD\x10\x00\x00\x00'+re.escape(SURFACE),re.S)
class FormatError(ValueError):pass
class Reader:
 def __init__(self,m):self.m=m
 def expect(self,p,t):
  if self.m[p:p+len(t)]!=t:raise FormatError(f'{p}: expected {t!r}, got {self.m[p:p+16]!r}')
  return p+len(t)
 def chunk(self,p,tag=None):
  if p<0:raise FormatError('Negative chunk offset')
  if p+8>len(self.m):raise FormatError('Truncated chunk')
  t=self.m[p:p+4];n=struct.unpack_from('<I',self.m,p+4)[0]
  if tag is not None and t!=tag:raise FormatError(f'{p}: expected {tag!r}, got {t!r}')
  if n>len(self.m)-p-8:raise FormatError('Chunk length outside source')
  return {'tag':t,'offset':p+8,'length':n},p+8+n
 def data(self,c):return self.m[c['offset']:c['offset']+c['length']]
 def small(self,p,tag):
  c,p=self.chunk(p,tag)
  if c['length']>4096:raise FormatError('Metadata too large')
  return self.data(c),p
 def uint(self,p,tag):
  d,p=self.small(p,tag)
  if len(d)!=4:raise FormatError('uint size')
  return struct.unpack('<I',d)[0],p
 def schema(self,p):
  p=self.expect(p,b'ICSI');n,p=self.uint(p,b'INIC')
  if n>128:raise FormatError('Too many fields')
  names=[]
  for _ in range(n):
   p=self.expect(p,b'INIT');name=None
   for _ in range(12):
    if self.m[p:p+4]==b'IIIS':break
    c,p=self.chunk(p)
    if c['length']>4096:raise FormatError('Metadata too large')
    if c['tag']==b'IINW':name=self.data(c).decode('utf-16le').split('\x00')[0]
   if self.m[p:p+4]!=b'IIIS':raise FormatError('Field descriptor limit')
   p+=4
   if name is None or name in names:raise FormatError('Missing/duplicate field')
   names.append(name);_,p=self.small(p,b'IIOM')
   # descriptor'dan sonra opsiyonel default type identifier gelebilir.
   if self.m[p:p+4]==b'IIIT':_,p=self.small(p,b'IIIT')
  return names,self.expect(p,b'ICIF')
 def value(self,p):
  if self.m[p:p+4]==b'IFNS':return None,p+4
  p=self.expect(p,b'IFSI');_,p=self.small(p,b'IIOM');t=self.m[p:p+4]
  if t in (b'IIVE',b'IIV1',b'IIM1'):
   raw,p=self.small(p,t);n={b'IIVE':1,b'IIV1':4,b'IIM1':16}[t]
   if len(raw)!=n*4:raise FormatError('Numeric field size')
   v=list(struct.unpack('<'+'f'*n,raw))
   if not all(math.isfinite(x) for x in v):raise FormatError('Nonfinite property')
   if n==1:v=v[0]
  elif t==b'CHIT':
   node,p=self.channel(p)
   if node['kind']!='Text':raise FormatError('Property channel must be Text')
   v=node['text']
  else:
   if p+16>len(self.m):raise FormatError('Type identifier truncated')
   v={'type_ref':self.m[p:p+16].hex()};p+=16
  return v,self.expect(p,b'ENDL')
 def channel(self,p,depth=0):
  if depth>12:raise FormatError('Nesting limit')
  if self.m[p:p+4]==b'SKDA':return {'kind':'empty'},p+4
  start=p;_,p=self.uint(p,b'CHIT');name,p=self.small(p,b'CHNW');name=name.decode('utf-16le').rstrip('\x00');_,p=self.uint(p,b'CHIT');n,p=self.uint(p,b'CHLC')
  if n>64:raise FormatError('Channel link limit')
  links=[]
  for _ in range(n):
   li,p=self.uint(p,b'CHLI');ul,p=self.uint(p,b'CHUL');links.append((li,ul))
  node={'kind':name,'offset':start,'links':links}
  if name=='Text':
   raw,p=self.small(p,b'STWA');node['text']=raw.decode('utf-16le');_,p=self.small(p,b'STLR');return node,p
  if name=='OO Class Instances List':
   _,p=self.small(p,b'ICSD');n,p=self.uint(p,b'ICLL')
   if n>64:raise FormatError('List limit')
   node['items']=[]
   for _ in range(n):item,p=self.instance(p,depth+1);node['items'].append(item)
   return node,p
  if name=='OO ClassInstance' or name.startswith('ClassInstance->'):
   p=self.expect(p,b'ISP2');_,p=self.small(p,b'ICSD')
   if self.m[p:p+4]==b'NOIS':return node,p+4
   item,p=self.instance(p,depth+1,terminated=False);node['item']=item;return node,p
  if name=='Vertex Data':
   tags={};allowed={b'VRCO',b'VPPI',b'VNNI',b'VTD0',b'VTO0',b'VTD1',b'VTO1',b'VTTB',b'MBCT',b'POCO',b'PO32',b'POTY',b'PONM',b'POTT',b'POSO',b'PUAV',b'PSRV'}
   while self.m[p:p+4] in allowed:
    c,p=self.chunk(p);t=c['tag'].decode()
    if t in tags:raise FormatError('Duplicate vertex tag')
    c.pop('tag');tags[t]=c
   node['buffers']=tags
   for t in ['VRCO','VPPI','VNNI','VTD0','VTD1','PO32']:
    if t not in tags:raise FormatError('Missing '+t)
   if tags['VRCO']['length']!=4:raise FormatError('Vertex count size')
   vc=struct.unpack('<I',self.data(tags['VRCO']))[0]
   if any(tags[t]['length']!=vc*k for t,k in [('VPPI',12),('VNNI',6),('VTD0',8),('VTD1',8)]):raise FormatError('Vertex attribute count mismatch')
   if tags['PO32']['length']%12:raise FormatError('Nontriangle index buffer')
   node['vertices']=vc;return node,p
  if name=='3D ObjectData':
   vc,p=self.uint(p,b'VRCO')
   if vc!=0:raise FormatError('Unresolved referenced geometry')
   for tag in (b'MBCT',b'POCO',b'POTY',b'PONM',b'POTT',b'POSO',b'PUAV',b'PSRV'):
    _,p=self.small(p,tag)
   return node,p
  if name=='Texture':
   tags={};allowed={b'TEXM',b'TEXW',b'TEXH',b'TEPU',b'TEXS',b'TEXT',b'TENT',b'TECM',b'TEGC',b'TEOW',b'TELM',b'RWEN'}
   while self.m[p:p+4] in allowed:
    c,p=self.chunk(p);key=c.pop('tag').decode()
    if key in tags:raise FormatError('Duplicate texture field')
    tags[key]=c
   if any(t not in tags for t in ['TEXW','TEXH','TEXS','TEXT']):raise FormatError('Texture fields missing')
   if any(tags[t]['length']!=4 for t in ['TEXW','TEXH','TEXS']):raise FormatError('Texture field size')
   if struct.unpack('<I',self.data(tags['TEXS']))[0]!=tags['TEXT']['length']:raise FormatError('Texture byte size')
   node['buffers']=tags;return node,p
  raise FormatError(f'Unsupported channel {name!r} at {start}')
 def instance(self,p,depth=0,terminated=True):
  if depth>16:raise FormatError('Instance nesting limit')
  start=p;_,p=self.small(p,b'ICUD');count,p=self.uint(p,b'ICIC')
  if not 1<=count<=16:raise FormatError('Invalid class section count')
  fields={};children={};sections=[]
  for _ in range(count):
   kind,p=self.small(p,b'ICTD')
   if len(kind)!=16:raise FormatError('Class type identifier size')
   names,p=self.schema(p);vals=[];sub={}
   for _ in names:v,p=self.value(p);vals.append(v)
   for name in names:
    if self.m[p:p+4]==b'IFNS':p+=4;sub[name]=None;continue
    p=self.expect(p,b'IFSI');t=self.m[p:p+4];p+=4
    if t==b'OCHA':sub[name]=None
    elif t==b'CHAC':sub[name],p=self.channel(p,depth+1)
    else:raise FormatError(f'Invalid child marker {t!r} at {p-4}')
   p=self.expect(p,b'ENDI');values=dict(zip(names,vals))
   if set(fields)&set(values):raise FormatError('Conflicting inherited fields')
   fields.update(values);children.update(sub);sections.append({'type':kind.hex(),'fields':values})
  if terminated:p=self.expect(p,b'^EN^')
  return {'offset':start,'end':p,'type':sections[0]['type'],'sections':sections,'fields':fields,'children':children},p
 def surfaces(self):
  out=[]
  for mt in INSTANCE.finditer(self.m):
   item,_=self.instance(mt.start());geo=item['children'].get('ObjectData')
   if geo and geo.get('kind')=='Vertex Data':out.append(item)
  return out


FILTER = bytes.fromhex("daf0495380ebbe4497e19680ea2238d9")


def material_overrides(reader):
    pattern = re.compile(rb"ICUD\x20\x00\x00\x00.{32}ICIC\x04\x00\x00\x00.{4}ICTD\x10\x00\x00\x00" + re.escape(FILTER), re.S)
    result = {}
    for match in pattern.finditer(reader.m):
        node, _ = reader.instance(match.start())
        name = node["fields"].get("materialname")
        if not isinstance(name, str) or name in result:
            raise FormatError("Ambiguous material filter name")
        result[name] = node
    return result


def transform_matrix(matrix):
    # column-major affine matris, cofactor'lar normal'ler için inverse-transpose'u verir.
    if matrix is None or len(matrix) != 16 or not all(math.isfinite(x) for x in matrix):
        raise FormatError("A single resolved world matrix is required")
    if any(abs(matrix[i]) > 1e-6 for i in (3, 7, 11)) or abs(matrix[15]-1) > 1e-6:
        raise FormatError("Non-affine world matrix")
    a,b,c,d,e,f,g,h,i = (matrix[j] for j in (0,4,8,1,5,9,2,6,10))
    co = (e*i-f*h, f*g-d*i, d*h-e*g, c*h-b*i, a*i-c*g, b*g-a*h,
          b*f-c*e, c*d-a*f, a*e-b*d)
    det = a*co[0] + b*co[1] + c*co[2]
    if not math.isfinite(det) or abs(det) < 1e-20:
        raise FormatError("Singular world matrix")
    def position(v):
        x,y,z = (sum(v[k]*matrix[j+4*k] for k in range(3))+matrix[j+12] for j in range(3))
        return x,-z,y
    def normal(v):
        n = [sum(co[j*3+k]*v[k] for k in range(3))/det for j in range(3)]
        length = math.sqrt(sum(x*x for x in n))
        if length: n = [x/length for x in n]
        return n[0],-n[2],n[1]
    return position, normal, det < 0


def export(reader, target, matrix, progress=None):
    """Çözülmüş tek LS10 modelini export eder. Vertex/triangle azaltma yok, kayıpsız.

    Attribute bazında sadece birebir aynı değerler dedup edilir, UV seam'leri ve
    sert normal'ler korunur. Map'lenmemiş shader parametreleri ve texture'lar
    provenance bundle'ında kalır. Yeni bundle ve OBJ ancak tam doğrulamadan
    sonra yayımlanır.
    """
    from pathlib import Path
    from array import array
    import hashlib, json, os, shutil, tempfile, uuid
    target = Path(target)
    pos_transform, normal_transform, mirrored = transform_matrix(matrix)
    surfaces = reader.surfaces()
    if not surfaces: raise FormatError("No structured surfaces")
    numbers = [s["fields"].get("surfaceNr") for s in surfaces]
    if len(set(numbers)) != len(numbers): raise FormatError("Ambiguous surface numbers")
    overrides = material_overrides(reader)
    target.parent.mkdir(parents=True, exist_ok=True)
    bundle_name = "lumion-" + uuid.uuid4().hex[:12]
    stage = Path(tempfile.mkdtemp(prefix=".lumion-", dir=target.parent))
    bundle = stage / bundle_name
    bundle.mkdir()
    obj = stage / target.name
    manifest = {"version": 1, "partial": True, "world_matrix": list(matrix),
                "surfaces": [], "overrides": overrides, "warnings": [
                    "Lumion shader, glass, auto-UV and photometry are not physically calibrated.",
                    "Embedded texture payloads are preserved, unresolved roles/transforms are not assigned as albedo."],
                "source_bytes": len(reader.m), "source_sha256": hashlib.sha256(reader.m).hexdigest(), "mesh": len(surfaces), "ucgen": 0,
                "vertex": 0, "source_vertex": 0, "atlanan": 0}
    offsets = [0,0,0]
    try:
        def save_texture(node, role):
            tags = node["buffers"]; chunk = tags["TEXT"]
            raw = reader.data(chunk); digest = hashlib.sha256(raw).hexdigest()
            ext = ".png" if raw.startswith(b"\x89PNG\r\n\x1a\n") else ".jpg" if raw.startswith(b"\xff\xd8") else ".dds" if raw.startswith(b"DDS ") else ".bin"
            name = digest + ext
            file = bundle / name
            if not file.exists(): file.write_bytes(raw)
            return {"role": role, "file": name, "sha256": digest, "offset": chunk["offset"], "bytes": len(raw),
                    "width": struct.unpack("<I", reader.data(tags["TEXW"]))[0],
                    "height": struct.unpack("<I", reader.data(tags["TEXH"]))[0], "applied": False}
        def textures(node, path=""):
            if not isinstance(node, dict): return []
            if node.get("kind") == "Texture": return [save_texture(node, path)]
            out=[]
            for key, child in node.items():
                if key in ("sections", "fields", "buffers"): continue
                if isinstance(child, dict): out.extend(textures(child, path+"/"+key))
                elif isinstance(child, list):
                    for n,item in enumerate(child): out.extend(textures(item, path+"/"+key+"/"+str(n)))
            return out
        with obj.open("w", encoding="ascii", buffering=1024*1024) as out, (bundle/"materials.mtl").open("w", encoding="ascii") as mtl:
            out.write("# CTLux structured Lumion recovery, see manifest.json\nmtllib " + bundle_name + "/materials.mtl\n")
            for si, surface in enumerate(surfaces):
                fields=surface["fields"]; geo=surface["children"]["ObjectData"]; tags=geo["buffers"]
                diffuse=fields.get("Diffuse")
                if not isinstance(diffuse,list) or len(diffuse)!=4 or not all(0<=v<=1 for v in diffuse[:3]):
                    raise FormatError("Invalid diffuse color")
                override=overrides.get(fields.get("surfaceName"))
                material = ((override or {}).get("children",{}).get("materialInfo") or {}).get("item",{})
                mf=material.get("fields",{}); color=mf.get("color")
                rgb=color[:3] if isinstance(color,list) and len(color)==4 and all(0<=v<=1 for v in color[:3]) else diffuse[:3]
                # Lumion'daki color.w opacity değil. GlassSettings olduğu gibi saklanır,
                # belgelenmiş bir eşleme olmadan transmission diye yorumlanmaz.
                name="lumion_%s_%03d" % (manifest["source_sha256"][:12], si)
                mtl.write("newmtl %s\nKd %.9g %.9g %.9g\nKs 0 0 0\nd 1\nillum 1\n\n" % (name,*rgb))
                # VTO0 ve VTO1 parser için opsiyonel, burada ise zorunlu.
                for t in ("VTO0","VTO1"):
                    if t not in tags: raise FormatError("Missing UV transform: " + t)
                    if tags[t]["length"] != 24: raise FormatError("UV transform size: " + t)
                info={"index":si,"surfaceNr":fields["surfaceNr"],"name":fields["surfaceName"],
                      "offset":surface["offset"],"material":name,"base_color":diffuse,
                      "display_color":rgb,"color_source":"Lumion color RGB (approximate shader)" if color else "Surface Diffuse",
                      "buffers":tags,"source_vertices":geo["vertices"],
                      "uv_transforms":{t:list(struct.unpack("<6f",reader.data(tags[t]))) for t in ("VTO0","VTO1")},
                      "textures":textures(surface)+textures(material)}
                for uv_name, values in info['uv_transforms'].items():
                    if not all(math.isfinite(v) for v in values):
                        raise FormatError('Nonfinite UV transform: ' + uv_name)
                    if values != [0,0,0,0,1,1]:
                        manifest['warnings'].append('Surface %s: %s transform not baked, UV mapping is partial' % (si, uv_name))
                if override:
                    info["override_offset"]=override["offset"]
                    if not color: info["shader_status"]="Original Diffuse shown, Lumion shader color mapping unresolved"
                # OBJ tek UV set taşısa da UV1 byte'larını olduğu gibi sakla.
                uv1_name = "uv1_%03d.f32le" % si
                uv1 = reader.data(tags["VTD1"])
                for value in struct.iter_unpack("<2f", uv1):
                    if not all(math.isfinite(x) for x in value):
                        raise FormatError("Nonfinite UV1 attribute")
                (bundle/uv1_name).write_bytes(uv1)
                info["uv1_file"] = uv1_name
                manifest["surfaces"].append(info)
                out.write("o mesh_%03d\nusemtl %s\n" % (si,name))
                maps=[]; counts=[]
                for ai,(tag,fmt,prefix,convert) in enumerate([
                    ("VPPI","<3f","v",pos_transform),
                    ("VTD0","<2f","vt",lambda x:x),
                    ("VNNI","<3h","vn",normal_transform)]):
                    chunk=tags[tag]; view=memoryview(reader.m)[chunk["offset"]:chunk["offset"]+chunk["length"]]
                    table={}; indices=array('I')
                    try:
                        for value in struct.iter_unpack(fmt,view):
                            if not all(math.isfinite(x) for x in value): raise FormatError("Nonfinite vertex attribute")
                            idx=table.get(value)
                            if idx is None:
                                idx=len(table)+1;table[value]=idx; transformed=convert(value)
                                out.write(prefix+" "+" ".join("%.17g" % v for v in transformed)+"\n")
                            indices.append(idx+offsets[ai])
                    finally: view.release()
                    maps.append(indices);counts.append(len(table));table.clear()
                chunk=tags["PO32"];view=memoryview(reader.m)[chunk["offset"]:chunk["offset"]+chunk["length"]]
                nfaces=0
                try:
                    for tri in struct.iter_unpack("<3I",view):
                        if max(tri)>=geo["vertices"]:raise FormatError("Face index outside vertex buffer")
                        if mirrored:tri=(tri[0],tri[2],tri[1])
                        out.write("f "+" ".join("%d/%d/%d"%(maps[0][i],maps[1][i],maps[2][i]) for i in tri)+"\n")
                        nfaces+=1
                finally:view.release()
                info["triangles"]=nfaces;info["export_attribute_counts"]=counts
                offsets=[a+b for a,b in zip(offsets,counts)]
                manifest["ucgen"]+=nfaces;manifest["vertex"]+=counts[0];manifest["source_vertex"]+=geo["vertices"]
                if progress: progress(si+1,len(surfaces))
        (bundle/"manifest.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
        os.replace(bundle,target.parent/bundle_name)
        try:
            os.replace(obj,target)
        except BaseException:
            shutil.rmtree(target.parent/bundle_name)
            raise
        manifest["manifest"] = str(target.parent/bundle_name/"manifest.json")
        return manifest
    finally:
        shutil.rmtree(stage)
