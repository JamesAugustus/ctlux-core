# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Armatürün fotometri dosyasını proje sınırı içinde bulur."""
import os
from core.file_safety import ic_yol


def ies_kaynagi(proje_dir, armatur, kutuphane=None):
    """kutuphane verilirse <kutuphane>/armaturler de aranır, default klasör yok."""
    aday = []
    if armatur.get("ies"):
        ad = armatur["ies"]
        kokler = [proje_dir, os.path.join(proje_dir, "isik")]
        if kutuphane:
            kokler.append(os.path.join(kutuphane, "armaturler"))
        for kok in kokler:
            try:
                aday.append(ic_yol(kok, ad))
            except ValueError:
                continue
    if armatur.get("urun_rad"):
        aday.append(ic_yol(proje_dir, os.path.splitext(armatur["urun_rad"])[0] + ".ies"))
    return next((p for p in aday if os.path.isfile(p)), None)


# Yazılan her IES dosyasına konan sahiplik notu. LM-63-2002'de alt çizgiyle başlayan anahtar sözcük
# kullanıcıya aittir. Metin yalnız ASCII: IES okuyucuları ASCII dışı baytta bozuluyor. Satır <= 80.
NOT_BASI = "[_NOTICE] Photometric data belongs to its owner, normally the luminaire"
NOT_PROJE = (NOT_BASI, "[MORE] manufacturer. Written from a lighting project file to rebuild",
             "[MORE] that project.")
NOT_DOSYA = (NOT_BASI, "[MORE] manufacturer. Copied from a file supplied by the user.")


def notlu_ies(veri, satirlar=NOT_DOSYA):
    """IES baytlarında TILT= satırından hemen önce sahiplik notunu ekler.

    Kaynağın anahtar sözcükleri ve baytları olduğu gibi kalır, satır sonu kaynağınkine uyar.
    Başlıkta not zaten varsa ya da TILT satırı yoksa veri değişmeden döner.
    """
    metin = veri.decode("latin-1")
    parca = metin.splitlines(keepends=True)
    for i, satir in enumerate(parca):
        if satir.strip().startswith(NOT_BASI):
            return veri
        if satir.strip().upper().startswith("TILT="):
            ornek = parca[i - 1] if i else satir
            son = ornek[len(ornek.rstrip("\r\n")):] or "\n"
            eklenen = "".join(s + son for s in satirlar)
            return "".join(parca[:i] + [eklenen] + parca[i:]).encode("latin-1")
    return veri
