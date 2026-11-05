"""Coaching routes: reports, owner pick, progress and the voice profile.

The sidecar's verbatim ``words`` never leave the backend through these routes.
``/coaching/progress`` and ``/coaching/voice`` are fixed paths, registered before
the ``<sid>`` routes (and "progress"/"voice" are not session ids anyway).
"""

import copy
import logging
import time
from pathlib import Path
from threading import Lock, Thread

from flask import Blueprint, jsonify, request

from app.coaching import runner, store, voice
from app.coaching.progress import compute_progress
from app.coaching.targets import TARGETS
from app.utils.config import update_config
from app.utils.paths import InvalidSessionIdError, validate_session_id
from app.utils.recording_utils import extract_recording_date

bp = Blueprint("coaching", __name__)
log = logging.getLogger(__name__)

DEFAULT_WEEKS = 12
MAX_WEEKS = 52

_voice_lock = Lock()
_voice_state: dict = {"installing": False, "error": None}


def _bad_id():
    return jsonify({"error": "Not found"}), 404


def _valid(sid: str) -> bool:
    try:
        validate_session_id(sid)
    except InvalidSessionIdError:
        return False
    return True


def _public(sidecar: dict) -> dict:
    """The sidecar as the UI sees it: never the verbatim words."""
    out = {k: v for k, v in (sidecar or {}).items() if k != "words"}
    return out


def _recording_time(session_id: str, sidecar_path: Path) -> float:
    """When the recording started, the way the recordings list works it out: the date in
    the session id, else the audio file's modification time, else the sidecar's."""
    date = extract_recording_date(session_id)
    if date is not None:
        return date.timestamp()
    try:
        from app.routes.recordings import _find_recording

        audio = _find_recording(session_id)
        if audio:
            return audio.stat().st_mtime
    except Exception:
        pass
    return sidecar_path.stat().st_mtime


# ---------------------------------------------------------------- fixed paths


@bp.route("/coaching/progress", methods=["GET"])
def get_progress():
    try:
        weeks = int(request.args.get("weeks", DEFAULT_WEEKS))
    except (TypeError, ValueError):
        weeks = DEFAULT_WEEKS
    weeks = max(1, min(MAX_WEEKS, weeks))
    sidecars = []
    directory = store.COACHING_DIR
    for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
        sid = path.stem
        if not _valid(sid):
            continue
        sidecar = store.load(sid)
        if not sidecar:
            continue
        try:
            sidecars.append((sid, _recording_time(sid, path), sidecar))
        except OSError:
            continue
    return jsonify(compute_progress(sidecars, now=time.time(), weeks=weeks))


@bp.route("/coaching/voice", methods=["GET"])
def voice_status():
    exemplars = len(voice._load_exemplars())
    with _voice_lock:
        installing, error = _voice_state["installing"], _voice_state["error"]
    return jsonify(
        {
            "profile": exemplars > 0,
            "exemplars": exemplars,
            "model": voice.model_available(),
            "installing": installing,
            "install_error": error,
        }
    )


def _install_voice_model() -> None:
    error = None
    try:
        ok, message = voice.install_model()
        if not ok:
            error = message or "Could not install the speaker model."
    except Exception as e:
        log.warning("voice model install failed: %s", e, exc_info=True)
        error = f"Could not install the speaker model ({e})."
    with _voice_lock:
        _voice_state.update(installing=False, error=error)


@bp.route("/coaching/voice/install", methods=["POST"])
def voice_install():
    with _voice_lock:
        if _voice_state["installing"]:
            return jsonify({"started": True})
        _voice_state.update(installing=True, error=None)
    try:
        Thread(target=_install_voice_model, daemon=True).start()
    except BaseException:
        with _voice_lock:
            _voice_state["installing"] = False
        raise
    return jsonify({"started": True})


@bp.route("/coaching/voice/setup", methods=["POST"])
def voice_setup():
    data = request.get_json(silent=True) or {}
    sid, labels = data.get("session_id"), data.get("labels")
    if not isinstance(sid, str) or not _valid(sid):
        return _bad_id()
    if not voice.model_available():
        return jsonify({"error": "voice model not installed"}), 409
    if not isinstance(labels, list) or not labels or not all(isinstance(x, str) for x in labels):
        return jsonify({"error": "pick at least one speaker"}), 400
    transcript = runner._read_transcript(sid)
    if transcript is None:
        return jsonify({"error": "no transcript for this recording"}), 404
    segments = transcript.get("segments") or []
    if not set(labels) <= set(runner._speaker_labels(segments)):
        return jsonify({"error": "unknown speaker label"}), 400
    audio = runner._resolve_audio(sid, None)
    if not audio:
        return jsonify({"error": "no audio file for this recording"}), 404
    try:
        add = voice.replace_profile if data.get("replace") is True else voice.add_exemplars
        added = add(audio, segments, sorted(set(labels)))
    except Exception as e:
        log.warning("voice setup failed for %s: %s", sid, e, exc_info=True)
        return jsonify({"error": "Could not learn your voice from this recording"}), 500
    return jsonify({"exemplars": added})


@bp.route("/coaching/voice", methods=["DELETE"])
def voice_delete():
    voice.delete_profile()
    return jsonify({"deleted": True})


# ---------------------------------------------------------------- one recording


@bp.route("/coaching/<sid>", methods=["GET"])
def get_coaching(sid):
    if not _valid(sid):
        return _bad_id()
    out = _public(runner.current(sid))
    out["targets"] = copy.deepcopy(TARGETS)
    return jsonify(out)


@bp.route("/coaching/<sid>/run", methods=["POST"])
def run_coaching(sid):
    if not _valid(sid):
        return _bad_id()
    runner.start_coaching_async(sid, None, force=True)
    return jsonify({"started": True})


def _set_owner_in_background(sid: str, labels: list[str], learn: bool) -> None:
    try:
        runner.set_owner(sid, labels, learn)
    except Exception as e:
        log.warning("setting the coaching owner for %s failed: %s", sid, e, exc_info=True)


@bp.route("/coaching/<sid>/owner", methods=["POST"])
def set_owner(sid):
    if not _valid(sid):
        return _bad_id()
    data = request.get_json(silent=True) or {}
    labels = data.get("labels")
    if not isinstance(labels, list) or not labels or not all(isinstance(x, str) and x for x in labels):
        return jsonify({"error": "pick at least one speaker"}), 400
    transcript = runner._read_transcript(sid)
    if transcript is None:
        return jsonify({"error": "no transcript for this recording"}), 404
    known = set(runner._speaker_labels(transcript.get("segments") or []))
    unknown = sorted(set(labels) - known)
    if unknown:
        return jsonify({"error": f"unknown speaker label: {', '.join(unknown)}"}), 400
    Thread(target=_set_owner_in_background, args=(sid, labels, bool(data.get("learn", True))), daemon=True).start()
    out = _public(runner.current(sid))
    out["started"] = True
    return jsonify(out)


@bp.route("/coaching/<sid>/mixed-up", methods=["POST"])
def set_mixed_up(sid):
    if not _valid(sid):
        return _bad_id()
    data = request.get_json(silent=True) or {}
    flag = data.get("mixed_up")
    if not isinstance(flag, bool):
        return jsonify({"error": "mixed_up must be true or false"}), 400
    return jsonify(_public(store.update(sid, lambda d: d.update(mixed_up=flag))))


# ---------------------------------------------------------------- startup


def migrate_speaker_labels() -> None:
    """Once: turn speaker labels (and so automatic coaching) on for existing users.

    A user who later turns ``diarize`` off stays off: the flag is only set the first time.
    """

    def _apply(config: dict) -> None:
        if config.get("coaching_migrated"):
            return
        if not config.get("diarize"):
            config["diarize"] = True
        config.setdefault("coaching_auto", True)
        config["coaching_migrated"] = True

    update_config(_apply)
