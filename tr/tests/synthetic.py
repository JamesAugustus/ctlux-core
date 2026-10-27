# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Paylaşılabilir test girdileri, içinde gerçek proje ya da üretici verisi yok."""
import base64
import json
from pathlib import Path
import struct
import sys
import zipfile
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

FBX_MAGIC = b"Kaydara FBX Binary  \x00\x1a\x00"

VERTICES = (0., 0., 0., 1., 0., 0., 0., 1., 0.)

def gltf_document():
    return {"asset": {"version": "2.0", "generator": "synthetic-format-lab"},
            "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0}],
            "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1, "mode": 4}]}],
            "buffers": [{"byteLength": 42}],
            "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": 36},
                            {"buffer": 0, "byteOffset": 36, "byteLength": 6}],
            "accessors": [{"bufferView": 0, "componentType": 5126, "count": 3, "type": "VEC3",
                           "min": [0, 0, 0], "max": [1, 1, 0]},
                          {"bufferView": 1, "componentType": 5123, "count": 3, "type": "SCALAR"}]}

def glb_bytes(bad_index=False):
    doc = json.dumps(gltf_document(), separators=(",", ":")).encode()
    doc += b" " * (-len(doc) % 4)
    binary = struct.pack("<9f3H", *VERTICES, 0, 1, 9 if bad_index else 2)
    binary += b"\x00" * (-len(binary) % 4)
    chunks = struct.pack("<II", len(doc), 0x4E4F534A) + doc
    chunks += struct.pack("<II", len(binary), 0x004E4942) + binary
    return struct.pack("<4sII", b"glTF", 2, 12 + len(chunks)) + chunks

def chunk3ds(kind, payload):
    return struct.pack("<HI", kind, 6 + len(payload)) + payload

def three_ds_bytes(bad_index=False):
    vertices = chunk3ds(0x4110, struct.pack("<H9f", 3, *VERTICES))
    faces = chunk3ds(0x4120, struct.pack("<H4H", 1, 0, 1, 9 if bad_index else 2, 7))
    obj = chunk3ds(0x4000, b"triangle\x00" + chunk3ds(0x4100, vertices + faces))
    editor = chunk3ds(0x3D3D, chunk3ds(0x3D3E, struct.pack("<I", 3)) + obj)
    return chunk3ds(0x4D4D, chunk3ds(0x0002, struct.pack("<I", 3)) + editor)

def fbx_property(value):
    if isinstance(value, str):
        raw = value.encode()
        return b"S" + struct.pack("<I", len(raw)) + raw
    if isinstance(value, int):
        return b"L" + struct.pack("<q", value)
    if len(value) == 2 and value[0] == "I":
        return b"I" + struct.pack("<i", value[1])
    kind, values, compressed = value
    raw = struct.pack("<" + ("d" if kind == "d" else "i") * len(values), *values)
    payload = zlib.compress(raw) if compressed else raw
    return kind.encode() + struct.pack("<III", len(values), int(compressed), len(payload)) + payload

def fbx_bytes(compressed=False, version=7400, wrong_version_type=False):
    """Küçük bir FBX ağacı, footer/SDK uyumluluğunun tamamını kanıtladığını iddia etmiyor."""
    version_value = version if wrong_version_type else ("I", version)
    tree = [("FBXHeaderExtension", [], [("FBXHeaderVersion", [("I", 1003)], []), ("FBXVersion", [version_value], [])]),
            ("Objects", [], [
                ("Geometry", [1, "Geometry::Triangle", "Mesh"], [
                    ("Vertices", [("d", VERTICES, compressed)], []),
                    ("PolygonVertexIndex", [("i", (0, 1, -3), compressed)], [])]),
                ("Model", [2, "Model::Triangle", "Mesh"], [("Version", [("I", 232)], [])])]),
            ("Connections", [], [("C", ["OO", 1, 2], []), ("C", ["OO", 2, 0], [])])]
    header_size, header_fmt = (25, "<QQQB") if version >= 7500 else (13, "<IIIB")

    def node(spec, offset):
        name, props, children = spec
        encoded_name, properties = name.encode(), b"".join(map(fbx_property, props))
        child_start = offset + header_size + len(encoded_name) + len(properties)
        body = b""
        for child in children:
            body += node(child, child_start + len(body))
        if children:
            body += bytes(header_size)
        end = child_start + len(body)
        return struct.pack(header_fmt, end, len(props), len(properties), len(encoded_name)) + encoded_name + properties + body

    data = FBX_MAGIC + struct.pack("<I", version)
    for spec in tree:
        data += node(spec, len(data))
    return data + bytes(header_size)

FBX_ASCII = b'''; FBX 7.4.0 project file
FBXHeaderExtension: {
 FBXHeaderVersion: 1003
 FBXVersion: 7400
}
Objects: {
 Geometry: 1, "Geometry::Triangle", "Mesh" {
  Vertices: *9 { a: 0,0,0,1,0,0,0,1,0 }
  PolygonVertexIndex: *3 { a: 0,1,-3 }
 }
 Model: 2, "Model::Triangle", "Mesh" { Version: 232 }
}
Connections: {
 C: "OO",1,2
 C: "OO",2,0
}
'''

OBJ = b"# synthetic triangle\nv 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n"

STL_ASCII = b"solid triangle\nfacet normal 0 0 1\nouter loop\nvertex 0 0 0\nvertex 1 0 0\nvertex 0 1 0\nendloop\nendfacet\nendsolid triangle\n"

DAE = b'''<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
<asset><created>2026-01-01T00:00:00Z</created><modified>2026-01-01T00:00:00Z</modified><unit name="meter" meter="1"/><up_axis>Y_UP</up_axis></asset>
<library_geometries><geometry id="triangle"><mesh>
<source id="positions"><float_array id="positions-array" count="9">0 0 0 1 0 0 0 1 0</float_array><technique_common><accessor source="#positions-array" count="3" stride="3"><param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/></accessor></technique_common></source>
<vertices id="vertices"><input semantic="POSITION" source="#positions"/></vertices>
<triangles count="1"><input semantic="VERTEX" source="#vertices" offset="0"/><p>0 1 2</p></triangles>
</mesh></geometry></library_geometries>
<library_visual_scenes><visual_scene id="scene"><node id="node"><instance_geometry url="#triangle"/></node></visual_scene></library_visual_scenes>
<scene><instance_visual_scene url="#scene"/></scene></COLLADA>'''

IES = ('IESNA:LM-63-2002\n[TEST] Synthetic\nTILT=NONE\n'
       '1 1256.637061 1 3 1 1 2 0 0 0\n1 1 0\n0 90 180\n0\n100 100 100\n')


def lumion(defaults=False):
    from test_ls10_preservation import model_kaydi, tlv, matris, isik
    mesh = tlv(b'VPPI', [0,0,0, 2,0,0, 0,0,2])+tlv(b'PO32',[0,1,2],'I')
    light = isik(0, 1)
    if defaults:
        chunks = 'ClassType->cLightObject'.encode('utf-16le')
        for n in range(1, 38):
            if n == 3:
                chunks += tlv(b'IIM1', matris(2))
            elif n == 28:
                chunks += tlv(b'IIVE', [1])
            elif n in (26, 35, 36, 37):
                chunks += tlv(b'IIV1', [1,1,1,1])
            else:
                chunks += tlv(b'IIVE', [1])
        light = chunks
    return model_kaydi(0)+mesh+light+isik(1, 1.5)


def evo(path, count=1, truncated=False):
    from test_evo_components import STEP
    text = STEP
    for i in range(1, count):
        text += "\n#%d=LuminaireElement(%d,'ışık',(),#5,$,$);" % (1000+i, 1000+i)
    if truncated:
        text += "\n#999999=Broken('unfinished"
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('Project/ProjectData/ProjectData.dat', text)
    return path


KAMASMA_STEP = '''
#1=CoordSys3D((0,0,0),(1,0,0),(0,1,0),(0,0,1));
#2=CoordSys3D((0,0,0),(1,0,0),(0,1,0),(0,0,1));
#5=CoordSys3D((2,2.5,2.8),(1,0,0),(0,1,0),(0,0,1));
#10=Storey(10,'kat',(),#1,#11,$);
#11=InstanceGeometricRepresentation(#10,#12);
#12=StoreyRepresentationData(#10,0,(),(),False,(1,1,1),3);
#20=StoreyElement(20,'eleman',(),#2,$,$);
#30=StoreyContour(30,'kontur',(),#2,#31,$);
#31=InstanceGeometricRepresentation(#30,#32);
#32=StoreyContourRepresentationData(#30,0,(),(),False,(1,1,1),((0,0),(4,0),(4,5),(0,5)),$,(),.Inner.,False,$,$);
#40=Space(40,'oda',(),#2,#41,$);
#41=SpaceGeometricRepresentation(#40,#42);
#42=SpaceRepresentationData(#40,0,(#43),(),False,(1,1,1));
#43=StoreyContourBasedSpaceRepresentationDataPart(#42,1,#2,.Storey.,3);
#60=LuminaireElement(60,'tavan lambası',(),#5,$,$);
#70=RelContainedInSpatialStructure(70,'',#10,(#20,#60));
#71=RelAggregates(71,'',#20,(#30));
#72=RelAggregates(72,'',#10,(#40));
#74=RelAssociatesStoreyContourBasedSpace(74,'',#30,#40,True);
'''


def kamasma_odasi(path):
    """4 x 5 x 3 m oda, tavanda aşağı bakan tek ışık, kamaşma komutunun uçtan uca testi için."""
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('Project/ProjectData/ProjectData.dat', KAMASMA_STEP)
    return path


def urun_ies(lumen, dikey, kandela, yatay=(0,), ad='Sentetik spot', uretici='Ornek Uretici'):
    """Sentetik IES, açı ve kandela listeleri çağırandan gelir, doğrulama yapılmaz."""
    rows = [kandela[i*len(dikey):(i+1)*len(dikey)] for i in range(len(yatay))]
    return ('IESNA:LM-63-2002\n[TEST] Synthetic\n[MANUFAC] %s\n[LUMINAIRE] %s\nTILT=NONE\n'
            '1 %g 1 %d %d 1 2 0 0 0\n1 1 10\n%s\n%s\n%s\n' % (
                uretici, ad, lumen, len(dikey), len(yatay), ' '.join('%g' % x for x in dikey),
                ' '.join('%g' % x for x in yatay), '\n'.join(' '.join('%g' % x for x in r) for r in rows)))


def _evo_urunu(no, kod, ad, yatay, dikey, kandela, lumen, ornekler):
    """Tek ürün: prototip, ürün adı, fotometri dağılımı ve opsiyonel lümen kaydı."""
    liste = lambda values: '(%s)' % ','.join('%g' % v for v in values)
    satirlar = [
        "#%d=LuminairePrototype(%d,'P%d',(#%d),#%d,$);" % (no, 9000+no, no, no+10, no+1),
        "#%d=PrototypeGeometricRepresentation(#%d,#%d);" % (no+1, no, no+2),
        "#%d=LuminairePrototypeRepresentationData(#%d,0,(),(),False,(1,1,1));" % (no+2, no),
        "#%d=ProductDataPropertySet(%d,'',#%d,#%d);" % (no+10, no+10, no, no+11),
        "#%d=ProductData('%s','',#%d,$);" % (no+11, kod, no+12),
        "#%d=LanguageDependentTextContainer(((1055,((.ArticleName.,'%s')))));" % (no+12, ad),
        "#%d=RelDefinesByPrototype(%d,'',(%s),#%d);" % (no+20, no+20, ','.join('#%d' % o for o in ornekler), no),
        "#%d=LightDistributionConnection(#%d,0,#%d,#%d);" % (no+30, no+2, no+31, no+32),
        "#%d=LightEmittingPart(#%d,0);" % (no+31, no+2),
        "#%d=LightDistribution(#%d);" % (no+32, no+33),
        "#%d=LightDistributionData((),%s,%s,%s);" % (no+33, liste(yatay), liste(dikey), liste(kandela))]
    if lumen is not None:
        satirlar.append("#%d=LampTypeChannel(#%d,0,1,%g);" % (no+34, no+2, lumen))
    return satirlar


def urunlu_evo(path):
    """Dört ürün: A iki kez kullanılıyor, B bir kez, C lümensiz, D negatif kandelalı.

    #65 hiçbir ürüne bağlı değil. Sayılar sadece format testi için.
    """
    lines = ["#1=CoordSys3D((0,0,0),(1,0,0),(0,1,0),(0,0,1));",
             "#10=Storey(10,'kat',(),#1,$,$);"]
    ornekler = (60, 61, 62, 63, 64, 65)
    for i, no in enumerate(ornekler):
        lines.append("#%d=CoordSys3D((%d,1,2.8),(1,0,0),(0,1,0),(0,0,1));" % (no+100, i))
        lines.append("#%d=LuminaireElement(%d,'ışık %d',(),#%d,$,$);" % (no, 1000+no, no, no+100))
    lines.append("#70=RelContainedInSpatialStructure(70,'',#10,(%s));" % ','.join('#%d' % o for o in ornekler))
    lines += _evo_urunu(200, 'SPOT-A', 'Sentetik spot A', (0,), (0, 10, 20, 30, 40, 90),
                        (1000, 900, 400, 100, 20, 0), 1200, (60, 61))
    lines += _evo_urunu(300, 'GENIS-B', 'Sentetik geniş B', (0, 90, 180, 270), (0, 45, 90),
                        (500, 450, 100, 520, 460, 110, 500, 450, 100, 480, 440, 90), 3000, (62,))
    lines += _evo_urunu(400, 'LUMENSIZ-C', 'Sentetik lümensiz C', (0,), (0, 90),
                        (300, 0), None, (63,))
    lines += _evo_urunu(500, 'BOZUK-D', 'Sentetik bozuk D', (0,), (0, 45, 90),
                        (800, -5, 0), 800, (64,))
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('Project/ProjectData/ProjectData.dat', '\n'.join(lines)+'\n')
    return path


def fotometri_zip(path):
    """Üretici paketi: geçerli IES, LDT, mutlak (lümensiz) IES, negatif IES ve aynı içerikli kopya."""
    from test_ldt_translation import LDTTest
    spot = urun_ies(1500, (0, 10, 20, 30, 90), (2000, 1500, 600, 50, 0), ad='Paket spot')
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('Uretici/spot_a.ies', spot)
        archive.writestr('Uretici/kopya/spot_a.ies', spot)
        archive.writestr('Uretici/genis.ldt', '\n'.join(LDTTest().fixture()))
        archive.writestr('Uretici/mutlak.ies', urun_ies(-1, (0, 90), (100, 0), ad='Mutlak'))
        archive.writestr('Uretici/bozuk.ies', urun_ies(900, (0, 90), (100, -1), ad='Bozuk'))
        archive.writestr('__MACOSX/Uretici/._spot_a.ies', b'meta')
        archive.writestr('Uretici/OKUBENI.txt', 'sentetik')
    return path


def gldf(path):
    """GLDF container'ı: sadece fotometri klasörü, ürün XML'i okunmaz."""
    from test_ldt_translation import LDTTest
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('product.xml', '<Root/>')
        archive.writestr('ldc/urun.ldt', '\n'.join(LDTTest().fixture()))
    return path


def uret(directory):
    from test_ldt_translation import LDTTest
    root = Path(directory); root.mkdir(parents=True, exist_ok=True)
    payloads = {'ucgen.obj': OBJ, 'ascii.stl': STL_ASCII,
        'binary.stl': b'synthetic'.ljust(80,b' ')+struct.pack('<I12fH',1,0,0,1,*VERTICES,0),
        'ucgen.3ds': three_ds_bytes(), 'ucgen.glb': glb_bytes(),
        'ucgen.dae': DAE, 'ascii.fbx': FBX_ASCII, 'binary.fbx': fbx_bytes(),
        'sahne.ls10': lumion(), 'varsayilan.ls10': lumion(True),
        'isik.ies': IES.encode(), 'isik.ldt': '\n'.join(LDTTest().fixture()).encode(),
        'ucgen.usda': b'''#usda 1.0
(metersPerUnit = 1
 upAxis = "Z")
def Mesh "Triangle" {
 int[] faceVertexCounts = [3]
 int[] faceVertexIndices = [0,1,2]
 point3f[] points = [(0,0,0),(1,0,0),(0,1,0)]
}
'''}
    gltf = gltf_document()
    gltf['buffers'][0]['uri'] = 'data:application/octet-stream;base64,'+base64.b64encode(struct.pack('<9f3H',*VERTICES,0,1,2)).decode()
    payloads['ucgen.gltf'] = json.dumps(gltf).encode()
    for name, data in payloads.items():
        path=root/name
        if path.exists():
            raise FileExistsError(name)
        path.write_bytes(data)
    evo(root/'oda.evo')
    return sorted(root.iterdir())


if __name__ == '__main__':
    for path in uret(sys.argv[1]):
        print(path.name)
