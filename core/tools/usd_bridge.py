#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""USD bridge running in a separate process.

A separate process is used because importing pxr (usd-core) for the first time in a worker
thread can freeze the entire program on macOS. Here pxr loads on the process main thread.
If the process hangs, the caller terminates it on timeout without affecting the main program.

Usage: python3 usd_bridge.py INPUT.usd OUTPUT.obj [LIGHTS.json]
Exit code: 0 = geometry written (lights, if present, are also in JSON)
            2 = no geometry but lights found (JSON written, stdout 'NO_GEOMETRY: reason')
            1 = error (message on stderr)
"""
import sys, os, json

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)


def extract_lights(source):
    """Extract UsdLux lights -> list of dictionaries (world positions in meters, Z-up).
    Light kinds: area (Rect/Disk/Cylinder), omni (Sphere), sun (Distant), dome (Dome).
    UsdLux lights face local -Z, transformed by the world matrix."""
    from pxr import Usd, UsdGeom, UsdLux, Gf
    stage = Usd.Stage.Open(source)
    if stage is None:
        return []
    mpu = UsdGeom.GetStageMetersPerUnit(stage) or 0.01      # USD defaults to cm
    y_up = (UsdGeom.GetStageUpAxis(stage) == UsdGeom.Tokens.y)
    t = Usd.TimeCode.Default()
    cache = UsdGeom.XformCache(t)

    def point(v):
        x, y, z = v[0] * mpu, v[1] * mpu, v[2] * mpu
        if y_up:
            x, y, z = x, -z, y
        return [round(x, 3), round(y, 3), round(z, 3)]

    def unit_direction(v):
        x, y, z = v[0], v[1], v[2]
        if y_up:
            x, y, z = x, -z, y
        n = (x * x + y * y + z * z) ** 0.5 or 1.0
        return [round(x / n, 3), round(y / n, 3), round(z / n, 3)]

    out = []
    for prim in stage.Traverse(Usd.TraverseInstanceProxies(Usd.PrimDefaultPredicate)):
        # IsA + type name: Houdini writes new schema versions (e.g. 'DomeLight_1').
        # If usd-core does not know a schema, IsA fails. Checking the name catches both versions.
        tn = str(prim.GetTypeName())
        if prim.IsA(UsdLux.RectLight) or tn.startswith("RectLight"):          record_type = "area"
        elif prim.IsA(UsdLux.DiskLight) or tn.startswith("DiskLight"):        record_type = "area"
        elif prim.IsA(UsdLux.CylinderLight) or tn.startswith("CylinderLight"):record_type = "area"
        elif prim.IsA(UsdLux.SphereLight) or tn.startswith("SphereLight"):    record_type = "omni"
        elif prim.IsA(UsdLux.DistantLight) or tn.startswith("DistantLight"):  record_type = "sun"
        elif prim.IsA(UsdLux.DomeLight) or tn.startswith("DomeLight"):        record_type = "dome"
        else:
            continue
        if UsdGeom.Imageable(prim).ComputeVisibility(t) == UsdGeom.Tokens.invisible:
            continue
        M = cache.GetLocalToWorldTransform(prim)
        # Rect local X/Y sides also include the world transform scale.
        # The unit-axis norm gives side length even with combined rotation and
        # nonuniform scale. Shear and the full emitter frame are outside this scope.
        rect = prim.IsA(UsdLux.RectLight) or tn.startswith("RectLight")
        sx = M.TransformDir(Gf.Vec3d(1, 0, 0)).GetLength() if rect else 1.0
        sy = M.TransformDir(Gf.Vec3d(0, 1, 0)).GetLength() if rect else 1.0

        def value(name, default):
            a = prim.GetAttribute(name)
            v = a.Get(t) if a else None
            return default if v is None else v

        intensity = float(value("inputs:intensity", 1.0)) * (2.0 ** float(value("inputs:exposure", 0.0)))
        color = value("inputs:color", (1.0, 1.0, 1.0))
        kelvin = None
        if value("inputs:enableColorTemperature", False):
            kelvin = int(float(value("inputs:colorTemperature", 6500.0)))
        texture = value("inputs:texture:file", None)
        texture_name = None
        if texture is not None:                                   # Sdf.AssetPath (keep None when empty)
            p = str(getattr(texture, "resolvedPath", "") or getattr(texture, "path", "") or "")
            texture_name = p if p else None
        out.append({
            "kind": record_type, "name": prim.GetName(),
            "position": point(M.Transform(Gf.Vec3d(0, 0, 0))),
            "direction": unit_direction(M.TransformDir(Gf.Vec3d(0, 0, -1))),
            "intensity": round(intensity, 4),
            "color": [round(float(color[0]), 3), round(float(color[1]), 3), round(float(color[2]), 3)],
            "kelvin": kelvin,
            "w": round(float(value("inputs:width", 1.0)) * mpu * sx, 3),
            "h": round(float(value("inputs:height", 1.0)) * mpu * sy, 3),
            "radius": round(float(value("inputs:radius", 0.5)) * mpu, 3),
            "texture": texture_name,
        })
    return out


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    if len(sys.argv) < 3:
        print("usage: usd_bridge.py INPUT OUTPUT.obj [LIGHTS.json]", file=sys.stderr)
        sys.exit(1)
    source, target = sys.argv[1], sys.argv[2]
    light_json = sys.argv[3] if len(sys.argv) > 3 else None
    geometry_error = None
    try:
        from core import engine as engine                      # Import the engine module first to avoid a circular import failure.
        from core import importer
        importer._usd_obj_native(source, target)
    except Exception as e:
        geometry_error = str(e)
    lights = []
    if light_json:
        try:
            lights = extract_lights(source)
            with open(light_json, "w") as f:
                json.dump(lights, f, ensure_ascii=False, indent=1)
        except Exception as e:
            print("light extraction error: %s" % e, file=sys.stderr)
    if geometry_error and not lights:
        print(geometry_error, file=sys.stderr)
        sys.exit(1)
    if geometry_error:
        print("NO_GEOMETRY: %s" % geometry_error)
        sys.exit(2)
    sys.exit(0)
