"""Atomic file writes: a unique temp file in the same folder, then os.replace."""

import os
import tempfile
from pathlib import Path


def atomic_write(path, data, *, mode=None, fsync=False):
    """Replace ``path`` with ``data`` (str is written as UTF-8). The temp file is
    created 0600 (or ``mode``); with ``fsync`` it is flushed to disk before the rename."""
    path = Path(path)
    if isinstance(data, str):
        data = data.encode("utf-8")
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        if mode is not None:
            os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            if fsync:
                f.flush()
                os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
