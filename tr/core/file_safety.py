# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Proje sınırı kontrolü ve atomik kayıt."""
import json
import os
import re
import stat
import tempfile
from pathlib import Path


def ic_yol(kok, goreli):
    """Absolute path, .. ya da symlink ile kök dışına çıkan yolu reddet."""
    s = str(goreli).replace("\\", "/")
    # Sürücü öneki (C:) ya da mutlak/UNC biçim hiçbir zaman göreli değildir. Windows'ta ':' ayrıca
    # alternatif veri akışı açar, başka sistemlerde sıradan bir dosya adı karakteridir.
    if (not s or s.startswith("/") or re.match(r"[A-Za-z]:", s)
            or (os.name == "nt" and ":" in s) or ".." in s.split("/")):
        raise ValueError("Geçersiz göreli dosya yolu")
    root = Path(kok).resolve()
    p = (root / s).resolve()
    if p == root or root not in p.parents:
        raise ValueError("Dosya yolu proje dışında")
    return str(p)


def _dosya_izni(yol):
    """Var olan dosyanın izni, yoksa süreç umask'ı ile sınırlanan 0666."""
    try:
        return stat.S_IMODE(os.stat(yol).st_mode)
    except FileNotFoundError:
        pass
    try:
        # umask burada okunur, böylece başka thread'ler dosya oluştururken değiştirilmez.
        with open("/proc/self/status", encoding="ascii") as f:
            umask = next(int(line.split()[1], 8) for line in f if line.startswith("Umask:"))
    except (OSError, StopIteration, ValueError, IndexError):
        umask = os.umask(0o022)
        os.umask(umask)
    return 0o666 & ~umask


def atomik_json(yol, veri):
    """Aynı file system'de replace, yarım kalan JSON eski kaydı bozmaz."""
    parent = os.path.dirname(os.path.abspath(yol))
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".kayit_", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(veri, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        # mkstemp 0600 oluşturur. Değiştirilen dosyanın izni korunur, yeni dosyada umask varsayılanı kullanılır.
        os.chmod(tmp, _dosya_izni(yol))
        os.replace(tmp, yol)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
