#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""
IES analiz: armatürün fotometri dosyasını okur, Türkçe bir künye çıkarır.
Kullanım:  python3 ies_analysis.py dosya.ies [dosya2.ies ...]
Ne verir:  lümen, watt, verim (lm/W), tepe kandela, beam açısı,
           simetri, birim kontrolü, Kelvin tahmini, Radiance warning'leri.
"""
import sys, os, re, math


def _sayilar(metin):
    # sayısal gövdede bozuk token'ı atlarsak header alanları kayar.
    values = [float(x) for x in re.split(r"[\s,]+", metin.strip()) if x]
    if not all(math.isfinite(x) for x in values):
        raise ValueError("IES sayısal gövdesinde sonlu olmayan değer var")
    return values


def ies_oku(yol):
    with open(yol, "rb") as f:
        ham = f.read()
    non_ascii = sum(1 for b in ham if b > 127)
    metin = ham.decode("latin-1")
    satirlar = metin.splitlines()

    anahtarlar = {}
    tilt_i = None
    for i, ln in enumerate(satirlar):
        m = re.match(r"\s*\[(\w+)\]\s*(.*)", ln)
        if m:
            anahtarlar[m.group(1).upper()] = m.group(2).strip()
        if ln.strip().upper().startswith("TILT="):
            tilt_i = i
            tilt = ln.strip()[5:].strip()
            break
    if tilt_i is None:
        raise ValueError("TILT satırı yok, geçerli bir IES değil.")

    govde = " ".join(satirlar[tilt_i + 1:])
    n = _sayilar(govde)
    if tilt.upper() == "INCLUDE":
        # tilt bloğunu atla: lamp2tilt, çift sayısı, n açı, n çarpan
        if len(n) < 2 or n[1] < 0 or not n[1].is_integer():
            raise ValueError('IES TILT tablosu bozuk')
        adet = int(n[1])
        if len(n) < 2 + 2 * adet + 13:
            raise ValueError('IES TILT tablosu eksik')
        n = n[2 + 2 * adet:]
        # baştaki lamp-to-luminaire (n[0]) da atlandı
    elif tilt.upper() != 'NONE':
        raise ValueError('Harici IES TILT dosyası desteklenmiyor')
    if len(n) < 13:
        raise ValueError("Fotometrik satırlar eksik/bozuk.")

    lamba      = int(n[0]);  lumen_lamba = n[1];  carpan = n[2]
    if any(x < 1 or not x.is_integer() for x in (n[3], n[4])):
        raise ValueError('IES açı sayıları pozitif tamsayı olmalı')
    n_dikey    = int(n[3]);  n_yatay     = int(n[4])
    fot_tip    = int(n[5]);  birim       = int(n[6])
    en, boy, yuk = n[7], n[8], n[9]
    balast     = n[10];      gelecek     = n[11];  watt = n[12]

    kalan   = n[13:]
    dikey_a = kalan[:n_dikey]
    yatay_a = kalan[n_dikey:n_dikey + n_yatay]
    kandela = kalan[n_dikey + n_yatay:n_dikey + n_yatay + n_dikey * n_yatay]
    beklenen = n_dikey + n_yatay + n_dikey * n_yatay
    if len(kalan) != beklenen:
        raise ValueError('IES açı/kandela tablosu boyu uyuşmuyor: beklenen=%d, bulunan=%d' %
                         (beklenen, len(kalan)))
    if carpan < 0 or any(x < 0 for x in kandela):
        raise ValueError('IES kandela/çarpan negatif olamaz')

    return {
        "dosya": os.path.basename(yol), "anahtarlar": anahtarlar,
        "lamba": lamba, "lumen_lamba": lumen_lamba, "carpan": carpan,
        "n_dikey": n_dikey, "n_yatay": n_yatay, "fot_tip": fot_tip,
        "birim": birim, "boyut": (en, boy, yuk),
        "balast": balast, "watt": watt,
        "dikey_acilar": dikey_a, "yatay_acilar": yatay_a, "kandela": kandela,
        "non_ascii": non_ascii,
    }


def isin_acisi(dikey_a, kandela, n_dikey, n_yatay):
    """İlk yatay düzlemde değerin tepenin %50'sine düştüğü açı -> beam angle."""
    if not kandela or n_dikey == 0:
        return None, None
    duzlem = kandela[:n_dikey]
    tepe = max(duzlem)
    if tepe <= 0:
        return None, tepe
    yarim = None
    for i in range(len(duzlem)):
        if duzlem[i] < tepe / 2.0:
            if i == 0:
                yarim = dikey_a[0]
            else:
                # linear interpolation
                a0, a1 = dikey_a[i - 1], dikey_a[i]
                c0, c1 = duzlem[i - 1], duzlem[i]
                yarim = a0 + (a1 - a0) * (c0 - tepe / 2.0) / max(c0 - c1, 1e-9)
            break
    return (2 * yarim if yarim is not None else None), tepe


def kelvin_tahmini(v):
    kaynaklar = [v["dosya"]] + list(v["anahtarlar"].values())
    for s in kaynaklar:
        m = re.search(r"(\d{4})\s*K", s, re.I)
        if m and 1500 <= int(m.group(1)) <= 10000:
            return int(m.group(1)), s
        m = re.search(r"\b(27|30|35|40|50|57|65)K\b", s, re.I)
        if m:
            return int(m.group(1)) * 100, s
    return None, None


def analiz_yaz(yol):
    v = ies_oku(yol)
    A = v["anahtarlar"]
    beam, tepe = isin_acisi(v["dikey_acilar"], v["kandela"], v["n_dikey"], v["n_yatay"])
    kelvin, k_kaynak = kelvin_tahmini(v)

    mutlak = v["lumen_lamba"] < 0
    toplam_lm = None if mutlak else v["lamba"] * v["lumen_lamba"] * (v["carpan"] or 1)
    verim = (toplam_lm / v["watt"]) if (toplam_lm and v["watt"] > 0) else None

    maks_dik = max(v["dikey_acilar"]) if v["dikey_acilar"] else 0
    min_dik  = min(v["dikey_acilar"]) if v["dikey_acilar"] else 0
    if maks_dik <= 90:
        dagilim = "AŞAĞI yayan (downlight/spot tipi)"
    elif min_dik >= 90:
        dagilim = "YUKARI yayan (uplight)"
    else:
        dagilim = "hem aşağı hem yukarı yayan"

    simetri = {1: "tam simetrik (0° tek düzlem)", }.get(
        v["n_yatay"], "%d yatay düzlem (asimetrik olabilir)" % v["n_yatay"])
    if v["n_yatay"] == 1:
        simetri = "dönel simetrik"

    print("=" * 62)
    print("IES KÜNYE: %s" % v["dosya"])
    print("=" * 62)
    for etiket, anahtar in (("Üretici", "MANUFAC"), ("Ürün kodu", "LUMCAT"),
                            ("Armatür", "LUMINAIRE"), ("Lamba", "LAMP"),
                            ("Lamba kodu", "LAMPCAT"), ("Test", "TEST")):
        if A.get(anahtar):
            print("  %-12s: %s" % (etiket, A[anahtar]))
    print("-" * 62)
    if mutlak:
        print("  Lümen       : MUTLAK fotometri (LED tipik, lümen kandela tablosunda)")
    else:
        print("  Lümen       : %.0f lm  (%d lamba × %.0f lm)"
              % (toplam_lm, v["lamba"], v["lumen_lamba"]))
    print("  Güç         : %.1f W" % v["watt"] if v["watt"] > 0 else "  Güç         : IES'te yazmıyor")
    if verim:
        print("  Verim       : %.0f lm/W" % verim)
    if kelvin:
        print("  Renk sıc.   : ~%d K  (kaynak: '%s')" % (kelvin, (k_kaynak or "")[:40]))
    else:
        print("  Renk sıc.   : IES'te bilgi yok, üretici föyünden teyit et")
    print("  Tepe şiddet : %.0f cd" % (tepe or 0))
    if beam:
        print("  Işın açısı  : ~%.0f°  (%s)" % (beam,
              "dar spot" if beam < 20 else "orta" if beam < 45 else "geniş/flood"))
    print("  Dağılım     : %s , %s" % (dagilim, simetri))
    print("  Boyut       : %.2f × %.2f × %.2f %s" % (
        v["boyut"][0], v["boyut"][1], v["boyut"][2],
        "m" if v["birim"] == 2 else "FEET (!)"))
    print("-" * 62)
    uyarilar = []
    if v["birim"] != 2:
        uyarilar.append("Birim FEET, ies2rad halleder ama sahnenin METRE olduğundan emin ol.")
    if v["non_ascii"]:
        uyarilar.append("%d adet non-ASCII bayt var (örn ®), armatür düzenlemesini "
                        "BINARY modda yap, yoksa 0 ışık verir." % v["non_ascii"])
    if not mutlak and toplam_lm and toplam_lm < 1500:
        uyarilar.append("Düşük lümen (%d lm), yüksek cephede tek başına zayıf kalır, "
                        "çok armatür veya ies2rad -m ile boost düşün." % int(toplam_lm))
    if v["fot_tip"] != 1:
        uyarilar.append("Fotometri tipi C değil (tip %d), yön kontrolünü render'da teyit et." % v["fot_tip"])
    if uyarilar:
        print("  UYARILAR:")
        for u in uyarilar:
            print("   ⚠ " + u)
    else:
        print("  Uyarı yok, dosya temiz görünüyor.")
    print("  Radiance'a çevir:  ies2rad -m 1 -o <ad> \"%s\"" % v["dosya"])
    print("=" * 62)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    for yol in sys.argv[1:]:
        try:
            analiz_yaz(yol)
        except Exception as e:
            print("HATA (%s): %s" % (yol, e))
