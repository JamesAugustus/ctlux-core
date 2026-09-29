#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Lumion project file (.ls10) reader.

Lumion projects use unencrypted tagged binary (legacy Quest3D/ACT-3D "CHIT" chunks,
UTF-16LE strings). This module only reads files: model world matrix, mesh, and
light records. Called by the importer module. It is not a command-line tool.
"""
import sys, re, struct, array, math
import warnings


def count_signature(m, sig, limit=2000000):
    n = 0; i = 0; first = -1
    while True:
        j = m.find(sig, i)
        if j < 0:
            break
        if first < 0:
            first = j
        n += 1; i = j + len(sig)
        if n > limit:
            break
    return n, first


# ---- read geometry ----
# Lumion mesh node layout (recovered by examining files):
#   TAG(4B ASCII) + LEN(uint32 LE) + payload
#   VPPI = position (3x float32)   PO32 = index (uint32, triangle)
#   VNNI = normal (int16 snorm)  VTD0/VTD1 = UV (2x float32)  (currently only position + index are used)
# Buffer positions may be in model-local space. The world matrix is a separate record.
def single_model_matrix(m):
    """Return the world matrix only for a single cCustomObject + single cImportObject layout.

    For multiple models, do not choose a matrix without resolving buffer/instance links.
    Return None for files outside this narrow layout. The caller reports a partial import.
    """
    tag = 'ClassType->cCustomObject'.encode('utf-16le')
    starts = [x.start() for x in re.finditer(re.escape(tag), m)]
    imp = 'ClassInstance->cImportObject'.encode('utf-16le')
    if len(starts) != 1 or count_signature(m, imp)[0] != 1:
        return None
    a = starts[0] + len(tag)
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


def extract_mesh(m, out_obj, zup=False, limit=None, matrix=None, unique=False):
    n_mesh = 0; tot_v = 0; tot_f = 0; voff = 0; skipped = 0
    # Mirrored world matrices reverse winding. The (x, -z, y) Z-up rotation has determinant +1.
    second, third = 1, 2
    if matrix is not None:
        det = (matrix[0]*(matrix[5]*matrix[10]-matrix[9]*matrix[6])
               - matrix[4]*(matrix[1]*matrix[10]-matrix[9]*matrix[2])
               + matrix[8]*(matrix[1]*matrix[6]-matrix[5]*matrix[2]))
        if det < 0:
            second, third = 2, 1
    with open(out_obj, "w") as o:
        o.write("# Lumion .ls10 -> OBJ  (CTLux / read_lumion)\n")
        i = 0
        while True:
            p = m.find(b'VPPI', i)
            if p < 0:
                break
            if p + 8 > len(m):
                skipped += 1; break
            ln = struct.unpack_from('<I', m, p + 4)[0]
            vstart = p + 8
            if ln == 0 or ln % 12 or ln > 300_000_000 or vstart + ln > len(m):
                i = p + 4; continue
            vc = ln // 12
            q = m.find(b'PO32', vstart + ln)
            if q < 0:
                break
            if q + 8 > len(m):
                skipped += 1; break
            next_offset = m.find(b'VPPI', vstart + ln, q)
            if next_offset >= 0:
                skipped += 1; i = next_offset; continue
            iln = struct.unpack_from('<I', m, q + 4)[0]
            istart = q + 8
            if not iln or iln % 12 or istart + iln > len(m):
                i = vstart + ln; continue
            inner = iln // 4
            try:
                verts = array.array('f'); verts.frombytes(memoryview(m)[vstart:vstart+ln])
                idx = array.array('I'); idx.frombytes(memoryview(m)[istart:istart+iln])
                if sys.byteorder != 'little':
                    verts.byteswap(); idx.byteswap()
            except struct.error:
                i = istart + iln; continue
            if not all(math.isfinite(v) for v in verts):
                skipped += 1; i = istart + iln; continue
            if inner and max(idx) >= vc:      # incorrect match. Skip this mesh
                skipped += 1; i = istart + iln; continue
            # Merge only identical vertices: no grid, distance threshold, or face removal.
            # Preserve group boundaries. Apply the matrix in Lumion space, then convert to Z-up.
            table = {}; index_map = array.array('I'); written = 0
            o.write("o mesh_%03d\n" % n_mesh)
            for k in range(vc):
                p3 = tuple(verts[3*k:3*k+3])
                if unique and p3 in table:
                    index_map.append(table[p3]); continue
                index_map.append(written); table[p3] = written; written += 1
                x, y, z = p3
                if matrix is not None:
                    x, y, z = (x*matrix[j]+y*matrix[j+4]+z*matrix[j+8]+matrix[j+12]
                               for j in range(3))
                if zup:
                    x, y, z = x, -z, y
                o.write("v %.17g %.17g %.17g\n" % (x, y, z))
            fl = "".join("f %d %d %d\n" % (voff+index_map[idx[k]]+1, voff+index_map[idx[k+second]]+1, voff+index_map[idx[k+third]]+1)
                         for k in range(0, inner - 2, 3))
            o.write(fl)
            n_mesh += 1; tot_v += written; tot_f += inner // 3; voff += written
            i = istart + iln
            if limit and n_mesh >= limit:
                break
    return {"mesh": n_mesh, "vertex": tot_v, "triangle": tot_f, "skipped": skipped, "obj": out_obj}


# ---- read lights ----
# cLightObject block layout recovered by examining files. Numbers show the value chunk order:
#   #3  IIM1 = 4x4 world matrix -> position [12,13,14], direction [8,9,10]
#   #26 IIVE cone angle, #28 IIV1 raw vector (color meaning is unverified).
#   Vector length is used as relative intensity. #37 IIVE type (0 spot, 1 omni, 3 area)
#   Value tag sizes: IIVE=4B, IIV1=16B, IIM1=64B. Block boundary: 'cLightObject' (one per block).
def _light_blocks(m):
    tag = 'cLightObject'.encode('utf-16le')
    st = []; i = 0
    while True:
        j = m.find(tag, i)
        if j < 0:
            break
        st.append(j); i = j + len(tag)
    st.append(len(m))
    return st

def decode_lights(m, merge=False):
    """Decode each cLightObject block into position, direction, RGB, intensity, type,
    cone angle, width, and height (Lumion Y-up). Preserve all decodable records
    by default. Enabling merge uses only the legacy,
    lossy proximity merge, which can also remove independent lights."""
    st = _light_blocks(m)
    out = []
    for bi in range(len(st) - 1):
        a, b = st[bi], st[bi + 1]
        vals = []; next_offset = a
        # The C regex implementation searches without a Python loop over every byte.
        for match in re.compile(rb'IIVE|IIV1|IIM1').finditer(m, a, b):
            p = match.start()
            if p < next_offset or p + 8 > b:
                continue
            t = match.group()
            exp = {b'IIVE': 4, b'IIV1': 16, b'IIM1': 64}[t]
            ln = struct.unpack_from('<I', m, p + 4)[0]
            if ln == exp and p + 8 + ln <= b:
                vals.append((t, m[p + 8:p + 8 + ln])); next_offset = p + 8 + ln
        if len(vals) < 37:
            warnings.warn('Lumion light ls:%x skipped: fewer than 37 value records' % a, RuntimeWarning)
            continue
        # Anchor: the single IIM1 (world matrix) is layout #3. Count other fields relative to it:
        # #N = vals[j + (N-3)]. This compensates for shifts caused by extra value chunks in the RTTI section.
        j = next((ix for ix, (tg, d) in enumerate(vals) if tg == b'IIM1'), None)
        if j is None:
            warnings.warn('Lumion light ls:%x skipped: no world matrix' % a, RuntimeWarning)
            continue
        def V(n):
            k2 = j + (n - 3)
            return vals[k2] if 0 <= k2 < len(vals) else (b'', b'')
        mtx = vals[j]
        M = struct.unpack('<16f', mtx[1])
        if not all(math.isfinite(v) for v in M):
            warnings.warn('Lumion light ls:%x skipped: nonfinite world matrix' % a, RuntimeWarning)
            continue
        pos = (M[12], M[13], M[14]); dr = (M[8], M[9], M[10])
        col = V(28); rgb = struct.unpack('<4f', col[1])[:3] if col[0] == b'IIV1' else (1., 1., 1.)
        intensity = (rgb[0] ** 2 + rgb[1] ** 2 + rgb[2] ** 2) ** 0.5
        tp = V(37); kind = struct.unpack('<f', tp[1])[0] if tp[0] == b'IIVE' else 0.0
        cn = V(26); cone = struct.unpack('<f', cn[1])[0] if cn[0] == b'IIVE' else 1.0
        w = V(35); h = V(36)
        wv = struct.unpack('<f', w[1])[0] if w[0] == b'IIVE' else 1.0
        hv = struct.unpack('<f', h[1])[0] if h[0] == b'IIVE' else 1.0
        # NaN or infinite fields would abort later rounding and power steps.
        bad = [name for name, values in (('rgb', rgb), ('type', (kind,)), ('cone', (cone,)),
               ('w', (wv,)), ('h', (hv,)), ('intensity', (intensity,)))
               if not all(math.isfinite(v) for v in values)]
        if bad:
            warnings.warn('Lumion light ls:%x skipped: nonfinite %s' % (a, ', '.join(bad)), RuntimeWarning)
            continue
        kind = int(round(kind))
        defaults = []
        for name, value, tag, fallback in (('rgb', col, b'IIV1', 'white'),
                ('type', tp, b'IIVE', '0'), ('cone', cn, b'IIVE', '1.0'),
                ('w', w, b'IIVE', '1.0'), ('h', h, b'IIVE', '1.0')):
            if value[0] != tag:
                defaults.append(name)
                warnings.warn('Lumion light ls:%x: %s default=%s (missing field/different tag)' %
                              (a, name, fallback), RuntimeWarning)
        out.append({"source_id": "ls:%x" % a, "pos": pos, "dir": dr, "rgb": rgb, "intensity": intensity,
                    "type": kind, "cone": cone, "w": wv, "h": hv, "defaults": defaults})
    if not merge:
        return out
    # The legacy proximity assumption does not prove these are the same light in the source program.
    # Nearby spot and area lights facing the same direction may be independent, so merging is opt-in.
    parse_fields = {}
    for L in out:
        if L["type"] == 3:
            parse_fields.setdefault(tuple(round(c, 3) for c in L["dir"]), []).append(L)
    if not parse_fields:
        return out
    def _is_sample(L):
        for A in parse_fields.get(tuple(round(c, 3) for c in L["dir"]), ()):
            threshold = max(A["w"], A["h"]) / 2.0 + 0.15
            dx = L["pos"][0] - A["pos"][0]
            dy = L["pos"][1] - A["pos"][1]
            dz = L["pos"][2] - A["pos"][2]
            if dx*dx + dy*dy + dz*dz <= threshold*threshold:
                return True
        return False
    return [L for L in out if not (L["type"] == 0 and _is_sample(L))]

def _zup(v):   # Lumion Y-up -> Radiance Z-up: (x,y,z)->(x,-z,y), the same transform for direction and position
    return (v[0], -v[2], v[1])

def _norm(v):
    import math as _mm
    l = _mm.sqrt(v[0]*v[0] + v[1]*v[1] + v[2]*v[2]) or 1.0
    return (v[0]/l, v[1]/l, v[2]/l)

def _cross(a, b):
    return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])

def _perp(n):
    """Generate an orthonormal (u, v) basis perpendicular to n."""
    a = (0.0, 0.0, 1.0) if abs(n[2]) < 0.9 else (1.0, 0.0, 0.0)
    u = _norm(_cross(n, a)); v = _norm(_cross(n, u))
    return u, v


def light_luminaires(m, zup=True, k=200.0):
    """Convert lights to luminaire dictionaries using the project schema.
    Lumion lights then enter the scene through the same path as EVO lights."""
    import math
    decoded_lights = decode_lights(m)
    out = []
    for i, L in enumerate(decoded_lights):
        p = _zup(L["pos"]) if zup else L["pos"]
        d = _norm(_zup(L["dir"]) if zup else L["dir"])
        cone = L["cone"]; angle = cone * 180.0 / math.pi if cone < 6.3 else cone
        angle = min(max(angle, 10.0), 160.0)
        label = {0: "spot", 1: "omni", 3: "area"}.get(L["type"], "spot")
        power = L["intensity"] / k * 1000
        if not math.isfinite(power):
            warnings.warn('Lumion light %s skipped: nonfinite power' % L["source_id"], RuntimeWarning)
            continue
        rec = {
            "name": "lumion_%s_%d" % (label, i),
            "x": round(p[0], 3), "y": round(p[1], 3), "z": round(p[2], 3),
            "color": [1.0, 1.0, 1.0],
            "color_source": "default_neutral",
            "color_note": "Light color in the Lumion file could not be verified. Neutral white was used as the default. This is not the source color temperature.",
            "power": max(1, int(round(power))),
            "radius": 0.1, "type": label, "angle": round(angle, 1),  # Preserve the actual type (spot/omni/area) in the output.
            "source": "lumion", "source_id": L["source_id"],
            "photometry_status": "approximate",
            "defaults": list(L.get('defaults', [])),
        }
        if label == "omni":
            rec["direction"] = None                              # omni lights have no direction
        else:
            rec["direction"] = [round(d[0], 3), round(d[1], 3), round(d[2], 3)]
        if label == "area":                               # area light: panel size
            rec["w"] = round(float(L.get("w", 1.0)), 3)
            rec["h"] = round(float(L.get("h", 1.0)), 3)
        out.append(rec)
    return out
