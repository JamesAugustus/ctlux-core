# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Sadece bu programın başlattığı child process'leri yönetir."""
import os
import signal
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from core import render_resources as render_kaynak

_kilit = threading.RLock()
_aktif = set()
_render_kilit = threading.RLock()
_nesil = 0
_oturum = threading.local()
_oturumlar = {}
_surec_sahipleri = {}


class IslemIptal(RuntimeError):
    """Kullanıcı iptali, hesap hatası da değil, başarılı çıktı da değil."""


def oturum_kimligi():
    return getattr(_oturum, 'kimlik', None)


def iptal_kontrol():
    """İptal edilen isteğin bir sonraki aşamayı başlatmasını engelle."""
    with _kilit:
        if (getattr(_oturum, 'nesil', _nesil) != _nesil or
                _oturumlar.get(oturum_kimligi(), False)):
            raise IslemIptal("İşlem iptal edildi")


@contextmanager
def islem_oturumu():
    """Hazırlık, render ve dönüşüm aynı iptal kimliğini taşır."""
    yeni = not hasattr(_oturum, 'nesil')
    if yeni:
        with _kilit:
            _oturum.nesil = _nesil
            _oturum.kimlik = uuid.uuid4().hex
            _oturumlar[_oturum.kimlik] = False
    try:
        iptal_kontrol()
        yield oturum_kimligi()
        iptal_kontrol()
    finally:
        if yeni:
            with _kilit:
                _oturumlar.pop(_oturum.kimlik, None)
            del _oturum.kimlik
            del _oturum.nesil


def _bitir(p):
    try:
        if os.name == "nt":
            if p.poll() is not None:
                return
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"],
                           capture_output=True, timeout=5)
        else:
            # group leader çıkmış olsa da stdout'u açık tutan alt process'ler
            # yaşıyor olabilir. Başlatılan grubun tamamı iptal edilmeli.
            os.killpg(p.pid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass


def calistir(cmd, *, shell=False, cwd=None, timeout=300, env=None, stdout_yolu=None,
             bellek_siniri=None):
    """communicate iki pipe'ı birlikte okur, timeout child'ları da kapatır."""
    dosya = None
    p = None
    try:
        with _kilit:
            iptal_kontrol()
            budget = getattr(_oturum, 'render_butce', None)
            limit = min(budget, bellek_siniri) if budget and bellek_siniri else budget or bellek_siniri
            if limit:
                render_kaynak.kontrol()
                command, use_shell = render_kaynak.komut(cmd, limit, shell)
            else:
                command, use_shell = cmd, shell
            dosya = open(stdout_yolu, 'wb') if stdout_yolu else None
            p = subprocess.Popen(command, shell=use_shell, cwd=cwd, env=env,
                                 stdout=dosya if dosya else subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, start_new_session=(os.name != "nt"))
            _aktif.add(p)
            _surec_sahipleri[p] = oturum_kimligi()
        try:
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(cmd, timeout)
                try:
                    out, err = p.communicate(timeout=min(.5, remaining) if limit else remaining)
                    break
                except subprocess.TimeoutExpired:
                    if not limit or time.monotonic() >= deadline:
                        raise
                    iptal_kontrol()
                    render_kaynak.kontrol()
        except subprocess.TimeoutExpired:
            _bitir(p)
            p.communicate()
            raise RuntimeError("İşlem zaman aşımına uğradı")
        except BaseException:
            _bitir(p)
            p.communicate()
            raise
        iptal_kontrol()
        return subprocess.CompletedProcess(cmd, p.returncode, out or '', err)
    finally:
        if p is not None and os.name != "nt":
            # bu API senkron komutun sahibidir. Leader normal çıkarken
            # pipe'larını kapatmış alt process'ler kalabilir, grubu bırakma.
            _bitir(p)
        with _kilit:
            _aktif.discard(p)
            _surec_sahipleri.pop(p, None)
        if dosya:
            dosya.close()


@contextmanager
def render_sirasi():
    with islem_oturumu():
        with _render_kilit:
            iptal_kontrol()
            if hasattr(_oturum, 'render_butce'):
                yield
                return
            # aynı kullanıcının başka CTLux/CLI process'leri de bu lock'u
            # kullanır. Lock dosyası silinmez, inode yarışı çıkmaz.
            with _makine_render_kilidi():
                _oturum.render_butce = render_kaynak.butce()
                try:
                    yield
                finally:
                    del _oturum.render_butce


@contextmanager
def _makine_render_kilidi():
    if os.name == 'nt':
        yield
        return
    import fcntl
    import stat
    path = os.environ.get('CTLUX_RENDER_LOCK') or '/tmp/ctlux-render-%d.lock' % os.getuid()
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode):
            raise RuntimeError('Render sıra kilidi güvenli değil')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError('Başka bir CTLux/Radiance hesabı çalışıyor, bitmesini bekleyin.') from error
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
