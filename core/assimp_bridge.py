# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Convert Assimp output to validated OBJ. A failed operation leaves the existing target untouched."""
from array import array
import hashlib
import math
import os
from pathlib import Path, PureWindowsPath
import shlex
import shutil
import tempfile
import threading
import warnings

from core import processes as processes


_ROOT = Path(__file__).resolve().parent.parent
_DELIVERY_LOCK = threading.Lock()
_MAP = {'bump', 'disp', 'decal', 'refl', 'norm'}
_SINGLE_OPTIONS = {'-blendu', '-blendv', '-boost', '-bm', '-clamp', '-texres',
        '-imfchan', '-type', '-colorspace'}


def _words(s):
    lexer = shlex.shlex(s, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ''
    lexer.escape = ''  # Preserve backslashes in Windows texture paths.
    return list(lexer)


def _local_path(base, name, root):
    """Reject absolute, foreign-machine or out-of-root paths before stat/read. Do not follow symlinks."""
    if not name or Path(name).is_absolute() or PureWindowsPath(name).drive:
        return None
    candidate = Path(os.path.abspath(base / name.replace('\\', '/')))
    if not candidate.is_relative_to(root):
        return None
    path = root
    for chunk in candidate.relative_to(root).parts:
        path = path / chunk
        if path.is_symlink():
            return None
    return candidate


def _logical_lines(fp):
    for raw in fp:
        s = raw.rstrip('\r\n')
        while s.endswith('\\'):
            continuation = next(fp, None)
            if continuation is None:
                raise RuntimeError('OBJ/MTL line continuation is incomplete')
            s = s[:-1] + ' ' + continuation.rstrip('\r\n')
        yield s


def _index(s, quantity):
    try:
        n = int(s)
    except ValueError as exc:
        raise RuntimeError('OBJ face index is not a number') from exc
    i = n-1 if n > 0 else quantity+n
    if n == 0 or not 0 <= i < quantity:
        raise RuntimeError('OBJ face index is out of range')
    return i


def _face_has_area(v, ids):
    """Detect collinear faces without a threshold, preserving small nondegenerate triangles."""
    a = v[3*ids[0]:3*ids[0]+3]
    edge = None
    for i in ids[1:]:
        e = [v[3*i+j]-a[j] for j in range(3)]
        length = max(map(abs, e))
        if not math.isfinite(length):
            raise RuntimeError('OBJ coordinate difference is not finite')
        if not length:
            continue
        e = [x/length for x in e]
        if edge is None:
            edge = e
        elif any(edge[j]*e[(j+1) % 3] != edge[(j+1) % 3]*e[j] for j in range(3)):
            return True
    return False


def _validate_obj(obj):
    """Read one record at a time with a compact double position table. Resolve negative indices at that point."""
    v = array('d')
    uv = normal = face = 0
    libraries, used = [], set()
    with obj.open(encoding='utf-8', errors='surrogateescape') as fp:
        for no, line in enumerate(_logical_lines(fp), 1):
            if no % 4096 == 0:
                processes.check_cancellation()
            clean = line.split('#', 1)[0].strip()
            p = clean.split()
            if not p:
                continue
            if p[0] in ('v', 'vt', 'vn'):
                try:
                    numbers = [float(x) for x in p[1:]]
                except ValueError as exc:
                    raise RuntimeError('OBJ coordinate is not a number') from exc
                valid = (len(numbers) in (3, 4, 6, 7) if p[0] == 'v'
                         else 1 <= len(numbers) <= 3 if p[0] == 'vt'
                         else len(numbers) == 3)
                if not valid or not all(map(math.isfinite, numbers)):
                    raise RuntimeError('OBJ coordinate is missing or not finite')
                if p[0] == 'v':
                    if len(numbers) == 4 and numbers[3] != 1:
                        raise RuntimeError('OBJ homogeneous positions are not supported by this bridge')
                    v.extend(numbers[:3])
                elif p[0] == 'vt':
                    uv += 1
                else:
                    normal += 1
            elif p[0] == 'f':
                if len(p) < 4:
                    raise RuntimeError('OBJ face must contain at least three vertices')
                ids = []
                empty_uv = all(token.count('/') == 1 and token.endswith('/') for token in p[1:])
                for token in p[1:]:
                    q = token.split('/')
                    if len(q) > 3 or not q[0] or (len(q) > 1 and not q[-1] and not empty_uv):
                        raise RuntimeError('OBJ face vertex is malformed')
                    ids.append(_index(q[0], len(v)//3))
                    if len(q) > 1 and q[1]:
                        _index(q[1], uv)
                    if len(q) == 3:
                        _index(q[2], normal)
                if len(set(ids)) != len(ids) or not _face_has_area(v, ids):
                    raise RuntimeError('OBJ face has repeated vertices or zero area')
                face += 1
            elif p[0] == 'mtllib':
                names = _words(clean[len('mtllib'):].strip())
                if not names:
                    raise RuntimeError('OBJ material reference is empty')
                libraries.extend(names)
            elif p[0] == 'usemtl':
                if len(p) < 2:
                    raise RuntimeError('OBJ material name is empty')
                used.add(' '.join(p[1:]))
    if not face:
        raise RuntimeError('Assimp output contains no valid faces')
    return list(dict.fromkeys(libraries)), used


def _copy_digest(source, target):
    h = hashlib.sha256()
    with source.open('rb') as input_stream, target.open('wb') as output_stream:
        while chunk := input_stream.read(1024*1024):
            processes.check_cancellation()
            h.update(chunk)
            output_stream.write(chunk)
    return h.hexdigest()


def _parse_map(s):
    """Preserve known MTL options. Do not guess ambiguous syntax."""
    p = _words(s)
    i = 1
    while i < len(p) and p[i].startswith('-'):
        option = p[i]
        i += 1
        if option in ('-s', '-o', '-t'):
            first = i
            while i < len(p) and i-first < 3:
                try:
                    if not math.isfinite(float(p[i])):
                        break
                except ValueError:
                    break
                i += 1
            if i == first:
                return None
        elif option in _SINGLE_OPTIONS or option == '-mm':
            i += 2 if option == '-mm' else 1
        else:
            return None
    if i >= len(p) or any(any(c.isspace() for c in x) for x in p[:i]):
        return None
    return ' '.join(p[:i]), ' '.join(p[i:])


def _prepare_mtl(source, target, assets, source_directory):
    names, warning = set(), 0
    with source.open(encoding='utf-8', errors='surrogateescape') as input_stream, target.open(
            'w', encoding='utf-8', errors='surrogateescape', newline='\n') as output_stream:
        for no, s in enumerate(_logical_lines(input_stream), 1):
            if no % 4096 == 0:
                processes.check_cancellation()
            p = s.split()
            if p and p[0] == 'newmtl':
                if len(p) < 2:
                    raise RuntimeError('MTL material name is empty')
                names.add(' '.join(p[1:]))
            if p and (p[0].startswith('map_') or p[0] in _MAP):
                try:
                    resolved = _parse_map(s)
                except ValueError:
                    resolved = None
                texture = None
                if resolved:
                    for base in (source.parent, source_directory):
                        candidate = _local_path(base, resolved[1], _ROOT)
                        if candidate is not None and candidate.is_file():
                            texture = candidate
                            break
                if texture is not None:
                    temporary = assets / 'texture.tmp'
                    signature = _copy_digest(texture, temporary)
                    suffix = texture.suffix.lower()
                    if len(suffix) > 10 or not suffix[1:].isalnum():
                        suffix = '.bin'
                    new = 'texture_' + signature + suffix
                    os.replace(temporary, assets / new)
                    s = resolved[0] + ' ' + new
                else:
                    warning += 1
                    output_stream.write('# assimp_bridge: unresolved texture, original reference preserved\n')
            output_stream.write(s + '\n')
    return names, warning


def _asset_signature(directory):
    h = hashlib.sha256()
    for p in sorted(directory.iterdir()):
        if p.is_symlink() or not p.is_file():
            raise RuntimeError('Assimp asset directory contains an unexpected entry')
        h.update(p.name.encode('utf-8') + b'\0')
        h.update(str(p.stat().st_size).encode('ascii') + b'\0')
        with p.open('rb') as fp:
            while chunk := fp.read(1024*1024):
                processes.check_cancellation()
                h.update(chunk)
    return h.hexdigest()


def convert(source, target, timeout=120):
    """Deliver OBJ and content-addressed MTL/textures. Replace the target only on success.

    Unresolved textures retain an MTL comment and emit a RuntimeWarning.
    Absolute or foreign-machine texture paths are not read.
    The original MTL reference is preserved.
    """
    source = Path(source).absolute()
    target = Path(target).absolute()
    if not source.is_file():
        raise RuntimeError('Assimp source file not found')
    if source.resolve() == target.resolve() or (target.exists() and os.path.samefile(source, target)):
        raise ValueError('Assimp source and target cannot be the same file')
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('Assimp timeout must be positive and finite')
    tool = shutil.which('assimp')
    if not tool:
        raise RuntimeError('assimp is not installed')
    target.parent.mkdir(parents=True, exist_ok=True)
    with processes.process_session(), tempfile.TemporaryDirectory(prefix='.assimp_', dir=target.parent) as td:
        temporary = Path(td)
        obj = temporary / 'model.obj'
        r = processes.run([tool, 'export', str(source), str(obj), '-fobj'],
                              cwd=str(temporary), timeout=timeout)
        if r.returncode:
            raise RuntimeError('Assimp conversion failed: ' + (r.stderr or r.stdout or '')[-800:])
        if obj.is_symlink() or not obj.is_file():
            raise RuntimeError('Assimp did not produce new OBJ output')
        libraries, used = _validate_obj(obj)
        assets = temporary / 'assets'
        assets.mkdir()
        mapping, definitions, warning = {}, set(), 0
        for i, name in enumerate(libraries):
            mtl = _local_path(temporary, name, temporary)
            if mtl is None or not mtl.is_file():
                raise RuntimeError('Assimp MTL output is missing or outside the temporary directory')
            new = 'material_%d.mtl' % i
            names, quantity = _prepare_mtl(mtl, assets / new, assets, source.parent)
            definitions.update(names)
            warning += quantity
            mapping[name] = new
        if used - definitions:
            raise RuntimeError('OBJ uses an undefined MTL material')
        signature = _asset_signature(assets)
        asset = target.parent / ('assimp_assets_' + signature)
        delivery = temporary / 'delivery.obj'
        with obj.open(encoding='utf-8', errors='surrogateescape') as input_stream, delivery.open(
                'w', encoding='utf-8', errors='surrogateescape', newline='\n') as output_stream:
            for s in _logical_lines(input_stream):
                if s.lstrip().split(None, 1)[:1] == ['mtllib']:
                    for name in _words(s.split('#', 1)[0].split(None, 1)[1]):
                        output_stream.write('mtllib ' + asset.name + '/' + mapping[name] + '\n')
                else:
                    output_stream.write(s + '\n')
            output_stream.flush()
            os.fsync(output_stream.fileno())
        if warning:
            # Callers treating warnings as errors must also stop before delivery.
            warnings.warn('%d texture references could not be resolved. Original MTL lines were preserved' % warning,
                          RuntimeWarning, stacklevel=2)
        with _DELIVERY_LOCK:
            processes.check_cancellation()
            if libraries:
                if asset.is_symlink():
                    raise RuntimeError('Assimp asset target cannot be a symbolic link')
                if asset.exists():
                    if not asset.is_dir() or _asset_signature(asset) != signature:
                        raise RuntimeError('Assimp asset target content differs from the expected content')
                else:
                    os.rename(assets, asset)
            processes.check_cancellation()
            os.replace(delivery, target)
    return str(target)
