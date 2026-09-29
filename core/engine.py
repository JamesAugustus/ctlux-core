# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""
engine.py: shared core of the Radiance pipeline. Used by the conversion command,
format detector, importer and tools/*. Python 3.9 standard library, with no installation required.
"""

import json, os, math, re, subprocess, shutil, struct, zlib, hashlib
import functools, shlex, uuid, tempfile
from core import render_cache as render_cache
from core import render_parallel as render_parallel
from core import render_progress as render_progress
from core import processes as processes
from core import paths as ctlux_paths
from core.file_safety import atomic_json, internal_path
from core.photometry import ies_source

WINDOWS = (os.name == "nt")

SETTINGS_FILE = str(ctlux_paths.SETTINGS_FILE)


def _settings():
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def find_radiance():
    """Find Radiance binaries: RADIANCE_BIN or settings > PATH > known locations."""
    a = os.environ.get("RADIANCE_BIN") or _settings().get("radiance_bin")
    if a and os.path.isdir(a):
        return a
    r = shutil.which("rpict")
    if r:
        return os.path.dirname(r)
    candidates = [os.path.expanduser("~/radiance/bin"),
               "/usr/local/radiance/bin", "/opt/radiance/bin",
               "/Applications/Radiance/bin",
               r"C:\Radiance\bin", r"C:\Program Files\Radiance\bin",
               r"C:\Program Files (x86)\Radiance\bin"]
    for k in candidates:
        if os.path.isfile(os.path.join(k, "rpict")) or \
           os.path.isfile(os.path.join(k, "rpict.exe")):
            return k
    return None


RADIANCE_BIN = find_radiance() or os.path.expanduser("~/radiance/bin")
RAYPATH = _settings().get("raypath") or (
    ".;" + os.path.join(os.path.dirname(RADIANCE_BIN), "lib") if WINDOWS
    else ".:" + os.path.join(os.path.dirname(RADIANCE_BIN), "lib"))


# Preview calculates direct light only. Draft/final add indirect light with more sampling.
# Parameter names alone do not establish photometric accuracy or standards compliance.
QUALITY = {
    # For direct light and shadow checks, excluding indirect lighting and analysis.
    "preview": "-ab 0 -ds 0 -dt 0.25 -dc 0.25 -dr 0 -ps 8 -pt 0.15 -pj 0 -lw 0.01",
    "draft": "-ab 2 -ad 512 -aa 0.2 -ps 3",
    "medium":   "-ab 3 -ad 1024 -aa 0.15 -as 256 -ps 2 -pj 0.9",
    "final":  "-ab 5 -ad 2048 -aa 0.1 -as 1024 -ar 128 -ps 1 -pj 0.9 -lw 1e-4",
    # Denser sampling for reports. Source photometry, scene validation and convergence
    # testing are still required. This setting is not a compliance certificate.
    "report":  "-ab 6 -ad 4096 -as 1024 -ar 256 -aa 0.1 -ds 0.02 -dt 0.05 -dc 0.75 -dr 3 -lr 8 -lw 1e-5 -ps 1 -pj 0.9",
}


def prepare_environment():
    """Complete PATH, which may be empty when launched from Finder on macOS. Set RAYPATH only in children."""
    # Place the selected distribution first even if it already appears in PATH.
    # Auxiliary directories must not take precedence over the selected Radiance commands.
    existing = os.environ.get("PATH", "").split(os.pathsep)
    candidates = [RADIANCE_BIN, *existing, "/usr/local/bin", "/opt/homebrew/bin",
               os.path.expanduser("~/.local/bin")]
    result = []
    for p in candidates:
        if p and p not in result and os.path.isdir(p):
            result.append(p)
    os.environ["PATH"] = os.pathsep.join(result)


def radiance_environment(env=None):
    """Complete Radiance search paths without changing the caller's environment."""
    env = dict(os.environ if env is None else env)
    env['PATH'] = RADIANCE_BIN + os.pathsep + env.get('PATH', '')
    paths = ['.']
    for path in env.get('RAYPATH', RAYPATH).split(os.pathsep):
        if path and path not in paths:
            paths.append(path)
    lib = os.path.join(os.path.dirname(os.path.realpath(RADIANCE_BIN)), 'lib')
    if os.path.isdir(lib) and lib not in paths:
        paths.append(lib)
    env['RAYPATH'] = os.pathsep.join(paths)
    return env


def sh(cmd, cwd=None, extra_env=None):
    """Run in a shell -> (return code, stdout, stderr)."""
    env = radiance_environment()
    env.update(extra_env or {})
    p = processes.run(cmd, shell=True, cwd=cwd, timeout=3600, env=env)
    return p.returncode, p.stdout, p.stderr


def _sequential_render(fn):
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        with processes.render_queue():
            return fn(*args, **kwargs)
    return wrapped


# ---------------------------------------------------------------- project ----

def save_project(project_dir, data):
    atomic_json(os.path.join(project_dir, "project.json"), data)


# ---------------------------------------------------------------- scene ----

def _rad_command_path(path):
    """Use platform quoting rules for paths in Radiance !command lines."""
    return subprocess.list2cmdline([str(path)]) if WINDOWS else shlex.quote(str(path))


def _geometry_line(project_dir, g, full_geometry=False, mat_rel="material/materials.rad"):
    """Geometry entry -> _build.rad line. Mesh preserves UVs. Rad does not.
    full_geometry=True: use a full-resolution X.full.obj sidecar when present (final/report renders).
    False uses the file selected by the project. Geometry is not simplified here."""
    file = os.path.join(project_dir, g["file"])
    if full_geometry:
        full = file + ".full.obj"
        if os.path.exists(full):
            file = full
    scale = g.get("scale", 1) or 1
    rx    = g.get("rx", 0) or 0
    if g.get("type") == "mesh":
        arguments = '"%s"' % file
        n = 1
        if scale != 1: arguments += " -s %g" % scale; n += 2
        if rx:         arguments += " -rx %g" % rx;   n += 2
        name = os.path.splitext(os.path.basename(g["file"]))[0]
        return "void mesh %s\n%d %s\n0\n0\n" % (name, n, arguments)
    xf = ""
    if scale != 1: xf += "-s %g " % scale
    if rx:         xf += "-rx %g " % rx
    if file.lower().endswith(".obj"):
        # Textured OBJ (UV + map_Kd) -> obj2mesh .rtm (preserves UVs, applies colorpict).
        # Mesh primitives read unquoted paths. A relative _cache path without spaces is safe.
        if os.path.getsize(file) >= 1_000_000 or g.get("render_mesh") or _textured_obj(project_dir, file):
            rtm = _obj_rtm(project_dir, file, mat_rel)
            if rtm:
                name = "m_" + hashlib.md5(file.encode("utf-8")).hexdigest()[:8]
                arguments = rtm
                n = 1
                if scale != 1: arguments += " -s %g" % scale; n += 2
                if rx:         arguments += " -rx %g" % rx;   n += 2
                kind = "instance" if rtm.lower().endswith('.oct') else "mesh"
                return "void %s %s\n%d %s\n0\n0\n" % (kind, name, n, arguments)
        # xform cannot read OBJ (it expects Radiance .rad). Convert with obj2rad at render time.
        # Write temporary output in _cache (mtime cached). Preserve the user's OBJ and create no permanent .rad.
        file = _obj_rad(project_dir, file)
    return '!xform %s%s\n' % (xf, _rad_command_path(file))


def _obj_rad(project_dir, obj_path):
    """Use only complete RAD files with verified content hashes, including for small OBJs."""
    cache = os.path.join(project_dir, "_cache")
    os.makedirs(cache, exist_ok=True)
    tool = shutil.which("obj2rad")
    source_digest = render_cache.file_digest(obj_path)
    key = render_cache.object_digest(["obj2rad-v2", os.path.abspath(obj_path),
        source_digest, render_cache.file_digest(tool) if tool else None])
    rad = os.path.join(cache, "geo_%s.rad" % key)
    if render_cache.valid_output(rad, rad + '.json'):
        return rad
    temporary = rad + '.' + uuid.uuid4().hex + '.tmp'
    try:
        r = processes.run(['obj2rad', os.path.abspath(obj_path)], cwd=project_dir, env=radiance_environment(),
                             stdout_path=temporary, timeout=300)
        if r.returncode or os.path.getsize(temporary) < 50:
            raise RuntimeError("obj2rad failed (%s): %s" %
                               (os.path.basename(obj_path), (r.stderr or "Empty output")[:200]))
        if render_cache.file_digest(obj_path) != source_digest:
            raise RuntimeError("The source changed during OBJ conversion. Try again")
        os.replace(temporary, rad)
        atomic_json(rad + '.json', {'sha256': render_cache.file_digest(rad)})
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)
    return rad


def _textured_obj(project_dir, obj_path):
    """Determine whether this OBJ needs textured rendering: UVs (vt) and map_Kd in its MTL.
    (Untextured OBJ files keep using obj2rad with unchanged behavior.)"""
    uv = False
    mtl_paths = []
    obj_dir = os.path.dirname(obj_path)
    try:
        with open(obj_path, "rb") as source:
            for ln in source:
                if ln[:3] == b"vt ":
                    uv = True
                elif ln[:7] == b"mtllib ":
                    raw = ln[7:].strip().decode("latin-1", "replace")
                    candidates = [raw] if os.path.exists(os.path.join(obj_dir, raw)) else raw.split()
                    mtl_paths += [os.path.join(obj_dir, a) for a in candidates]
    except OSError:
        return False
    if not uv:
        return False
    mtl = _read_mtl(mtl_paths)
    return any(p.get("map") and _find_texture(project_dir, p["map"], p.get("_mtl_path")) for p in mtl.values())


def _obj_rtm(project_dir, obj_path, mat_rel):
    """Prepare the OBJ once as an immutable Radiance mesh identified by its content hash."""
    from core.render_mesh import build
    try:
        return build(project_dir, obj_path, os.path.join(project_dir, mat_rel))
    except (OSError, RuntimeError) as e:
        processes.check_cancellation()
        raise RuntimeError("Radiance geometry preparation failed. The expensive polygon fallback was not started. " + str(e)[-1500:]) from e


def _read_mtl(mtl_paths):
    """Read .mtl files -> {material_name: {Kd,Ks,Ns,d,map}}. Use latin-1 (the same name
    encoding as obj2rad). Skip unknown lines and omit color fields when absent."""
    table = {}
    for path in mtl_paths:
        if not os.path.exists(path):
            continue
        active = None
        try:
            with open(path, "rb") as source:
                for raw in source:
                    ln = raw.decode("latin-1", "replace").rstrip("\r\n")
                    s = ln.strip()
                    if not s or s[0] == "#":
                        continue
                    p = s.split()
                    a = p[0].lower()
                    if a == "newmtl":
                        active = s.split(None, 1)[1].strip() if len(p) > 1 else None
                        if active:
                            table[active] = {"_mtl_path": path}
                    elif active is None:
                        continue
                    elif a in ("kd", "ks") and len(p) >= 4:
                        try:
                            table[active][a] = [float(p[1]), float(p[2]), float(p[3])]
                        except ValueError:
                            pass
                    elif a == "ns" and len(p) >= 2:
                        try: table[active]["ns"] = float(p[1])
                        except ValueError: pass
                    elif a in ("d", "tr") and len(p) >= 2:
                        try:
                            v = float(p[1])
                            table[active]["d"] = (1.0 - v) if a == "tr" else v
                        except ValueError:
                            pass
                    elif a == "map_kd":
                        # The path may be absolute or a Windows path (C:\...\mat0_c.jpg). Keep only the filename.
                        # Search the project's texture/ or model/ directory for the actual file (_find_texture).
                        raw_path = s.split(None, 1)[1].strip() if len(s.split(None, 1)) > 1 else ""
                        if raw_path:
                            y = raw_path.replace("\\", "/")
                            table[active]["map"] = y.rsplit("/", 1)[-1] if (":" in y or y.startswith("/")) else y
        except OSError:
            pass
    return table


def _find_texture(project_dir, name, mtl_path=None):
    """Resolve the MTL-relative path and dedicated texture/model directories within the project boundary."""
    candidates = []
    if mtl_path:
        rel = os.path.relpath(os.path.dirname(mtl_path), project_dir)
        candidates.append(os.path.join(rel, name))
        if rel == "model" or rel.startswith("model" + os.sep):
            candidates.append(os.path.join("texture", os.path.relpath(rel, "model"), name))
    candidates += [os.path.join(lower, name) for lower in ("texture", "model", ".")]
    for candidate in candidates:
        try:
            p = internal_path(project_dir, os.path.normpath(candidate))
        except ValueError:
            continue
        if os.path.isfile(p):
            return p
    return None


def _texture_pic(project_dir, name, mtl_path=None):
    """Convert a texture image (jpg/png/tiff) to Radiance .pic in _cache/texture/, cached by mtime.
    Return a project-relative path (render cwd=project_dir, as absolute paths containing spaces do not
    work in Radiance scenes). Return None if missing or unconvertible: use a flat material color without failing the render."""
    source = _find_texture(project_dir, name, mtl_path)
    if not source:
        return None
    cache = os.path.join(project_dir, "_cache", "texture")
    os.makedirs(cache, exist_ok=True)
    body = hashlib.sha256(os.path.relpath(source, project_dir).encode()).hexdigest()[:20]   # Avoid spaces in the scene path.
    pic = os.path.join(cache, body + ".pic")
    try:
        if os.path.exists(pic) and os.path.getmtime(pic) >= os.path.getmtime(source):
            return os.path.relpath(pic, project_dir)
    except OSError:
        pass
    if source.lower().endswith((".hdr", ".pic")):               # Already in Radiance format.
        shutil.copy(source, pic)
        return os.path.relpath(pic, project_dir)
    intermediate_tif = os.path.join(cache, body + "_tmp.tiff")          # jpg/png -> Pillow -> TIFF -> ra_tiff
    try:
        from PIL import Image
        with Image.open(source) as im:
            im.convert("RGB").save(intermediate_tif, format="TIFF")
        r = processes.run(["ra_tiff", "-r", intermediate_tif, pic], cwd=project_dir, env=radiance_environment())
        code, se = r.returncode, r.stderr
    except (ImportError, OSError, RuntimeError):
        return None
    finally:
        try:
            os.remove(intermediate_tif)
        except OSError:
            pass
    if code != 0 or not os.path.exists(pic) or os.path.getsize(pic) < 100:
        return None
    return os.path.relpath(pic, project_dir)


def _mtl_rad(name, p, pic_rel=None):
    """Convert one .mtl material to a Radiance block with deterministic mapping.
    Kd->diffuse RGB, Ks->specular (0..0.1), Ns->roughness, d<0.95->transparency (trans).
    If pic_rel is provided (map_Kd texture converted to .pic), apply colorpict to plastic.
    UVs come from the mesh (obj2mesh) geometry (Lu/Lv)."""
    short = lambda v: max(0.0, min(1.0, v))
    kd = p.get("kd", [0.55, 0.55, 0.55])
    r, g, b = short(kd[0]), short(kd[1]), short(kd[2])
    ks = p.get("ks", [0.0, 0.0, 0.0])
    specular = min(0.1, max(0.0, max(ks)))                 # Keep plastic specularity low.
    ns = p.get("ns", 32.0)
    roughness = round(short(0.18 * (1.0 - min(ns, 100.0) / 100.0)), 3)  # High Ns = glossy = low roughness.
    d = p.get("d", 1.0)
    if d < 0.95:                                        # Transparent -> trans (avoid black glass).
        convert_to = round(short((1.0 - d) + 0.2), 3)
        return ("void trans %s\n0\n0\n7 %.4g %.4g %.4g %.3f %.3f %.3f 0\n"
                % (name, r, g, b, max(specular, 0.03), max(roughness, 0.02), convert_to))
    if pic_rel:                                         # Textured: image color acts as a multiplier.
        return ("void colorpict %s_dk\n7 red green blue %s . frac(Lu) frac(Lv)\n0\n0\n"
                "%s_dk plastic %s\n0\n0\n5 %.4g %.4g %.4g %.3f %.3f\n"
                % (name, pic_rel, name, name, max(r, 0.9), max(g, 0.9), max(b, 0.9), specular, roughness))
    return "void plastic %s\n0\n0\n5 %.4g %.4g %.4g %.3f %.3f\n" % (name, r, g, b, specular, roughness)


def _complete_materials(project_dir, project):
    """Ensure imported models' usemtl names are defined in materials.rad.
    Undefined materials cause an 'undefined modifier' render error or missing geometry.
    If an adjacent .mtl (mtllib) exists, convert its actual color and gloss. Otherwise use
    neutral gray so every model renders on the first attempt, with its own colors when possible."""
    import re
    material_path = os.path.join(project_dir, project.get("material", "material/materials.rad"))
    try:
        with open(material_path, encoding="latin-1") as f:
            existing = f.read()
    except OSError:
        return
    defined = set(re.findall(r'^\s*\w+\s+\w+\s+([\w.\-]+)', existing, re.M))
    required = set()
    mtl_paths = []
    for g in project.get("geometry", []):
        gp = os.path.join(project_dir, g.get("file", ""))
        if not gp.lower().endswith(".obj") or not os.path.exists(gp):
            continue
        obj_dir = os.path.dirname(gp)
        active_material = None
        active_group = "white"  # obj2rad: use the primary group if usemtl is absent, then fall back to white.
        try:
            with open(gp, "rb") as source:
                for ln in source:
                    if ln[:7] == b"usemtl ":
                        active_material = ln.split(None, 1)[1].strip().decode("latin-1", "replace")
                    elif ln[:2] == b"g " or ln.strip() == b"g":
                        group = ln.split()
                        active_group = group[1].decode("latin-1", "replace") if len(group)>1 else "white"
                    elif ln[:2] == b"f ":
                        required.add(active_material or active_group)
                    elif ln[:7] == b"mtllib ":
                        raw = ln[7:].strip().decode("latin-1", "replace")
                        candidates = [raw] if os.path.exists(os.path.join(obj_dir, raw)) else raw.split()
                        for mm in candidates:
                            mp = os.path.join(obj_dir, mm)
                            if os.path.exists(mp) and mp not in mtl_paths:
                                mtl_paths.append(mp)
        except OSError:
            pass
    missing = sorted(m for m in required if m and m not in defined)
    if not missing:
        return
    mtl = _read_mtl(mtl_paths)
    with open(material_path, "a", encoding="latin-1") as f:
        f.write("\n# Imported model materials (converted from .mtl, with missing colors using neutral "
                "gray). Refine their appearance in the Material editor\n")
        for m in missing:
            # m came from latin-1. Write it unchanged to latin-1 to match the obj2rad name.
            if m in mtl:
                pic = _texture_pic(project_dir, mtl[m]["map"], mtl[m].get("_mtl_path")) if mtl[m].get("map") else None
                f.write(_mtl_rad(m, mtl[m], pic_rel=pic))
            else:
                f.write("void plastic %s\n0\n0\n5 0.55 0.55 0.55 0 0\n" % m)


def _render_tool_signature():
    """Invalidate cached calculations when a tool version or the Radiance .cal library changes."""
    files = []
    for name in ("oconv", "obj2mesh", "obj2rad", "xform", "rpict", "gensky", "ies2rad"):
        path = shutil.which(name)
        if path:
            files.append((path, render_cache.file_digest(path)))
    for directory in radiance_environment()['RAYPATH'].split(os.pathsep):
        if not directory or directory == "." or not os.path.isdir(directory):
            continue
        for root, _, names in os.walk(directory, followlinks=False):
            for name in sorted(names):
                path = os.path.join(root, name)
                if os.path.isfile(path):
                    files.append((path, render_cache.file_digest(path)))
    return files


@_sequential_render
def compile_scene(project_dir, project, sky_text, night_lights=False, label="scene",
                luminaire=True, full_geometry=False):
    """Cache geometry as meshes and scenes as frozen octrees addressed by content.

    The returned path is immutable: changed lights, materials or geometry produce a new file.
    Camera and resolution do not enter the build key. Preserve the previous successful
    output until every stage finishes. Never reuse an incomplete octree.
    """
    from pathlib import Path
    cache = os.path.join(project_dir, "_cache")
    os.makedirs(cache, exist_ok=True)
    render_progress.step('compilation_lock', 'Waiting for scene preparation')
    with render_cache.build_lock:
        processes.check_cancellation()
        render_progress.step('light', 'Preparing luminaire and photometry files')
        # Include photometry sidecars in the signature on the first call too.
        light_files = [("luminaire", generate_luminaire(project_dir, project) if luminaire else None),
                         ("led", generate_led_strips(project_dir, project) if luminaire else None)]
        render_progress.step('source', 'Verifying scene input content hashes')
        source = render_cache.sources(project_dir, project)
        # Avoid scanning millions of faces for material names whenever the camera changes.
        mk = render_cache.object_digest(["materials-v1", source, project.get("geometry", [])])
        ready = os.path.join(cache, "material_check_" + mk + ".json")
        if not os.path.isfile(ready):
            render_progress.step('material', 'Preparing geometry material mappings')
            _complete_materials(project_dir, project)
            source = render_cache.sources(project_dir, project)
            mk = render_cache.object_digest(["materials-v1", source, project.get("geometry", [])])
            atomic_json(os.path.join(cache, "material_check_" + mk + ".json"), {"ready": True})
        scene = {"geometry": project.get("geometry", []),
                 "material": project.get("material", "material/materials.rad"),
                 "luminaires": project.get("luminaires", []) if luminaire else [],
                 "led_strips": project.get("led_strips", []) if luminaire else [],
                 "lights": project.get("lights", []) if night_lights else []}
        key = render_cache.object_digest({"version": "scene-v4", "source": source,
              "scene": scene, "sky": sky_text, "luminaire": luminaire,
              "night_lights": night_lights, "full_geometry": full_geometry,
              "tools": _render_tool_signature(),
              "dynamic": uuid.uuid4().hex if render_cache.has_dynamic_rad(source) else None})
        oct_p = os.path.join(cache, "scene_" + key + ".oct")
        record = oct_p + ".json"
        render_progress.step('scene_cache', 'Verifying the cached Radiance scene')
        if render_cache.valid_scene(project_dir, oct_p, record):
            render_progress.step('scene_ready', 'Loaded the verified Radiance scene from cache',
                                cache={'scene': True})
            return oct_p
        render_progress.step('geometry', 'Preparing scene geometry', cache={'scene': False})
        sky_path = os.path.join(cache, "sky_" + key + ".rad")
        build_p = os.path.join(cache, "build_" + key + ".rad")
        Path(sky_path).write_text(sky_text)
        lines = []
        for order, g in enumerate(scene["geometry"]):
            processes.check_cancellation()
            render_progress.step('geometry', 'Preparing scene geometry',
                                completed=order, total=len(scene['geometry']), unit='file',
                                detail=g.get('file'))
            lines.append(_geometry_line(project_dir, g, full_geometry, scene["material"]))
        render_progress.step('verify_geometry', 'Verifying prepared geometry files',
                            completed=len(lines), total=len(scene['geometry']), unit='file')
        # A frozen main scene does not embed RTM/instance files. Even with a valid main OCT,
        # missing or damaged derived geometry must invalidate the cache hit.
        derived_paths = set(re.findall(r'(?m)^\d+ (_cache/mesh_[0-9a-f]+\.(?:rtm|oct))(?:\s|$)',
                                           '\n'.join(lines)))
        from core.render_mesh import chunk_dependencies
        for rel in list(derived_paths):
            derived_paths.update(chunk_dependencies(project_dir, rel))
        derived = [{"path": rel, "sha256": render_cache.file_digest(internal_path(project_dir, rel))}
                      for rel in sorted(derived_paths)]
        # Copy light generators' working files to separate inputs belonging to this scene.
        for name, path in light_files:
            if path:
                fixed = os.path.join(cache, name + "_" + key + ".rad")
                shutil.copyfile(path, fixed)
                lines.append('!xform %s\n' % _rad_command_path(fixed))
        for lt in scene["lights"]:
            lp = internal_path(project_dir, lt)
            if os.path.exists(lp):
                lines.append('!xform %s\n' % _rad_command_path(lp))
        Path(build_p).write_text("\n".join(lines))
        mat = internal_path(project_dir, scene["material"])
        temporary = oct_p + "." + uuid.uuid4().hex + ".tmp"
        try:
            render_progress.step('oconv', 'Compiling the Radiance scene (oconv)')
            r = processes.run(["oconv", "-f", mat, sky_path, build_p], env=radiance_environment(),
                                  cwd=project_dir, stdout_path=temporary, timeout=3600)
            if r.returncode != 0 or os.path.getsize(temporary) < 64:
                raise RuntimeError("oconv ERROR:\n" + (r.stderr or "Empty output")[-1500:])
            # If user inputs other than intermediate files change during the build,
            # restart with a new cache key. Do not store an incorrect scene.
            render_progress.step('verify_scene', 'Verifying consistency between the compiled scene and its sources')
            suffix = render_cache.sources(project_dir, project)
            previous_k = dict(source); last_k = dict(suffix)
            if last_k != previous_k:
                raise RuntimeError("The source changed during scene preparation. Restart the render")
            os.replace(temporary, oct_p)
            atomic_json(record, {"sha256": render_cache.file_digest(oct_p), "key": key,
                               "derived": derived})
            return oct_p
        finally:
            if os.path.exists(temporary):
                os.remove(temporary)


def _ies_product_rad(project_dir, ies_name, dimmer=1.0):
    """Key by source content and dimmer to avoid reusing outdated photometry."""
    dimmer = float(dimmer)
    if not math.isfinite(dimmer) or dimmer < 0:
        raise ValueError("The photometry multiplier must be finite and nonnegative")
    source = ies_source(project_dir, {"ies": ies_name})
    if not source:
        raise ValueError("IES source not found: " + str(ies_name))
    with open(source, "rb") as f:
        raw = f.read()
    key = hashlib.sha256(raw + repr(dimmer).encode()).hexdigest()[:24]
    cache = os.path.join(project_dir, "_cache", "ies")
    os.makedirs(cache, exist_ok=True)
    rel_out = "_cache/ies/k_" + key
    target = os.path.join(project_dir, rel_out + ".rad")
    if os.path.isfile(target) and os.path.isfile(os.path.join(project_dir, rel_out + ".dat")):
        return rel_out + ".rad"
    with open(os.path.join(project_dir, rel_out + ".ies"), "wb") as f:
        f.write(raw)
    r = processes.run(["ies2rad", "-m", str(dimmer), "-o", rel_out,
                          rel_out + ".ies"], cwd=project_dir, env=radiance_environment())
    if r.returncode or not os.path.isfile(target):
        raise RuntimeError("IES conversion failed: " + r.stderr[-500:])
    return rel_out + ".rad"


def _distribute_leds(strip):
    """Generate evenly spaced LED positions along the strip's vertices.
    Return [(x, y, z), ...]. Use z=0 for 2D vertices."""
    k = strip.get("vertex_points") or []
    if len(k) < 2:
        return []
    def end_vertex(p):
        return (p[0], p[1], p[2] if len(p) > 2 else 0.0)
    kk = [end_vertex(p) for p in k]
    if strip.get("closed") and len(kk) > 2:
        kk = kk + [kk[0]]
    seg, length, L = [], [], 0.0
    for i in range(len(kk) - 1):
        d = tuple(kk[i+1][j] - kk[i][j] for j in range(3))
        l = math.sqrt(sum(c*c for c in d))
        seg.append(d); length.append(l); L += l
    if L < 1e-6:
        return []
    if strip.get("spacing_mode") == "step":
        d = max(0.001, float(strip.get("spacing_m") or 0.0166))
        N = max(2, int(L / d) + 1)
    else:
        N = max(2, int(strip.get("pixel_count") or 60))
    step = L / N if strip.get("closed") else L / (N - 1)
    out, si, accumulator = [], 0, 0.0
    for p in range(N):
        t = p * step
        while si < len(seg) - 1 and t > accumulator + length[si]:
            accumulator += length[si]; si += 1
        u = min(1.0, (t - accumulator) / length[si]) if length[si] > 1e-6 else 0.0
        out.append(tuple(kk[si][j] + seg[si][j] * u for j in range(3)))
    return out


def generate_led_strips(project_dir, project):
    """Convert LEDs in project['led_strips'] to an emissive Radiance .rad file.
    Each LED is a small glow/light source using its pixel color or default warm white.
    Companion to generate_luminaires. Return _cache/_led_strips.rad, or None for an empty list."""
    strips = project.get("led_strips") or []
    if not strips:
        return None
    cache = os.path.join(project_dir, "_cache")
    os.makedirs(cache, exist_ok=True)
    path = os.path.join(cache, "_led_strips.rad")
    was_written = 0
    with open(path, "w") as f:
        f.write("# Generated from the LED editor (generate_led_strips): emissive pixel sources\n")
        for si, s in enumerate(strips):
            leds = _distribute_leds(s)
            if not leds:
                continue
            photo = s.get("photo") or {}
            lm = float(photo.get("lumens_per_pixel") or 1.1)        # Lumens of one pixel at full brightness.
            field = float(photo.get("led_area_m2") or 5e-5)         # Emissive area of one LED.
            half = max(0.004, math.sqrt(field) / 2)                # Small sphere radius.
            colors = ((s.get("color_source") or {}).get("colors")) or []
            # Default color: warm white [1,0.85,0.6].
            vg = [1.0, 0.85, 0.6]
            for i, (x, y, z) in enumerate(leds):
                if i < len(colors) and colors[i]:
                    r, g, b = [max(0, min(255, c)) / 255.0 for c in colors[i][:3]]
                else:
                    r, g, b = vg
                # Perceived brightness -> pixel lumens -> Lambertian radiance (lm/(pi*area)/179).
                Y = 0.265 * r + 0.670 * g + 0.065 * b
                rad = (lm * max(Y, 1e-4)) / (math.pi * field) / 179.0
                mx = max(r, g, b, 1e-4)
                rr, gg, bb = rad * r / mx, rad * g / mx, rad * b / mx
                name = "led_%d_%d" % (si, i)
                # light: direct source for actual lux (illuminates surfaces
                # and appears in the camera). Hundreds of pixels may be slow.
                f.write("void light %s_m\n0\n0\n3 %g %g %g\n" % (name, rr, gg, bb))
                f.write("%s_m sphere %s\n0\n0\n4 %g %g %g %g\n\n" % (name, name, x, y, z, half))
                was_written += 1
    return path if was_written else None


def photometry_rotation(direction, c0=None):
    """xform -rx, -ry, -rz angles in degrees for an ies2rad fixture (nadir -Z, C0 plane +X).

    The nadir turns to direction and the C0 plane to c0. Without c0, world +X projected
    perpendicular to direction is used, world +Y when the light is horizontal along X.
    A downward light therefore keeps C0 on +X, as ies2rad writes it.
    """
    length = math.sqrt(sum(v*v for v in direction))
    if not math.isfinite(length) or length == 0:
        raise ValueError("Invalid light direction")
    z = [-v/length for v in direction]
    candidates = ([c0] if c0 is not None else []) + [[1, 0, 0], [0, 1, 0]]
    for axis in candidates:
        along = sum(a*b for a, b in zip(axis, z))
        x = [a - along*b for a, b in zip(axis, z)]
        norm = math.sqrt(sum(v*v for v in x))
        if math.isfinite(norm) and norm > 1e-6:
            break
    x = [v/norm for v in x]
    y = [z[1]*x[2] - z[2]*x[1], z[2]*x[0] - z[0]*x[2], z[0]*x[1] - z[1]*x[0]]
    # Columns x, y, z form R = Rz(c) Ry(b) Rx(a), the order xform applies -rx -ry -rz.
    b = math.asin(max(-1.0, min(1.0, -x[2])))
    if abs(x[2]) < 1 - 1e-12:
        a, c = math.atan2(y[2], z[2]), math.atan2(x[1], x[0])
    else:
        a, c = 0.0, math.atan2(-y[0], y[1])
    return tuple(math.degrees(v) + 0.0 for v in (a, b, c))


def generate_luminaire(project_dir, project):
    """Generate a positioned light .rad file from project['luminaires'].
    Each luminaire: {name, x, y, z, type(light|illum|spot), color[3], power, radius,
                  direction[3](for spot)}. Empty list -> None.
    Luminaire coordinates are converted to Radiance here."""
    luminaires = project.get("luminaires") or []
    if not luminaires:
        return None
    cache = os.path.join(project_dir, "_cache")
    os.makedirs(cache, exist_ok=True)
    path = os.path.join(cache, "_luminaires.rad")
    with open(path, "w") as f:
        f.write("# Generated from the Light Placement editor. Coordinates synchronized with project.json\n")
        for a in luminaires:
            if a.get("enabled") is False:
                continue
            name = a.get("name", "light")
            x, y, z = a.get("x", 0), a.get("y", 0), a.get("z", 3)
            # Actual IES photometry (evo_ies): rotate and place the product .rad along its emission direction.
            # Turn the -Z-facing ies2rad fixture to direction and C0, then move it to the position.
            product = a.get("product_rad")
            dimmer = float(a.get("dimmer", 1.0))
            if not math.isfinite(dimmer) or dimmer < 0:
                raise ValueError("Invalid photometry multiplier")
            if dimmer == 0:
                continue
            if a.get("ies"):
                product = _ies_product_rad(project_dir, a["ies"], dimmer)
            elif product:
                source = ies_source(project_dir, a)
                if source:
                    product = _ies_product_rad(project_dir, os.path.relpath(source, project_dir), dimmer)
                elif dimmer != 1 or not os.path.isfile(internal_path(project_dir, product)):
                    raise ValueError("Missing luminaire source photometry: " + name)
            if product:
                rx, ry, rz = photometry_rotation(a.get("direction") or [0, 0, -1], a.get("c0_direction"))
                # xform changes to the directory containing _luminaires.rad when reading it.
                # The nested command path is relative to this file. The .dat path is relative to the render cwd.
                local_product = os.path.relpath(internal_path(project_dir, product), cache)
                f.write('!xform -rx %.12g -ry %.12g -rz %.12g -t %g %g %g %s\n'
                        % (rx, ry, rz, x, y, z, _rad_command_path(local_product)))
                continue
            r, g, b = (a.get("color") or [1, 1, 1])
            power = a.get("power", 1.0)
            half = a.get("radius", 0.15)
            kind = a.get("type", "light")
            rr, gg, bb = r * power, g * power, b * power
            if kind == "spot":
                dx, dy, dz = (a.get("direction") or [0, 0, -1])
                angle = a.get("angle", 60)
                f.write("void spotlight %s_m\n0\n0\n7 %g %g %g %g %g %g %g\n"
                        % (name, rr, gg, bb, angle, dx, dy, dz))
            elif kind == "illum":
                f.write("void illum %s_m\n1 void\n0\n3 %g %g %g\n" % (name, rr, gg, bb))
            else:                                     # light / omni / area -> nondirectional emission material
                f.write("void light %s_m\n0\n0\n3 %g %g %g\n" % (name, rr, gg, bb))
            # Source geometry: spot -> disk facing the emission direction (thin from the side,
            # avoids a large black sphere outside the cone, like a downlight aperture). Area -> perpendicular
            # w×h polygon (panel/strip luminaire, same logic as Lumion type 3). light/illum/omni -> sphere.
            if kind == "spot":
                dx, dy, dz = (a.get("direction") or [0, 0, -1])
                f.write("%s_m ring %s\n0\n0\n8 %g %g %g %g %g %g 0 %g\n\n"
                        % (name, name, x, y, z, dx, dy, dz, half))
            elif kind in ("area", "area"):
                dx, dy, dz = (a.get("direction") or [0, 0, -1])
                L = math.sqrt(dx*dx + dy*dy + dz*dz) or 1.0
                dx, dy, dz = dx/L, dy/L, dz/L
                w = float(a.get("w") or 2*half); h = float(a.get("h") or 2*half)
                rfx, rfy, rfz = ((1, 0, 0) if abs(dz) > 0.9 else (0, 0, 1))   # Reference vector (not parallel to direction).
                ux, uy, extension = dy*rfz - dz*rfy, dz*rfx - dx*rfz, dx*rfy - dy*rfx   # u = direction x ref
                Lu = math.sqrt(ux*ux + uy*uy + extension*extension) or 1.0; ux, uy, extension = ux/Lu, uy/Lu, extension/Lu
                vx, vy, vz = dy*extension - dz*uy, dz*ux - dx*extension, dx*uy - dy*ux         # v = direction x u
                vertex = []
                for current, sv in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
                    vertex += [x + ux*current*w/2 + vx*sv*h/2, y + uy*current*w/2 + vy*sv*h/2, z + extension*current*w/2 + vz*sv*h/2]
                f.write("%s_m polygon %s\n0\n0\n12 " % (name, name)
                        + " ".join("%g" % c for c in vertex) + "\n\n")
            else:
                f.write("%s_m sphere %s\n0\n0\n4 %g %g %g %g\n\n"
                        % (name, name, x, y, z, half))
    return path


def _wire_source(gp):
    """.rtm (binary obj2mesh output) cannot be parsed as text.
    Use the adjacent original .obj to read geometry/bounds (same vertices, with
    rendering still using the textured .rtm)."""
    if gp.lower().endswith(".rtm"):
        lower = gp[:-4] + ".obj"
        if os.path.exists(lower):
            return lower
    return gp


def _transform_point(p, scale, rx):
    """Geometry-entry transform: scale plus rx degrees around the X axis.
    Exactly matches Radiance `xform -rx`. Handling only rx==90 and treating other angles as
    identity would make the render disagree with CTScene."""
    x, y, z = p[0] * scale, p[1] * scale, p[2] * scale
    if not rx:
        return (x, y, z)
    r = math.radians(rx); c, s = math.cos(r), math.sin(r)
    return (x, y * c - z * s, y * s + z * c)


# ---------------------------------------------- deterministic converters ----
# Prepare models for Radiance deterministically: source -> OBJ -> (obj2mesh) -> .rtm.

ASSIMP_CONVERTER = {".fbx", ".dae", ".gltf", ".glb", ".stl", ".3ds", ".obj"}
USD_CONVERTER = {".usd", ".usda", ".usdc", ".usdz"}


def converter_status():
    """Check installed converters with find_spec without loading native modules such as pxr."""
    import importlib.util
    present = lambda name: importlib.util.find_spec(name) is not None
    return {"assimp": bool(shutil.which("assimp")), "usd": present("pxr")}


def luminaire_bbox(project_dir, project):
    """Return scene bounds: project['bbox'], geometry bounds, or target±radius as a fallback."""
    # Measure bounds from geometry with getbbox, falling back to target±radius.
    bb = project.get("bbox")
    if not bb:
        combined = None                     # Union of all geometry (room + furniture + OBJ).
        for g in (project.get("geometry") or []):
            try:
                gp = _wire_source(internal_path(project_dir, g.get("file", "")))
                if not os.path.exists(gp):
                    continue
                scale = g.get("scale", 1) or 1
                rx = g.get("rx", 0) or 0
                xf = ("-s %g " % scale if scale != 1 else "") + ("-rx %g " % rx if rx else "")
                tool = "obj2rad" if gp.lower().endswith(".obj") else "cat"
                _, so, _ = sh('%s %s | xform %s| getbbox -h' % (tool, _rad_command_path(gp), xf))
                v = [float(x) for x in so.split()]
                if len(v) >= 6:
                    v = v[:6]
                    combined = v if combined is None else [
                        min(combined[0], v[0]), max(combined[1], v[1]),
                        min(combined[2], v[2]), max(combined[3], v[3]),
                        min(combined[4], v[4]), max(combined[5], v[5])]
            except Exception:
                continue
        bb = combined
    if not bb:
        h = project.get("target") or [0, 0, 0]
        r = project.get("scene_radius", 30)
        bb = [h[0] - r, h[0] + r, h[1] - r, h[1] + r, 0, h[2] * 2 or 15]
    return bb


def auto_frame(project_dir, project, save=True):
    """Derive missing target/scene_radius from actual geometry bounds and write project.json.
    Avoid assuming imported models are at the origin or eye level and aiming the camera into empty space
    (previously, out-of-frame models could appear as an 'overexposed slice').
    target = bounds center. Scene_radius = camera distance that fits the model in the FOV.
    Preserve an existing target from user settings or room setup."""
    if project.get("target"):
        return project
    bb = luminaire_bbox(project_dir, project)          # [xmin,xmax,ymin,ymax,zmin,zmax]
    if not bb or len(bb) < 6:
        return project
    cx, cy, cz = (bb[0] + bb[1]) / 2.0, (bb[2] + bb[3]) / 2.0, (bb[4] + bb[5]) / 2.0
    dx, dy, dz = bb[1] - bb[0], bb[3] - bb[2], bb[5] - bb[4]
    diag = math.sqrt(dx * dx + dy * dy + dz * dz)
    if diag <= 0:
        return project
    # Fit the bounding sphere inside half of FOV(45): distance = r / sin(22.5°) + 15% margin.
    distance = (diag / 2.0) / math.sin(math.radians(22.5)) * 1.15
    project["target"] = [round(cx, 3), round(cy, 3), round(cz, 3)]
    project["scene_radius"] = round(max(distance, 3.0), 2)
    if save:
        try:
            save_project(project_dir, project)
        except Exception:
            pass
    return project


def free_view(vp, vd, fov=50, fisheye=False):
    """Create a view directly from camera position and viewing vector.
    fisheye=True -> 180° fisheye (for glare/DGP-UGR, -vta)."""
    if fisheye:
        return ("-vta -vp %g %g %g -vd %g %g %g -vu 0 0 1 -vh 180 -vv 180"
                % (vp[0], vp[1], vp[2], vd[0], vd[1], vd[2]))
    return ("-vtv -vp %g %g %g -vd %g %g %g -vu 0 0 1 -vh %g -vv %g"
            % (vp[0], vp[1], vp[2], vd[0], vd[1], vd[2], fov, fov * 0.75))


def view(project, view_azimuth=180, distance=None, eye=1.6, fov=45, record_type="perspective"):
    """Camera aimed at a target point -> rpict view arguments."""
    target = project.get("target") or [0, 0, 0]
    distance = distance or project.get("scene_radius", 30)
    tx, ty, tz = target
    a = math.radians(view_azimuth)
    cx = tx + distance * math.sin(a)
    cy = ty + distance * math.cos(a)
    cz = eye
    vd = (tx - cx, ty - cy, tz - cz)
    vt = {"perspective": "-vtv", "fisheye": "-vta", "plan": "-vtl"}.get(record_type, "-vtv")
    if record_type == "plan":
        cx, cy, cz = tx, ty, distance + tz
        vd = (0, 0, -1)
    if record_type == "fisheye":
        return "%s -vp %g %g %g -vd %g %g %g -vu 0 0 1 -vh 180 -vv 180" % (
            vt, cx, cy, cz, vd[0], vd[1], vd[2])
    return "%s -vp %g %g %g -vd %g %g %g -vu 0 0 1 -vh %g -vv %g" % (
        vt, cx, cy, cz, vd[0], vd[1], vd[2], fov, fov * 0.75)


def _ambient_file(oct_p, settings=""):
    """SHA-256 ambient key derived from octree bytes and calculation settings.
    Changes to external texture/cal dependencies also need tracking."""
    try:
        h = hashlib.sha256(str(settings).encode("utf-8"))
        # The scene name derived from content also reflects external RTM/.dat/.cal/texture changes.
        h.update(os.path.abspath(oct_p).encode("utf-8"))
        with open(oct_p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return os.path.join(os.path.dirname(oct_p), "amb_%s.amb" % h.hexdigest()[:12])
    except OSError:
        return None


@_sequential_render
def render(oct_p, view_string, quality="draft", W=1000, H=750, out_hdr=None,
           irradiance=False):
    out_hdr = out_hdr or (os.path.splitext(oct_p)[0] + ".hdr")
    # quality: a QUALITY key (draft/medium/final) or raw rpict arguments
    # (such as "-ab 4 -ad 2048...", manually entered ray tracing settings).
    if quality in QUALITY:
        q = QUALITY[quality]
    elif isinstance(quality, str) and "-" in quality:
        q = quality
    else:
        q = QUALITY["draft"]
    if not (1 <= int(W) <= 16384 and 1 <= int(H) <= 16384):
        raise ValueError("Render dimensions must be between 1 and 16384")
    if quality == "preview" and irradiance:
        raise ValueError("Direct preview cannot be used for measurement or analysis")
    ir = "-i " if irradiance else ""
    amb = _ambient_file(oct_p, q + " " + ir) if quality != "preview" else None
    args = shlex.split(view_string) + shlex.split(q)
    if irradiance:
        args += ["-i"]
    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(oct_p)))
    out_hdr = os.path.abspath(out_hdr)
    temporary = out_hdr + "." + uuid.uuid4().hex + ".tmp"
    plan = render_parallel.plan_render(W, H, quality, oct_p, args=args, cwd=project_dir)
    job_root = os.path.join(project_dir, '_cache', 'render_jobs')
    os.makedirs(job_root, exist_ok=True)
    job_dir = tempfile.mkdtemp(prefix='job_', dir=job_root)
    job_log = os.path.join(job_dir, 'rpict.log')
    token = render_progress.start(plan['engine'], workers=plan['workers'],
        total_tiles=plan['total_tiles'], status_path=os.path.join(job_dir, 'progress.json')
        if plan['engine'] == 'rpiece' else None, log_path=job_log if plan['engine'] == 'rpict' else None,
        ambient_path=amb, memory_budget=plan.get('memory_budget'), worker_memory=plan.get('worker_memory'))
    try:
        if plan['engine'] == 'rpiece':
            r = render_parallel.run(plan, args, oct_p, project_dir, temporary, amb, job_dir)
        else:
            cmd = ([] if WINDOWS else ["nice", "-n", "10"]) + ["rpict", *args]
            if amb:
                cmd += ["-af", os.path.relpath(amb, project_dir)]
            cmd += ["-t", "5", "-e", os.path.relpath(job_log, project_dir), "-x", str(int(W)), "-y", str(int(H)), os.path.abspath(oct_p)]
            env = radiance_environment(); env['TMPDIR'] = job_dir
            r = processes.run(cmd, cwd=project_dir, env=env, stdout_path=temporary, timeout=3600)
        if r.returncode != 0 or not os.path.isfile(temporary) or not os.path.getsize(temporary):
            error = r.stderr or ''
            if not error and os.path.isfile(job_log):
                with open(job_log, 'rb') as f:
                    f.seek(max(0, os.fstat(f.fileno()).st_size - 1500))
                    error = f.read().decode('utf-8', errors='replace')
            raise RuntimeError("%s ERROR:\n%s" % (plan['engine'], (error or "Empty output")[-1500:]))
        processes.check_cancellation()
        os.replace(temporary, out_hdr)
    finally:
        render_progress.finish(token)
        if os.path.exists(temporary):
            os.remove(temporary)
    return out_hdr


def _read_ppm(path):
    """binary P6 PPM -> (w, h, rgb_bytes)."""
    with open(path, "rb") as f:
        data = f.read()
    if not data.startswith(b"P6"):
        raise RuntimeError("Not a PPM file: " + path)
    # Header: P6 <w> <h> <maxval> (may contain # comments).
    i, parse_fields = 2, []
    while len(parse_fields) < 3:
        while i < len(data) and data[i:i+1].isspace():
            i += 1
        if data[i:i+1] == b"#":
            while i < len(data) and data[i:i+1] != b"\n":
                i += 1
            continue
        j = i
        while j < len(data) and not data[j:j+1].isspace():
            j += 1
        parse_fields.append(int(data[i:j]))
        i = j
    i += 1  # One whitespace character after the header.
    w, h, maximum = parse_fields
    raw = data[i:i + w * h * 3]
    if maximum != 255:  # Approximate 16-bit data as 8-bit.
        raw = bytes(b for k, b in enumerate(data[i:i + w * h * 6]) if k % 2 == 0)
    return w, h, raw


def write_png(w, h, rgb, out_png):
    """Pure Python PNG writer (8-bit RGB). No sips/ImageMagick required.
    Works the same on Windows and Mac."""
    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data +
                struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff))
    lines = b"".join(b"\x00" + rgb[y * w * 3:(y + 1) * w * 3]
                        for y in range(h))
    with open(out_png, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)))
        f.write(chunk(b"IDAT", zlib.compress(lines, 6)))
        f.write(chunk(b"IEND", b""))
    return out_png


def hdr_to_png(hdr_p, out_png, exposure=0.5, automatic=False):
    """Exposure -> PPM -> PNG (pure Python, platform independent).
    automatic=True -> pcond -h (human vision). Zero energy stays black."""
    ppm = os.path.abspath(os.path.splitext(out_png)[0] + ".ppm")
    # pcond starts pfilt in its own shell without quoting the name. Run in the HDR
    # directory and pass only its filename so directory names containing $( ) cannot execute commands.
    directory, name = os.path.split(os.path.abspath(hdr_p))
    hdr_q, ppm_q = _rad_command_path(name), _rad_command_path(ppm)
    if automatic:
        code, ex, se = sh('pextrem %s' % hdr_q, cwd=directory)
        if code != 0:
            raise RuntimeError("exposure ERROR:\n" + se)
        black = all(float(v) == 0 for v in ex.strip().splitlines()[-1].split()[-3:])
        if black:
            code, _, se = sh('ra_ppm %s > %s' % (hdr_q, ppm_q), cwd=directory)
        else:
            code, _, se = sh('pcond -h %s | ra_ppm > %s' % (hdr_q, ppm_q), cwd=directory)
    else:
        # Manual exposure: do not divide by mean brightness. Black or sparse HDR data is valid.
        code, _, se = sh('pfilt -1 -e %g %s | ra_ppm > %s' % (exposure, hdr_q, ppm_q), cwd=directory)
    if code != 0 or not os.path.exists(ppm):
        raise RuntimeError("exposure ERROR:\n" + se)
    w, h, rgb = _read_ppm(ppm)
    write_png(w, h, rgb, out_png)
    try:
        os.remove(ppm)
    except OSError:
        pass
    return out_png


def falsecolor_png(hdr_p, out_png, unit="Lux"):
    """Irradiance HDR -> falsecolor lux-map PNG (color scale + values).
    The HDR must be produced with rpict -i (irradiance). Falsecolor converts W/m² to lux using ×179 by default.
    Fit the scale to the scene maximum to avoid saturated yellow output."""
    scale = ""
    try:                                   # Automatic upper scale = maximum irradiance ×179.
        _, ex, _ = sh('pextrem %s' % _rad_command_path(hdr_p))
        rgb = ex.strip().splitlines()[-1].split()[-3:]
        mx = max(float(v) for v in rgb) * 179.0
        if mx > 1:
            scale = "-s %g " % mx
    except Exception:
        pass
    ppm = os.path.splitext(out_png)[0] + ".ppm"
    # falsecolor pastes the label and picture name into its own unquoted shell commands.
    # Restrict the label and feed the HDR on stdin so no path reaches that shell.
    if not re.fullmatch(r"[\w./%-]+", str(unit)):
        raise ValueError("Invalid falsecolor unit")
    # Its File::Temp directory under TMPDIR also enters those commands unquoted.
    extra = None
    if not re.fullmatch(r"[\w./-]*", radiance_environment().get("TMPDIR", "")):
        safe = next((d for d in ("/tmp", "/var/tmp") if os.path.isdir(d) and os.access(d, os.W_OK)), None)
        extra = {"TMPDIR": safe} if safe else None
    code, _, se = sh('falsecolor -l %s %s< %s | ra_ppm > %s'
                     % (shlex.quote(unit), scale, _rad_command_path(hdr_p), _rad_command_path(ppm)), extra_env=extra)
    if code != 0 or not os.path.exists(ppm) or not os.path.getsize(ppm):
        raise RuntimeError("falsecolor ERROR:\n" + se)
    w, h, rgb = _read_ppm(ppm)
    write_png(w, h, rgb, out_png)
    try:
        os.remove(ppm)
    except OSError:
        pass
    return out_png


def pextrem(hdr_p):
    _, so, _ = sh('pextrem %s' % _rad_command_path(hdr_p))
    return so.strip()


# ==================== import / converter layer ====================
# The implementation is in importer.py. The re-export below preserves the `engine.X`
# entry points used by the detector and conversion command. Keep this import last:
# the core must be fully defined when importer imports engine.
from core.importer import (  # noqa: E402
    lumion_luminaires, is_convertible, ldt_to_ies, _evo_step_ents, evo_luminaires, evo_ies, evo_rooms, evo_furniture, convert, run_usd_bridge, usd_luminaires, USD_EXTENSIONS,
)
