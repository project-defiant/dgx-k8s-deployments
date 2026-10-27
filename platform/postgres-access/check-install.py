#!/usr/bin/python3
"""Fail closed before adopting an existing managed credential installation."""
import json
from pathlib import Path
import stat
import sys

MARKER = 'TheDefiant postgres-service.sh'


def check(base, state, uid, environment, gid, root_uid=0):
    for directory in (base, state):
        for path in (directory, *directory.parents):
            if path == directory and not path.exists() and not path.is_symlink():
                continue
            info = path.lstat()
            if path.is_symlink() or info.st_uid != root_uid or info.st_mode & 0o022:
                raise ValueError('Credential directories and parents must be trusted root-owned paths')
    if not base.exists() and not state.exists():
        return
    if not base.is_dir() or not state.is_dir():
        raise ValueError('Incomplete installation requires explicit review; do not remove credentials blindly')
    if {p.name for p in base.iterdir()} != {'config.json', 'refresh-credentials.py'}:
        raise ValueError('Unexpected credential configuration files require review')
    if {p.name for p in state.iterdir()} - {'kubeconfig'}:
        raise ValueError('Unexpected credential state files require review')
    for path, owner, mode in [(base / 'config.json', root_uid, 0o600),
                              (base / 'refresh-credentials.py', root_uid, 0o700),
                              (state / 'kubeconfig', root_uid, 0o640)]:
        if not path.exists() and path.name == 'kubeconfig' and not path.is_symlink():
            continue
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != owner or stat.S_IMODE(info.st_mode) != mode
                or (path.name == 'kubeconfig' and info.st_gid != gid)):
            raise ValueError('Unexpected credential file ownership, type or permissions')
    config = json.loads((base / 'config.json').read_text())
    if config.get('managed') != MARKER or '# Managed by ' + MARKER not in (base / 'refresh-credentials.py').read_text().splitlines()[:3]:
        raise ValueError('Refusing to adopt an unmanaged credential installation')
    if config.get('uid') != uid or config.get('environment') != environment or config.get('gid') != gid:
        raise ValueError('Identity/environment migration requires explicit review: stop services/timer, revoke old RoleBinding, archive protected state, then reinstall')


if __name__ == '__main__':
    try:
        check(Path('/etc/spark-postgres-access'), Path('/var/lib/spark-postgres-access'), int(sys.argv[1]), 'dev', int(sys.argv[2]))
    except (OSError, ValueError, KeyError) as error:
        raise SystemExit(str(error)) from None
