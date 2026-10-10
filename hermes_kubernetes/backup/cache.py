"""Release clean page cache of private backup staging files on Linux.

Ceph page cache is charged to the backup's cgroup too. Streaming alone does
not bound it. Flush only private staging writes before advising DONTNEED;
live source descriptors receive advice only when opened read-only by this
backup. No source data, writer descriptor or host cache setting is changed.
"""

from contextlib import contextmanager
import os
from pathlib import Path
import stat
import threading


def release(roots, read_roots=()):
    """Inspect this process and its children, considering only explicit roots."""
    pending, seen, advised = [os.getpid()], set(), 0
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        proc = Path("/proc") / str(pid)
        try:
            for task in (proc / "task").iterdir():
                pending.extend(map(int, (task / "children").read_text().split()))
            descriptors = list((proc / "fd").iterdir())
        except (OSError, ValueError):
            continue
        for descriptor in descriptors:
            try:
                target = descriptor.resolve(strict=True)
                writable_allowed = any(
                    target == root or target.is_relative_to(root) for root in roots
                )
                if not writable_allowed and not any(
                    target == root or target.is_relative_to(root) for root in read_roots
                ):
                    continue
                details = descriptor.stat()
                if not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid():
                    continue
                info = dict(
                    line.split(":", 1)
                    for line in (proc / "fdinfo" / descriptor.name).read_text().splitlines()
                )
                writable = int(info["flags"].strip(), 8) & os.O_ACCMODE != os.O_RDONLY
                if writable and not writable_allowed:
                    continue
                with descriptor.open("rb+" if writable else "rb", buffering=0) as handle:
                    opened = os.fstat(handle.fileno())
                    if (opened.st_dev, opened.st_ino) != (
                        details.st_dev,
                        details.st_ino,
                    ):
                        continue
                    # Opening /proc/PID/fd never changes the original file position.
                    if writable:
                        os.fdatasync(handle.fileno())
                        length = 0
                    else:
                        # SQLite uses pread (position stays zero) for random
                        # integrity reads. Invalidating its whole file every
                        # half second makes quick_check repeatedly miss cache.
                        # Sequential readers can release only the consumed prefix.
                        length = max(0, int(info["pos"]) - 4 * 1024 * 1024)
                        if not length:
                            continue
                    os.posix_fadvise(handle.fileno(), 0, length, os.POSIX_FADV_DONTNEED)
                    advised += 1
            except (OSError, ValueError, KeyError):
                # A descriptor can close between the scan and the advice. Cache
                # management must not invalidate an otherwise valid backup.
                continue
    return advised


@contextmanager
def bounded_cache(*paths, read_roots=(), interval=0.5):
    roots = tuple(Path(path).resolve() for path in paths)
    reads = tuple(Path(path).resolve() for path in read_roots)
    if not Path("/proc/self/fd").is_dir() or not hasattr(os, "posix_fadvise"):
        yield
        return
    stop = threading.Event()

    def worker():
        while not stop.wait(interval):
            release(roots, reads)

    thread = threading.Thread(target=worker, name="backup-page-cache", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()
        release(roots, reads)


@contextmanager
def open_read(path):
    """Release pages after a completed sequential backup read, including small files."""
    with Path(path).open("rb") as stream:
        try:
            yield stream
        finally:
            if hasattr(os, "posix_fadvise"):
                try:
                    os.posix_fadvise(stream.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
                except OSError:
                    pass
