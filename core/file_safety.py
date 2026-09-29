# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Project boundary checks and atomic saves."""
import json
import os
import re
import stat
import tempfile
from pathlib import Path


def internal_path(root_directory, relative):
    """Reject absolute paths, .. components, and symlinks that escape the root."""
    s = str(relative).replace("\\", "/")
    # A drive prefix (C:) or an absolute/UNC form is never relative. On Windows ':' also opens
    # alternate data streams, elsewhere it is an ordinary file name character.
    if (not s or s.startswith("/") or re.match(r"[A-Za-z]:", s)
            or (os.name == "nt" and ":" in s) or ".." in s.split("/")):
        raise ValueError("Invalid relative file path")
    root = Path(root_directory).resolve()
    p = (root / s).resolve()
    if p == root or root not in p.parents:
        raise ValueError("File path is outside the project")
    return str(p)


def _file_mode(path):
    """Mode of an existing file, otherwise 0666 limited by the process umask."""
    try:
        return stat.S_IMODE(os.stat(path).st_mode)
    except FileNotFoundError:
        pass
    try:
        # Reading the umask here avoids changing it while other threads create files.
        with open("/proc/self/status", encoding="ascii") as f:
            umask = next(int(line.split()[1], 8) for line in f if line.startswith("Umask:"))
    except (OSError, StopIteration, ValueError, IndexError):
        umask = os.umask(0o022)
        os.umask(umask)
    return 0o666 & ~umask


def atomic_json(path, data):
    """Replace on the same filesystem. Incomplete JSON does not corrupt the old file."""
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".save_", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        # mkstemp creates 0600. Keep the mode of the file being replaced, or the umask default.
        os.chmod(tmp, _file_mode(path))
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
