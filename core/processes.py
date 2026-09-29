# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Manage only child processes started by this program."""
import os
import signal
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from core import render_resources as render_resources

_lock = threading.RLock()
_active = set()
_render_lock = threading.RLock()
_generation = 0
_session = threading.local()
_sessions = {}
_process_owners = {}


class OperationCancelled(RuntimeError):
    """User cancellation, distinct from a calculation failure or successful output."""


def session_identity():
    return getattr(_session, 'identity', None)


def check_cancellation():
    """Prevent a cancelled request from starting its next stage."""
    with _lock:
        if (getattr(_session, 'generation', _generation) != _generation or
                _sessions.get(session_identity(), False)):
            raise OperationCancelled("Operation cancelled")


@contextmanager
def process_session():
    """Preparation, rendering and conversion share a cancellation identity."""
    new = not hasattr(_session, 'generation')
    if new:
        with _lock:
            _session.generation = _generation
            _session.identity = uuid.uuid4().hex
            _sessions[_session.identity] = False
    try:
        check_cancellation()
        yield session_identity()
        check_cancellation()
    finally:
        if new:
            with _lock:
                _sessions.pop(_session.identity, None)
            del _session.identity
            del _session.generation


def _terminate(p):
    try:
        if os.name == "nt":
            if p.poll() is not None:
                return
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"],
                           capture_output=True, timeout=5)
        else:
            # Descendants keeping stdout open may survive after the group leader
            # exits. Cancel the entire group that was started.
            os.killpg(p.pid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass


def run(cmd, *, shell=False, cwd=None, timeout=300, env=None, stdout_path=None,
             memory_limit=None):
    """communicate reads both pipes together. A timeout also terminates descendants."""
    file = None
    p = None
    try:
        with _lock:
            check_cancellation()
            budget = getattr(_session, 'render_budget', None)
            limit = min(budget, memory_limit) if budget and memory_limit else budget or memory_limit
            if limit:
                render_resources.check()
                command, use_shell = render_resources.command(cmd, limit, shell)
            else:
                command, use_shell = cmd, shell
            file = open(stdout_path, 'wb') if stdout_path else None
            p = subprocess.Popen(command, shell=use_shell, cwd=cwd, env=env,
                                 stdout=file if file else subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, start_new_session=(os.name != "nt"))
            _active.add(p)
            _process_owners[p] = session_identity()
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
                    check_cancellation()
                    render_resources.check()
        except subprocess.TimeoutExpired:
            _terminate(p)
            p.communicate()
            raise RuntimeError("Operation timed out")
        except BaseException:
            _terminate(p)
            p.communicate()
            raise
        check_cancellation()
        return subprocess.CompletedProcess(cmd, p.returncode, out or '', err)
    finally:
        if p is not None and os.name != "nt":
            # This API owns the synchronous command. Descendants that closed their
            # pipes may survive a normal leader exit. Terminate the group.
            _terminate(p)
        with _lock:
            _active.discard(p)
            _process_owners.pop(p, None)
        if file:
            file.close()


@contextmanager
def render_queue():
    with process_session():
        with _render_lock:
            check_cancellation()
            if hasattr(_session, 'render_budget'):
                yield
                return
            # Other CTLux/CLI processes belonging to the same user also use this
            # lock. Keep the lock file to avoid an inode race.
            with _machine_render_lock():
                _session.render_budget = render_resources.budget()
                try:
                    yield
                finally:
                    del _session.render_budget


@contextmanager
def _machine_render_lock():
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
            raise RuntimeError('Render queue lock is unsafe')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError('Another CTLux/Radiance calculation is running. Wait for it to finish.') from error
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
