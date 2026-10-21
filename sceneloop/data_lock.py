"""One server per task directory, enforced by an operating-system lock.

The persistent file is a lock target, never a PID lease. Do not unlink it:
another process could otherwise lock a different inode at the same path.
"""
from __future__ import annotations

import errno
import os
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class DataDirectoryInUse(Exception):
    """Another server still owns this task directory."""


class DataDirectoryLock:
    """Explicit lock handle; close only after all writers have stopped."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self._stream = None

    def acquire(self):
        if self._stream is not None:
            raise RuntimeError("Data directory lock is already acquired.")
        self.root.mkdir(parents=True, exist_ok=True)
        stream = (self.root / ".server.lock").open("a+b")
        try:
            # Use a stable byte for Windows byte-range locks; never truncate.
            if os.fstat(stream.fileno()).st_size == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            stream.close()
            if error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise DataDirectoryInUse("该任务目录已由另一个 SceneLoop 服务使用。请复用已有服务，或指定独立的 --data-dir。") from None
            raise
        except BaseException:
            stream.close()
            raise
        self._stream = stream
        return self

    def close(self):
        if self._stream is not None:
            self._stream.close()
            self._stream = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_):
        self.close()


# HTTP can stop while daemon request/job threads still write. Keep handles for
# the process lifetime; no early release in main's finally or an atexit callback.
# OS exit/crash releases the locks automatically. Never remove the lock file.
_PROCESS_LOCKS = []


def acquire_process_lock(root):
    """Acquire before RunManager; retain for the entire server process.

    Production and fixture server entry points must both call this. This is not
    a context manager around serve_forever(): daemon workers can outlive it.
    """
    lock = DataDirectoryLock(root).acquire()
    _PROCESS_LOCKS.append(lock)
    return lock
