# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""
Dosya dedektifi: verilen klasördeki dosyaları tanır, status + warning üretir.
Revit/DIALux/Rhino/SketchUp/3ds Max gibi kaynaklar için baştan uyarır, iş
ortasında sürpriz çıkmasın. EVO bileşenlerini çeviri komutu için okur
(_evo_yerlestir), dosya taşımaz.

durum değerleri:
  hazir        -> doğrudan kullanılır, projeye kopyalanır
  cevrilir     -> biz çeviririz (yerel converter), kopyalanır
  disari_aktar -> kaynak programdan export gerekir (talimatlı warning)
  olmaz        -> kullanılamaz (nedeni + varsa alternatif yol)
"""
import os, zipfile
from core import engine as motor  # deterministik converter'lar (assimp/usd-core)


def fotometri_paketi_mi(yol):
    """Üretici fotometri ZIP'i mi? (içinde IES/LDT/EULUMDAT/GLDF olan, proje
    paketi olmayan zip, `urunler` komutu bunu IES dosyalarına çıkarır)"""
    try:
        if not zipfile.is_zipfile(yol):
            return False
        with zipfile.ZipFile(yol, "r") as z:
            adlar = z.namelist()
            if "proje.json" in adlar:
                return False
            return any(a.lower().endswith((".ies", ".ldt", ".eulumdat", ".gldf"))
                       for a in adlar if "__MACOSX" not in a)
    except Exception:
        return False

# assimp/usd-core ile OBJ'ye çevrilebilen kaynaklar (kuruluysa "cevrilir")
CEVIRICILI = {
    ".fbx": "FBX (assimp)", ".dae": "Collada (assimp)", ".gltf": "glTF (assimp)",
    ".glb": "glTF (assimp)", ".stl": "STL (assimp)", ".3ds": "3DS (assimp)",
    ".usd": "USD (usd-core)", ".usda": "USD (usd-core)", ".usdc": "USD (usd-core)",
    ".usdz": "USDZ (usd-core)",
}

# uzanti -> (durum, hedef_klasor, tur_adi, mesaj)
TABLO = {
    ".obj":  ("hazir", "model", "Model (OBJ)",
              "Doğrudan kullanılır. Yanında .mtl olmalı (malzeme adları için)."),
    ".mtl":  ("hazir", "model", "Malzeme listesi (MTL)",
              "OBJ'nin eşi, malzeme eşleştirme tablosu bundan kurulur."),
    ".rad":  ("hazir", "model", "Radiance geometri",
              "Doğrudan kullanılır."),
    ".rtm":  ("hazir", "model", "Radiance mesh",
              "Doğrudan kullanılır (UV korumalı)."),
    ".ies":  ("hazir", "isik", "Armatür fotometrisi (IES)",
              "Doğrudan kullanılır, otomatik künye analizi yapılır."),
    ".jpg":  ("cevrilir", "doku", "Görsel (JPG)",
              "Dokuysa .pic'e çevrilir, adı 'ref_' ile başlıyorsa referans görsel sayılır."),
    ".jpeg": ("cevrilir", "doku", "Görsel (JPG)", "Dokuysa .pic'e çevrilir."),
    ".png":  ("cevrilir", "doku", "Görsel (PNG)",
              "Dokuysa .pic'e çevrilir, 'ref_' önekli ise referans."),
    ".tif":  ("cevrilir", "doku", "Görsel (TIFF)", "ra_tiff ile .hdr'a çevrilir."),
    ".tiff": ("cevrilir", "doku", "Görsel (TIFF)", "ra_tiff ile .hdr'a çevrilir."),
    ".hdr":  ("hazir", "doku", "Radiance görüntü/doku", "Doğrudan kullanılır."),
    ".pic":  ("hazir", "doku", "Radiance görüntü/doku", "Doğrudan kullanılır."),
    ".csv":  ("hazir", ".", "Ürün listesi", "IES analizleriyle karşılaştırılır."),
    ".xlsx": ("cevrilir", ".", "Ürün listesi (Excel)",
              "Okunur, mümkünse CSV tercih et (daha sağlam)."),
    ".txt":  ("hazir", ".", "Not/liste", "Proje köküne alınır."),
    ".pdf":  ("hazir", ".", "Döküman/föy", "Proje köküne alınır (armatür föyü vb.)."),
    ".vf":   ("hazir", "gorunum", "Kamera görünümü", "Doğrudan kullanılır."),
    # ---- kaynak programlar: export talimatı ----
    ".skp":  ("disari_aktar", None, "SketchUp",
              "Doğrudan okunmaz. SketchUp'ta: File > Export > 3D Model > OBJ "
              "(Export texture maps, Triangulate all faces). OBJ+MTL'yi buraya at."),
    ".3dm":  ("disari_aktar", None, "Rhino",
              "Doğrudan okunmaz. Rhino'da: File > Export Selected > OBJ "
              "(malzemeleri koru, üçgenleştir). OBJ+MTL'yi buraya at."),
    ".rvt":  ("disari_aktar", None, "Revit",
              "Doğrudan okunmaz. Revit'te: 3B görünüm aç > Export > FBX, sonra "
              "3ds Max/Blender ile OBJ'ye çevir. (Alternatif: Export > IFC.) "
              "Malzeme adlarının anlamlı olmasına dikkat et."),
    ".rfa":  ("disari_aktar", None, "Revit ailesi",
              "Aile dosyası tek başına kullanılmaz, projeyi (.rvt) OBJ/FBX olarak aktar."),
    ".max":  ("disari_aktar", None, "3ds Max",
              "Doğrudan okunmaz. Max'te: Export > OBJ (malzemeler dahil). "
              "FBX yerine OBJ tercih et."),
    ".fbx":  ("disari_aktar", None, "FBX",
              "Radiance FBX okumaz. Blender (ücretsiz) ile aç > Export > OBJ, "
              "veya kaynak programdan doğrudan OBJ al."),
    ".dae":  ("disari_aktar", None, "Collada",
              "Doğrudan okunmaz, Blender ile OBJ'ye çevir."),
    ".dwg":  ("disari_aktar", None, "AutoCAD",
              "2B/3B DWG doğrudan okunmaz. 3B ise OBJ dışa aktar, 2B çizim "
              "sadece referans olur."),
    ".dxf":  ("disari_aktar", None, "DXF",
              "Doğrudan okunmaz, kaynak programdan OBJ al."),
    ".ifc":  ("disari_aktar", None, "IFC",
              "Doğrudan okunmaz. Blender+BIM eklentisi ile açıp OBJ dışa aktar."),
    ".stl":  ("cevrilir", "model", "STL",
              "Çevrilebilir ama MALZEMESİZ gelir (tek parça), mümkünse OBJ tercih et."),
    ".evo":  ("cevrilir", "model", "DIALux evo projesi",
              "FBX varsa geometrisi, FBX olmasa da STEP oda ve armatür kayıtları okunur. M3D/GDMS ayrıntıları desteklenmez. Desteklenen dosyalarda "
              "ürün fotometrisi IES'e çıkarılır, Radiance hazırlığı için ies2rad gerekir. "
              "Parça konumları, birimler ve fiziksel sonuçlar kaynak sahneyle kontrol edilmeli."),
    ".ldt":  ("hazir", "isik", "Armatür fotometrisi (EULUMDAT/LDT)",
              "Otomatik IES'e çevrilir (simetri açılır, mutlak cd), `urunler` "
              "komutu ürün tablosuna da çıkarır."),
    ".gldf": ("hazir", "isik", "GLDF armatür (DIALux açık format)",
              "Açık ZIP, içindeki IES/LDT fotometriyi `urunler` komutu çıkarır."),
    ".stf":  ("disari_aktar", None, "DIALux STF (eski değişim formatı)",
              "DIALux 4 CAD değişim formatı. evo'da mümkünse IFC'yi tercih et, "
              "gerekirse bir CAD'e alıp OBJ/IFC dışa aktar."),
    ".skb":  ("olmaz", None, "SketchUp yedeği", "Yedek dosya, gerekmez."),
    ".zip":  ("olmaz", None, "ZIP arşivi",
              "Genel ZIP işlenmez. Üretici fotometri paketini `urunler` komutu çıkarır."),
}

# Lumion proje dosyası (tools/ls10_reader.py): sahne envanteri + gömülü texture.
# geometri ve light okuma örnek dosyada doğrulandı, her versiyon için garanti yok.
_LUMION_MESAJ = (
    "Örüntü okuyucusu gömülü mesh ve ışık kayıtlarını çıkarır, tam geometri kopyası "
    "saklanır, ince yüzler otomatik silinmez. Dosya sürümüne göre eksik varlık olabilir. "
    "Işık yoğunlukları yaklaşık eşlenir, fiziksel fotometri ayrıca doğrulanmalı.")
for _e in (".ls10",):
    TABLO[_e] = ("cevrilir", "model", "Lumion projesi", _LUMION_MESAJ)

KAYNAK_TAHMIN = {".skp": "SketchUp", ".3dm": "Rhino", ".rvt": "Revit",
                 ".rfa": "Revit", ".max": "3ds Max", ".evo": "DIALux",
                 ".fbx": "3ds Max/FBX", ".obj": "OBJ (SketchUp/Rhino/Max olabilir)",
                 ".ls": "Lumion", ".ls9": "Lumion", ".ls10": "Lumion", ".ls11": "Lumion",
                 ".ls12": "Lumion", ".lsf": "Lumion"}


def analiz(klasor):
    """Klasördeki dosyaları tanı -> [{dosya, tur, durum, hedef, mesaj}], kaynak_tahmini"""
    sonuc, kaynaklar = [], []
    if not os.path.isdir(klasor):
        return sonuc, None
    from pathlib import Path
    # klasör içindeki göreli alt klasörleri koru, işlenmiş ya da yükleme arşivlerini tarama.
    yollar = []
    for kok, dirs, fs in os.walk(klasor, followlinks=False):
        dirs[:] = [d for d in dirs if not d.startswith((".", "_"))
                   and not os.path.islink(os.path.join(kok, d))]
        yollar.extend(os.path.relpath(os.path.join(kok, f), klasor) for f in fs
                      if not os.path.islink(os.path.join(kok, f)))
    for ad in sorted(yollar):
        tam = os.path.join(klasor, ad)
        if ad.startswith(".") or ad.startswith("OKUBENI") or os.path.isdir(tam):
            continue
        uz = os.path.splitext(ad)[1].lower()
        durum, hedef, tur, mesaj = TABLO.get(
            uz, ("olmaz", None, "Bilinmeyen (%s)" % uz,
                 "Bu dosya türü desteklenmiyor."))
        # üretici fotometri ZIP'i: içindeki IES/LDT/GLDF'yi `urunler` komutu çıkarır
        if uz == ".zip" and fotometri_paketi_mi(tam):
            durum, hedef, tur = "cevrilir", "urunler", "Üretici fotometri paketi (ZIP)"
            mesaj = ("İçindeki IES/LDT/GLDF fotometrileri `urunler` komutu IES dosyalarına "
                     "çıkarır (adlara ışın+lümen künyesi yazılır).")
        # assimp/usd-core kuruluysa bu formatlar deterministik olarak OBJ'ye çevrilir
        if uz in CEVIRICILI and motor.cevrilebilir(uz):
            durum, hedef, tur = "cevrilir", "model", CEVIRICILI[uz]
            mesaj = "Deterministik olarak OBJ'ye çevrilir, sonra Radiance mesh'i yapılır."
        if uz in (".jpg", ".jpeg", ".png") and ad.lower().startswith("ref_"):
            tur, hedef, durum = "Referans görsel", ".", "hazir"
            mesaj = "Referans olarak proje köküne alınır."
        sonuc.append({"dosya": ad, "tur": tur, "durum": durum,
                      "hedef": hedef, "mesaj": mesaj})
        if uz in KAYNAK_TAHMIN:
            kaynaklar.append(KAYNAK_TAHMIN[uz])
    kaynak = kaynaklar[0] if kaynaklar else None
    return sonuc, kaynak


def uyarilar(dosyalar, istenen_ciktilar):
    """Eksik gereklilikler için warning listesi."""
    u = []
    adlar = [d["dosya"].lower() for d in dosyalar]
    var = lambda uz: any(a.endswith(uz) for a in adlar)
    kullanilabilir_model = any(
        d["durum"] in ("hazir", "cevrilir") and d["hedef"] == "model"
        and not d["dosya"].lower().endswith(".mtl") for d in dosyalar)
    if not kullanilabilir_model:
        u.append("Kullanılabilir MODEL yok (OBJ/RAD/RTM). Kaynak programdan "
                 "OBJ dışa aktarımı gerekiyor, dosya listesindeki talimata bak.")
    if var(".obj") and not var(".mtl"):
        u.append("OBJ var ama .MTL yok, malzeme eşleştirmesi eksik kalabilir. MTL dosyasını da ekleyin.")
    gece_istendi = any(c in ("gece", "luks", "saat_tarama_gece")
                       for c in istenen_ciktilar)
    if gece_istendi and not var(".ies"):
        u.append("GECE/lüks çıktısı istendi ama hiç .IES yok, armatür "
                 "fotometrisi olmadan gece sahnesi kurulamaz. Üretici sitesinden "
                 "IES indir.")
    for d in dosyalar:
        if d["durum"] == "olmaz":
            u.append("%s: %s" % (d["dosya"], d["mesaj"]))
        elif d["durum"] == "disari_aktar":
            u.append("%s: %s" % (d["dosya"], d["mesaj"]))
    return u


def _evo_yerlestir(kaynak, proje_dir, hedef_obj, rapor, hatalar):
    """Bağımsız EVO bileşenlerini kurtar, desteklenmeyen ayrıntıyı kısmi diye bildir."""
    import re
    modeller, armaturler = [], []
    with zipfile.ZipFile(kaynak) as z:
        uzantilar = {os.path.splitext(n)[1].lower() for n in z.namelist()}
    if ".fbx" in uzantilar:
        try:
            motor.cevir(kaynak, hedef_obj)
            modeller.append(os.path.relpath(hedef_obj, proje_dir))
            rapor.append("EVO gömülü FBX geometrisi OBJ'ye çevrildi")
        except Exception as e:
            hatalar.append("EVO FBX: " + str(e))
    else:
        rapor.append("EVO'da FBX yok, STEP oda ve armatür verileri ayrıca okunuyor")
    eksik = sorted(uzantilar & {".m3d", ".gdms"})
    if eksik:
        hatalar.append("EVO gömülü " + "/".join(eksik) +
                       " geometri ayrıntıları henüz desteklenmiyor, STEP oda ve yaklaşık mobilya kutuları kullanılabilir")
    try:
        ents = motor._evo_step_ents(kaynak) or {}
    except Exception as e:
        ents = {}; hatalar.append("EVO STEP: " + str(e))
    turler = {t for t, _ in ents.values()}
    if not ents:
        hatalar.append("EVO okunabilir STEP kaydı içermiyor")
    if "LuminaireElement" in turler:
        try:
            armaturler = motor.evo_armaturler(kaynak)
            rapor.append("EVO STEP: %d armatür konumu okundu" % len(armaturler))
            if not armaturler:
                hatalar.append("EVO armatür kayıtları var ancak konumları çözülemedi")
        except Exception as e:
            hatalar.append("EVO armatür: " + str(e))
        try:
            urunler = motor.evo_ies(kaynak, proje_dir)
            bagli = 0
            for armatur in armaturler:
                kimlik = armatur.get('kaynak_id')
                if kimlik is None:
                    # eski kayıt uyumu: kullanıcının verdiği adın sonundaki
                    # sayı ürün kimliği sayılmaz, sadece eski teknik ad.
                    eski = re.fullmatch(r'armatur_(\d+)', str(armatur.get('ad', '')))
                    kimlik = eski[1] if eski else None
                u = urunler.get(str(kimlik)) if kimlik is not None else None
                if u:
                    armatur["urun_rad"] = u["rad"]; armatur["lumen"] = u["lumen"]
                    for alan in ('kaynak_id', 'urun_id', 'urun_guid', 'urun_kayit',
                                 'urun_ad', 'urun_adlari', 'urun_kod', 'urun_temsil_kayit'):
                        if alan in u:
                            armatur.setdefault(alan, u[alan])
                    bagli += 1
            rapor.append("EVO ürün fotometrisi %d armatüre bağlandı" % bagli)
            if bagli < len(armaturler):
                hatalar.append("%d EVO armatüründe ürün fotometrisi eşleşmedi, güçleri yaklaşık" % (len(armaturler)-bagli))
        except Exception as e:
            hatalar.append("EVO fotometri: " + str(e))
    for tur, ad, oku in (("Space", "oda_evo.rad", motor.evo_odalar),
                         ("FurnitureElement", "mobilya_evo.rad", motor.evo_mobilyalar)):
        if tur not in turler:
            continue
        hedef = os.path.join(os.path.dirname(hedef_obj), ad)
        try:
            _, n = oku(kaynak, hedef)
            if n:
                modeller.append(os.path.relpath(hedef, proje_dir))
                rapor.append("EVO STEP %s: %d öğe" % (tur, n))
            else:
                hatalar.append("EVO %s kayıtları geometri üretmedi" % tur)
        except Exception as e:
            hatalar.append("EVO %s: %s" % (tur, e))
    if not modeller and not armaturler:
        hatalar.append("EVO'dan kullanılabilir geometri veya armatür çıkarılamadı")
    return modeller, armaturler
