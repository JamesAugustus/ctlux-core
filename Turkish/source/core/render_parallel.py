# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Radiance'ın local rpiece kuyruğu, aynı kamera, paylaşılan ambient dosyası.

Her işin supervisor'ı surecler.calistir ile ayrı process group'ta başlatılır.
rpiece, rpict ve rpiece'in writer child'ları bu grupta kalır: iptal ya da
timeout bütün işi kapatır. Ne shell ne de ortak /tmp kullanılır.
Parça sayısı hesap ilerlemesidir, süre tahmini ya da doğruluk ölçüsü değildir.
"""
import json
import math
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
import sys
import time
import uuid

if __name__ == '__main__' and not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import processes as surecler
from core import render_resources as render_kaynak


def _cpu_sayisi():
    try:
        count = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        count = os.cpu_count() or 1
    for group in render_kaynak.cgroup_yollari():
        try:
            quota, period = (group / 'cpu.max').read_text().split()
            if quota != 'max':
                q, p = int(quota), int(period)
                if q > 0 and p > 0:
                    count = min(count, max(1, q // p))
        except (OSError, ValueError):
            pass
    return count


def _bos_bellek():
    """İşletim sistemi ve varsa cgroup limitinin küçüğü, byte."""
    return render_kaynak.bos_bellek()


def _sahne_boyutu(oct_p):
    """Küçük ana OCT'nin dışarıda tuttuğu mesh/instance dosyalarını da say.

    Hazırlık içerik bütünlüğünü zaten doğruluyor. Burada büyük dosyalar
    tekrar okunmaz: manifest ve stat yeterli, aynı bağımlılık bir kere sayılır.
    """
    octree = Path(oct_p).resolve()
    paths = {octree}
    record = Path(str(octree) + '.json')
    if record.is_file():
        if record.stat().st_size > 1 << 20:
            raise ValueError('Sahne bağımlılık kaydı çok büyük')
        data = json.loads(record.read_text())
        root = octree.parent.parent
        for entry in data['turetilmis']:
            path = (root / entry['yol']).resolve()
            if not path.is_relative_to(root):
                raise ValueError('Sahne bağımlılığı proje dışında')
            paths.add(path)
    return sum(p.stat().st_size for p in paths)


def _gorunum_args(args):
    counts = {'-vp': 3, '-vd': 3, '-vu': 3, '-vh': 1, '-vv': 1, '-vo': 1,
              '-va': 1, '-vs': 1, '-vl': 1, '-vf': 1, '-pa': 1}
    output = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith('-vt') and len(arg) == 4:
            output.append(arg)
        elif arg in counts:
            count = counts[arg]
            if i + count >= len(args):
                raise ValueError('Eksik kamera argümanı: ' + arg)
            output.extend(args[i:i + count + 1])
            i += count
        i += 1
    return output


def _paralel_args_guvenli(args):
    # bilinmeyen option'ların rpiece'in -X/-F/-o sözleşmesini değiştirmesine
    # izin verme. Hazır kalite option'larının değerleri aynen geçer.
    numeric = {'-vp': 3, '-vd': 3, '-vu': 3, '-av': 3, '-me': 3, '-ma': 3}
    for name in ('-vh', '-vv', '-vo', '-va', '-vs', '-vl', '-pa', '-aw', '-ab', '-aa',
                 '-ad', '-as', '-ar', '-dc', '-dt', '-dj', '-ds', '-dr', '-dp', '-st', '-ss',
                 '-lr', '-lw', '-ps', '-pt', '-pj', '-pd', '-mg', '-ms'):
        numeric[name] = 1
    toggles = {name + suffix for name in ('-i', '-bv', '-u', '-w', '-dv') for suffix in ('', '+', '-')}
    i = 0
    while i < len(args):
        value = args[i]
        if value in toggles or value in ('-vtv', '-vtl', '-vta', '-vth', '-vts', '-vtc'):
            i += 1
            continue
        count = numeric.get(value, 1 if value == '-vf' else None)
        if count is None or i + count >= len(args):
            return False
        values = args[i + 1:i + count + 1]
        if value == '-vf':
            if values[0].startswith(('@', '$', '-')):
                return False
        else:
            try:
                if not all(math.isfinite(float(item)) for item in values):
                    return False
            except ValueError:
                return False
        i += count + 1
    return True


def _bolen_grid(width, height, workers):
    """rpiece'in yuvarlamasına izin verme: iki eksenin de tam bölenleri."""
    target = workers * 4
    candidates = []
    for x in range(1, min(32, width // 8) + 1):
        if width % x:
            continue
        for y in range(1, min(32, height // 8) + 1):
            total = x * y
            if height % y or not workers <= total <= workers * 8:
                continue
            shape = abs(math.log((width / x) / (height / y)))
            score = abs(total - target) / target + shape * .5
            candidates.append((score, total, x, y))
    if not candidates:
        return None
    _, _, x, y = min(candidates)
    return x, y


def planla(W, H, kalite, oct_p, *, args=(), cwd=None, env=None):
    """Kaynak limitleri + native pixel boyutu, uymuyorsa seri rpict.

    CTLUX_RENDER_WORKERS=1 seriyi zorlar. 2..16
    istekleri CPU ve bellek limitlerini aşamaz. Texture/instance RAM'i dosya
    boyutundan tam çıkmaz, worker sayısı tahmin. Native process'lere ayrıca
    address space limiti uygulanır.
    """
    from core import engine as motor
    env = motor.radiance_ortami(env)
    result = {'motor': 'rpict', 'isciler': 1, 'xdiv': 1, 'ydiv': 1,
              'toplam_parca': None, 'width': int(W), 'height': int(H), 'neden': ''}
    budget = max(64 << 20, int(_bos_bellek() * .5))
    result.update(bellek_butce=budget, bellek_isci=budget)
    def serial(reason):
        result['neden'] = reason
        return result
    if os.name == 'nt':
        return serial('Windows için seri Radiance yolu')
    try:
        requested = int(env.get('CTLUX_RENDER_WORKERS', '0'))
    except ValueError:
        requested = 0
    if requested == 1:
        return serial('CTLUX_RENDER_WORKERS=1')
    # direct light önizlemesi de çok kaynaklı sahnelerde pahalı olabiliyor.
    # kalite ayarlarına dokunmadan, yeterli pixel varsa işi bölebiliriz.
    if int(W) * int(H) < 65536:
        return serial('Küçük görüntü')
    rpiece = shutil.which('rpiece', path=env.get('PATH'))
    vwrays = shutil.which('vwrays', path=env.get('PATH'))
    if not rpiece or not vwrays:
        return serial('rpiece/vwrays bulunamadı')
    rpiece = str(Path(rpiece).resolve())
    # rpiece, rpict'i PATH üzerinden açar. Symlink launcher'ların gerçek dağıtımı
    # bulunur, farklı dağıtımdan bir child ile sessizce devam edilmez.
    sibling = Path(rpiece).parent / 'rpict'
    rpict = str(sibling) if sibling.is_file() and os.access(sibling, os.X_OK) else shutil.which('rpict', path=env.get('PATH'))
    if not rpict or Path(rpict).resolve().parent != Path(rpiece).parent:
        return serial('rpiece ve rpict aynı Radiance dağıtımında değil')
    # kalıcı rpict process'leri, çoklu görüntü ve kullanıcının çıktı yönlendirmesi
    # rpiece'in senkronizasyon sözleşmesine karıştırılmaz.
    if not _paralel_args_guvenli(args):
        return serial('Özel rpict seçeneği seri çalışma gerektiriyor')
    cpus = _cpu_sayisi()
    cpu_limit = max(1, cpus - 2)
    workers = min(max(2, min(16, requested)) if requested > 1 else 6, cpu_limit)
    try:
        scene_bytes = _sahne_boyutu(oct_p)
    except (OSError, ValueError, KeyError, TypeError):
        return serial('Sahne bağımlılıkları okunamadı, sınırlı tek işçi')
    result['sahne_bayt'] = scene_bytes
    # ana OCT ile bütün bağlı geometriler, process başına en az 128 MiB.
    # tahmini aşan native allocation, makineyi taşırmadan hata verir.
    memory_limit = max(1, budget // max(128 << 20, scene_bytes * 2))
    workers = min(workers, memory_limit)
    if workers < 2:
        return serial('CPU/bellek payı tek işçiye uygun')
    try:
        view = _gorunum_args(list(args))
        native = surecler.calistir([vwrays, '-d', *view, '-x', str(int(W)), '-y', str(int(H))],
                                  cwd=cwd, env=env, timeout=15)
    except (OSError, ValueError):
        return serial('Native kamera boyutu okunamadı')
    match = re.search(r'-x\s+(\d+)\s+-y\s+(\d+)', native.stdout or '')
    if native.returncode or not match:
        return serial('Native kamera boyutu okunamadı')
    width, height = map(int, match.groups())
    grid = _bolen_grid(width, height, workers)
    if not grid:
        return serial('Piksel boyutunu koruyan parça bölümü bulunamadı')
    x, y = grid
    result.update(motor='rpiece', isciler=workers, xdiv=x, ydiv=y,
                  toplam_parca=x * y, width=width, height=height,
                  bellek_isci=budget // workers,
                  rpiece=rpiece, radiance_bin=str(Path(rpiece).parent), neden='Paylaşılan ambient ve yerel parça kuyruğu')
    return result


def _atomik_json(path, data):
    path = Path(path)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(',', ':')), encoding='utf-8')
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def calistir(plan, rpict_args, octree, cwd, out_hdr, ambient, job_dir, *, timeout=3600):
    """Paralel supervisor'ı surecler'deki sahibine bağla, HDR'ı yayımlamaz."""
    if plan.get('motor') != 'rpiece':
        raise ValueError('Bu çalıştırıcı yalnız rpiece planını kabul eder')
    job_dir = Path(job_dir).resolve()
    job_dir.mkdir(parents=True, exist_ok=True)
    job = {'version': 1, 'plan': plan, 'args': list(rpict_args),
           'octree': os.path.abspath(octree), 'cwd': os.path.abspath(cwd),
           'output': os.path.abspath(out_hdr), 'ambient': os.path.abspath(ambient) if ambient else None,
           'directory': str(job_dir)}
    path = job_dir / 'job.json'
    _atomik_json(path, job)
    from core import engine as motor
    env = motor.radiance_ortami()
    env['TMPDIR'] = str(job_dir)
    # rpiece'in kendi rpict child'ı aynı Radiance dağıtımından bulunur.
    env['PATH'] = str(Path(plan['rpiece']).resolve().parent) + os.pathsep + env.get('PATH', '')
    return surecler.calistir([sys.executable, str(Path(__file__).resolve()), str(path)],
                            cwd=cwd, env=env, timeout=timeout,
                            bellek_siniri=plan.get('bellek_isci'))


def _sync_tamamlanan(path, xdiv, ydiv):
    """İlk iki satır grid ve son atanan iş, sadece sonradan eklenen çiftler biter."""
    try:
        import fcntl
        with open(path, 'rb') as stream:
            # native rpiece F_SETLKW kullanıyor, Linux flock lock'ları bununla
            # çakışmaz. lockf aynı POSIX record lock'unu paylaşır.
            fcntl.lockf(stream, fcntl.LOCK_SH)
            data = stream.read(1024 * 1024)
            fcntl.lockf(stream, fcntl.LOCK_UN)
    except FileNotFoundError:
        return set()
    lines = data.splitlines()
    if data and not data.endswith(b'\n'):
        lines.pop()  # yarım kalmış son append biten parça sayılmaz.
    if len(lines) < 3:
        return set()
    try:
        if tuple(map(int, lines[0].split())) != (xdiv, ydiv):
            raise RuntimeError('Parça senkronizasyon grid bilgisi uyuşmuyor')
        done = set()
        for line in lines[3:]:
            if not line.strip():
                continue
            x, y = map(int, line.split())
            if not (0 <= x < xdiv and 0 <= y < ydiv):
                raise RuntimeError('Parça senkronizasyon indeksi sınır dışında')
            done.add((x, y))
        return done
    except ValueError as error:
        raise RuntimeError('Parça senkronizasyon dosyası bozuk') from error


def _hdr_dogrula(path, width, height):
    """rpiece'in sıkıştırılmamış RGBE gövdesi, tek başına bittiğinin kanıtı değil."""
    with open(path, 'rb') as stream:
        header = bytearray()
        while len(header) < 65536:
            line = stream.readline(4096)
            if not line:
                raise RuntimeError('Parça HDR başlığı eksik')
            header.extend(line)
            if line == b'\n':
                break
        else:
            raise RuntimeError('Parça HDR başlığı çok büyük')
        if not header.startswith((b'#?RADIANCE\n', b'#?RGBE\n')) or b'FORMAT=32-bit_rle_rgbe' not in header:
            raise RuntimeError('Parça HDR biçimi geçersiz')
        resolution = stream.readline(256)
        match = re.fullmatch(rb'-Y\s+(\d+)\s+\+X\s+(\d+)\s*\n', resolution)
        if not match or tuple(map(int, match.groups())) != (height, width):
            raise RuntimeError('Parça HDR piksel boyutu native kamerayla uyuşmuyor')
        if os.fstat(stream.fileno()).st_size - stream.tell() != width * height * 4:
            raise RuntimeError('Parça HDR piksel gövdesi eksik')


def _denetle(job):
    """Sadece surecler tarafından, yeni group leader olarak çalıştırılır."""
    if os.name == 'nt' or os.getpgrp() != os.getpid():
        raise RuntimeError('Paralel denetçi ayrı süreç grubunda çalışmalı')
    directory = Path(job['directory']).resolve()
    plan = job['plan']
    workers = int(plan['isciler'])
    xdiv, ydiv = int(plan['xdiv']), int(plan['ydiv'])
    width, height = int(plan['width']), int(plan['height'])
    if not (2 <= workers <= 16 and 1 <= xdiv <= 32 and 1 <= ydiv <= 32 and
            1 <= width <= 16384 and 1 <= height <= 16384 and width % xdiv == 0 and height % ydiv == 0):
        raise ValueError('Paralel iş planı geçersiz')
    if job.get('version') != 1 or not directory.is_dir():
        raise ValueError('Paralel iş dosyası geçersiz')
    sync = directory / 'pieces.sync'
    if sync.exists() or Path(job['output']).exists():
        raise RuntimeError('Paralel iş yalnız yeni geçici dosyalara yazabilir')
    status = {'tamamlanan': 0, 'baslanan': 0, 'toplam': xdiv * ydiv,
              'isciler': workers, 'asama': 'hesap'}
    processes, logs, buffers, started = [], [], {}, set()
    owners, pipe_workers, completed = {}, {}, set()
    last_codes = None
    last_publish = 0
    def publish():
        nonlocal last_codes, last_publish
        last_publish = time.monotonic()
        # koordinatlar native rpiece düzeninde: (0,0) sol alttaki parça.
        # worker id 1'den başlar, PID sadece bu işin başlattığı rpiece'e ait.
        status['grid'] = {'x': xdiv, 'y': ydiv}
        last_codes = tuple(p.poll() for p in processes)
        status['calisanlar'] = [{'isci': i + 1, 'pid': p.pid, 'etkin': last_codes[i] is None}
                                for i, p in enumerate(processes)]
        status['parcalar'] = [
            {'x': x, 'y': y, 'durum': 'tamam' if (x, y) in completed else 'hesap' if (x, y) in started else 'sirada',
             'isci': owners.get((x, y))}
            for y in range(ydiv) for x in range(xdiv)]
        _atomik_json(directory / 'ilerleme.json', status)
    publish()
    args = [plan['rpiece'], '-v', *job['args']]
    if job['ambient']:
        # native rpict'in option kontrolü ASCII dışı -af metnini
        # reddediyor. Üstteki proje path'i Türkçe olabilir, üretilen cache adı
        # ASCII. cwd'ye göreli path aynı paylaşılan dosyayı gösterir.
        args.extend(['-af', os.path.relpath(job['ambient'], job['cwd'])])
    args.extend(['-pa', '0', '-x', str(width), '-y', str(height), '-X', str(xdiv), '-Y', str(ydiv),
                 '-F', str(sync), '-o', job['output'], job['octree']])
    command = (['nice', '-n', '10'] if shutil.which('nice') else []) + args
    selector = selectors.DefaultSelector()
    try:
        for i in range(workers):
            error_log = open(directory / ('worker_%02d.log' % i), 'wb')
            logs.append(error_log)
            process = subprocess.Popen(command, cwd=job['cwd'], stdout=subprocess.PIPE, stderr=error_log,
                                       stdin=subprocess.DEVNULL, start_new_session=False)
            processes.append(process)
            os.set_blocking(process.stdout.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ)
            buffers[process.stdout] = b''
            pipe_workers[process.stdout] = i + 1
        publish()
        while selector.get_map() or any(p.poll() is None for p in processes):
            for key, _ in selector.select(.1):
                chunk = os.read(key.fileobj.fileno(), 8192)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                value = buffers[key.fileobj] + chunk
                lines = value.split(b'\n')
                buffers[key.fileobj] = lines.pop()
                if len(buffers[key.fileobj]) > 8192:
                    raise RuntimeError('Beklenmeyen parça günlük satırı')
                for line in lines:
                    event = re.fullmatch(rb'\s*(\d+)\s+(\d+)\s+(begun|done)\s*', line)
                    if event:
                        tile = tuple(map(int, event.groups()[:2]))
                        if not (0 <= tile[0] < xdiv and 0 <= tile[1] < ydiv):
                            raise RuntimeError('Parça günlük indeksi sınır dışında')
                        started.add(tile)
                        owners[tile] = pipe_workers[key.fileobj]
                        status['baslanan'] = len(started)
                        completed = _sync_tamamlanan(sync, xdiv, ydiv)
                        status['tamamlanan'] = len(completed)
                        publish()
            codes = tuple(p.poll() for p in processes)
            if any(code not in (None, 0) for code in codes):
                raise RuntimeError('Radiance parça işçisi başarısız oldu')
            if codes != last_codes or time.monotonic() - last_publish >= 1:
                # başka bir parça uzun sürerken işini bitirmiş worker'ı aktif gösterme.
                publish()
            completed = _sync_tamamlanan(sync, xdiv, ydiv)
            done = len(completed)
            if done != status['tamamlanan']:
                status['tamamlanan'] = done
                publish()
        for process in processes:
            if process.wait() != 0:
                raise RuntimeError('Radiance parça işçisi başarısız oldu')
        done = completed = _sync_tamamlanan(sync, xdiv, ydiv)
        if len(done) != xdiv * ydiv:
            raise RuntimeError('Radiance bütün parçaları tamamlamadı: %d/%d' % (len(done), xdiv * ydiv))
        status.update(tamamlanan=len(done), asama='birlestirme')
        publish()
        _hdr_dogrula(job['output'], width, height)
        status['asama'] = 'tamam'
        publish()
    except BaseException as error:
        try:
            status['hata'] = str(error)
            try:
                publish()
            except OSError:
                pass  # disk dolu olsa da temizlik yapılır.
            print(str(error), file=sys.stderr, flush=True)
            for i, log in enumerate(logs):
                try:
                    log.flush()
                    with open(directory / ('worker_%02d.log' % i), 'rb') as stream:
                        stream.seek(max(0, os.fstat(stream.fileno()).st_size - 2500))
                        print(stream.read().decode('utf-8', errors='replace'), file=sys.stderr, flush=True)
                except OSError:
                    pass
        finally:
            if processes:
                # grup bu supervisor'a ait. İçinde ana program ya da başka render process'i
                # yok. Rapor dosyası hatası temizliği atlatamaz.
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                os.killpg(os.getpid(), signal.SIGTERM)
                time.sleep(.1)
                os.killpg(os.getpid(), signal.SIGKILL)
        raise
    finally:
        selector.close()
        for process in processes:
            if process.stdout:
                process.stdout.close()
        for log in logs:
            log.close()


if __name__ == '__main__':
    try:
        if len(sys.argv) != 2:
            raise ValueError('Tek yerel iş JSON dosyası gerekli')
        path = Path(sys.argv[1]).resolve()
        job = json.loads(path.read_text(encoding='utf-8'))
        if Path(job['directory']).resolve() != path.parent:
            raise ValueError('İş dosyası kendi geçici dizininde olmalı')
        _denetle(job)
    except BaseException as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
