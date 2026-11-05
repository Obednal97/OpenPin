"""Read models for AI tools (the /mcp/v1 routes in app/routes/mcp_api.py).

Everything here is built from what the app itself shows: the recordings list
(``recordings.collect_recordings``), the transcript items (``recordings.transcript_item``,
which applies speaker names), tags, and the coaching sidecar as the coaching view
reads it (``runner.current``). Nothing is copied from the app's logic, so the window
and AI tools always see the same data.

Only the fields listed in the spec leave this module: never API keys, the voice
profile, pairing data, absolute paths outside a display folder, or audio bytes.
"""

import base64
import binascii
import json
from datetime import date, datetime, time, timedelta
from pathlib import Path

from app.coaching import runner
from app.routes import recordings
from app.state import speakers
from app.utils import speaker_names as sn
from app.utils.config import load_config

DEFAULT_LIMIT = 25
MAX_LIMIT = 100
INCLUDE_PARTS = ("transcript", "summary", "coaching")
_NO_DATE = -1e18  # sorts recordings without a date last


class BadRequest(ValueError):
    """Input an AI tool sent that can't be used: the message is shown to it."""


# --------------------------------------------------------------------------- small helpers


def display_path(path) -> str:
    """A folder as the user would write it: the home folder shown as ``~``."""
    path = Path(path)
    home = Path.home()
    try:
        rel = path.relative_to(home)
    except ValueError:
        return str(path)
    return "~" if str(rel) == "." else f"~/{rel}"


def _recorded_at(entry: dict) -> datetime | None:
    """The recording's start as an aware datetime in the Mac's time zone, or None."""
    raw = entry.get("date")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        when = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return when.astimezone()  # naive values are local time, as the app writes them


def _date_title(when: datetime | None) -> str:
    return f"{when.day} {when:%B %Y} at {when:%H:%M}" if when else ""


def _number(value) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return round(float(value), 1)
    return None


def _named(value, names: dict[str, str]):
    """``value`` with speaker names applied to every string inside it."""
    if isinstance(value, str):
        return sn.apply_names(value, names)
    if isinstance(value, list):
        return [_named(v, names) for v in value]
    if isinstance(value, dict):
        return {k: _named(v, names) for k, v in value.items()}
    return value


# --------------------------------------------------------------------------- rows


class _Row:
    """One recording with what list and get need: the list entry, transcript and coaching."""

    def __init__(self, entry: dict, profile: str, notion_urls: dict):
        self.entry = entry
        self.id = entry["session_id"]
        raw = recordings.load_transcript(self.id) if entry.get("has_transcript") else None
        self.raw_transcript = raw
        self.transcript = recordings.transcript_item(self.id, raw, profile) if raw is not None else None
        self.sidecar = runner.current(self.id)
        self.when = _recorded_at(entry)
        self.notion_url = notion_urls.get(self.id) or (raw or {}).get("notion_page_url") or None

    @property
    def names(self) -> dict[str, str]:
        return dict(self.transcript["speaker_names"]) if self.transcript else {}

    @property
    def sort_key(self) -> tuple[float, str]:
        return (self.when.timestamp() if self.when else _NO_DATE, self.id)

    def list_item(self) -> dict:
        t = self.transcript or {}
        title = self.entry.get("title") or t.get("auto_title") or _date_title(self.when) or self.id
        return {
            "id": self.id,
            "title": title,
            "recorded_at": self.when.isoformat() if self.when else None,
            "duration_s": _number(t.get("duration")) or _number(self.entry.get("duration")),
            "tags": list(self.entry.get("tags") or []),
            "participants": list(self.entry.get("participants") or []),
            "has_transcript": self.transcript is not None,
            "has_summary": bool(t.get("summary")),
            "coaching_status": self.sidecar.get("status") or None,
            "notion_url": self.notion_url,
        }

    def search_text(self) -> str:
        t = self.transcript or {}
        item = self.list_item()
        parts = [item["title"], *item["tags"], *item["participants"], *self.names.values()]
        parts += [t.get("text") or "", t.get("summary") or ""]
        return "\n".join(p for p in parts if isinstance(p, str)).casefold()

    def file_info(self) -> dict:
        entry = self.entry
        size = entry.get("size") if isinstance(entry.get("size"), int) else None
        if entry.get("cloud_only"):
            return {
                "name": f"{self.id}.ogg",
                "format": "ogg",
                "size_bytes": size,
                "folder": None,
                "location": "gdrive",
                "audio_on_mac": False,
            }
        audio = Path(entry["ogg_path"]) if entry.get("ogg_path") else None
        return {
            "name": audio.name if audio else f"{self.id}.ogg",
            "format": "ogg",
            "size_bytes": size,
            "folder": display_path(audio.parent) if audio else None,
            "location": "local",
            "audio_on_mac": audio is not None,
        }

    def coaching(self) -> dict | None:
        side = self.sidecar
        if side.get("excluded") or not side.get("status"):
            return None
        return {
            "status": side.get("status"),
            "metrics": side.get("metrics"),
            "review": _named(side.get("review"), self.names),
        }

    def detail(self, include) -> dict:
        out = self.list_item()
        out["file"] = self.file_info()
        names = self.names
        out["speakers"] = names
        out["you"] = sn.confirmed_owner_labels(self.raw_transcript, self.sidecar) if self.raw_transcript else []
        t = self.transcript
        if "transcript" in include:
            out["transcript"] = (
                [
                    {
                        "start_s": s.get("start"),
                        "end_s": s.get("end"),
                        "speaker_label": s.get("speaker") or None,
                        "speaker_name": names.get(s.get("speaker")) if s.get("speaker") else None,
                        "text": s.get("text", ""),
                    }
                    for s in t.get("segments") or []
                    if isinstance(s, dict)
                ]
                if t
                else None
            )
        if "summary" in include:
            summary = t.get("summary") if t else None
            out["summary"] = sn.apply_names(summary, names) if isinstance(summary, str) and summary else None
        if "coaching" in include:
            out["coaching"] = self.coaching()
        return out


def _rows(only: str | None = None) -> list[_Row]:
    """A row per recording the app lists, newest first (just ``only``'s, when given)."""
    config = load_config()
    profile = speakers.profile_name() or ""
    notion_urls = config.get("notion_page_urls") or {}
    entries = recordings.collect_recordings()
    if only is not None:
        entries = [e for e in entries if e.get("session_id") == only]
    rows = [_Row(entry, profile, notion_urls) for entry in entries]
    rows.sort(key=lambda r: r.sort_key, reverse=True)
    return rows


# --------------------------------------------------------------------------- arguments


def parse_limit(raw) -> int:
    if raw is None or raw == "":
        return DEFAULT_LIMIT
    try:
        limit = int(raw)
    except (TypeError, ValueError):
        raise BadRequest("limit must be a whole number from 1 to 100") from None
    if not 1 <= limit <= MAX_LIMIT:
        raise BadRequest("limit must be a whole number from 1 to 100")
    return limit


def _parse_bound(raw, name: str, end: bool) -> datetime | None:
    """``from``/``to``: a YYYY-MM-DD date (the whole day) or an ISO 8601 date-time."""
    if raw is None or raw == "":
        return None
    raw = raw.strip()
    try:
        if len(raw) == 10:
            day = date.fromisoformat(raw)
            if end:
                return datetime.combine(day + timedelta(days=1), time()).astimezone() - timedelta(microseconds=1)
            return datetime.combine(day, time()).astimezone()
        when = datetime.fromisoformat(raw.replace("Z", "+00:00") if raw.endswith("Z") else raw)
    except ValueError:
        raise BadRequest(f"{name} must be a date (YYYY-MM-DD) or an ISO 8601 date-time") from None
    return when.astimezone()


def encode_cursor(key: tuple[float, str]) -> str:
    return base64.urlsafe_b64encode(json.dumps(list(key)).encode()).decode().rstrip("=")


def decode_cursor(raw) -> tuple[float, str] | None:
    if raw is None or raw == "":
        return None
    try:
        padded = raw + "=" * (-len(raw) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, binascii.Error, UnicodeEncodeError):
        raise BadRequest("cursor isn't valid: pass next_cursor from the previous page") from None
    if (
        not isinstance(value, list)
        or len(value) != 2
        or not isinstance(value[0], (int, float))
        or isinstance(value[0], bool)
        or not isinstance(value[1], str)
    ):
        raise BadRequest("cursor isn't valid: pass next_cursor from the previous page")
    return (float(value[0]), value[1])


def parse_include(raw) -> tuple[str, ...]:
    if raw is None or raw.strip() == "":
        return INCLUDE_PARTS
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    unknown = [p for p in parts if p not in INCLUDE_PARTS]
    if unknown:
        raise BadRequest(f"include can only name {', '.join(INCLUDE_PARTS)}, not {', '.join(unknown)}")
    return tuple(dict.fromkeys(parts))


# --------------------------------------------------------------------------- the read models


def list_recordings(query=None, tag=None, start=None, end=None, limit=None, cursor=None) -> dict:
    """``{items, next_cursor}``: recordings newest first, filtered and paged."""
    limit = parse_limit(limit)
    after = decode_cursor(cursor)
    lower = _parse_bound(start, "from", end=False)
    upper = _parse_bound(end, "to", end=True)
    if lower and upper and lower > upper:
        raise BadRequest("from is after to")
    needle = query.strip().casefold() if isinstance(query, str) and query.strip() else None
    wanted_tag = tag.strip().casefold() if isinstance(tag, str) and tag.strip() else None

    page: list[_Row] = []
    more = False
    for row in _rows():
        if after is not None and not row.sort_key < after:
            continue
        if wanted_tag and wanted_tag not in {t.casefold() for t in row.entry.get("tags") or [] if isinstance(t, str)}:
            continue
        if (lower or upper) and row.when is None:
            continue
        if lower and row.when < lower:
            continue
        if upper and row.when > upper:
            continue
        if needle and needle not in row.search_text():
            continue
        if len(page) == limit:
            more = True
            break
        page.append(row)
    return {
        "items": [row.list_item() for row in page],
        "next_cursor": encode_cursor(page[-1].sort_key) if more else None,
    }


def get_recording(session_id: str, include=INCLUDE_PARTS) -> dict | None:
    """One recording in full, or None when there's no such recording."""
    if not recordings.recording_exists(session_id):  # validates the id first
        return None
    rows = _rows(only=session_id)
    return rows[0].detail(include) if rows else None


def speaker_map(session_id: str) -> dict[str, str]:
    """The recording's effective speaker names (yours included once confirmed)."""
    return speakers.get_names(session_id)
