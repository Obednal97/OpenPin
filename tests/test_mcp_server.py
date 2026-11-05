"""The stdio MCP connector (app/mcp/server.py).

Most tests drive server.py as a subprocess, exactly as Claude starts it: JSON-RPC
lines on stdin, JSON-RPC lines on stdout. The backend is a stub HTTP server on a
free port, with a connection file in a temp data folder. A few tests import the
script by path to reach the timeout and per-call file reading directly.
"""

import ast
import importlib.util
import json
import os
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
SERVER = PROJECT_ROOT / "app" / "mcp" / "server.py"
KEY = "mcp-key-0123456789abcdef"
NOT_RUNNING = "Open Pin isn't running. Open it and try again."
TOOLS = {
    "list_recordings",
    "get_recording",
    "list_people",
    "list_tags",
    "set_speaker_names",
    "add_tags",
    "remove_tags",
}


# --- stub backend -------------------------------------------------------------


class StubBackend:
    """Records each request; answers with ``routes[(method, path)]`` or 200 ``{"ok": true}``."""

    def __init__(self):
        self.requests = []
        self.routes = {}
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                parts = urlsplit(self.path)
                stub.requests.append(
                    {
                        "method": self.command,
                        "path": unquote(parts.path),
                        "raw_path": self.path,
                        "query": parse_qs(parts.query),
                        "headers": {k.lower(): v for k, v in self.headers.items()},  # header names are case-insensitive
                        "body": json.loads(body) if body else None,
                    }
                )
                status, payload = stub.routes.get((self.command, unquote(parts.path)), (200, {"ok": True}))
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = _answer
            do_POST = _answer

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def backend():
    stub = StubBackend()
    yield stub
    stub.close()


@pytest.fixture
def data_dir(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    return d


def write_connection(data_dir, port, key=KEY, pid=None):
    path = data_dir / "mcp-connection.json"
    path.write_text(json.dumps({"port": port, "key": key, "pid": os.getpid() if pid is None else pid}))
    return path


@pytest.fixture
def connected(backend, data_dir):
    write_connection(data_dir, backend.port)
    return backend


# --- driving the subprocess ------------------------------------------------------


def run_lines(data_dir, lines):
    """Feed raw lines to server.py; return (stdout lines, stderr)."""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PLAUD_DATA_DIR"] = str(data_dir)
    proc = subprocess.run(
        [sys.executable, str(SERVER)],
        input="".join(line + "\n" for line in lines),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=30,
        cwd=data_dir,
    )
    assert proc.returncode == 0, proc.stderr
    return [line for line in proc.stdout.splitlines() if line.strip()], proc.stderr


def rpc(data_dir, *messages):
    """Send JSON-RPC messages; return the parsed responses in order."""
    out, _ = run_lines(data_dir, [json.dumps(m) for m in messages])
    return [json.loads(line) for line in out]


def initialize(version="2025-06-18", msg_id=1):
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "method": "initialize",
        "params": {"protocolVersion": version, "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
    }


INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}


def call(name, arguments=None, msg_id=2):
    params = {"name": name}
    if arguments is not None:
        params["arguments"] = arguments
    return {"jsonrpc": "2.0", "id": msg_id, "method": "tools/call", "params": params}


def call_tool(data_dir, name, arguments=None):
    """Run one tool call after the handshake; return the tools/call result."""
    responses = rpc(data_dir, initialize(), INITIALIZED, call(name, arguments))
    assert [r["id"] for r in responses] == [1, 2]
    assert "result" in responses[1], responses[1]
    return responses[1]["result"]


def text_of(result):
    assert result["content"][0]["type"] == "text"
    return result["content"][0]["text"]


# --- protocol ---------------------------------------------------------------------


def test_initialize_handshake(data_dir):
    responses = rpc(data_dir, initialize(), INITIALIZED, {"jsonrpc": "2.0", "id": 2, "method": "ping"})
    assert len(responses) == 2  # the notification gets no answer
    init = responses[0]
    assert init["jsonrpc"] == "2.0" and init["id"] == 1
    result = init["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert "tools" in result["capabilities"]
    assert result["serverInfo"]["name"] == "open-pin"
    assert result["serverInfo"]["version"]
    assert "list_recordings" in result["instructions"]
    assert responses[1] == {"jsonrpc": "2.0", "id": 2, "result": {}}


@pytest.mark.parametrize("version", ["2024-11-05", "2025-03-26", "2025-06-18"])
def test_initialize_echoes_a_supported_version(data_dir, version):
    (init,) = rpc(data_dir, initialize(version))
    assert init["result"]["protocolVersion"] == version


def test_initialize_falls_back_for_an_unknown_version(data_dir):
    (init,) = rpc(data_dir, initialize("1999-01-01"))
    assert init["result"]["protocolVersion"] == "2025-06-18"


def test_unknown_method_is_32601_and_unknown_notification_is_ignored(data_dir):
    responses = rpc(
        data_dir,
        {"jsonrpc": "2.0", "method": "notifications/whatever"},
        {"jsonrpc": "2.0", "id": 7, "method": "resources/list"},
    )
    assert len(responses) == 1
    assert responses[0]["id"] == 7
    assert responses[0]["error"]["code"] == -32601


def test_bad_json_is_parse_error_and_the_server_keeps_going(data_dir):
    out, _ = run_lines(data_dir, ["{not json", json.dumps({"jsonrpc": "2.0", "id": 3, "method": "ping"})])
    first, second = (json.loads(line) for line in out)
    assert first["id"] is None and first["error"]["code"] == -32700
    assert second == {"jsonrpc": "2.0", "id": 3, "result": {}}


def test_invalid_request_is_32600(data_dir):
    (resp,) = rpc(data_dir, {"id": 4, "method": "ping"})  # no "jsonrpc": "2.0"
    assert resp["id"] == 4 and resp["error"]["code"] == -32600


def test_client_responses_get_no_answer(data_dir):
    responses = rpc(data_dir, {"jsonrpc": "2.0", "id": 9, "result": {}}, {"jsonrpc": "2.0", "id": 10, "method": "ping"})
    assert [r["id"] for r in responses] == [10]


def test_tools_list(data_dir):
    responses = rpc(data_dir, initialize(), INITIALIZED, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = responses[1]["result"]["tools"]
    assert {t["name"] for t in tools} == TOOLS
    for tool in tools:
        assert len(tool["description"]) > 40, tool["name"]
        schema = tool["inputSchema"]
        assert schema["type"] == "object"
        assert set(schema.get("required", [])) <= set(schema["properties"])
    by_name = {t["name"]: t for t in tools}
    assert "speaker names" in by_name["list_recordings"]["description"]
    assert "transcript" in by_name["list_recordings"]["description"]
    assert by_name["list_recordings"]["inputSchema"]["properties"]["limit"]["maximum"] == 100
    assert by_name["get_recording"]["inputSchema"]["required"] == ["id"]
    assert by_name["list_people"]["annotations"]["readOnlyHint"] is True
    assert by_name["set_speaker_names"]["annotations"]["readOnlyHint"] is False


def test_unknown_tool_is_a_protocol_error(data_dir):
    responses = rpc(data_dir, initialize(), call("delete_everything", {}))
    assert responses[1]["error"]["code"] == -32602
    assert "delete_everything" in responses[1]["error"]["message"]


def test_stdout_holds_only_json_rpc(data_dir, backend):
    # The backend is down and the input includes rubbish: stdout must still be
    # nothing but JSON-RPC 2.0 messages, one per line.
    write_connection(data_dir, free_port())
    out, _ = run_lines(
        data_dir,
        [
            json.dumps(initialize()),
            json.dumps(INITIALIZED),
            "",
            "garbage",
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
            json.dumps(call("list_recordings", {}, msg_id=3)),
            json.dumps(call("get_recording", {"id": 5}, msg_id=4)),
        ],
    )
    assert len(out) == 5
    for line in out:
        msg = json.loads(line)
        assert msg["jsonrpc"] == "2.0"
        assert ("result" in msg) != ("error" in msg)


# --- tools against the stub backend ----------------------------------------------


def test_list_recordings_calls_the_backend_with_the_key(data_dir, connected):
    page = {"items": [{"id": "abc", "title": "Standup"}], "next_cursor": "c2"}
    connected.routes[("GET", "/mcp/v1/recordings")] = (200, page)
    result = call_tool(
        data_dir,
        "list_recordings",
        {"query": "budget", "tag": "Work", "from": "2026-10-01", "to": "2026-10-31", "limit": 10, "cursor": "c1"},
    )
    assert result["isError"] is False
    assert json.loads(text_of(result)) == page
    (req,) = connected.requests
    assert req["method"] == "GET" and req["path"] == "/mcp/v1/recordings"
    assert req["headers"]["x-open-pin-mcp-key"] == KEY
    assert "origin" not in req["headers"]
    assert req["query"] == {
        "query": ["budget"],
        "tag": ["Work"],
        "from": ["2026-10-01"],
        "to": ["2026-10-31"],
        "limit": ["10"],
        "cursor": ["c1"],
    }


def test_list_recordings_with_no_arguments(data_dir, connected):
    connected.routes[("GET", "/mcp/v1/recordings")] = (200, {"items": [], "next_cursor": None})
    result = call_tool(data_dir, "list_recordings")
    assert result["isError"] is False
    (req,) = connected.requests
    assert req["query"] == {}


def test_get_recording_quotes_the_id_and_defaults_include(data_dir, connected):
    record = {"id": "2026-10-01 09:00", "title": "Weekly", "transcript": []}
    connected.routes[("GET", "/mcp/v1/recordings/2026-10-01 09:00")] = (200, record)
    result = call_tool(data_dir, "get_recording", {"id": "2026-10-01 09:00"})
    assert json.loads(text_of(result)) == record
    (req,) = connected.requests
    assert "%20" in req["raw_path"] and "%3A" in req["raw_path"]
    assert req["query"] == {"include": ["transcript,summary,coaching"]}


def test_get_recording_with_a_narrower_include(data_dir, connected):
    call_tool(data_dir, "get_recording", {"id": "abc", "include": ["summary"]})
    assert connected.requests[0]["query"] == {"include": ["summary"]}


def test_get_recording_unknown_id_passes_the_message_through(data_dir, connected):
    connected.routes[("GET", "/mcp/v1/recordings/nope")] = (404, {"error": "No recording with that id"})
    result = call_tool(data_dir, "get_recording", {"id": "nope"})
    assert result["isError"] is True
    assert text_of(result) == "No recording with that id"


@pytest.mark.parametrize(("tool", "path"), [("list_people", "/mcp/v1/people"), ("list_tags", "/mcp/v1/tags")])
def test_list_people_and_tags(data_dir, connected, tool, path):
    payload = {"items": [{"name": "Sarah Jones", "recordings": 3}]}
    connected.routes[("GET", path)] = (200, payload)
    result = call_tool(data_dir, tool, {})
    assert result["isError"] is False
    assert json.loads(text_of(result)) == payload
    assert connected.requests[0]["path"] == path


def test_set_speaker_names_posts_the_names(data_dir, connected):
    names = {"Speaker 2": "Sarah Jones", "Speaker 3": None}
    connected.routes[("POST", "/mcp/v1/recordings/abc/speaker-names")] = (
        200,
        {"speakers": {"Speaker 2": "Sarah Jones"}},
    )
    result = call_tool(data_dir, "set_speaker_names", {"id": "abc", "names": names})
    assert result["isError"] is False
    assert json.loads(text_of(result)) == {"speakers": {"Speaker 2": "Sarah Jones"}}
    (req,) = connected.requests
    assert req["method"] == "POST"
    assert req["body"] == {"names": names}
    assert req["headers"]["content-type"] == "application/json"
    assert req["headers"]["x-open-pin-mcp-key"] == KEY


def test_backend_validation_message_passes_through(data_dir, connected):
    connected.routes[("POST", "/mcp/v1/recordings/abc/speaker-names")] = (
        400,
        {"error": "No Speaker 9 in this transcript"},
    )
    result = call_tool(data_dir, "set_speaker_names", {"id": "abc", "names": {"Speaker 9": "Bob"}})
    assert result["isError"] is True
    assert text_of(result) == "No Speaker 9 in this transcript"


@pytest.mark.parametrize(
    ("tool", "body"),
    [("add_tags", {"add": ["Work", "Q4"], "remove": []}), ("remove_tags", {"add": [], "remove": ["Work", "Q4"]})],
)
def test_tag_tools_post_add_and_remove(data_dir, connected, tool, body):
    connected.routes[("POST", "/mcp/v1/recordings/abc/tags")] = (200, {"tags": ["Q4"]})
    result = call_tool(data_dir, tool, {"id": "abc", "tags": ["Work", "Q4"]})
    assert result["isError"] is False
    (req,) = connected.requests
    assert req["path"] == "/mcp/v1/recordings/abc/tags"
    assert req["body"] == body


# --- not running and access off ----------------------------------------------------


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_no_connection_file_means_not_running(data_dir):
    result = call_tool(data_dir, "list_tags", {})
    assert result == {"content": [{"type": "text", "text": NOT_RUNNING}], "isError": True}


def test_unreadable_connection_file_means_not_running(data_dir):
    (data_dir / "mcp-connection.json").write_text("{half")
    result = call_tool(data_dir, "list_tags", {})
    assert result["isError"] is True and text_of(result) == NOT_RUNNING


def test_refused_connection_means_not_running(data_dir):
    write_connection(data_dir, free_port())
    result = call_tool(data_dir, "list_tags", {})
    assert result["isError"] is True and text_of(result) == NOT_RUNNING


def test_rejected_key_means_not_running(data_dir, connected):
    connected.routes[("GET", "/mcp/v1/tags")] = (401, {"error": "Unauthorized"})
    result = call_tool(data_dir, "list_tags", {})
    assert result["isError"] is True and text_of(result) == NOT_RUNNING


def test_dead_pid_means_not_running_without_sending_the_key(data_dir, backend):
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    write_connection(data_dir, backend.port, pid=dead.pid)
    result = call_tool(data_dir, "list_tags", {})
    assert result["isError"] is True and text_of(result) == NOT_RUNNING
    assert backend.requests == []


def test_access_off_passes_the_backend_message(data_dir, connected):
    message = "AI tool access is turned off in Open Pin"
    connected.routes[("GET", "/mcp/v1/recordings")] = (403, {"error": message})
    result = call_tool(data_dir, "list_recordings", {})
    assert result["isError"] is True and text_of(result) == message


def test_server_error_is_a_tool_error(data_dir, connected):
    connected.routes[("GET", "/mcp/v1/tags")] = (500, {"error": "boom"})
    result = call_tool(data_dir, "list_tags", {})
    assert result["isError"] is True
    assert "boom" in text_of(result)


# --- bad arguments -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "arguments", "fragment"),
    [
        ("get_recording", {}, "id"),
        ("get_recording", {"id": ""}, "id"),
        ("get_recording", {"id": 12}, "id"),
        ("get_recording", {"id": "a/b"}, "id"),
        ("get_recording", {"id": "abc", "include": ["audio"]}, "include"),
        ("get_recording", {"id": "abc", "include": "summary"}, "include"),
        ("list_recordings", {"limit": 500}, "limit"),
        ("list_recordings", {"limit": 0}, "limit"),
        ("list_recordings", {"limit": "ten"}, "limit"),
        ("list_recordings", {"limit": True}, "limit"),
        ("list_recordings", {"query": 5}, "query"),
        ("list_recordings", {"colour": "red"}, "colour"),
        ("set_speaker_names", {"id": "abc"}, "names"),
        ("set_speaker_names", {"id": "abc", "names": {}}, "names"),
        ("set_speaker_names", {"id": "abc", "names": ["Sarah"]}, "names"),
        ("set_speaker_names", {"id": "abc", "names": {"Speaker 1": 3}}, "names"),
        ("add_tags", {"id": "abc", "tags": "Work"}, "tags"),
        ("add_tags", {"id": "abc", "tags": []}, "tags"),
        ("add_tags", {"id": "abc", "tags": ["Work", 7]}, "tags"),
        ("remove_tags", {"tags": ["Work"]}, "id"),
    ],
)
def test_bad_arguments_are_tool_errors_and_never_reach_the_backend(data_dir, connected, tool, arguments, fragment):
    result = call_tool(data_dir, tool, arguments)
    assert result["isError"] is True
    assert fragment in text_of(result)
    assert connected.requests == []


def test_arguments_that_are_not_an_object_are_a_tool_error(data_dir, connected):
    result = call_tool(data_dir, "list_tags", ["x"])
    assert result["isError"] is True
    assert connected.requests == []


# --- in-process: per-call file reading, timeout, imports ------------------------------


def load_server():
    spec = importlib.util.spec_from_file_location("open_pin_mcp_server_under_test", SERVER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_connection_file_is_read_for_every_call(data_dir, monkeypatch):
    server = load_server()
    monkeypatch.setenv("PLAUD_DATA_DIR", str(data_dir))
    first, second = StubBackend(), StubBackend()
    try:
        write_connection(data_dir, first.port, key="k1")
        server.handle_message(call("list_tags", {}))
        write_connection(data_dir, second.port, key="k2")
        server.handle_message(call("list_tags", {}))
    finally:
        first.close()
        second.close()
    assert [r["headers"]["x-open-pin-mcp-key"] for r in first.requests] == ["k1"]
    assert [r["headers"]["x-open-pin-mcp-key"] for r in second.requests] == ["k2"]


def test_slow_backend_times_out_with_a_tool_error(data_dir, monkeypatch):
    server = load_server()
    monkeypatch.setenv("PLAUD_DATA_DIR", str(data_dir))
    monkeypatch.setattr(server, "HTTP_TIMEOUT_S", 0.2)
    # A socket that accepts and never answers.
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    try:
        write_connection(data_dir, listener.getsockname()[1])
        response = server.handle_message(call("list_tags", {}))
    finally:
        listener.close()
    result = response["result"]
    assert result["isError"] is True
    assert "didn't answer" in text_of(result)


def test_default_http_timeout_is_ten_seconds():
    assert load_server().HTTP_TIMEOUT_S == 10


def test_server_uses_only_the_standard_library():
    tree = ast.parse(SERVER.read_text())
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "no relative imports: server.py runs as a plain script"
            modules.add(node.module.split(".")[0])
    assert modules, "no imports found"
    assert modules <= set(sys.stdlib_module_names) | {"__future__"}, modules - set(sys.stdlib_module_names)
