"""People directory routes (Settings › People) and "Not them" on a voice-matched speaker.

No response ever carries a voice vector. The write routes run synchronously: each is one
pass over the transcripts.
"""

from flask import Blueprint, jsonify, request

from app.people import backlog, directory, worker
from app.utils.paths import InvalidSessionIdError, validate_session_id

bp = Blueprint("people", __name__)


def _refused(e: directory.DirectoryError):
    return jsonify({"error": str(e)}), e.status


def _body() -> dict:
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else {}


@bp.route("/people/directory", methods=["GET"])
def people_directory():
    return jsonify(directory.directory())


@bp.route("/people/<person_id>/rename", methods=["POST"])
def rename_person(person_id):
    try:
        directory.rename(person_id, _body().get("name"))
    except directory.DirectoryError as e:
        return _refused(e)
    return jsonify(directory.directory())


@bp.route("/people/<person_id>/merge", methods=["POST"])
def merge_person(person_id):
    try:
        directory.merge(person_id, _body().get("into"))
    except directory.DirectoryError as e:
        return _refused(e)
    return jsonify(directory.directory())


@bp.route("/people/<person_id>", methods=["DELETE"])
def delete_person(person_id):
    try:
        directory.delete(person_id)
    except directory.DirectoryError as e:
        return _refused(e)
    return jsonify(directory.directory())


@bp.route("/people/fold-namesake", methods=["POST"])
def fold_namesake():
    """Your name was committed: queue folding the person with that name into you."""
    worker.enqueue("fold_into_me")
    return jsonify({"queued": True})


@bp.route("/people/backlog", methods=["GET"])
def backlog_state():
    """Just the backlog's progress: what the People section polls while it runs."""
    return jsonify(backlog.state())


@bp.route("/people/backlog", methods=["POST"])
def start_backlog():
    if not backlog.start():
        return jsonify({"error": "Install the speaker model first", "backlog": backlog.state()}), 409
    return jsonify({"backlog": backlog.state()})


@bp.route("/people/backlog", methods=["DELETE"])
def cancel_backlog():
    backlog.cancel()
    return jsonify({"backlog": backlog.state()})


@bp.route("/transcripts/speaker-names/reject", methods=["POST"])
def reject_voice_match():
    """``{session_id, label}``: the speaker isn't the person their voice matched."""
    body = _body()
    try:
        session_id = validate_session_id(body.get("session_id"))
    except InvalidSessionIdError as e:
        return jsonify({"error": str(e)}), 400
    try:
        names, sources = directory.reject(session_id, body.get("label"))
    except KeyError:
        return jsonify({"error": "Transcript not found"}), 404
    except directory.DirectoryError as e:
        return _refused(e)
    return jsonify({"speaker_names": names, "speaker_name_sources": sources})
