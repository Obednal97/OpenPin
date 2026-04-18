"""Notion MCP integration routes."""

import json
import logging
import re
import time as _time
import traceback

from flask import Blueprint, jsonify, request

from app.state import notion
from app.utils.config import load_config, save_config
from app.utils.constants import TRANSCRIPTS_DIR


def _error_json(base, exc):
    """Return error JSON with traceback for debugging (auto-reported to GitHub)."""
    base["error"] = str(exc)
    base["traceback"] = traceback.format_exc()
    return base

bp = Blueprint("notion", __name__, url_prefix="/notion")


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
    return jsonify(notion.disconnect())


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

    try:
        result = notion.mcp_call("notion-fetch", {"url": url})
        content = result.get("content", [])
        text = ""
        for item in content:
            text += item.get("text", "") if isinstance(item, dict) else str(item)

        id_match = re.search(r"([a-f0-9]{32})", url.replace("-", ""))
        db_id = id_match.group(1) if id_match else ""

        title = "Notion Database"
        try:
            parsed = json.loads(text)
            title = parsed.get("title", parsed.get("name", title))
            if isinstance(title, list) and title:
                title = title[0].get("plain_text", title[0].get("text", {}).get("content", str(title[0])))
            if not db_id:
                db_id = parsed.get("id", db_id)
        except (json.JSONDecodeError, TypeError, IndexError):
            first_line = text.split("\n")[0][:80]
            if first_line:
                title = first_line

        if not db_id:
            return jsonify({"error": "Could not extract database ID from URL"}), 400

        if len(db_id) == 32 and "-" not in db_id:
            db_id = f"{db_id[:8]}-{db_id[8:12]}-{db_id[12:16]}-{db_id[16:20]}-{db_id[20:]}"

        notion.set_database(db_id, title)

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
    return jsonify({"saved": True})


def _build_notion_content(t_data):
    """Build markdown content for a Notion page from transcript data."""
    content_parts = []
    summary = t_data.get("summary", "")
    if summary:
        content_parts.append(summary)
        content_parts.append("\n---\n")

    content_parts.append("## Full Transcript\n")
    segments = t_data.get("segments", [])
    if segments:
        for seg in segments:
            ts = f"[{int(seg.get('start', 0)) // 60:02d}:{int(seg.get('start', 0)) % 60:02d}]"
            speaker = f" **{seg['speaker']}**:" if seg.get("speaker") else ""
            content_parts.append(f"{ts}{speaker} {seg.get('text', '').strip()}")
    else:
        content_parts.append(t_data.get("text", ""))

    return "\n".join(content_parts)


def _extract_page_url(result):
    """Extract the Notion page URL from an MCP response."""
    # The MCP response may contain the URL in various places
    if not result:
        return None
    # Check content array for text that contains a URL
    content = result.get("content", [])
    for item in content if isinstance(content, list) else [content]:
        text = item.get("text", "") if isinstance(item, dict) else str(item)
        if "notion.so" in text or "notion.site" in text:
            # Extract URL
            import re as _re

            m = _re.search(r"https://[^\s\"'<>]+notion[^\s\"'<>]+", text)
            if m:
                return m.group(0)
        # Try parsing as JSON for page ID
        try:
            parsed = json.loads(text) if isinstance(text, str) else text
            if isinstance(parsed, dict):
                url = parsed.get("url", "")
                if url:
                    return url
                page_id = parsed.get("id", "")
                if page_id:
                    return f"https://notion.so/{page_id.replace('-', '')}"
                # Check results array
                results = parsed.get("results", [parsed])
                for r in results:
                    if isinstance(r, dict) and r.get("url"):
                        return r["url"]
        except (json.JSONDecodeError, TypeError):
            pass
    return None


def _save_notion_page_url(session_id, url):
    """Save the Notion page URL for a recording."""
    config = load_config()
    pages = config.get("notion_page_urls", {})
    pages[session_id] = url
    config["notion_page_urls"] = pages
    save_config(config)

    # Also persist in transcript JSON for recovery after config resets
    transcript_path = TRANSCRIPTS_DIR / f"{session_id}.json"
    if transcript_path.exists():
        try:
            t_data = json.loads(transcript_path.read_text())
            t_data["notion_page_url"] = url
            transcript_path.write_text(json.dumps(t_data, indent=2, ensure_ascii=False))
        except Exception:
            pass


def _get_notion_page_url(session_id):
    """Get the saved Notion page URL for a recording, if any."""
    config = load_config()
    url = config.get("notion_page_urls", {}).get(session_id, "")
    if not url:
        # Try recovering from transcript JSON
        transcript_path = TRANSCRIPTS_DIR / f"{session_id}.json"
        if transcript_path.exists():
            try:
                t_data = json.loads(transcript_path.read_text())
                url = t_data.get("notion_page_url", "")
                if url:
                    # Re-save to config for future lookups
                    config = load_config()
                    pages = config.get("notion_page_urls", {})
                    pages[session_id] = url
                    config["notion_page_urls"] = pages
                    save_config(config)
            except Exception:
                pass
    return url


@bp.route("/page-url/<session_id>")
def page_url(session_id):
    """Get the Notion page URL for a recording."""
    url = _get_notion_page_url(session_id)
    return jsonify({"url": url, "has_page": bool(url)})


def _save_notion_pushed_title(session_id, title):
    """Store the title we last pushed to Notion for this recording."""
    config = load_config()
    titles = config.get("notion_pushed_titles", {})
    titles[session_id] = title
    config["notion_pushed_titles"] = titles
    save_config(config)


def _get_notion_pushed_title(session_id):
    """Get the title we last pushed to Notion, or empty string."""
    return load_config().get("notion_pushed_titles", {}).get(session_id, "")


def _fetch_notion_page_title(page_url):
    """Fetch the current title of a Notion page. Returns title string or None on error."""
    try:
        result = notion.mcp_call("notion-fetch", {"id": page_url})
        # The fetch result contains page content — extract title from the response
        content = result.get("content", []) if isinstance(result, dict) else []
        for item in content if isinstance(content, list) else []:
            text = item.get("text", "") if isinstance(item, dict) else str(item)
            # The MCP response includes the title in the page metadata
            if "title" in str(item).lower():
                return text
        # Fallback: try to find title in the raw result text
        result_str = json.dumps(result) if isinstance(result, dict) else str(result)
        # Look for title property in the result
        if '"title"' in result_str:
            import re as _re
            # Match title in various Notion MCP response formats
            m = _re.search(r'#\s+(.+?)(?:\n|$)', result_str)
            if m:
                return m.group(1).strip()
    except Exception as e:
        logging.warning(f"Could not fetch Notion page title: {e}")
    return None


def _build_notion_properties(title, tags=None):
    """Build Notion page properties dict with title and optional tags.

    Title can be None to skip title update (preserves Notion-side custom title).
    Notion MCP expects multi-select values as a JSON-array string (it's stored
    as TEXT in the SQLite-style schema — see `notion-fetch` on a data source,
    which annotates multi-select columns as `JSON array with zero or more of`).
    """
    props = {}
    if title is not None:
        props["title"] = title
    if tags is not None:
        # Always send — an empty array clears the property on Notion's side.
        # Notion auto-creates new multi-select options when it sees unfamiliar values.
        props["Tags"] = json.dumps(tags)
    return props


def _extract_page_id(url_or_id):
    """Extract a dashed UUID page_id from a Notion URL or bare id."""
    if not url_or_id:
        return None
    stripped = url_or_id.replace("-", "")
    m = re.search(r"([a-f0-9]{32})", stripped)
    if not m:
        return None
    h = m.group(1)
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


# Session-cached view of the Tags column schema. Populated on first push that
# carries tags (so we don't hit Notion on startup or on tagless pushes).
_tags_schema_cache = None  # None = uninitialised; else {"exists": bool, "options": set[str]}


def _fetch_tags_schema():
    """Read the live Tags column state from the Notion data source.

    Returns {"exists": bool, "options": set[str]}. Empty on error — callers
    degrade gracefully rather than blocking the push.
    """
    default = {"exists": False, "options": set()}
    db_id = notion.get_database_id()
    if not db_id:
        return default
    try:
        result = notion.mcp_call("notion-fetch", {"id": db_id})
    except Exception as e:
        logging.warning(f"Could not fetch Notion schema: {e}")
        return default

    # The fetch result is MCP-wrapped markdown containing a <data-source-state>
    # JSON blob. Pull the text out of whichever envelope MCP returned.
    text = ""
    if isinstance(result, dict):
        if isinstance(result.get("text"), str):
            text = result["text"]
        else:
            for item in result.get("content", []) if isinstance(result.get("content"), list) else []:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    text += item["text"]
    if not text:
        return default

    m = re.search(r"<data-source-state>\s*(\{.*?\})\s*</data-source-state>", text, re.DOTALL)
    if not m:
        return default
    try:
        state = json.loads(m.group(1))
    except json.JSONDecodeError:
        return default

    tags_schema = state.get("schema", {}).get("Tags")
    if not isinstance(tags_schema, dict) or tags_schema.get("type") != "multi_select":
        return default
    options = {
        opt["name"]
        for opt in tags_schema.get("options", [])
        if isinstance(opt, dict) and isinstance(opt.get("name"), str)
    }
    return {"exists": True, "options": options}


def _ensure_tags_schema(tags):
    """Make sure the Tags MULTI_SELECT column exists AND all `tags` are
    registered as options on it.

    Notion MCP rejects page writes whose multi_select values aren't already
    declared in the column schema — it does NOT auto-create new options on
    write. So before every write we reconcile the schema: ADD the column if
    missing, then ALTER SET with (existing ∪ needed) options so we preserve
    any options the user added manually on Notion's side.
    """
    global _tags_schema_cache
    if _tags_schema_cache is None:
        _tags_schema_cache = _fetch_tags_schema()

    needed_opts = {t for t in (tags or []) if isinstance(t, str) and t}
    missing_opts = needed_opts - _tags_schema_cache["options"]

    if _tags_schema_cache["exists"] and not missing_opts:
        return  # Nothing to do

    db_id = notion.get_database_id()
    if not db_id:
        return

    if not _tags_schema_cache["exists"]:
        try:
            notion.mcp_call(
                "notion-update-data-source",
                {"data_source_id": db_id, "statements": 'ADD COLUMN "Tags" MULTI_SELECT()'},
            )
            _tags_schema_cache["exists"] = True
        except Exception as e:
            err = str(e).lower()
            if "already" in err or "exists" in err or "duplicate" in err:
                _tags_schema_cache["exists"] = True
            else:
                logging.warning(f"Could not create Tags column: {e}")
                return

    if missing_opts:
        all_opts = _tags_schema_cache["options"] | needed_opts
        # ALTER SET replaces the full option list — keep every existing one so
        # we don't wipe options the user added manually on Notion's side.
        options_str = ", ".join(f"'{n.replace(chr(39), chr(39) * 2)}':default" for n in sorted(all_opts))
        try:
            notion.mcp_call(
                "notion-update-data-source",
                {
                    "data_source_id": db_id,
                    "statements": f'ALTER COLUMN "Tags" SET MULTI_SELECT({options_str})',
                },
            )
            _tags_schema_cache["options"] = all_opts
        except Exception as e:
            logging.warning(f"Could not register Tags options: {e}")
            # Invalidate the cache so the next push retries a fresh fetch.
            _tags_schema_cache = None


# Back-compat alias — older call sites in this module / recordings.py use this name.
def _ensure_tags_property_once(tags):
    _ensure_tags_schema(tags)


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


def push_to_notion(session_id, t_data=None, config=None):
    """Push or update a recording in Notion. Used by both manual push and auto-push.

    Returns dict with keys: pushed, updated, url, error (if any).
    """
    if not notion.is_connected():
        return {"error": "Not connected to Notion"}
    if not notion.get_database_id():
        return {"error": "No database selected — configure in Settings"}

    if t_data is None:
        transcript_path = TRANSCRIPTS_DIR / f"{session_id}.json"
        if not transcript_path.exists():
            return {"error": "Transcript not found"}
        t_data = json.loads(transcript_path.read_text())

    if config is None:
        config = load_config()

    custom_title = config.get("recording_names", {}).get(session_id, "")
    app_title = custom_title or t_data.get("auto_title", "") or session_id
    tags = config.get("recording_tags", {}).get(session_id, [])
    full_content = _build_notion_content(t_data)

    # Make sure the Tags MULTI_SELECT column exists before we try to write to
    # it. Notion MCP silently drops unknown properties, so we can't rely on
    # an error from the page write to tell us the column is missing.
    _ensure_tags_property_once(tags)

    existing_url = _get_notion_page_url(session_id)

    if existing_url:
        # Updating existing page — protect Notion-side title changes
        page_id = _extract_page_id(existing_url)
        if not page_id:
            return {"error": "Could not parse page ID from saved Notion URL"}

        resolved_title = _resolve_title_for_update(session_id, app_title, existing_url)
        properties = (
            _build_notion_properties(resolved_title, tags)
            if resolved_title is not None
            else _build_notion_properties(None, tags)
        )

        # Two separate MCP calls: properties update, then content replace.
        # notion-update-page requires an explicit command per call.
        if properties:
            notion.mcp_call(
                "notion-update-page",
                {
                    "page_id": page_id,
                    "command": "update_properties",
                    "properties": properties,
                    "content_updates": [],
                },
            )

        result = notion.mcp_call(
            "notion-update-page",
            {
                "page_id": page_id,
                "command": "replace_content",
                "properties": {},
                "content_updates": [],
                "new_str": full_content,
                "allow_deleting_content": True,
            },
        )

        if resolved_title is not None:
            _save_notion_pushed_title(session_id, resolved_title)
        return {"pushed": True, "updated": True, "url": existing_url, "result": result}
    else:
        # Creating new page — always use app title
        properties = _build_notion_properties(app_title, tags)
        result = notion.mcp_call(
            "notion-create-pages",
            {
                "parent": {
                    "type": "database_id",
                    "database_id": notion.get_database_id(),
                },
                "pages": [
                    {
                        "properties": properties,
                        "content": full_content,
                        "icon": "\U0001f399\ufe0f",
                    }
                ],
            },
        )
        page_url = _extract_page_url(result)
        if page_url:
            _save_notion_page_url(session_id, page_url)
        _save_notion_pushed_title(session_id, app_title)
        return {"pushed": True, "updated": False, "url": page_url or "", "result": result}


@bp.route("/push", methods=["POST"])
def push():
    """Push or update a recording's transcript and summary in Notion."""
    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id", "")
    if not session_id:
        return jsonify({"error": "No session_id"}), 400

    try:
        result = push_to_notion(session_id)
        if result.get("error"):
            code = 401 if "Not connected" in result["error"] else 400
            return jsonify(result), code
        return jsonify(result)
    except Exception as e:
        return jsonify(_error_json({}, e)), 500
