"""PLACEHOLDER for Task 1 of the engagement plan: Task 1 replaces this whole file.

The real module detects laughter on the Mac with the CED-Tiny ONNX model. This stub only
keeps its contract so the engagement code (Task 2) can be built and tested in parallel:
``detect`` never raises and reports laughter as unavailable.
"""


def runtime_available() -> bool:
    """True when onnxruntime and numpy import. Placeholder: always False."""
    return False


def detect(audio_path: str) -> dict:
    """Laughter events in ``audio_path``: ``{"status", "events", "reason"}``. Never raises.

    ``status`` is "done" or "unavailable"; each event is ``{"start", "end", "score", "loudness_db"}``.
    Placeholder: always unavailable.
    """
    return {"status": "unavailable", "events": [], "reason": "Laughter detection isn't installed yet"}
