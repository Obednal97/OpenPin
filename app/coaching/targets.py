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
    # Engagement parts (app/coaching/engagement.py). Each part scores 100 at or above "low".
    "laughs_per_hour": {"low": 6.0, "high": None, "label": "Laughs after your lines (per hour)"},
    "follow_up_share": {"low": 0.25, "high": None, "label": "Follow-up questions to you (share of your turns)"},
    "built_on_share": {"low": 0.5, "high": None, "label": "Your points others built on (share)"},
}

# Recordings shorter than this (seconds) are not coached.
MIN_COACHING_SECONDS = 120

# An owner turn starting within this many seconds of the other turn ending can count as a likely interruption.
INTERRUPT_MAX_GAP_S = 0.2

# A same-side run of segments is split into separate turns when the silence between them exceeds this (seconds).
TURN_SPLIT_GAP_S = 10.0

# Engagement (app/coaching/engagement.py).
# A laugh counts for your turn when it starts within this many seconds after the turn ends...
LAUGH_AFTER_S = 2.0
# ...or overlaps this many final seconds of the turn.
LAUGH_OVERLAP_S = 1.0
# A laugh's loudness is measured from the audio from this long before it starts (but never before
# your line ends) to this long after it ends, cut at your next line...
LAUGH_MEASURE_PAD_S = 0.5
# ...and a laugh with less than this much audio left to measure isn't counted.
LAUGH_MIN_MEASURE_S = 0.25
# A laugh at least this loud, relative to your median speech loudness minus this margin (dB), is probably yours.
OWN_LAUGH_MARGIN_DB = 3.0
# Laughter is not checked on recordings longer than this (seconds).
LAUGHTER_MAX_S = 3 * 3600
# A follow-up question has a "?" within this many words of the start of the other person's turn.
FOLLOW_UP_WORDS = 30
# Your turns shorter than this many words (backchannels) are not followed up.
FOLLOW_UP_MIN_TURN_WORDS = 3
# Follow-up and built-on shares need at least this many of your turns or reactions.
ENGAGEMENT_MIN_BASE = 3
# Reactions to your points that count as others building on them.
BUILT_ON_RESPONSES = frozenset({"engaged", "follow_up"})

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
