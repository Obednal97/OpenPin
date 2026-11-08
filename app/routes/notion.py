"""Notion MCP integration routes."""

import json
import logging
import re
import threading
import time as _time
import traceback

import requests
from flask import Blueprint, jsonify, request

from app.state import notion
from app.utils import notion_legacy, speaker_names, transcripts
from app.utils.config import load_config, update_config
from app.utils.constants import DEFAULT_TAGS, TRANSCRIPTS_DIR
from app.utils.paths import InvalidSessionIdError, validate_session_id
from app.utils.transcript_locks import get_transcript_lock


def _error_json(base, exc):
    """Return error JSON with traceback for debugging (auto-reported to GitHub)."""
    base["error"] = str(exc)
    base["traceback"] = traceback.format_exc()
    return base


bp = Blueprint("notion", __name__, url_prefix="/notion")


# A Notion id is 32 hex chars, optionally dashed 8-4-4-4-12. In URLs it is the
# tail of the last path segment, after a title slug ("Meeting-Notes-<id>").
_HEX32_AT_RUN_END = re.compile(r"[0-9a-fA-F]{32}(?![0-9a-fA-F])")


def _extract_notion_id(url_or_id):
    """Return the dashed, lower-case Notion id from a URL or bare id, or None.

    Handles ``https://www.notion.so/<workspace>/<Slug-Words>-<32hex>``,
    ``https://notion.so/<32hex>?v=<view id>``, ``.../<dashed uuid>#frag`` and
    bare ids. Only the final path segment is considered (the ``?v=`` view id
    and fragments are ignored), and the *last* 32 hex chars are taken: slug
    words made of hex letters ("Recorded-", "Cafe-") directly precede the id
    once dashes are removed, so taking the first 32-hex run would pull them in.
    """
    if not url_or_id or not isinstance(url_or_id, str):
        return None
    s = re.split(r"[?#]", url_or_id.strip(), maxsplit=1)[0].rstrip("/")
    segment = s.rsplit("/", 1)[-1].replace("-", "")
    matches = _HEX32_AT_RUN_END.findall(segment)
    if not matches:
        return None
    h = matches[-1].lower()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def _invalid_session_id():
    return jsonify({"error": "Invalid session_id"}), 400


def _read_transcript_json(session_id):
    """Read TRANSCRIPTS_DIR/<session_id>.json under the transcript lock.

    Returns the parsed dict, or None if the file is missing.
    """
    validate_session_id(session_id)
    path = TRANSCRIPTS_DIR / f"{session_id}.json"
    with get_transcript_lock(session_id):
        if not path.exists():
            return None
        return json.loads(path.read_text())


def _update_transcript_json(session_id, updates):
    """Merge ``updates`` into the transcript JSON for ``session_id``.

    Re-reads the file under the per-session transcript lock (so keys written
    concurrently by transcription/diarization/tagging survive) and writes it
    back atomically (temp file + os.replace). Does nothing if the transcript
    does not exist. Returns True if the file was written.
    """

    def _merge(data):
        data.update(updates)
        return True

    return _mutate_transcript_json(session_id, _merge)


def _mutate_transcript_json(session_id, mutate):
    """Apply ``mutate(data)`` to the transcript JSON under its lock.

    ``mutate`` changes the dict in place and returns True if it changed
    anything; the file is then written back atomically. Returns True if the
    file was written.
    """
    validate_session_id(session_id)
    return transcripts.update(TRANSCRIPTS_DIR / f"{session_id}.json", lambda d: bool(mutate(d)))


_last_notion_verify = 0


@bp.route("/status")
def status():
    global _last_notion_verify
    state = notion.get_state()
    # Periodically verify the token is still valid (every 5 minutes)
    if state["connected"] and _time.time() - _last_notion_verify > 300:
        _last_notion_verify = _time.time()
        if not notion.verify_connection():
            state = notion.get_state()  # Refresh after verify updated the state
    return jsonify(state)


@bp.route("/connect", methods=["POST"])
def connect():
    """Start the Notion OAuth flow — returns the authorization URL."""
    try:
        auth_url = notion.start_connect()
        return jsonify({"url": auth_url})
    except Exception as e:
        return jsonify(_error_json({}, e)), 500


@bp.route("/disconnect", methods=["POST"])
def disconnect():
    reset_schema_cache()
    return jsonify(notion.disconnect())


@bp.route("/queue-status")
def queue_status():
    """Snapshot of the background push queue: pending, paused, failed jobs."""
    from app.utils import notion_queue

    return jsonify(notion_queue.snapshot())


@bp.route("/queue-resume", methods=["POST"])
def queue_resume():
    """Un-pause the queue and re-queue permanently failed pushes ("Retry")."""
    from app.utils import notion_queue

    notion_queue.resume()
    notion_queue.retry_failed()
    return jsonify(notion_queue.snapshot())


@bp.route("/databases")
def databases():
    """Search for databases. Pass ?q=term for fuzzy search."""
    if not notion.is_connected():
        return jsonify({"error": "Not connected to Notion"}), 401

    query = request.args.get("q", "").strip()
    search_query = f"Notion database {query}" if query else "Notion database"

    try:
        result = notion.mcp_call(
            "notion-search",
            {
                "query": search_query,
                "query_type": "internal",
                "content_search_mode": "workspace_search",
                "filters": {},
                "max_highlight_length": 0,
            },
        )
        content = result.get("content", [])
        text = ""
        for item in content:
            text += item.get("text", "") if isinstance(item, dict) else str(item)

        dbs = []
        try:
            parsed = json.loads(text)
            results = parsed.get("results", [parsed] if isinstance(parsed, dict) else [])
            for r in results:
                if not isinstance(r, dict):
                    continue
                obj_type = r.get("object", r.get("type", ""))
                if obj_type != "database":
                    continue
                title = r.get("title", r.get("name", ""))
                if isinstance(title, list) and title:
                    title = title[0].get("plain_text", str(title[0]))
                db_id = r.get("id", "")
                url = r.get("url", "")
                dbs.append(
                    {
                        "id": str(db_id),
                        "title": str(title),
                        "url": str(url),
                    }
                )
        except json.JSONDecodeError:
            pass

        if not dbs and text:
            dbs.append({"id": "", "title": text[:200], "url": "", "type": "raw"})

        return jsonify({"databases": dbs})
    except Exception as e:
        return jsonify(_error_json({}, e)), 500


@bp.route("/set-database-url", methods=["POST"])
def set_database_url():
    """Set database by pasting a Notion database URL."""
    data = request.get_json(silent=True) or {}
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    if not notion.is_connected():
        return jsonify({"error": "Not connected to Notion"}), 401

    db_id = _extract_notion_id(url)
    if not db_id:
        return jsonify({"error": "Could not extract database ID from URL"}), 400

    try:
        # The Notion MCP notion-fetch tool takes {"id": ...}; it has no "url" argument.
        result = notion.mcp_call("notion-fetch", {"id": db_id})
        content = result.get("content", [])
        text = ""
        for item in content:
            text += item.get("text", "") if isinstance(item, dict) else str(item)

        title = "Notion Database"
        try:
            parsed = json.loads(text)
            title = parsed.get("title", parsed.get("name", title))
            if isinstance(title, list) and title:
                title = title[0].get("plain_text", title[0].get("text", {}).get("content", str(title[0])))
        except (json.JSONDecodeError, TypeError, IndexError, AttributeError):
            first_line = text.split("\n")[0][:80]
            if first_line:
                title = first_line

        notion.set_database(db_id, title)
        reset_schema_cache()

        return jsonify({"saved": True, "database_id": db_id, "database_name": title})
    except Exception as e:
        return jsonify(_error_json({}, e)), 500


@bp.route("/set-database", methods=["POST"])
def set_database():
    """Set the target database for pushing recordings."""
    data = request.get_json(silent=True) or {}
    db_id = data.get("database_id", "")
    db_name = data.get("database_name", "")
    if not db_id:
        return jsonify({"error": "No database_id"}), 400
    notion.set_database(db_id, db_name)
    reset_schema_cache()
    return jsonify({"saved": True})


def _build_notion_content(t_data, names=None):
    """Build markdown content for a Notion page from transcript data.

    ``names`` (``{label: name}``) are applied to the summary and the speaker of each
    line; the stored transcript keeps its labels.
    """
    names = names or {}
    content_parts = []
    summary = t_data.get("summary", "")
    if summary:
        content_parts.append(speaker_names.apply_names(summary, names))
        content_parts.append("\n---\n")

    content_parts.append("## Full Transcript\n")
    segments = t_data.get("segments", [])
    if segments:
        for seg in segments:
            ts = f"[{int(seg.get('start', 0)) // 60:02d}:{int(seg.get('start', 0)) % 60:02d}]"
            speaker = f" **{names.get(seg['speaker'], seg['speaker'])}**:" if seg.get("speaker") else ""
            content_parts.append(f"{ts}{speaker} {seg.get('text', '').strip()}")
    else:
        content_parts.append(t_data.get("text", ""))

    return "\n".join(content_parts)


# ---------------------------------------------------------------------------
# The app-owned section of a Notion page
# ---------------------------------------------------------------------------
#
# Everything the app writes into a page body lives between a marked heading
# and a marked end line. Updates replace only that span (notion-update-page
# ``update_content`` with the exact current text as ``old_str``), so notes and
# blocks the user adds above or below it in Notion are never touched.

APP_SECTION_HEADING = "Open Pin — transcript & summary"
APP_SECTION_END = "End of Open Pin section"
# Sections written before the app was renamed carry its old name. They're still
# recognised, so an update replaces them in place (with the new markers).
_SECTION_APP_NAMES = ("Open Pin", "Plaud V2")
SESSION_ID_PROP = "Session ID"
TAGS_PROP = "Tags"

_NAMES_RE = "(?:" + "|".join(re.escape(n) for n in _SECTION_APP_NAMES) + ")"
_SECTION_START_RE = re.compile(r"^#{1,6}[ \t]+" + _NAMES_RE + r" — transcript & summary.*$", re.M)
_SECTION_END_RE = re.compile(r"^.*End of " + _NAMES_RE + r" section.*$", re.M)

# Creating a page with a long transcript can take a while.
_CREATE_TIMEOUT = 120
# How long to wait on an async create: briefly in the request path (the UI is
# waiting), longer in the background queue.
_POLL_BUDGET_REQUEST = 20.0
_POLL_BUDGET_QUEUE = 90.0
# Forget an async create task we haven't been able to resolve after a day.
_PENDING_CREATE_MAX_AGE = 24 * 3600

_sleep = _time.sleep  # patched in tests


def _build_app_section(t_data, session_id, stamp=None):
    """The app-owned block: marked heading, session line, body, end marker.

    The body shows the recording's speaker names (no AI call: labels are swapped for names).
    """
    try:
        from app.state import speakers

        names = speakers.names_for(session_id, t_data, profile=speakers.profile_name())
    except Exception as e:
        logging.warning(f"Could not read speaker names for {session_id}: {e}")
        names = {}
    heading = f"## {APP_SECTION_HEADING}" + (f" ({stamp})" if stamp else "")
    return "\n".join(
        [
            heading,
            f"*Session ID: {session_id} · Written by Open Pin. Updating from the app replaces everything from "
            f"this heading down to the end line below; add your own notes above or below it.*",
            "",
            _build_notion_content(t_data, names),
            "",
            "---",
            f"*{APP_SECTION_END}*",
        ]
    )


def _locate_app_section(content):
    """Return the exact text of the last app-owned section in ``content``, or None."""
    if not content:
        return None
    found = None
    for start in _SECTION_START_RE.finditer(content):
        end = _SECTION_END_RE.search(content, start.end())
        if end:
            found = content[start.start() : end.end()]
    return found


def _unique_tail(body, max_lines=20):
    """The shortest run of trailing lines that occurs exactly once in ``body``.

    Used as the ``old_str`` anchor to append after the end of a page without
    rewriting anything.
    """
    body = body.rstrip()
    if not body:
        return None
    lines = body.split("\n")
    for k in range(1, min(len(lines), max_lines) + 1):
        cand = "\n".join(lines[-k:])
        if cand.strip() and body.count(cand) == 1:
            return cand
    return None


# ---------------------------------------------------------------------------
# Parsing MCP tool results
# ---------------------------------------------------------------------------

_PAGE_URL_RE = re.compile(
    r"https://(?:www\.)?notion\.(?:so|com)/[^\s\"'<>\\)]+|https://[\w-]+\.notion\.site/[^\s\"'<>\\)]+"
)


def _result_text(result):
    """Concatenated text of an MCP tool result."""
    if not isinstance(result, dict):
        return str(result or "")
    if isinstance(result.get("text"), str):
        return result["text"]
    text = ""
    content = result.get("content")
    for item in content if isinstance(content, list) else [content] if content else []:
        if isinstance(item, dict):
            if isinstance(item.get("text"), str):
                text += item["text"]
        elif item:
            text += str(item)
    return text


def _result_json(result):
    """The tool result as a JSON value (structuredContent, or parsed text), or None."""
    if isinstance(result, dict) and isinstance(result.get("structuredContent"), (dict, list)):
        return result["structuredContent"]
    try:
        return json.loads(_result_text(result))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def _find_page_url(obj, depth=0):
    """First Notion page URL found under a ``url`` key, or from a page ``id``."""
    if depth > 6:
        return None
    if isinstance(obj, dict):
        url = obj.get("url")
        if isinstance(url, str) and _PAGE_URL_RE.match(url):
            return url
        for key in ("pages", "page", "result", "results", "data"):
            if key in obj:
                found = _find_page_url(obj[key], depth + 1)
                if found:
                    return found
        if "async_task" not in obj and isinstance(obj.get("id"), str) and _extract_notion_id(obj["id"]):
            if obj.get("object", "page") == "page":
                return f"https://www.notion.so/{_extract_notion_id(obj['id']).replace('-', '')}"
    elif isinstance(obj, list):
        for item in obj:
            found = _find_page_url(item, depth + 1)
            if found:
                return found
    return None


def _extract_page_url(result):
    """Extract the Notion page URL from an MCP response."""
    if not result:
        return None
    found = _find_page_url(_result_json(result))
    if found:
        return found
    m = _PAGE_URL_RE.search(_result_text(result))
    return m.group(0) if m else None


def _parse_fetch(result):
    """Split a notion-fetch result into title, properties, content and flags.

    The hosted server returns JSON ``{"title", "url", "text", ...}`` whose
    ``text`` holds ``<properties>{json}</properties>`` and
    ``<content>markdown</content>``. Anything missing comes back as None/{}.
    """
    info = {"title": None, "properties": {}, "content": None, "text": "", "truncated": False}
    raw = _result_text(result)
    inner = raw
    obj = _result_json(result)
    if isinstance(obj, dict):
        if isinstance(obj.get("title"), str):
            info["title"] = obj["title"]
        if isinstance(obj.get("text"), str):
            inner = obj["text"]
        info["truncated"] = bool(obj.get("truncated"))
    info["text"] = inner
    m = re.search(r"<properties>\s*(\{.*?\})\s*</properties>", inner, re.S)
    if m:
        try:
            props = json.loads(m.group(1))
            if isinstance(props, dict):
                info["properties"] = props
        except json.JSONDecodeError:
            pass
    m = re.search(r"<content>\n?(.*?)\n?</content>", inner, re.S)
    if m:
        info["content"] = m.group(1)
    if info["title"] is None and isinstance(info["properties"].get("title"), str):
        info["title"] = info["properties"]["title"]
    return info


def _text_value(value):
    """Plain string from a property value (string, rich-text list, or dict)."""
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, (list, dict)):
                return _text_value(parsed)
        except (json.JSONDecodeError, ValueError):
            pass
        return value.strip()
    if isinstance(value, list):
        return "".join(_text_value(v) for v in value).strip()
    if isinstance(value, dict):
        for key in ("plain_text", "text", "content", "value", "string"):
            if key in value:
                return _text_value(value[key])
    return ""


def _row_session_id(row):
    if not isinstance(row, dict):
        return ""
    props = row.get("properties") if isinstance(row.get("properties"), dict) else row
    return _text_value(props.get(SESSION_ID_PROP))


def _result_rows(result):
    obj = _result_json(result)
    if isinstance(obj, dict):
        for key in ("results", "rows", "pages", "data"):
            if isinstance(obj.get(key), list):
                return [r for r in obj[key] if isinstance(r, dict)]
        return []
    if isinstance(obj, list):
        return [r for r in obj if isinstance(r, dict)]
    return []


# ---------------------------------------------------------------------------
# Page URL / title bookkeeping
# ---------------------------------------------------------------------------


def _save_notion_page_url(session_id, url):
    """Save the Notion page URL for a recording."""
    validate_session_id(session_id)
    update_config(lambda c: c.setdefault("notion_page_urls", {}).__setitem__(session_id, url))

    # Also persist in transcript JSON for recovery after config resets
    try:
        _update_transcript_json(session_id, {"notion_page_url": url})
    except Exception as e:
        logging.warning(f"Could not save Notion page URL into transcript {session_id}: {e}")


def _get_notion_page_url(session_id):
    """Get the saved Notion page URL for a recording, if any."""
    validate_session_id(session_id)
    url = load_config().get("notion_page_urls", {}).get(session_id, "")
    if not url:
        # Try recovering from transcript JSON
        try:
            t_data = _read_transcript_json(session_id)
            url = t_data.get("notion_page_url", "") if isinstance(t_data, dict) else ""
            if url:
                # Re-save to config for future lookups (never clobber a URL saved meanwhile).
                # update_config treats a non-None return as the new config, so don't return.
                def _recover(c):
                    c.setdefault("notion_page_urls", {}).setdefault(session_id, url)

                update_config(_recover)
        except Exception:
            pass
    return url


def _forget_notion_page_url(session_id, gone_url):
    """Drop a saved page URL (config and transcript) that points at ``gone_url``.

    Only removes entries for the same page, so a URL saved meanwhile (e.g. by
    a concurrent create) survives.
    """
    validate_session_id(session_id)
    gone_id = _extract_notion_id(gone_url)

    def _same(url):
        return bool(url) and (url == gone_url or (gone_id and _extract_notion_id(url) == gone_id))

    def _drop_from_config(c):
        urls = c.get("notion_page_urls")
        if isinstance(urls, dict) and _same(urls.get(session_id)):
            del urls[session_id]

    update_config(_drop_from_config)

    def _drop_from_transcript(data):
        if _same(data.get("notion_page_url")):
            del data["notion_page_url"]
            return True
        return False

    try:
        _mutate_transcript_json(session_id, _drop_from_transcript)
    except Exception as e:
        logging.warning(f"Could not clear the Notion page URL in transcript {session_id}: {e}")


# A recording's saved page no longer exists, or is archived/in the trash.
# Notion: 404 object_not_found "Could not find page with ID: …"
# (https://developers.notion.com/reference/status-codes); editing a trashed
# page is a 400 validation_error "Can't edit block that is archived. You must
# unarchive the block before editing."
_PAGE_GONE_MARKERS = (
    "object_not_found",
    "could not find page",
    "could not find block",
    "that is archived",
    "is archived",
    "is in trash",
    "in_trash",
)


def _is_page_gone_error(err):
    """True if ``err`` (exception or message) says the page was deleted/archived.

    Checks the message (MCP ``isError`` text) and, for HTTP errors, the body.
    """
    text = str(err or "")
    response = getattr(err, "response", None)
    if response is not None:
        text += " " + str(getattr(response, "text", "") or "")
    lower = text.lower()
    return any(marker in lower for marker in _PAGE_GONE_MARKERS)


@bp.route("/page-url/<session_id>")
def page_url(session_id):
    """Get the Notion page URL for a recording."""
    try:
        validate_session_id(session_id)
    except InvalidSessionIdError:
        return _invalid_session_id()
    url = _get_notion_page_url(session_id)
    out = {"url": url, "has_page": bool(url)}
    if not url:
        from app.utils import notion_queue

        review = notion_queue.needs_review_for(session_id)
        if review:
            out["needs_review"] = True
            out["candidates"] = review.get("candidates") or []
            # No candidates: the check itself couldn't run (MATCH_UNCHECKED).
            out["message"] = _needs_review(session_id, out["candidates"], unchecked=not out["candidates"])["message"]
    return jsonify(out)


def _save_notion_pushed_title(session_id, title):
    """Store the title we last pushed to Notion for this recording."""
    update_config(lambda c: c.setdefault("notion_pushed_titles", {}).__setitem__(session_id, title))


def _get_notion_pushed_title(session_id):
    """Get the title we last pushed to Notion, or empty string."""
    return load_config().get("notion_pushed_titles", {}).get(session_id, "")


def _fetch_notion_page_title(page_url):
    """Fetch the current title of a Notion page. Returns the title or None on error."""
    try:
        page_id = _extract_page_id(page_url) or page_url
        title = _parse_fetch(notion.mcp_call("notion-fetch", {"id": page_id}))["title"]
        return title.strip() if isinstance(title, str) else None
    except Exception as e:
        logging.warning(f"Could not fetch Notion page title: {e}")
    return None


def _extract_page_id(url_or_id):
    """Extract a dashed UUID page_id from a Notion URL or bare id."""
    return _extract_notion_id(url_or_id)


def _save_pending_create(session_id, task_id):
    update_config(
        lambda c: c.setdefault("notion_pending_creates", {}).__setitem__(
            session_id, {"task_id": task_id, "started_at": _time.time()}
        )
    )


def _clear_pending_create(session_id):
    update_config(lambda c: c.setdefault("notion_pending_creates", {}).pop(session_id, None))


def _get_pending_create(session_id):
    pending = load_config().get("notion_pending_creates", {}).get(session_id)
    return pending if isinstance(pending, dict) and pending.get("task_id") else None


# ---------------------------------------------------------------------------
# Database schema: add-only, never from a stale or empty view
# ---------------------------------------------------------------------------

# Last schema we read, keyed by database id. Only used to skip work when it
# already shows everything we need; any schema *change* is preceded by a fresh
# fetch. Reset when the database changes (and ignored if the id differs).
_schema_cache = None
# Guards check-then-change on the schema: push_to_notion runs on both Flask
# request threads and the notion_queue worker.
_schema_lock = threading.Lock()


def reset_schema_cache():
    global _schema_cache
    with _schema_lock:
        _schema_cache = None
    reset_rows_cache()


def _fetch_db_schema(db_id):
    """Read the live schema of the selected database.

    Returns a dict, or None if the response has no schema we can parse.
    Raises if the fetch itself fails — callers must not change the schema then.
    """
    result = notion.mcp_call("notion-fetch", {"id": db_id})
    raw = _result_text(result)
    obj = _result_json(result)
    text = obj["text"] if isinstance(obj, dict) and isinstance(obj.get("text"), str) else raw
    m = re.search(r"<data-source-state>\s*(\{.*?\})\s*</data-source-state>", text, re.DOTALL)
    if not m:
        logging.warning("Notion database fetch had no <data-source-state>; leaving the schema alone")
        return None
    try:
        state = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    if not isinstance(state, dict):
        return None
    schema = state.get("schema") if isinstance(state.get("schema"), dict) else {}

    ds = re.search(r"collection://([0-9a-fA-F-]{32,36})", text)
    tags = schema.get(TAGS_PROP)
    sess = schema.get(SESSION_ID_PROP)
    tags_exists = isinstance(tags, dict) and tags.get("type") == "multi_select"
    options, options_known = {}, True
    if tags_exists:
        options, options_known = _parse_tag_options(tags)
        if options_known and not options and _text_suggests_tag_options(text[: m.start()] + text[m.end() :]):
            options_known = False
        if not options_known:
            logging.warning(
                "Couldn't read the Tags options from the Notion database schema; treating them as unknown "
                "(no option changes this time)"
            )
    return {
        "db_id": db_id,
        "data_source_id": ds.group(1) if ds else None,
        "tags_exists": tags_exists,
        "tags_conflict": isinstance(tags, dict) and tags.get("type") != "multi_select",
        "tag_options": options,
        # False: the Tags column exists but its option list couldn't be read
        # (missing, malformed, or contradicted by the rest of the response).
        # Then "SET MULTI_SELECT(...)" must not be sent: it replaces the list.
        "tag_options_known": options_known,
        "session_prop": isinstance(sess, dict) and sess.get("type") in ("text", "rich_text"),
        "session_conflict": isinstance(sess, dict) and sess.get("type") not in ("text", "rich_text"),
    }


def _parse_tag_options(prop):
    """``({name: color}, known)`` for a multi-select property's options.

    ``known`` is False when the list is missing or any entry is unreadable:
    a partial list must never be written back.
    """
    raw = prop.get("options")
    if raw is None and isinstance(prop.get("multi_select"), dict):
        raw = prop["multi_select"].get("options")  # REST-API shape
    if not isinstance(raw, list):
        return {}, False
    options = {}
    for opt in raw:
        if not (isinstance(opt, dict) and isinstance(opt.get("name"), str)):
            return options, False
        options[opt["name"]] = opt.get("color") if isinstance(opt.get("color"), str) else "default"
    return options, True


_TAGS_Q = re.escape(TAGS_PROP)
# Signs, outside the parsed schema JSON, that the Tags column has options:
# a JSON property object with a non-empty "options" list, or DDL listing them.
_TAG_OPTION_HINTS = (
    re.compile(r'"' + _TAGS_Q + r'"\s*:\s*\{[^{}]*?(?:\{[^{}]*?)?"options"\s*:\s*\[\s*[^\]\s]'),
    re.compile(r"[\"']" + _TAGS_Q + r"[\"']\s+MULTI_SELECT\s*\(\s*'", re.I),
)
_TAG_OPTION_LINE_HINT = re.compile(r"\boptions?\b\W{0,5}[\[({]?\s*[\"'{]", re.I)


def _text_suggests_tag_options(text):
    if any(rx.search(text) for rx in _TAG_OPTION_HINTS):
        return True
    for line in text.splitlines():
        if (f'"{TAGS_PROP}"' in line or f"'{TAGS_PROP}'" in line) and _TAG_OPTION_LINE_HINT.search(line):
            return True
    return False


# Notion option names: "Commas are not allowed" (property object reference,
# Select options; page property values: "Option names cannot contain
# commas"). Longer names fail validation with "name.length should be ≤ 100".
_TAG_MAX_LEN = 100


def _sanitize_tags(tags):
    """Tag names Notion accepts as multi-select options, in order.

    Commas become spaces, whitespace is collapsed, names are cut to 100
    characters, and empty or case-insensitive duplicates (Notion matches
    option names without regard to case) are dropped.
    """
    out, seen = [], set()
    for tag in tags or []:
        if not isinstance(tag, str):
            continue
        name = re.sub(r"\s+", " ", tag.replace(",", " ")).strip()
        if len(name) > _TAG_MAX_LEN:
            name = name[:_TAG_MAX_LEN].rstrip()
        if name != tag.strip():
            logging.warning(f"Tag {tag!r} isn't a valid Notion option name; sending it as {name!r}")
        key = name.casefold()
        if not name or key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


def _writable_tags(tags, schema):
    """Tags safe to write to a page, or None to leave the page's Tags alone.

    With an unknown option list, only tags known to exist (read from the
    partial list) are sent. If none are known the property is left untouched:
    an empty list would clear the page's tags.
    """
    if not tags or not schema or not schema.get("tags_exists") or schema.get("tag_options_known", True):
        return tags
    existing = {n.casefold() for n in schema["tag_options"]}
    known = [t for t in tags if t.casefold() in existing]
    if len(known) < len(tags):
        logging.warning(
            f"Not writing tags {sorted(set(tags) - set(known))} to Notion: the database's Tags options couldn't "
            "be read, so they can't safely be added"
        )
    return known or None


def _options_sql(options):
    """``'name':color, ...`` for a MULTI_SELECT DDL clause (quotes doubled)."""
    parts = []
    for name in sorted(options):
        color = options[name] if re.fullmatch(r"[a-z_]+", str(options[name] or "")) else "default"
        parts.append("'" + name.replace("'", "''") + "':" + color)
    return ", ".join(parts)


def _schema_satisfies(schema, needed_tags):
    if not (schema["session_prop"] or schema["session_conflict"]):
        return False
    if not needed_tags or schema["tags_conflict"]:
        return True
    if not schema.get("tag_options_known", True):
        return False  # re-read next time; maybe the list parses then
    existing = {name.casefold() for name in schema["tag_options"]}
    return schema["tags_exists"] and all(t.casefold() in existing for t in needed_tags)


# Failures worth retrying as-is: rate limits, timeouts, Notion/gateway
# outages, conflicts (status-codes reference: rate_limited 429,
# conflict_error 409, internal_server_error 500, bad_gateway 502,
# service_unavailable 503, gateway_timeout 504, service_overload 529).
_TRANSIENT_MARKERS = (
    "rate limit",
    "rate_limited",
    "conflict",
    "timeout",
    "timed out",
    "try again",
    "unavailable",
    "overloaded",
    "bad gateway",
    "internal_server_error",
    "unexpected error",
    "connection",
    # Auth trouble isn't the options' fault (the queue pauses on these).
    "session expired",
    "unauthorized",
    "not connected",
)


def _is_transient_error(err):
    if isinstance(err, (requests.ConnectionError, requests.Timeout)):
        return True
    status = getattr(getattr(err, "response", None), "status_code", None)
    if isinstance(status, int) and (status in (408, 409, 429) or status >= 500):
        return True
    lower = str(err).lower()
    return any(marker in lower for marker in _TRANSIENT_MARKERS)


def _ensure_schema(tags):
    """Make sure the database has a Session ID text column and, when this
    recording has tags, a Tags multi-select with every one of them as an option.

    Rules (a mistake here deletes the user's tag options from *every* page):
    - The schema is re-fetched right before any change; if that fetch fails we
      raise and change nothing (the push is retried later).
    - Changes only ever add: a missing column, or missing options. Notion's
      DDL ``SET MULTI_SELECT(...)`` replaces the whole option list, so we send
      the union of the freshly-fetched options (keeping their colours) and the
      new ones — never a list built from a cache.
    - If the fresh option list can't be read (missing/malformed, or empty
      while the response mentions options), it is unknown and never SET.
    - No Tags column is created for a recording without tags.
    - A transient failure to add options raises a retryable error; Notion
      rejecting the options themselves raises a permanent one.

    Returns the schema dict (as after the changes), or None if the database
    layout couldn't be read (then no change is attempted).
    """
    global _schema_cache
    db_id = notion.get_database_id()
    if not db_id:
        return None
    needed = set(_sanitize_tags(tags))
    with _schema_lock:
        cache = _schema_cache if _schema_cache and _schema_cache.get("db_id") == db_id else None
        if cache and _schema_satisfies(cache, needed):
            return cache

        try:
            fresh = _fetch_db_schema(db_id)
        except Exception as e:
            _schema_cache = None
            raise ValueError(f"Could not read the Notion database schema: {e}") from e
        if fresh is None:
            _schema_cache = None
            return None

        target = fresh["data_source_id"] or db_id
        option_error = None

        def _run(statement):
            try:
                notion.mcp_call("notion-update-data-source", {"data_source_id": target, "statements": statement})
                return None
            except Exception as e:
                if any(w in str(e).lower() for w in ("already exists", "duplicate")):
                    return None
                logging.warning(f"Notion schema change failed ({statement[:60]}…): {e}")
                return e

        if needed and not fresh["tags_conflict"]:
            if not fresh["tags_exists"]:
                new_opts = dict.fromkeys(needed, "default")
                option_error = _run(f'ADD COLUMN "{TAGS_PROP}" MULTI_SELECT({_options_sql(new_opts)})')
                if option_error is None:
                    fresh["tags_exists"] = True
                    fresh["tag_options"] = new_opts
            elif not fresh.get("tag_options_known", True):
                # Never SET the list from a view we couldn't read: it would
                # replace the user's options. The page gets known tags only.
                logging.warning("Not adding Notion tag options: the current option list couldn't be read")
            else:
                existing = {name.casefold() for name in fresh["tag_options"]}
                missing = {t for t in needed if t.casefold() not in existing}
                if missing:
                    union = dict(fresh["tag_options"])
                    union.update(dict.fromkeys(missing, "default"))
                    option_error = _run(f'ALTER COLUMN "{TAGS_PROP}" SET MULTI_SELECT({_options_sql(union)})')
                    if option_error is None:
                        fresh["tag_options"] = union

        if not fresh["session_prop"] and not fresh["session_conflict"]:
            if _run(f'ADD COLUMN "{SESSION_ID_PROP}" RICH_TEXT') is None:
                fresh["session_prop"] = True

        _schema_cache = fresh
        if option_error is not None:
            if _is_transient_error(option_error):
                # Writing tags Notion doesn't know would fail validation; retry later.
                raise ValueError(f"Could not add tag options to the Notion database: {option_error}")
            # Notion refused the options themselves: retrying the same names
            # fails the same way, so the queue gives this job up.
            raise ValueError(f"Notion rejected the tag options for this recording: {option_error}")
        return fresh


def _page_properties(title, tags, session_id, schema):
    """Properties for a page write. ``title`` None leaves the title alone.

    Notion MCP takes multi-select values as a JSON-array string. Tags are only
    sent when the column exists (an empty list clears them, None leaves them
    alone); with an unknown schema, only non-empty tags are sent.
    """
    props = {}
    if title is not None:
        props["title"] = title
    if schema is None:
        if tags:
            props[TAGS_PROP] = json.dumps(tags)
        return props
    if schema["tags_exists"] and tags is not None:
        props[TAGS_PROP] = json.dumps(tags)
    if schema["session_prop"]:
        props[SESSION_ID_PROP] = session_id
    return props


def _resolve_title_for_update(session_id, app_title, page_url):
    """Decide what title to send when updating an existing Notion page.

    If the user renamed the page on Notion's side, we don't overwrite it.
    We compare the current Notion title against what we last pushed.
    - If Notion title == last pushed title → user hasn't changed it on Notion → safe to update
    - If Notion title != last pushed title → user customised it on Notion → keep Notion's title
    """
    last_pushed = _get_notion_pushed_title(session_id)
    if not last_pushed:
        # First update after this feature was added — assume safe to update
        return app_title

    if last_pushed == app_title:
        # Title hasn't changed in our app — no update needed, use current
        return app_title

    # Title changed in our app — check if user also changed it on Notion
    notion_title = _fetch_notion_page_title(page_url)
    if notion_title is None:
        # Couldn't fetch — play it safe, update with our title
        return app_title
    if notion_title == last_pushed:
        # Notion still has what we pushed last time → user didn't change it → safe to update
        return app_title
    else:
        # User changed the title on Notion — don't overwrite
        logging.info(f"Notion title differs from last push ('{notion_title}' vs '{last_pushed}'), keeping Notion title")
        return None  # Signal: don't include title in properties


# ---------------------------------------------------------------------------
# Finding an existing page (upsert) and async creates
# ---------------------------------------------------------------------------


def _page_has_session_id(url, session_id):
    page_id = _extract_page_id(url)
    if not page_id:
        return False
    info = _parse_fetch(notion.mcp_call("notion-fetch", {"id": page_id}))
    if _text_value(info["properties"].get(SESSION_ID_PROP)) == session_id:
        return True
    body = info["content"] if info["content"] is not None else info["text"]
    return bool(re.search(r"Session ID:\s*`?" + re.escape(session_id) + r"(?![\w.-])", body or ""))


def _find_page_by_session_id(session_id, schema):
    """URL of an existing page for this recording, found by its Session ID.

    Tries a rows query of the data source filtered on Session ID, then a
    keyword search for the id (the app section of every page we create
    contains it). Every candidate is checked before it's trusted. Raises if
    every lookup method failed, so the caller doesn't create a duplicate
    blindly; returns None if nothing matched.
    """
    tried = failed = 0
    ds = (schema or {}).get("data_source_id")
    if schema and schema.get("session_prop") and ds:
        tried += 1
        try:
            result = notion.mcp_call(
                "notion-query-data-sources",
                {
                    "data": {
                        "mode": "rows",
                        "data_source_url": f"collection://{ds}",
                        "filter": {"property": SESSION_ID_PROP, "rich_text": {"equals": session_id}},
                        "limit": 10,
                    }
                },
            )
            for row in _result_rows(result):
                if _row_session_id(row) == session_id:
                    url = _find_page_url(row)
                    if url:
                        return url
        except Exception as e:
            failed += 1
            logging.info(f"Notion rows query by Session ID failed (falling back to search): {e}")

    tried += 1
    try:
        result = notion.mcp_call("notion-search", {"query": session_id, "query_type": "internal", "filters": {}})
        for cand in _result_rows(result)[:5]:
            url = _find_page_url(cand)
            if not url:
                continue
            if _row_session_id(cand) == session_id or _page_has_session_id(url, session_id):
                return url
    except Exception as e:
        failed += 1
        logging.warning(f"Notion search by Session ID failed: {e}")

    if failed == tried:
        raise ValueError("Could not check Notion for an existing page for this recording")
    return None


def _async_task(result):
    obj = _result_json(result)
    if isinstance(obj, dict) and isinstance(obj.get("async_task"), dict) and obj["async_task"].get("id"):
        return obj["async_task"]
    return None


def _poll_async_task(task_id, budget, first_wait=2.0):
    """Poll notion-get-async-task until it finishes or ``budget`` seconds pass.

    Returns ("succeeded", result) / ("failed", message) / ("running", None).
    """
    deadline = _time.monotonic() + max(0.0, budget)
    wait = first_wait
    while True:
        wait = max(1.0, min(float(wait or 2.0), 15.0))
        if _time.monotonic() + wait > deadline:
            return "running", None
        _sleep(wait)
        result = notion.mcp_call("notion-get-async-task", {"task_id": task_id})
        obj = _result_json(result)
        obj = obj if isinstance(obj, dict) else {}
        task = obj.get("async_task") if isinstance(obj.get("async_task"), dict) else obj
        status = str(task.get("status") or "").lower()
        if status == "succeeded":
            return "succeeded", result
        if status == "failed":
            err = task.get("error")
            if isinstance(err, dict):
                err = err.get("message") or json.dumps(err)
            return "failed", str(err or "Notion could not create the page")
        wait = task.get("poll_after_seconds") or wait


def _resume_pending_create(session_id, budget):
    """Finish a create that Notion accepted asynchronously earlier.

    Returns (url, still_pending).
    """
    pending = _get_pending_create(session_id)
    if not pending:
        return None, False
    if _time.time() - float(pending.get("started_at") or 0) > _PENDING_CREATE_MAX_AGE:
        _clear_pending_create(session_id)
        return None, False
    try:
        status, payload = _poll_async_task(pending["task_id"], budget, first_wait=1.0)
    except ValueError as e:
        # Unknown/expired task: forget it; the Session ID lookup finds the page if it exists.
        logging.info(f"Notion async task {pending['task_id']} no longer available: {e}")
        _clear_pending_create(session_id)
        return None, False
    if status == "running":
        return None, True
    _clear_pending_create(session_id)
    if status == "succeeded":
        url = _extract_page_url(payload)
        if url:
            _save_notion_page_url(session_id, url)
        return url, False
    logging.warning(f"Notion async create for {session_id} failed: {payload}")
    return None, False


# ---------------------------------------------------------------------------
# Writing pages
# ---------------------------------------------------------------------------


def _create_page(session_id, t_data, app_title, tags, schema, poll_budget):
    properties = _page_properties(app_title, tags, session_id, schema)
    result = notion.mcp_call(
        "notion-create-pages",
        {
            "parent": {"type": "database_id", "database_id": notion.get_database_id()},
            "pages": [
                {
                    "properties": properties,
                    "content": _build_app_section(t_data, session_id),
                    "icon": "\U0001f399️",
                }
            ],
            # Large pages: let Notion queue the write and hand back a task to
            # poll, rather than holding the HTTP request open.
            "allow_async": True,
        },
        timeout=_CREATE_TIMEOUT,
    )
    task = _async_task(result)
    if task:
        # Persist before polling: if we crash or time out now, the next
        # attempt polls this task instead of creating a second page.
        _save_pending_create(session_id, task["id"])
        status, payload = _poll_async_task(task["id"], poll_budget, first_wait=task.get("poll_after_seconds") or 2)
        if status == "running":
            _save_notion_pushed_title(session_id, app_title)
            return {"pending": True, "message": "Notion is still creating the page"}
        _clear_pending_create(session_id)
        if status == "failed":
            raise ValueError(f"Notion could not create the page: {payload}")
        result = payload

    page_url = _extract_page_url(result)
    if not page_url:
        try:
            page_url = _find_page_by_session_id(session_id, schema)
        except Exception:
            page_url = None
    if page_url:
        _save_notion_page_url(session_id, page_url)
    _save_notion_pushed_title(session_id, app_title)
    return {"pushed": True, "updated": False, "url": page_url or ""}


def _update_app_section(page_id, t_data, session_id, append=True):
    """Replace only the app-owned section of an existing page.

    Returns ``(outcome, note)``; outcome is "replaced", "unchanged",
    "appended", "skipped" or "kept". Nothing outside the section is ever
    deleted: if the section can't be found or replaced (e.g. pages made by
    older versions, or the user removed the markers), a new dated section is
    appended after the existing content instead, unless ``append`` is False
    (a legacy page the recording adopted, whose body already holds the
    transcript): then the body is left as it is ("kept").
    """
    section = _build_app_section(t_data, session_id)
    info = _parse_fetch(notion.mcp_call("notion-fetch", {"id": page_id}))
    content = info["content"]
    haystack = content if content is not None else info["text"]

    old = _locate_app_section(haystack)
    if old is not None:
        if old.strip() == section.strip():
            return "unchanged", ""
        try:
            notion.mcp_call(
                "notion-update-page",
                {
                    "page_id": page_id,
                    "command": "update_content",
                    "properties": {},
                    "content_updates": [{"old_str": old, "new_str": section}],
                },
                timeout=_CREATE_TIMEOUT,
            )
            return "replaced", ""
        except ValueError as e:
            if not append:
                logging.warning(f"Could not replace the Open Pin section in place ({e}); leaving the page as it is")
                return "kept", ""
            logging.warning(f"Could not replace the Open Pin section in place ({e}); appending a new one")

    if not append:
        return "kept", ""

    stamp = _time.strftime("%Y-%m-%d %H:%M")
    dated = _build_app_section(t_data, session_id, stamp=stamp)
    note = "Added an updated transcript section at the end of the page; your earlier content was kept."
    if content is not None and not info["truncated"] and not content.strip():
        # Empty page: nothing to lose.
        notion.mcp_call(
            "notion-update-page",
            {
                "page_id": page_id,
                "command": "replace_content",
                "properties": {},
                "content_updates": [],
                "new_str": dated,
            },
            timeout=_CREATE_TIMEOUT,
        )
        return "appended", ""
    anchor = _unique_tail(content) if content is not None and not info["truncated"] else None
    if anchor is None:
        return (
            "skipped",
            "Couldn't find a safe place in the Notion page to add the transcript; only properties were updated.",
        )
    notion.mcp_call(
        "notion-update-page",
        {
            "page_id": page_id,
            "command": "update_content",
            "properties": {},
            "content_updates": [{"old_str": anchor, "new_str": anchor + "\n\n" + dated}],
        },
        timeout=_CREATE_TIMEOUT,
    )
    return "appended", note


# ---------------------------------------------------------------------------
# Pages made by older app versions (before v2.4.0: no Session ID)
# ---------------------------------------------------------------------------
#
# If the app's data is lost and the recordings are synced and transcribed
# again, their old pages can only be recognised by content. Before creating a
# page, push_to_notion lists the database rows, fingerprints the pages without
# a Session ID (cached in .notion_legacy_cache.json, see
# app/utils/notion_legacy.py) and scores them against the local transcript.
#
# Adopting a legacy page (automatically on a confident match, or when the
# user picks it):
# - sets its Session ID property (and merges its tags with the recording's;
#   the page's tags are also added to the recording in the app, so later
#   pushes don't drop them); the title is left as it is;
# - saves its URL locally and records the adoption in the config
#   (``notion_legacy_pages``), keyed by session id.
# The body is never changed: it already holds the transcript, and adding the
# app section would put a second copy on the page. Later pushes of an adopted
# page update its properties only, plus the app section if one is ever found
# on it (replaced in place, never appended). Its Notion title is kept until
# the recording is renamed in the app; from then on the usual rule applies
# (_resolve_title_for_update).

LEGACY_PAGES_KEY = "notion_legacy_pages"

# Rows listing: page size, a cap on pages, and how long a listing is reused
# (pushing a batch of recordings lists the database once, not once each).
_ROWS_PAGE_SIZE = 100
_ROWS_MAX_PAGES = 50
_ROWS_CACHE_TTL = 600

_rows_cache = {}
_rows_lock = threading.Lock()

_CREATED_KEYS = ("created_time", "createdTime", "created")
_EDITED_KEYS = ("last_edited_time", "lastEditedTime", "last_edited", "edited")


def reset_rows_cache():
    with _rows_lock:
        _rows_cache.clear()


def _row_field(row, keys):
    for source in (row, row.get("properties") if isinstance(row.get("properties"), dict) else {}):
        for key in keys:
            value = source.get(key)
            if isinstance(value, (str, int, float)) and value:
                return value
            if isinstance(value, dict):
                for inner in ("created_time", "last_edited_time", "date", "start"):
                    if isinstance(value.get(inner), str) and value[inner]:
                        return value[inner]
    return None


def _row_page_id(row):
    for key in ("id", "page_id"):
        value = row.get(key)
        if isinstance(value, str) and _extract_notion_id(value):
            return _extract_notion_id(value)
    url = _find_page_url(row)
    return _extract_notion_id(url) if url else None


def _row_title(row):
    props = row.get("properties") if isinstance(row.get("properties"), dict) else row
    for key in ("title", "Name", "Title", "name"):
        title = _text_value(props.get(key))
        if title:
            return title
    return ""


def _list_db_rows(ds, refresh=False):
    """All rows of the data source ``ds`` (paginated, reused for a while).

    Returns ``(rows, complete)``; ``complete`` is False if the listing was
    cut off at _ROWS_MAX_PAGES. Raises if a page of the listing fails.
    """
    with _rows_lock:
        hit = _rows_cache.get(ds)
        if hit and not refresh and _time.monotonic() - hit[0] < _ROWS_CACHE_TTL:
            return list(hit[1]), hit[2]
        rows, seen_ids, seen_cursors, cursor, complete = [], set(), set(), None, False
        for _ in range(_ROWS_MAX_PAGES):
            data = {"mode": "rows", "data_source_url": f"collection://{ds}", "limit": _ROWS_PAGE_SIZE}
            if cursor:
                data["start_cursor"] = cursor
            result = notion.mcp_call("notion-query-data-sources", {"data": data})
            for row in _result_rows(result):
                pid = _row_page_id(row)
                if pid and pid not in seen_ids:
                    seen_ids.add(pid)
                    rows.append(row)
            obj = _result_json(result)
            obj = obj if isinstance(obj, dict) else {}
            nxt = obj.get("next_cursor") or obj.get("nextCursor")
            if not (nxt and obj.get("has_more", obj.get("hasMore", True))) or nxt in seen_cursors:
                complete = True
                break
            seen_cursors.add(nxt)
            cursor = nxt
        if not complete:
            logging.warning(
                f"Notion database has more than {_ROWS_MAX_PAGES * _ROWS_PAGE_SIZE} rows; checked the first"
            )
        _rows_cache[ds] = (_time.monotonic(), rows, complete)
        return list(rows), complete


def _tag_values(value):
    """Tag names from a multi-select property value (JSON string or list)."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return [t.strip() for t in value.split(",") if t.strip()]
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        name = item.get("name") if isinstance(item, dict) else item
        if isinstance(name, str) and name.strip():
            out.append(name.strip())
    return out


def _page_entry(page_id, row, info, db_id, edited):
    """Cache entry (fingerprint plus facts) for a fetched page."""
    content = info["content"] if info["content"] is not None else info["text"]
    props = info["properties"]
    session = _text_value(props.get(SESSION_ID_PROP)) or _row_session_id(row)
    section = _locate_app_section(content or "")
    if section and not session:
        m = re.search(r"Session ID:\s*`?([\w.-]+)", section)
        session = m.group(1) if m else ""
    entry = notion_legacy.fingerprint_page(section or content or "")
    entry.update(
        {
            "edited": edited,
            "fetched_at": _time.time(),
            "db": db_id,
            "url": _find_page_url(row) or f"https://www.notion.so/{page_id.replace('-', '')}",
            "title": (info["title"] or _row_title(row) or "").strip(),
            "created": notion_legacy.parse_time(_row_field(row, _CREATED_KEYS)),
            # "app": has the app section (made by v2.4.0+); "legacy": doesn't.
            "kind": "app" if section else "legacy",
            "session_id": session,
            "truncated": bool(info["truncated"]),
        }
    )
    return entry


def _drop_cached_page(page_id):
    reset_rows_cache()
    cache = notion_legacy.load_cache()
    if cache.get("pages", {}).pop(page_id, None) is not None:
        notion_legacy.save_cache(cache)


def _page_fingerprints(rows, db_id, wanted=None, prune=False):
    """Fingerprints of the rows' pages, from the cache or fetched one by one.

    Pages that are gone or can't be read are skipped; a transient failure
    raises (the push is retried later rather than matching on a partial view).
    ``prune``: ``rows`` is the whole database, so cached pages of this
    database that aren't in it any more are dropped.
    """
    cache = notion_legacy.load_cache()
    pages = cache.setdefault("pages", {})
    out, fetched = [], 0
    if prune:
        listed = {_row_page_id(r) for r in rows}
        stale = [pid for pid, e in pages.items() if isinstance(e, dict) and e.get("db") == db_id and pid not in listed]
        for pid in stale:
            del pages[pid]
        fetched = len(stale)  # forces a save
    try:
        for row in rows:
            pid = _row_page_id(row)
            if not pid or (wanted and not wanted(row, pid)):
                continue
            edited = _row_field(row, _EDITED_KEYS)
            edited = str(edited) if edited else None
            entry = notion_legacy.cached_entry(cache, pid, edited)
            if entry is None:
                try:
                    info = _parse_fetch(notion.mcp_call("notion-fetch", {"id": pid}))
                except Exception as e:
                    if _is_page_gone_error(e):
                        pages.pop(pid, None)
                        continue
                    if _is_transient_error(e):
                        raise
                    logging.warning(f"Could not read Notion page {pid} to compare it: {e}")
                    continue
                entry = _page_entry(pid, row, info, db_id, edited)
                pages[pid] = entry
                fetched += 1
                if fetched % 10 == 0:
                    notion_legacy.save_cache(cache)
            out.append(dict(entry, page_id=pid))
    finally:
        if fetched:
            notion_legacy.save_cache(cache)
    return out


def _linked_page_ids(except_session=None):
    """Ids of pages already saved as some recording's page."""
    urls = load_config().get("notion_page_urls", {})
    return {_extract_notion_id(u) for s, u in urls.items() if s != except_session and isinstance(u, str)} - {None}


def _match_legacy_page(session_id, t_data, schema):
    """Look for a legacy page holding this recording.

    Returns ``(outcome, candidates)`` as notion_legacy.classify does; each
    candidate has page_id, url, title, score and duration_ok. Raises on a
    transient Notion failure (the queue retries later). If the database can't
    be checked at all, returns MATCH_UNCHECKED: the caller asks the user rather
    than risk a duplicate (they can still choose "Create a new page").
    """
    local = notion_legacy.fingerprint_local(t_data)
    if local["count"] < notion_legacy.MIN_SHINGLES:
        return notion_legacy.MATCH_NONE, []
    ds = (schema or {}).get("data_source_id")
    db_id = notion.get_database_id()
    if not ds:
        logging.warning(f"No data source id for the Notion database; can't look for older pages of {session_id}")
        return MATCH_UNCHECKED, []
    try:
        rows, _complete = _list_db_rows(ds)
    except Exception as e:
        if _is_transient_error(e):
            raise
        logging.warning(f"Couldn't list the Notion database to look for older pages of {session_id}: {e}")
        return MATCH_UNCHECKED, []

    taken = _linked_page_ids(except_session=session_id)
    entries = _page_fingerprints(rows, db_id, wanted=lambda row, pid: not _row_session_id(row) and pid not in taken)
    scored = []
    for entry in entries:
        if entry["kind"] != "legacy" or entry.get("session_id"):
            continue
        result = notion_legacy.score_candidate(local, entry, session_id)
        if result is None:
            continue
        scored.append(
            {
                "page_id": entry["page_id"],
                "url": entry["url"],
                "title": entry.get("title") or "",
                "score": result["score"],
                "duration_ok": result["duration_ok"],
            }
        )
    outcome, found = notion_legacy.classify(scored)
    if outcome != notion_legacy.MATCH_NONE:
        summary = ", ".join(f"{c['page_id']} ({c['score']:.2f})" for c in found)
        logging.info(f"Older Notion page(s) for {session_id}: {outcome}: {summary}")
    return outcome, found


# _match_legacy_page couldn't read the database: ask, don't create.
MATCH_UNCHECKED = "unchecked"


def _needs_review(session_id, candidates, unchecked=False):
    return {
        "pushed": False,
        "needs_review": True,
        "url": "",
        "message": (
            "Couldn't check Notion for an older page of this recording, so nothing was created."
            if unchecked
            else "This recording may already be in Notion, on a page an older version of the app made."
        ),
        "candidates": [
            {"url": c["url"], "title": c.get("title") or "", "score": c["score"], "duration_ok": c["duration_ok"]}
            for c in candidates
        ],
    }


def _get_legacy_adoption(session_id, page_id=None):
    record = load_config().get(LEGACY_PAGES_KEY, {}).get(session_id)
    if not isinstance(record, dict):
        return None
    if page_id and record.get("page_id") != page_id:
        return None
    return record


def _release_adopted_title(session_id):
    """The recording was renamed in the app: from now on its title follows
    the usual rule (no longer kept as the adopted page had it)."""

    def _drop(c):
        record = c.get(LEGACY_PAGES_KEY, {}).get(session_id)
        if isinstance(record, dict):
            record.pop("app_title", None)

    update_config(_drop)


def _forget_legacy_adoption(session_id, page_id):
    def _drop(c):
        records = c.get(LEGACY_PAGES_KEY)
        if isinstance(records, dict) and (records.get(session_id) or {}).get("page_id") == page_id:
            del records[session_id]

    update_config(_drop)


def _import_page_tags(session_id, page_tags):
    """Add a legacy page's tags to the recording in the app (add-only)."""
    if not page_tags:
        return

    def _merge_tags(current):
        current = [t for t in (current or []) if isinstance(t, str)]
        have = {t.casefold() for t in current}
        return current + [t for t in page_tags if t.casefold() not in have]

    def _config(c):
        rec = c.setdefault("recording_tags", {})
        rec[session_id] = _merge_tags(rec.get(session_id))
        custom = c.setdefault("custom_tags", [])
        for tag in page_tags:
            if tag not in DEFAULT_TAGS and tag not in custom:
                custom.append(tag)

    update_config(_config)

    def _transcript(data):
        merged = _merge_tags(data.get("tags"))
        if merged == data.get("tags"):
            return False
        data["tags"] = merged
        return True

    try:
        _mutate_transcript_json(session_id, _transcript)
    except Exception as e:
        logging.warning(f"Could not add the Notion page's tags to transcript {session_id}: {e}")


def _adopt_legacy_page(session_id, page_url, app_title, tags, schema, how, info=None):
    """Make an existing page this recording's page without touching its body.

    Sets the Session ID (and merged tags), saves the URL and records the
    adoption. See the comment at the top of this section.
    """
    page_id = _extract_page_id(page_url)
    if info is None:
        info = _parse_fetch(notion.mcp_call("notion-fetch", {"id": page_id}))
    page_tags = _sanitize_tags(_tag_values(info["properties"].get(TAGS_PROP)))
    merged = _sanitize_tags(list(tags or []) + page_tags)
    props = _page_properties(None, _writable_tags(merged, schema) or None, session_id, schema)
    if props:
        try:
            notion.mcp_call(
                "notion-update-page",
                {"page_id": page_id, "command": "update_properties", "properties": props, "content_updates": []},
            )
        except Exception:
            reset_schema_cache()
            raise
    _save_notion_page_url(session_id, page_url)
    title = (info["title"] or "").strip()
    if title:
        _save_notion_pushed_title(session_id, title)
    record = {"page_id": page_id, "adopted_at": _time.time(), "how": how, "app_title": app_title}
    update_config(lambda c: c.setdefault(LEGACY_PAGES_KEY, {}).__setitem__(session_id, record))
    _import_page_tags(session_id, page_tags)

    cache = notion_legacy.load_cache()
    entry = cache.get("pages", {}).get(page_id)
    if isinstance(entry, dict):
        entry["session_id"] = session_id
        entry["edited"] = None  # setting the property changed it; re-read next time
        notion_legacy.save_cache(cache)

    logging.info(f"Linked recording {session_id} to the older Notion page {page_id} ({how}); its content was kept")
    return {
        "pushed": True,
        "updated": True,
        "adopted": True,
        "url": page_url,
        "content": "kept",
        "content_note": "Linked to the page an older version of the app made. Its content was left as it is.",
    }


def _link_page(session_id, link_url, t_data, app_title, tags, schema, mode):
    """The user chose an existing page for this recording (``link_url``)."""
    page_id = _extract_page_id(link_url)
    saved = _get_notion_page_url(session_id)
    if saved and _extract_page_id(saved) != page_id:
        return {"error": "This recording is already linked to another Notion page"}
    try:
        info = _parse_fetch(notion.mcp_call("notion-fetch", {"id": page_id}))
    except Exception as e:
        if _is_page_gone_error(e):
            return {"error": "That Notion page doesn't exist any more, or is in the trash"}
        raise
    content = info["content"] if info["content"] is not None else info["text"]
    owner = _text_value(info["properties"].get(SESSION_ID_PROP))
    if not owner:
        m = re.search(r"Session ID:\s*`?([\w.-]+)", _locate_app_section(content or "") or "")
        owner = m.group(1) if m else ""
    if owner and owner != session_id:
        return {"error": "That Notion page belongs to another recording"}
    page_url = link_url if _PAGE_URL_RE.match(link_url) else f"https://www.notion.so/{page_id.replace('-', '')}"
    if owner == session_id:
        # Already this recording's page (made by the app): a normal update.
        _save_notion_page_url(session_id, page_url)
        return _update_existing_page(session_id, page_id, page_url, t_data, app_title, tags, schema, mode)
    return _adopt_legacy_page(session_id, page_url, app_title, tags, schema, how="manual", info=info)


def _read_schema_for_scan():
    """The database schema without changing it (for read-only checks)."""
    db_id = notion.get_database_id()
    with _schema_lock:
        cached = _schema_cache if _schema_cache and _schema_cache.get("db_id") == db_id else None
    return cached or _fetch_db_schema(db_id)


def find_duplicate_pairs(entries):
    """Pairs (app-made page, legacy page) holding the same recording.

    Uses the same confident rule as automatic adoption, comparing the app
    page's transcript and last timestamp with each legacy page's.
    """
    app_pages = [e for e in entries if e["kind"] == "app" and e.get("session_id")]
    legacy = [e for e in entries if e["kind"] == "legacy"]
    pairs = []
    for newer in app_pages:
        scored = []
        for older in legacy:
            if older["page_id"] == newer["page_id"]:
                continue
            result = notion_legacy.score_candidate(newer, older, newer["session_id"])
            if result is not None:
                scored.append({**result, "entry": older})
        outcome, found = notion_legacy.classify(scored)
        if outcome != notion_legacy.MATCH_CONFIDENT:
            continue
        older = found[0]["entry"]
        pairs.append(
            {
                "session_id": newer["session_id"],
                "score": found[0]["score"],
                "older": {"url": older["url"], "title": older.get("title") or "", "created": older.get("created")},
                "newer": {"url": newer["url"], "title": newer.get("title") or "", "created": newer.get("created")},
            }
        )
    return pairs


@bp.route("/duplicates")
def duplicates():
    """Pages the app made that hold the same recording as an older page.

    Read-only: lists the pairs so the owner can delete one in Notion.
    """
    if not notion.is_connected():
        return jsonify({"error": "Not connected to Notion"}), 401
    db_id = notion.get_database_id()
    if not db_id:
        return jsonify({"error": "No database selected — configure in Settings"}), 400
    try:
        schema = _read_schema_for_scan()
        ds = (schema or {}).get("data_source_id")
        if not ds:
            return jsonify({"error": "Couldn't read the Notion database's layout"}), 400
        rows, complete = _list_db_rows(ds, refresh=True)
        entries = _page_fingerprints(rows, db_id, prune=complete)
        pairs = find_duplicate_pairs(entries)
        return jsonify({"pairs": pairs, "checked": len(entries), "complete": complete})
    except Exception as e:
        return jsonify(_error_json({}, e)), 500


# One lock per recording: auto-push and a click (or two clicks) must not both
# see "no page yet" and create two pages.
_push_locks = {}
_push_locks_guard = threading.Lock()


def _push_lock(session_id):
    with _push_locks_guard:
        return _push_locks.setdefault(session_id, threading.Lock())


MODE_FULL = "full"
MODE_PROPERTIES = "properties"


def push_to_notion(
    session_id,
    t_data=None,
    config=None,
    mode=MODE_FULL,
    poll_budget=None,
    lock_timeout=180,
    force_new=False,
    link_url=None,
):
    """Create or update a recording's Notion page (upsert by Session ID).

    ``mode``:
    - "full": create the page if needed; otherwise update properties and
      replace only the app-owned section of the body.
    - "properties": title/tags/Session ID only; the body is never touched.
      Does nothing if the recording has no page yet.

    Before creating, a page an older app version made for this recording
    (no Session ID) is looked for by content: a confident match is adopted,
    a doubtful one returns ``needs_review`` (nothing is created).
    ``force_new`` skips that check; ``link_url`` adopts the given page.

    Returns a dict: pushed, updated, url, content/content_note (full updates),
    adopted, needs_review/candidates, pending (async create still running),
    skipped, or error.
    """
    try:
        validate_session_id(session_id)
    except InvalidSessionIdError:
        return {"error": "Invalid session_id"}
    if not notion.is_connected():
        return {"error": "Not connected to Notion"}
    if not notion.get_database_id():
        return {"error": "No database selected — configure in Settings"}

    lock = _push_lock(session_id)
    if not lock.acquire(timeout=lock_timeout):
        return {"busy": True, "error": "A Notion push for this recording is already running"}
    try:
        return _push_locked(session_id, t_data, config, mode, poll_budget, force_new, link_url)
    finally:
        lock.release()


def _push_locked(session_id, t_data, config, mode, poll_budget, force_new=False, link_url=None):
    if poll_budget is None:
        poll_budget = _POLL_BUDGET_QUEUE
    if t_data is None:
        t_data = _read_transcript_json(session_id)
        if t_data is None:
            return {"error": "Transcript not found"}
    if config is None:
        config = load_config()

    custom_title = config.get("recording_names", {}).get(session_id, "")
    app_title = custom_title or t_data.get("auto_title", "") or session_id
    tags = _sanitize_tags(config.get("recording_tags", {}).get(session_id, []))

    schema = _ensure_schema(tags)
    tags = _writable_tags(tags, schema)

    if link_url:
        return _link_page(session_id, link_url, t_data, app_title, tags, schema, mode)

    page_url = _get_notion_page_url(session_id)
    gone_id = None
    while True:
        if not page_url:
            page_url, pending = _resume_pending_create(session_id, poll_budget)
            if pending:
                return {"pending": True, "message": "Notion is still creating the page"}
        if not page_url:
            page_url = _find_page_by_session_id(session_id, schema)
            if page_url and gone_id and _extract_page_id(page_url) == gone_id:
                page_url = None  # the search index still lists the page we just found gone
            if page_url:
                logging.info(f"Found existing Notion page for {session_id} by Session ID; updating it")
                _save_notion_page_url(session_id, page_url)
        if not page_url:
            if mode == MODE_PROPERTIES:
                return {"pushed": False, "skipped": True, "url": ""}
            if not force_new:
                outcome, found = _match_legacy_page(session_id, t_data, schema)
                if outcome == notion_legacy.MATCH_CONFIDENT:
                    try:
                        return _adopt_legacy_page(session_id, found[0]["url"], app_title, tags, schema, how="auto")
                    except Exception as e:
                        if not _is_page_gone_error(e):
                            raise
                        # Deleted in Notion since it was scored: forget it and
                        # let the retry look again (no message the queue treats
                        # as permanent).
                        _drop_cached_page(found[0]["page_id"])
                        raise ValueError("The older Notion page for this recording went away; will look again") from e
                if outcome == notion_legacy.MATCH_REVIEW:
                    return _needs_review(session_id, found)
                if outcome == MATCH_UNCHECKED:
                    return _needs_review(session_id, [], unchecked=True)
            return _create_page(session_id, t_data, app_title, tags, schema, poll_budget)

        page_id = _extract_page_id(page_url)
        if not page_id:
            return {"error": "Could not parse page ID from saved Notion URL"}

        try:
            return _update_existing_page(session_id, page_id, page_url, t_data, app_title, tags, schema, mode)
        except Exception as e:
            if gone_id is not None or not _is_page_gone_error(e):
                raise
            if mode == MODE_PROPERTIES:
                # Recreating the page needs its body: the queue turns this
                # job into a full push (once).
                return {"error": f"The Notion page for this recording is gone: {e}", "page_gone": True}
            logging.warning(f"Notion page for {session_id} was deleted or archived ({e}); recreating it")
            _forget_notion_page_url(session_id, page_url)
            _forget_legacy_adoption(session_id, page_id)
            gone_id = page_id
            page_url = None


def _update_existing_page(session_id, page_id, page_url, t_data, app_title, tags, schema, mode):
    """Update an existing page: properties, then (full mode) the app section.

    A legacy page the recording adopted keeps its body (see
    ``_adopt_legacy_page``) and its Notion title until the recording is
    renamed in the app.
    """
    adopted = _get_legacy_adoption(session_id, page_id)
    if adopted and "app_title" in adopted and adopted["app_title"] == app_title:
        resolved_title = None
    else:
        resolved_title = _resolve_title_for_update(session_id, app_title, page_url)
    properties = _page_properties(resolved_title, tags, session_id, schema)
    if properties:
        try:
            notion.mcp_call(
                "notion-update-page",
                {"page_id": page_id, "command": "update_properties", "properties": properties, "content_updates": []},
            )
        except Exception:
            reset_schema_cache()  # e.g. an option was deleted in Notion meanwhile
            raise
    if resolved_title is not None:
        _save_notion_pushed_title(session_id, resolved_title)
        if adopted and "app_title" in adopted:
            _release_adopted_title(session_id)

    out = {"pushed": True, "updated": True, "url": page_url}
    if mode == MODE_FULL:
        outcome, note = _update_app_section(page_id, t_data, session_id, append=not adopted)
        out["content"] = outcome
        if note:
            out["content_note"] = note
    return out


def enqueue_property_sync(session_id):
    """Queue a properties-only Notion update (title/tags) for a recording that
    already has a page. Never raises. Returns True if queued."""
    try:
        validate_session_id(session_id)
        if notion.is_connected() and notion.get_database_id() and _get_notion_page_url(session_id):
            from app.utils import notion_queue

            notion_queue.enqueue(session_id, notion_queue.KIND_PROPERTIES)
            return True
    except Exception as e:
        logging.warning(f"Could not queue Notion property sync for {session_id}: {e}")
    return False


def enqueue_auto_push(session_id):
    """Queue a full push after summarising (auto-push). Never raises.

    Returns the keys the /summarize response carries: notion_queued,
    notion_url (the existing page, if any) and notion_error.
    """
    out = {"notion_queued": False, "notion_url": "", "notion_error": ""}
    try:
        validate_session_id(session_id)
        if not notion.is_connected():
            out["notion_error"] = "Not connected to Notion"
            return out
        if not notion.get_database_id():
            out["notion_error"] = "No database selected — configure in Settings"
            return out
        from app.utils import notion_queue

        notion_queue.enqueue(session_id, notion_queue.KIND_FULL)
        out["notion_queued"] = True
        out["notion_url"] = _get_notion_page_url(session_id) or ""
    except Exception as e:
        logging.warning(f"Notion auto-push enqueue failed: {e}")
        out["notion_error"] = f"Notion push failed: {e}"
    return out


@bp.route("/push", methods=["POST"])
def push():
    """Push or update a recording's transcript and summary in Notion.

    Explicit user action: creates the page, or updates its properties and
    replaces the app-owned section. If Notion is still creating the page (async)
    or another push for this recording is running, the work is handed to the
    background queue and the response says ``queued``.

    If the recording looks like it is already in Notion on a page an older
    app version made, the response says ``needs_review`` with the candidate
    pages; the user then sends ``link_url`` (use that page) or
    ``force_new: true`` (create a new page anyway).
    """
    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id", "")
    if not session_id:
        return jsonify({"error": "No session_id"}), 400
    try:
        validate_session_id(session_id)
    except InvalidSessionIdError:
        return _invalid_session_id()
    link_url = data.get("link_url") or None
    if link_url is not None and (not isinstance(link_url, str) or not _extract_notion_id(link_url)):
        return jsonify({"error": "That isn't a Notion page link"}), 400
    force_new = data.get("force_new") is True

    from app.utils import notion_queue

    try:
        result = push_to_notion(
            session_id,
            mode=MODE_FULL,
            poll_budget=_POLL_BUDGET_REQUEST,
            lock_timeout=20,
            force_new=force_new,
            link_url=link_url,
        )
        if result.get("needs_review"):
            notion_queue.mark_needs_review(session_id, result.get("candidates") or [])
            return jsonify(result)
        if result.get("pending") or result.get("busy"):
            if result.get("busy") and (force_new or link_url):
                # The queue can't carry the choice; ask again rather than let
                # the background push decide. (A pending create is safe to
                # queue: the queue resumes it before matching anything.)
                return jsonify(
                    {"error": "Another Notion push for this recording is running. Try again in a moment."}
                ), 409
            notion_queue.enqueue(session_id, notion_queue.KIND_FULL)
            return jsonify(
                {
                    "pushed": False,
                    "queued": True,
                    "message": "Still working on it in the background — the Notion page will appear shortly.",
                }
            )
        if result.get("error"):
            code = 401 if "Not connected" in result["error"] else 400
            return jsonify(result), code
        if result.get("pushed"):
            notion_queue.clear_needs_review(session_id)
        return jsonify(result)
    except Exception as e:
        return jsonify(_error_json({}, e)), 500
