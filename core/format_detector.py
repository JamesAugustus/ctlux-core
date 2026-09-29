# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""
File detector: identify files in a directory and produce status and warnings.
Warn early about sources such as Revit/DIALux/Rhino/SketchUp/3ds Max to avoid
unexpected issues mid-operation. Read EVO components for the conversion command
(_place_evo) without moving files.

status values:
  ready        -> directly usable, copied into the project
  convertible     -> converted locally and copied
  export_required -> source application export required (warning includes instructions)
  unusable        -> unusable (reason plus alternative when available)
"""
import os, zipfile
from core import engine as engine  # Deterministic converters (assimp/usd-core).


def is_photometry_package(path):
    """Identify manufacturer photometry ZIPs (containing IES/LDT/EULUMDAT/GLDF rather
    than a project package). The `products` command extracts them into IES files."""
    try:
        if not zipfile.is_zipfile(path):
            return False
        with zipfile.ZipFile(path, "r") as z:
            names = z.namelist()
            if "project.json" in names:
                return False
            return any(a.lower().endswith((".ies", ".ldt", ".eulumdat", ".gldf"))
                       for a in names if "__MACOSX" not in a)
    except Exception:
        return False

# Sources convertible to OBJ using assimp/usd-core ("convertible" when installed).
CONVERTIBLE_FORMATS = {
    ".fbx": "FBX (assimp)", ".dae": "Collada (assimp)", ".gltf": "glTF (assimp)",
    ".glb": "glTF (assimp)", ".stl": "STL (assimp)", ".3ds": "3DS (assimp)",
    ".usd": "USD (usd-core)", ".usda": "USD (usd-core)", ".usdc": "USD (usd-core)",
    ".usdz": "USDZ (usd-core)",
}

# Extension -> (status, target directory, type name, message).
TABLE = {
    ".obj":  ("ready", "model", "Model (OBJ)",
              "Use directly. Include an adjacent .mtl file for material names."),
    ".mtl":  ("ready", "model", "Material list (MTL)",
              "OBJ companion file used to build the material mapping table."),
    ".rad":  ("ready", "model", "Radiance geometry",
              "Use directly."),
    ".rtm":  ("ready", "model", "Radiance mesh",
              "Use directly with UVs preserved."),
    ".ies":  ("ready", "light", "Luminaire photometry (IES)",
              "Use directly. Metadata is analyzed automatically."),
    ".jpg":  ("convertible", "texture", "Image (JPG)",
              "Convert textures to .pic. Names beginning with 'ref_' indicate reference images."),
    ".jpeg": ("convertible", "texture", "Image (JPG)", "Convert textures to .pic."),
    ".png":  ("convertible", "texture", "Image (PNG)",
              "Convert textures to .pic. The 'ref_' prefix indicates a reference image."),
    ".tif":  ("convertible", "texture", "Image (TIFF)", "Convert to .hdr using ra_tiff."),
    ".tiff": ("convertible", "texture", "Image (TIFF)", "Convert to .hdr using ra_tiff."),
    ".hdr":  ("ready", "texture", "Radiance image/texture", "Use directly."),
    ".pic":  ("ready", "texture", "Radiance image/texture", "Use directly."),
    ".csv":  ("ready", ".", "Product list", "Compare with IES analyses."),
    ".xlsx": ("convertible", ".", "Product list (Excel)",
              "This format is readable. Prefer CSV when possible for reliability."),
    ".txt":  ("ready", ".", "Note/list", "Place in the project root."),
    ".pdf":  ("ready", ".", "Document/datasheet", "Place in the project root (luminaire datasheets, etc.)."),
    ".vf":   ("ready", "view", "Camera view", "Use directly."),
    # ---- Source applications: export instructions. ----
    ".skp":  ("export_required", None, "SketchUp",
              "Not read directly. In SketchUp: File > Export > 3D Model > OBJ "
              "(Export texture maps, Triangulate all faces). Place the OBJ+MTL here."),
    ".3dm":  ("export_required", None, "Rhino",
              "Not read directly. In Rhino: File > Export Selected > OBJ "
              "(preserve materials, triangulate). Place the OBJ+MTL here."),
    ".rvt":  ("export_required", None, "Revit",
              "Not read directly. In Revit: open a 3D view > Export > FBX, then "
              "convert to OBJ using 3ds Max/Blender. (Alternative: Export > IFC.) "
              "Use meaningful material names."),
    ".rfa":  ("export_required", None, "Revit family",
              "A family file cannot be used alone. Export the project (.rvt) to OBJ/FBX."),
    ".max":  ("export_required", None, "3ds Max",
              "Not read directly. In Max: Export > OBJ (include materials). "
              "Prefer OBJ over FBX."),
    ".fbx":  ("export_required", None, "FBX",
              "Radiance cannot read FBX. Open in Blender (free) > Export > OBJ, "
              "or export OBJ directly from the source application."),
    ".dae":  ("export_required", None, "Collada",
              "Not read directly. Convert to OBJ with Blender."),
    ".dwg":  ("export_required", None, "AutoCAD",
              "2D/3D DWG is not read directly. Export 3D models to OBJ. 2D drawings "
              "can serve as references only."),
    ".dxf":  ("export_required", None, "DXF",
              "Not read directly. Export OBJ from the source application."),
    ".ifc":  ("export_required", None, "IFC",
              "Not read directly. Open with Blender and a BIM add-on, then export OBJ."),
    ".stl":  ("convertible", "model", "STL",
              "Convertible, but has NO MATERIALS (one part). Prefer OBJ when possible."),
    ".evo":  ("convertible", "model", "DIALux evo project",
              "Read FBX geometry when present and STEP room/luminaire records even without FBX. M3D/GDMS details are unsupported. For supported files, "
              "extract product photometry to IES. Radiance preparation requires ies2rad. "
              "Verify part positions, units and physical results against the source scene."),
    ".ldt":  ("ready", "light", "Luminaire photometry (EULUMDAT/LDT)",
              "Convert automatically to IES (expanded symmetry, absolute cd). `products` "
              "also extracts a product table."),
    ".gldf": ("ready", "light", "GLDF luminaire (DIALux open format)",
              "Open ZIP format. The `products` command extracts its IES/LDT photometry."),
    ".stf":  ("export_required", None, "DIALux STF (legacy exchange format)",
              "DIALux 4 CAD exchange format. Prefer IFC in evo when possible. "
              "If needed, import into CAD and export OBJ/IFC."),
    ".skb":  ("unusable", None, "SketchUp backup", "This backup file is not needed."),
    ".zip":  ("unusable", None, "ZIP archive",
              "Generic ZIPs are not processed. Use `products` to extract manufacturer photometry packages."),
}

# Lumion project file (tools/ls10_reader.py): scene inventory and embedded textures.
# Geometry and light reading were validated on a sample file, not every version.
_LUMION_MESSAGE = (
    "The pattern reader extracts embedded mesh and light records, preserves a complete "
    "geometry copy and does not automatically remove thin faces. Some assets may be missing depending on the file version. "
    "Light intensities are mapped approximately. Physical photometry requires separate validation.")
for _e in (".ls10",):
    TABLE[_e] = ("convertible", "model", "Lumion project", _LUMION_MESSAGE)

SOURCE_GUESS = {".skp": "SketchUp", ".3dm": "Rhino", ".rvt": "Revit",
                 ".rfa": "Revit", ".max": "3ds Max", ".evo": "DIALux",
                 ".fbx": "3ds Max/FBX", ".obj": "OBJ (possibly SketchUp/Rhino/Max)",
                 ".ls": "Lumion", ".ls9": "Lumion", ".ls10": "Lumion", ".ls11": "Lumion",
                 ".ls12": "Lumion", ".lsf": "Lumion"}


def analyze(directory):
    """Identify directory files -> [{file, kind, status, target, message}], source_guess."""
    result, sources = [], []
    if not os.path.isdir(directory):
        return result, None
    from pathlib import Path
    # Preserve relative subdirectories. Do not scan processed or upload archives.
    paths = []
    for root, dirs, fs in os.walk(directory, followlinks=False):
        dirs[:] = [d for d in dirs if not d.startswith((".", "_"))
                   and not os.path.islink(os.path.join(root, d))]
        paths.extend(os.path.relpath(os.path.join(root, f), directory) for f in fs
                      if not os.path.islink(os.path.join(root, f)))
    for name in sorted(paths):
        full = os.path.join(directory, name)
        if name.startswith(".") or name.startswith("README") or os.path.isdir(full):
            continue
        extension = os.path.splitext(name)[1].lower()
        status, target, record_type, message = TABLE.get(
            extension, ("unusable", None, "Unknown (%s)" % extension,
                 "This file type is unsupported."))
        # Manufacturer photometry ZIP: the `products` command extracts its IES/LDT/GLDF files.
        if extension == ".zip" and is_photometry_package(full):
            status, target, record_type = "convertible", "products", "Manufacturer photometry package (ZIP)"
            message = ("The `products` command extracts the IES/LDT/GLDF photometry into IES files "
                     "with beam-angle and lumens metadata in their filenames.")
        # With assimp/usd-core installed, these formats convert deterministically to OBJ.
        if extension in CONVERTIBLE_FORMATS and engine.is_convertible(extension):
            status, target, record_type = "convertible", "model", CONVERTIBLE_FORMATS[extension]
            message = "Convert deterministically to OBJ, then create a Radiance mesh."
        if extension in (".jpg", ".jpeg", ".png") and name.lower().startswith("ref_"):
            record_type, target, status = "Reference image", ".", "ready"
            message = "Place in the project root as a reference."
        result.append({"file": name, "kind": record_type, "status": status,
                      "target": target, "message": message})
        if extension in SOURCE_GUESS:
            sources.append(SOURCE_GUESS[extension])
    source = sources[0] if sources else None
    return result, source


def warnings(files, requested_outputs):
    """Return warnings for missing requirements."""
    u = []
    names = [d["file"].lower() for d in files]
    present = lambda extension: any(a.endswith(extension) for a in names)
    usable_model = any(
        d["status"] in ("ready", "convertible") and d["target"] == "model"
        and not d["file"].lower().endswith(".mtl") for d in files)
    if not usable_model:
        u.append("No usable MODEL (OBJ/RAD/RTM). Export OBJ from the source application. "
                 "See the instructions in the file list.")
    if present(".obj") and not present(".mtl"):
        u.append("OBJ is present without .MTL. Material mapping may be incomplete. Add the MTL file too.")
    night_requested = any(c in ("night", "lux", "night_hour_scan")
                       for c in requested_outputs)
    if night_requested and not present(".ies"):
        u.append("NIGHT/lux output was requested without any .IES files. A night scene requires "
                 "luminaire photometry. Download IES files from "
                 "the manufacturer's website.")
    for d in files:
        if d["status"] == "unusable":
            u.append("%s: %s" % (d["file"], d["message"]))
        elif d["status"] == "export_required":
            u.append("%s: %s" % (d["file"], d["message"]))
    return u


def _place_evo(source, project_dir, target_obj, report, errors):
    """Recover independent EVO components and report unsupported details as partial results."""
    import re
    models, luminaires = [], []
    with zipfile.ZipFile(source) as z:
        extensions = {os.path.splitext(n)[1].lower() for n in z.namelist()}
    if ".fbx" in extensions:
        try:
            engine.convert(source, target_obj)
            models.append(os.path.relpath(target_obj, project_dir))
            report.append("Embedded EVO FBX geometry converted to OBJ")
        except Exception as e:
            errors.append("EVO FBX: " + str(e))
    else:
        report.append("EVO has no FBX. Reading STEP room and luminaire data separately")
    missing = sorted(extensions & {".m3d", ".gdms"})
    if missing:
        errors.append("Embedded EVO " + "/".join(missing) +
                       " geometry details are not yet supported. STEP rooms and approximate furniture boxes may be available")
    try:
        ents = engine._evo_step_ents(source) or {}
    except Exception as e:
        ents = {}; errors.append("EVO STEP: " + str(e))
    kinds = {t for t, _ in ents.values()}
    if not ents:
        errors.append("EVO contains no readable STEP records")
    if "LuminaireElement" in kinds:
        try:
            luminaires = engine.evo_luminaires(source)
            report.append("EVO STEP: %d luminaire positions read" % len(luminaires))
            if not luminaires:
                errors.append("EVO luminaire records exist, but their positions could not be resolved")
        except Exception as e:
            errors.append("EVO luminaire: " + str(e))
        try:
            products = engine.evo_ies(source, project_dir)
            linked = 0
            for luminaire in luminaires:
                identity = luminaire.get('source_id')
                if identity is None:
                    # Legacy record compatibility: a number at the end of a user-provided name
                    # is not a product identity. Only the legacy technical name is recognized.
                    old = re.fullmatch(r'luminaire_(\d+)', str(luminaire.get('name', '')))
                    identity = old[1] if old else None
                u = products.get(str(identity)) if identity is not None else None
                if u:
                    luminaire["product_rad"] = u["rad"]; luminaire["lumens"] = u["lumens"]
                    for field in ('source_id', 'product_id', 'product_guid', 'product_record',
                                 'product_name', 'product_names', 'product_code', 'product_representation_record'):
                        if field in u:
                            luminaire.setdefault(field, u[field])
                    linked += 1
            report.append("EVO product photometry linked to %d luminaires" % linked)
            if linked < len(luminaires):
                errors.append("%d EVO luminaires have unmatched product photometry. Their power is approximate" % (len(luminaires)-linked))
        except Exception as e:
            errors.append("EVO photometry: " + str(e))
    for record_type, name, read in (("Space", "evo_rooms.rad", engine.evo_rooms),
                         ("FurnitureElement", "evo_furniture.rad", engine.evo_furniture)):
        if record_type not in kinds:
            continue
        target = os.path.join(os.path.dirname(target_obj), name)
        try:
            _, n = read(source, target)
            if n:
                models.append(os.path.relpath(target, project_dir))
                report.append("EVO STEP %s: %d items" % (record_type, n))
            else:
                errors.append("EVO %s records produced no geometry" % record_type)
        except Exception as e:
            errors.append("EVO %s: %s" % (record_type, e))
    if not models and not luminaires:
        errors.append("No usable geometry or luminaires could be extracted from EVO")
    return models, luminaires
