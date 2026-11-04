"""Target ranges and tunable constants for the coaching metrics.

This file is the single place for the numbers; app/coaching/metrics.py reads
them and must not hard-code any of its own. "low"/"high" of None means that
side is unbounded.
"""

TARGETS: dict[str, dict] = {
    "pace_wpm": {"low": 110.0, "high": 160.0, "label": "Pace (words per minute)"},
    "talk_share": {"low": 0.4, "high": 0.6, "label": "Talk share"},
    "longest_monologue_s": {"low": None, "high": 150.0, "label": "Longest monologue (seconds)"},
    "interruption_rate": {"low": None, "high": 0.1, "label": "Likely interruptions per owner turn"},
    "patience_s": {"low": 0.5, "high": 1.5, "label": "Patience (median gap before you speak, seconds)"},
    "questions_per_hour": {"low": 10.0, "high": 18.0, "label": "Questions per hour"},
    "fillers_per_min": {"low": 0.6, "high": 3.0, "label": "Filler words per speaking minute"},
    "interactivity": {"low": 5.0, "high": None, "label": "Speaker switches per 5 minutes"},
}

# Recordings shorter than this (seconds) are not coached.
MIN_COACHING_SECONDS = 120

# An owner turn starting within this many seconds of the other turn ending can count as a likely interruption.
INTERRUPT_MAX_GAP_S = 0.2

BACKCHANNELS = frozenset(
    {"yeah", "yes", "yep", "mm", "mhm", "right", "ok", "okay", "sure", "uh-huh", "true", "exactly"}
)

# Matched case-insensitively against verbatim words, approximately: "like" is counted only when
# it is followed by a comma or sits between two other fillers (so "like a house" is not a filler).
# Two-word phrases are matched as adjacent word pairs.
FILLER_PHRASES = (
    "um",
    "uh",
    "er",
    "erm",
    "like",
    "you know",
    "i mean",
    "sort of",
    "kind of",
    "basically",
    "actually",
)
