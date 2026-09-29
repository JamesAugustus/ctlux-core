# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Kayıtlı render işinin ölçülmüş ilerlemesi, process başlatmaz ya da durdurmaz.

Paralel motor atomik JSON yazar. Seri rpict ``-t 5 -e <işe özel log>``
raporunun sadece son tamamlanmış satırı okunur. Rapor formatının kaynağı:
https://github.com/LBNL-ETA/Radiance/blob/master/src/rt/rpict.c (report).
Yüzde, biten parça/pixel oranıdır, kalan süre tahmini değildir.
"""
import json
import math
import os
import re
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar


_kilit = threading.Lock()
_aktif = None
_akis = None
_akis_sahibi = ContextVar('radiance_akis_sahibi', default=None)
_JSON_SINIR = 65536
_GUNLUK_SINIR = 16384
_TAM_SINIR = 2**31 - 1
_RAPOR = re.compile(r'(?<![\w.+-])(?P<rays>\d+)\s+rays,[^\r\n]*?(?P<pct>[+-]?\d+(?:\.\d+)?)%\s+after\b[^\r\n]*\bhours\b')


@contextmanager
def is_akisi(islem_id):
    """Hazırlıktan çıktıya kadar tek sahibin gerçek aşamalarını takip eder."""
    global _akis
    simdi = time.monotonic()
    kayit = {'token': uuid.uuid4().hex, 'islem_id': islem_id, 'motor': 'radiance',
             'asama': 'hazirlik', 'adim': 'cerceve', 'etiket': 'Sahne sınırları hazırlanıyor',
             't0': simdi, 'asama_t0': simdi, 'adim_t0': simdi,
             'tamamlanan': None, 'toplam': None, 'birim': None, 'ayrinti': None,
             'onbellek': {}}
    baglam = _akis_sahibi.set(kayit['token'])
    with _kilit:
        _akis = kayit
    try:
        yield
    finally:
        with _kilit:
            if _akis is kayit:
                _akis = None
        _akis_sahibi.reset(baglam)


def adim(kod, etiket, *, asama='hazirlik', tamamlanan=None, toplam=None,
         birim=None, ayrinti=None, onbellek=None):
    """Sayaç sadece ölçülen local işi sayar, bütün işin yüzdesine çevrilmez."""
    if asama not in ('hazirlik', 'render', 'donusum'):
        raise ValueError('Bilinmeyen render aşaması')
    if (tamamlanan is not None or toplam is not None) and not (
            type(tamamlanan) is int and type(toplam) is int and 0 <= tamamlanan <= toplam):
        raise ValueError('İlerleme sayacı geçersiz')
    simdi = time.monotonic()
    with _kilit:
        if _akis is None or _akis['token'] != _akis_sahibi.get():
            return False
        if _akis['asama'] != asama:
            _akis['asama_t0'] = simdi
        if (_akis['asama'], _akis['adim'], _akis['ayrinti']) != (asama, kod, ayrinti):
            _akis['adim_t0'] = simdi
        _akis.update(asama=asama, adim=kod, etiket=etiket, tamamlanan=tamamlanan,
                     toplam=toplam, birim=birim, ayrinti=ayrinti)
        if onbellek:
            _akis['onbellek'].update(onbellek)
        return True


def akis_durumu(islem_id):
    """Başka işin ya da biten sahibin kaydı çağırana verilmez, diskten okuma yok."""
    simdi = time.monotonic()
    with _kilit:
        if _akis is None or not islem_id or _akis['islem_id'] != islem_id:
            return None
        sonuc = {k: v for k, v in _akis.items()
                 if k not in ('token', 'islem_id', 't0', 'asama_t0', 'adim_t0', 'onbellek')}
        sonuc.update(gecen_sn=round(max(0, simdi - _akis['t0']), 1),
                     asama_sn=round(max(0, simdi - _akis['asama_t0']), 1),
                     adim_sn=round(max(0, simdi - _akis['adim_t0']), 1),
                     genel_yuzde=None, eta_sn=None, onbellek=dict(_akis['onbellek']))
        return sonuc


def _tamsayi(deger, alt, ust):
    return type(deger) is int and alt <= deger <= ust


def _kimlik(s):
    return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)


def _yol_kaydi(yol):
    if yol is None:
        return None
    yol = os.path.abspath(os.fspath(yol))
    try:
        s = os.stat(yol, follow_symlinks=False)
        onceki = _kimlik(s) if stat.S_ISREG(s.st_mode) else None
    except OSError:
        onceki = None
    return (yol, onceki)


def baslat(backend, isciler=1, toplam_parca=None, durum_yolu=None, gunluk_yolu=None,
           ambient_yolu=None, bellek_butce=None, bellek_isci=None):
    """İş başlamadan önce çağrılır, path'ler sadece motorun sahip olduğu dosyalardır.

    Yeni kayıt öncekinin yerine geçer. Önceki işin geç gelen ``bitir`` çağrısı
    yeni kaydı silemez. Dosya bilgisi alınırken durum lock'u tutulmaz.
    """
    if backend not in ('rpict', 'rpiece'):
        raise ValueError('Bilinmeyen render ilerleme motoru')
    if not _tamsayi(isciler, 1, 1024):
        raise ValueError('İşçi sayısı 1 ile 1024 arasında tam sayı olmalı')
    if backend == 'rpict' and isciler != 1:
        raise ValueError('Seri rpict ilerlemesi bir işçiye aittir')
    if toplam_parca is not None and not _tamsayi(toplam_parca, 1, _TAM_SINIR):
        raise ValueError('Toplam parça pozitif tam sayı olmalı')
    kayit = {'token': uuid.uuid4().hex, 'backend': backend, 'isciler': isciler,
             'toplam': toplam_parca, 'baslangic': time.time(),
             'monotonic': time.monotonic(),
             'json': _yol_kaydi(durum_yolu), 'log': _yol_kaydi(gunluk_yolu),
             'ambient': _yol_kaydi(ambient_yolu), 'son_etkinlik': None, 'son_sayac': None,
             'bellek': {'butce_bayt': bellek_butce if _tamsayi(bellek_butce, 1, 2**53-1) else None,
                        'isci_sinir_bayt': bellek_isci if _tamsayi(bellek_isci, 1, 2**53-1) else None}}
    global _aktif
    with _kilit:
        _aktif = kayit
    return kayit['token']


def bitir(token):
    """Kaydı sadece onu başlatan sahip temizler, dosyalara dokunmaz."""
    global _aktif
    with _kilit:
        if _aktif is not None and _aktif['token'] == token:
            _aktif = None
            return True
    return False


def _oku(kayit, sinir, kuyruk=False):
    if kayit is None:
        return None
    yol, eski = kayit
    try:
        flags = os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_NOFOLLOW', 0)
        with os.fdopen(os.open(yol, flags), 'rb') as f:
            s = os.fstat(f.fileno())
            if not stat.S_ISREG(s.st_mode) or _kimlik(s) == eski:
                return None
            if not kuyruk:
                return f.read(sinir + 1) if s.st_size <= sinir else None
            # aynı log büyüdüyse eski işin satırlarını okuma. Yeni inode
            # ya da kısalmış log, yeni raporlara sıfırdan başlar.
            bas = eski[2] if eski and eski[:2] == _kimlik(s)[:2] and s.st_size >= eski[2] else 0
            ofset = max(bas, s.st_size - sinir)
            f.seek(ofset)
            data = f.read(sinir)
            if ofset > bas:  # tail'in ortasından kesilmiş ilk satırı at.
                data = data.partition(b'\n')[2]
            return data
    except (OSError, ValueError):
        return None


def _parca(data, kayit):
    try:
        d = json.loads(data)
    except (ValueError, TypeError, UnicodeError):
        return None
    if not isinstance(d, dict):
        return None
    done, total, workers = d.get('tamamlanan'), d.get('toplam'), d.get('isciler')
    phase = d.get('asama')
    if not (_tamsayi(total, 1, _TAM_SINIR) and _tamsayi(done, 0, total)
            and _tamsayi(workers, 1, 1024) and workers == kayit['isciler']
            and (kayit['toplam'] is None or total == kayit['toplam'])
            and phase in ('hesap', 'birlestirme', 'tamam')):
        return None
    if phase in ('birlestirme', 'tamam') and done != total:
        return None
    result = {'tamamlanan': done, 'toplam': total, 'yuzde': (10000 * done // total) / 100,
              'asama': phase, 'olcum': 'parca'}
    dagilim = _dagilim(d, done, total, workers)
    if dagilim:
        result.update(dagilim)
    return result


def _dagilim(data, done, total, workers):
    """Yeni alanların hepsi doğrulanır, eski yüzde JSON'ları geçerli kalır."""
    grid, tiles, processes = data.get('grid'), data.get('parcalar'), data.get('calisanlar')
    if not (isinstance(grid, dict) and isinstance(tiles, list) and isinstance(processes, list)
            and _tamsayi(grid.get('x'), 1, 32) and _tamsayi(grid.get('y'), 1, 32)
            and grid['x'] * grid['y'] == total and len(tiles) == total <= 1024
            and workers <= 16 and len(processes) <= workers):
        return None
    cleaned_processes, known_workers, pids = [], set(), set()
    for process in processes:
        if not isinstance(process, dict):
            return None
        worker, pid = process.get('isci'), process.get('pid')
        if not (_tamsayi(worker, 1, workers) and _tamsayi(pid, 1, _TAM_SINIR)
                and worker not in known_workers and pid not in pids):
            return None
        item = {'isci': worker, 'pid': pid}
        if 'etkin' in process:
            if type(process['etkin']) is not bool:
                return None
            item['etkin'] = process['etkin']
        cleaned_processes.append(item); known_workers.add(worker); pids.add(pid)
    cleaned_tiles, coords, completed = [], set(), 0
    for tile in tiles:
        if not isinstance(tile, dict):
            return None
        x, y, phase, worker = tile.get('x'), tile.get('y'), tile.get('durum'), tile.get('isci')
        if not (_tamsayi(x, 0, grid['x'] - 1) and _tamsayi(y, 0, grid['y'] - 1)
                and (x, y) not in coords and phase in ('sirada', 'hesap', 'tamam')):
            return None
        if worker is not None and (not _tamsayi(worker, 1, workers) or worker not in known_workers):
            return None
        if (phase == 'sirada' and worker is not None) or (phase == 'hesap' and worker is None):
            return None
        # sync append, stdout'taki begun satırından önce görünebilir: o zaman
        # biten parça bilinir, worker henüz bilinmediği için null kalır.
        completed += phase == 'tamam'
        coords.add((x, y)); cleaned_tiles.append({'x': x, 'y': y, 'durum': phase, 'isci': worker})
    if completed != done:
        return None
    return {'grid': {'x': grid['x'], 'y': grid['y']}, 'parcalar': cleaned_tiles,
            'calisanlar': cleaned_processes}


def _piksel(data):
    if not data:
        return None
    for line in reversed(data.decode('utf-8', errors='replace').splitlines(keepends=True)):
        if not line.endswith(('\n', '\r')):
            continue  # motorun henüz yazmakta olduğu yarım raporu yüzde sanma.
        match = _RAPOR.search(line)
        if match:
            pct = float(match.group('pct'))
            if not math.isfinite(pct) or not 0 <= pct <= 100:
                return None
            ray_text = match.group('rays')
            # JSON'u JavaScript okuyabilir, 2^53 üstü sayaç orada tam temsil edilmez, gönderme.
            rays = int(ray_text) if len(ray_text) <= 16 else None
            if rays is not None and rays > 2**53 - 1:
                rays = None
            return {'yuzde': pct, 'olcum': 'piksel', 'isin_sayisi': rays,
                    'isin_olcum_kaynagi': 'rpict_native_report',
                    'isin_sayisi_aciklama': 'Sayaç güvenli tam sayı sınırını aşıyor' if rays is None else None}
    return None


def _dosya_ornegi(kayit):
    """İşe bağlı dosyanın sadece metadata'sı, symlink takip edilmez."""
    if kayit is None:
        return None
    try:
        info = os.stat(kayit[0], follow_symlinks=False)
        return info if stat.S_ISREG(info.st_mode) else None
    except OSError:
        return None


def _faaliyet(kayit, sonuc):
    now = time.time()
    ambient = _dosya_ornegi(kayit['ambient'])
    original = kayit['ambient'][1] if kayit['ambient'] else None
    changed = ambient is not None and _kimlik(ambient) != original
    worker_sample = _dosya_ornegi(kayit['json'])
    workers = sonuc.get('calisanlar')
    active = (sum(p['etkin'] for p in workers) if workers is not None
              and all(type(p.get('etkin')) is bool for p in workers) else None)
    counter = (sonuc.get('tamamlanan'), sonuc.get('yuzde'), sonuc.get('isin_sayisi'))
    counter_sample = worker_sample if kayit['backend'] == 'rpiece' else _dosya_ornegi(kayit['log'])
    with _kilit:
        if _aktif is not kayit:
            return None
        if changed and kayit['baslangic'] <= ambient.st_mtime <= now:
            kayit['son_etkinlik'] = max(kayit['son_etkinlik'] or 0, ambient.st_mtime)
        if (counter != kayit['son_sayac'] and any(v is not None and v > 0 for v in counter)
                and counter_sample is not None and kayit['baslangic'] <= counter_sample.st_mtime <= now):
            kayit['son_etkinlik'] = max(kayit['son_etkinlik'] or 0, counter_sample.st_mtime)
        if sonuc.get('olcum') is not None:
            kayit['son_sayac'] = counter
        last = kayit['son_etkinlik']
    return {'ornek_zamani': now, 'son_etkinlik_zamani': last,
            'etkin_isci': active, 'isci_ornek_zamani': worker_sample.st_mtime if worker_sample else None,
            'ambient_bayt': ambient.st_size if ambient else None,
            'ambient_degisim_bayt': max(0, ambient.st_size - (original[2] if original else 0)) if ambient else None}


def durum():
    """Sabit boyutlu dosya okuması lock dışında, eski sahibin sonucu atılır."""
    with _kilit:
        kayit = _aktif
    if kayit is None:
        return None
    sonuc = {'backend': kayit['backend'], 'isciler': kayit['isciler'],
             'yuzde': None, 'tamamlanan': None, 'toplam': kayit['toplam'],
             'olcum': None, 'isin_sayisi': None, 'asama': 'hesap', 'baslangic': kayit['baslangic'],
             'gecen_sn': round(max(0, time.monotonic() - kayit['monotonic']), 1),
             'aciklama': 'Parça/piksel ilerlemesi, süre tahmini değil.'}
    if kayit['backend'] == 'rpiece':
        olcum = _parca(_oku(kayit['json'], _JSON_SINIR), kayit)
    else:
        olcum = _piksel(_oku(kayit['log'], _GUNLUK_SINIR, kuyruk=True))
    if olcum:
        sonuc.update(olcum)
    sonuc['faaliyet'] = _faaliyet(kayit, sonuc)
    sonuc['bellek'] = dict(kayit['bellek'])
    with _kilit:
        return sonuc if _aktif is kayit else None
