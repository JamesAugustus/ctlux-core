# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""
engine.py: Radiance pipeline'ının ortak çekirdeği. Çeviri komutu, dedektif,
importer ve tools/* buradan beslenir. Python 3.9 stdlib, kurulum yok.
"""

import json, os, math, re, subprocess, shutil, struct, zlib, hashlib
import functools, shlex, uuid, tempfile
from core import render_cache as render_onbellek
from core import render_parallel as render_paralel
from core import render_progress as render_ilerleme
from core import processes as surecler
from core import paths as ctlux_yollar
from core.file_safety import atomik_json, ic_yol
from core.photometry import ies_kaynagi

WINDOWS = (os.name == "nt")

AYAR_DOSYASI = str(ctlux_yollar.AYAR_DOSYASI)


def _ayar():
    try:
        with open(AYAR_DOSYASI, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def radiance_bul():
    """Radiance bin klasörünü bul: RADIANCE_BIN ya da ayar dosyası > PATH > bilinen konumlar."""
    a = os.environ.get("RADIANCE_BIN") or _ayar().get("radiance_bin")
    if a and os.path.isdir(a):
        return a
    r = shutil.which("rpict")
    if r:
        return os.path.dirname(r)
    adaylar = [os.path.expanduser("~/radiance/bin"),
               "/usr/local/radiance/bin", "/opt/radiance/bin",
               "/Applications/Radiance/bin",
               r"C:\Radiance\bin", r"C:\Program Files\Radiance\bin",
               r"C:\Program Files (x86)\Radiance\bin"]
    for k in adaylar:
        if os.path.isfile(os.path.join(k, "rpict")) or \
           os.path.isfile(os.path.join(k, "rpict.exe")):
            return k
    return None


RADIANCE_BIN = radiance_bul() or os.path.expanduser("~/radiance/bin")
RAYPATH = _ayar().get("raypath") or (
    ".;" + os.path.join(os.path.dirname(RADIANCE_BIN), "lib") if WINDOWS
    else ".:" + os.path.join(os.path.dirname(RADIANCE_BIN), "lib"))


# onizleme sadece direct light hesaplar, taslak/final indirect sampling'i artırarak ekler.
# parametre adları tek başına fotometrik doğruluğu ya da standarda uygunluğu kanıtlamaz.
KALITE = {
    # direct light ve gölge kontrolü için, indirect aydınlatma ve analizde kullanılmaz.
    "onizleme": "-ab 0 -ds 0 -dt 0.25 -dc 0.25 -dr 0 -ps 8 -pt 0.15 -pj 0 -lw 0.01",
    "taslak": "-ab 2 -ad 512 -aa 0.2 -ps 3",
    "orta":   "-ab 3 -ad 1024 -aa 0.15 -as 256 -ps 2 -pj 0.9",
    "final":  "-ab 5 -ad 2048 -aa 0.1 -as 1024 -ar 128 -ps 1 -pj 0.9 -lw 1e-4",
    # rapor için daha yoğun sampling, kaynak fotometrisi, sahne doğrulaması ve
    # yakınsama testi ayrıca lazım. Bu ayar bir uygunluk sertifikası değil.
    "rapor":  "-ab 6 -ad 4096 -as 1024 -ar 256 -aa 0.1 -ds 0.02 -dt 0.05 -dc 0.75 -dr 3 -lr 8 -lw 1e-5 -ps 1 -pj 0.9",
}


def ortam_hazirla():
    """PATH'i tamamlar, macOS'ta Finder'dan açılınca PATH boş gelebilir. RAYPATH sadece child'da."""
    # seçilen dağıtım PATH'te daha önce olsa bile en başa alınır.
    # yardımcı dizinler seçilen Radiance komutlarının önüne geçmez.
    mevcut = os.environ.get("PATH", "").split(os.pathsep)
    adaylar = [RADIANCE_BIN, *mevcut, "/usr/local/bin", "/opt/homebrew/bin",
               os.path.expanduser("~/.local/bin")]
    sonuc = []
    for p in adaylar:
        if p and p not in sonuc and os.path.isdir(p):
            sonuc.append(p)
    os.environ["PATH"] = os.pathsep.join(sonuc)


def radiance_ortami(env=None):
    """Radiance search path'lerini tamamla, çağıranın env'ine dokunmadan."""
    env = dict(os.environ if env is None else env)
    env['PATH'] = RADIANCE_BIN + os.pathsep + env.get('PATH', '')
    yollar = ['.']
    for yol in env.get('RAYPATH', RAYPATH).split(os.pathsep):
        if yol and yol not in yollar:
            yollar.append(yol)
    lib = os.path.join(os.path.dirname(os.path.realpath(RADIANCE_BIN)), 'lib')
    if os.path.isdir(lib) and lib not in yollar:
        yollar.append(lib)
    env['RAYPATH'] = os.pathsep.join(yollar)
    return env


def sh(cmd, cwd=None, ek_ortam=None):
    """Shell'de çalıştır -> (kod, stdout, stderr)."""
    env = radiance_ortami()
    env.update(ek_ortam or {})
    p = surecler.calistir(cmd, shell=True, cwd=cwd, timeout=3600, env=env)
    return p.returncode, p.stdout, p.stderr


def _sirali_render(fn):
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        with surecler.render_sirasi():
            return fn(*args, **kwargs)
    return wrapped


# ---------------------------------------------------------------- proje ----

def proje_kaydet(proje_dir, veri):
    atomik_json(os.path.join(proje_dir, "proje.json"), veri)


# ---------------------------------------------------------------- sahne ----

def _rad_komut_yolu(yol):
    """Radiance !komut satırındaki path için platformun quote kuralı."""
    return subprocess.list2cmdline([str(yol)]) if WINDOWS else shlex.quote(str(yol))


def _geo_satiri(proje_dir, g, tam_geo=False, mat_rel="malzeme/materials.rad"):
    """geometri girdisi -> _build.rad satırı. mesh UV'yi korur, rad korumaz.
    tam_geo=True: yanında X.tam.obj varsa tam çözünürlüklü olanı kullan (final/rapor render'ı),
    False'ta projenin seçtiği dosya kullanılır. Burada geometri sadeleştirilmez."""
    dosya = os.path.join(proje_dir, g["dosya"])
    if tam_geo:
        tam = dosya + ".tam.obj"
        if os.path.exists(tam):
            dosya = tam
    olcek = g.get("olcek", 1) or 1
    rx    = g.get("rx", 0) or 0
    if g.get("tip") == "mesh":
        arglar = '"%s"' % dosya
        n = 1
        if olcek != 1: arglar += " -s %g" % olcek; n += 2
        if rx:         arglar += " -rx %g" % rx;   n += 2
        ad = os.path.splitext(os.path.basename(g["dosya"]))[0]
        return "void mesh %s\n%d %s\n0\n0\n" % (ad, n, arglar)
    xf = ""
    if olcek != 1: xf += "-s %g " % olcek
    if rx:         xf += "-rx %g " % rx
    if dosya.lower().endswith(".obj"):
        # texture'lı OBJ (UV + map_Kd) -> obj2mesh .rtm (UV korunur, colorpict kaplar).
        # mesh primitive'inde path quote'suz okunur, _cache göreli path'i boşluksuz, güvenli.
        if os.path.getsize(dosya) >= 1_000_000 or g.get("mesh_render") or _obj_dokulu(proje_dir, dosya):
            rtm = _obj_rtm(proje_dir, dosya, mat_rel)
            if rtm:
                ad = "m_" + hashlib.md5(dosya.encode("utf-8")).hexdigest()[:8]
                arglar = rtm
                n = 1
                if olcek != 1: arglar += " -s %g" % olcek; n += 2
                if rx:         arglar += " -rx %g" % rx;   n += 2
                tip = "instance" if rtm.lower().endswith('.oct') else "mesh"
                return "void %s %s\n%d %s\n0\n0\n" % (tip, ad, n, arglar)
        # xform OBJ okuyamaz (Radiance .rad bekler), render anında obj2rad ile .rad'a çevir.
        # temp _cache'e yazılır (mtime cache'li), kullanıcının OBJ'si değişmez, kalıcı .rad üretilmez.
        dosya = _obj_rad(proje_dir, dosya)
    return '!xform %s%s\n' % (xf, _rad_komut_yolu(dosya))


def _obj_rad(proje_dir, obj_yol):
    """Küçük OBJ'de de sadece tam ve content hash'i doğrulanmış RAD kullan."""
    cache = os.path.join(proje_dir, "_cache")
    os.makedirs(cache, exist_ok=True)
    arac = shutil.which("obj2rad")
    kaynak_ozeti = render_onbellek.dosya_ozeti(obj_yol)
    anahtar = render_onbellek.nesne_ozeti(["obj2rad-v2", os.path.abspath(obj_yol),
        kaynak_ozeti, render_onbellek.dosya_ozeti(arac) if arac else None])
    rad = os.path.join(cache, "geo_%s.rad" % anahtar)
    if render_onbellek.gecerli_cikti(rad, rad + '.json'):
        return rad
    gecici = rad + '.' + uuid.uuid4().hex + '.tmp'
    try:
        r = surecler.calistir(['obj2rad', os.path.abspath(obj_yol)], cwd=proje_dir, env=radiance_ortami(),
                             stdout_yolu=gecici, timeout=300)
        if r.returncode or os.path.getsize(gecici) < 50:
            raise RuntimeError("obj2rad başarısız (%s): %s" %
                               (os.path.basename(obj_yol), (r.stderr or "Boş çıktı")[:200]))
        if render_onbellek.dosya_ozeti(obj_yol) != kaynak_ozeti:
            raise RuntimeError("OBJ dönüştürülürken kaynak değişti, yeniden deneyin")
        os.replace(gecici, rad)
        atomik_json(rad + '.json', {'sha256': render_onbellek.dosya_ozeti(rad)})
    finally:
        if os.path.exists(gecici):
            os.remove(gecici)
    return rad


def _obj_dokulu(proje_dir, obj_yol):
    """Bu OBJ texture'lı render ister mi? UV (vt) varsa ve mtl'sinde map_Kd varsa evet.
    (Texture'sız OBJ obj2rad yolunda kalır, davranış değişmez.)"""
    uv = False
    mtl_yollari = []
    obj_dir = os.path.dirname(obj_yol)
    try:
        with open(obj_yol, "rb") as kaynak:
            for ln in kaynak:
                if ln[:3] == b"vt ":
                    uv = True
                elif ln[:7] == b"mtllib ":
                    ham = ln[7:].strip().decode("latin-1", "replace")
                    adaylar = [ham] if os.path.exists(os.path.join(obj_dir, ham)) else ham.split()
                    mtl_yollari += [os.path.join(obj_dir, a) for a in adaylar]
    except OSError:
        return False
    if not uv:
        return False
    mtl = _mtl_oku(mtl_yollari)
    return any(p.get("harita") and _doku_bul(proje_dir, p["harita"], p.get("_mtl_yolu")) for p in mtl.values())


def _obj_rtm(proje_dir, obj_yol, mat_rel):
    """OBJ'yi değişmez, content hash'li bir Radiance mesh olarak bir kere hazırla."""
    from core.render_mesh import derle
    try:
        return derle(proje_dir, obj_yol, os.path.join(proje_dir, mat_rel))
    except (OSError, RuntimeError) as e:
        surecler.iptal_kontrol()
        raise RuntimeError("Radiance geometrisi hazırlanamadı, ağır polygon yolu başlatılmadı. " + str(e)[-1500:]) from e


def _mtl_oku(mtl_yollari):
    """.mtl dosyalarını okur -> {malzeme_adi: {Kd,Ks,Ns,d,harita}}. latin-1 (obj2rad ile
    aynı ad encoding'i). Bilinmeyen satırlar atlanır, renk yoksa alan yazılmaz."""
    tablo = {}
    for yol in mtl_yollari:
        if not os.path.exists(yol):
            continue
        aktif = None
        try:
            with open(yol, "rb") as kaynak:
                for ham in kaynak:
                    ln = ham.decode("latin-1", "replace").rstrip("\r\n")
                    s = ln.strip()
                    if not s or s[0] == "#":
                        continue
                    p = s.split()
                    a = p[0].lower()
                    if a == "newmtl":
                        aktif = s.split(None, 1)[1].strip() if len(p) > 1 else None
                        if aktif:
                            tablo[aktif] = {"_mtl_yolu": yol}
                    elif aktif is None:
                        continue
                    elif a in ("kd", "ks") and len(p) >= 4:
                        try:
                            tablo[aktif][a] = [float(p[1]), float(p[2]), float(p[3])]
                        except ValueError:
                            pass
                    elif a == "ns" and len(p) >= 2:
                        try: tablo[aktif]["ns"] = float(p[1])
                        except ValueError: pass
                    elif a in ("d", "tr") and len(p) >= 2:
                        try:
                            v = float(p[1])
                            tablo[aktif]["d"] = (1.0 - v) if a == "tr" else v
                        except ValueError:
                            pass
                    elif a == "map_kd":
                        # path absolute ya da Windows path'i olabilir (C:\...\mat0_c.jpg), sadece dosya adı alınır,
                        # gerçek dosya projenin doku/ ya da model/ klasöründe aranır (_doku_bul)
                        ham_yol = s.split(None, 1)[1].strip() if len(s.split(None, 1)) > 1 else ""
                        if ham_yol:
                            y = ham_yol.replace("\\", "/")
                            tablo[aktif]["harita"] = y.rsplit("/", 1)[-1] if (":" in y or y.startswith("/")) else y
        except OSError:
            pass
    return tablo


def _doku_bul(proje_dir, ad, mtl_yolu=None):
    """MTL'ye göre path'i ve ayrılmış doku/model klasörlerini proje sınırı içinde çöz."""
    adaylar = []
    if mtl_yolu:
        rel = os.path.relpath(os.path.dirname(mtl_yolu), proje_dir)
        adaylar.append(os.path.join(rel, ad))
        if rel == "model" or rel.startswith("model" + os.sep):
            adaylar.append(os.path.join("doku", os.path.relpath(rel, "model"), ad))
    adaylar += [os.path.join(alt, ad) for alt in ("doku", "model", ".")]
    for aday in adaylar:
        try:
            p = ic_yol(proje_dir, os.path.normpath(aday))
        except ValueError:
            continue
        if os.path.isfile(p):
            return p
    return None


def _doku_pic(proje_dir, ad, mtl_yolu=None):
    """Texture görselini (jpg/png/tiff) Radiance .pic'e çevirir, _cache/doku/ altında, mtime cache'li.
    Proje göreli path döner (render cwd=proje_dir, boşluklu absolute path Radiance sahnesinde
    çalışmaz). Bulunamaz ya da çevrilemezse None: malzeme düz renge düşer, render patlamaz."""
    kaynak = _doku_bul(proje_dir, ad, mtl_yolu)
    if not kaynak:
        return None
    cache = os.path.join(proje_dir, "_cache", "doku")
    os.makedirs(cache, exist_ok=True)
    govde = hashlib.sha256(os.path.relpath(kaynak, proje_dir).encode()).hexdigest()[:20]   # sahne path'inde boşluk olmasın
    pic = os.path.join(cache, govde + ".pic")
    try:
        if os.path.exists(pic) and os.path.getmtime(pic) >= os.path.getmtime(kaynak):
            return os.path.relpath(pic, proje_dir)
    except OSError:
        pass
    if kaynak.lower().endswith((".hdr", ".pic")):               # zaten Radiance formatı
        shutil.copy(kaynak, pic)
        return os.path.relpath(pic, proje_dir)
    ara_tif = os.path.join(cache, govde + "_tmp.tiff")          # jpg/png -> Pillow -> TIFF -> ra_tiff
    try:
        from PIL import Image
        with Image.open(kaynak) as im:
            im.convert("RGB").save(ara_tif, format="TIFF")
        r = surecler.calistir(["ra_tiff", "-r", ara_tif, pic], cwd=proje_dir, env=radiance_ortami())
        kod, se = r.returncode, r.stderr
    except (ImportError, OSError, RuntimeError):
        return None
    finally:
        try:
            os.remove(ara_tif)
        except OSError:
            pass
    if kod != 0 or not os.path.exists(pic) or os.path.getsize(pic) < 100:
        return None
    return os.path.relpath(pic, proje_dir)


def _mtl_rad(ad, p, pic_rel=None):
    """Tek .mtl malzemesini Radiance bloğuna çevirir (deterministik eşleme).
    Kd->diffuse RGB, Ks->specular (0..0.1 arası), Ns->roughness, d<0.95->saydam (trans).
    pic_rel verilirse (map_Kd texture'ı .pic'e çevrilmiş) plastic colorpict ile kaplanır,
    UV'ler mesh (obj2mesh) geometrisinden gelir (Lu/Lv)."""
    kis = lambda v: max(0.0, min(1.0, v))
    kd = p.get("kd", [0.55, 0.55, 0.55])
    r, g, b = kis(kd[0]), kis(kd[1]), kis(kd[2])
    ks = p.get("ks", [0.0, 0.0, 0.0])
    spek = min(0.1, max(0.0, max(ks)))                 # plastic specular'ı küçük tut
    ns = p.get("ns", 32.0)
    puruz = round(kis(0.18 * (1.0 - min(ns, 100.0) / 100.0)), 3)  # Ns yüksek = parlak = düşük roughness
    d = p.get("d", 1.0)
    if d < 0.95:                                        # saydam -> trans (siyah cam çıkmasın)
        gecir = round(kis((1.0 - d) + 0.2), 3)
        return ("void trans %s\n0\n0\n7 %.4g %.4g %.4g %.3f %.3f %.3f 0\n"
                % (ad, r, g, b, max(spek, 0.03), max(puruz, 0.02), gecir))
    if pic_rel:                                         # texture'lı: resmin rengi çarpan olur
        return ("void colorpict %s_dk\n7 red green blue %s . frac(Lu) frac(Lv)\n0\n0\n"
                "%s_dk plastic %s\n0\n0\n5 %.4g %.4g %.4g %.3f %.3f\n"
                % (ad, pic_rel, ad, ad, max(r, 0.9), max(g, 0.9), max(b, 0.9), spek, puruz))
    return "void plastic %s\n0\n0\n5 %.4g %.4g %.4g %.3f %.3f\n" % (ad, r, g, b, spek, puruz)


def _malzeme_tamamla(proje_dir, proje):
    """Import edilen modellerin usemtl adlarının materials.rad'da tanımlı olmasını garantiler.
    Tanımsız malzeme render'da 'undefined modifier' hatası ya da boş geometri demek.
    OBJ'nin yanındaki .mtl (mtllib) varsa gerçek renk/parlaklık oradan çevrilir, yoksa
    nötr gri. Böylece her model ilk seferde (mümkünse kendi renkleriyle) render olur."""
    import re
    mat_yol = os.path.join(proje_dir, proje.get("malzeme", "malzeme/materials.rad"))
    try:
        with open(mat_yol, encoding="latin-1") as f:
            mevcut = f.read()
    except OSError:
        return
    tanimli = set(re.findall(r'^\s*\w+\s+\w+\s+([\w.\-]+)', mevcut, re.M))
    gereken = set()
    mtl_yollari = []
    for g in proje.get("geometri", []):
        gp = os.path.join(proje_dir, g.get("dosya", ""))
        if not gp.lower().endswith(".obj") or not os.path.exists(gp):
            continue
        obj_dir = os.path.dirname(gp)
        aktif_malzeme = None
        aktif_grup = "white"  # obj2rad: usemtl yoksa ana grup, o da yoksa white
        try:
            with open(gp, "rb") as kaynak:
                for ln in kaynak:
                    if ln[:7] == b"usemtl ":
                        aktif_malzeme = ln.split(None, 1)[1].strip().decode("latin-1", "replace")
                    elif ln[:2] == b"g " or ln.strip() == b"g":
                        grup = ln.split()
                        aktif_grup = grup[1].decode("latin-1", "replace") if len(grup)>1 else "white"
                    elif ln[:2] == b"f ":
                        gereken.add(aktif_malzeme or aktif_grup)
                    elif ln[:7] == b"mtllib ":
                        ham = ln[7:].strip().decode("latin-1", "replace")
                        adaylar = [ham] if os.path.exists(os.path.join(obj_dir, ham)) else ham.split()
                        for mm in adaylar:
                            mp = os.path.join(obj_dir, mm)
                            if os.path.exists(mp) and mp not in mtl_yollari:
                                mtl_yollari.append(mp)
        except OSError:
            pass
    eksik = sorted(m for m in gereken if m and m not in tanimli)
    if not eksik:
        return
    mtl = _mtl_oku(mtl_yollari)
    with open(mat_yol, "a", encoding="latin-1") as f:
        f.write("\n# ice aktarilan modelin malzemeleri (.mtl'den cevrildi, renksizler notr "
                "gri), gercegini Malzeme editorunde inceltebilirsin\n")
        for m in eksik:
            # m latin-1'den geldi, latin-1 dosyaya birebir yazılır (obj2rad'daki adla eşleşir)
            if m in mtl:
                pic = _doku_pic(proje_dir, mtl[m]["harita"], mtl[m].get("_mtl_yolu")) if mtl[m].get("harita") else None
                f.write(_mtl_rad(m, mtl[m], pic_rel=pic))
            else:
                f.write("void plastic %s\n0\n0\n5 0.55 0.55 0.55 0 0\n" % m)


def _render_arac_imzasi():
    """Araç versiyonu ya da Radiance .cal kütüphanesi değişirse eski hesabı kullanma."""
    dosyalar = []
    for ad in ("oconv", "obj2mesh", "obj2rad", "xform", "rpict", "gensky", "ies2rad"):
        yol = shutil.which(ad)
        if yol:
            dosyalar.append((yol, render_onbellek.dosya_ozeti(yol)))
    for dizin in radiance_ortami()['RAYPATH'].split(os.pathsep):
        if not dizin or dizin == "." or not os.path.isdir(dizin):
            continue
        for kok, _, adlar in os.walk(dizin, followlinks=False):
            for ad in sorted(adlar):
                yol = os.path.join(kok, ad)
                if os.path.isfile(yol):
                    dosyalar.append((yol, render_onbellek.dosya_ozeti(yol)))
    return dosyalar


@_sirali_render
def sahne_derle(proje_dir, proje, gok_metni, gece_isiklari=False, etiket="sahne",
                armatur=True, tam_geo=False):
    """Geometriyi mesh, sahneyi content-addressed frozen octree olarak cache'le.

    Dönen path değişmez: yeni light/material/geometri ayrı bir dosya üretir.
    Kamera ve çözünürlük build key'ine girmez. Bütün aşamalar bitene kadar
    eski başarılı çıktı korunur, yarım octree tekrar kullanılmaz.
    """
    from pathlib import Path
    cache = os.path.join(proje_dir, "_cache")
    os.makedirs(cache, exist_ok=True)
    render_ilerleme.adim('derleme_kilidi', 'Sahne hazırlama sırası bekleniyor')
    with render_onbellek.derleme_kilidi:
        surecler.iptal_kontrol()
        render_ilerleme.adim('isik', 'Armatür ve fotometri dosyaları hazırlanıyor')
        # fotometri yan dosyaları ilk çağrıda da imzaya girsin.
        isik_dosyalari = [("armatur", armatur_uret(proje_dir, proje) if armatur else None),
                         ("led", led_serit_uret(proje_dir, proje) if armatur else None)]
        render_ilerleme.adim('kaynak', 'Sahne girdilerinin içerik kimlikleri doğrulanıyor')
        kaynak = render_onbellek.kaynaklar(proje_dir, proje)
        # kamera her değiştiğinde milyonlarca yüzü sırf malzeme adı için tarama.
        mk = render_onbellek.nesne_ozeti(["materials-v1", kaynak, proje.get("geometri", [])])
        hazir = os.path.join(cache, "mat_kontrol_" + mk + ".json")
        if not os.path.isfile(hazir):
            render_ilerleme.adim('malzeme', 'Geometri malzeme eşleşmeleri hazırlanıyor')
            _malzeme_tamamla(proje_dir, proje)
            kaynak = render_onbellek.kaynaklar(proje_dir, proje)
            mk = render_onbellek.nesne_ozeti(["materials-v1", kaynak, proje.get("geometri", [])])
            atomik_json(os.path.join(cache, "mat_kontrol_" + mk + ".json"), {"hazir": True})
        sahne = {"geometri": proje.get("geometri", []),
                 "malzeme": proje.get("malzeme", "malzeme/materials.rad"),
                 "armaturler": proje.get("armaturler", []) if armatur else [],
                 "led_seritler": proje.get("led_seritler", []) if armatur else [],
                 "isiklar": proje.get("isiklar", []) if gece_isiklari else []}
        key = render_onbellek.nesne_ozeti({"surum": "scene-v4", "kaynak": kaynak,
              "sahne": sahne, "gok": gok_metni, "armatur": armatur,
              "gece_isiklari": gece_isiklari, "tam_geo": tam_geo,
              "araclar": _render_arac_imzasi(),
              "dinamik": uuid.uuid4().hex if render_onbellek.dinamik_rad_var(kaynak) else None})
        oct_p = os.path.join(cache, "sahne_" + key + ".oct")
        kayit = oct_p + ".json"
        render_ilerleme.adim('sahne_onbellek', 'Hazır Radiance sahnesi doğrulanıyor')
        if render_onbellek.gecerli_sahne(proje_dir, oct_p, kayit):
            render_ilerleme.adim('sahne_hazir', 'Doğrulanmış Radiance sahnesi önbellekten alındı',
                                onbellek={'sahne': True})
            return oct_p
        render_ilerleme.adim('geometri', 'Sahne geometrisi hazırlanıyor', onbellek={'sahne': False})
        gok_p = os.path.join(cache, "gok_" + key + ".rad")
        build_p = os.path.join(cache, "build_" + key + ".rad")
        Path(gok_p).write_text(gok_metni)
        satirlar = []
        for sira, g in enumerate(sahne["geometri"]):
            surecler.iptal_kontrol()
            render_ilerleme.adim('geometri', 'Sahne geometrisi hazırlanıyor',
                                tamamlanan=sira, toplam=len(sahne['geometri']), birim='dosya',
                                ayrinti=g.get('dosya'))
            satirlar.append(_geo_satiri(proje_dir, g, tam_geo, sahne["malzeme"]))
        render_ilerleme.adim('geometri_dogrula', 'Hazır geometri dosyaları doğrulanıyor',
                            tamamlanan=len(satirlar), toplam=len(sahne['geometri']), birim='dosya')
        # frozen ana sahne RTM/instance dosyasını içine gömmez. Ana OCT sağlam
        # olsa bile silinmiş ya da bozulmuş türetilmiş geometri cache hit sayılmamalı.
        turetilmis_yollar = set(re.findall(r'(?m)^\d+ (_cache/mesh_[0-9a-f]+\.(?:rtm|oct))(?:\s|$)',
                                           '\n'.join(satirlar)))
        from core.render_mesh import parca_bagimliliklari
        for rel in list(turetilmis_yollar):
            turetilmis_yollar.update(parca_bagimliliklari(proje_dir, rel))
        turetilmis = [{"yol": rel, "sha256": render_onbellek.dosya_ozeti(ic_yol(proje_dir, rel))}
                      for rel in sorted(turetilmis_yollar)]
        # light üreticilerinin çalışma dosyasını bu sahneye ait ayrı bir girdiye kopyala.
        for ad, yol in isik_dosyalari:
            if yol:
                sabit = os.path.join(cache, ad + "_" + key + ".rad")
                shutil.copyfile(yol, sabit)
                satirlar.append('!xform %s\n' % _rad_komut_yolu(sabit))
        for lt in sahne["isiklar"]:
            lp = ic_yol(proje_dir, lt)
            if os.path.exists(lp):
                satirlar.append('!xform %s\n' % _rad_komut_yolu(lp))
        Path(build_p).write_text("\n".join(satirlar))
        mat = ic_yol(proje_dir, sahne["malzeme"])
        gecici = oct_p + "." + uuid.uuid4().hex + ".tmp"
        try:
            render_ilerleme.adim('oconv', 'Radiance sahnesi derleniyor (oconv)')
            r = surecler.calistir(["oconv", "-f", mat, gok_p, build_p], env=radiance_ortami(),
                                  cwd=proje_dir, stdout_yolu=gecici, timeout=3600)
            if r.returncode != 0 or os.path.getsize(gecici) < 64:
                raise RuntimeError("oconv HATA:\n" + (r.stderr or "Boş çıktı")[-1500:])
            # ara dosyalar dışında kullanıcının girdisi build sırasında değiştiyse
            # yeni cache key'iyle baştan hazırlanmalı, yanlış sahne saklanmaz.
            render_ilerleme.adim('sahne_dogrula', 'Derlenen sahne ve kaynak tutarlılığı doğrulanıyor')
            son = render_onbellek.kaynaklar(proje_dir, proje)
            once_k = dict(kaynak); son_k = dict(son)
            if son_k != once_k:
                raise RuntimeError("Sahne hazırlanırken kaynak değişti, renderı yeniden başlatın")
            os.replace(gecici, oct_p)
            atomik_json(kayit, {"sha256": render_onbellek.dosya_ozeti(oct_p), "anahtar": key,
                               "turetilmis": turetilmis})
            return oct_p
        finally:
            if os.path.exists(gecici):
                os.remove(gecici)


def _ies_urun_rad(proje_dir, ies_ad, dimmer=1.0):
    """Kaynak içeriği + dimmer key'i, eski fotometriyi tekrar kullanmaz."""
    dimmer = float(dimmer)
    if not math.isfinite(dimmer) or dimmer < 0:
        raise ValueError("Fotometri çarpanı sonlu ve sıfır veya pozitif olmalı")
    kaynak = ies_kaynagi(proje_dir, {"ies": ies_ad})
    if not kaynak:
        raise ValueError("IES kaynağı bulunamadı: " + str(ies_ad))
    with open(kaynak, "rb") as f:
        ham = f.read()
    key = hashlib.sha256(ham + repr(dimmer).encode()).hexdigest()[:24]
    cache = os.path.join(proje_dir, "_cache", "ies")
    os.makedirs(cache, exist_ok=True)
    rel_out = "_cache/ies/k_" + key
    hedef = os.path.join(proje_dir, rel_out + ".rad")
    if os.path.isfile(hedef) and os.path.isfile(os.path.join(proje_dir, rel_out + ".dat")):
        return rel_out + ".rad"
    with open(os.path.join(proje_dir, rel_out + ".ies"), "wb") as f:
        f.write(ham)
    r = surecler.calistir(["ies2rad", "-m", str(dimmer), "-o", rel_out,
                          rel_out + ".ies"], cwd=proje_dir, env=radiance_ortami())
    if r.returncode or not os.path.isfile(hedef):
        raise RuntimeError("IES çevrilemedi: " + r.stderr[-500:])
    return rel_out + ".rad"


def _led_dagit(serit):
    """Şeridin köşelerinden LED konumlarını eşit aralıkla çıkarır.
    Döner: [(x, y, z), ...]. 2B köşede z=0."""
    k = serit.get("kose_noktalari") or []
    if len(k) < 2:
        return []
    def uc(p):
        return (p[0], p[1], p[2] if len(p) > 2 else 0.0)
    kk = [uc(p) for p in k]
    if serit.get("kapali") and len(kk) > 2:
        kk = kk + [kk[0]]
    seg, boy, L = [], [], 0.0
    for i in range(len(kk) - 1):
        d = tuple(kk[i+1][j] - kk[i][j] for j in range(3))
        l = math.sqrt(sum(c*c for c in d))
        seg.append(d); boy.append(l); L += l
    if L < 1e-6:
        return []
    if serit.get("aralik_modu") == "adim":
        d = max(0.001, float(serit.get("aralik_m") or 0.0166))
        N = max(2, int(L / d) + 1)
    else:
        N = max(2, int(serit.get("pixel_sayisi") or 60))
    adim = L / N if serit.get("kapali") else L / (N - 1)
    out, si, akum = [], 0, 0.0
    for p in range(N):
        t = p * adim
        while si < len(seg) - 1 and t > akum + boy[si]:
            akum += boy[si]; si += 1
        u = min(1.0, (t - akum) / boy[si]) if boy[si] > 1e-6 else 0.0
        out.append(tuple(kk[si][j] + seg[si][j] * u for j in range(3)))
    return out


def led_serit_uret(proje_dir, proje):
    """proje['led_seritler'] listesindeki LED'leri Radiance emissive .rad'ına çevirir.
    Her LED küçük bir glow/light kaynağı, renk pixel renginden, yoksa default sıcak beyaz.
    armatur_uret'in kardeşi, _cache/_led_seritleri.rad döner. Liste boşsa None."""
    seritler = proje.get("led_seritler") or []
    if not seritler:
        return None
    cache = os.path.join(proje_dir, "_cache")
    os.makedirs(cache, exist_ok=True)
    yol = os.path.join(cache, "_led_seritleri.rad")
    yazildi = 0
    with open(yol, "w") as f:
        f.write("# LED editöründen üretildi (led_serit_uret): pixel emissive kaynaklar\n")
        for si, s in enumerate(seritler):
            leds = _led_dagit(s)
            if not leds:
                continue
            foto = s.get("foto") or {}
            lm = float(foto.get("lumen_per_pixel") or 1.1)        # tam parlaklıkta 1 pixel'in lümeni
            alan = float(foto.get("led_alan_m2") or 5e-5)         # tek LED'in emissive alanı
            yari = max(0.004, math.sqrt(alan) / 2)                # küçük sphere yarıçapı
            renkler = ((s.get("renk_kaynagi") or {}).get("renkler")) or []
            # default renk: sıcak beyaz [1,0.85,0.6]
            vg = [1.0, 0.85, 0.6]
            for i, (x, y, z) in enumerate(leds):
                if i < len(renkler) and renkler[i]:
                    r, g, b = [max(0, min(255, c)) / 255.0 for c in renkler[i][:3]]
                else:
                    r, g, b = vg
                # algısal parlaklık -> pixel lümeni -> Lambert radiance (lm/(pi*alan)/179)
                Y = 0.265 * r + 0.670 * g + 0.065 * b
                rad = (lm * max(Y, 1e-4)) / (math.pi * alan) / 179.0
                mx = max(r, g, b, 1e-4)
                rr, gg, bb = rad * r / mx, rad * g / mx, rad * b / mx
                ad = "led_%d_%d" % (si, i)
                # light: direct kaynak, gerçek lüks için doğru olan bu (yüzeyi gerçekten aydınlatır
                # ve kamerada görünür). Yüzlerce pixel'de yavaşlayabilir.
                f.write("void light %s_m\n0\n0\n3 %g %g %g\n" % (ad, rr, gg, bb))
                f.write("%s_m sphere %s\n0\n0\n4 %g %g %g %g\n\n" % (ad, ad, x, y, z, yari))
                yazildi += 1
    return yol if yazildi else None


def fotometri_donusu(yon, c0=None):
    """ies2rad armatürü (nadir -Z, C0 düzlemi +X) için derece cinsinden xform -rx, -ry, -rz açıları.

    Nadir yon'a, C0 düzlemi c0'a döner. c0 yoksa dünya +X ekseninin yon'a dik izdüşümü,
    ışık X boyunca yataysa dünya +Y kullanılır. Aşağı bakan ışıkta C0, ies2rad'daki gibi +X'te kalır.
    """
    uzunluk = math.sqrt(sum(v*v for v in yon))
    if not math.isfinite(uzunluk) or uzunluk == 0:
        raise ValueError("Geçersiz ışık yönü")
    z = [-v/uzunluk for v in yon]
    adaylar = ([c0] if c0 is not None else []) + [[1, 0, 0], [0, 1, 0]]
    for eksen in adaylar:
        izdusum = sum(a*b for a, b in zip(eksen, z))
        x = [a - izdusum*b for a, b in zip(eksen, z)]
        norm = math.sqrt(sum(v*v for v in x))
        if math.isfinite(norm) and norm > 1e-6:
            break
    x = [v/norm for v in x]
    y = [z[1]*x[2] - z[2]*x[1], z[2]*x[0] - z[0]*x[2], z[0]*x[1] - z[1]*x[0]]
    # x, y, z sütunları R = Rz(c) Ry(b) Rx(a) olur, xform -rx -ry -rz sırasını bu şekilde uygular.
    b = math.asin(max(-1.0, min(1.0, -x[2])))
    if abs(x[2]) < 1 - 1e-12:
        a, c = math.atan2(y[2], z[2]), math.atan2(x[1], x[0])
    else:
        a, c = 0.0, math.atan2(-y[0], y[1])
    return tuple(math.degrees(v) + 0.0 for v in (a, b, c))


def armatur_uret(proje_dir, proje):
    """proje['armaturler'] listesinden koordinatlı light .rad'ı üretir.
    Her armatür: {ad, x, y, z, tip(light|illum|spot), renk[3], guc, yaricap,
                  yon[3](spot için)}. Boş liste -> None.
    Armatür koordinatları Radiance'a burada çevrilir."""
    arms = proje.get("armaturler") or []
    if not arms:
        return None
    cache = os.path.join(proje_dir, "_cache")
    os.makedirs(cache, exist_ok=True)
    yol = os.path.join(cache, "_armaturler.rad")
    with open(yol, "w") as f:
        f.write("# Işık Yerleşimi editöründen üretildi, koordinatlar proje.json'la senkron\n")
        for a in arms:
            if a.get("etkin") is False:
                continue
            ad = a.get("ad", "isik")
            x, y, z = a.get("x", 0), a.get("y", 0), a.get("z", 3)
            # gerçek IES fotometrisi (evo_ies): ürün .rad'ını emisyon yönüne çevirip yerleştir.
            # -Z'ye bakan ies2rad fixture'ını yon'a ve C0'a çevir, sonra konuma taşı.
            urun = a.get("urun_rad")
            dimmer = float(a.get("dimmer", 1.0))
            if not math.isfinite(dimmer) or dimmer < 0:
                raise ValueError("Geçersiz fotometri çarpanı")
            if dimmer == 0:
                continue
            if a.get("ies"):
                urun = _ies_urun_rad(proje_dir, a["ies"], dimmer)
            elif urun:
                kaynak = ies_kaynagi(proje_dir, a)
                if kaynak:
                    urun = _ies_urun_rad(proje_dir, os.path.relpath(kaynak, proje_dir), dimmer)
                elif dimmer != 1 or not os.path.isfile(ic_yol(proje_dir, urun)):
                    raise ValueError("Armatürün kaynak fotometrisi eksik: " + ad)
            if urun:
                rx, ry, rz = fotometri_donusu(a.get("yon") or [0, 0, -1], a.get("c0_yonu"))
                # xform, _armaturler.rad'ı okurken onun dizinine geçer.
                # içteki komutun path'i bu dosyaya göre, .dat path'i render cwd'sine göre.
                yerel_urun = os.path.relpath(ic_yol(proje_dir, urun), cache)
                f.write('!xform -rx %.12g -ry %.12g -rz %.12g -t %g %g %g %s\n'
                        % (rx, ry, rz, x, y, z, _rad_komut_yolu(yerel_urun)))
                continue
            r, g, b = (a.get("renk") or [1, 1, 1])
            guc = a.get("guc", 1.0)
            yari = a.get("yaricap", 0.15)
            tip = a.get("tip", "light")
            rr, gg, bb = r * guc, g * guc, b * guc
            if tip == "spot":
                dx, dy, dz = (a.get("yon") or [0, 0, -1])
                aci = a.get("aci", 60)
                f.write("void spotlight %s_m\n0\n0\n7 %g %g %g %g %g %g %g\n"
                        % (ad, rr, gg, bb, aci, dx, dy, dz))
            elif tip == "illum":
                f.write("void illum %s_m\n1 void\n0\n3 %g %g %g\n" % (ad, rr, gg, bb))
            else:                                     # light / omni / alan -> yönsüz emisyon malzemesi
                f.write("void light %s_m\n0\n0\n3 %g %g %g\n" % (ad, rr, gg, bb))
            # kaynak geometrisi: spot -> emisyon yönüne bakan disk (yandan ince görünür,
            # koni dışında koca siyah top çıkmaz, downlight açıklığı gibi). alan -> yöne dik
            # w×h polygon (panel/şerit armatür, Lumion tip 3 ile aynı mantık). light/illum/omni -> sphere.
            if tip == "spot":
                dx, dy, dz = (a.get("yon") or [0, 0, -1])
                f.write("%s_m ring %s\n0\n0\n8 %g %g %g %g %g %g 0 %g\n\n"
                        % (ad, ad, x, y, z, dx, dy, dz, yari))
            elif tip in ("alan", "area"):
                dx, dy, dz = (a.get("yon") or [0, 0, -1])
                L = math.sqrt(dx*dx + dy*dy + dz*dz) or 1.0
                dx, dy, dz = dx/L, dy/L, dz/L
                w = float(a.get("w") or 2*yari); h = float(a.get("h") or 2*yari)
                rfx, rfy, rfz = ((1, 0, 0) if abs(dz) > 0.9 else (0, 0, 1))   # referans (yon'a paralel olmayan)
                ux, uy, uz = dy*rfz - dz*rfy, dz*rfx - dx*rfz, dx*rfy - dy*rfx   # u = yon x ref
                Lu = math.sqrt(ux*ux + uy*uy + uz*uz) or 1.0; ux, uy, uz = ux/Lu, uy/Lu, uz/Lu
                vx, vy, vz = dy*uz - dz*uy, dz*ux - dx*uz, dx*uy - dy*ux         # v = yon x u
                kose = []
                for su, sv in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
                    kose += [x + ux*su*w/2 + vx*sv*h/2, y + uy*su*w/2 + vy*sv*h/2, z + uz*su*w/2 + vz*sv*h/2]
                f.write("%s_m polygon %s\n0\n0\n12 " % (ad, ad)
                        + " ".join("%g" % c for c in kose) + "\n\n")
            else:
                f.write("%s_m sphere %s\n0\n0\n4 %g %g %g %g\n\n"
                        % (ad, ad, x, y, z, yari))
    return yol


def _wire_kaynagi(gp):
    """.rtm (obj2mesh'in binary çıktısı) text olarak parse edilemez,
    geometri/bbox okumak için yanındaki asıl .obj'a yönlendir (aynı vertex'ler,
    render yine texture'lı .rtm ile yapılır)."""
    if gp.lower().endswith(".rtm"):
        alt = gp[:-4] + ".obj"
        if os.path.exists(alt):
            return alt
    return gp


def _don_nokta(p, olcek, rx):
    """Geometri girdisinin transform'u: scale + X ekseni etrafında rx derece.
    Radiance `xform -rx` ile birebir aynı. Sadece rx==90'ı işleyip diğerlerini
    identity'ye düşürürsen render ile CTScene birbirini tutmaz."""
    x, y, z = p[0] * olcek, p[1] * olcek, p[2] * olcek
    if not rx:
        return (x, y, z)
    r = math.radians(rx); c, s = math.cos(r), math.sin(r)
    return (x, y * c - z * s, y * s + z * c)


# ---------------------------------------------- deterministik converter'lar ----
# modelleri deterministik olarak, birebir Radiance'a hazırlar: kaynak -> OBJ -> (obj2mesh) -> .rtm

CEVIRICI_ASSIMP = {".fbx", ".dae", ".gltf", ".glb", ".stl", ".3ds", ".obj"}
CEVIRICI_USD = {".usd", ".usda", ".usdc", ".usdz"}


def cevirici_durum():
    """Hangi converter kurulu, bakar, native modülleri (pxr) yüklemez, sadece find_spec."""
    import importlib.util
    var = lambda ad: importlib.util.find_spec(ad) is not None
    return {"assimp": bool(shutil.which("assimp")), "usd": var("pxr")}


def armatur_bbox(proje_dir, proje):
    """Sahnenin bbox'ını döner: proje['bbox'], yoksa geometriden ölçülür, o da yoksa hedef±yarıçap."""
    # bbox: geometriden getbbox ile ölç (yoksa hedef±yarıçap)
    bb = proje.get("bbox")
    if not bb:
        birlesik = None                     # bütün geometrilerin birleşimi (oda + mobilya + obj)
        for g in (proje.get("geometri") or []):
            try:
                gp = _wire_kaynagi(ic_yol(proje_dir, g.get("dosya", "")))
                if not os.path.exists(gp):
                    continue
                olcek = g.get("olcek", 1) or 1
                rx = g.get("rx", 0) or 0
                xf = ("-s %g " % olcek if olcek != 1 else "") + ("-rx %g " % rx if rx else "")
                arac = "obj2rad" if gp.lower().endswith(".obj") else "cat"
                _, so, _ = sh('%s %s | xform %s| getbbox -h' % (arac, _rad_komut_yolu(gp), xf))
                v = [float(x) for x in so.split()]
                if len(v) >= 6:
                    v = v[:6]
                    birlesik = v if birlesik is None else [
                        min(birlesik[0], v[0]), max(birlesik[1], v[1]),
                        min(birlesik[2], v[2]), max(birlesik[3], v[3]),
                        min(birlesik[4], v[4]), max(birlesik[5], v[5])]
            except Exception:
                continue
        bb = birlesik
    if not bb:
        h = proje.get("hedef") or [0, 0, 0]
        r = proje.get("sahne_yaricap", 30)
        bb = [h[0] - r, h[0] + r, h[1] - r, h[1] + r, 0, h[2] * 2 or 15]
    return bb


def oto_cerceve(proje_dir, proje, kaydet=True):
    """hedef/sahne_yaricap yoksa gerçek geometrinin bbox'ından çıkarır + proje.json'a yazar.
    Import edilen model origin'de ya da göz hizasında sanılıp kamera boşluğa bakmasın
    (model kadraj dışında kalıp 'aşırı pozlanmış dilim' görünüyordu).
    hedef = bbox merkezi, sahne_yaricap = modeli fov'a sığdıran kamera mesafesi.
    hedef zaten varsa dokunmaz (kullanıcının ya da oda kurulumunun ayarı korunur)."""
    if proje.get("hedef"):
        return proje
    bb = armatur_bbox(proje_dir, proje)          # [xmin,xmax,ymin,ymax,zmin,zmax]
    if not bb or len(bb) < 6:
        return proje
    cx, cy, cz = (bb[0] + bb[1]) / 2.0, (bb[2] + bb[3]) / 2.0, (bb[4] + bb[5]) / 2.0
    dx, dy, dz = bb[1] - bb[0], bb[3] - bb[2], bb[5] - bb[4]
    diag = math.sqrt(dx * dx + dy * dy + dz * dz)
    if diag <= 0:
        return proje
    # bounding sphere fov(45)'in yarısına sığsın: mesafe = r / sin(22.5°) + %15 pay
    mesafe = (diag / 2.0) / math.sin(math.radians(22.5)) * 1.15
    proje["hedef"] = [round(cx, 3), round(cy, 3), round(cz, 3)]
    proje["sahne_yaricap"] = round(max(mesafe, 3.0), 2)
    if kaydet:
        try:
            proje_kaydet(proje_dir, proje)
        except Exception:
            pass
    return proje


def gorunum_serbest(vp, vd, fov=50, balik=False):
    """Kamera konumu + bakış vektörü ile doğrudan view.
    balik=True -> 180° fisheye (glare/DGP-UGR için, -vta)."""
    if balik:
        return ("-vta -vp %g %g %g -vd %g %g %g -vu 0 0 1 -vh 180 -vv 180"
                % (vp[0], vp[1], vp[2], vd[0], vd[1], vd[2]))
    return ("-vtv -vp %g %g %g -vd %g %g %g -vu 0 0 1 -vh %g -vv %g"
            % (vp[0], vp[1], vp[2], vd[0], vd[1], vd[2], fov, fov * 0.75))


def gorunum(proje, bakis_az=180, mesafe=None, goz=1.6, fov=45, tur="perspektif"):
    """Hedef noktaya bakan kamera -> rpict view argümanları."""
    hedef = proje.get("hedef") or [0, 0, 0]
    mesafe = mesafe or proje.get("sahne_yaricap", 30)
    tx, ty, tz = hedef
    a = math.radians(bakis_az)
    cx = tx + mesafe * math.sin(a)
    cy = ty + mesafe * math.cos(a)
    cz = goz
    vd = (tx - cx, ty - cy, tz - cz)
    vt = {"perspektif": "-vtv", "balikgozu": "-vta", "plan": "-vtl"}.get(tur, "-vtv")
    if tur == "plan":
        cx, cy, cz = tx, ty, mesafe + tz
        vd = (0, 0, -1)
    if tur == "balikgozu":
        return "%s -vp %g %g %g -vd %g %g %g -vu 0 0 1 -vh 180 -vv 180" % (
            vt, cx, cy, cz, vd[0], vd[1], vd[2])
    return "%s -vp %g %g %g -vd %g %g %g -vu 0 0 1 -vh %g -vv %g" % (
        vt, cx, cy, cz, vd[0], vd[1], vd[2], fov, fov * 0.75)


def _ambient_dosyasi(oct_p, ayarlar=""):
    """Octree byte'ları ve hesap ayarlarına bağlı SHA-256 ambient key'i.
    Dışarıdaki texture/cal bağımlılıklarının değişimini ayrıca takip etmek lazım."""
    try:
        h = hashlib.sha256(str(ayarlar).encode("utf-8"))
        # content-addressed sahne adı dışarıdaki RTM/.dat/.cal/texture değişimini de taşır.
        h.update(os.path.abspath(oct_p).encode("utf-8"))
        with open(oct_p, "rb") as f:
            for parca in iter(lambda: f.read(1 << 20), b""):
                h.update(parca)
        return os.path.join(os.path.dirname(oct_p), "amb_%s.amb" % h.hexdigest()[:12])
    except OSError:
        return None


@_sirali_render
def render(oct_p, gorunum_str, kalite="taslak", W=1000, H=750, out_hdr=None,
           irradiance=False):
    out_hdr = out_hdr or (os.path.splitext(oct_p)[0] + ".hdr")
    # kalite: KALITE key'i (taslak/orta/final) ya da doğrudan ham rpict argümanları
    # ("-ab 4 -ad 2048..." gibi, elle girilmiş özel ray tracing ayarları)
    if kalite in KALITE:
        q = KALITE[kalite]
    elif isinstance(kalite, str) and "-" in kalite:
        q = kalite
    else:
        q = KALITE["taslak"]
    if not (1 <= int(W) <= 16384 and 1 <= int(H) <= 16384):
        raise ValueError("Render boyutları 1 ile 16384 arasında olmalı")
    if kalite == "onizleme" and irradiance:
        raise ValueError("Doğrudan önizleme ölçüm/analiz için kullanılamaz")
    ir = "-i " if irradiance else ""
    amb = _ambient_dosyasi(oct_p, q + " " + ir) if kalite != "onizleme" else None
    args = shlex.split(gorunum_str) + shlex.split(q)
    if irradiance:
        args += ["-i"]
    proje_dir = os.path.dirname(os.path.dirname(os.path.abspath(oct_p)))
    out_hdr = os.path.abspath(out_hdr)
    gecici = out_hdr + "." + uuid.uuid4().hex + ".tmp"
    plan = render_paralel.planla(W, H, kalite, oct_p, args=args, cwd=proje_dir)
    job_root = os.path.join(proje_dir, '_cache', 'render_jobs')
    os.makedirs(job_root, exist_ok=True)
    job_dir = tempfile.mkdtemp(prefix='is_', dir=job_root)
    job_log = os.path.join(job_dir, 'rpict.log')
    token = render_ilerleme.baslat(plan['motor'], isciler=plan['isciler'],
        toplam_parca=plan['toplam_parca'], durum_yolu=os.path.join(job_dir, 'ilerleme.json')
        if plan['motor'] == 'rpiece' else None, gunluk_yolu=job_log if plan['motor'] == 'rpict' else None,
        ambient_yolu=amb, bellek_butce=plan.get('bellek_butce'), bellek_isci=plan.get('bellek_isci'))
    try:
        if plan['motor'] == 'rpiece':
            r = render_paralel.calistir(plan, args, oct_p, proje_dir, gecici, amb, job_dir)
        else:
            cmd = ([] if WINDOWS else ["nice", "-n", "10"]) + ["rpict", *args]
            if amb:
                cmd += ["-af", os.path.relpath(amb, proje_dir)]
            cmd += ["-t", "5", "-e", os.path.relpath(job_log, proje_dir), "-x", str(int(W)), "-y", str(int(H)), os.path.abspath(oct_p)]
            env = radiance_ortami(); env['TMPDIR'] = job_dir
            r = surecler.calistir(cmd, cwd=proje_dir, env=env, stdout_yolu=gecici, timeout=3600)
        if r.returncode != 0 or not os.path.isfile(gecici) or not os.path.getsize(gecici):
            hata = r.stderr or ''
            if not hata and os.path.isfile(job_log):
                with open(job_log, 'rb') as f:
                    f.seek(max(0, os.fstat(f.fileno()).st_size - 1500))
                    hata = f.read().decode('utf-8', errors='replace')
            raise RuntimeError("%s HATA:\n%s" % (plan['motor'], (hata or "Boş çıktı")[-1500:]))
        surecler.iptal_kontrol()
        os.replace(gecici, out_hdr)
    finally:
        render_ilerleme.bitir(token)
        if os.path.exists(gecici):
            os.remove(gecici)
    return out_hdr


def _ppm_oku(yol):
    """binary P6 PPM -> (w, h, rgb_bytes)."""
    with open(yol, "rb") as f:
        veri = f.read()
    if not veri.startswith(b"P6"):
        raise RuntimeError("PPM değil: " + yol)
    # header: P6 <w> <h> <maxval> (araya # yorumları girebilir)
    i, alanlar = 2, []
    while len(alanlar) < 3:
        while i < len(veri) and veri[i:i+1].isspace():
            i += 1
        if veri[i:i+1] == b"#":
            while i < len(veri) and veri[i:i+1] != b"\n":
                i += 1
            continue
        j = i
        while j < len(veri) and not veri[j:j+1].isspace():
            j += 1
        alanlar.append(int(veri[i:j]))
        i = j
    i += 1  # header'dan sonra tek whitespace
    w, h, maks = alanlar
    ham = veri[i:i + w * h * 3]
    if maks != 255:  # 16-bit ise kabaca 8-bit'e indir
        ham = bytes(b for k, b in enumerate(veri[i:i + w * h * 6]) if k % 2 == 0)
    return w, h, ham


def png_yaz(w, h, rgb, out_png):
    """Saf Python PNG writer (8-bit RGB), sips/ImageMagick gerekmez,
    Windows/Mac aynı çalışır."""
    def parca(tip, veri):
        return (struct.pack(">I", len(veri)) + tip + veri +
                struct.pack(">I", zlib.crc32(tip + veri) & 0xffffffff))
    satirlar = b"".join(b"\x00" + rgb[y * w * 3:(y + 1) * w * 3]
                        for y in range(h))
    with open(out_png, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(parca(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)))
        f.write(parca(b"IDAT", zlib.compress(satirlar, 6)))
        f.write(parca(b"IEND", b""))
    return out_png


def hdr_to_png(hdr_p, out_png, poz=0.5, otomatik=False):
    """pozlama -> ppm -> png (saf Python, platformdan bağımsız).
    otomatik=True -> pcond -h (insan görüşü), sıfır enerji siyah kalır."""
    ppm = os.path.abspath(os.path.splitext(out_png)[0] + ".ppm")
    # pcond pfilt'i kendi kabuğuyla açar ve adı alıntılamaz. Bu yüzden HDR'nin
    # klasöründe çalışılır ve yalnız dosya adı verilir, $( ) içeren klasör adı komut çalıştırmaz.
    klasor, ad = os.path.split(os.path.abspath(hdr_p))
    hdr_q, ppm_q = _rad_komut_yolu(ad), _rad_komut_yolu(ppm)
    if otomatik:
        kod, ex, se = sh('pextrem %s' % hdr_q, cwd=klasor)
        if kod != 0:
            raise RuntimeError("pozlama HATA:\n" + se)
        siyah = all(float(v) == 0 for v in ex.strip().splitlines()[-1].split()[-3:])
        if siyah:
            kod, _, se = sh('ra_ppm %s > %s' % (hdr_q, ppm_q), cwd=klasor)
        else:
            kod, _, se = sh('pcond -h %s | ra_ppm > %s' % (hdr_q, ppm_q), cwd=klasor)
    else:
        # elle pozlama: ortalama parlaklığa bölme yok, siyah ya da seyrek HDR de geçerli.
        kod, _, se = sh('pfilt -1 -e %g %s | ra_ppm > %s' % (poz, hdr_q, ppm_q), cwd=klasor)
    if kod != 0 or not os.path.exists(ppm):
        raise RuntimeError("pozlama HATA:\n" + se)
    w, h, rgb = _ppm_oku(ppm)
    png_yaz(w, h, rgb, out_png)
    try:
        os.remove(ppm)
    except OSError:
        pass
    return out_png


def falsecolor_png(hdr_p, out_png, birim="Lux"):
    """Irradiance HDR -> falsecolor lüks haritası PNG (renk skalası + değerler).
    HDR rpict -i (irradiance) ile üretilmiş olmalı, falsecolor default ×179 ile W/m²->lüks yapar.
    Skala sahnenin gerçek maksimumuna oturtulur (yoksa hep sarı, doygun çıkar)."""
    olcek = ""
    try:                                   # otomatik üst skala = max irradiance ×179
        _, ex, _ = sh('pextrem %s' % _rad_komut_yolu(hdr_p))
        rgb = ex.strip().splitlines()[-1].split()[-3:]
        mx = max(float(v) for v in rgb) * 179.0
        if mx > 1:
            olcek = "-s %g " % mx
    except Exception:
        pass
    ppm = os.path.splitext(out_png)[0] + ".ppm"
    # falsecolor etiketi ve resim adını kendi tırnaksız shell komutlarına yapıştırır.
    # Etiket kısıtlanır, HDR stdin'den verilir, böylece o shell'e path ulaşmaz.
    if not re.fullmatch(r"[\w./%-]+", str(birim)):
        raise ValueError("Geçersiz falsecolor birimi")
    # TMPDIR altındaki File::Temp klasörü de bu komutlara tırnaksız girer.
    ek = None
    if not re.fullmatch(r"[\w./-]*", radiance_ortami().get("TMPDIR", "")):
        guvenli = next((d for d in ("/tmp", "/var/tmp") if os.path.isdir(d) and os.access(d, os.W_OK)), None)
        ek = {"TMPDIR": guvenli} if guvenli else None
    kod, _, se = sh('falsecolor -l %s %s< %s | ra_ppm > %s'
                    % (shlex.quote(birim), olcek, _rad_komut_yolu(hdr_p), _rad_komut_yolu(ppm)), ek_ortam=ek)
    if kod != 0 or not os.path.exists(ppm) or not os.path.getsize(ppm):
        raise RuntimeError("falsecolor HATA:\n" + se)
    w, h, rgb = _ppm_oku(ppm)
    png_yaz(w, h, rgb, out_png)
    try:
        os.remove(ppm)
    except OSError:
        pass
    return out_png


def pextrem(hdr_p):
    _, so, _ = sh('pextrem %s' % _rad_komut_yolu(hdr_p))
    return so.strip()


# ==================== import / converter katmanı ====================
# asıl kod importer.py'de. Aşağıdaki re-export sadece çağrılan `motor.X`
# adlarını taşır (dedektif ve çeviri komutu). Bu import en sonda olmalı:
# importer `import motor` yaptığında çekirdek tamamen tanımlı olsun.
from core.importer import (  # noqa: E402
    lumion_armaturler, cevrilebilir, ldt_ies, _evo_step_ents, evo_armaturler, evo_ies, evo_odalar, evo_mobilyalar, cevir, usd_kopru_calistir, usd_armaturler, USD_UZ,
)
