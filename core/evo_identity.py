# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Attach read-only source metadata to existing EVO lights.

Read only an explicit project-relative source or the single EVO file in model/.
Preserve source geometry, light values and project files. The source
world matrix records provenance only and does not replace the user's current
x/y/z/direction fields. Do not write names or source paths to logs or warnings.
"""
from collections import OrderedDict
from copy import deepcopy
from pathlib import Path
import re
import threading

from core.file_safety import internal_path


METADATA_FIELDS = (
    'source_id', 'source_guid', 'source_record', 'source_name',
    'product_id', 'product_guid', 'product_record', 'product_name', 'product_names', 'product_code',
    'product_representation_record', 'array_id', 'array_guid', 'array_record', 'array_name',
    'source_world_matrix',
)
_cache = OrderedDict()
_lock = threading.RLock()


def _candidate_identity(luminaire):
    if not isinstance(luminaire, dict):
        return None
    source = luminaire.get('source')
    # Do not infer EVO provenance from names belonging to other formats or explicitly
    # user-created records. Support legacy technical records with no source field.
    if source not in (None, '', 'evo/STEP'):
        return None
    identity = luminaire.get('source_id')
    if source == 'evo/STEP' and identity is not None:
        return str(identity)
    old = re.fullmatch(r'luminaire_(\d+)', str(luminaire.get('name', '')))
    return old[1] if old else None


def _source(project_dir, project):
    import_meta = project.get('importer')
    import_meta = import_meta if isinstance(import_meta, dict) else {}
    if 'source_evo' in project or 'source_evo' in import_meta:
        relative = project.get('source_evo', import_meta.get('source_evo'))
        if not isinstance(relative, str) or not relative.lower().endswith('.evo'):
            raise ValueError('source')
        path = Path(internal_path(project_dir, relative))
    else:
        folder = Path(internal_path(project_dir, 'model'))
        candidates = [p.name for p in folder.iterdir() if p.suffix.lower() == '.evo'] if folder.is_dir() else []
        if len(candidates) != 1:
            return None, ('Multiple local EVO sources exist. Identity metadata was not attached without selecting a source.'
                          if candidates else 'No local EVO source found. Source names may require reimporting.')
        path = Path(internal_path(project_dir, 'model/' + candidates[0]))
    if not path.is_file():
        return None, 'No local EVO source found. Existing light records were preserved.'
    return path, None


def _signature(path):
    stat = path.stat()
    return str(path.resolve()), stat.st_size, stat.st_mtime_ns


def _metadata(path):
    from core import engine as engine
    with _lock:
        key = _signature(path)
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
        records = engine.evo_luminaires(str(path))
        if _signature(path) != key:
            raise ValueError('source changed')
        result = {}
        for record in records:
            identity = record.get('source_id')
            if identity is not None:
                identity = str(identity)
                if identity in result:
                    raise ValueError('duplicate source identity')
                result[identity] = {field: deepcopy(record[field]) for field in METADATA_FIELDS if field in record}
        _cache[key] = result
        _cache.move_to_end(key)
        while len(_cache) > 8:
            _cache.popitem(last=False)
        return result


def enrich(project_dir, project, luminaires):
    """Return (independent light copy, general notes) without writing any files.

    Never overwrite existing metadata. Match source=evo/STEP records using
    source_id. Legacy records without a source match only the exact luminaire_N
    technical name. An explicit source error must not trigger a broader directory search.
    """
    copied = deepcopy(luminaires)
    if not isinstance(copied, list):
        return copied, []
    eligible = [(a, _candidate_identity(a)) for a in copied]
    eligible = [(a, identity) for a, identity in eligible if identity is not None]
    if not eligible:
        return copied, []
    try:
        path, note = _source(project_dir, project)
        if note:
            return copied, [note]
        metadata = _metadata(path)
    except Exception:
        return copied, ['EVO source identities could not be read safely. Existing light records were preserved.']
    missing = 0
    for luminaire, identity in eligible:
        source = metadata.get(identity)
        if source is None:
            missing += 1
            continue
        for field, value in source.items():
            if field not in luminaire:
                luminaire[field] = deepcopy(value)
    notes = ['Some lights had no matching EVO source identity. Their existing values were preserved.'] if missing else []
    return copied, notes
