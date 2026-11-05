"""One-click setup for Claude Desktop and Claude Code.

Adds (or removes) Open Pin's MCP server entry, ``mcpServers["open-pin"]``, in
the clients' own settings files, pointing at the launcher from
``app/mcp/access.py``. Nothing else in those files changes:

- The file is copied to ``<name>.open-pin-backup-<YYYYMMDD-HHMMSS>`` first,
  keeping the last three backups (by name).
- It's written atomically (a temporary file in the same folder, then a
  rename), with the original's permissions, through any symlink.
- A file that isn't a valid JSON object is never written: ``ValueError``.
- If the file changes between our read and our rename (Claude Code rewrites
  its file often), we read it again, so its change isn't lost.

Files:
- Claude Desktop: ``~/Library/Application Support/Claude/claude_desktop_config.json``.
  Created if missing. Entry ``{"command": launcher}``.
- Claude Code: every ``~/.claude.json`` and ``~/.claude*/.claude.json`` (a
  desktop app can't see ``CLAUDE_CONFIG_DIR``). Only existing files are
  edited. Entry ``{"type": "stdio", "command": launcher, "args": [], "env": {}}``
  at the top level (user scope), the shape ``claude mcp add`` writes.

This never runs the ``claude`` CLI.
"""

import json
import os
import re
import shutil
import stat
import tempfile
import time
from pathlib import Path

from app.mcp import access

SERVER_NAME = "open-pin"
BACKUP_MARKER = ".open-pin-backup-"
KEEP_BACKUPS = 3
KINDS = ("desktop", "code")
DESKTOP_NEW_FILE_MODE = 0o644
_WRITE_ATTEMPTS = 3


def _home() -> Path:
    return Path.home()


def claude_desktop_path() -> Path:
    return _home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"


def claude_code_files() -> list[Path]:
    """Claude Code settings files in the home folder that exist (one per real file)."""
    home = _home()
    found: list[Path] = []
    seen: set[Path] = set()
    for path in [home / ".claude.json", *sorted(home.glob(".claude*/.claude.json"))]:
        try:
            if not path.is_file():
                continue
            real = path.resolve()
        except OSError:
            continue
        if real in seen:
            continue
        seen.add(real)
        found.append(path)
    return found


def shown_path(path: Path) -> str:
    """The path as the window shows it, with the home folder as ``~``."""
    try:
        return "~/" + str(path.relative_to(_home()))
    except ValueError:
        return str(path)


def _entry(kind: str) -> dict:
    launcher = str(access.launcher_path())
    if kind == "desktop":
        return {"command": launcher}
    return {"type": "stdio", "command": launcher, "args": [], "env": {}}


def _check_kind(kind: str) -> None:
    if kind not in KINDS:
        raise ValueError(f"Unknown AI tool: {kind!r}")


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _parse(path: Path, text: str) -> dict:
    """The file's JSON object, or ValueError. An empty file counts as ``{}``."""
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        raise ValueError(
            f"{shown_path(path)} isn't valid JSON, so Open Pin left it alone. Fix it in a text editor, then try again."
        ) from None
    if not isinstance(data, dict):
        raise ValueError(f"{shown_path(path)} isn't a settings file Open Pin understands, so it was left alone.")
    servers = data.get("mcpServers")
    if servers is not None and not isinstance(servers, dict):
        raise ValueError(
            f"{shown_path(path)} has an mcpServers entry Open Pin doesn't understand, so it was left alone."
        )
    return data


def _entry_state(path: Path) -> tuple[bool, bool, str | None]:
    """(connected, stale, error) for one file. Stale: the entry points at another launcher."""
    try:
        data = _parse(path, path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False, False, None
    except ValueError as e:
        return False, False, str(e)
    except (OSError, UnicodeDecodeError) as e:
        return False, False, f"Couldn't read {shown_path(path)}: {getattr(e, 'strerror', None) or e}"
    entry = (data.get("mcpServers") or {}).get(SERVER_NAME)
    if entry is None:
        return False, False, None
    command = entry.get("command") if isinstance(entry, dict) else None
    return True, command != str(access.launcher_path()), None


def status() -> dict:
    """What the Settings section shows. Paths are shown with ``~``."""
    desktop = claude_desktop_path()
    connected, stale, _error = _entry_state(desktop)
    code = []
    for path in claude_code_files():
        c_connected, c_stale, c_error = _entry_state(path)
        code.append({"path": shown_path(path), "connected": c_connected, "stale": c_stale, "error": c_error})
    return {
        "desktop": {"path": shown_path(desktop), "exists": desktop.is_file(), "connected": connected, "stale": stale},
        "code": code,
    }


def resolve(kind: str, shown) -> Path | None:
    """The real file behind a path ``status()`` lists for ``kind``, or None.

    The window may only name files Open Pin found itself, never any path.
    """
    if not isinstance(shown, str):
        return None
    if kind == "desktop":
        candidates = [claude_desktop_path()]
    elif kind == "code":
        candidates = claude_code_files()
    else:
        return None
    for path in candidates:
        if shown_path(path) == shown:
            return path
    return None


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def _timestamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _backup_pattern(path: Path) -> re.Pattern:
    """Open Pin's own backups of ``path``: ``<name>.open-pin-backup-YYYYMMDD-HHMMSS[-N]``."""
    return re.compile(rf"^{re.escape(path.name)}{re.escape(BACKUP_MARKER)}(\d{{8}}-\d{{6}})(?:-(\d+))?$")


def _prune_backups(path: Path) -> None:
    """Keep the newest three of Open Pin's backups, newest by (timestamp, N)."""
    pattern = _backup_pattern(path)
    ours = []
    for candidate in path.parent.iterdir():
        m = pattern.match(candidate.name)
        if m:
            ours.append(((m.group(1), int(m.group(2) or 1)), candidate))
    ours.sort(key=lambda item: item[0])
    for _key, old in ours[:-KEEP_BACKUPS]:
        try:
            old.unlink()
        except OSError:
            pass


def _backup(path: Path) -> Path:
    """Copy ``path`` beside itself (its mode and times kept); keep the newest three.

    The copy is created 0600 and only gets the original's mode once it's
    written, so a private settings file is never readable by others meanwhile.
    """
    stamp = _timestamp()
    stem = f"{path.name}{BACKUP_MARKER}{stamp}"
    # Number after the newest backup of this second, so the order stays true
    # after pruning frees lower numbers.
    pattern = _backup_pattern(path)
    taken = [
        int(m.group(2) or 1)
        for m in (pattern.match(c.name) for c in path.parent.iterdir())
        if m and m.group(1) == stamp
    ]
    n = max(taken) + 1 if taken else 1
    while True:
        target = path.with_name(stem if n == 1 else f"{stem}-{n}")
        try:
            fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            break
        except FileExistsError:
            n += 1
    try:
        with os.fdopen(fd, "wb") as out, open(path, "rb") as src:
            shutil.copyfileobj(src, out)
            out.flush()
            os.fsync(out.fileno())
        shutil.copystat(path, target)
    except BaseException:
        try:
            target.unlink()
        except OSError:
            pass
        raise
    _prune_backups(path)
    return target


def _signature(st: os.stat_result) -> tuple[int, int, int]:
    return st.st_mtime_ns, st.st_size, st.st_ino


def _edit(path: Path, change, create: bool) -> None:
    """Read the JSON object at ``path``, let ``change(data)`` edit it, write it back.

    ``change`` returns False when nothing needs writing. Missing file: created
    if ``create`` (mode 0644), otherwise ValueError.
    """
    # Edit the real file, so a symlinked settings file stays a symlink.
    target = path.resolve() if path.is_symlink() else path
    backed_up = False  # once per call, however many times a change by someone else makes us retry
    for _attempt in range(_WRITE_ATTEMPTS):
        try:
            before = target.stat()
            raw = target.read_bytes()
        except FileNotFoundError:
            if not create:
                raise ValueError(f"{shown_path(path)} wasn't found. Open Claude Code once, then try again.") from None
            before, raw = None, b""
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError(f"{shown_path(path)} isn't valid JSON, so Open Pin left it alone.") from None
        data = _parse(path, text)
        if change(data) is False:
            return
        out = json.dumps(data, indent=2, ensure_ascii=False)
        if text.endswith("\n") or before is None:
            out += "\n"
        mode = stat.S_IMODE(before.st_mode) if before is not None else DESKTOP_NEW_FILE_MODE

        if before is not None and not backed_up:
            _backup(target)
            backed_up = True
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".open-pin.tmp")
        try:
            os.fchmod(fd, mode)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(out)
                f.flush()
                os.fsync(f.fileno())
            # Someone else wrote the file since we read it: start again from theirs.
            try:
                now = _signature(target.stat())
            except FileNotFoundError:
                now = None
            if now != (_signature(before) if before is not None else None):
                os.unlink(tmp)
                continue
            os.replace(tmp, target)
            return
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    raise OSError(f"{shown_path(path)} kept changing while Open Pin was saving it. Try again in a moment.")


def connect(path: Path, kind: str) -> None:
    """Add (or update) the ``open-pin`` entry in ``path``."""
    _check_kind(kind)
    entry = _entry(kind)

    def add(data: dict):
        servers = data.setdefault("mcpServers", {})
        if servers.get(SERVER_NAME) == entry:
            return False
        servers[SERVER_NAME] = entry
        return True

    _edit(path, add, create=(kind == "desktop"))


def disconnect(path: Path, kind: str) -> None:
    """Remove only the ``open-pin`` entry from ``path``. A missing file or entry is fine."""
    _check_kind(kind)

    def remove(data: dict):
        servers = data.get("mcpServers")
        if not servers or SERVER_NAME not in servers:
            return False
        del servers[SERVER_NAME]
        return True

    try:
        _edit(path, remove, create=False)
    except ValueError:
        if not path.exists():
            return
        raise
