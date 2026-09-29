#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Import / conversion layer: read a file and convert it to OBJ plus a light list.
Lumion (.ls10), DIALux (.evo), USD, Assimp formats, LDT/IES, GLDF -> OBJ/RAD/IES.
engine.py re-exports this module as a facade: `engine.convert(...)` continues to work.
Core dependencies are used only at call time: engine.sh/processes/converter_status +
engine.ASSIMP_CONVERTER/USD_CONVERTER. The circular import is safe: symbols
are read only at call time, and engine.py loads this module last during import."""
import io, json, os, math, subprocess, tempfile, time, shutil, struct, zlib, hashlib, zipfile
import warnings
import unicodedata
from core.photometry import FILE_NOTICE, PROJECT_NOTICE, annotated_ies


LUMION_EXTENSIONS = {".ls10"}
USD_EXTENSIONS = (".usd", ".usda", ".usdc", ".usdz")


def _lumion_tool():
    """Load tools/ls10_reader from core/tools."""
    from core.tools import ls10_reader as read_lumion
    return read_lumion


def _lumion_obj(source, target):
    """Lumion -> OBJ, preserving all faces and object boundaries."""
    import mmap
    L = _lumion_tool()
    with open(source, "rb") as f:
        # mmap cannot map an empty file, report it clearly instead.
        if os.fstat(f.fileno()).st_size == 0:
            raise RuntimeError("Lumion file is empty: " + os.path.basename(source))
        with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
            matrix = L.single_model_matrix(m)
            from core.tools.ls10_surface import Reader, export, INSTANCE
            if matrix is not None and str(source).lower().endswith('.ls10') and INSTANCE.search(m):
                r = export(Reader(m), target, matrix)
            else:
                r = L.extract_mesh(m, target, zup=True, matrix=matrix, unique=True)
    if not r.get("mesh") or not os.path.exists(target):
        raise RuntimeError("Lumion geometry could not be extracted (no mesh found)")
    if os.path.exists(target + ".full.obj"):
        # Prevent an old .full.obj sidecar from overriding the new model in final renders.
        shutil.copy2(target, target + ".full.obj")
    from core.file_safety import atomic_json
    warnings = ["Lumion import is partial: materials, textures and multiple-object links are not fully resolved.",
                "Light records are not merged automatically. Verify physical luminaire counts and photometry."]
    if r.get("manifest"):
        warnings[0] = ("Lumion colors, UVs and normals were imported. Glass, custom shaders and texture mapping are partial. "
                       "Original material settings and embedded textures are preserved in the import manifest.")
    if matrix is None:
        warnings.append("The model world matrix could not be matched. Geometry may use local coordinates.")
    atomic_json(target + ".import.json", {
        "mesh": r["mesh"], "triangle": r["triangle"], "vertex": r["vertex"],
        "skipped": r["skipped"], "world_matrix": matrix,
        "material_manifest": os.path.relpath(r["manifest"], os.path.dirname(target)) if r.get("manifest") else None,
        "preview_triangles": min(max(r["triangle"], 300000), 3000000), "warnings": warnings})
    return target


def lumion_luminaires(source, k=200.0):
    """Return Lumion lights as luminaire dictionaries (the project dictionary's 'luminaires').
    This adds lights to the scene through the same path as EVO lights."""
    import mmap
    L = _lumion_tool()
    with open(source, "rb") as f:
        if os.fstat(f.fileno()).st_size == 0:
            raise RuntimeError("Lumion file is empty: " + os.path.basename(source))
        with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
            luminaires = L.light_luminaires(m, zup=True, k=k)
    return luminaires


def is_convertible(extension):
    extension = extension.lower(); d = engine.converter_status()
    if extension in LUMION_EXTENSIONS:            # Lumion uses pure Python and can always be converted.
        return True
    if extension in engine.ASSIMP_CONVERTER:
        return d["assimp"] or extension == ".obj"
    if extension in engine.USD_CONVERTER:
        return d["usd"]
    if extension == ".evo":               # STEP rooms/lights use the standard library. Assimp is needed only for embedded FBX.
        return True
    return False


def run_usd_bridge(source, target_obj):
    """Run USD conversion in a separate process -> (has_geometry, lights, message).
    Loading pxr in a worker thread can freeze the entire application on macOS.
    A separate process with a timeout keeps the application responsive if conversion hangs.
    lights: UsdLux list (area/omni/sun/dome), converted to luminaires by usd_luminaires."""
    bridge = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools", "usd_bridge.py")
    ij = target_obj + ".lights.json"
    import sys as _sys
    r = engine.processes.run([_sys.executable, bridge, source, target_obj, ij],
                               timeout=300)
    if r.returncode in (0, 2) and r.stderr.strip():
        warnings.warn('USD bridge: ' + r.stderr.strip()[:1000], RuntimeWarning)
    lights = []
    try:
        if os.path.exists(ij):
            with open(ij) as f:
                lights = json.load(f)
            os.remove(ij)
    except Exception:
        pass
    if r.returncode == 0:
        return True, lights, ""
    if r.returncode == 2:                     # No geometry, but lights were found.
        return False, lights, (r.stdout or "").strip()[:200]
    raise RuntimeError((r.stderr or r.stdout or "USD bridge failed").strip()[:300])


def _usd_obj(source, target):
    """USD -> OBJ through the bridge (the general entry point used by engine.convert)."""
    has_geometry, lights, _ = run_usd_bridge(source, target)
    if not has_geometry:
        extra = ", but %d lights were found (import with Add Project)" % len(lights) if lights else ""
        raise RuntimeError("No mesh geometry in USD (payload/reference "
                           "may be unresolved)" + extra)


def usd_luminaires(lights):
    """Convert the bridge's UsdLux list to project luminaire dictionaries.
    area/omni become luminaires. Sun (DistantLight) and dome (DomeLight HDRI) do
    not. Return (luminaires, notes). Notes are written to the report.
    The power scale is approximate: UsdLux intensity units vary by scene and have not been
    calibrated. power = intensity*100, clamped to 0..5000."""
    luminaires, notes = [], []
    n = 0
    for it in lights:
        record_type = it.get("kind")
        if record_type == "sun":
            notes.append("USD DistantLight '%s' represents the sun. A render sun already exists, so it was skipped"
                          % it.get("name", "?"))
            continue
        if record_type == "dome":
            d = it.get("texture") or ""
            notes.append("USD DomeLight '%s' represents an HDRI sky%s. Sky-module integration is planned"
                          % (it.get("name", "?"), (" (%s)" % os.path.basename(d)) if d else ""))
            continue
        n += 1
        k = it.get("position") or [0, 0, 1]
        luminaires.append({
            "name": it.get("name") or ("usd_light_%d" % n),
            "x": k[0], "y": k[1], "z": k[2],
            "color": it.get("color") or [1, 1, 1],
            "power": max(0, min(5000, round((1.0 if it.get("intensity") is None else it["intensity"]) * 100))),
            "photometry_status": "approximate",
            "usd_intensity": it.get("intensity"),
            "radius": max(0.05, it.get("radius") or 0.35),
            "type": "area" if record_type == "area" else "omni",
            "angle": 120,
            "direction": it.get("direction") if record_type == "area" else None,
            "w": max(0.05, it.get("w") or 0.5),
            "h": max(0.05, it.get("h") or 0.5),
            "source": "usd",
            "kelvin": it.get("kelvin") or None,
        })
    return luminaires, notes


def _usd_obj_native(source, target):
    """USD (.usd/.usda/.usdc/.usdz) -> OBJ. Apply the world transform, normalize cm->meters
    and Y-up->Z-up (Radiance), and write primvars:st UVs and group names
    so lights can be located by name. Requires usd-core (pxr).
    This loads pxr in the current process, so call it only from the usd_bridge.py
    subprocess. External entry points are _usd_obj/run_usd_bridge."""
    from pxr import Usd, UsdGeom, Gf
    stage = Usd.Stage.Open(source)
    if stage is None:
        raise RuntimeError("Could not open USD")
    mpu = UsdGeom.GetStageMetersPerUnit(stage) or 0.01     # Defaults to cm.
    y_up = (UsdGeom.GetStageUpAxis(stage) == UsdGeom.Tokens.y)
    t = Usd.TimeCode.Default()
    V, VT, F = [], [], []                                   # F: (group, [vi], [ti] or None).

    class USDTopologyError(ValueError):
        """Reject invalid indices rather than accepting partial or damaged OBJ output as successful."""

    def visible(prim):
        """Exclude invisible geometry and guide/proxy purposes from the scene."""
        try:
            img = UsdGeom.Imageable(prim)
            if img.ComputeVisibility(t) == UsdGeom.Tokens.invisible:
                return False
            if img.ComputePurpose() in (UsdGeom.Tokens.guide, UsdGeom.Tokens.proxy):
                return False
        except Exception:
            pass
        return True

    def write_mesh(mesh, M, group):
        """Append a UsdGeom.Mesh to V/VT/F using world matrix M."""
        prim = mesh.GetPrim()
        pts = mesh.GetPointsAttr().Get(t)
        counts = mesh.GetFaceVertexCountsAttr().Get(t)
        idx = mesh.GetFaceVertexIndicesAttr().Get(t)
        if not pts or not counts or not idx:
            return
        valid, reason = UsdGeom.Mesh.ValidateTopology(idx, counts, len(pts))
        if not valid:
            raise USDTopologyError("Invalid USD mesh topology %s: %s" % (prim.GetPath(), reason))
        base = len(V)
        for p in pts:
            w = M.Transform(Gf.Vec3d(p[0], p[1], p[2]))     # World coordinates (USD units).
            x, y, z = w[0]*mpu, w[1]*mpu, w[2]*mpu          # Convert to meters.
            if y_up:
                x, y, z = x, -z, y                          # Y-up -> Z-up
            V.append((x + 0.0, y + 0.0, z + 0.0))           # Normalize -0.0 to 0.0 for display.
        # UVs (primvars:st) are for textures. Failures here do not block geometry conversion.
        uvs = interp = None
        try:
            st = UsdGeom.PrimvarsAPI(prim).GetPrimvar("st")
            if st and st.HasValue():
                uvs = st.ComputeFlattened(t)                # Flatten indexed values.
                interp = st.GetInterpolation()
        except Exception:
            uvs = None
        uv_base = len(VT)
        if uvs:
            for uv in uvs:
                VT.append((uv[0], uv[1]))                   # No V flip: USD, OBJ and Radiance all use the lower-left origin.
        # Vertex/varying UVs require at least as many values as points to avoid out-of-range vt indices.
        vertUV = uvs and interp in (UsdGeom.Tokens.vertex, UsdGeom.Tokens.varying) and len(uvs) >= len(pts)
        k = fv = 0
        for c in counts:
            vi = [base + idx[k+j] + 1 for j in range(c)]
            ti = None
            if uvs:
                if interp == UsdGeom.Tokens.faceVarying and (fv + c) <= len(uvs):
                    ti = [uv_base + (fv + j) + 1 for j in range(c)]
                elif vertUV:
                    ti = [uv_base + idx[k+j] + 1 for j in range(c)]
            F.append((group, vi, ti))
            k += c; fv += c

    pred = Usd.TraverseInstanceProxies(Usd.PrimDefaultPredicate)
    # Collect PointInstancer prototypes. Do not write them independently in the main loop
    # (otherwise both prototypes and instances render, duplicating geometry).
    prototype_paths = []
    for prim in stage.Traverse(pred):
        if prim.IsA(UsdGeom.PointInstancer):
            try:
                for pp in UsdGeom.PointInstancer(prim).GetPrototypesRel().GetTargets():
                    prototype_paths.append(pp)
            except Exception:
                pass
    def is_prototype(prim):
        path = prim.GetPath()
        return any(path.HasPrefix(pp) for pp in prototype_paths)
    for prim in stage.Traverse(pred):
        if prim.IsA(UsdGeom.Mesh):
            if not visible(prim) or is_prototype(prim):        # Let the instancer write prototype meshes.
                continue
            write_mesh(UsdGeom.Mesh(prim),
                     UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(t),
                     prim.GetName())
        elif prim.IsA(UsdGeom.PointInstancer) and visible(prim):
            # Scatter (trees/chairs/people): write each instance with its own matrix.
            try:
                pi = UsdGeom.PointInstancer(prim)
                protos = pi.GetPrototypesRel().GetTargets()
                pidx = pi.GetProtoIndicesAttr().Get(t) or []
                # The matrix includes the prototype root's local transform. rel below
                # contains only the child mesh's transform relative to the prototype.
                # IgnoreMask preserves array length and the protoIndices/n correspondence.
                mats = pi.ComputeInstanceTransformsAtTime(
                    t, t, UsdGeom.PointInstancer.IncludeProtoXform,
                    UsdGeom.PointInstancer.IgnoreMask)
                mask = pi.ComputeMaskAtTime(t)  # Empty mask = all instances visible.
                pw = UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(t)  # Instancer world matrix.
                for n, pil in enumerate(pidx):
                    if mask and not mask[n]:
                        continue
                    if pil < 0 or pil >= len(protos) or n >= len(mats):
                        continue
                    proto = stage.GetPrimAtPath(protos[pil])
                    if not proto:
                        continue
                    proto_inv = UsdGeom.Xformable(proto).ComputeLocalToWorldTransform(t).GetInverse()
                    for sub in Usd.PrimRange(proto):
                        if sub.IsA(UsdGeom.Mesh) and visible(sub):
                            rel = UsdGeom.Xformable(sub).ComputeLocalToWorldTransform(t) * proto_inv
                            write_mesh(UsdGeom.Mesh(sub), rel * mats[n] * pw, sub.GetName())
            except USDTopologyError:
                raise
            except Exception:
                pass    # If PointInstancer cannot be resolved, retain the rest of the geometry.
    if not V:
        raise RuntimeError("No mesh geometry in USD (payload/reference may be unresolved)")
    with open(target, "w") as fp:
        for v in V:
            fp.write("v %g %g %g\n" % v)
        for vt in VT:
            fp.write("vt %g %g\n" % vt)
        suffix = None
        for group, vi, ti in F:
            if group != suffix:
                fp.write("g %s\n" % group); suffix = group       # Allow lights to be found by name.
            if ti:
                fp.write("f " + " ".join("%d/%d" % (a, b) for a, b in zip(vi, ti)) + "\n")
            else:
                fp.write("f " + " ".join(str(a) for a in vi) + "\n")
    return target


def ldt_to_ies(source, target, info=None):
    """EULUMDAT (.ldt) -> IES (LM-63-2002, Type C, TILT=NONE). Expand symmetry (Isym) to a full
    360° and convert relative cd/1000lm to absolute cd using total lumens.
    Validated symmetry modes are 0/1/2/4. Unknown or incomplete data raises an explicit error.
    If info is provided, write the source name, lumens, power and CCT to that dictionary."""
    with open(source, "rb") as stream:
        raw = stream.read().decode("latin-1")
    L = [x.strip() for x in raw.replace("\r", "").split("\n")]
    if len(L) < 30:
        raise RuntimeError("LDT is too short or invalid")

    def fi(i):
        try:
            value = float(L[i])
        except (ValueError, IndexError) as error:
            raise RuntimeError('LDT numeric data is missing or invalid: line %d' % (i+1)) from error
        if not math.isfinite(value):
            raise RuntimeError('LDT nonfinite number: line %d' % (i+1))
        return value
    def count(i):
        value = fi(i)
        if value != int(value) or value < 0 or value > 1000000:
            raise RuntimeError('Invalid LDT count/symmetry field: line %d' % (i+1))
        return int(value)
    Isym, Mc, Ng = count(2), count(3), count(5)
    if Mc <= 0 or Ng <= 0 or Mc*Ng > 10000000:
        raise RuntimeError('Invalid LDT angle counts')
    if Isym not in (0, 1, 2, 4):
        raise RuntimeError('LDT symmetry mode has not been validated: %d' % Isym)
    if (Isym == 2 and Mc % 2) or (Isym == 4 and Mc % 4):
        raise RuntimeError('LDT plane count does not match its symmetry')
    name = L[8] or L[9] or os.path.basename(source)
    n_set = count(25)
    if n_set != 1:
        raise RuntimeError('Multiple or missing LDT lamp sets are unsupported')
    b = 26                                  # First lamp set.
    total_lumens = fi(b + 2)
    if total_lumens <= 0:
        raise RuntimeError('LDT total lumens must be positive. No assumed lumens were generated')
    cct = L[b + 3].strip() if b + 3 < len(L) else ""
    try:
        if not math.isfinite(float(cct)) or float(cct) <= 0:
            cct = ""
    except ValueError:
        cct = ""
    watt = fi(b + 5)
    idx = 26 + n_set * 6 + 10               # Lamp sets + 10 room-index ratios.
    C = [fi(idx + i) for i in range(Mc)]; idx += Mc          # C-plane angles.
    G = [fi(idx + i) for i in range(Ng)]; idx += Ng          # Gamma angles.
    nC = {0: Mc, 1: 1, 2: Mc // 2 + 1, 3: Mc // 2 + 1, 4: Mc // 4 + 1}.get(Isym, Mc)
    raw_v = [fi(idx + i) for i in range(nC * Ng)]            # cd/1000lm
    if any(v < 0 for v in raw_v):
        raise RuntimeError('LDT contains negative luminous intensity')
    if any(a >= b for a, b in zip(G, G[1:])) or not (0 <= G[0] <= G[-1] <= 180):
        raise RuntimeError('Invalid LDT gamma angles')
    if any(a >= b for a, b in zip(C, C[1:])) or not (0 <= C[0] <= C[-1] <= 360):
        raise RuntimeError('Invalid LDT C angles')
    stored = [raw_v[c * Ng:(c + 1) * Ng] for c in range(nC)]   # stored[c][g]

    factor = fi(23)
    if factor <= 0:
        raise RuntimeError('LDT luminous-intensity conversion multiplier must be positive')
    if fi(24) != 0:
        raise RuntimeError('LDT measurement tilt is not supported. TILT=NONE was not assumed')
    if Isym in (2, 4) and any(abs(angle - i*360.0/Mc) > 1e-3 for i, angle in enumerate(C)):
        raise RuntimeError('LDT requires symmetric, evenly spaced C planes')
    scale = total_lumens / 1000.0 * factor
    # Expand to all Mc planes according to Isym, in absolute cd.
    def plane(c):                          # c: 0..Mc-1 -> gamma list (cd).
        if Isym == 1:
            src = 0
        elif Isym == 0:
            src = c
        elif Isym == 2:                     # Mirror around C0-C180.
            src = c if c <= Mc // 2 else Mc - c
        elif Isym == 4:                     # Mirror the C0-90 quadrant into all four quadrants.
            q = Mc // 4
            cc = c % (2 * q)
            src = cc if cc <= q else 2 * q - cc
        src = max(0, min(src, nC - 1))
        values = [v * scale for v in stored[src]]
        if not all(math.isfinite(v) for v in values):
            raise RuntimeError('LDT absolute candela calculation overflowed')
        return values

    full_c_angles = C if (Isym == 0 and len(C) == Mc) else [round(i * 360.0 / Mc, 4) for i in range(Mc)]
    if Isym == 1:
        full_c_angles = [0.0]                        # Axial symmetry: one horizontal plane.
        planes = [plane(0)]
    else:
        planes = [plane(c) for c in range(Mc)]
        full_c_angles = full_c_angles + [360.0] if full_c_angles[-1] != 360.0 else full_c_angles
        if len(full_c_angles) == Mc + 1:
            planes.append(planes[0])        # 360 duplicates 0 to close the distribution.

    def header(value):
        text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
        return " ".join("".join(c if 32 <= ord(c) < 127 else " " for c in text).split())

    nH = len(full_c_angles)
    with open(target, "w", encoding="ascii") as f:
        f.write("IESNA:LM-63-2002\n")
        f.write("[TEST] not available\n")
        f.write("[TESTLAB] not available\n")
        f.write("[ISSUEDATE] %s\n" % (header(L[11])[:68] or "not available"))
        f.write("[MANUFAC] %s\n" % (header(L[0])[:70] or "not available"))
        f.write("[LUMINAIRE] %s\n" % (header(name)[:68] or "not available"))
        if cct: f.write("[LAMP] CCT %s\n" % header(cct)[:69])
        f.write("[_SOURCE] Converted from EULUMDAT, Isym=%d\n" % Isym)
        f.write("[_LUMENS] %.17g\n" % total_lumens)
        f.write("".join(s + "\n" for s in FILE_NOTICE))
        f.write("TILT=NONE\n")
        # 10 fields: lamps=1, lumens=-1 (absolute), multiplier=1, Nv, Nh, type=1 (C), units=2 (m), width, length, height.
        f.write("1 -1 1 %d %d 1 2 0 0 0\n" % (Ng, nH))
        f.write("1 1 %g\n" % (watt or 0))
        f.write(" ".join("%g" % g for g in G) + "\n")        # Vertical (gamma).
        f.write(" ".join("%g" % c for c in full_c_angles) + "\n")     # Horizontal (C).
        for c in range(nH):                                  # Candela: all vertical angles for each horizontal angle.
            f.write(" ".join("%g" % v for v in planes[c]) + "\n")
    if info is not None:
        info.update(name=name, lumens=total_lumens, watt=watt, cct=cct)
    return target


def _new_file(target_directory, base, extension, data):
    """Write data to an available name in target_directory and return the name.

    Try _2, _3 when the name is taken. Open in "x" mode to avoid overwriting existing files.
    """
    name, i = base + extension, 2
    while True:
        try:
            with open(os.path.join(target_directory, name), "xb") as f:
                f.write(data)
            return name
        except FileExistsError:
            name = "%s_%d%s" % (base, i, extension)
            i += 1


def extract_gldf(source, target_directory, base=None):
    """Extract IES/LDT/EULUMDAT photometry into target_directory from GLDF (DIALux's open
    luminaire format: ZIP containing product.xml, ldc/ photometry, geo/ L3D and image/).

    source may be a path or a file object. Supply base for file objects. If a name is
    taken, choose _2, _3 without overwriting. Return a list of extracted file paths.
    """
    os.makedirs(target_directory, exist_ok=True)
    extracted = []
    if base is None:
        base = os.path.splitext(os.path.basename(source))[0]
    with zipfile.ZipFile(source) as z:
        for n in z.namelist():
            lower = n.lower()
            if lower.endswith((".ies", ".ldt", ".eulumdat")):
                extension = ".ldt" if lower.endswith((".ldt", ".eulumdat")) else ".ies"
                data = z.read(n)
                name = _new_file(target_directory, "%s_%s" % (base, os.path.splitext(os.path.basename(n))[0]),
                                 extension, annotated_ies(data) if extension == ".ies" else data)
                extracted.append(os.path.join(target_directory, name))
    if not extracted:
        raise RuntimeError("No IES/LDT photometry found in GLDF")
    return extracted


def _ies_metadata_name(base, ies_path):
    """Read the beam angle and lumens from IES and append them to the filename so it
    describes the file for both people and matching code. Return base if unreadable."""
    try:
        import sys
        ap = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools")
        if ap not in sys.path:
            sys.path.insert(0, ap)
        from core.tools import ies_analysis as ies_analysis
        d = ies_analysis.read_ies(ies_path)
        beam, _ = ies_analysis.beam_angle(d["vertical_angles"], d["candela"],
                                        d["n_vertical"], d["n_horizontal"])
        lm = d.get("lumens_per_lamp") or 0
        chunk = []
        if beam:
            chunk.append("%dd" % round(beam * 2))
        if lm > 0:
            chunk.append("%dlm" % round(lm))
        if chunk and not any(p in base for p in chunk):   # Do not append metadata already present in the name.
            return base + "_" + "_".join(chunk)
    except Exception:
        pass
    return base


def extract_photometry_zip(source, target_directory):
    """Extract a manufacturer photometry ZIP into the caller's target directory.
    Flatten IES/LDT/EULUMDAT files into the target root and append IES metadata
    (_NNd_NNNNlm). Extract embedded GLDF files using extract_gldf. Resolve collisions with _2, _3...
    (never overwrite). Skip directory structure, __MACOSX and hidden files. Warn about empty
    or unreadable photometry files. Return the names of added files."""
    os.makedirs(target_directory, exist_ok=True)
    inserted = []
    with zipfile.ZipFile(source) as z:
        for n in z.namelist():
            base_name = os.path.basename(n)
            if not base_name or base_name.startswith(".") or "__MACOSX" in n:
                continue
            lower = base_name.lower()
            if not lower.endswith((".ies", ".ldt", ".eulumdat", ".gldf")):
                continue
            data = z.read(n)
            if not data:
                warnings.warn('Empty ZIP photometry member skipped: %s' % n, RuntimeWarning)
                continue
            base = os.path.splitext(base_name)[0]
            if lower.endswith(".gldf"):                 # Pass embedded GLDF data from memory to its own reader.
                try:
                    for path in extract_gldf(io.BytesIO(data), target_directory, base):
                        inserted.append(os.path.basename(path))
                except Exception as error:
                    warnings.warn('Could not open GLDF in ZIP. Skipped: %s (%s)' % (n, error),
                                  RuntimeWarning)
                continue
            extension = ".ies" if lower.endswith(".ies") else ".ldt"
            if extension == ".ies":
                # Read metadata from a hidden temporary file whose name cannot collide with extracted names.
                fd, temporary = tempfile.mkstemp(prefix=".metadata-", suffix=extension, dir=target_directory)
                try:
                    with os.fdopen(fd, "wb") as f:
                        f.write(data)
                    base = _ies_metadata_name(base, temporary)
                finally:
                    os.remove(temporary)
            inserted.append(_new_file(target_directory, base, extension, annotated_ies(data) if extension == ".ies" else data))
    if not inserted:
        raise RuntimeError("No usable photometry (IES/LDT/GLDF) found in ZIP")
    return inserted


def _evo_obj(source, target):
    """DIALux evo (.evo) is a ZIP that may contain standard FBX files for furniture/geometry.
    Extract all FBX files, convert them with assimp and combine them in a single OBJ
    with one 'g' group per part. Exact scene placement is stored in the Boost-serialized
    ScenegraphScene, so some parts may use local coordinates. This function extracts
    only FBX meshes. The detector separately reads STEP rooms/luminaires and
    product photometry."""
    if not shutil.which("assimp"):
        raise RuntimeError("assimp is not installed (required for evo geometry)")
    tmp = target + "_evotmp"
    os.makedirs(tmp, exist_ok=True)
    bv = bvt = bvn = chunk = 0
    discarded = []
    MAX_EXTENT = 80.0     # Preserve large parts too. Warn so units can be checked.
    try:
        with zipfile.ZipFile(source) as z, open(target, "w") as out:
            fbx_files = [n for n in z.namelist() if n.lower().endswith(".fbx")]
            if not fbx_files:
                raise RuntimeError("No FBX geometry in evo (possibly a different DIALux version)")
            for i, n in enumerate(fbx_files):
                fp = os.path.join(tmp, "p%d.fbx" % i)
                with z.open(n) as src, open(fp, "wb") as f:
                    shutil.copyfileobj(src, f)
                op = os.path.join(tmp, "p%d.obj" % i)
                result = engine.processes.run(['assimp', 'export', fp, op], cwd=tmp)
                if result.returncode != 0 or not os.path.exists(op):
                    warnings.warn('EVO FBX part skipped: %s (%s)' %
                                  (n, (result.stderr or result.stdout)[-300:]), RuntimeWarning)
                    continue
                # Load the part into memory and measure bounds to check units.
                vs, vts, vns, fs = [], [], [], []
                xn = yn = zn = 1e18; xx = yx = zx = -1e18
                with open(op, encoding="latin-1") as f:
                    for ln in f:
                        if ln.startswith("v "):
                            vs.append(ln)
                            try:
                                _, X, Y, Z = ln.split()[:4]
                                X, Y, Z = float(X), float(Y), float(Z)
                                xn = min(xn, X); yn = min(yn, Y); zn = min(zn, Z)
                                xx = max(xx, X); yx = max(yx, Y); zx = max(zx, Z)
                            except ValueError:
                                pass
                        elif ln.startswith("vt "):
                            vts.append(ln)
                        elif ln.startswith("vn "):
                            vns.append(ln)
                        elif ln.startswith("f "):
                            fs.append(ln)
                extent = max(xx - xn, yx - yn, zx - zn) if vs else 0
                name = n.split("/")[-2] if n.count("/") >= 2 else "chunk_%d" % i
                if extent > MAX_EXTENT:
                    warnings.warn('EVO FBX unit limit: %s extent=%.1f. Part preserved' %
                                  (name, extent), RuntimeWarning)
                out.write("g %s\n" % name)
                for ln in vs:
                    out.write(ln)
                for ln in vts:
                    out.write(ln)
                for ln in vns:
                    out.write(ln)
                for ln in fs:
                    y = ["f"]
                    for tok in ln.split()[1:]:
                        a = tok.split("/")
                        s = str(int(a[0]) + bv)
                        if len(a) >= 2:
                            s += "/" + (str(int(a[1]) + bvt) if a[1] else "")
                        if len(a) >= 3:
                            s += "/" + (str(int(a[2]) + bvn) if a[2] else "")
                        y.append(s)
                    out.write(" ".join(y) + "\n")
                bv += len(vs); bvt += len(vts); bvn += len(vns); chunk += 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if chunk == 0:
        raise RuntimeError("Could not convert FBX files in evo")
    if discarded:
        print("evo _evo_obj: discarded parts skipped:", ", ".join(discarded))
    return target


def _evo_step_ents(evo_path):
    """ProjectData.dat (STEP text) -> {id: (type, body)} dictionary."""
    import re
    with zipfile.ZipFile(evo_path) as z:
        if "Project/ProjectData/ProjectData.dat" not in z.namelist():
            return None
        d = z.read("Project/ProjectData/ProjectData.dat").decode("utf-8", "replace")
    from core.step_reader import parse_records
    return parse_records(d)


def _evo_references(body):
    """A quoted #123 is text, not a STEP reference."""
    import re
    outside_path = re.sub(r"'(?:[^']|'')*'", "", body)
    return [int(x) for x in re.findall(r'#(\d+)', outside_path)]


def _evo_text(field):
    """Decode quoted STEP text without inventing names from missing fields or GUIDs."""
    import re
    if not isinstance(field, str) or len(field) < 2 or field[0] != "'" or field[-1] != "'":
        return None
    value = field[1:-1].replace("''", "'")

    def unicode_text(match):
        try:
            return bytes.fromhex(match[2]).decode('utf-16-be' if match[1] == '2' else 'utf-32-be')
        except (ValueError, UnicodeError):
            raise ValueError('Invalid Unicode escape in EVO text') from None

    return re.sub(r'\\X([24])\\([0-9A-Fa-f]+)\\X0\\', unicode_text, value)


def _evo_identity(ents, ident):
    """Separate persistent numeric identity, file-local STEP record and display name."""
    import re
    from core.step_reader import parse_fields
    fields = parse_fields(ents[ident][1])
    source_id = fields[0] if fields and re.fullmatch(r'\d+', fields[0]) else 'step:%d' % ident
    result = {'source_id': source_id, 'source_record': '#%d' % ident}
    title = _evo_text(fields[1]) if len(fields) > 1 else None
    if title and re.fullmatch(r'\{?[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\}?', title):
        result['source_guid'] = title
    elif title:
        # The second field may be a direct name in older or synthetic variants.
        result['source_name'] = title
    for ref in _evo_references(fields[2]) if len(fields) > 2 else []:
        if ents.get(ref, ('',))[0] != 'CommonInformationPropertySet':
            continue
        common = parse_fields(ents[ref][1])
        title = _evo_text(common[3]) if len(common) > 3 else None
        if title:
            result['source_name'] = title
    return result


def _evo_match_prototypes(ents):
    """Instance -> prototype -> photometry representation record, independent of user names."""
    elements, representations = {}, {}
    for _, (kind, body) in ents.items():
        if kind != 'RelDefinesByPrototype':
            continue
        refs = _evo_references(body)
        prototypes = {r for r in refs if ents.get(r, ('',))[0] == 'LuminairePrototype'}
        instances = {r for r in refs if ents.get(r, ('',))[0] == 'LuminaireElement'}
        if instances and len(prototypes) > 1:
            raise ValueError('EVO luminaire product relationship is not unique')
        for instance in instances:
            if not prototypes:
                continue
            prototype = next(iter(prototypes))
            if instance in elements and elements[instance] != prototype:
                raise ValueError('EVO luminaire product relationship conflicts')
            elements[instance] = prototype
    for ident, (kind, body) in ents.items():
        if kind != 'LuminairePrototype':
            continue
        candidates = set()
        for ref in _evo_references(body):
            if ents.get(ref, ('',))[0] == 'PrototypeGeometricRepresentation':
                candidates.update(r for r in _evo_references(ents[ref][1])
                                  if ents.get(r, ('',))[0] == 'LuminairePrototypeRepresentationData')
        if len(candidates) > 1:
            raise ValueError('EVO product photometry representation is not unique')
        if candidates:
            representations[ident] = next(iter(candidates))
    return elements, representations


def _evo_product_metadata(ents, prototype):
    """Read product identity and ArticleName text, not manufacturer CAD or dimensions."""
    from core.step_reader import parse_fields
    identity = _evo_identity(ents, prototype)
    result = {key.replace('source_', 'product_', 1): value for key, value in identity.items()}
    fields = parse_fields(ents[prototype][1])
    names = []
    for ref in _evo_references(fields[2]) if len(fields) > 2 else []:
        if ents.get(ref, ('',))[0] != 'ProductDataPropertySet':
            continue
        for product_ref in _evo_references(ents[ref][1]):
            if ents.get(product_ref, ('',))[0] != 'ProductData':
                continue
            product = parse_fields(ents[product_ref][1])
            code = _evo_text(product[0]) if product else None
            if code:
                result['product_code'] = code  # Source product-code field from ProductData.
            for container_ref in _evo_references(ents[product_ref][1]):
                if ents.get(container_ref, ('',))[0] != 'LanguageDependentTextContainer':
                    continue
                container = parse_fields(ents[container_ref][1])
                if not container or not container[0].startswith('('):
                    continue
                for entry in parse_fields(container[0][1:-1]):
                    if not entry.startswith('('):
                        continue
                    parts = parse_fields(entry[1:-1])
                    if len(parts) != 2 or not parts[1].startswith('('):
                        continue
                    for label in parse_fields(parts[1][1:-1]):
                        pair = parse_fields(label[1:-1]) if label.startswith('(') else []
                        if len(pair) == 2 and pair[0] == '.ArticleName.':
                            title = _evo_text(pair[1])
                            item = {'language': parts[0], 'name': title}
                            if title and item not in names:
                                names.append(item)
    if names:
        result['product_names'] = names
        result['product_name'] = names[0]['name']  # Preserve source order and language information.
    return result


def _evo_helpers(ents):
    """Shared helpers for STEP entities (field splitting, vectors, CoordSys, references)."""
    import re

    from core.step_reader import parse_fields as top_fields

    def vecs(body):
        return [tuple(float(x) for x in g) for g in re.findall(
            r'\(\s*(-?[\d.eE+-]+)\s*,\s*(-?[\d.eE+-]+)\s*,\s*(-?[\d.eE+-]+)\s*\)', body)]

    def cs(i):
        if i in ents and ents[i][0] == "CoordSys3D":
            try:
                fields = top_fields(ents[i][1])
                if len(fields) != 4 or any(not f.startswith('(') or not f.endswith(')') for f in fields):
                    raise ValueError
                v = tuple(tuple(float(x) for x in top_fields(f[1:-1])) for f in fields)
                if any(len(p) != 3 or not all(math.isfinite(x) for x in p) for p in v):
                    raise ValueError
                return v
            except (ValueError, OverflowError):
                raise ValueError('EVO coordinate system must contain four finite 3D vectors') from None
        return None

    def apply(CS, p):
        o, xa, ya, za = CS
        return (o[0]+p[0]*xa[0]+p[1]*ya[0]+p[2]*za[0],
                o[1]+p[0]*xa[1]+p[1]*ya[1]+p[2]*za[1],
                o[2]+p[0]*xa[2]+p[1]*ya[2]+p[2]*za[2])

    def rot(CS, v):
        _, xa, ya, za = CS
        return (v[0]*xa[0]+v[1]*ya[0]+v[2]*za[0],
                v[0]*xa[1]+v[1]*ya[1]+v[2]*za[1],
                v[0]*xa[2]+v[1]*ya[2]+v[2]*za[2])

    def refs(i):
        return _evo_references(ents[i][1]) if i in ents else []
    return top_fields, vecs, cs, apply, rot, refs


def _evo_world_cs(ents):
    """Compose local storey/arrangement/element frames from the root into world coordinates."""
    parse_fields, _, cs, apply, rot, refs = _evo_helpers(ents)
    parents = {}
    # Physical groups take precedence over broader spatial containers.
    for relationship in ("RelContainedInSpatialStructure", "RelAggregates"):
        for i, (record_type, _) in ents.items():
            if record_type == relationship:
                rr = refs(i)
                if rr:
                    for lower in rr[1:]:
                        parents[lower] = rr[0]
    unit = ((0., 0., 0.), (1., 0., 0.), (0., 1., 0.), (0., 0., 1.))
    ready, visit = {}, set()

    def world(i):
        if i in ready:
            return ready[i]
        if i in visit:
            raise ValueError("Cycle in the EVO coordinate hierarchy")
        visit.add(i)
        try:
            local = unit
            if i in ents:
                f = parse_fields(ents[i][1])
                if len(f) > 3:
                    rr = _evo_references(f[3])
                    if rr:
                        local = cs(int(rr[0])) or unit
            if i in parents:
                upper = world(parents[i])
                C = (apply(upper, local[0]), *(rot(upper, v) for v in local[1:]))
            else:
                C = local
            ready[i] = C
            return C
        finally:
            visit.discard(i)
    return world, parents


def evo_luminaires(evo_path, max_luminaires=4000):
    """DIALux .evo ProjectData.dat (STEP) -> luminaires in world coordinates.
    Placement = storey/parent objects ∘ arrangement ∘ element local frame.
    Emission = composed frame's -z axis (downlights face downward)."""
    if type(max_luminaires) is not int or max_luminaires < 0:
        raise ValueError('EVO luminaire limit must be a nonnegative integer')
    ents = _evo_step_ents(evo_path)
    if not ents:
        return []
    top_fields, vecs, cs, apply, rot, refs = _evo_helpers(ents)
    world, parents = _evo_world_cs(ents)
    el2proto, proto2eq = _evo_match_prototypes(ents)
    product_metadata = {p: _evo_product_metadata(ents, p) for p in set(el2proto.values())}

    def local_cs(i):
        f = top_fields(ents[i][1])
        rr = _evo_references(f[3]) if len(f) > 3 else []
        return cs(int(rr[0])) if rr else None

    luminaires, seen = [], set()

    def add(C, entity):
        pos, xa, ya, za = C
        length = math.hypot(*za)
        if not all(math.isfinite(v) for vector in C for v in vector) or not math.isfinite(length) or length <= 0:
            raise ValueError('EVO luminaire world transform or light direction is invalid')
        direction = [-v / length for v in za]   # Emission = world frame's -z axis.
        meta = _evo_identity(ents, entity)
        ident = meta['source_id']
        if ident in seen:
            raise ValueError('Duplicate EVO luminaire source identity')
        seen.add(ident)
        prototype = el2proto.get(entity)
        if prototype is not None:
            meta.update(product_metadata[prototype])
            if prototype in proto2eq:
                meta['product_representation_record'] = '#%d' % proto2eq[prototype]
        parent = parents.get(entity)
        while parent is not None:
            if ents.get(parent, ('',))[0] == 'LuminaireArrangement':
                meta.update({k.replace('source_', 'array_', 1): v
                             for k, v in _evo_identity(ents, parent).items()})
                break
            parent = parents.get(parent)
        # Column-major world matrix: preserve source roll and scale.
        meta['source_world_matrix'] = [*xa, 0.0, *ya, 0.0, *za, 0.0, *pos, 1.0]
        luminaires.append({"name": "luminaire_%s" % ident.replace(':', '_'),
                     "x": pos[0], "y": pos[1], "z": pos[2],
                     "color": [1, 1, 1],
                     "color_source": "default_neutral",
                     "color_note": "The EVO/STEP light color could not be verified. Neutral white was used by default. This is not the source color temperature.",
                     "power": 25000, "radius": 0.18,
                     "type": "spot", "angle": 90, "direction": direction, "source": "evo/STEP", **meta})

    for i, (t, b) in ents.items():
        if t != "LuminaireElement":
            continue
        upper = parents.get(i)
        if not local_cs(i) and (upper not in ents or ents[upper][0] != "LuminaireArrangement"):
            warnings.warn('EVO luminaire position could not be resolved. Skipped: #%d' % i, RuntimeWarning)
            continue
        C = world(i)
        add(C, i)
    if len(luminaires) > max_luminaires:
        warnings.warn('EVO luminaire limit: read=%d, limit=%d, skipped=%d' %
                      (len(luminaires), max_luminaires, len(luminaires)-max_luminaires), RuntimeWarning)
    return luminaires[:max_luminaires]


def evo_ies(evo_path, project_dir, rejected=None):
    """DIALux .evo STEP -> actual IES (candela + lumens) and ies2rad .rad/.dat for each product.
    Chain: LuminaireElement --RelDefinesByPrototype--> LuminairePrototype
    --PrototypeGeometricRepresentation--> (equipment ID) -> LightDistribution (candela)
    + LampTypeChannel (lumens). Run ies2rad from project_dir with relative paths (to handle
    spaces and resolve .dat files). Return {source_id(str): {'rad': relative_path, 'lumens': lm,
    ...product metadata}}. Display names are not matching keys.
    If rejected is provided, invalid product lumens or candela tables do not raise:
    append {'product_representation_record', 'reason', 'usage', ...product metadata} to that list,
    omit that product's IES and continue with other products."""
    import re
    ents = _evo_step_ents(evo_path)
    if not ents:
        return {}
    cache_dir = os.path.join(project_dir, "_cache", "ies")
    rel = "_cache/ies"
    top_fields, vecs, cs, apply, rot, refs = _evo_helpers(ents)

    def R(i):
        return _evo_references(ents[i][1]) if i in ents else []

    def nums(s):
        text = s.strip()
        if not (text.startswith('(') and text.endswith(')')):
            raise ValueError('Invalid EVO photometry numeric array')
        values = [float(x.strip()) for x in text[1:-1].split(',')]
        if not values or not all(math.isfinite(x) for x in values):
            raise ValueError('EVO photometry array is empty or nonfinite')
        return values

    el2proto, proto2eq = _evo_match_prototypes(ents)
    source_ids = {}
    for entity in el2proto:
        ident = _evo_identity(ents, entity)['source_id']
        if ident in source_ids:
            raise ValueError('Duplicate EVO luminaire source identity')
        source_ids[ident] = entity
    product_metadata = {p: _evo_product_metadata(ents, p) for p in set(el2proto.values())}
    eq2dist = {}                         # equipment -> LightDistributionData
    for i, (t, b) in ents.items():
        if t == "LightDistributionConnection":
            r = R(i)
            if not r:
                continue
            for x in r[1:]:
                if x in ents and ents[x][0] == "LightDistribution":
                    dd = [y for y in R(x) if y in ents and ents[y][0] == "LightDistributionData"]
                    if dd:
                        eq2dist[r[0]] = dd[0]; break
    eq2lm = {}                           # equipment -> lumens (fourth LampTypeChannel field).
    equipment_errors = {}                         # Per-product lumens errors when rejected is provided.
    for i, (t, b) in ents.items():
        if t == "LampTypeChannel":
            try:
                eq = R(i)[0]; lm = float(top_fields(b)[3])
            except (IndexError,ValueError,TypeError) as error:
                raise ValueError('Missing or invalid EVO lumens record: #%d' % i) from error
            if not math.isfinite(lm) or lm <= 0:
                error_text = 'EVO lumens must be finite and positive: #%d' % i
                if rejected is None:
                    raise ValueError(error_text)
                equipment_errors[eq] = error_text
                continue
            eq2lm[eq] = lm

    os.makedirs(cache_dir, exist_ok=True)
    eq_rad = {}                          # equipment -> (rad_path, lumens).
    for eq, dd in eq2dist.items():
        try:
            if eq in equipment_errors:
                raise ValueError(equipment_errors[eq])
            f = top_fields(ents[dd][1])
            if len(f) < 4:
                raise ValueError("Incomplete EVO light distribution: #%d" % dd)
            C = nums(f[1]); G = nums(f[2]); cd = nums(f[3])
            if eq not in eq2lm:
                raise ValueError('EVO product has no lumens. 1000 lm was not assumed: #%d' % eq)
            lm = eq2lm[eq]
            if len(cd) != len(C)*len(G) or any(v < 0 for v in cd):
                raise ValueError('Invalid EVO candela array count/value: #%d' % dd)
            for angles,maximum in ((C,360),(G,180)):
                if any(v < 0 or v > maximum for v in angles) or any(a >= b for a,b in zip(angles,angles[1:])):
                    raise ValueError('Invalid EVO photometry angle order/range: #%d' % dd)
            if not all(math.isfinite(v*lm/1000.0) for v in cd):
                raise ValueError('EVO photometry scaling overflowed: #%d' % dd)
        except ValueError as error:
            if rejected is None:
                raise
            # Reject only this product. Retain its identity and scene usage count in the report.
            prototypes = sorted(p for p, e in proto2eq.items() if e == eq and p in product_metadata)
            rejected.append({'product_representation_record': '#%d' % eq, 'reason': str(error),
                               'usage': sum(1 for p in el2proto.values() if proto2eq.get(p) == eq),
                               **(product_metadata[prototypes[0]] if prototypes else {})})
            continue
        ies = os.path.join(cache_dir, "product_%d.ies" % eq)
        with open(ies, "w") as o:
            # Write the four mandatory LM-63-2002 keywords with neutral values, without the source application's name.
            o.write("IESNA:LM-63-2002\n[TEST] not available\n[TESTLAB] not available\n"
                    "[ISSUEDATE] not available\n[MANUFAC] not read from the project record\n")
            o.write("".join(s + "\n" for s in PROJECT_NOTICE))
            o.write("TILT=NONE\n")
            o.write("1 %g 1 %d %d 1 2 0.3 0.3 0.1\n" % (lm, len(G), len(C)))
            o.write("1.0 1.0 0.0\n")
            o.write(" ".join("%g" % g for g in G) + "\n")
            o.write(" ".join("%g" % c for c in C) + "\n")
            per = len(G)
            for ci in range(len(C)):
                block = cd[ci * per:(ci + 1) * per]
                o.write(" ".join("%g" % (v * lm / 1000.0) for v in block) + "\n")
        # Run ies2rad from project_dir with relative paths: .rad references "_cache/ies/product_N.dat"
        # relatively. Spaces in the project path are safe. Rpict resolves it from cwd=project_dir.
        rel_ies = "%s/product_%d.ies" % (rel, eq)
        rel_out = "%s/product_%d" % (rel, eq)
        code, _, error_text = engine.sh('ies2rad -o "%s" "%s"' % (rel_out, rel_ies), cwd=project_dir)
        if code or not os.path.exists(os.path.join(cache_dir, "product_%d.rad" % eq)):
            raise RuntimeError("EVO photometry conversion failed (product %d): %s" % (eq, error_text[-300:]))
        eq_rad[eq] = (rel_out + ".rad", lm)
    out = {}
    for ident, e in source_ids.items():
        p = el2proto[e]
        eq = proto2eq.get(p)
        if eq in eq_rad:
            out[ident] = {"rad": eq_rad[eq][0], "lumens": eq_rad[eq][1],
                          'source_id': ident, 'product_representation_record': '#%d' % eq,
                          **product_metadata[p]}
    return out


def write_evo_photometry(project_dir, target_directory, project_name=None):
    """Copy product photometry (product_*.ies) extracted by evo_ies under project_dir into the
    caller's target_directory. Name: project_<project>_<type>_<beam>d_<lumens>lm_u<eq>.ies,
    where <project> comes from project_name or the project_dir directory name. Do not recopy existing files.
    Warn about unreadable photometry. Data comes from the supplied project file.
    Return newly copied filenames."""
    import sys, re
    ar = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools")
    if ar not in sys.path:
        sys.path.insert(0, ar)
    from core.tools import ies_analysis as ies_analysis
    source_directory = os.path.join(project_dir, "_cache", "ies")
    if not os.path.isdir(source_directory):
        return []
    target = target_directory
    os.makedirs(target, exist_ok=True)
    project_name = re.sub(r'[^\w]+', '_', project_name or os.path.basename(project_dir.rstrip("/")))[:16].lower() or "project"
    written = []
    for f in sorted(os.listdir(source_directory)):
        m = re.match(r'product_(\d+)\.ies$', f)
        if not m:
            continue
        path = os.path.join(source_directory, f)
        try:
            d = ies_analysis.read_ies(path)
            beam, _ = ies_analysis.beam_angle(d["vertical_angles"], d["candela"],
                                            d["n_vertical"], d["n_horizontal"])
            lm = int(round(d["lumens_per_lamp"] or 0))
        except Exception as error:
            warnings.warn('Could not read EVO product photometry. Skipped: %s (%s)' % (f, error), RuntimeWarning)
            continue
        b2 = round((beam or 0) * 2)
        kind = "spot" if 0 < b2 <= 35 else ("downlight" if b2 <= 75 else "wide")
        name = "project_%s_%s_%dd_%dlm_u%s.ies" % (project_name, kind, b2, lm, m.group(1))
        h = os.path.join(target, name)
        if os.path.exists(h):                     # Avoid duplicates when importing the same project again.
            continue
        shutil.copy(path, h)
        written.append(name)
    return written


def evo_rooms(evo_path, target_rad):
    """DIALux .evo ProjectData.dat (STEP) -> room shell (floor + walls + ceiling) as Radiance .rad.
    Space -> PolygonBasedSpaceRepresentationDataPart -> PolyPoint2D floor contour extruded
    by the height. Place in world coordinates using the Space CoordSys."""
    import re
    ents = _evo_step_ents(evo_path)
    if not ents:
        raise RuntimeError("ProjectData.dat is missing")
    top_fields, vecs, cs, apply, rot, refs = _evo_helpers(ents)

    def pp2d(i):                          # PolyPoint2D -> (x, y)
        if i in ents and ents[i][0] == "PolyPoint2D":
            m = re.search(r'\(\s*(-?[\d.eE+-]+)\s*,\s*(-?[\d.eE+-]+)\s*\)\s*$', ents[i][1].strip())
            if m:
                return (float(m.group(1)), float(m.group(2)))
        return None

    world, parents = _evo_world_cs(ents)
    contours = {}
    for i, (record_type, _) in ents.items():
        if record_type == "RelAssociatesStoreyContourBasedSpace":
            rr = refs(i)
            contour = next((r for r in rr if r in ents and ents[r][0] == "StoreyContour"), None)
            for r in rr:
                if contour is not None and r in ents and ents[r][0] == "Space":
                    contours.setdefault(r, []).append(contour)

    def representation(object, record_type):
        f = top_fields(ents[object][1])
        stack = [int(x) for x in re.findall(r'#(\d+)', f[4])] if len(f) > 4 else []
        view = set()
        while stack:
            n = stack.pop()
            if n in view or n not in ents:
                continue
            view.add(n)
            if ents[n][0] == record_type:
                return n
            if "Representation" in ents[n][0]:
                stack.extend(refs(n))
        return None

    def contour_prism(space, chunk):
        import ast
        cc = contours.get(space, [])
        if len(cc) != 1:
            raise ValueError("EVO room contour is not unique or its relationship is missing")
        contour = cc[0]; data = representation(contour, "StoreyContourRepresentationData")
        if data is None:
            raise ValueError("Missing EVO contour representation data")
        f = top_fields(ents[data][1])
        pts = ast.literal_eval(f[6])
        if not isinstance(pts, (tuple, list)) or len(pts) < 3 or any(
            not isinstance(p, (tuple, list)) or len(p) != 2 or
            any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in p) for p in pts):
            raise ValueError("Invalid EVO contour points")
        pts = list(pts)
        if pts[0] == pts[-1]:
            pts.pop()
        if len(pts) < 3:
            raise ValueError("EVO contour has fewer than three vertices")
        pf = top_fields(ents[chunk][1]); h = float(pf[4])
        # This variant requires an identity frame for the child representation. Do not guess unknown placement.
        cr = re.findall(r'#(\d+)', pf[2]); child_cs = cs(int(cr[0])) if cr else None
        unit = ((0., 0., 0.), (1., 0., 0.), (0., 1., 0.), (0., 0., 1.))
        if child_cs != unit:
            raise ValueError("Nonidentity child frames in EVO storey contours are not yet supported")
        if pf[3] == ".Storey.":
            storey = parents.get(space); view = set()
            while storey in ents and ents[storey][0] != "Storey" and storey not in view:
                view.add(storey); storey = parents.get(storey)
            if storey not in ents or ents[storey][0] != "Storey":
                raise ValueError("Parent storey not found for EVO room")
            kd = representation(storey, "StoreyRepresentationData")
            if kd is None:
                raise ValueError("EVO storey height not found")
            h = float(top_fields(ents[kd][1])[6])
        if not math.isfinite(h) or h <= 0:
            raise ValueError("Invalid EVO room height")
        C = world(contour)
        return [apply(C, (x, y, 0.)) for x, y in pts], [apply(C, (x, y, h)) for x, y in pts]

    def add_prism(floor, ceil):
        # Radiance normals follow the right-hand rule for vertex order. All surface types must face inward.
        # Reverse clockwise floor contours first if their normals point away from the ceiling.
        nx = ny = nz = 0.0
        for k, (x, y, z) in enumerate(floor):           # Newell normal.
            x2, y2, z2 = floor[(k+1) % len(floor)]
            nx += (y - y2) * (z + z2); ny += (z - z2) * (x + x2); nz += (x - x2) * (y + y2)
        up = [c - f for c, f in zip(ceil[0], floor[0])]
        if nx * up[0] + ny * up[1] + nz * up[2] < 0:
            floor, ceil = floor[::-1], ceil[::-1]
        polys.append(("floor", floor)); polys.append(("ceiling", list(reversed(ceil))))
        for k in range(len(floor)):
            next_offset = (k+1) % len(floor)
            polys.append(("wall", [floor[next_offset], floor[k], ceil[k], ceil[next_offset]]))

    polys = []   # (World vertex list).
    for i, (t, b) in ents.items():
        if t != "Space":
            continue
        storey_part = representation(i, "StoreyContourBasedSpaceRepresentationDataPart")
        if storey_part is not None:
            add_prism(*contour_prism(i, storey_part))
            continue
        spaceCS = world(i)
        # Find PolygonBasedSpaceRepresentationDataPart in the Space subtree.
        f = top_fields(b)
        stack = [int(r) for r in re.findall(r"#(\d+)", f[4])] if len(f) > 4 else []
        view = set()
        while stack:
            j = stack.pop()
            if j in view or j not in ents:
                continue
            view.add(j); jt, jb = ents[j]
            if jt == "PolygonBasedSpaceRepresentationDataPart":
                jf = top_fields(jb)
                h = 3.0
                height_inferred = False
                for x in jf:
                    try:
                        v = float(x)
                        if 1.5 < v < 30:
                            h = v; height_inferred = True; break
                    except ValueError:
                        pass
                pts = [pp2d(int(r)) for r in re.findall(r'#(\d+)', jb)]
                pts = [p for p in pts if p is not None and all(math.isfinite(v) for v in p)]
                if pts and pts[0] == pts[-1]:
                    pts.pop()
                if len(pts) < 3:
                    raise ValueError('EVO polygon has fewer than three valid vertices')
                add_prism([apply(spaceCS, (p[0], p[1], 0.0)) for p in pts],
                            [apply(spaceCS, (p[0], p[1], h)) for p in pts])
                if height_inferred:
                    height_note = 'EVO polygon room height is unverified: inferred %g m from the first scalar satisfying 1.5 < value < 30; the field has not been identified as a height.' % h
                else:
                    height_note = 'EVO polygon room height is unverified: no scalar satisfies 1.5 < value < 30; using the fallback height of 3 m.'
                warnings.warn(height_note, RuntimeWarning, stacklevel=2)
            elif "Representation" in jt:
                stack.extend(refs(j))
    if not polys:
        raise RuntimeError("No Space/room geometry found in evo")
    # Use wood floor, warm walls and white ceiling materials instead of flat gray.
    with open(target_rad, "w") as fp:
        fp.write("# DIALux evo rooms (STEP ProjectData.dat): floor+walls+ceiling\n")
        # Illustrative material defaults; reflectances were not read from the source project.
        fp.write("void plastic floor_mat 0 0 5 0.33 0.22 0.13 0 0\n")   # Brown wood (rho_v~0.24).
        fp.write("void plastic wall_mat 0 0 5 0.52 0.49 0.44 0 0\n")   # Warm wall (rho_v~0.50).
        fp.write("void plastic ceiling_mat 0 0 5 0.75 0.75 0.73 0 0\n")   # Ceiling (rho_v~0.75).
        mat = {"floor": "floor_mat", "wall": "wall_mat", "ceiling": "ceiling_mat"}
        for n, (kind, poly) in enumerate(polys):
            fp.write("%s polygon room_%d\n0\n0\n%d\n" % (mat[kind], n, len(poly) * 3))
            for v in poly:
                fp.write(" %g %g %g\n" % (v[0], v[1], v[2]))
    return target_rad, len(polys)


def evo_furniture(evo_path, target_rad, max_furniture=4000):
    """DIALux .evo ProjectData.dat (STEP) -> furniture boxes (placement + orientation + dimensions), Radiance .rad.
    Each FurnitureElement's fourth field is its world CoordSys. The fifth is MappedFurniture...
    -> representation data. The final vector gives actual dimensions for ExtrudedPolygon, or scale
    relative to the prototype's native size for prototypes (approximate box). Place boxes inside the room."""
    import re
    if type(max_furniture) is not int or max_furniture < 1:
        raise ValueError('EVO furniture limit must be a positive integer')
    ents = _evo_step_ents(evo_path)
    if not ents:
        raise RuntimeError("ProjectData.dat is missing")
    top_fields, vecs, cs, apply, rot, refs = _evo_helpers(ents)

    world, _ = _evo_world_cs(ents)

    def first_ref(s):
        m = re.search(r'#(\d+)', s)
        return int(m.group(1)) if m else None

    boxes = []   # (Center CoordSys, (sx, sy, sz), exact_dimensions).
    for i, (t, b) in ents.items():
        if t != "FurnitureElement":
            continue
        f = top_fields(b)
        if len(f) < 5:
            warnings.warn('Incomplete EVO furniture fields. Skipped: #%d' % i, RuntimeWarning)
            continue
        C = cs(first_ref(f[3])) if len(f) > 3 else None       # Fourth field = world CoordSys.
        if not C:
            warnings.warn('EVO furniture position could not be resolved. Skipped: #%d' % i, RuntimeWarning)
            continue
        C = world(i)
        dimensions, full = (0.5, 0.5, 0.7), False
        mr = first_ref(f[4]) if len(f) > 4 else None
        if mr and mr in ents:
            dat = first_ref(top_fields(ents[mr][1])[-1])
            if dat and dat in ents:
                dt = ents[dat][0]
                dv = vecs(ents[dat][1])
                if dv:
                    suffix = dv[-1]
                    if dt == "MappedExtrudedPolygonFurnitureGeometricRepresentationData":
                        dimensions, full = suffix, True                # Actual dimensions.
                    else:                                     # Prototype: approximate box from scale.
                        d = [max(0.15, min(2.2, s if abs(s - 1) > 1e-3 else v))
                             for s, v in zip(suffix, (0.55, 0.55, 0.72))]
                        dimensions = tuple(d)
        boxes.append((C, dimensions, full))
    if len(boxes) > max_furniture:
        warnings.warn('EVO furniture limit: read=%d, limit=%d, skipped=%d' %
                      (len(boxes), max_furniture, len(boxes)-max_furniture), RuntimeWarning)
        boxes = boxes[:max_furniture]
    if boxes:
        warnings.warn('EVO furniture geometry was approximated with placement boxes.', RuntimeWarning)
    if not boxes:
        raise RuntimeError("No FurnitureElement found in evo")

    def box_vertices(C, s):
        hx, hy, sz = s[0] / 2.0, s[1] / 2.0, s[2]            # Horizontally centered, base z=0.
        local = [(-hx, -hy, 0), (hx, -hy, 0), (hx, hy, 0), (-hx, hy, 0),
                 (-hx, -hy, sz), (hx, -hy, sz), (hx, hy, sz), (-hx, hy, sz)]
        return [apply(C, p) for p in local]

    face = [(0, 1, 2, 3), (7, 6, 5, 4), (0, 4, 5, 1),
           (1, 5, 6, 2), (2, 6, 7, 3), (3, 7, 4, 0)]         # Bottom, top, four sides.
    with open(target_rad, "w") as fp:
        fp.write("# DIALux evo furniture (STEP ProjectData.dat): placement boxes\n")
        fp.write("void plastic furniture_mat 0 0 5 0.62 0.5 0.38 0 0\n")
        fp.write("void plastic furniture_exact 0 0 5 0.5 0.55 0.6 0 0\n")
        n = 0
        for C, s, full in boxes:
            k = box_vertices(C, s)
            mat = "furniture_exact" if full else "furniture_mat"
            for fy in face:
                fp.write("%s polygon mob_%d\n0\n0\n%d\n" % (mat, n, 4 * 3))
                for idx in fy:
                    v = k[idx]
                    fp.write(" %g %g %g\n" % (v[0], v[1], v[2]))
                n += 1
    return target_rad, len(boxes)


def convert(source, target_obj=None):
    """Convert the source deterministically to OBJ. Return target_obj."""
    source = os.path.abspath(source)
    extension = os.path.splitext(source)[1].lower()
    target_obj = target_obj or (os.path.splitext(source)[0] + ".obj")
    if extension == ".obj":
        return source
    if extension in engine.ASSIMP_CONVERTER:
        from core.assimp_bridge import convert as assimp_convert
        assimp_convert(source, target_obj)
    elif extension in engine.USD_CONVERTER:
        _usd_obj(source, target_obj)
    elif extension == ".evo":
        _evo_obj(source, target_obj)
    elif extension in LUMION_EXTENSIONS:
        _lumion_obj(source, target_obj)
    else:
        raise RuntimeError("No deterministic converter for this format: " + extension)
    return target_obj

# The facade supports either import order. Calls occur after module loading.
from core import engine as engine
