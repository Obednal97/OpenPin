"""Per-session threading locks for transcript file I/O.

Transcript JSON files (TRANSCRIPTS_DIR/{session_id}.json) can be read and
written concurrently by:

- The main transcription handler (transcribe.py)
- The background diarization thread (_bg_diarize in transcribe.py)
- The /transcripts/update route (recordings.py)
- The /recordings/tag and /recordings/rename routes (recordings.py)
- The /summarize route (transcribe.py)

Without coordination, concurrent writes silently overwrite each other,
losing diarization data, tags, or summaries.

Use get_transcript_lock(session_id) to obtain a per-session Lock and wrap
every read-modify-write sequence with:

    with get_transcript_lock(session_id):
        data = json.loads(path.read_text())
        ...mutate data...
        path.write_text(json.dumps(data, ...))
"""

import threading

_transcript_locks: dict[str, threading.Lock] = {}
_transcript_locks_lock = threading.Lock()


def get_transcript_lock(session_id: str) -> threading.Lock:
    """Return the threading.Lock for the given session_id.

    Locks are created lazily and never deleted (one per session for the
    lifetime of the process). The internal dict is itself protected by a
    meta-lock so concurrent callers for the same new session_id don't race
    to create two separate Lock objects.
    """
    with _transcript_locks_lock:
        if session_id not in _transcript_locks:
            _transcript_locks[session_id] = threading.Lock()
        return _transcript_locks[session_id]
