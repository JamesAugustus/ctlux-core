#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""USD köprüsü, ayrı process olarak çalışır.

Neden ayrı process: pxr (usd-core) bir worker thread'inde ilk kez yüklenince macOS'ta
bütün program donabiliyor. Burada pxr bu process'in main thread'inde yüklenir,
process donarsa çağıran timeout ile öldürür, ana program etkilenmez.

Kullanım: python3 usd_bridge.py GIRDI.usd CIKTI.obj [ISIKLAR.json]
Çıkış kodu: 0 = geometri yazıldı (light'lar da varsa json'da)
            2 = geometri yok ama light bulundu (json yazıldı, stdout 'GEO_YOK: sebep')
            1 = hata (mesaj stderr'de)
"""
import sys, os, json

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)


def isiklar_cikar(kaynak):
    """UsdLux light'larını çıkarır -> dict listesi (world position metre, Z-up).
    tur: alan (Rect/Disk/Cylinder), omni (Sphere), gunes (Distant), kubbe (Dome).
    UsdLux light'ı local -Z yönüne bakar, world matrix ile çevrilir."""
    from pxr import Usd, UsdGeom, UsdLux, Gf
    stage = Usd.Stage.Open(kaynak)
    if stage is None:
        return []
    mpu = UsdGeom.GetStageMetersPerUnit(stage) or 0.01      # USD default'u cm
    y_up = (UsdGeom.GetStageUpAxis(stage) == UsdGeom.Tokens.y)
    t = Usd.TimeCode.Default()
    cache = UsdGeom.XformCache(t)

    def nokta(v):
        x, y, z = v[0] * mpu, v[1] * mpu, v[2] * mpu
        if y_up:
            x, y, z = x, -z, y
        return [round(x, 3), round(y, 3), round(z, 3)]

    def birim_yon(v):
        x, y, z = v[0], v[1], v[2]
        if y_up:
            x, y, z = x, -z, y
        n = (x * x + y * y + z * z) ** 0.5 or 1.0
        return [round(x / n, 3), round(y / n, 3), round(z / n, 3)]

    out = []
    for prim in stage.Traverse(Usd.TraverseInstanceProxies(Usd.PrimDefaultPredicate)):
        # IsA + type name: Houdini yeni şema versiyonları yazıyor (ör. 'DomeLight_1').
        # usd-core o şemayı tanımaz, IsA tutmaz, ada da bakınca ikisi de yakalanır.
        tn = str(prim.GetTypeName())
        if prim.IsA(UsdLux.RectLight) or tn.startswith("RectLight"):          tur = "alan"
        elif prim.IsA(UsdLux.DiskLight) or tn.startswith("DiskLight"):        tur = "alan"
        elif prim.IsA(UsdLux.CylinderLight) or tn.startswith("CylinderLight"):tur = "alan"
        elif prim.IsA(UsdLux.SphereLight) or tn.startswith("SphereLight"):    tur = "omni"
        elif prim.IsA(UsdLux.DistantLight) or tn.startswith("DistantLight"):  tur = "gunes"
        elif prim.IsA(UsdLux.DomeLight) or tn.startswith("DomeLight"):        tur = "kubbe"
        else:
            continue
        if UsdGeom.Imageable(prim).ComputeVisibility(t) == UsdGeom.Tokens.invisible:
            continue
        M = cache.GetLocalToWorldTransform(prim)
        # Rect'in local X/Y kenarları world transform'un scale'ini de taşır.
        # birim eksenlerin norm'u, rotation ve non-uniform scale birlikteyken de
        # kenar uzunluğunu verir, shear ve emitter'ın tam frame'i bu kapsamda değil.
        rect = prim.IsA(UsdLux.RectLight) or tn.startswith("RectLight")
        sx = M.TransformDir(Gf.Vec3d(1, 0, 0)).GetLength() if rect else 1.0
        sy = M.TransformDir(Gf.Vec3d(0, 1, 0)).GetLength() if rect else 1.0

        def deger(ad, varsayilan):
            a = prim.GetAttribute(ad)
            v = a.Get(t) if a else None
            return varsayilan if v is None else v

        yog = float(deger("inputs:intensity", 1.0)) * (2.0 ** float(deger("inputs:exposure", 0.0)))
        renk = deger("inputs:color", (1.0, 1.0, 1.0))
        kelvin = None
        if deger("inputs:enableColorTemperature", False):
            kelvin = int(float(deger("inputs:colorTemperature", 6500.0)))
        doku = deger("inputs:texture:file", None)
        doku_ad = None
        if doku is not None:                                   # Sdf.AssetPath (boşsa None kalsın)
            p = str(getattr(doku, "resolvedPath", "") or getattr(doku, "path", "") or "")
            doku_ad = p if p else None
        out.append({
            "tur": tur, "ad": prim.GetName(),
            "konum": nokta(M.Transform(Gf.Vec3d(0, 0, 0))),
            "yon": birim_yon(M.TransformDir(Gf.Vec3d(0, 0, -1))),
            "siddet": round(yog, 4),
            "renk": [round(float(renk[0]), 3), round(float(renk[1]), 3), round(float(renk[2]), 3)],
            "kelvin": kelvin,
            "w": round(float(deger("inputs:width", 1.0)) * mpu * sx, 3),
            "h": round(float(deger("inputs:height", 1.0)) * mpu * sy, 3),
            "yaricap": round(float(deger("inputs:radius", 0.5)) * mpu, 3),
            "doku": doku_ad,
        })
    return out


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    if len(sys.argv) < 3:
        print("kullanim: usd_bridge.py GIRDI CIKTI.obj [ISIKLAR.json]", file=sys.stderr)
        sys.exit(1)
    kaynak, hedef = sys.argv[1], sys.argv[2]
    isik_json = sys.argv[3] if len(sys.argv) > 3 else None
    geo_hata = None
    try:
        from core import engine as motor                      # önce motor import edilmeli, yoksa döngüsel import patlar
        from core import importer
        importer._usd_obj_yerli(kaynak, hedef)
    except Exception as e:
        geo_hata = str(e)
    isiklar = []
    if isik_json:
        try:
            isiklar = isiklar_cikar(kaynak)
            with open(isik_json, "w") as f:
                json.dump(isiklar, f, ensure_ascii=False, indent=1)
        except Exception as e:
            print("isik cikarma hatasi: %s" % e, file=sys.stderr)
    if geo_hata and not isiklar:
        print(geo_hata, file=sys.stderr)
        sys.exit(1)
    if geo_hata:
        print("GEO_YOK: %s" % geo_hata)
        sys.exit(2)
    sys.exit(0)
