#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Lumion proje dosyası (.ls10) reader'ı.

Lumion projeleri şifresiz, tag'li binary (Quest3D/ACT-3D'den kalma "CHIT" chunk'ları,
UTF-16LE string'ler). Bu modül dosyayı sadece okur: model world matrix'i, mesh ve
light kayıtları. Çağıran ice_aktarma, komut satırından çalışmaz.
"""
import sys, re, struct, array, math
import warnings


def imza_say(m, sig, sinir=2000000):
    n = 0; i = 0; ilk = -1
    while True:
        j = m.find(sig, i)
        if j < 0:
            break
        if ilk < 0:
            ilk = j
        n += 1; i = j + len(sig)
        if n > sinir:
            break
    return n, ilk


# ---- geometriyi oku ----
# Lumion mesh node'unun layout'u (dosyaya bakarak çözdük):
#   TAG(4B ASCII) + LEN(uint32 LE) + payload
#   VPPI = position (3x float32)   PO32 = index (uint32, üçgen)
#   VNNI = normal (int16 snorm)  VTD0/VTD1 = UV (2x float32)  (şimdilik sadece position + index kullanıyoruz)
# buffer'daki konumlar model local space'inde olabilir, world matrix ayrı kayıtta.
def tek_model_matrisi(m):
    """Sadece tek cCustomObject + tek cImportObject düzeninde world matrix'i döner.

    Birden çok modelde buffer/instance bağlantısını çözmeden matris seçmiyoruz.
    Bu dar düzenin dışındaki dosyalarda None döner, çağıran kısmi aktarım bildirir.
    """
    tag = 'ClassType->cCustomObject'.encode('utf-16le')
    bas = [x.start() for x in re.finditer(re.escape(tag), m)]
    imp = 'ClassInstance->cImportObject'.encode('utf-16le')
    if len(bas) != 1 or imza_say(m, imp)[0] != 1:
        return None
    a = bas[0] + len(tag)
    b = m.find('ClassType->'.encode('utf-16le'), a)
    b = b if b >= 0 else len(m)
    if m.find('world'.encode('utf-16le'), a, b) < 0:
        return None
    p = m.find(b'IIM1', a, b)
    if p < 0 or p + 72 > b or struct.unpack_from('<I', m, p+4)[0] != 64:
        return None
    mat = struct.unpack_from('<16f', m, p+8)
    if not all(math.isfinite(v) for v in mat):
        return None
    if any(abs(mat[i]) > 1e-6 for i in (3, 7, 11)) or abs(mat[15]-1) > 1e-6:
        return None
    det = (mat[0]*(mat[5]*mat[10]-mat[9]*mat[6])
           - mat[4]*(mat[1]*mat[10]-mat[9]*mat[2])
           + mat[8]*(mat[1]*mat[6]-mat[5]*mat[2]))
    if not math.isfinite(det) or abs(det) < 1e-20:
        return None
    return mat


def mesh_cikar(m, out_obj, zup=False, limit=None, matris=None, tekil=False):
    n_mesh = 0; tot_v = 0; tot_f = 0; voff = 0; atlanan = 0
    # Aynalı dünya matrisi yüz sırasını ters çevirir. (x, -z, y) Z-up dönüşümünün determinantı +1.
    ikinci, ucuncu = 1, 2
    if matris is not None:
        det = (matris[0]*(matris[5]*matris[10]-matris[9]*matris[6])
               - matris[4]*(matris[1]*matris[10]-matris[9]*matris[2])
               + matris[8]*(matris[1]*matris[6]-matris[5]*matris[2]))
        if det < 0:
            ikinci, ucuncu = 2, 1
    with open(out_obj, "w") as o:
        o.write("# Lumion .ls10 -> OBJ  (CTLux / lumion_oku)\n")
        i = 0
        while True:
            p = m.find(b'VPPI', i)
            if p < 0:
                break
            if p + 8 > len(m):
                atlanan += 1; break
            ln = struct.unpack_from('<I', m, p + 4)[0]
            vstart = p + 8
            if ln == 0 or ln % 12 or ln > 300_000_000 or vstart + ln > len(m):
                i = p + 4; continue
            vc = ln // 12
            q = m.find(b'PO32', vstart + ln)
            if q < 0:
                break
            if q + 8 > len(m):
                atlanan += 1; break
            sonraki = m.find(b'VPPI', vstart + ln, q)
            if sonraki >= 0:
                atlanan += 1; i = sonraki; continue
            iln = struct.unpack_from('<I', m, q + 4)[0]
            istart = q + 8
            if not iln or iln % 12 or istart + iln > len(m):
                i = vstart + ln; continue
            ic = iln // 4
            try:
                verts = array.array('f'); verts.frombytes(memoryview(m)[vstart:vstart+ln])
                idx = array.array('I'); idx.frombytes(memoryview(m)[istart:istart+iln])
                if sys.byteorder != 'little':
                    verts.byteswap(); idx.byteswap()
            except struct.error:
                i = istart + iln; continue
            if not all(math.isfinite(v) for v in verts):
                atlanan += 1; i = istart + iln; continue
            if ic and max(idx) >= vc:      # yanlış eşleşme, bu mesh'i atla
                atlanan += 1; i = istart + iln; continue
            # sadece birebir aynı vertex'ler merge edilir: grid, mesafe eşiği ya da yüz silme yok.
            # grup sınırları korunur. Matris önce Lumion space'inde, Z-up dönüşümü sonra.
            tablo = {}; esle = array.array('I'); yazilan = 0
            o.write("o mesh_%03d\n" % n_mesh)
            for k in range(vc):
                p3 = tuple(verts[3*k:3*k+3])
                if tekil and p3 in tablo:
                    esle.append(tablo[p3]); continue
                esle.append(yazilan); tablo[p3] = yazilan; yazilan += 1
                x, y, z = p3
                if matris is not None:
                    x, y, z = (x*matris[j]+y*matris[j+4]+z*matris[j+8]+matris[j+12]
                               for j in range(3))
                if zup:
                    x, y, z = x, -z, y
                o.write("v %.17g %.17g %.17g\n" % (x, y, z))
            fl = "".join("f %d %d %d\n" % (voff+esle[idx[k]]+1, voff+esle[idx[k+ikinci]]+1, voff+esle[idx[k+ucuncu]]+1)
                         for k in range(0, ic - 2, 3))
            o.write(fl)
            n_mesh += 1; tot_v += yazilan; tot_f += ic // 3; voff += yazilan
            i = istart + iln
            if limit and n_mesh >= limit:
                break
    return {"mesh": n_mesh, "vertex": tot_v, "ucgen": tot_f, "atlanan": atlanan, "obj": out_obj}


# ---- light'ları oku ----
# cLightObject bloğunun layout'u (dosyaya bakarak çözdük, numara = value chunk sırası):
#   #3  IIM1 = 4x4 world matrix -> position [12,13,14], direction [8,9,10]
#   #26 IIVE cone angle, #28 IIV1 ham vektör (renk anlamı doğrulanmadı,
#   uzunluğunu göreli şiddet diye kullanıyoruz), #37 IIVE tip (0 spot, 1 omni, 3 area)
#   value tag boyları: IIVE=4B, IIV1=16B, IIM1=64B. Blok sınırı 'cLightObject' (her blokta 1 tane).
def _isik_bloklari(m):
    tag = 'cLightObject'.encode('utf-16le')
    st = []; i = 0
    while True:
        j = m.find(tag, i)
        if j < 0:
            break
        st.append(j); i = j + len(tag)
    st.append(len(m))
    return st

def isik_coz(m, birlestir=False):
    """Her cLightObject bloğunu çözer -> [{pos,dir,rgb,sidd,tip,cone,w,h}] (Lumion Y-up).
    Default olarak çözülebilen bütün kayıtlar kalır. birlestir=True sadece eski,
    kayıplı yakınlık merge'ünü açar, birbirinden bağımsız light'ları da silebilir."""
    st = _isik_bloklari(m)
    out = []
    for bi in range(len(st) - 1):
        a, b = st[bi], st[bi + 1]
        vals = []; sonraki = a
        # aramayı C tarafındaki regex yapıyor, her byte için Python loop'u dönmez.
        for eslesme in re.compile(rb'IIVE|IIV1|IIM1').finditer(m, a, b):
            p = eslesme.start()
            if p < sonraki or p + 8 > b:
                continue
            t = eslesme.group()
            exp = {b'IIVE': 4, b'IIV1': 16, b'IIM1': 64}[t]
            ln = struct.unpack_from('<I', m, p + 4)[0]
            if ln == exp and p + 8 + ln <= b:
                vals.append((t, m[p + 8:p + 8 + ln])); sonraki = p + 8 + ln
        if len(vals) < 37:
            warnings.warn('Lumion ışık ls:%x atlandı: 37 değer kaydından az' % a, RuntimeWarning)
            continue
        # çapa: tek IIM1 (world matrix) = layout'taki #3. Diğer alanlar buna göre sayılır:
        # #N = vals[j + (N-3)]. RTTI bölümündeki fazladan value chunk'ların yarattığı kaymayı böyle atlatıyoruz.
        j = next((ix for ix, (tg, d) in enumerate(vals) if tg == b'IIM1'), None)
        if j is None:
            warnings.warn('Lumion ışık ls:%x atlandı: dünya matrisi yok' % a, RuntimeWarning)
            continue
        def V(n):
            k2 = j + (n - 3)
            return vals[k2] if 0 <= k2 < len(vals) else (b'', b'')
        mtx = vals[j]
        M = struct.unpack('<16f', mtx[1])
        if not all(math.isfinite(v) for v in M):
            warnings.warn('Lumion ışık ls:%x atlandı: dünya matrisi sonlu değil' % a, RuntimeWarning)
            continue
        pos = (M[12], M[13], M[14]); dr = (M[8], M[9], M[10])
        col = V(28); rgb = struct.unpack('<4f', col[1])[:3] if col[0] == b'IIV1' else (1., 1., 1.)
        sidd = (rgb[0] ** 2 + rgb[1] ** 2 + rgb[2] ** 2) ** 0.5
        tp = V(37); tip = struct.unpack('<f', tp[1])[0] if tp[0] == b'IIVE' else 0.0
        cn = V(26); cone = struct.unpack('<f', cn[1])[0] if cn[0] == b'IIVE' else 1.0
        w = V(35); h = V(36)
        wv = struct.unpack('<f', w[1])[0] if w[0] == b'IIVE' else 1.0
        hv = struct.unpack('<f', h[1])[0] if h[0] == b'IIVE' else 1.0
        # NaN ya da sonsuz alan sonraki yuvarlama ve güç adımlarını durdurur.
        bozuk = [ad for ad, degerler in (('rgb', rgb), ('tip', (tip,)), ('cone', (cone,)),
                 ('w', (wv,)), ('h', (hv,)), ('sidd', (sidd,)))
                 if not all(math.isfinite(v) for v in degerler)]
        if bozuk:
            warnings.warn('Lumion ışık ls:%x atlandı: sonlu olmayan %s' % (a, ', '.join(bozuk)), RuntimeWarning)
            continue
        tip = int(round(tip))
        defaults = []
        for name, value, tag, fallback in (('rgb', col, b'IIV1', 'beyaz'),
                ('tip', tp, b'IIVE', '0'), ('cone', cn, b'IIVE', '1.0'),
                ('w', w, b'IIVE', '1.0'), ('h', h, b'IIVE', '1.0')):
            if value[0] != tag:
                defaults.append(name)
                warnings.warn('Lumion ışık ls:%x: %s varsayılan=%s (alan yok/etiket farklı)' %
                              (a, name, fallback), RuntimeWarning)
        out.append({"kaynak_id": "ls:%x" % a, "pos": pos, "dir": dr, "rgb": rgb, "sidd": sidd,
                    "tip": tip, "cone": cone, "w": wv, "h": hv, "varsayilanlar": defaults})
    if not birlestir:
        return out
    # eski yakınlık varsayımı, kaynak programda gerçekten aynı light olduklarını kanıtlamaz.
    # aynı yöne bakan yakın spot ile area light bağımsız da olabilir. O yüzden opt-in.
    alanlar = {}
    for L in out:
        if L["tip"] == 3:
            alanlar.setdefault(tuple(round(c, 3) for c in L["dir"]), []).append(L)
    if not alanlar:
        return out
    def _ornekleme_mi(L):
        for A in alanlar.get(tuple(round(c, 3) for c in L["dir"]), ()):
            esik = max(A["w"], A["h"]) / 2.0 + 0.15
            dx = L["pos"][0] - A["pos"][0]
            dy = L["pos"][1] - A["pos"][1]
            dz = L["pos"][2] - A["pos"][2]
            if dx*dx + dy*dy + dz*dz <= esik*esik:
                return True
        return False
    return [L for L in out if not (L["tip"] == 0 and _ornekleme_mi(L))]

def _zup(v):   # Lumion Y-up -> Radiance Z-up: (x,y,z)->(x,-z,y), yön ve konuma aynı dönüşüm
    return (v[0], -v[2], v[1])

def _norm(v):
    import math as _mm
    l = _mm.sqrt(v[0]*v[0] + v[1]*v[1] + v[2]*v[2]) or 1.0
    return (v[0]/l, v[1]/l, v[2]/l)

def _cross(a, b):
    return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])

def _perp(n):
    """n'e dik ortonormal (u, v) taban üret."""
    a = (0.0, 0.0, 1.0) if abs(n[2]) < 0.9 else (1.0, 0.0, 0.0)
    u = _norm(_cross(n, a)); v = _norm(_cross(n, u))
    return u, v


def isik_armatur(m, zup=True, k=200.0):
    """Light'ları armatür dict formatına çevirir (proje dict'indeki 'armaturler' şeması).
    Böylece Lumion light'ları da EVO'dakiler gibi aynı yoldan sahneye girer."""
    import math
    isk = isik_coz(m)
    out = []
    for i, L in enumerate(isk):
        p = _zup(L["pos"]) if zup else L["pos"]
        d = _norm(_zup(L["dir"]) if zup else L["dir"])
        cone = L["cone"]; aci = cone * 180.0 / math.pi if cone < 6.3 else cone
        aci = min(max(aci, 10.0), 160.0)
        etiket = {0: "spot", 1: "omni", 3: "alan"}.get(L["tip"], "spot")
        guc = L["sidd"] / k * 1000
        if not math.isfinite(guc):
            warnings.warn('Lumion ışık %s atlandı: güç sonlu değil' % L["kaynak_id"], RuntimeWarning)
            continue
        rec = {
            "ad": "lumion_%s_%d" % (etiket, i),
            "x": round(p[0], 3), "y": round(p[1], 3), "z": round(p[2], 3),
            "renk": [1.0, 1.0, 1.0],
            "renk_kaynagi": "varsayilan_notr",
            "renk_notu": "Lumion dosyasındaki ışık rengi doğrulanamadı, nötr beyaz varsayılan kullanıldı. Kaynak renk sıcaklığı değildir.",
            "guc": max(1, int(round(guc))),
            "yaricap": 0.1, "tip": etiket, "aci": round(aci, 1),  # gerçek tür (spot/omni/alan), çıktıda ayrı kalsın
            "kaynak": "lumion", "kaynak_id": L["kaynak_id"],
            "fotometri_durumu": "yaklasik",
            "varsayilanlar": list(L.get('varsayilanlar', [])),
        }
        if etiket == "omni":
            rec["yon"] = None                              # omni'nin yönü yok
        else:
            rec["yon"] = [round(d[0], 3), round(d[1], 3), round(d[2], 3)]
        if etiket == "alan":                               # area light: panel boyutu
            rec["w"] = round(float(L.get("w", 1.0)), 3)
            rec["h"] = round(float(L.get("h", 1.0)), 3)
        out.append(rec)
    return out
