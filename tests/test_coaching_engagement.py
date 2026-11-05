"""Engagement block: laughs after your lines, follow-up questions to you, points others built on.

Pure functions apart from ``owner_loudness_db``, which reads a synthetic WAV.
"""

import math

import pytest

from app.coaching import engagement as E

OWNER = ["Speaker 1"]
HOUR = 3600.0
LONG_FOLLOW_UP = " ".join(["word"] * 30) + " but what then?"

# Your qualifying turns: 0-10, 21-30 and 61-70 (41-50 is a backchannel).
SEGS = [
    {"start": 0.0, "end": 10.0, "text": "I think we should launch in March next year.", "speaker": "Speaker 1"},
    {"start": 11.0, "end": 20.0, "text": "Why March? What changes then?", "speaker": "Speaker 2"},
    {"start": 21.0, "end": 30.0, "text": "Because the budget lands then, honestly.", "speaker": "Speaker 1"},
    {"start": 31.0, "end": 40.0, "text": "Right, makes sense.", "speaker": "Speaker 2"},
    {"start": 41.0, "end": 42.0, "text": "Yeah.", "speaker": "Speaker 1"},
    {"start": 43.0, "end": 50.0, "text": "And what about hiring?", "speaker": "Speaker 2"},
    {"start": 61.0, "end": 70.0, "text": "We hire two engineers first, then design.", "speaker": "Speaker 1"},
    {"start": 71.0, "end": 80.0, "text": LONG_FOLLOW_UP, "speaker": "Speaker 2"},
]


def ev(start, end=None, loudness=-40.0, score=0.5):
    return {"start": start, "end": start + 1.0 if end is None else end, "score": score, "loudness_db": loudness}


def compute(events=(), status="done", reason="", review=None, duration=HOUR, loud=-20.0, segs=SEGS):
    return E.compute(segs, OWNER, list(events), status, reason, review, duration, loud)


def reactions(*responses):
    return {"reactions": [{"t": 0.0, "owner_point": "p", "response": r, "evidence": "e"} for r in responses]}


# ---------------------------------------------------------------- laughs


def test_block_has_the_spec_shape():
    out = compute()
    assert set(out) == {
        "score",
        "laughs",
        "laughs_count",
        "laughs_per_hour",
        "follow_ups",
        "follow_up_share",
        "follow_up_turns",
        "built_on",
        "built_on_share",
        "laughter_status",
        "laughter_reason",
    }


def test_laugh_just_after_your_line_counts_with_the_line():
    out = compute([ev(11.5)])
    assert out["laughs"] == [{"t": 11.5, "line_start": 0.0, "line_text": SEGS[0]["text"]}]
    assert out["laughs_count"] == 1
    assert out["laughs_per_hour"] == 1.0
    assert out["laughter_status"] == "done"


@pytest.mark.parametrize(
    ("event", "counted"),
    [
        (ev(12.0), True),  # starts exactly 2.0 s after your turn ends
        (ev(12.01), False),  # just past the 2.0 s window
        (ev(8.0, 9.0), True),  # ends exactly where your turn's last 1.0 s begins
        (ev(8.0, 8.99), False),  # ends just before your turn's last 1.0 s
        (ev(9.5, 10.5), True),  # overlaps the end of your turn
    ],
)
def test_window_edges(event, counted):
    assert compute([event])["laughs_count"] == (1 if counted else 0)


def test_your_own_loud_laugh_is_not_counted():
    """Review focus 1: your clip-on mic hears you loudest, so a loud laugh after your line is yours."""
    out = compute([ev(11.0, loudness=-18.0)], loud=-20.0)
    assert out["laughs_count"] == 0 and out["laughs"] == []
    # The margin: at your median minus 3 dB it's still yours; quieter than that it's someone else's.
    assert compute([ev(11.0, loudness=-23.0)], loud=-20.0)["laughs_count"] == 0
    assert compute([ev(11.0, loudness=-23.5)], loud=-20.0)["laughs_count"] == 1


def test_your_own_laugh_does_not_hide_a_quieter_one_after_the_same_line():
    out = compute([ev(10.2, loudness=-15.0), ev(11.5, loudness=-40.0)])
    assert [x["t"] for x in out["laughs"]] == [11.5]


def test_at_most_one_laugh_per_turn():
    out = compute([ev(10.5), ev(11.8), ev(31.0)])
    assert out["laughs_count"] == 2
    assert [x["line_start"] for x in out["laughs"]] == [0.0, 21.0]


def test_laugh_far_from_any_of_your_lines_is_not_counted():
    assert compute([ev(15.0), ev(55.0)])["laughs_count"] == 0


def test_laughs_without_your_voice_level_are_unavailable():
    """Without your loudness a laugh can't be told apart from yours, so nothing is counted."""
    out = compute([ev(11.0)], loud=None)
    assert out["laughter_status"] == "unavailable"
    assert out["laughter_reason"]
    assert out["laughs_count"] is None and out["laughs_per_hour"] is None


def test_no_events_need_no_voice_level():
    out = compute([], loud=None)
    assert out["laughter_status"] == "done"
    assert out["laughs_count"] == 0 and out["laughs_per_hour"] == 0.0


def test_unavailable_laughter_counts_nothing_and_keeps_the_reason():
    out = compute([ev(11.0)], status="unavailable", reason="no model")
    assert out["laughter_status"] == "unavailable" and out["laughter_reason"] == "no model"
    assert out["laughs"] == [] and out["laughs_count"] is None and out["laughs_per_hour"] is None


def test_malformed_events_are_skipped():
    out = compute([{"start": "x"}, None, {"start": 11.0, "end": math.nan, "loudness_db": -40}, ev(11.5)])
    assert [x["t"] for x in out["laughs"]] == [11.5]


# ---------------------------------------------------------------- follow-ups


def test_follow_up_questions_to_you():
    out = compute()
    # 11.0 has a "?"; 31.0 doesn't; 43.0 follows a backchannel; 71.0's "?" is past 30 words.
    assert out["follow_ups"] == [{"t": 11.0, "text": SEGS[1]["text"]}]
    assert out["follow_up_share"] == round(1 / 3, 4)
    assert out["follow_up_turns"] == 3  # "1 of 3 of your turns"


def test_question_within_the_first_30_words_counts():
    segs = [*SEGS[:-1], {**SEGS[-1], "text": " ".join(["word"] * 28) + " what then?"}]
    out = compute(segs=segs)
    assert [f["t"] for f in out["follow_ups"]] == [11.0, 71.0]


def test_backchannel_turns_are_not_counted():
    segs = [s for s in SEGS if s["start"] != 41.0]  # without the "Yeah." the 43.0 question follows 21.0's turn
    segs = [s for s in segs if s["start"] != 31.0]
    out = compute(segs=segs)
    assert [f["t"] for f in out["follow_ups"]] == [11.0, 43.0]
    assert out["follow_up_share"] == round(2 / 3, 4)


def test_follow_up_share_needs_three_of_your_turns():
    out = compute(segs=SEGS[:4])
    assert out["follow_ups"] == [{"t": 11.0, "text": SEGS[1]["text"]}]
    assert out["follow_up_share"] is None
    assert out["follow_up_turns"] == 2


# ---------------------------------------------------------------- built on


def test_built_on_from_the_reviews_reactions():
    out = compute(review=reactions("engaged", "follow_up", "agreed", "short"))
    assert out["built_on"] == {"count": 2, "of": 4}
    assert out["built_on_share"] == 0.5


def test_review_off_has_no_built_on_but_still_scores():
    out = compute([ev(11.0)], review=None)
    assert out["built_on"] is None and out["built_on_share"] is None
    assert out["score"] is not None


def test_built_on_needs_three_reactions():
    out = compute(review=reactions("engaged", "engaged"))
    assert out["built_on"] is None and out["built_on_share"] is None


# ---------------------------------------------------------------- score


def test_score_maps_each_part_against_its_target_and_means_them():
    # laughs: 3 per hour -> 50; follow-ups: 1/3 >= 0.25 -> 100; built on: 1/4 -> 50.
    out = compute([ev(11.0), ev(31.0), ev(71.0)], review=reactions("engaged", "agreed", "short", "deflected"))
    assert out["laughs_per_hour"] == 3.0
    assert out["score"] == round((50 + 100 + 50) / 3)


def test_parts_at_or_above_target_score_100():
    out = compute([ev(11.0)], duration=600.0, review=reactions("engaged", "engaged", "short"))
    assert out["laughs_per_hour"] == 6.0
    assert out["score"] == 100


def test_missing_parts_never_count_as_zero():
    """Review focus 3: no audio and no review must not drag the score down."""
    out = compute(status="unavailable", reason="no audio", review=None)
    assert out["follow_up_share"] == round(1 / 3, 4)
    assert out["score"] == 100


def test_no_parts_no_score():
    out = compute(status="unavailable", reason="no audio", segs=SEGS[:4])
    assert out["score"] is None


def test_zero_laughs_score_zero_for_that_part():
    out = compute([], status="done", segs=SEGS[:4])  # follow-ups unavailable, laughs ran and found none
    assert out["laughs_per_hour"] == 0.0
    assert out["score"] == 0


def test_no_duration_means_no_rate():
    out = compute([ev(11.0)], duration=0.0, segs=SEGS[:4])
    assert out["laughs_count"] == 1 and out["laughs_per_hour"] is None
    assert out["score"] is None


# ---------------------------------------------------------------- your loudness


def _wav_copy(tmp_path, monkeypatch, data, rate=16000):
    sf = pytest.importorskip("soundfile")
    src = tmp_path / "src.wav"
    sf.write(src, data, rate)
    import src.transcribe as tr

    copy = tmp_path / "copy.wav"
    monkeypatch.setattr(tr, "_to_wav16k", lambda p: str(copy) if copy.write_bytes(src.read_bytes()) else None)
    return copy


def test_owner_loudness_is_the_median_of_your_lines_peak_frames(tmp_path, monkeypatch):
    np = pytest.importorskip("numpy")
    sf = pytest.importorskip("soundfile")
    second = 16000
    data = np.concatenate(
        [
            np.full(second, 0.1),  # you: -20 dBFS
            np.full(second, 0.9),  # them, much louder
            np.full(second, 0.2),  # you: about -14 dBFS
            np.full(second, 0.05),  # you: about -26 dBFS
        ]
    ).astype("float32")
    copy = _wav_copy(tmp_path, monkeypatch, data)
    reads = []
    real_read = sf.read

    def spy(path, *args, **kwargs):
        reads.append(kwargs)
        return real_read(path, *args, **kwargs)

    monkeypatch.setattr(sf, "read", spy)
    monkeypatch.setattr(E, "READ_CHUNK_FRAMES", 4000)
    segs = [
        {"start": 0.0, "end": 1.0, "speaker": "Speaker 1"},
        {"start": 1.0, "end": 2.0, "speaker": "Speaker 2"},
        {"start": 2.0, "end": 3.0, "speaker": "Speaker 1"},
        {"start": 3.0, "end": 4.0, "speaker": "Speaker 1"},
    ]
    assert E.owner_loudness_db("x.ogg", segs, OWNER) == pytest.approx(-20.0, abs=0.01)
    # Only your lines are read, in chunks, never the whole file.
    assert len(reads) == 3 * 4
    assert all(r.get("dtype") == "float32" and r.get("frames") <= 4000 for r in reads)
    assert not copy.exists()


def test_owner_loudness_none_without_your_lines_or_audio(tmp_path, monkeypatch):
    np = pytest.importorskip("numpy")
    _wav_copy(tmp_path, monkeypatch, np.full(16000, 0.5).astype("float32"))
    assert E.owner_loudness_db("x.ogg", [{"start": 0.0, "end": 1.0, "speaker": "Speaker 2"}], OWNER) is None
    import src.transcribe as tr

    monkeypatch.setattr(tr, "_to_wav16k", lambda p: None)
    assert E.owner_loudness_db("x.ogg", [{"start": 0.0, "end": 1.0, "speaker": "Speaker 1"}], OWNER) is None
