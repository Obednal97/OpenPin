"""Notion MCP integration routes."""

import json
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


@bp.route("/push", methods=["POST"])
def push():
    """Push or update a recording's transcript and summary in Notion."""
    if not notion.is_connected():
        return jsonify({"error": "Not connected to Notion"}), 401
    if not notion.get_database_id():
        return jsonify({"error": "No database selected — configure in Settings"}), 400

    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id", "")
    if not session_id:
        return jsonify({"error": "No session_id"}), 400

    transcript_path = TRANSCRIPTS_DIR / f"{session_id}.json"
    if not transcript_path.exists():
        return jsonify({"error": "Transcript not found"}), 404

    t_data = json.loads(transcript_path.read_text())
    config = load_config()
    custom_title = config.get("recording_names", {}).get(session_id, "")
    title = custom_title or t_data.get("auto_title", "") or session_id
    full_content = _build_notion_content(t_data)

    # Check if we already have a page for this recording
    existing_url = _get_notion_page_url(session_id)

    try:
        if existing_url:
            # Update existing page
            result = notion.mcp_call(
                "notion-update-page",
                {
                    "url": existing_url,
                    "properties": {"title": title},
                    "content": full_content,
                },
            )
            return jsonify({"pushed": True, "updated": True, "url": existing_url, "result": result})
        else:
            # Create new page
            result = notion.mcp_call(
                "notion-create-pages",
                {
                    "parent": {"database_id": notion.get_database_id()},
                    "pages": [
                        {
                            "properties": {"title": title},
                            "content": full_content,
                            "icon": "\U0001f399\ufe0f",
                        }
                    ],
                },
            )
            # Extract and save the page URL
            page_url = _extract_page_url(result)
            if page_url:
                _save_notion_page_url(session_id, page_url)
            return jsonify({"pushed": True, "updated": False, "url": page_url or "", "result": result})
    except Exception as e:
        return jsonify(_error_json({}, e)), 500
