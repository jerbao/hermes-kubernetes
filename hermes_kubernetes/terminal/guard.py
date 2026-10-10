"""Coordinate periodic Git updates with read-only workspace snapshots."""

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat


@contextmanager
def guard(path, *, create=False, blocking=True):
    path = Path(path)
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    flags = (os.O_RDWR | os.O_CREAT if create else os.O_RDONLY) | os.O_NOFOLLOW | os.O_NONBLOCK
    descriptor = os.open(path, flags, 0o600)
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise ValueError("Workspace coordination requires a regular file")
        fcntl.flock(descriptor, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        current = path.stat(follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (details.st_dev, details.st_ino):
            raise ValueError("Workspace coordination file changed")
        yield
    finally:
        os.close(descriptor)
