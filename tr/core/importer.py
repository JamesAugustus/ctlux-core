#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Import / converter katmanı: dosyayı okur, OBJ + light listesine çevirir.
Lumion (.ls10), DIALux (.evo), USD, Assimp formatları, LDT/IES, GLDF -> OBJ/RAD/IES.
engine.py bu modülü facade olarak re-export eder: `motor.cevir(...)` aynen çalışır.
Çekirdeğe bağımlılık sadece çağrı anında: motor.sh/surecler/cevirici_durum +
motor.CEVIRICI_ASSIMP/CEVIRICI_USD. `import motor` döngüsel ama sorun çıkarmaz: semboller
sadece çağrı anında okunur, engine.py de bu modülü import sırasında en sonda yükler."""
import io, json, os, math, subprocess, tempfile, time, shutil, struct, zlib, hashlib, zipfile
import warnings
import unicodedata
from core.photometry import NOT_DOSYA, NOT_PROJE, notlu_ies


LUMION_UZ = {".ls10"}
USD_UZ = (".usd", ".usda", ".usdc", ".usdz")


def _lumion_arac():
    """tools/ls10_reader modülünü yükler (core/tools altında)."""
    from core.tools import ls10_reader as lumion_oku
    return lumion_oku


def _lumion_obj(kaynak, hedef):
    """Lumion -> OBJ, bütün yüzler ve nesne sınırları korunur."""
    import mmap
    L = _lumion_arac()
    with open(kaynak, "rb") as f:
        # mmap boş dosyayı eşleyemez, bunu açıkça bildir.
        if os.fstat(f.fileno()).st_size == 0:
            raise RuntimeError("Lumion dosyası boş: " + os.path.basename(kaynak))
        with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
            matris = L.tek_model_matrisi(m)
            from core.tools.ls10_surface import Reader, export, INSTANCE
            if matris is not None and str(kaynak).lower().endswith('.ls10') and INSTANCE.search(m):
                r = export(Reader(m), hedef, matris)
            else:
                r = L.mesh_cikar(m, hedef, zup=True, matris=matris, tekil=True)
    if not r.get("mesh") or not os.path.exists(hedef):
        raise RuntimeError("Lumion geometrisi çıkarılamadı (mesh bulunamadı)")
    if os.path.exists(hedef + ".tam.obj"):
        # eski aktarımdan kalan .tam.obj sidecar'ı final render'da yeni modelin önüne geçmesin.
        shutil.copy2(hedef, hedef + ".tam.obj")
    from core.file_safety import atomik_json
    uyarilar = ["Lumion aktarımı kısmi: malzeme, doku ve çoklu nesne bağlantıları tam çözülmüş değil.",
                "Işık kayıtları otomatik birleştirilmez, fiziksel armatür sayısı ve fotometri doğrulanmalı."]
    if r.get("manifest"):
        uyarilar[0] = ("Lumion renk, UV ve normal aktarıldı, cam, özel shader ve doku eşlemesi kısmi. "
                       "Özgün malzeme ayarları ve gömülü dokular aktarım manifestinde korunuyor.")
    if matris is None:
        uyarilar.append("Model dünya matrisi eşlenemedi, geometri yerel koordinatlarda olabilir.")
    atomik_json(hedef + ".aktarim.json", {
        "mesh": r["mesh"], "ucgen": r["ucgen"], "vertex": r["vertex"],
        "atlanan": r["atlanan"], "dunya_matrisi": matris,
        "malzeme_manifest": os.path.relpath(r["manifest"], os.path.dirname(hedef)) if r.get("manifest") else None,
        "onizleme_ucgen": min(max(r["ucgen"], 300000), 3000000), "uyarilar": uyarilar})
    return hedef


def lumion_armaturler(kaynak, k=200.0):
    """Lumion light'larını armatür dict formatında döner (proje dict'indeki 'armaturler').
    Böylece light'lar EVO'dakilerle aynı yoldan sahneye girer."""
    import mmap
    L = _lumion_arac()
    with open(kaynak, "rb") as f:
        if os.fstat(f.fileno()).st_size == 0:
            raise RuntimeError("Lumion dosyası boş: " + os.path.basename(kaynak))
        with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
            arms = L.isik_armatur(m, zup=True, k=k)
    return arms


def cevrilebilir(uz):
    uz = uz.lower(); d = motor.cevirici_durum()
    if uz in LUMION_UZ:            # Lumion saf Python, her zaman çevrilebilir
        return True
    if uz in motor.CEVIRICI_ASSIMP:
        return d["assimp"] or uz == ".obj"
    if uz in motor.CEVIRICI_USD:
        return d["usd"]
    if uz == ".evo":               # STEP oda/ışık stdlib ile okunur, assimp sadece gömülü FBX için lazım
        return True
    return False


def usd_kopru_calistir(kaynak, hedef_obj):
    """USD çevirisini ayrı process'te koşturur -> (geo_var, isiklar, mesaj).
    Neden: pxr bir worker thread'inde yüklenince macOS'ta bütün program donabiliyor.
    Ayrı process + timeout: o taraf donsa bile program ayakta kalır.
    isiklar: UsdLux listesi (alan/omni/gunes/kubbe), usd_armaturler armatüre çevirir."""
    kopru = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools", "usd_bridge.py")
    ij = hedef_obj + ".isiklar.json"
    import sys as _sys
    r = motor.surecler.calistir([_sys.executable, kopru, kaynak, hedef_obj, ij],
                               timeout=300)
    if r.returncode in (0, 2) and r.stderr.strip():
        warnings.warn('USD köprüsü: ' + r.stderr.strip()[:1000], RuntimeWarning)
    isiklar = []
    try:
        if os.path.exists(ij):
            with open(ij) as f:
                isiklar = json.load(f)
            os.remove(ij)
    except Exception:
        pass
    if r.returncode == 0:
        return True, isiklar, ""
    if r.returncode == 2:                     # geometri yok ama light bulundu
        return False, isiklar, (r.stdout or "").strip()[:200]
    raise RuntimeError((r.stderr or r.stdout or "USD köprüsü başarısız").strip()[:300])


def _usd_obj(kaynak, hedef):
    """USD -> OBJ, köprü üzerinden (motor.cevir'in kullandığı genel giriş)."""
    geo_var, isiklar, _ = usd_kopru_calistir(kaynak, hedef)
    if not geo_var:
        ek = ", ama %d ışık bulundu (Proje ekle ile alınır)" % len(isiklar) if isiklar else ""
        raise RuntimeError("USD içinde mesh geometri yok (payload/reference "
                           "çözülemedi olabilir)" + ek)


def usd_armaturler(isiklar):
    """Köprünün UsdLux listesini proje armatür dict'lerine çevirir.
    alan/omni armatür olur, gunes (DistantLight) ve kubbe (DomeLight HDRI) armatür
    olmaz. (armaturler, notlar) döner, notlar rapora yazılır.
    guc ölçeği tahmini: UsdLux intensity'nin birimi sahneye göre değişiyor, kalibre
    edilmedi. guc = siddet*100, 5..5000 arasına clamp edilir."""
    arms, notlar = [], []
    n = 0
    for it in isiklar:
        tur = it.get("tur")
        if tur == "gunes":
            notlar.append("USD DistantLight '%s' = güneş, render güneşi zaten var, atlandı"
                          % it.get("ad", "?"))
            continue
        if tur == "kubbe":
            d = it.get("doku") or ""
            notlar.append("USD DomeLight '%s' = HDRI gök%s, gök modülü ileride bağlanacak"
                          % (it.get("ad", "?"), (" (%s)" % os.path.basename(d)) if d else ""))
            continue
        n += 1
        k = it.get("konum") or [0, 0, 1]
        arms.append({
            "ad": it.get("ad") or ("usd_isik_%d" % n),
            "x": k[0], "y": k[1], "z": k[2],
            "renk": it.get("renk") or [1, 1, 1],
            "guc": max(0, min(5000, round((1.0 if it.get("siddet") is None else it["siddet"]) * 100))),
            "fotometri_durumu": "yaklasik",
            "usd_siddet": it.get("siddet"),
            "yaricap": max(0.05, it.get("yaricap") or 0.35),
            "tip": "alan" if tur == "alan" else "omni",
            "aci": 120,
            "yon": it.get("yon") if tur == "alan" else None,
            "w": max(0.05, it.get("w") or 0.5),
            "h": max(0.05, it.get("h") or 0.5),
            "kaynak": "usd",
            "kelvin": it.get("kelvin") or None,
        })
    return arms, notlar


def _usd_obj_yerli(kaynak, hedef):
    """USD (.usd/.usda/.usdc/.usdz) -> OBJ. World transform uygulanır, cm->metre
    ve Y-up->Z-up (Radiance) normalize edilir, primvars:st UV ve grup adı
    yazılır (light'lar isimden bulunabilsin diye). usd-core (pxr) gerekir.
    Dikkat: pxr'ı bu process'in içinde yükler, o yüzden sadece usd_bridge.py
    subprocess'inden çağrılır. Dışarıdan giriş _usd_obj/usd_kopru_calistir."""
    from pxr import Usd, UsdGeom, Gf
    stage = Usd.Stage.Open(kaynak)
    if stage is None:
        raise RuntimeError("USD açılamadı")
    mpu = UsdGeom.GetStageMetersPerUnit(stage) or 0.01     # default cm, dikkat
    y_up = (UsdGeom.GetStageUpAxis(stage) == UsdGeom.Tokens.y)
    t = Usd.TimeCode.Default()
    V, VT, F = [], [], []                                   # F: (grup, [vi], [ti] ya da None)

    class USDTopolojiHatasi(ValueError):
        """Geçersiz index'leri kısmi ya da bozuk OBJ'yi başarı sayarak geçiştirmeyelim."""

    def gorunur(prim):
        """invisible ve guide/proxy purpose geometriyi ele, sahneye girmesin"""
        try:
            img = UsdGeom.Imageable(prim)
            if img.ComputeVisibility(t) == UsdGeom.Tokens.invisible:
                return False
            if img.ComputePurpose() in (UsdGeom.Tokens.guide, UsdGeom.Tokens.proxy):
                return False
        except Exception:
            pass
        return True

    def yaz_mesh(mesh, M, grup):
        """bir UsdGeom.Mesh'i world matrix M ile V/VT/F'e ekler"""
        prim = mesh.GetPrim()
        pts = mesh.GetPointsAttr().Get(t)
        counts = mesh.GetFaceVertexCountsAttr().Get(t)
        idx = mesh.GetFaceVertexIndicesAttr().Get(t)
        if not pts or not counts or not idx:
            return
        gecerli, sebep = UsdGeom.Mesh.ValidateTopology(idx, counts, len(pts))
        if not gecerli:
            raise USDTopolojiHatasi("USD geçersiz mesh topolojisi %s: %s" % (prim.GetPath(), sebep))
        taban = len(V)
        for p in pts:
            w = M.Transform(Gf.Vec3d(p[0], p[1], p[2]))     # world (USD birimi)
            x, y, z = w[0]*mpu, w[1]*mpu, w[2]*mpu          # metreye
            if y_up:
                x, y, z = x, -z, y                          # Y-up -> Z-up
            V.append((x + 0.0, y + 0.0, z + 0.0))           # -0.0'ı 0.0 yap (kozmetik)
        # UV (primvars:st) texture için, burada hata çıkarsa geometri yine çevrilir
        uvs = interp = None
        try:
            st = UsdGeom.PrimvarsAPI(prim).GetPrimvar("st")
            if st and st.HasValue():
                uvs = st.ComputeFlattened(t)                # index'liyse flatten eder
                interp = st.GetInterpolation()
        except Exception:
            uvs = None
        uv_taban = len(VT)
        if uvs:
            for uv in uvs:
                VT.append((uv[0], uv[1]))                   # V-flip yok: USD, OBJ ve Radiance üçü de sol-alt köşeden başlar
        # vertex/varying UV sadece dizi points kadar uzunsa güvenli (range dışı vt olmasın)
        vertUV = uvs and interp in (UsdGeom.Tokens.vertex, UsdGeom.Tokens.varying) and len(uvs) >= len(pts)
        k = fv = 0
        for c in counts:
            vi = [taban + idx[k+j] + 1 for j in range(c)]
            ti = None
            if uvs:
                if interp == UsdGeom.Tokens.faceVarying and (fv + c) <= len(uvs):
                    ti = [uv_taban + (fv + j) + 1 for j in range(c)]
                elif vertUV:
                    ti = [uv_taban + idx[k+j] + 1 for j in range(c)]
            F.append((grup, vi, ti))
            k += c; fv += c

    pred = Usd.TraverseInstanceProxies(Usd.PrimDefaultPredicate)
    # PointInstancer prototiplerini topla, bunlar ana loop'ta tek başına yazılmamalı
    # (yoksa hem prototip hem instance olarak çizilir, geometri çift çıkar)
    proto_yollari = []
    for prim in stage.Traverse(pred):
        if prim.IsA(UsdGeom.PointInstancer):
            try:
                for pp in UsdGeom.PointInstancer(prim).GetPrototypesRel().GetTargets():
                    proto_yollari.append(pp)
            except Exception:
                pass
    def prototip_mi(prim):
        yol = prim.GetPath()
        return any(yol.HasPrefix(pp) for pp in proto_yollari)
    for prim in stage.Traverse(pred):
        if prim.IsA(UsdGeom.Mesh):
            if not gorunur(prim) or prototip_mi(prim):        # prototip mesh'ini instancer yazsın, burada değil
                continue
            yaz_mesh(UsdGeom.Mesh(prim),
                     UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(t),
                     prim.GetName())
        elif prim.IsA(UsdGeom.PointInstancer) and gorunur(prim):
            # scatter (ağaç/koltuk/insan dağıtımı): her instance'ı kendi matrisiyle yaz
            try:
                pi = UsdGeom.PointInstancer(prim)
                protos = pi.GetPrototypesRel().GetTargets()
                pidx = pi.GetProtoIndicesAttr().Get(t) or []
                # prototip kökünün local transform'u matrise dahil. Aşağıdaki rel
                # sadece prototip içindeki alt mesh'in göreli transform'unu taşır.
                # IgnoreMask dizi boyunu küçültmesin: protoIndices/n eşleşmesi korunur.
                mats = pi.ComputeInstanceTransformsAtTime(
                    t, t, UsdGeom.PointInstancer.IncludeProtoXform,
                    UsdGeom.PointInstancer.IgnoreMask)
                maske = pi.ComputeMaskAtTime(t)  # boş mask = bütün instance'lar görünür
                pw = UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(t)  # instancer'ın world matrix'i
                for n, pil in enumerate(pidx):
                    if maske and not maske[n]:
                        continue
                    if pil < 0 or pil >= len(protos) or n >= len(mats):
                        continue
                    proto = stage.GetPrimAtPath(protos[pil])
                    if not proto:
                        continue
                    proto_inv = UsdGeom.Xformable(proto).ComputeLocalToWorldTransform(t).GetInverse()
                    for sub in Usd.PrimRange(proto):
                        if sub.IsA(UsdGeom.Mesh) and gorunur(sub):
                            rel = UsdGeom.Xformable(sub).ComputeLocalToWorldTransform(t) * proto_inv
                            yaz_mesh(UsdGeom.Mesh(sub), rel * mats[n] * pw, sub.GetName())
            except USDTopolojiHatasi:
                raise
            except Exception:
                pass    # PointInstancer çözülemezse geometrinin geri kalanını bozma
    if not V:
        raise RuntimeError("USD içinde mesh geometri yok (payload/reference çözülemedi olabilir)")
    with open(hedef, "w") as fp:
        for v in V:
            fp.write("v %g %g %g\n" % v)
        for vt in VT:
            fp.write("vt %g %g\n" % vt)
        son = None
        for grup, vi, ti in F:
            if grup != son:
                fp.write("g %s\n" % grup); son = grup       # light'ları isimden bulabilmek için
            if ti:
                fp.write("f " + " ".join("%d/%d" % (a, b) for a, b in zip(vi, ti)) + "\n")
            else:
                fp.write("f " + " ".join(str(a) for a in vi) + "\n")
    return hedef


def ldt_ies(kaynak, hedef, bilgi=None):
    """EULUMDAT (.ldt) -> IES (LM-63-2002, Type C, TILT=NONE). Simetriyi (Isym) tam
    360°'ye açar, cd/1000lm göreli değerleri toplam lümenle mutlak cd'ye çevirir.
    Doğrulanmış simetriler 0/1/2/4, bilinmeyen ya da eksik veride açık hata verir.
    bilgi dict'i verilirse kaynaktan okunan ad, lümen, güç ve CCT oraya yazılır."""
    with open(kaynak, "rb") as stream:
        ham = stream.read().decode("latin-1")
    L = [x.strip() for x in ham.replace("\r", "").split("\n")]
    if len(L) < 30:
        raise RuntimeError("LDT çok kısa / bozuk")

    def fi(i):
        try:
            value = float(L[i])
        except (ValueError, IndexError) as error:
            raise RuntimeError('LDT sayısal veri eksik veya bozuk: satır %d' % (i+1)) from error
        if not math.isfinite(value):
            raise RuntimeError('LDT sonlu olmayan sayı: satır %d' % (i+1))
        return value
    def count(i):
        value = fi(i)
        if value != int(value) or value < 0 or value > 1000000:
            raise RuntimeError('LDT adet/simetri alanı geçersiz: satır %d' % (i+1))
        return int(value)
    Isym, Mc, Ng = count(2), count(3), count(5)
    if Mc <= 0 or Ng <= 0 or Mc*Ng > 10000000:
        raise RuntimeError('LDT açı sayıları geçersiz')
    if Isym not in (0, 1, 2, 4):
        raise RuntimeError('LDT simetri türü henüz doğrulanmadı: %d' % Isym)
    if (Isym == 2 and Mc % 2) or (Isym == 4 and Mc % 4):
        raise RuntimeError('LDT düzlem sayısı simetriye uygun değil')
    ad = L[8] or L[9] or os.path.basename(kaynak)
    n_set = count(25)
    if n_set != 1:
        raise RuntimeError('LDT birden fazla/eksik lamba seti desteklenmiyor')
    b = 26                                  # ilk lamba seti
    toplam_lm = fi(b + 2)
    if toplam_lm <= 0:
        raise RuntimeError('LDT toplam lümen pozitif olmalı, varsayımsal lümen üretilmedi')
    cct = L[b + 3].strip() if b + 3 < len(L) else ""
    try:
        if not math.isfinite(float(cct)) or float(cct) <= 0:
            cct = ""
    except ValueError:
        cct = ""
    watt = fi(b + 5)
    idx = 26 + n_set * 6 + 10               # lamba setleri + 10 oda index oranı
    C = [fi(idx + i) for i in range(Mc)]; idx += Mc          # C düzlemi açıları
    G = [fi(idx + i) for i in range(Ng)]; idx += Ng          # gamma açıları
    nC = {0: Mc, 1: 1, 2: Mc // 2 + 1, 3: Mc // 2 + 1, 4: Mc // 4 + 1}.get(Isym, Mc)
    ham_v = [fi(idx + i) for i in range(nC * Ng)]            # cd/1000lm
    if any(v < 0 for v in ham_v):
        raise RuntimeError('LDT negatif ışık şiddeti içeriyor')
    if any(a >= b for a, b in zip(G, G[1:])) or not (0 <= G[0] <= G[-1] <= 180):
        raise RuntimeError('LDT gamma açıları geçersiz')
    if any(a >= b for a, b in zip(C, C[1:])) or not (0 <= C[0] <= C[-1] <= 360):
        raise RuntimeError('LDT C açıları geçersiz')
    saklanan = [ham_v[c * Ng:(c + 1) * Ng] for c in range(nC)]   # saklanan[c][g]

    factor = fi(23)
    if factor <= 0:
        raise RuntimeError('LDT ışık şiddeti dönüşüm çarpanı pozitif olmalı')
    if fi(24) != 0:
        raise RuntimeError('LDT ölçüm eğimi henüz desteklenmiyor, TILT=NONE varsayılmadı')
    if Isym in (2, 4) and any(abs(angle - i*360.0/Mc) > 1e-3 for i, angle in enumerate(C)):
        raise RuntimeError('LDT simetrik, eşit aralıklı C düzlemleri gerektiriyor')
    olcek = toplam_lm / 1000.0 * factor
    # Isym'e göre tam Mc düzlemine aç (mutlak cd olarak)
    def duzlem(c):                          # c: 0..Mc-1 -> gamma listesi (cd)
        if Isym == 1:
            src = 0
        elif Isym == 0:
            src = c
        elif Isym == 2:                     # C0-C180 aynası
            src = c if c <= Mc // 2 else Mc - c
        elif Isym == 4:                     # C0-90 çeyreği 4'e aynala
            q = Mc // 4
            cc = c % (2 * q)
            src = cc if cc <= q else 2 * q - cc
        src = max(0, min(src, nC - 1))
        values = [v * olcek for v in saklanan[src]]
        if not all(math.isfinite(v) for v in values):
            raise RuntimeError('LDT mutlak kandela hesabı taştı')
        return values

    tamC = C if (Isym == 0 and len(C) == Mc) else [round(i * 360.0 / Mc, 4) for i in range(Mc)]
    if Isym == 1:
        tamC = [0.0]                        # eksenel simetri: tek yatay düzlem
        planes = [duzlem(0)]
    else:
        planes = [duzlem(c) for c in range(Mc)]
        tamC = tamC + [360.0] if tamC[-1] != 360.0 else tamC
        if len(tamC) == Mc + 1:
            planes.append(planes[0])        # 360 = 0'ın kopyası (kapanış)

    def baslik(value):
        text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
        return " ".join("".join(c if 32 <= ord(c) < 127 else " " for c in text).split())

    nH = len(tamC)
    with open(hedef, "w", encoding="ascii") as f:
        f.write("IESNA:LM-63-2002\n")
        f.write("[TEST] not available\n")
        f.write("[TESTLAB] not available\n")
        f.write("[ISSUEDATE] %s\n" % (baslik(L[11])[:68] or "not available"))
        f.write("[MANUFAC] %s\n" % (baslik(L[0])[:70] or "not available"))
        f.write("[LUMINAIRE] %s\n" % (baslik(ad)[:68] or "not available"))
        if cct: f.write("[LAMP] CCT %s\n" % baslik(cct)[:69])
        f.write("[_SOURCE] Converted from EULUMDAT, Isym=%d\n" % Isym)
        f.write("[_LUMENS] %.17g\n" % toplam_lm)
        f.write("".join(s + "\n" for s in NOT_DOSYA))
        f.write("TILT=NONE\n")
        # 10 alan: lamba=1, lumen=-1 (mutlak), carpan=1, Nv, Nh, tip=1 (C), birim=2 (m), en, boy, yuk
        f.write("1 -1 1 %d %d 1 2 0 0 0\n" % (Ng, nH))
        f.write("1 1 %g\n" % (watt or 0))
        f.write(" ".join("%g" % g for g in G) + "\n")        # dikey (gamma)
        f.write(" ".join("%g" % c for c in tamC) + "\n")     # yatay (C)
        for c in range(nH):                                  # kandela: her yatay için bütün dikeyler
            f.write(" ".join("%g" % v for v in planes[c]) + "\n")
    if bilgi is not None:
        bilgi.update(ad=ad, lumen=toplam_lm, watt=watt, cct=cct)
    return hedef


def _yeni_dosya(hedef_dizin, taban, uz, veri):
    """veri'yi hedef_dizin'de boş bir ada yazar ve adı döner.

    Ad doluysa _2, _3 denenir. Dosya "x" kipinde açılır, var olan dosya ezilmez.
    """
    ad, i = taban + uz, 2
    while True:
        try:
            with open(os.path.join(hedef_dizin, ad), "xb") as f:
                f.write(veri)
            return ad
        except FileExistsError:
            ad = "%s_%d%s" % (taban, i, uz)
            i += 1


def gldf_ac(kaynak, hedef_dizin, taban=None):
    """GLDF (DIALux'un açık armatür formatı, ZIP: product.xml + ldc/ fotometri + geo/ L3D +
    image/) içindeki IES/LDT/EULUMDAT fotometri dosyalarını hedef_dizin'e çıkarır.

    kaynak yol ya da dosya nesnesi olabilir, dosya nesnesinde taban verilir. Ad
    doluysa _2, _3 seçilir, var olan dosya ezilmez. Çıkan dosyaların path listesini döner.
    """
    os.makedirs(hedef_dizin, exist_ok=True)
    cikan = []
    if taban is None:
        taban = os.path.splitext(os.path.basename(kaynak))[0]
    with zipfile.ZipFile(kaynak) as z:
        for n in z.namelist():
            alt = n.lower()
            if alt.endswith((".ies", ".ldt", ".eulumdat")):
                uz = ".ldt" if alt.endswith((".ldt", ".eulumdat")) else ".ies"
                veri = z.read(n)
                ad = _yeni_dosya(hedef_dizin, "%s_%s" % (taban, os.path.splitext(os.path.basename(n))[0]),
                                 uz, notlu_ies(veri) if uz == ".ies" else veri)
                cikan.append(os.path.join(hedef_dizin, ad))
    if not cikan:
        raise RuntimeError("GLDF içinde IES/LDT fotometri bulunamadı")
    return cikan


def _ies_kunye_ad(taban, ies_yolu):
    """IES'ten künyeyi (beam açısı + lümen) okuyup dosya adına ekler, ad
    kendini anlatsın (hem eşleştirici hem insan ada bakıyor). Okunamazsa taban döner."""
    try:
        import sys
        ap = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools")
        if ap not in sys.path:
            sys.path.insert(0, ap)
        from core.tools import ies_analysis as ies_analiz
        d = ies_analiz.ies_oku(ies_yolu)
        beam, _ = ies_analiz.isin_acisi(d["dikey_acilar"], d["kandela"],
                                        d["n_dikey"], d["n_yatay"])
        lm = d.get("lumen_lamba") or 0
        parca = []
        if beam:
            parca.append("%dd" % round(beam * 2))
        if lm > 0:
            parca.append("%dlm" % round(lm))
        if parca and not any(p in taban for p in parca):   # adda künye zaten varsa ekleme
            return taban + "_" + "_".join(parca)
    except Exception:
        pass
    return taban


def zip_kutuphane(kaynak, hedef_dizin):
    """Üretici fotometri ZIP'ini çağıranın verdiği hedef klasöre açar.
    İçindeki IES/LDT/EULUMDAT dosyalarını hedefin köküne düz adla çıkarır, IES adına
    künye ekler (_NNd_NNNNlm), gömülü GLDF'leri gldf_ac ile açar. Çakışmada _2, _3...
    (var olan dosya ezilmez). Klasör yapısı, __MACOSX ve gizli dosyalar atlanır, boş
    ya da açılamayan fotometri dosyası warning ile bildirilir. Eklenen dosya adlarını döner."""
    os.makedirs(hedef_dizin, exist_ok=True)
    eklenen = []
    with zipfile.ZipFile(kaynak) as z:
        for n in z.namelist():
            taban_ad = os.path.basename(n)
            if not taban_ad or taban_ad.startswith(".") or "__MACOSX" in n:
                continue
            alt = taban_ad.lower()
            if not alt.endswith((".ies", ".ldt", ".eulumdat", ".gldf")):
                continue
            veri = z.read(n)
            if not veri:
                warnings.warn('ZIP fotometri üyesi boş, atlandı: %s' % n, RuntimeWarning)
                continue
            taban = os.path.splitext(taban_ad)[0]
            if alt.endswith(".gldf"):                 # gömülü GLDF bellekten kendi açıcısına gider
                try:
                    for yol in gldf_ac(io.BytesIO(veri), hedef_dizin, taban):
                        eklenen.append(os.path.basename(yol))
                except Exception as error:
                    warnings.warn('ZIP içindeki GLDF açılamadı, atlandı: %s (%s)' % (n, error),
                                  RuntimeWarning)
                continue
            uz = ".ies" if alt.endswith(".ies") else ".ldt"
            if uz == ".ies":
                # künye dosyadan okunur, geçici dosya çıkan adlarla çakışmayan gizli bir addır.
                fd, gecici = tempfile.mkstemp(prefix=".kunye-", suffix=uz, dir=hedef_dizin)
                try:
                    with os.fdopen(fd, "wb") as f:
                        f.write(veri)
                    taban = _ies_kunye_ad(taban, gecici)
                finally:
                    os.remove(gecici)
            eklenen.append(_yeni_dosya(hedef_dizin, taban, uz, notlu_ies(veri) if uz == ".ies" else veri))
    if not eklenen:
        raise RuntimeError("ZIP içinde kullanılabilir fotometri (IES/LDT/GLDF) bulunamadı")
    return eklenen


def _evo_obj(kaynak, hedef):
    """DIALux evo (.evo) aslında düz bir ZIP, içinde her mobilya/geometri için
    standart FBX var. Bütün FBX'leri çıkarır, assimp ile OBJ'ye çevirir, tek OBJ'de
    birleştirir (her parça bir 'g' grubu). Not: parçaların sahnedeki tam yerleşimi
    Boost ile serialize edilmiş ScenegraphScene'de durduğu için bazı parçalar local
    koordinatta gelebilir. Bu fonksiyon sadece FBX mesh çıkarır, STEP oda/armatür ve
    ürün fotometrisini dedektif ayrıca okur."""
    if not shutil.which("assimp"):
        raise RuntimeError("assimp kurulu değil (evo geometrisi için gerekli)")
    tmp = hedef + "_evotmp"
    os.makedirs(tmp, exist_ok=True)
    bv = bvt = bvn = parca = 0
    atilan = []
    MAX_UZANIM = 80.0     # büyük parçalar da korunur, birim kontrolü için warning verilir
    try:
        with zipfile.ZipFile(kaynak) as z, open(hedef, "w") as out:
            fbxler = [n for n in z.namelist() if n.lower().endswith(".fbx")]
            if not fbxler:
                raise RuntimeError("evo içinde FBX geometri yok (farklı DIALux sürümü olabilir)")
            for i, n in enumerate(fbxler):
                fp = os.path.join(tmp, "p%d.fbx" % i)
                with z.open(n) as src, open(fp, "wb") as f:
                    shutil.copyfileobj(src, f)
                op = os.path.join(tmp, "p%d.obj" % i)
                result = motor.surecler.calistir(['assimp', 'export', fp, op], cwd=tmp)
                if result.returncode != 0 or not os.path.exists(op):
                    warnings.warn('EVO FBX parçası atlandı: %s (%s)' %
                                  (n, (result.stderr or result.stdout)[-300:]), RuntimeWarning)
                    continue
                # parçayı belleğe al, birim kontrolü için bbox ölç
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
                uzanim = max(xx - xn, yx - yn, zx - zn) if vs else 0
                ad = n.split("/")[-2] if n.count("/") >= 2 else "parca_%d" % i
                if uzanim > MAX_UZANIM:
                    warnings.warn('EVO FBX birim sınırı: %s uzanım=%.1f, parça korundu' %
                                  (ad, uzanim), RuntimeWarning)
                out.write("g %s\n" % ad)
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
                bv += len(vs); bvt += len(vts); bvn += len(vns); parca += 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if parca == 0:
        raise RuntimeError("evo içindeki FBX'ler çevrilemedi")
    if atilan:
        print("evo _evo_obj: cop parcalar atlandi:", ", ".join(atilan))
    return hedef


def _evo_step_ents(evo_path):
    """ProjectData.dat (STEP metni) -> {id: (tip, gövde)} dict'i."""
    import re
    with zipfile.ZipFile(evo_path) as z:
        if "Project/ProjectData/ProjectData.dat" not in z.namelist():
            return None
        d = z.read("Project/ProjectData/ProjectData.dat").decode("utf-8", "replace")
    from core.step_reader import kayitlar
    return kayitlar(d)


def _evo_referanslar(body):
    """Tırnak içindeki #123 metindir, STEP referansı değildir."""
    import re
    disarisi = re.sub(r"'(?:[^']|'')*'", "", body)
    return [int(x) for x in re.findall(r'#(\d+)', disarisi)]


def _evo_metin(field):
    """STEP quoted metni çöz, eksik alanı ya da GUID'yi ad diye uydurma."""
    import re
    if not isinstance(field, str) or len(field) < 2 or field[0] != "'" or field[-1] != "'":
        return None
    value = field[1:-1].replace("''", "'")

    def unicode_text(match):
        try:
            return bytes.fromhex(match[2]).decode('utf-16-be' if match[1] == '2' else 'utf-32-be')
        except (ValueError, UnicodeError):
            raise ValueError('EVO metninde geçersiz Unicode kaçışı') from None

    return re.sub(r'\\X([24])\\([0-9A-Fa-f]+)\\X0\\', unicode_text, value)


def _evo_kimlik(ents, ident):
    """Kalıcı sayısal kimliği, dosya içi STEP satırını ve görünen adı ayırır."""
    import re
    from core.step_reader import alanlar
    fields = alanlar(ents[ident][1])
    source_id = fields[0] if fields and re.fullmatch(r'\d+', fields[0]) else 'step:%d' % ident
    result = {'kaynak_id': source_id, 'kaynak_kayit': '#%d' % ident}
    title = _evo_metin(fields[1]) if len(fields) > 1 else None
    if title and re.fullmatch(r'\{?[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\}?', title):
        result['kaynak_guid'] = title
    elif title:
        # eski ya da sentetik varyantlarda ikinci alan doğrudan ad olabilir.
        result['kaynak_ad'] = title
    for ref in _evo_referanslar(fields[2]) if len(fields) > 2 else []:
        if ents.get(ref, ('',))[0] != 'CommonInformationPropertySet':
            continue
        common = alanlar(ents[ref][1])
        title = _evo_metin(common[3]) if len(common) > 3 else None
        if title:
            result['kaynak_ad'] = title
    return result


def _evo_prototip_esle(ents):
    """Instance -> prototip -> fotometri temsil kaydı, kullanıcının verdiği ada bağlı değil."""
    elements, representations = {}, {}
    for _, (kind, body) in ents.items():
        if kind != 'RelDefinesByPrototype':
            continue
        refs = _evo_referanslar(body)
        prototypes = {r for r in refs if ents.get(r, ('',))[0] == 'LuminairePrototype'}
        instances = {r for r in refs if ents.get(r, ('',))[0] == 'LuminaireElement'}
        if instances and len(prototypes) > 1:
            raise ValueError('EVO armatürünün ürün ilişkisi tekil değil')
        for instance in instances:
            if not prototypes:
                continue
            prototype = next(iter(prototypes))
            if instance in elements and elements[instance] != prototype:
                raise ValueError('EVO armatürünün ürün ilişkisi çelişiyor')
            elements[instance] = prototype
    for ident, (kind, body) in ents.items():
        if kind != 'LuminairePrototype':
            continue
        candidates = set()
        for ref in _evo_referanslar(body):
            if ents.get(ref, ('',))[0] == 'PrototypeGeometricRepresentation':
                candidates.update(r for r in _evo_referanslar(ents[ref][1])
                                  if ents.get(r, ('',))[0] == 'LuminairePrototypeRepresentationData')
        if len(candidates) > 1:
            raise ValueError('EVO ürününün fotometri temsili tekil değil')
        if candidates:
            representations[ident] = next(iter(candidates))
    return elements, representations


def _evo_urun_meta(ents, prototype):
    """Ürün kimliği ve ArticleName metinleri, üreticinin CAD'i ya da ölçüsü değil."""
    from core.step_reader import alanlar
    identity = _evo_kimlik(ents, prototype)
    result = {key.replace('kaynak_', 'urun_', 1): value for key, value in identity.items()}
    fields = alanlar(ents[prototype][1])
    names = []
    for ref in _evo_referanslar(fields[2]) if len(fields) > 2 else []:
        if ents.get(ref, ('',))[0] != 'ProductDataPropertySet':
            continue
        for product_ref in _evo_referanslar(ents[ref][1]):
            if ents.get(product_ref, ('',))[0] != 'ProductData':
                continue
            product = alanlar(ents[product_ref][1])
            code = _evo_metin(product[0]) if product else None
            if code:
                result['urun_kod'] = code  # ProductData'nın kaynak kod alanı.
            for container_ref in _evo_referanslar(ents[product_ref][1]):
                if ents.get(container_ref, ('',))[0] != 'LanguageDependentTextContainer':
                    continue
                container = alanlar(ents[container_ref][1])
                if not container or not container[0].startswith('('):
                    continue
                for entry in alanlar(container[0][1:-1]):
                    if not entry.startswith('('):
                        continue
                    parts = alanlar(entry[1:-1])
                    if len(parts) != 2 or not parts[1].startswith('('):
                        continue
                    for label in alanlar(parts[1][1:-1]):
                        pair = alanlar(label[1:-1]) if label.startswith('(') else []
                        if len(pair) == 2 and pair[0] == '.ArticleName.':
                            title = _evo_metin(pair[1])
                            item = {'dil': parts[0], 'ad': title}
                            if title and item not in names:
                                names.append(item)
    if names:
        result['urun_adlari'] = names
        result['urun_ad'] = names[0]['ad']  # kaynaktaki sıra korunur, dil bilgisi de kalır.
    return result


def _evo_yardimci(ents):
    """STEP entity'leri için ortak helper'lar (alan bölme, vektör, CoordSys, refs)."""
    import re

    from core.step_reader import alanlar as top_fields

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
                raise ValueError('EVO koordinat sistemi dört sonlu 3B vektör içermeli') from None
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
        return _evo_referanslar(ents[i][1]) if i in ents else []
    return top_fields, vecs, cs, apply, rot, refs


def _evo_dunya_cs(ents):
    """Kat/yerleşim/eleman local frame'lerini kökten world'e doğru birleştirir."""
    alanlar, _, cs, apply, rot, refs = _evo_yardimci(ents)
    ebeveyn = {}
    # fiziksel grup, daha genel mekânsal kapsayıcının önüne geçer.
    for iliski in ("RelContainedInSpatialStructure", "RelAggregates"):
        for i, (tur, _) in ents.items():
            if tur == iliski:
                rr = refs(i)
                if rr:
                    for alt in rr[1:]:
                        ebeveyn[alt] = rr[0]
    birim = ((0., 0., 0.), (1., 0., 0.), (0., 1., 0.), (0., 0., 1.))
    hazir, ziyaret = {}, set()

    def dunya(i):
        if i in hazir:
            return hazir[i]
        if i in ziyaret:
            raise ValueError("EVO koordinat hiyerarşisinde döngü")
        ziyaret.add(i)
        try:
            yerel = birim
            if i in ents:
                f = alanlar(ents[i][1])
                if len(f) > 3:
                    rr = _evo_referanslar(f[3])
                    if rr:
                        yerel = cs(int(rr[0])) or birim
            if i in ebeveyn:
                ust = dunya(ebeveyn[i])
                C = (apply(ust, yerel[0]), *(rot(ust, v) for v in yerel[1:]))
            else:
                C = yerel
            hazir[i] = C
            return C
        finally:
            ziyaret.discard(i)
    return dunya, ebeveyn


def evo_armaturler(evo_path, max_arm=4000):
    """DIALux .evo ProjectData.dat (STEP) -> armatürler, world konumlarıyla.
    Yerleşim = kat/üst nesneler ∘ arrangement ∘ elemanın local frame'i.
    Emisyon = birleşik frame'in -z ekseni (downlight aşağı bakar)."""
    if type(max_arm) is not int or max_arm < 0:
        raise ValueError('EVO armatür sınırı sıfır veya pozitif tamsayı olmalı')
    ents = _evo_step_ents(evo_path)
    if not ents:
        return []
    top_fields, vecs, cs, apply, rot, refs = _evo_yardimci(ents)
    dunya, ebeveyn = _evo_dunya_cs(ents)
    el2proto, proto2eq = _evo_prototip_esle(ents)
    urun_meta = {p: _evo_urun_meta(ents, p) for p in set(el2proto.values())}

    def yerel_cs(i):
        f = top_fields(ents[i][1])
        rr = _evo_referanslar(f[3]) if len(f) > 3 else []
        return cs(int(rr[0])) if rr else None

    arms, gorulen = [], set()

    def ekle(C, entity):
        pos, xa, ya, za = C
        uzunluk = math.hypot(*za)
        if not all(math.isfinite(v) for vector in C for v in vector) or not math.isfinite(uzunluk) or uzunluk <= 0:
            raise ValueError('EVO armatür dünya dönüşümü veya ışık yönü geçersiz')
        yon = [-v / uzunluk for v in za]   # emisyon = world frame'in -z ekseni.
        meta = _evo_kimlik(ents, entity)
        ident = meta['kaynak_id']
        if ident in gorulen:
            raise ValueError('Tekrarlanan EVO armatür kaynak kimliği')
        gorulen.add(ident)
        prototype = el2proto.get(entity)
        if prototype is not None:
            meta.update(urun_meta[prototype])
            if prototype in proto2eq:
                meta['urun_temsil_kayit'] = '#%d' % proto2eq[prototype]
        parent = ebeveyn.get(entity)
        while parent is not None:
            if ents.get(parent, ('',))[0] == 'LuminaireArrangement':
                meta.update({k.replace('kaynak_', 'dizi_', 1): v
                             for k, v in _evo_kimlik(ents, parent).items()})
                break
            parent = ebeveyn.get(parent)
        # column-major world matrix: kaynaktaki roll ve scale kaybolmaz.
        meta['kaynak_dunya_matrisi'] = [*xa, 0.0, *ya, 0.0, *za, 0.0, *pos, 1.0]
        arms.append({"ad": "armatur_%s" % ident.replace(':', '_'),
                     "x": pos[0], "y": pos[1], "z": pos[2],
                     "renk": [1, 1, 1],
                     "renk_kaynagi": "varsayilan_notr",
                     "renk_notu": "EVO/STEP kaydındaki ışık rengi doğrulanamadı, nötr beyaz varsayılan kullanıldı. Kaynak renk sıcaklığı değildir.",
                     "guc": 25000, "yaricap": 0.18,
                     "tip": "spot", "aci": 90, "yon": yon, "kaynak": "evo/STEP", **meta})

    for i, (t, b) in ents.items():
        if t != "LuminaireElement":
            continue
        ust = ebeveyn.get(i)
        if not yerel_cs(i) and (ust not in ents or ents[ust][0] != "LuminaireArrangement"):
            warnings.warn('EVO armatür konumu çözülemedi, atlandı: #%d' % i, RuntimeWarning)
            continue
        C = dunya(i)
        ekle(C, i)
    if len(arms) > max_arm:
        warnings.warn('EVO armatür sınırı: okunan=%d, sınır=%d, atlanan=%d' %
                      (len(arms), max_arm, len(arms)-max_arm), RuntimeWarning)
    return arms[:max_arm]


def evo_ies(evo_path, proje_dir, reddedilen=None):
    """DIALux .evo STEP -> her ürün için gerçek IES (kandela + lümen) + ies2rad .rad/.dat.
    Zincir: LuminaireElement --RelDefinesByPrototype--> LuminairePrototype
    --PrototypeGeometricRepresentation--> (equipment id) -> LightDistribution (kandela)
    + LampTypeChannel (lümen). ies2rad proje_dir'den göreli path ile çalışır (path'teki
    boşluk ve .dat çözümü için). Döner: {kaynak_id(str): {'rad': göreli_yol, 'lumen': lm,
    ...ürün metadata'sı}}. Görünen ad eşleme key'i değildir.
    reddedilen listesi verilirse lümeni ya da kandela tablosu bozuk üründe exception
    yükselmez: ürün {'urun_temsil_kayit', 'neden', 'kullanim', ...ürün metadata'sı}
    olarak listeye eklenir, IES yazılmaz, diğer ürünler devam eder."""
    import re
    ents = _evo_step_ents(evo_path)
    if not ents:
        return {}
    cache_dir = os.path.join(proje_dir, "_cache", "ies")
    rel = "_cache/ies"
    top_fields, vecs, cs, apply, rot, refs = _evo_yardimci(ents)

    def R(i):
        return _evo_referanslar(ents[i][1]) if i in ents else []

    def nums(s):
        text = s.strip()
        if not (text.startswith('(') and text.endswith(')')):
            raise ValueError('EVO fotometri sayı dizisi geçersiz')
        values = [float(x.strip()) for x in text[1:-1].split(',')]
        if not values or not all(math.isfinite(x) for x in values):
            raise ValueError('EVO fotometri dizisi boş veya sonlu değil')
        return values

    el2proto, proto2eq = _evo_prototip_esle(ents)
    source_ids = {}
    for entity in el2proto:
        ident = _evo_kimlik(ents, entity)['kaynak_id']
        if ident in source_ids:
            raise ValueError('Tekrarlanan EVO armatür kaynak kimliği')
        source_ids[ident] = entity
    urun_meta = {p: _evo_urun_meta(ents, p) for p in set(el2proto.values())}
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
    eq2lm = {}                           # equipment -> lümen (LampTypeChannel 4. alan)
    eq2hata = {}                         # reddedilen verildiyse ürün başına lümen hatası
    for i, (t, b) in ents.items():
        if t == "LampTypeChannel":
            try:
                eq = R(i)[0]; lm = float(top_fields(b)[3])
            except (IndexError,ValueError,TypeError) as error:
                raise ValueError('EVO lümen kaydı eksik/geçersiz: #%d' % i) from error
            if not math.isfinite(lm) or lm <= 0:
                hata = 'EVO lümen değeri sonlu ve pozitif olmalı: #%d' % i
                if reddedilen is None:
                    raise ValueError(hata)
                eq2hata[eq] = hata
                continue
            eq2lm[eq] = lm

    os.makedirs(cache_dir, exist_ok=True)
    eq_rad = {}                          # equipment -> (rad_yol, lümen)
    for eq, dd in eq2dist.items():
        try:
            if eq in eq2hata:
                raise ValueError(eq2hata[eq])
            f = top_fields(ents[dd][1])
            if len(f) < 4:
                raise ValueError("EVO ışık dağılımı eksik: #%d" % dd)
            C = nums(f[1]); G = nums(f[2]); cd = nums(f[3])
            if eq not in eq2lm:
                raise ValueError('EVO ürün lümeni yok, 1000 lm varsayılmadı: #%d' % eq)
            lm = eq2lm[eq]
            if len(cd) != len(C)*len(G) or any(v < 0 for v in cd):
                raise ValueError('EVO kandela dizisi sayısı/değeri geçersiz: #%d' % dd)
            for angles,maximum in ((C,360),(G,180)):
                if any(v < 0 or v > maximum for v in angles) or any(a >= b for a,b in zip(angles,angles[1:])):
                    raise ValueError('EVO fotometri açı sırası/aralığı geçersiz: #%d' % dd)
            if not all(math.isfinite(v*lm/1000.0) for v in cd):
                raise ValueError('EVO fotometri ölçeklemesi taştı: #%d' % dd)
        except ValueError as error:
            if reddedilen is None:
                raise
            # ürün tek başına reddedilir, kimliği ve sahnede kaç kez kullanıldığı raporda kalır.
            prototipler = sorted(p for p, e in proto2eq.items() if e == eq and p in urun_meta)
            reddedilen.append({'urun_temsil_kayit': '#%d' % eq, 'neden': str(error),
                               'kullanim': sum(1 for p in el2proto.values() if proto2eq.get(p) == eq),
                               **(urun_meta[prototipler[0]] if prototipler else {})})
            continue
        ies = os.path.join(cache_dir, "urun_%d.ies" % eq)
        with open(ies, "w") as o:
            # LM-63-2002'nin dört zorunlu anahtar sözcüğü nötr değerle yazılır, kaynak programın adı girmez.
            o.write("IESNA:LM-63-2002\n[TEST] not available\n[TESTLAB] not available\n"
                    "[ISSUEDATE] not available\n[MANUFAC] not read from the project record\n")
            o.write("".join(s + "\n" for s in NOT_PROJE))
            o.write("TILT=NONE\n")
            o.write("1 %g 1 %d %d 1 2 0.3 0.3 0.1\n" % (lm, len(G), len(C)))
            o.write("1.0 1.0 0.0\n")
            o.write(" ".join("%g" % g for g in G) + "\n")
            o.write(" ".join("%g" % c for c in C) + "\n")
            per = len(G)
            for ci in range(len(C)):
                blok = cd[ci * per:(ci + 1) * per]
                o.write(" ".join("%g" % (v * lm / 1000.0) for v in blok) + "\n")
        # ies2rad'ı proje_dir'den göreli path ile çalıştır: .rad, .dat'a "_cache/ies/urun_N.dat"
        # diye göreli referans verir. Path'teki boşluk sorun olmaz, rpict cwd=proje_dir'den çözer.
        rel_ies = "%s/urun_%d.ies" % (rel, eq)
        rel_out = "%s/urun_%d" % (rel, eq)
        kod, _, hata = motor.sh('ies2rad -o "%s" "%s"' % (rel_out, rel_ies), cwd=proje_dir)
        if kod or not os.path.exists(os.path.join(cache_dir, "urun_%d.rad" % eq)):
            raise RuntimeError("EVO fotometrisi dönüştürülemedi (ürün %d): %s" % (eq, hata[-300:]))
        eq_rad[eq] = (rel_out + ".rad", lm)
    out = {}
    for ident, e in source_ids.items():
        p = el2proto[e]
        eq = proto2eq.get(p)
        if eq in eq_rad:
            out[ident] = {"rad": eq_rad[eq][0], "lumen": eq_rad[eq][1],
                          'kaynak_id': ident, 'urun_temsil_kayit': '#%d' % eq,
                          **urun_meta[p]}
    return out


def evo_fotometri_yaz(proje_dir, hedef_dizin, proje_ad=None):
    """evo_ies'in proje_dir altına çıkardığı ürün fotometrilerini (urun_*.ies) çağıranın
    verdiği hedef_dizin'e kopyalar. Ad: proje_<proje>_<tip>_<ışın>d_<lümen>lm_u<eq>.ies,
    <proje> proje_ad verilirse ondan, yoksa proje_dir adından türer. Var olanı tekrar kopyalamaz,
    okunamayan fotometri warning ile bildirilir. Veri verilen proje dosyasından gelir.
    Yeni kopyalanan dosya adlarını döner."""
    import sys, re
    ar = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools")
    if ar not in sys.path:
        sys.path.insert(0, ar)
    from core.tools import ies_analysis as ies_analiz
    kaynak_dir = os.path.join(proje_dir, "_cache", "ies")
    if not os.path.isdir(kaynak_dir):
        return []
    hedef = hedef_dizin
    os.makedirs(hedef, exist_ok=True)
    proje_ad = re.sub(r'[^\w]+', '_', proje_ad or os.path.basename(proje_dir.rstrip("/")))[:16].lower() or "proje"
    yazilan = []
    for f in sorted(os.listdir(kaynak_dir)):
        m = re.match(r'urun_(\d+)\.ies$', f)
        if not m:
            continue
        yol = os.path.join(kaynak_dir, f)
        try:
            d = ies_analiz.ies_oku(yol)
            beam, _ = ies_analiz.isin_acisi(d["dikey_acilar"], d["kandela"],
                                            d["n_dikey"], d["n_yatay"])
            lm = int(round(d["lumen_lamba"] or 0))
        except Exception as error:
            warnings.warn('EVO ürün fotometrisi okunamadı, atlandı: %s (%s)' % (f, error), RuntimeWarning)
            continue
        b2 = round((beam or 0) * 2)
        tip = "spot" if 0 < b2 <= 35 else ("downlight" if b2 <= 75 else "genis")
        ad = "proje_%s_%s_%dd_%dlm_u%s.ies" % (proje_ad, tip, b2, lm, m.group(1))
        h = os.path.join(hedef, ad)
        if os.path.exists(h):                     # aynı projeyi tekrar import edince kopya çoğalmasın
            continue
        shutil.copy(yol, h)
        yazilan.append(ad)
    return yazilan


def evo_odalar(evo_path, hedef_rad):
    """DIALux .evo ProjectData.dat (STEP) -> oda kabuğu (zemin + duvar + tavan), Radiance .rad.
    Space -> PolygonBasedSpaceRepresentationDataPart -> PolyPoint2D taban konturu + yükseklik
    kadar extrusion. World'e Space'in CoordSys'i ile oturtulur."""
    import re
    ents = _evo_step_ents(evo_path)
    if not ents:
        raise RuntimeError("ProjectData.dat yok")
    top_fields, vecs, cs, apply, rot, refs = _evo_yardimci(ents)

    def pp2d(i):                          # PolyPoint2D -> (x, y)
        if i in ents and ents[i][0] == "PolyPoint2D":
            m = re.search(r'\(\s*(-?[\d.eE+-]+)\s*,\s*(-?[\d.eE+-]+)\s*\)\s*$', ents[i][1].strip())
            if m:
                return (float(m.group(1)), float(m.group(2)))
        return None

    dunya, ebeveyn = _evo_dunya_cs(ents)
    konturlar = {}
    for i, (tur, _) in ents.items():
        if tur == "RelAssociatesStoreyContourBasedSpace":
            rr = refs(i)
            contour = next((r for r in rr if r in ents and ents[r][0] == "StoreyContour"), None)
            for r in rr:
                if contour is not None and r in ents and ents[r][0] == "Space":
                    konturlar.setdefault(r, []).append(contour)

    def temsil(nesne, tur):
        f = top_fields(ents[nesne][1])
        yigin = [int(x) for x in re.findall(r'#(\d+)', f[4])] if len(f) > 4 else []
        gor = set()
        while yigin:
            n = yigin.pop()
            if n in gor or n not in ents:
                continue
            gor.add(n)
            if ents[n][0] == tur:
                return n
            if "Representation" in ents[n][0]:
                yigin.extend(refs(n))
        return None

    def kontur_prizmasi(space, parca):
        import ast
        cc = konturlar.get(space, [])
        if len(cc) != 1:
            raise ValueError("EVO oda konturu tekil değil veya ilişki eksik")
        contour = cc[0]; data = temsil(contour, "StoreyContourRepresentationData")
        if data is None:
            raise ValueError("EVO kontur temsil verisi eksik")
        f = top_fields(ents[data][1])
        pts = ast.literal_eval(f[6])
        if not isinstance(pts, (tuple, list)) or len(pts) < 3 or any(
            not isinstance(p, (tuple, list)) or len(p) != 2 or
            any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in p) for p in pts):
            raise ValueError("EVO kontur noktaları geçersiz")
        pts = list(pts)
        if pts[0] == pts[-1]:
            pts.pop()
        if len(pts) < 3:
            raise ValueError("EVO konturu üç köşeden az")
        pf = top_fields(ents[parca][1]); h = float(pf[4])
        # bu varyantta alt temsilin frame'i identity olmalı, bilinmeyen yerleşimde tahmin yürütme.
        cr = re.findall(r'#(\d+)', pf[2]); alt_cs = cs(int(cr[0])) if cr else None
        birim = ((0., 0., 0.), (1., 0., 0.), (0., 1., 0.), (0., 0., 1.))
        if alt_cs != birim:
            raise ValueError("EVO kat konturunda özdeş olmayan alt çerçeve henüz desteklenmiyor")
        if pf[3] == ".Storey.":
            kat = ebeveyn.get(space); gor = set()
            while kat in ents and ents[kat][0] != "Storey" and kat not in gor:
                gor.add(kat); kat = ebeveyn.get(kat)
            if kat not in ents or ents[kat][0] != "Storey":
                raise ValueError("EVO oda için üst kat bulunamadı")
            kd = temsil(kat, "StoreyRepresentationData")
            if kd is None:
                raise ValueError("EVO kat yüksekliği bulunamadı")
            h = float(top_fields(ents[kd][1])[6])
        if not math.isfinite(h) or h <= 0:
            raise ValueError("EVO oda yüksekliği geçersiz")
        C = dunya(contour)
        return [apply(C, (x, y, 0.)) for x, y in pts], [apply(C, (x, y, h)) for x, y in pts]

    def prizma_ekle(floor, ceil):
        # Radiance normali sağ el kuralıyla köşe sırasından gelir, üç yüz de odanın içine bakmalı.
        # Taban saat yönünde saklandıysa (normali tavandan uzağa bakıyorsa) önce ters çevrilir.
        nx = ny = nz = 0.0
        for k, (x, y, z) in enumerate(floor):           # Newell normali
            x2, y2, z2 = floor[(k+1) % len(floor)]
            nx += (y - y2) * (z + z2); ny += (z - z2) * (x + x2); nz += (x - x2) * (y + y2)
        yukari = [c - f for c, f in zip(ceil[0], floor[0])]
        if nx * yukari[0] + ny * yukari[1] + nz * yukari[2] < 0:
            floor, ceil = floor[::-1], ceil[::-1]
        polys.append(("zemin", floor)); polys.append(("tavan", list(reversed(ceil))))
        for k in range(len(floor)):
            sonraki = (k+1) % len(floor)
            polys.append(("duvar", [floor[sonraki], floor[k], ceil[k], ceil[sonraki]]))

    polys = []   # (world köşe listesi)
    for i, (t, b) in ents.items():
        if t != "Space":
            continue
        kat_parca = temsil(i, "StoreyContourBasedSpaceRepresentationDataPart")
        if kat_parca is not None:
            prizma_ekle(*kontur_prizmasi(i, kat_parca))
            continue
        spaceCS = dunya(i)
        # Space alt ağacında PolygonBasedSpaceRepresentationDataPart'ı bul
        f = top_fields(b)
        yigin = [int(r) for r in re.findall(r"#(\d+)", f[4])] if len(f) > 4 else []
        gor = set()
        while yigin:
            j = yigin.pop()
            if j in gor or j not in ents:
                continue
            gor.add(j); jt, jb = ents[j]
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
                    raise ValueError('EVO poligonu üç geçerli köşeden az')
                prizma_ekle([apply(spaceCS, (p[0], p[1], 0.0)) for p in pts],
                            [apply(spaceCS, (p[0], p[1], h)) for p in pts])
                if height_inferred:
                    height_note = 'EVO poligon oda yüksekliği doğrulanmamıştır: 1.5 < değer < 30 koşulunu sağlayan ilk skalerden %g m tahmin edildi; alanın yükseklik olduğu belirlenmemiştir.' % h
                else:
                    height_note = 'EVO poligon oda yüksekliği doğrulanmamıştır: 1.5 < değer < 30 koşulunu sağlayan skaler yok; varsayılan 3 m yükseklik kullanılıyor.'
                warnings.warn(height_note, RuntimeWarning, stacklevel=2)
            elif "Representation" in jt:
                yigin.extend(refs(j))
    if not polys:
        raise RuntimeError("evo içinde Space/oda geometrisi bulunamadı")
    # düz gri yerine gerçekçi malzeme: ahşap zemin, sıcak duvar, beyaz tavan
    with open(hedef_rad, "w") as fp:
        fp.write("# DIALux evo odaları (STEP ProjectData.dat): zemin+duvar+tavan\n")
        # Örnek malzeme varsayılanları; yansıtma değerleri kaynak projeden okunmamıştır.
        fp.write("void plastic zemin_mat 0 0 5 0.33 0.22 0.13 0 0\n")   # ahşap kahve (rho_v~0.24)
        fp.write("void plastic duvar_mat 0 0 5 0.52 0.49 0.44 0 0\n")   # sıcak duvar (rho_v~0.50)
        fp.write("void plastic tavan_mat 0 0 5 0.75 0.75 0.73 0 0\n")   # tavan (rho_v~0.75)
        mat = {"zemin": "zemin_mat", "duvar": "duvar_mat", "tavan": "tavan_mat"}
        for n, (tip, poly) in enumerate(polys):
            fp.write("%s polygon oda_%d\n0\n0\n%d\n" % (mat[tip], n, len(poly) * 3))
            for v in poly:
                fp.write(" %g %g %g\n" % (v[0], v[1], v[2]))
    return hedef_rad, len(polys)


def evo_mobilyalar(evo_path, hedef_rad, max_mob=4000):
    """DIALux .evo ProjectData.dat (STEP) -> mobilya kutuları (yerleşim + yön + ölçü), Radiance .rad.
    Her FurnitureElement'in 4. alanı world CoordSys'i, 5. alanı MappedFurniture...
    -> temsil verisi. ExtrudedPolygon tipinde son vektör gerçek ölçü, prototip tipinde
    prototipin doğal boyutuna göre scale (yaklaşık kutu). Kutular odanın içine oturur."""
    import re
    if type(max_mob) is not int or max_mob < 1:
        raise ValueError('EVO mobilya sınırı pozitif tamsayı olmalı')
    ents = _evo_step_ents(evo_path)
    if not ents:
        raise RuntimeError("ProjectData.dat yok")
    top_fields, vecs, cs, apply, rot, refs = _evo_yardimci(ents)

    dunya, _ = _evo_dunya_cs(ents)

    def ilk_ref(s):
        m = re.search(r'#(\d+)', s)
        return int(m.group(1)) if m else None

    kutular = []   # (merkez CoordSys, (sx, sy, sz), tam_olcu_mu)
    for i, (t, b) in ents.items():
        if t != "FurnitureElement":
            continue
        f = top_fields(b)
        if len(f) < 5:
            warnings.warn('EVO mobilya alanları eksik, atlandı: #%d' % i, RuntimeWarning)
            continue
        C = cs(ilk_ref(f[3])) if len(f) > 3 else None       # 4. alan = world CoordSys
        if not C:
            warnings.warn('EVO mobilya konumu çözülemedi, atlandı: #%d' % i, RuntimeWarning)
            continue
        C = dunya(i)
        boyut, tam = (0.5, 0.5, 0.7), False
        mr = ilk_ref(f[4]) if len(f) > 4 else None
        if mr and mr in ents:
            dat = ilk_ref(top_fields(ents[mr][1])[-1])
            if dat and dat in ents:
                dt = ents[dat][0]
                dv = vecs(ents[dat][1])
                if dv:
                    son = dv[-1]
                    if dt == "MappedExtrudedPolygonFurnitureGeometricRepresentationData":
                        boyut, tam = son, True                # gerçek ölçü
                    else:                                     # prototip: scale'den yaklaşık kutu
                        d = [max(0.15, min(2.2, s if abs(s - 1) > 1e-3 else v))
                             for s, v in zip(son, (0.55, 0.55, 0.72))]
                        boyut = tuple(d)
        kutular.append((C, boyut, tam))
    if len(kutular) > max_mob:
        warnings.warn('EVO mobilya sınırı: okunan=%d, sınır=%d, atlanan=%d' %
                      (len(kutular), max_mob, len(kutular)-max_mob), RuntimeWarning)
        kutular = kutular[:max_mob]
    if kutular:
        warnings.warn('EVO mobilya geometrisi yerleşim kutuları ile yaklaşık temsil edildi.', RuntimeWarning)
    if not kutular:
        raise RuntimeError("evo içinde FurnitureElement bulunamadı")

    def kutu_kose(C, s):
        hx, hy, sz = s[0] / 2.0, s[1] / 2.0, s[2]            # yatayda ortalı, taban z=0
        yerel = [(-hx, -hy, 0), (hx, -hy, 0), (hx, hy, 0), (-hx, hy, 0),
                 (-hx, -hy, sz), (hx, -hy, sz), (hx, hy, sz), (-hx, hy, sz)]
        return [apply(C, p) for p in yerel]

    yuz = [(0, 1, 2, 3), (7, 6, 5, 4), (0, 4, 5, 1),
           (1, 5, 6, 2), (2, 6, 7, 3), (3, 7, 4, 0)]         # alt, üst, 4 yan
    with open(hedef_rad, "w") as fp:
        fp.write("# DIALux evo mobilyaları (STEP ProjectData.dat): yerleşim kutuları\n")
        fp.write("void plastic mob_mat 0 0 5 0.62 0.5 0.38 0 0\n")
        fp.write("void plastic mob_tam 0 0 5 0.5 0.55 0.6 0 0\n")
        n = 0
        for C, s, tam in kutular:
            k = kutu_kose(C, s)
            mat = "mob_tam" if tam else "mob_mat"
            for fy in yuz:
                fp.write("%s polygon mob_%d\n0\n0\n%d\n" % (mat, n, 4 * 3))
                for idx in fy:
                    v = k[idx]
                    fp.write(" %g %g %g\n" % (v[0], v[1], v[2]))
                n += 1
    return hedef_rad, len(kutular)


def cevir(kaynak, hedef_obj=None):
    """Kaynağı deterministik olarak OBJ'ye çevirir. hedef_obj döner."""
    kaynak = os.path.abspath(kaynak)
    uz = os.path.splitext(kaynak)[1].lower()
    hedef_obj = hedef_obj or (os.path.splitext(kaynak)[0] + ".obj")
    if uz == ".obj":
        return kaynak
    if uz in motor.CEVIRICI_ASSIMP:
        from core.assimp_bridge import cevir as assimp_cevir
        assimp_cevir(kaynak, hedef_obj)
    elif uz in motor.CEVIRICI_USD:
        _usd_obj(kaynak, hedef_obj)
    elif uz == ".evo":
        _evo_obj(kaynak, hedef_obj)
    elif uz in LUMION_UZ:
        _lumion_obj(kaynak, hedef_obj)
    else:
        raise RuntimeError("bu format için deterministik çevirici yok: " + uz)
    return hedef_obj

# facade iki import sırasında da çalışır, çağrılar modül yüklendikten sonra yapılır.
from core import engine as motor
