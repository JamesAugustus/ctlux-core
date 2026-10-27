# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (C) 2026 James Augustus
"""Radiance için RAM payı, Linux'ta child process'e kernel'in uyguladığı address space limiti."""
import os
from pathlib import Path
import re
import sys


class BellekYetersiz(RuntimeError):
    pass


def cgroup_yollari():
    """Process'in cgroup v2 üyeliği ve köke kadar üst limitler."""
    root = Path('/sys/fs/cgroup')
    groups = {root}
    try:
        entry = next(s[3:] for s in Path('/proc/self/cgroup').read_text().splitlines()
                     if s.startswith('0::'))
        group = (root / entry.lstrip('/')).resolve()
        if group.is_relative_to(root):
            groups.update(p for p in (group, *group.parents) if p.is_relative_to(root))
    except (OSError, StopIteration):
        pass
    return groups


def bos_bellek():
    limits = []
    try:
        text = Path('/proc/meminfo').read_text()
        limits.append(int(re.search(r'^MemAvailable:\s+(\d+)', text, re.M)[1]) * 1024)
    except (OSError, TypeError, ValueError):
        try:
            limits.append(os.sysconf('SC_AVPHYS_PAGES') * os.sysconf('SC_PAGE_SIZE'))
        except (OSError, ValueError, AttributeError):
            pass
    groups = cgroup_yollari()
    for group in groups:
        try:
            maximum = (group / 'memory.max').read_text().strip()
            if maximum != 'max':
                limits.append(max(0, int(maximum) - int((group / 'memory.current').read_text())))
        except (OSError, ValueError):
            pass
    return min(limits) if limits else 512 << 20


def butce():
    available = bos_bellek()
    if available < 512 << 20:
        raise BellekYetersiz('Render başlamadı: kullanılabilir bellek yetersiz. Proje kaydı korundu.')
    return available // 2


def kontrol():
    if bos_bellek() < 512 << 20:
        raise BellekYetersiz('Render durduruldu: kullanılabilir bellek azaldı. Proje kaydı korundu.')


def komut(cmd, limit, shell=False):
    if not sys.platform.startswith('linux'):
        return cmd, shell
    if type(limit) is not int or limit < 64 << 20:
        raise BellekYetersiz('Render için ayrılan bellek payı yetersiz.')
    args = ['/bin/sh', '-c', cmd] if shell else list(cmd)
    # preexec_fn kullanmıyoruz, çağıran process thread'li olabilir. Limit yeni child'da,
    # native program ve onun alt process'leri başlamadan önce uygulanır.
    return [sys.executable, str(Path(__file__).resolve()), str(limit), *args], False


if __name__ == '__main__':
    import resource
    try:
        limit = int(sys.argv[1])
        soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        if hard != resource.RLIM_INFINITY:
            limit = min(limit, hard)
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        os.nice(5)
        os.execvp(sys.argv[2], sys.argv[2:])
    except (OSError, ValueError) as error:
        print('Radiance kaynak sınırı: ' + str(error), file=sys.stderr)
        sys.exit(1)
