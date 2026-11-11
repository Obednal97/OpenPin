"""Voice store: the owner's voice ("me") and the other people Open Pin has learnt.

One JSON file (``voices.json``, mode 0600) holds every person's voice samples.
The owner's vectors are mirrored to the v1 coaching profile so older code and
older app versions keep working. Embeddings never leave the Mac. Plain Python
only: importing this module needs no numpy, soundfile or wespeakerruntime.
"""

from __future__ import annotations

import json
import logging
import math
import os
import secrets
import shutil
import tempfile
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from app.utils.constants import PEOPLE_DIR

log = logging.getLogger(__name__)

VOICES_PATH = PEOPLE_DIR / "voices.json"
ME = "me"
MAX_SAMPLES = 20

_lock = threading.Lock()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _empty() -> dict:
    return {"version": 2, "people": {ME: {"name": None, "is_me": True, "samples": []}}}


def _norm_name(name: str) -> str:
    return " ".join(name.split()).casefold()


def _cosine(a: list[float], b: list[float]) -> float:
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if not na or not nb or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b, strict=True)) / (na * nb)


def _write_private(path: Path, payload: dict) -> None:
    """Atomic write (temp file in the same folder + replace), mode 0600."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ------------------------------------------------------------------- loading


def _read_v1() -> list[list[float]] | None:
    """Exemplars from the v1 coaching profile, or None when absent or unreadable."""
    from app.coaching import voice  # lazy: voice imports this module

    try:
        data = json.loads(voice.PROFILE_PATH.read_text())
        ex = data["exemplars"]
        if data.get("version") != 1 or not isinstance(ex, list):
            return None
        return [[float(x) for x in e] for e in ex if e]
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def _load_unlocked() -> dict:
    try:
        data = json.loads(VOICES_PATH.read_text())
        if data.get("version") == 2 and isinstance(data.get("people"), dict):
            data["people"].setdefault(ME, {"name": None, "is_me": True, "samples": []})
            return data
        log.warning("voices.json has an unknown shape; ignoring it")
        return _empty()
    except FileNotFoundError:
        pass
    except (OSError, ValueError, AttributeError) as e:
        log.warning("could not read voices.json: %s", e)
        return _empty()
    return _migrate_v1()


def _migrate_v1() -> dict:
    from app.coaching import voice

    data = _empty()
    exemplars = _read_v1()
    if exemplars is None:
        return data
    try:
        shutil.copy2(voice.PROFILE_PATH, voice.PROFILE_PATH.with_name(voice.PROFILE_PATH.name + ".v1-backup"))
    except OSError as e:
        log.warning("could not back up the v1 voice profile: %s", e)
    stamp = _now()
    data["people"][ME]["samples"] = [
        {"vec": v, "session_id": None, "label": None, "seconds": 0, "added_at": stamp} for v in exemplars
    ]
    try:
        _write_private(VOICES_PATH, data)
    except OSError as e:
        log.warning("could not save migrated voices: %s", e)
    return data


def load() -> dict:
    """The whole store: ``{"version": 2, "people": {id: {name, is_me, samples}}}``."""
    with _lock:
        return _load_unlocked()


def _mirror_me(data: dict) -> None:
    from app.coaching import voice

    vecs = [s["vec"] for s in data["people"][ME]["samples"]]
    if not vecs:
        try:
            voice.PROFILE_PATH.unlink()
        except FileNotFoundError:
            pass
        return
    _write_private(voice.PROFILE_PATH, {"version": 1, "exemplars": vecs, "updated_at": _now()})


def mutate(fn: Callable[[dict], None]) -> dict:
    """Load, apply ``fn`` in place, trim each person, save, mirror ``me`` to the v1 file."""
    with _lock:
        data = _load_unlocked()
        fn(data)
        for person in data["people"].values():
            person["samples"] = trim_varied(person["samples"])
        _write_private(VOICES_PATH, data)
        _mirror_me(data)
        return data


# ------------------------------------------------------------------- helpers


def find_person(data: dict, name: str) -> str | None:
    """Id of the person called ``name`` (case and spacing ignored). Never the owner."""
    want = _norm_name(name)
    if not want:
        return None
    for pid, person in data["people"].items():
        if pid != ME and person.get("name") and _norm_name(person["name"]) == want:
            return pid
    return None


def create_person(data: dict, name: str) -> str:
    name = " ".join(name.split())
    if not name:
        raise ValueError("a name is required")
    if find_person(data, name):
        raise ValueError(f"{name} already exists")
    pid = "p_" + secrets.token_hex(4)
    while pid in data["people"]:
        pid = "p_" + secrets.token_hex(4)
    data["people"][pid] = {"name": name, "is_me": False, "samples": []}
    return pid


def remove_sample(data: dict, session_id: str | None, label: str | None) -> str | None:
    """Drop the sample for this recording speaker; returns the person it came from."""
    found = None
    for pid, person in data["people"].items():
        kept = [s for s in person["samples"] if (s.get("session_id"), s.get("label")) != (session_id, label)]
        if len(kept) != len(person["samples"]):
            found = pid
            person["samples"] = kept
    return found


def add_sample(data: dict, person_id: str, sample: dict) -> None:
    """Append ``sample``, first removing any earlier one for the same recording speaker
    from every person (a re-assignment moves it). Samples with no session never collide."""
    if sample.get("session_id") is not None:
        remove_sample(data, sample["session_id"], sample.get("label"))
    data["people"][person_id]["samples"].append(sample)


def trim_varied(samples: list[dict], cap: int = MAX_SAMPLES) -> list[dict]:
    """Keep the ``cap`` most varied samples: while over, drop the one most like the rest
    (highest mean cosine to the others; ties go to the oldest)."""
    out = list(samples)
    while len(out) > cap:
        scores = [
            sum(_cosine(s["vec"], o["vec"]) for j, o in enumerate(out) if j != i) / (len(out) - 1)
            for i, s in enumerate(out)
        ]
        top = max(scores)  # ties: oldest added_at, then earliest position
        tied = [i for i, sc in enumerate(scores) if math.isclose(sc, top, abs_tol=1e-12)]
        worst = min(tied, key=lambda i: (out[i].get("added_at") or "", i))
        del out[worst]
    return out


def person_summary(data: dict) -> list[dict]:
    """One row per person, with no vectors."""
    rows = []
    for pid, p in data["people"].items():
        samples = p["samples"]
        heard = [s["added_at"] for s in samples if s.get("added_at")]
        rows.append(
            {
                "id": pid,
                "name": p.get("name"),
                "is_me": bool(p.get("is_me")),
                "samples": len(samples),
                "bytes": len(json.dumps(samples)),
                "last_heard": max(heard) if heard else None,
            }
        )
    return rows
