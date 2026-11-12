"""/mcp/v1: the data AI tools reach through Open Pin's MCP connector (app/mcp/server.py).

The guard in app/api_server.py has already checked the MCP key and refused
browsers before any of these run. Reads come from app/mcp/library.py, which
builds on the app's own listing; writes go through the same functions the
app's UI uses (``speakers.set_names``, ``tags.add_tags``/``remove_tags``), which
take the app's locks and bump the change counter so an open window reloads.

Errors are ``{"error": message}``: 404 "No recording with that id" for an
unknown (or invalid) id, 400 with a plain message for bad input. Recording ids
come from a ``path`` converter, so an encoded ``/`` or ``..`` arrives decoded;
every id is checked with ``validate_session_id`` (inside ``recording_exists``)
before any path is built from it.
"""

from flask import Blueprint, jsonify, request

from app.mcp import library
from app.routes.recordings import NO_SUCH_RECORDING, recording_exists
from app.state import speakers
from app.utils import tags as tags_store

bp = Blueprint("mcp_api", __name__, url_prefix="/mcp/v1")


def _error(message: str, status: int):
    return jsonify({"error": message}), status


def _not_found():
    return _error(NO_SUCH_RECORDING, 404)


def _body() -> dict | None:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else None


@bp.route("/recordings", methods=["GET"])
def list_recordings():
    args = request.args
    try:
        page = library.list_recordings(
            query=args.get("query"),
            tag=args.get("tag"),
            start=args.get("from"),
            end=args.get("to"),
            limit=args.get("limit"),
            cursor=args.get("cursor"),
            person=args.get("person"),
        )
    except library.BadRequest as e:
        return _error(str(e), 400)
    return jsonify(page)


@bp.route("/recordings/<path:recording_id>", methods=["GET"])
def get_recording(recording_id):
    try:
        include = library.parse_include(request.args.get("include"))
    except library.BadRequest as e:
        return _error(str(e), 400)
    found = library.get_recording(recording_id, include)
    if found is None:
        return _not_found()
    return jsonify(found)


@bp.route("/people", methods=["GET"])
def list_people():
    return jsonify({"items": library.people()})


@bp.route("/tags", methods=["GET"])
def list_tags():
    return jsonify({"items": tags_store.all_tags()})


@bp.route("/recordings/<path:recording_id>/speaker-names", methods=["POST"])
def set_speaker_names(recording_id):
    """``{names: {label: name or null}}`` -> ``{speakers: {label: name}}`` (the names now shown)."""
    if not recording_exists(recording_id):
        return _not_found()
    body = _body()
    names = body.get("names") if body else None
    if not isinstance(names, dict) or not names:
        return _error("names must map speaker labels to a name or null", 400)
    try:
        speakers.set_names(recording_id, names, from_ai=True)
    except KeyError:
        return _error("This recording has no transcript yet, so it has no speakers to name", 400)
    except ValueError as e:
        return _error(str(e), 400)
    return jsonify({"speakers": library.speaker_map(recording_id)})


def _tag_list(body: dict, key: str) -> list[str]:
    value = body.get(key, [])
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(t, str) for t in value):
        raise ValueError(f"{key} must be a list of tags")
    if len(value) > tags_store.MAX_TAGS_PER_CALL:
        raise ValueError(f"At most {tags_store.MAX_TAGS_PER_CALL} tags per call")
    return value


@bp.route("/recordings/<path:recording_id>/tags", methods=["POST"])
def change_tags(recording_id):
    """``{add: [...], remove: [...]}`` -> ``{tags: [...]}`` (the recording's tags now)."""
    if not recording_exists(recording_id):
        return _not_found()
    body = _body()
    if body is None:
        return _error("Send {add: [...], remove: [...]}", 400)
    try:
        add = _tag_list(body, "add")
        remove = _tag_list(body, "remove")
        if not add and not remove:
            raise ValueError("Name at least one tag to add or remove")
        # Check every tag before changing anything, so a bad one changes nothing.
        for tag in add:
            tags_store.normalise_tag(tag, [])
        if any(not t.strip() for t in remove):
            raise ValueError("A tag can't be empty")
        result = None
        if add:
            result = tags_store.add_tags(recording_id, add)
        if remove:
            result = tags_store.remove_tags(recording_id, remove)
    except ValueError as e:
        return _error(str(e), 400)
    return jsonify({"tags": result})
