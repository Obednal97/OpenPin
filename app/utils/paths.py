"""Validation for client-supplied session ids and paths.

The local API has no auth, so every session id or path that arrives in a
request body must be checked before it is joined onto a directory: an id like
``../../x`` or an absolute path would otherwise let a caller delete or rewrite
files anywhere on disk.

Session ids seen in practice:

- device recordings: the Unix timestamp, e.g. ``1715000000``
- Plaud Cloud downloads: ``cloud_<YYYY-MM-DD_HHMM>_<cloud filename[:40]>``
  (spaces → ``_``, ``/`` and ``:`` → ``-``; the filename part is user-chosen
  in the Plaud app, so it may hold punctuation or non-ASCII letters)
- derived stems such as ``<id>_uncropped``

Because the cloud part is free text, a strict ``[A-Za-z0-9_-]`` allow-list
would make existing cloud recordings impossible to delete/tag/rename. So the
rule is a deny-list of exactly what makes traversal possible: path
separators (``/`` ``\\``), ``:`` (the classic Mac separator), NUL and other
control characters, and a leading ``.`` (which covers ``.``, ``..`` and
hidden files). A valid id is therefore always one non-hidden path component;
routes additionally check containment with :func:`resolve_inside`.
"""

import re
from pathlib import Path

MAX_SESSION_ID_LEN = 128

_SESSION_ID_RE = re.compile(r"^[^/\\:\x00-\x1f\x7f]+$")


class InvalidSessionIdError(ValueError):
    """Raised for a session id that is not a safe single path component."""


class PathOutsideError(ValueError):
    """Raised when a path resolves outside the allowed base directories."""


def validate_session_id(sid) -> str:
    """Return ``sid`` unchanged if it is a safe session id, else raise.

    Raises :class:`InvalidSessionIdError` (a ``ValueError``).
    """
    if not isinstance(sid, str) or not sid:
        raise InvalidSessionIdError("Invalid session_id")
    if len(sid) > MAX_SESSION_ID_LEN:
        raise InvalidSessionIdError("Invalid session_id: too long")
    if sid.startswith(".") or not _SESSION_ID_RE.match(sid):
        raise InvalidSessionIdError("Invalid session_id")
    # Belt and braces: the id must be exactly one path component.
    if Path(sid).name != sid or "/" in sid or "\\" in sid or "\x00" in sid:
        raise InvalidSessionIdError("Invalid session_id")
    return sid


def validate_session_ids(sids) -> list:
    """Validate a list of session ids; raise on a non-list or any bad id."""
    if not isinstance(sids, list):
        raise InvalidSessionIdError("session_ids must be a list")
    return [validate_session_id(s) for s in sids]


AUXILIARY_SUFFIXES = ("_uncropped", "_crop_tmp")


def is_auxiliary_stem(stem) -> bool:
    """True for crop backups / temp files (``<sid>_uncropped.ogg``,
    ``<sid>_crop_tmp.ogg``) that live next to recordings but are not ones."""
    return str(stem).endswith(AUXILIARY_SUFFIXES)


def resolve_inside(base_dirs, name, *, must_exist=False) -> Path:
    """Resolve ``name`` and return it only if it lies inside one of ``base_dirs``.

    ``name`` may be absolute or relative. A relative name is tried against
    each base in turn (the first existing match wins; otherwise the first
    base). Symlinks are resolved on both sides before the containment check,
    so a link pointing outside the bases is rejected.

    Raises :class:`PathOutsideError` (a ``ValueError``), or
    ``FileNotFoundError`` when ``must_exist`` and nothing matches.
    """
    if isinstance(base_dirs, (str, Path)):
        base_dirs = [base_dirs]
    bases = []
    for b in base_dirs:
        try:
            bases.append(Path(b).resolve())
        except (OSError, RuntimeError):
            continue
    if not bases:
        raise PathOutsideError("No allowed directories")

    name = str(name)
    if not name or "\x00" in name:
        raise PathOutsideError("Invalid path")

    p = Path(name)
    candidates = [p] if p.is_absolute() else [b / p for b in bases]

    contained = []
    for c in candidates:
        try:
            r = c.resolve()
        except (OSError, RuntimeError):
            continue
        if any(r == b or r.is_relative_to(b) for b in bases):
            if r.exists():
                return r
            contained.append(r)

    if contained:
        if must_exist:
            raise FileNotFoundError(name)
        return contained[0]
    raise PathOutsideError("Path is outside the allowed directories")
