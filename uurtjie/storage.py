"""Private local storage and atomic file replacement (no named temporary files)."""
from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path


def private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if os.fstat(fd).st_uid != os.getuid():
            raise PermissionError(f"directory is not owned by this user: {path}")
        os.fchmod(fd, 0o700)
    finally:
        os.close(fd)


def private_file(path: Path) -> None:
    fd = os.open(path, os.O_CREAT | os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise PermissionError(f"not a regular file owned by this user: {path}")
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)


def atomic_write(path: Path, text: str) -> None:
    """Replace the destination itself, including a symlink, using a fresh 0600 file."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def runtime_dir(path: Path) -> None:
    """The runtime root must already be private and owned by the current user."""
    root = path.parent
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = root.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise PermissionError(f"runtime root must be private and owned by this user: {root}")
    private_dir(path)
