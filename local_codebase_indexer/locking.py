"""Bounded, process-safe coordination for a local index. No external lock service."""
from __future__ import annotations

import errno
import math
import os
import time
from contextlib import contextmanager


class IndexBusyError(ValueError):
    """A retryable contention error, suitable for a CLI/MCP response."""


def timeout_seconds(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, default))
    except ValueError as exc:
        raise ValueError(f'{name} must be a nonnegative number') from exc
    if not math.isfinite(value) or value < 0:
        raise ValueError(f'{name} must be a finite nonnegative number')
    return value


def is_busy(exc: Exception) -> bool:
    return 'locked' in str(exc).lower() or 'busy' in str(exc).lower()


@contextmanager
def index_lock(db_path: str, *, shared: bool = False):
    """Serialize writers; POSIX readers share a lock, Windows readers serialize.

    OS locks are released even if the owning process crashes. The lock file is
    retained so that new processes always lock the same inode.
    """
    path = os.path.abspath(db_path) + '.lock'
    os.makedirs(os.path.dirname(path), exist_ok=True)
    wait = timeout_seconds('INDEXER_LOCK_TIMEOUT', 10.0)
    deadline = time.monotonic() + wait
    with open(path, 'a+b') as stream:
        if os.name == 'nt':
            import msvcrt
            # Windows byte-range locks may extend beyond EOF. Do not seed an
            # empty file before locking: another process can already own byte 0,
            # making that initialization write fail with PermissionError.
            def acquire():
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            def release():
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            def acquire():
                fcntl.flock(stream.fileno(), (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
            def release():
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        while True:
            try:
                acquire()
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                if time.monotonic() >= deadline:
                    raise IndexBusyError(
                        f'Index is busy with another process (waited {wait:g}s). Retry shortly; '
                        'INDEXER_LOCK_TIMEOUT controls the maximum wait.') from exc
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        try:
            yield
        finally:
            release()
