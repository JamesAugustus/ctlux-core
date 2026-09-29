# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Var olan EVO light'larına salt okunur kaynak metadata'sı bağlar.

Sadece proje içindeki açık göreli kaynak ya da model/ içindeki tek EVO okunur.
Kaynak geometri, light değerleri ve proje dosyaları değişmez. Kaynaktaki
world matrix sadece köken bilgisidir, kullanıcının güncel x/y/z/yon alanlarının
yerine geçmez. İsimler ve kaynak path'leri log'a ya da warning metnine yazılmaz.
"""
from collections import OrderedDict
from copy import deepcopy
from pathlib import Path
import re
import threading

from core.file_safety import ic_yol


METADATA_ALANLARI = (
    'kaynak_id', 'kaynak_guid', 'kaynak_kayit', 'kaynak_ad',
    'urun_id', 'urun_guid', 'urun_kayit', 'urun_ad', 'urun_adlari', 'urun_kod',
    'urun_temsil_kayit', 'dizi_id', 'dizi_guid', 'dizi_kayit', 'dizi_ad',
    'kaynak_dunya_matrisi',
)
_cache = OrderedDict()
_lock = threading.RLock()


def _aday_kimlik(armatur):
    if not isinstance(armatur, dict):
        return None
    source = armatur.get('kaynak')
    # başka formatın ya da açıkça kullanıcının ürettiği kaydın adına bakıp
    # EVO'dan geldi sanma. Kaynağı eksik eski teknik kayıt desteklenir.
    if source not in (None, '', 'evo/STEP'):
        return None
    identity = armatur.get('kaynak_id')
    if source == 'evo/STEP' and identity is not None:
        return str(identity)
    old = re.fullmatch(r'armatur_(\d+)', str(armatur.get('ad', '')))
    return old[1] if old else None


def _kaynak(proje_dir, proje):
    import_meta = proje.get('ice_aktarma')
    import_meta = import_meta if isinstance(import_meta, dict) else {}
    if 'kaynak_evo' in proje or 'kaynak_evo' in import_meta:
        relative = proje.get('kaynak_evo', import_meta.get('kaynak_evo'))
        if not isinstance(relative, str) or not relative.lower().endswith('.evo'):
            raise ValueError('source')
        path = Path(ic_yol(proje_dir, relative))
    else:
        folder = Path(ic_yol(proje_dir, 'model'))
        candidates = [p.name for p in folder.iterdir() if p.suffix.lower() == '.evo'] if folder.is_dir() else []
        if len(candidates) != 1:
            return None, ('Birden fazla yerel EVO kaynağı var, kaynak seçilmeden kimlik bilgisi bağlanmadı.'
                          if candidates else 'Yerel EVO kaynağı bulunamadı, kaynak adları için yeniden içe aktarma gerekebilir.')
        path = Path(ic_yol(proje_dir, 'model/' + candidates[0]))
    if not path.is_file():
        return None, 'Yerel EVO kaynağı bulunamadı, mevcut ışık kayıtları korundu.'
    return path, None


def _imza(path):
    stat = path.stat()
    return str(path.resolve()), stat.st_size, stat.st_mtime_ns


def _metadata(path):
    from core import engine as motor
    with _lock:
        key = _imza(path)
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
        records = motor.evo_armaturler(str(path))
        if _imza(path) != key:
            raise ValueError('source changed')
        result = {}
        for record in records:
            identity = record.get('kaynak_id')
            if identity is not None:
                identity = str(identity)
                if identity in result:
                    raise ValueError('duplicate source identity')
                result[identity] = {field: deepcopy(record[field]) for field in METADATA_ALANLARI if field in record}
        _cache[key] = result
        _cache.move_to_end(key)
        while len(_cache) > 8:
            _cache.popitem(last=False)
        return result


def zenginlestir(proje_dir, proje, armaturler):
    """(bağımsız light kopyası, genel notlar) döner, hiçbir dosyaya yazmaz.

    Var olan metadata'nın bile üzerine yazılmaz. kaynak=evo/STEP kayıtları
    kaynak_id ile eşleşir, kaynağı eksik eski kayıtlar sadece tam armatur_N
    teknik adıyla eşleşir. Açık kaynak hatasında geniş dizin taramasına düşmez.
    """
    copied = deepcopy(armaturler)
    if not isinstance(copied, list):
        return copied, []
    eligible = [(a, _aday_kimlik(a)) for a in copied]
    eligible = [(a, identity) for a, identity in eligible if identity is not None]
    if not eligible:
        return copied, []
    try:
        path, note = _kaynak(proje_dir, proje)
        if note:
            return copied, [note]
        metadata = _metadata(path)
    except Exception:
        return copied, ['EVO kaynak kimlikleri güvenle okunamadı, mevcut ışık kayıtları korundu.']
    missing = 0
    for armatur, identity in eligible:
        source = metadata.get(identity)
        if source is None:
            missing += 1
            continue
        for field, value in source.items():
            if field not in armatur:
                armatur[field] = deepcopy(value)
    notes = ['Bazı ışıkların EVO kaynak kimliği eşleşmedi, mevcut değerleri korundu.'] if missing else []
    return copied, notes
