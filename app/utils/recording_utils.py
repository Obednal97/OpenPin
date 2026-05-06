"""Shared recording utility helpers."""

import re
from datetime import datetime


def extract_recording_date(stem):
    """Extract the actual recording timestamp from the session ID or filename.

    Tries three strategies in order:
    1. Pure-integer stem → Unix epoch (must be between 2023-11-14 and
       ~2033-05-18 to be treated as a plausible recording timestamp).
       # Upper bound: epoch second 2000000000 = ~2033-05-18 — revisit before then
    2. cloud_ prefix pattern: ``cloud_YYYY-MM-DD_HHMM_…``
    3. Any embedded ``YYYY-MM-DD`` substring.

    Returns a :class:`datetime` or ``None`` if no date can be extracted.
    """
    try:
        ts = int(stem)
        if 1700000000 < ts < 2000000000:  # Upper bound: epoch second 2000000000 = ~2033-05-18 — revisit before then
            return datetime.fromtimestamp(ts)
    except ValueError:
        pass

    m = re.match(r"cloud_(\d{4}-\d{2}-\d{2})_(\d{2})(\d{2})_", stem)
    if m:
        try:
            return datetime.strptime(f"{m.group(1)} {m.group(2)}:{m.group(3)}", "%Y-%m-%d %H:%M")
        except ValueError:
            pass

    m = re.search(r"(\d{4}-\d{2}-\d{2})", stem)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y-%m-%d")
        except ValueError:
            pass

    return None
