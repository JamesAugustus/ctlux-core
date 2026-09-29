# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Radiance geometry cache preserving materials without changing the source OBJ.

obj2mesh does not apply obj2rad's default ``white`` material to unassigned
surfaces. We write this default explicitly in the temporary OBJ. Vertex/face/UV/normal
records stay identical to the source. The render scene must reference the returned file
using ``void mesh`` because materials are defined inside the mesh. Geometry without UVs
that exceeds RTM patch capacity is converted to a frozen OCT and uses ``void instance``.
"""
import hashlib
import json
import os
import shutil
from pathlib import Path
import tempfile
import threading
import uuid

from core.render_cache import has_dynamic_rad, file_digest, object_digest
from core import processes as processes
from core import render_progress as render_progress
from core import engine as engine


_VERSION = 3
_CHUNK_THRESHOLD = 8 << 20
_CHUNK_FACES = 50000
_lock = threading.RLock()


class MeshBuildError(RuntimeError):
    """Build failed. The more expensive polygon path is not a safe fallback."""


class MeshUVSupportError(RuntimeError):
    """Replacing RTM with polygons loses local texture coordinates."""


def _prepare_obj(source, target):
    """obj2rad: usemtl > first g name > white. Compute the source hash during streaming."""
    digest = hashlib.sha256()
    explicit_material = False
    active_material = b"white"
    total = os.path.getsize(source)
    read_count, reported = 0, 0
    def report_progress():
        processes.check_cancellation()
        render_progress.step('obj_copy', 'Preparing OBJ material records',
                            completed=min(read_count, total), total=total, unit='bytes',
                            detail=os.path.basename(source))
    report_progress()
    with open(source, "rb") as input_stream, open(target, "wb") as output_stream:
        output_stream.write(b"usemtl white\n")
        for raw in input_stream:
            read_count += len(raw)
            if read_count - reported >= 1024 * 1024:
                report_progress()
                reported = read_count
            digest.update(raw)
            output_stream.write(raw)
            line = raw.lstrip()
            # No need to parse millions of numeric records in a large OBJ.
            if line[:1] not in (b"g", b"u"):
                continue
            parts = line.split()
            if not parts or parts[0] not in (b"g", b"usemtl"):
                continue
            # Read Wavefront continuation lines together for group/material records and
            # preserve the original physical lines in the derived file.
            logical = line.rstrip(b"\r\n")
            last_line = raw
            while logical.endswith(b"\\"):
                continuation = next(input_stream, b"")
                if not continuation:
                    break
                read_count += len(continuation)
                digest.update(continuation)
                output_stream.write(continuation)
                last_line = continuation
                logical = logical[:-1] + b" " + continuation.rstrip(b"\r\n")
            parts = logical.split()
            to_write = None
            if parts[0] == b"usemtl":
                # An empty usemtl does not clear the previous selection in obj2rad.
                # obj2mesh does clear it, so write the previous selection again.
                if len(parts) > 1:
                    explicit_material = True
                    active_material = parts[1]
                else:
                    to_write = active_material
            elif not explicit_material:
                active_material = parts[1] if len(parts) > 1 else b"white"
                to_write = active_material
            if to_write is not None:
                if not last_line.endswith(b"\n"):
                    output_stream.write(b"\n")
                output_stream.write(b"usemtl " + to_write + b"\n")
    report_progress()
    return digest.hexdigest()


def _valid(rtm, record, signature, format="rtm"):
    """Reuse only a successfully completed file matching its record."""
    try:
        info = json.loads(record.read_text(encoding="utf-8"))
        for entry in info.get('chunks', []):
            part = (rtm.parent.parent / entry['path']).resolve()
            if (not part.is_relative_to(rtm.parent.resolve()) or not part.is_file()
                    or part.stat().st_size != entry['dimensions'] or file_digest(part) != entry['sha256']):
                return False
        return (info.get("signature") == signature
                and info.get("format") == format
                and info.get("dimensions") == rtm.stat().st_size
                and info.get("dimensions", 0) > 128
                and info.get("sha256") == file_digest(rtm))
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        return False


def _check_rtm(rtm, format="rtm"):
    """Do not cache empty or text output even if the process succeeds."""
    if not rtm.is_file() or rtm.stat().st_size <= 128:
        raise RuntimeError("obj2mesh produced an empty or incomplete mesh")
    with rtm.open("rb") as f:
        header = f.read(16384).split(b"\n\n", 1)[0]
    format_line = b"FORMAT=Radiance_octree" if format == "oct" else b"FORMAT=Radiance_tmesh"
    if not header.startswith(b"#?RADIANCE\n") or format_line not in header.splitlines():
        raise RuntimeError("obj2mesh output is not in Radiance mesh format")


def _has_uv(obj):
    """Do not silently use a polygon fallback for input containing UVs."""
    with open(obj, "rb") as f:
        return any(s.lstrip().split(None, 1)[:1] == [b"vt"] for s in f)


def _frozen_oct(project, obj_tmp, mat, target, temporary_files):
    """RTM capacity limit: preserve all source faces in a frozen OCT."""
    fd, name = tempfile.mkstemp(prefix=".mesh_", suffix=".rad", dir=project / "_cache")
    os.close(fd)
    rad = Path(name)
    temporary_files.append(rad)
    for command, output in ((["obj2rad", os.path.relpath(obj_tmp, project)], rad),
                         (["oconv", "-f", os.path.relpath(mat, project), os.path.relpath(rad, project)], target)):
        render_progress.step('mesh_' + command[0], 'Preparing frozen Radiance geometry (%s)' % command[0])
        result = processes.run(command, cwd=str(project), stdout_path=str(output), timeout=3600,
                                 env=engine.radiance_environment())
        if result.returncode:
            raise RuntimeError("Could not prepare frozen geometry (%s): %s"
                               % (command[0], (result.stderr or "unknown error")[-1500:]))



def _build_chunks(project, obj, mat, target):
    """Sequential, bounded RTM chunks preserving vertices, UVs, normals and materials.

    Skip only triangles with mathematically exact zero area. Apply no tolerance,
    simplification or merging of overlapping faces. The source remains unchanged.
    """
    from array import array
    import math
    folder = Path(tempfile.mkdtemp(prefix='mesh_parts_', dir=project / '_cache'))
    records = [[], [], []]
    xyz = array('d')
    faces = []
    material = b'white'
    counts = {'face': 0, 'zero_area': 0, 'chunk': 0}
    entries = []
    read_bytes = 0
    total_bytes = obj.stat().st_size
    succeeded = False

    def emit(batch):
        if not batch:
            return
        processes.check_cancellation()
        serial = len(entries)
        source = folder / ('%06d.obj' % serial)
        compiled = folder / ('%06d.rtm' % serial)
        maps = [{}, {}, {}]
        for corners, _ in batch:
            for corner in corners:
                for k, index in enumerate(corner):
                    if index is not None and index not in maps[k]:
                        maps[k][index] = len(maps[k]) + 1
        with source.open('wb') as stream:
            for k, mapping in enumerate(maps):
                for index in mapping:
                    stream.write(records[k][index] + b'\n')
            last_material = None
            for corners, selected in batch:
                if selected != last_material:
                    stream.write(b'usemtl ' + selected + b'\n')
                    last_material = selected
                values = []
                for corner in corners:
                    components = [str(maps[k][index]).encode() if index is not None else b''
                                  for k, index in enumerate(corner)]
                    while len(components) > 1 and not components[-1]:
                        components.pop()
                    values.append(b'/'.join(components))
                stream.write(b'f ' + b' '.join(values) + b'\n')
        render_progress.step('mesh_chunk', 'Compiling Radiance geometry in chunks',
                            completed=min(read_bytes, total_bytes), total=total_bytes,
                            unit='bytes', detail='%d chunks completed' % len(entries))
        try:
            result = processes.run(['obj2mesh', '-a', os.path.relpath(mat, project),
                                       os.path.relpath(source, project), os.path.relpath(compiled, project)],
                                      cwd=str(project), memory_limit=768 << 20, env=engine.radiance_environment())
            if result.returncode:
                reason = (result.stderr or result.stdout or 'unknown error')[-1500:]
                if len(batch) > 1 and any(token in reason.lower() for token in (
                        'out of memory', 'cannot allocate memory', 'too many patch triangles')):
                    compiled.unlink(missing_ok=True)
                    middle = len(batch) // 2
                    emit(batch[:middle])
                    emit(batch[middle:])
                    return
                raise MeshBuildError('Could not compile geometry chunk: ' + reason)
            _check_rtm(compiled)
            entries.append({'path': os.path.relpath(compiled, project),
                            'dimensions': compiled.stat().st_size, 'sha256': file_digest(compiled)})
        finally:
            source.unlink(missing_ok=True)

    try:
        with obj.open('rb') as stream:
            pending = b''
            for raw in stream:
                read_bytes += len(raw)
                line = raw.split(b'#', 1)[0].strip()
                pending += line[:-1] + b' ' if line.endswith(b'\\') else line
                if line.endswith(b'\\'):
                    continue
                line, pending = pending, b''
                parts = line.split()
                if not parts:
                    continue
                tag = parts[0]
                if tag in (b'v', b'vt', b'vn'):
                    k = (b'v', b'vt', b'vn').index(tag)
                    records[k].append(line)
                    if k == 0:
                        # Do not cull by area for homogeneous coordinates or special records.
                        coords = [float(x) for x in parts[1:4]] if len(parts) == 4 else [math.nan]*3
                        if len(coords) != 3:
                            raise MeshBuildError('Invalid OBJ vertex record')
                        xyz.extend(coords)
                elif tag == b'usemtl':
                    if len(parts) > 1:
                        material = parts[1]
                elif tag == b'f':
                    counts['face'] += 1
                    corners = []
                    for value in parts[1:]:
                        components = value.split(b'/')
                        if len(components) > 3:
                            raise MeshBuildError('Invalid OBJ vertex reference')
                        corner = []
                        for k in range(3):
                            component = components[k] if k < len(components) else b''
                            if not component:
                                if k == 0:
                                    raise MeshBuildError('OBJ face is missing a vertex')
                                corner.append(None)
                                continue
                            index = int(component)
                            resolved = index-1 if index > 0 else len(records[k])+index
                            if index == 0 or not 0 <= resolved < len(records[k]):
                                raise MeshBuildError('OBJ face reference is out of range')
                            corner.append(resolved)
                        corners.append(tuple(corner))
                    if len(corners) < 3:
                        raise MeshBuildError('OBJ face must have at least three vertices')
                    if len(corners) == 3:
                        a, b, c = (corner[0]*3 for corner in corners)
                        ux, uy, extension = (xyz[b+j]-xyz[a+j] for j in range(3))
                        vx, vy, vz = (xyz[c+j]-xyz[a+j] for j in range(3))
                        if (uy*vz-extension*vy, extension*vx-ux*vz, ux*vy-uy*vx) == (0, 0, 0):
                            # Do not discard a face solely due to floating-point underflow/rounding.
                            # Confirm zero area using the original decimal coordinates.
                            from fractions import Fraction
                            exact = [[Fraction(x.decode('ascii')) for x in records[0][corner[0]].split()[1:4]]
                                     for corner in corners]
                            eu = [exact[1][j]-exact[0][j] for j in range(3)]
                            house = [exact[2][j]-exact[0][j] for j in range(3)]
                            if (eu[1]*house[2]-eu[2]*house[1], eu[2]*house[0]-eu[0]*house[2], eu[0]*house[1]-eu[1]*house[0]) == (0, 0, 0):
                                counts['zero_area'] += 1
                                continue
                    faces.append((corners, material))
                    if len(faces) >= _CHUNK_FACES:
                        emit(faces)
                        faces.clear()
                if counts['face'] % 8192 == 0:
                    processes.check_cancellation()
            if pending:
                raise MeshBuildError('Incomplete OBJ line continuation')
        emit(faces)
        if not entries:
            raise MeshBuildError('No renderable faces found. Source preserved')
        records.clear()
        del xyz
        faces.clear()
        wrapper = folder / 'parts.rad'
        wrapper.write_text(''.join('void mesh part_%d\n1 %s\n0\n0\n' % (i, item['path'])
                                   for i, item in enumerate(entries)))
        result = processes.run(['oconv', '-f', '-n', str(max(8, len(entries)+1)), os.path.relpath(wrapper, project)],
                                  cwd=str(project), stdout_path=str(target), memory_limit=768 << 20,
                                  env=engine.radiance_environment())
        if result.returncode:
            raise MeshBuildError('Could not combine geometry chunks: ' + (result.stderr or '')[-1500:])
        _check_rtm(target, 'oct')
        counts['chunk'] = len(entries)
        succeeded = True
        return entries, counts, folder
    finally:
        if not succeeded:
            shutil.rmtree(folder)


def chunk_dependencies(project, rel):
    """Include external RTM chunks in scene validation and memory accounting."""
    root = Path(project).resolve()
    record = (root / rel).with_suffix('.json')
    data = json.loads(record.read_text())
    result = []
    for entry in data.get('chunks', []):
        path = (root / entry['path']).resolve()
        if not path.is_relative_to(root / '_cache'):
            raise MeshBuildError('Mesh chunk is outside the project cache')
        result.append(os.path.relpath(path, root))
    return result


def build(project_dir, obj_path, material_path):
    """Return the relative path of the content-hashed .rtm, or .oct at the RTM capacity limit.

    A build failure raises RuntimeError. The caller decides whether to use the obj2rad
    fallback. The lock prevents two requests in one process from interleaving
    cache writes. The completion record is installed atomically as the final step.
    """
    project = Path(project_dir).resolve()
    obj = Path(obj_path)
    mat = Path(material_path)
    obj = obj if obj.is_absolute() else project / obj
    mat = mat if mat.is_absolute() else project / mat
    cache = project / "_cache"
    cache.mkdir(parents=True, exist_ok=True)
    with processes.render_queue(), _lock:
        temporary_files = []
        part_folder = None
        published = False
        try:
            processes.check_cancellation()
            render_progress.step('mesh_signature', 'Verifying OBJ and material content hashes',
                                detail=os.path.relpath(obj, project))
            obj_digest, material_digest = file_digest(obj), file_digest(mat)
            tools = {name: shutil.which(name) for name in ('obj2mesh', 'obj2rad', 'oconv')}
            signature = object_digest({"version": _VERSION, "obj": obj_digest, "mat": material_digest,
                               "tools": {name: file_digest(path) if path else None
                                            for name, path in tools.items()}})
            if has_dynamic_rad([(str(mat), material_digest)]):
                signature = object_digest([signature, uuid.uuid4().hex])
            for format in ("rtm", "oct"):
                target = cache / ("mesh_" + signature + "." + format)
                record = target.with_suffix(".json")
                render_progress.step('mesh_cache', 'Verifying cached Radiance geometry',
                                    detail=os.path.relpath(obj, project))
                if _valid(target, record, signature, format):
                    render_progress.step('mesh_ready', 'Verified Radiance geometry loaded from cache',
                                        cache={'mesh': True})
                    return os.path.relpath(target, project)
            render_progress.step('obj_copy', 'Starting OBJ geometry preparation', cache={'mesh': False})
            for suffix in (".obj", ".rtm", ".json"):
                fd, name = tempfile.mkstemp(prefix=".mesh_", suffix=suffix, dir=cache)
                os.close(fd)
                temporary_files.append(Path(name))
            obj_tmp, rtm_tmp, record_tmp = temporary_files
            if _prepare_obj(obj, obj_tmp) != obj_digest:
                raise RuntimeError("Source changed during OBJ preparation. Try again")
            parts, chunk_counts = [], {}
            if obj.stat().st_size >= _CHUNK_THRESHOLD:
                parts, chunk_counts, part_folder = _build_chunks(project, obj_tmp, mat, rtm_tmp)
                format = 'oct'
            else:
                command = ["obj2mesh", "-a", os.path.relpath(mat, project),
                         os.path.relpath(obj_tmp, project), os.path.relpath(rtm_tmp, project)]
                render_progress.step('mesh_build', 'Compiling OBJ geometry (obj2mesh)',
                                    detail=os.path.relpath(obj, project))
                result = processes.run(command, cwd=str(project), env=engine.radiance_environment())
                format = "rtm"
                if result.returncode:
                    reason = (result.stderr or result.stdout or "unknown error")[-1500:]
                    if "too many patch triangles" not in reason:
                        raise RuntimeError("obj2mesh failed: " + reason)
                    if _has_uv(obj_tmp):
                        raise MeshUVSupportError("RTM patch limit exceeded. The OBJ contains texture coordinates. "
                                                "The polygon/OCT alternative was not used because it would lose UVs.")
                    _frozen_oct(project, obj_tmp, mat, rtm_tmp, temporary_files)
                    format = "oct"
            render_progress.step('mesh_validate', 'Verifying compiled Radiance geometry',
                                detail=os.path.relpath(obj, project))
            _check_rtm(rtm_tmp, format)
            if file_digest(obj) != obj_digest or file_digest(mat) != material_digest:
                raise RuntimeError("Source or material changed during mesh compilation. Try again")
            target = cache / ("mesh_" + signature + "." + format)
            record = target.with_suffix(".json")
            info = {"signature": signature, "format": format, "dimensions": rtm_tmp.stat().st_size,
                     "sha256": file_digest(rtm_tmp), "chunks": parts, "counts": chunk_counts}
            record_tmp.write_text(json.dumps(info, sort_keys=True) + "\n", encoding="utf-8")
            os.replace(rtm_tmp, target)
            os.replace(record_tmp, record)
            published = True
            return os.path.relpath(target, project)
        except OSError as e:
            raise RuntimeError("Could not prepare Radiance mesh: " + str(e)) from e
        finally:
            if part_folder is not None and not published:
                shutil.rmtree(part_folder)
            for temporary in temporary_files:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
