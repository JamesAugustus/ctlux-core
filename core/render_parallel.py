# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Local Radiance rpiece queue with a shared camera and ambient file.

Each job's supervisor starts in a separate process group through processes.run.
rpiece, rpict and rpiece's writer children remain in that group: cancellation or
timeout terminates the entire job. Neither a shell nor shared /tmp files are used.
Tile count measures computation progress, not time remaining or accuracy.
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

from core import processes as processes
from core import render_resources as render_resources


def _cpu_count():
    try:
        count = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        count = os.cpu_count() or 1
    for group in render_resources.cgroup_paths():
        try:
            quota, period = (group / 'cpu.max').read_text().split()
            if quota != 'max':
                q, p = int(quota), int(period)
                if q > 0 and p > 0:
                    count = min(count, max(1, q // p))
        except (OSError, ValueError):
            pass
    return count


def _available_memory():
    """The smaller of the OS and applicable cgroup limits, in bytes."""
    return render_resources.available_memory()


def _scene_size(oct_p):
    """Also count mesh/instance files referenced externally by the small main OCT.

    Preparation already verifies content integrity. Do not reread large files here because
    the manifest and stat information suffice, and each dependency is counted once.
    """
    octree = Path(oct_p).resolve()
    paths = {octree}
    record = Path(str(octree) + '.json')
    if record.is_file():
        if record.stat().st_size > 1 << 20:
            raise ValueError('Scene dependency record is too large')
        data = json.loads(record.read_text())
        root = octree.parent.parent
        for entry in data['derived']:
            path = (root / entry['path']).resolve()
            if not path.is_relative_to(root):
                raise ValueError('Scene dependency is outside the project')
            paths.add(path)
    return sum(p.stat().st_size for p in paths)


def _view_args(args):
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
                raise ValueError('Missing camera argument: ' + arg)
            output.extend(args[i:i + count + 1])
            i += count
        i += 1
    return output


def _parallel_args_safe(args):
    # Prevent unknown options from changing rpiece's -X/-F/-o contract.
    # Pass preset quality option values through unchanged.
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


def _divisor_grid(width, height, workers):
    """Use exact divisors of both axes to prevent rpiece rounding."""
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


def plan_render(W, H, quality, oct_p, *, args=(), cwd=None, env=None):
    """Resource limits and native pixel dimensions. Use serial rpict if unsuitable.

    CTLUX_RENDER_WORKERS=1 forces serial execution.
    Requests for 2..16 workers remain subject to CPU and memory limits. File sizes do not
    fully determine texture/instance RAM use, so the worker count is an estimate.
    Native processes also receive an address space limit.
    """
    from core import engine as engine
    env = engine.radiance_environment(env)
    result = {'engine': 'rpict', 'workers': 1, 'xdiv': 1, 'ydiv': 1,
              'total_tiles': None, 'width': int(W), 'height': int(H), 'reason': ''}
    budget = max(64 << 20, int(_available_memory() * .5))
    result.update(memory_budget=budget, worker_memory=budget)
    def serial(reason):
        result['reason'] = reason
        return result
    if os.name == 'nt':
        return serial('Serial Radiance path for Windows')
    try:
        requested = int(env.get('CTLUX_RENDER_WORKERS', '0'))
    except ValueError:
        requested = 0
    if requested == 1:
        return serial('CTLUX_RENDER_WORKERS=1')
    # Direct lighting previews can also be expensive in scenes with many sources.
    # With enough pixels, split the job while preserving quality settings.
    if int(W) * int(H) < 65536:
        return serial('Small image')
    rpiece = shutil.which('rpiece', path=env.get('PATH'))
    vwrays = shutil.which('vwrays', path=env.get('PATH'))
    if not rpiece or not vwrays:
        return serial('rpiece/vwrays not found')
    rpiece = str(Path(rpiece).resolve())
    # rpiece starts rpict through PATH. Resolve the real distribution of symlink
    # launchers. Do not silently continue with a child from another distribution.
    sibling = Path(rpiece).parent / 'rpict'
    rpict = str(sibling) if sibling.is_file() and os.access(sibling, os.X_OK) else shutil.which('rpict', path=env.get('PATH'))
    if not rpict or Path(rpict).resolve().parent != Path(rpiece).parent:
        return serial('rpiece and rpict are not from the same Radiance distribution')
    # Persistent rpict processes, multiple images and user output redirection
    # must not interfere with rpiece's synchronization contract.
    if not _parallel_args_safe(args):
        return serial('A custom rpict option requires serial execution')
    cpus = _cpu_count()
    cpu_limit = max(1, cpus - 2)
    workers = min(max(2, min(16, requested)) if requested > 1 else 6, cpu_limit)
    try:
        scene_bytes = _scene_size(oct_p)
    except (OSError, ValueError, KeyError, TypeError):
        return serial('Could not read scene dependencies. Using one resource-limited worker')
    result['scene_bytes'] = scene_bytes
    # Include the main OCT and all referenced geometry. Allow at least 128 MiB per process.
    # Native allocations beyond the estimate fail without exhausting the machine.
    memory_limit = max(1, budget // max(128 << 20, scene_bytes * 2))
    workers = min(workers, memory_limit)
    if workers < 2:
        return serial('CPU/memory budget supports one worker')
    try:
        view = _view_args(list(args))
        native = processes.run([vwrays, '-d', *view, '-x', str(int(W)), '-y', str(int(H))],
                                  cwd=cwd, env=env, timeout=15)
    except (OSError, ValueError):
        return serial('Could not read native camera dimensions')
    match = re.search(r'-x\s+(\d+)\s+-y\s+(\d+)', native.stdout or '')
    if native.returncode or not match:
        return serial('Could not read native camera dimensions')
    width, height = map(int, match.groups())
    grid = _divisor_grid(width, height, workers)
    if not grid:
        return serial('No tile grid preserving pixel dimensions was found')
    x, y = grid
    result.update(engine='rpiece', workers=workers, xdiv=x, ydiv=y,
                  total_tiles=x * y, width=width, height=height,
                  worker_memory=budget // workers,
                  rpiece=rpiece, radiance_bin=str(Path(rpiece).parent), reason='Shared ambient file and local tile queue')
    return result


def _atomic_json(path, data):
    path = Path(path)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(',', ':')), encoding='utf-8')
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def run(plan, rpict_args, octree, cwd, out_hdr, ambient, job_dir, *, timeout=3600):
    """Attach the parallel supervisor to its owner in processes without publishing the HDR."""
    if plan.get('engine') != 'rpiece':
        raise ValueError('This runner accepts only an rpiece plan')
    job_dir = Path(job_dir).resolve()
    job_dir.mkdir(parents=True, exist_ok=True)
    job = {'version': 1, 'plan': plan, 'args': list(rpict_args),
           'octree': os.path.abspath(octree), 'cwd': os.path.abspath(cwd),
           'output': os.path.abspath(out_hdr), 'ambient': os.path.abspath(ambient) if ambient else None,
           'directory': str(job_dir)}
    path = job_dir / 'job.json'
    _atomic_json(path, job)
    from core import engine as engine
    env = engine.radiance_environment()
    env['TMPDIR'] = str(job_dir)
    # Resolve rpiece's rpict child from the same Radiance distribution.
    env['PATH'] = str(Path(plan['rpiece']).resolve().parent) + os.pathsep + env.get('PATH', '')
    return processes.run([sys.executable, str(Path(__file__).resolve()), str(path)],
                            cwd=cwd, env=env, timeout=timeout,
                            memory_limit=plan.get('worker_memory'))


def _sync_completed(path, xdiv, ydiv):
    """The first two lines contain the grid and last assigned task. Only appended pairs are completed."""
    try:
        import fcntl
        with open(path, 'rb') as stream:
            # Native rpiece uses F_SETLKW. Linux flock locks do not conflict with it.
            # lockf shares the same POSIX record lock.
            fcntl.lockf(stream, fcntl.LOCK_SH)
            data = stream.read(1024 * 1024)
            fcntl.lockf(stream, fcntl.LOCK_UN)
    except FileNotFoundError:
        return set()
    lines = data.splitlines()
    if data and not data.endswith(b'\n'):
        lines.pop()  # An incomplete final append does not count as a completed tile.
    if len(lines) < 3:
        return set()
    try:
        if tuple(map(int, lines[0].split())) != (xdiv, ydiv):
            raise RuntimeError('Tile synchronization grid mismatch')
        done = set()
        for line in lines[3:]:
            if not line.strip():
                continue
            x, y = map(int, line.split())
            if not (0 <= x < xdiv and 0 <= y < ydiv):
                raise RuntimeError('Tile synchronization index is out of range')
            done.add((x, y))
        return done
    except ValueError as error:
        raise RuntimeError('Invalid tile synchronization file') from error


def _validate_hdr(path, width, height):
    """rpiece's uncompressed RGBE body alone does not prove completion."""
    with open(path, 'rb') as stream:
        header = bytearray()
        while len(header) < 65536:
            line = stream.readline(4096)
            if not line:
                raise RuntimeError('Tile HDR header is missing')
            header.extend(line)
            if line == b'\n':
                break
        else:
            raise RuntimeError('Tile HDR header is too large')
        if not header.startswith((b'#?RADIANCE\n', b'#?RGBE\n')) or b'FORMAT=32-bit_rle_rgbe' not in header:
            raise RuntimeError('Invalid tile HDR format')
        resolution = stream.readline(256)
        match = re.fullmatch(rb'-Y\s+(\d+)\s+\+X\s+(\d+)\s*\n', resolution)
        if not match or tuple(map(int, match.groups())) != (height, width):
            raise RuntimeError('Tile HDR pixel dimensions do not match the native camera')
        if os.fstat(stream.fileno()).st_size - stream.tell() != width * height * 4:
            raise RuntimeError('Tile HDR pixel body is incomplete')


def _supervise(job):
    """Run only through processes, as a new group leader."""
    if os.name == 'nt' or os.getpgrp() != os.getpid():
        raise RuntimeError('Parallel supervisor must run in a separate process group')
    directory = Path(job['directory']).resolve()
    plan = job['plan']
    workers = int(plan['workers'])
    xdiv, ydiv = int(plan['xdiv']), int(plan['ydiv'])
    width, height = int(plan['width']), int(plan['height'])
    if not (2 <= workers <= 16 and 1 <= xdiv <= 32 and 1 <= ydiv <= 32 and
            1 <= width <= 16384 and 1 <= height <= 16384 and width % xdiv == 0 and height % ydiv == 0):
        raise ValueError('Invalid parallel job plan')
    if job.get('version') != 1 or not directory.is_dir():
        raise ValueError('Invalid parallel job file')
    sync = directory / 'pieces.sync'
    if sync.exists() or Path(job['output']).exists():
        raise RuntimeError('A parallel job may write only to new temporary files')
    status = {'completed': 0, 'started': 0, 'total': xdiv * ydiv,
              'workers': workers, 'phase': 'calculation'}
    processes, logs, buffers, started = [], [], {}, set()
    owners, pipe_workers, completed = {}, {}, set()
    last_codes = None
    last_publish = 0
    def publish():
        nonlocal last_codes, last_publish
        last_publish = time.monotonic()
        # Coordinates use native rpiece ordering: (0,0) is the bottom-left tile.
        # Worker IDs start at 1. Each PID belongs to an rpiece started by this job.
        status['grid'] = {'x': xdiv, 'y': ydiv}
        last_codes = tuple(p.poll() for p in processes)
        status['processes'] = [{'worker': i + 1, 'pid': p.pid, 'enabled': last_codes[i] is None}
                                for i, p in enumerate(processes)]
        status['chunks'] = [
            {'x': x, 'y': y, 'status': 'done' if (x, y) in completed else 'calculation' if (x, y) in started else 'queued',
             'worker': owners.get((x, y))}
            for y in range(ydiv) for x in range(xdiv)]
        _atomic_json(directory / 'progress.json', status)
    publish()
    args = [plan['rpiece'], '-v', *job['args']]
    if job['ambient']:
        # Native rpict option validation rejects non-ASCII -af text.
        # The parent project path may contain Unicode. Generated cache names are
        # ASCII. A path relative to cwd references the same shared file.
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
                    raise RuntimeError('Unexpected tile log line')
                for line in lines:
                    event = re.fullmatch(rb'\s*(\d+)\s+(\d+)\s+(begun|done)\s*', line)
                    if event:
                        tile = tuple(map(int, event.groups()[:2]))
                        if not (0 <= tile[0] < xdiv and 0 <= tile[1] < ydiv):
                            raise RuntimeError('Tile log index is out of range')
                        started.add(tile)
                        owners[tile] = pipe_workers[key.fileobj]
                        status['started'] = len(started)
                        completed = _sync_completed(sync, xdiv, ydiv)
                        status['completed'] = len(completed)
                        publish()
            codes = tuple(p.poll() for p in processes)
            if any(code not in (None, 0) for code in codes):
                raise RuntimeError('Radiance tile worker failed')
            if codes != last_codes or time.monotonic() - last_publish >= 1:
                # Do not report a completed worker as active while another tile takes longer.
                publish()
            completed = _sync_completed(sync, xdiv, ydiv)
            done = len(completed)
            if done != status['completed']:
                status['completed'] = done
                publish()
        for process in processes:
            if process.wait() != 0:
                raise RuntimeError('Radiance tile worker failed')
        done = completed = _sync_completed(sync, xdiv, ydiv)
        if len(done) != xdiv * ydiv:
            raise RuntimeError('Radiance did not complete all tiles: %d/%d' % (len(done), xdiv * ydiv))
        status.update(completed=len(done), stage='merging')
        publish()
        _validate_hdr(job['output'], width, height)
        status['phase'] = 'done'
        publish()
    except BaseException as error:
        try:
            status['error'] = str(error)
            try:
                publish()
            except OSError:
                pass  # Clean up even if the disk is full.
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
                # The group belongs to this supervisor and contains neither the main program
                # nor other rendering processes. A report file error cannot bypass cleanup.
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
            raise ValueError('Exactly one local job JSON file is required')
        path = Path(sys.argv[1]).resolve()
        job = json.loads(path.read_text(encoding='utf-8'))
        if Path(job['directory']).resolve() != path.parent:
            raise ValueError('The job file must be in its own temporary directory')
        _supervise(job)
    except BaseException as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
