# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Measured progress of the registered render job without starting or stopping processes.

The parallel engine writes JSON atomically. For serial rpict ``-t 5 -e <job-specific log>``,
read only the last complete report line. Report format source:
https://github.com/LBNL-ETA/Radiance/blob/master/src/rt/rpict.c (report).
Percentage is the fraction of completed tiles/pixels, not an estimate of time remaining.
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


_lock = threading.Lock()
_active = None
_workflow = None
_workflow_owner = ContextVar('radiance_workflow_owner', default=None)
_JSON_LIMIT = 65536
_LOG_LIMIT = 16384
_INTEGER_LIMIT = 2**31 - 1
_REPORT = re.compile(r'(?<![\w.+-])(?P<rays>\d+)\s+rays,[^\r\n]*?(?P<pct>[+-]?\d+(?:\.\d+)?)%\s+after\b[^\r\n]*\bhours\b')


@contextmanager
def workflow(operation_id):
    """Track the actual stages of one owner from preparation through output."""
    global _workflow
    now = time.monotonic()
    record = {'token': uuid.uuid4().hex, 'operation_id': operation_id, 'engine': 'radiance',
             'phase': 'preparation', 'step': 'bounds', 'label': 'Preparing scene bounds',
             't0': now, 'stage_t0': now, 'step_t0': now,
             'completed': None, 'total': None, 'unit': None, 'detail': None,
             'cache': {}}
    context = _workflow_owner.set(record['token'])
    with _lock:
        _workflow = record
    try:
        yield
    finally:
        with _lock:
            if _workflow is record:
                _workflow = None
        _workflow_owner.reset(context)


def step(code, label, *, stage='preparation', completed=None, total=None,
         unit=None, detail=None, cache=None):
    """The counter measures only local work. It is not converted to a percentage of the entire job."""
    if stage not in ('preparation', 'render', 'conversion'):
        raise ValueError('Unknown render stage')
    if (completed is not None or total is not None) and not (
            type(completed) is int and type(total) is int and 0 <= completed <= total):
        raise ValueError('Invalid progress counter')
    now = time.monotonic()
    with _lock:
        if _workflow is None or _workflow['token'] != _workflow_owner.get():
            return False
        if _workflow['phase'] != stage:
            _workflow['stage_t0'] = now
        if (_workflow['phase'], _workflow['step'], _workflow['detail']) != (stage, code, detail):
            _workflow['step_t0'] = now
        _workflow.update(stage=stage, step=code, label=label, completed=completed,
                     total=total, unit=unit, detail=detail)
        if cache:
            _workflow['cache'].update(cache)
        return True


def workflow_status(operation_id):
    """Return no record for another job or a finished owner without reading from disk."""
    now = time.monotonic()
    with _lock:
        if _workflow is None or not operation_id or _workflow['operation_id'] != operation_id:
            return None
        result = {k: v for k, v in _workflow.items()
                 if k not in ('token', 'operation_id', 't0', 'stage_t0', 'step_t0', 'cache')}
        result.update(elapsed_seconds=round(max(0, now - _workflow['t0']), 1),
                     stage_seconds=round(max(0, now - _workflow['stage_t0']), 1),
                     step_seconds=round(max(0, now - _workflow['step_t0']), 1),
                     overall_percent=None, eta_seconds=None, cache=dict(_workflow['cache']))
        return result


def _integer(value, lower, upper):
    return type(value) is int and lower <= value <= upper


def _identity(s):
    return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)


def _path_record(path):
    if path is None:
        return None
    path = os.path.abspath(os.fspath(path))
    try:
        s = os.stat(path, follow_symlinks=False)
        previous_state = _identity(s) if stat.S_ISREG(s.st_mode) else None
    except OSError:
        previous_state = None
    return (path, previous_state)


def start(backend, workers=1, total_tiles=None, status_path=None, log_path=None,
           ambient_path=None, memory_budget=None, worker_memory=None):
    """Call before starting the job. Paths refer only to files owned by the engine.

    A new record replaces the previous one. A late ``finish`` call from the previous
    job cannot delete the new record. Release the status lock while reading file metadata.
    """
    if backend not in ('rpict', 'rpiece'):
        raise ValueError('Unknown render progress backend')
    if not _integer(workers, 1, 1024):
        raise ValueError('Worker count must be an integer between 1 and 1024')
    if backend == 'rpict' and workers != 1:
        raise ValueError('Serial rpict progress belongs to one worker')
    if total_tiles is not None and not _integer(total_tiles, 1, _INTEGER_LIMIT):
        raise ValueError('Total tile count must be a positive integer')
    record = {'token': uuid.uuid4().hex, 'backend': backend, 'workers': workers,
             'total': total_tiles, 'start': time.time(),
             'monotonic': time.monotonic(),
             'json': _path_record(status_path), 'log': _path_record(log_path),
             'ambient': _path_record(ambient_path), 'last_activity': None, 'last_counter': None,
             'memory': {'budget_bytes': memory_budget if _integer(memory_budget, 1, 2**53-1) else None,
                        'worker_limit_bytes': worker_memory if _integer(worker_memory, 1, 2**53-1) else None}}
    global _active
    with _lock:
        _active = record
    return record['token']


def finish(token):
    """Only the owner that started the record can clear it. Files remain untouched."""
    global _active
    with _lock:
        if _active is not None and _active['token'] == token:
            _active = None
            return True
    return False


def _read(record, limit, tail=False):
    if record is None:
        return None
    path, old = record
    try:
        flags = os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_NOFOLLOW', 0)
        with os.fdopen(os.open(path, flags), 'rb') as f:
            s = os.fstat(f.fileno())
            if not stat.S_ISREG(s.st_mode) or _identity(s) == old:
                return None
            if not tail:
                return f.read(limit + 1) if s.st_size <= limit else None
            # If the same log grew, skip the previous job's lines. A new inode
            # or a truncated log starts new reports from the beginning.
            starts = old[2] if old and old[:2] == _identity(s)[:2] and s.st_size >= old[2] else 0
            offset = max(starts, s.st_size - limit)
            f.seek(offset)
            data = f.read(limit)
            if offset > starts:  # Discard the first line if the tail starts in its middle.
                data = data.partition(b'\n')[2]
            return data
    except (OSError, ValueError):
        return None


def _tile(data, record):
    try:
        d = json.loads(data)
    except (ValueError, TypeError, UnicodeError):
        return None
    if not isinstance(d, dict):
        return None
    done, total, workers = d.get('completed'), d.get('total'), d.get('workers')
    phase = d.get('phase')
    if not (_integer(total, 1, _INTEGER_LIMIT) and _integer(done, 0, total)
            and _integer(workers, 1, 1024) and workers == record['workers']
            and (record['total'] is None or total == record['total'])
            and phase in ('calculation', 'merging', 'done')):
        return None
    if phase in ('merging', 'done') and done != total:
        return None
    result = {'completed': done, 'total': total, 'percent': (10000 * done // total) / 100,
              'phase': phase, 'measurement': 'chunk'}
    distribution = _distribution(d, done, total, workers)
    if distribution:
        result.update(distribution)
    return result


def _distribution(data, done, total, workers):
    """Validate all new fields while preserving support for legacy percentage JSON records."""
    grid, tiles, processes = data.get('grid'), data.get('chunks'), data.get('processes')
    if not (isinstance(grid, dict) and isinstance(tiles, list) and isinstance(processes, list)
            and _integer(grid.get('x'), 1, 32) and _integer(grid.get('y'), 1, 32)
            and grid['x'] * grid['y'] == total and len(tiles) == total <= 1024
            and workers <= 16 and len(processes) <= workers):
        return None
    cleaned_processes, known_workers, pids = [], set(), set()
    for process in processes:
        if not isinstance(process, dict):
            return None
        worker, pid = process.get('worker'), process.get('pid')
        if not (_integer(worker, 1, workers) and _integer(pid, 1, _INTEGER_LIMIT)
                and worker not in known_workers and pid not in pids):
            return None
        item = {'worker': worker, 'pid': pid}
        if 'enabled' in process:
            if type(process['enabled']) is not bool:
                return None
            item['enabled'] = process['enabled']
        cleaned_processes.append(item); known_workers.add(worker); pids.add(pid)
    cleaned_tiles, coords, completed = [], set(), 0
    for tile in tiles:
        if not isinstance(tile, dict):
            return None
        x, y, phase, worker = tile.get('x'), tile.get('y'), tile.get('status'), tile.get('worker')
        if not (_integer(x, 0, grid['x'] - 1) and _integer(y, 0, grid['y'] - 1)
                and (x, y) not in coords and phase in ('queued', 'calculation', 'done')):
            return None
        if worker is not None and (not _integer(worker, 1, workers) or worker not in known_workers):
            return None
        if (phase == 'queued' and worker is not None) or (phase == 'calculation' and worker is None):
            return None
        # A sync append may appear before the begun line on stdout. The tile is then
        # known to be complete, but its worker remains null until identified.
        completed += phase == 'done'
        coords.add((x, y)); cleaned_tiles.append({'x': x, 'y': y, 'status': phase, 'worker': worker})
    if completed != done:
        return None
    return {'grid': {'x': grid['x'], 'y': grid['y']}, 'chunks': cleaned_tiles,
            'processes': cleaned_processes}


def _pixel(data):
    if not data:
        return None
    for line in reversed(data.decode('utf-8', errors='replace').splitlines(keepends=True)):
        if not line.endswith(('\n', '\r')):
            continue  # Do not interpret an incomplete report still being written as a percentage.
        match = _REPORT.search(line)
        if match:
            pct = float(match.group('pct'))
            if not math.isfinite(pct) or not 0 <= pct <= 100:
                return None
            ray_text = match.group('rays')
            # JavaScript may read the JSON. Omit counters above 2^53 that it cannot represent exactly.
            rays = int(ray_text) if len(ray_text) <= 16 else None
            if rays is not None and rays > 2**53 - 1:
                rays = None
            return {'percent': pct, 'measurement': 'pixel', 'ray_count': rays,
                    'ray_measurement_source': 'rpict_native_report',
                    'ray_count_description': 'Counter exceeds the safe integer limit' if rays is None else None}
    return None


def _file_sample(record):
    """Read only metadata for the job's file, without following symlinks."""
    if record is None:
        return None
    try:
        info = os.stat(record[0], follow_symlinks=False)
        return info if stat.S_ISREG(info.st_mode) else None
    except OSError:
        return None


def _activity(record, result):
    now = time.time()
    ambient = _file_sample(record['ambient'])
    original = record['ambient'][1] if record['ambient'] else None
    changed = ambient is not None and _identity(ambient) != original
    worker_sample = _file_sample(record['json'])
    workers = result.get('processes')
    active = (sum(p['enabled'] for p in workers) if workers is not None
              and all(type(p.get('enabled')) is bool for p in workers) else None)
    counter = (result.get('completed'), result.get('percent'), result.get('ray_count'))
    counter_sample = worker_sample if record['backend'] == 'rpiece' else _file_sample(record['log'])
    with _lock:
        if _active is not record:
            return None
        if changed and record['start'] <= ambient.st_mtime <= now:
            record['last_activity'] = max(record['last_activity'] or 0, ambient.st_mtime)
        if (counter != record['last_counter'] and any(v is not None and v > 0 for v in counter)
                and counter_sample is not None and record['start'] <= counter_sample.st_mtime <= now):
            record['last_activity'] = max(record['last_activity'] or 0, counter_sample.st_mtime)
        if result.get('measurement') is not None:
            record['last_counter'] = counter
        last = record['last_activity']
    return {'sample_time': now, 'last_activity_time': last,
            'active_workers': active, 'worker_sample_time': worker_sample.st_mtime if worker_sample else None,
            'ambient_bytes': ambient.st_size if ambient else None,
            'ambient_change_bytes': max(0, ambient.st_size - (original[2] if original else 0)) if ambient else None}


def status():
    """Read a bounded amount of file data outside the lock and discard results for a previous owner."""
    with _lock:
        record = _active
    if record is None:
        return None
    result = {'backend': record['backend'], 'workers': record['workers'],
             'percent': None, 'completed': None, 'total': record['total'],
             'measurement': None, 'ray_count': None, 'phase': 'calculation', 'start': record['start'],
             'elapsed_seconds': round(max(0, time.monotonic() - record['monotonic']), 1),
             'description': 'Tile/pixel progress, not an estimate of time remaining.'}
    if record['backend'] == 'rpiece':
        measurement = _tile(_read(record['json'], _JSON_LIMIT), record)
    else:
        measurement = _pixel(_read(record['log'], _LOG_LIMIT, tail=True))
    if measurement:
        result.update(measurement)
    result['activity'] = _activity(record, result)
    result['memory'] = dict(record['memory'])
    with _lock:
        return result if _active is record else None
