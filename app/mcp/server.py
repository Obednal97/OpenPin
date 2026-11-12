"""Open Pin's MCP connector: Model Context Protocol over stdio, for Claude Desktop and Claude Code.

Claude starts this script through the launcher in Application Support and talks
newline-delimited JSON-RPC 2.0 to it on stdin and stdout. It holds no state and
reads no library data itself: every tool call reads the connection file
(``$PLAUD_DATA_DIR/mcp-connection.json``, written by the running app) for the
backend's port and MCP key, then calls ``http://127.0.0.1:<port>/mcp/v1/*``.
No file, a refused connection or a rejected key means the app isn't running.

Runs as a plain script (``python server.py``) with the standard library only, so
it starts fast and never imports ``app.*``. stdout carries protocol messages and
nothing else; logs go to stderr.
"""

import http.client
import json
import logging
import os
import plistlib
import re
import socket
import sys
import threading
import tomllib
import urllib.parse
from pathlib import Path

log = logging.getLogger("open-pin-mcp")

SERVER_NAME = "open-pin"
PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")

# Keep in step with app/utils/constants.py (DATA_DIR_ENV, PACKAGED_DATA_DIR) and
# app/mcp/access.py (MCP_KEY_HEADER, the connection file name).
DATA_DIR_ENV = "PLAUD_DATA_DIR"
PACKAGED_DATA_DIR = "~/Library/Application Support/Plaud V2"
CONNECTION_FILE = "mcp-connection.json"
MCP_KEY_HEADER = "X-Open-Pin-MCP-Key"
API_PREFIX = "/mcp/v1"
HTTP_TIMEOUT_S = 10  # total, per call: connecting, sending and reading the whole answer
MAX_RESPONSE_BYTES = 50 * 1024 * 1024

NOT_RUNNING = "Open Pin isn't running. Open it and try again."
ACCESS_OFF = "AI tool access is turned off in Open Pin"
ACCESS_OFF_HINT = ACCESS_OFF + ". Turn it on in Open Pin's Settings, under General › AI tools."
TIMED_OUT = "Open Pin didn't answer in time. Try again."
TOO_LARGE = "Open Pin's answer was too large to pass on. Ask for less, for example without the transcript."

# JSON-RPC error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class ToolError(Exception):
    """A tool call that failed in a way the AI tool should read: becomes ``isError`` text."""


class RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# --- tools ------------------------------------------------------------------------

INSTRUCTIONS = (
    "Open Pin holds the user's recordings from their Plaud NotePin, with transcripts, summaries "
    "and conversation coaching. Use list_recordings to find a recording (it searches titles, tags, "
    "speaker names, transcripts and summaries), then get_recording to read it. Transcripts label "
    'speakers "Speaker 1", "Speaker 2" and so on: once you have worked out who someone is, name them '
    "with set_speaker_names, checking list_people for existing spellings first. Tag recordings with "
    "add_tags and remove_tags, checking list_tags first. Nothing else can be changed or deleted through these tools. "
    "They work only while the Open Pin app is open on this Mac."
)

_ID = {
    "type": "string",
    "minLength": 1,
    "pattern": r"^[^/\\]+$",
    "description": "The recording's id, from list_recordings.",
}
_TAGS = {
    "type": "array",
    "items": {"type": "string", "minLength": 1},
    "minItems": 1,
    "maxItems": 20,
}
_INCLUDE_PARTS = ["transcript", "summary", "coaching"]

_READ_ONLY = {"readOnlyHint": True, "openWorldHint": False}

TOOLS = [
    {
        "name": "list_recordings",
        "title": "Find recordings",
        "description": (
            "Find recordings in the user's Open Pin library, newest first. `query` is a plain, "
            "case-insensitive substring search over each recording's title, tags, speaker names, "
            "transcript and summary, so a person's name, a topic or a phrase someone said all work. "
            "Narrow with `person` (recordings that person was in, by exact name from list_people), "
            "`tag` and a `from`/`to` date range. Each item has id, title, recorded_at "
            "(ISO 8601 with the Mac's UTC offset), duration_s, tags, participants (named speakers, "
            "plus the user when they've confirmed which speaker they are), has_transcript, "
            "has_summary, coaching_status and notion_url. Returns {items, next_cursor}: pass "
            "next_cursor back as `cursor` for the next page. Use an item's id with get_recording."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Text to look for in titles, tags, speaker names, transcripts and summaries.",
                },
                "person": {
                    "type": "string",
                    "description": (
                        "Only recordings this person was in, named or recognised by voice: a name from "
                        "list_people (case doesn't matter). Unlike query, it skips recordings that only mention them."
                    ),
                },
                "tag": {"type": "string", "description": "Only recordings with this tag."},
                "from": {
                    "type": "string",
                    "description": "Only recordings made on or after this date (YYYY-MM-DD or an ISO 8601 date-time).",
                },
                "to": {
                    "type": "string",
                    "description": "Only recordings made on or before this date (YYYY-MM-DD or an ISO 8601 date-time).",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 25,
                    "description": "Recordings per page, 1 to 100 (default 25).",
                },
                "cursor": {"type": "string", "description": "next_cursor from the previous page."},
            },
            "additionalProperties": False,
        },
        "annotations": _READ_ONLY,
    },
    {
        "name": "get_recording",
        "title": "Read a recording",
        "description": (
            "Read one recording in full: everything list_recordings gives, plus file info (name, "
            "format, size_bytes, the folder it's in, whether it's stored locally or in Google Drive, "
            "and whether the audio is on this Mac), `speakers` (a {label: name} map) and `you` (the "
            "labels the user has confirmed are them). With `include` (default all three): "
            "`transcript` as segments {start_s, end_s, speaker_label, speaker_name, text}; `summary` "
            "with speaker names applied; and `coaching` (status, metrics, review and, when measured, "
            "engagement: laughs after the user's lines, follow-up questions to them and points others "
            "built on; or null when there is none or the user excluded the recording from coaching). "
            "Long transcripts can "
            "be large, so leave out what you don't need."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": _ID,
                "include": {
                    "type": "array",
                    "items": {"type": "string", "enum": _INCLUDE_PARTS},
                    "minItems": 1,
                    "default": _INCLUDE_PARTS,
                    "description": "Which parts to return: any of transcript, summary, coaching.",
                },
            },
            "required": ["id"],
            "additionalProperties": False,
        },
        "annotations": _READ_ONLY,
    },
    {
        "name": "list_people",
        "title": "List named speakers",
        "description": (
            "Everyone named in the library, typed or recognised by voice, with how many recordings "
            "each is in; `you` marks the user. Use a name with list_recordings' `person` to find "
            "every recording with someone. Check it before set_speaker_names so the same person keeps "
            'the same spelling (reuse "Sarah Jones" rather than adding "Sarah J"), and to see who the '
            "user talks to."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": _READ_ONLY,
    },
    {
        "name": "list_tags",
        "title": "List tags",
        "description": (
            "Every tag in the library, with how many recordings carry it. Check it before add_tags "
            "so you reuse the user's existing tags rather than creating near-duplicates."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": _READ_ONLY,
    },
    {
        "name": "set_speaker_names",
        "title": "Name speakers",
        "description": (
            "Name the speakers in one recording, for after you've worked out who a speaker is "
            "(from what they say, how others address them, the title or what the user told you). "
            'Transcripts label speakers "Speaker 1", "Speaker 2" and so on. `names` maps labels to a '
            'name, or to null to clear a name and bring the label back: {"Speaker 2": "Sarah Jones", '
            '"Speaker 3": null}. Labels not in the transcript are rejected. A name is 1 to 80 '
            "characters with no ':' or '*'. Only name someone you're reasonably sure of, and use "
            "list_people for existing spellings. Names show everywhere in Open Pin: the transcript, "
            "summary, coaching and Notion. Names you set teach Open Pin that person's voice, so later "
            "recordings are named automatically. Returns the updated speaker map."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": _ID,
                "names": {
                    "type": "object",
                    "additionalProperties": {"type": ["string", "null"]},
                    "minProperties": 1,
                    "description": 'Speaker label to name, or null to clear it, e.g. {"Speaker 2": "Sarah Jones"}.',
                },
            },
            "required": ["id", "names"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "add_tags",
        "title": "Add tags",
        "description": (
            "Add tags to one recording. A tag that matches an existing one case-insensitively keeps "
            "the existing spelling; a new tag is created and joins the app's tag picker. A tag is 1 "
            "to 40 characters, with at most 20 per call and 20 per recording. Check list_tags first "
            "to reuse the user's tags. Returns the recording's updated tags."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"id": _ID, "tags": {**_TAGS, "description": "Tags to add."}},
            "required": ["id", "tags"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "remove_tags",
        "title": "Remove tags",
        "description": (
            "Remove tags from one recording, matched case-insensitively. The tags stay available "
            "for other recordings. Returns the recording's updated tags."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"id": _ID, "tags": {**_TAGS, "description": "Tags to remove."}},
            "required": ["id", "tags"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False},
    },
]
TOOLS_BY_NAME = {tool["name"]: tool for tool in TOOLS}


# --- argument checks ----------------------------------------------------------------
# A small checker for the JSON Schema subset the tools above use, so the schema
# Claude sees and the check here can't drift apart. The backend applies the
# content rules (name and tag characters, labels in the transcript) as well.

_JSON_TYPES = {"string": str, "object": dict, "array": list, "null": type(None)}


def _type_ok(value, json_type: str) -> bool:
    if json_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, _JSON_TYPES[json_type])


def _check(name: str, value, schema: dict) -> None:
    types = schema.get("type")
    types = [types] if isinstance(types, str) else types or []
    if types and not any(_type_ok(value, t) for t in types):
        wanted = {
            "string": "text",
            "integer": "a whole number",
            "array": "a list",
            "object": "an object",
            "null": "null",
        }
        raise ToolError(f"{name} must be {' or '.join(wanted[t] for t in types)}")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or not value.strip() and schema.get("minLength"):
            raise ToolError(f"{name} can't be empty")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            raise ToolError(f"{name} isn't valid: {value!r}")
        if "enum" in schema and value not in schema["enum"]:
            raise ToolError(f"{name} must be one of {', '.join(schema['enum'])}")
    if _type_ok(value, "integer"):
        low, high = schema.get("minimum"), schema.get("maximum")
        if (low is not None and value < low) or (high is not None and value > high):
            raise ToolError(f"{name} must be from {low} to {high}")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            raise ToolError(f"{name} needs at least {schema['minItems']} item")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise ToolError(f"{name} can have at most {schema['maxItems']} items")
        for item in value:
            _check(f"each item in {name}", item, schema.get("items", {}))
    if isinstance(value, dict):
        if len(value) < schema.get("minProperties", 0):
            raise ToolError(f"{name} needs at least {schema['minProperties']} entry")
        properties = schema.get("properties", {})
        extra = schema.get("additionalProperties", True)
        for key, item in value.items():
            if key in properties:
                _check(key, item, properties[key])
            elif extra is False:
                raise ToolError(f"Unknown argument: {key}")
            elif isinstance(extra, dict):
                _check(f"{name} values", item, extra)
        for key in schema.get("required", []):
            if key not in value:
                raise ToolError(f"{key} is required")


def check_arguments(tool: dict, arguments) -> dict:
    if not isinstance(arguments, dict):
        raise ToolError("arguments must be an object")
    _check("arguments", arguments, tool["inputSchema"])
    return arguments


# --- the backend ------------------------------------------------------------------


def data_dir() -> Path:
    """Same rule as app/utils/constants.py: PLAUD_DATA_DIR if set, else the installed app's folder."""
    value = (os.environ.get(DATA_DIR_ENV) or "").strip()
    return Path(value or PACKAGED_DATA_DIR).expanduser()


def _pid_gone(pid) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False  # alive but not ours (PermissionError), or unknown: let HTTP decide
    return False


def read_connection() -> tuple[int, str]:
    """The backend's port and MCP key, read fresh for every call."""
    try:
        info = json.loads((data_dir() / CONNECTION_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ToolError(NOT_RUNNING) from None
    if not isinstance(info, dict):
        raise ToolError(NOT_RUNNING)
    port, key = info.get("port"), info.get("key")
    if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
        raise ToolError(NOT_RUNNING)
    if _pid_gone(info.get("pid")):
        # A file left behind by a crash: don't hand the key to whatever has the port now.
        raise ToolError(NOT_RUNNING)
    if info.get("enabled") is False:
        # The app is running with AI tool access switched off (a keyless marker).
        raise ToolError(ACCESS_OFF_HINT)
    if not isinstance(key, str) or not key:
        raise ToolError(NOT_RUNNING)
    return port, key


class _TooLarge(Exception):
    pass


def _read_capped(response: http.client.HTTPResponse) -> bytes:
    length = response.getheader("Content-Length")
    if length and length.isdigit() and int(length) > MAX_RESPONSE_BYTES:
        raise _TooLarge
    chunks, total = [], 0
    while chunk := response.read(1024 * 1024):
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise _TooLarge
        chunks.append(chunk)
    return b"".join(chunks)


def _exchange(port: int, method: str, path: str, data: bytes | None, headers: dict) -> tuple[int, bytes]:
    """One HTTP request to the backend, finished within HTTP_TIMEOUT_S in total.

    http.client's timeout applies to each socket operation, so a backend that
    trickles bytes could hold a call for ever. The request runs on a worker
    thread instead; at the deadline its socket is shut, which ends the thread.
    http.client never uses a proxy or follows a redirect, so the key only ever
    goes to 127.0.0.1.
    """
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=HTTP_TIMEOUT_S)
    outcome: dict = {}

    def work():
        try:
            conn.request(method, path, body=data, headers=headers)
            response = conn.getresponse()
            outcome["answer"] = (response.status, _read_capped(response))
        except BaseException as e:  # handed to the caller below
            outcome["error"] = e
        finally:
            conn.close()

    worker = threading.Thread(target=work, name="open-pin-mcp-http", daemon=True)
    worker.start()
    worker.join(HTTP_TIMEOUT_S)
    if worker.is_alive():
        sock = conn.sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        raise ToolError(TIMED_OUT)
    if "error" in outcome:
        raise outcome["error"]
    return outcome["answer"]


def _error_message(body: bytes) -> str:
    try:
        payload = json.loads(body)
    except (ValueError, RecursionError):
        return ""
    if isinstance(payload, dict) and isinstance(payload.get("error"), str):
        return payload["error"]
    return ""


def backend(method: str, path: str, query: dict | None = None, body: dict | None = None):
    port, key = read_connection()
    target = API_PREFIX + path
    if query:
        target += "?" + urllib.parse.urlencode(query)
    headers = {MCP_KEY_HEADER: key, "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    try:
        status, raw = _exchange(port, method, target, data, headers)
    except _TooLarge:
        raise ToolError(TOO_LARGE) from None
    except TimeoutError:
        raise ToolError(TIMED_OUT) from None
    except (OSError, http.client.HTTPException):
        raise ToolError(NOT_RUNNING) from None
    if status >= 300:
        message = _error_message(raw)
        if status == 401:
            raise ToolError(NOT_RUNNING)
        if status == 403:
            raise ToolError(message or ACCESS_OFF)
        if status >= 500:
            raise ToolError(f"Open Pin couldn't do that: {message or f'error {status}'}")
        raise ToolError(message or f"Open Pin refused the request (error {status}).")
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):
        raise ToolError("Open Pin sent an answer the connector couldn't read.") from None


def _recording_path(recording_id: str, suffix: str = "") -> str:
    return "/recordings/" + urllib.parse.quote(recording_id, safe="") + suffix


def _run_tool(name: str, args: dict):
    if name == "list_recordings":
        query = {k: args[k] for k in ("query", "person", "tag", "from", "to", "limit", "cursor") if k in args}
        return backend("GET", "/recordings", query=query)
    if name == "get_recording":
        include = args.get("include", _INCLUDE_PARTS)
        return backend("GET", _recording_path(args["id"]), query={"include": ",".join(include)})
    if name == "list_people":
        return backend("GET", "/people")
    if name == "list_tags":
        return backend("GET", "/tags")
    if name == "set_speaker_names":
        return backend("POST", _recording_path(args["id"], "/speaker-names"), body={"names": args["names"]})
    if name == "add_tags":
        return backend("POST", _recording_path(args["id"], "/tags"), body={"add": args["tags"], "remove": []})
    if name == "remove_tags":
        return backend("POST", _recording_path(args["id"], "/tags"), body={"add": [], "remove": args["tags"]})
    raise RpcError(INVALID_PARAMS, f"Unknown tool: {name}")


def _tool_result(text: str, is_error: bool) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def call_tool(params) -> dict:
    if not isinstance(params, dict) or not isinstance(params.get("name"), str):
        raise RpcError(INVALID_PARAMS, "tools/call needs a tool name")
    name = params["name"]
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        raise RpcError(INVALID_PARAMS, f"Unknown tool: {name}")
    log.info("tool %s", name)
    try:
        arguments = params.get("arguments")
        args = check_arguments(tool, {} if arguments is None else arguments)
        data = _run_tool(name, args)
    except ToolError as e:
        log.info("tool %s: %s", name, e)
        return _tool_result(str(e), True)
    except Exception as e:  # never crash the server over one call
        log.exception("tool %s failed", name)
        return _tool_result(f"The Open Pin connector hit an error: {e}", True)
    return _tool_result(json.dumps(data, ensure_ascii=False), False)


# --- JSON-RPC -----------------------------------------------------------------------


def app_version() -> str:
    """Open Pin's version: the bundle's Info.plist, or pyproject.toml in a checkout."""
    here = Path(__file__).resolve()
    try:
        with open(here.parents[3] / "Info.plist", "rb") as f:  # Contents/Resources/app/mcp/server.py
            version = plistlib.load(f).get("CFBundleShortVersionString")
        if isinstance(version, str) and version:
            return version
    except (OSError, IndexError, ValueError, plistlib.InvalidFileException):
        pass
    try:
        with open(here.parents[2] / "pyproject.toml", "rb") as f:
            return str(tomllib.load(f)["project"]["version"])
    except (OSError, IndexError, KeyError, ValueError):
        return "unknown"


def initialize(params) -> dict:
    requested = params.get("protocolVersion") if isinstance(params, dict) else None
    version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else PROTOCOL_VERSION
    return {
        "protocolVersion": version,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": SERVER_NAME, "title": "Open Pin", "version": app_version()},
        "instructions": INSTRUCTIONS,
    }


METHODS = {
    "initialize": initialize,
    "ping": lambda params: {},
    "tools/list": lambda params: {"tools": TOOLS},
    "tools/call": call_tool,
}


def _error(msg_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def handle_message(msg):
    """One JSON-RPC message in; the response, or None for notifications and client responses."""
    if not isinstance(msg, dict):
        return _error(None, INVALID_REQUEST, "Invalid request")
    has_id = "id" in msg
    msg_id = msg.get("id")
    if "method" not in msg:
        if has_id and ("result" in msg or "error" in msg):
            return None  # a response to a request we never send; ignore it
        return _error(msg_id, INVALID_REQUEST, "Invalid request")
    method = msg["method"]
    if msg.get("jsonrpc") != "2.0" or not isinstance(method, str):
        return _error(msg_id, INVALID_REQUEST, "Invalid request") if has_id else None
    if not has_id:
        return None  # notifications (initialized, cancelled, ...) need nothing from us
    handler = METHODS.get(method)
    if handler is None:
        return _error(msg_id, METHOD_NOT_FOUND, f"Method not found: {method}")
    try:
        result = handler(msg.get("params"))
    except RpcError as e:
        return _error(msg_id, e.code, e.message)
    except Exception as e:
        log.exception("%s failed", method)
        return _error(msg_id, INTERNAL_ERROR, f"Internal error: {e}")
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _handle_line(line: bytes):
    try:
        msg = json.loads(line)
    except (ValueError, RecursionError):  # RecursionError: absurdly deep nesting
        return _error(None, PARSE_ERROR, "Parse error")
    if isinstance(msg, list):  # a batch (protocol versions before 2025-06-18)
        if not msg:
            return _error(None, INVALID_REQUEST, "Invalid request")
        responses = [r for r in (handle_message(m) for m in msg) if r is not None]
        return responses or None
    return handle_message(msg)


def main() -> int:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="open-pin-mcp: %(message)s")
    protocol_out = sys.stdout.buffer
    sys.stdout = sys.stderr  # a stray print() must never corrupt the protocol stream
    log.info("started (data folder %s)", data_dir())
    try:
        for line in sys.stdin.buffer:
            if not line.strip():
                continue
            try:
                response = _handle_line(line)
                if response is None:
                    continue
                encoded = json.dumps(response).encode("ascii")  # ASCII: no encoding surprises
            except Exception:  # no single line may end the loop
                log.exception("couldn't handle a message")
                encoded = json.dumps(_error(None, INTERNAL_ERROR, "Internal error")).encode("ascii")
            protocol_out.write(encoded + b"\n")
            protocol_out.flush()
    except (BrokenPipeError, KeyboardInterrupt):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
