"""Engagement block: laughs after your lines, follow-up questions to you, points others built on.

Pure functions apart from ``measure_loudness``, which reads a synthetic WAV.
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


def measured(events, loud=-20.0, segs=SEGS):
    """What ``measure_loudness`` would return: in these tests an event's ``loudness_db`` stands
    for the loudness the audio has over its candidate window."""
    if loud is None:
        return None
    evs = E._events(list(events))
    return {
        "owner_db": loud,
        "windows": [evs[c["event"]]["loudness_db"] for c in E.laugh_candidates(segs, OWNER, events)],
    }


def compute(events=(), status="done", reason="", review=None, duration=HOUR, loud=-20.0, segs=SEGS, loudness=...):
    events = list(events)
    if loudness is ...:
        loudness = measured(events, loud, segs)
    return E.compute(segs, OWNER, events, status, reason, review, duration, loudness)


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
        (ev(8.0, 8.99), False),  # ends before your turn's last 1.0 s
        # Overlapping your turn's last 1.0 s, but measured only after your line ends (to end + 0.5 s):
        (ev(9.0, 9.75), True),  # exactly 0.25 s left to measure
        (ev(9.0, 9.74), False),  # under 0.25 s left
        (ev(8.0, 9.0), False),  # entirely inside your line: nothing of it after your line
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


def test_laugh_windows_start_after_your_line_and_stop_at_your_next():
    cands = E.laugh_candidates(SEGS, OWNER, [ev(9.5, 12.5), ev(29.0, 40.8)])
    # The detector's event reaches back into your line; the window starts where your line ends.
    assert [(c["t"], c["line_start"], c["window"]) for c in cands] == [
        (9.5, 0.0, (10.0, 13.0)),
        (29.0, 21.0, (30.0, 41.0)),  # cut at your next line (41.0), not 41.3
    ]


def test_never_falls_back_to_the_detectors_loudness():
    """An unmeasured window isn't counted, even when the detector's own loudness looks quiet."""
    out = compute([ev(11.0, loudness=-40.0)], loudness={"owner_db": -20.0, "windows": [None]})
    assert out["laughter_status"] == "done" and out["laughs_count"] == 0


def test_loudness_for_other_windows_is_unavailable():
    out = compute([ev(11.0)], loudness={"owner_db": -20.0, "windows": []})
    assert out["laughter_status"] == "unavailable" and out["laughs_count"] is None


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


def test_measure_loudness_reads_your_lines_and_the_windows_once(tmp_path, monkeypatch):
    np = pytest.importorskip("numpy")
    sf = pytest.importorskip("soundfile")
    second = 16000
    data = np.concatenate(
        [
            np.full(second, 0.1),  # you: -20 dBFS
            np.full(second, 0.9),  # them, much louder
            np.full(second, 0.2),  # you: about -14 dBFS
            np.full(second, 0.05),  # you: about -26 dBFS
            np.full(second, 0.025),  # a laugh: about -32 dBFS
        ]
    ).astype("float32")
    copy = _wav_copy(tmp_path, monkeypatch, data)
    import src.transcribe as tr

    convert = tr._to_wav16k
    conversions = []
    monkeypatch.setattr(tr, "_to_wav16k", lambda p: conversions.append(p) or convert(p))
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
    out = E.measure_loudness("x.ogg", segs, OWNER, [(4.0, 5.0), (5.0, 5.5)])
    assert out["owner_db"] == pytest.approx(-20.0, abs=0.01)
    assert out["windows"][0] == pytest.approx(-32.04, abs=0.01)
    assert out["windows"][1] is None  # past the end of the audio: nothing to measure
    assert conversions == ["x.ogg"]  # one conversion for everything
    # Only your lines and the windows are read, in chunks, never the whole file.
    assert len(reads) == 3 * 4 + 4 + 1
    assert all(r.get("dtype") == "float32" and r.get("frames") <= 4000 for r in reads)
    assert not copy.exists()


def test_measure_loudness_none_without_audio(monkeypatch):
    import src.transcribe as tr

    monkeypatch.setattr(tr, "_to_wav16k", lambda p: None)
    assert E.measure_loudness("x.ogg", SEGS, OWNER, [(10.0, 11.0)]) is None


def test_measure_loudness_without_your_lines_has_no_owner_level(tmp_path, monkeypatch):
    np = pytest.importorskip("numpy")
    _wav_copy(tmp_path, monkeypatch, np.full(16000, 0.5).astype("float32"))
    out = E.measure_loudness("x.ogg", [{"start": 0.0, "end": 1.0, "speaker": "Speaker 2"}], OWNER, [])
    assert out == {"owner_db": None, "windows": []}


def _laugh_after_your_line(tmp_path, monkeypatch, laugh_amplitude):
    """You at -20 dBFS until 10.0 s, a laugh from 10.3 to 11.8 s, them speaking from 14 s.

    The detector's event reaches back into your line and carries your loudness (-20 dBFS)."""
    np = pytest.importorskip("numpy")
    rate = 16000
    audio = np.zeros(rate * 20, dtype="float32")
    audio[: rate * 10] = 0.1
    audio[int(rate * 10.3) : int(rate * 11.8)] = laugh_amplitude
    audio[rate * 14 :] = 0.05
    _wav_copy(tmp_path, monkeypatch, audio)
    segs = [
        {"start": 0.0, "end": 10.0, "text": "And that was the whole of the launch plan.", "speaker": "Speaker 1"},
        {"start": 14.0, "end": 20.0, "text": "Brilliant, thank you.", "speaker": "Speaker 2"},
    ]
    events = [{"start": 9.5, "end": 12.5, "score": 0.6, "loudness_db": -20.0}]
    windows = [c["window"] for c in E.laugh_candidates(segs, OWNER, events)]
    loudness = E.measure_loudness("x.ogg", segs, OWNER, windows)
    return E.compute(segs, OWNER, events, "done", "", None, 1800.0, loudness)


def test_a_quiet_laugh_straight_after_your_loud_line_counts(tmp_path, monkeypatch):
    """Review fix: the laugh is measured after your line, not over the detector's windows."""
    out = _laugh_after_your_line(tmp_path, monkeypatch, 0.025)  # about -32 dBFS
    assert out["laughs_count"] == 1
    assert out["laughs"][0]["t"] == 9.5


def test_your_own_loud_laugh_after_your_line_still_does_not_count(tmp_path, monkeypatch):
    out = _laugh_after_your_line(tmp_path, monkeypatch, 0.126)  # about -18 dBFS: you
    assert out["laughter_status"] == "done" and out["laughs_count"] == 0


def test_architecture_describes_the_loudness_rule_and_the_voice_level_reason():
    from pathlib import Path

    doc = (Path(__file__).resolve().parent.parent / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    section = doc[doc.index("### Engagement and laughter") :]
    section = section[: section.index("\n### ", 1)]
    assert "voice level couldn't be" in section  # listed with the other unavailable reasons
    assert "measure_loudness" in section and "never before your" in section and "0.25 s" in section
    assert "within 1 s" in section


# ---------------------------------------------------------------- final review fixes


def test_a_non_string_response_never_raises():
    review = {
        "reactions": [
            {"t": 0.0, "response": ["engaged"]},  # unhashable
            {"t": 0.0, "response": {"x": 1}},
            {"t": 0.0, "response": None},
            {"t": 0.0, "response": "engaged"},
        ]
    }
    out = compute(review=review)
    assert out["built_on"] == {"count": 1, "of": 4}


def test_a_laugh_between_two_of_your_lines_counts():
    """Your lines 0-5 and 7-12 merge into one turn; the laugh at 5.2 follows your first line."""
    segs = [
        {"start": 0.0, "end": 5.0, "text": "So I told him the price.", "speaker": "Speaker 1"},
        {"start": 7.0, "end": 12.0, "text": "And he just walked out.", "speaker": "Speaker 1"},
        {"start": 13.0, "end": 20.0, "text": "No way, really?", "speaker": "Speaker 2"},
    ]
    events = [ev(5.2, 6.8)]
    cands = E.laugh_candidates(segs, OWNER, events)
    # Anchored on the end of your line at 5.0, cut at your next line at 7.0.
    assert [(c["line_start"], c["window"]) for c in cands] == [(0.0, (5.0, 7.0))]
    out = compute(events, segs=segs)
    assert out["laughs_count"] == 1
    assert out["laughs"] == [{"t": 5.2, "line_start": 0.0, "line_text": "So I told him the price."}]


def test_one_laugh_per_line_of_yours():
    segs = [
        {"start": 0.0, "end": 5.0, "text": "So I told him the price.", "speaker": "Speaker 1"},
        {"start": 9.0, "end": 12.0, "text": "And he just walked out.", "speaker": "Speaker 1"},
        {"start": 13.0, "end": 20.0, "text": "No way, really?", "speaker": "Speaker 2"},
    ]
    out = compute([ev(5.2, 6.0), ev(6.5, 7.5), ev(12.5, 13.5)], segs=segs)
    assert [x["t"] for x in out["laughs"]] == [5.2, 12.5]


def test_measure_loudness_reuses_a_given_wav_and_leaves_it_to_the_caller(tmp_path, monkeypatch):
    np = pytest.importorskip("numpy")
    sf = pytest.importorskip("soundfile")
    wav = tmp_path / "detector.wav"
    sf.write(wav, np.full(16000 * 2, 0.1, dtype="float32"), 16000)
    import src.transcribe as tr

    monkeypatch.setattr(tr, "_to_wav16k", lambda p: pytest.fail("converted again"))
    segs = [{"start": 0.0, "end": 1.0, "speaker": "Speaker 1"}]
    out = E.measure_loudness("x.ogg", segs, OWNER, [(1.0, 2.0)], wav=str(wav))
    assert out["owner_db"] == pytest.approx(-20.0, abs=0.01)
    assert out["windows"][0] == pytest.approx(-20.0, abs=0.01)
    assert wav.exists()


def test_architecture_uses_the_same_reason_wording_and_the_final_rules():
    from pathlib import Path

    from app.coaching import laughter

    doc = (Path(__file__).resolve().parent.parent / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    section = doc[doc.index("### Engagement and laughter") :]
    section = " ".join(section[: section.index("\n### ", 1)].split())
    for reason in (laughter.REASON_NO_AUDIO, laughter.REASON_TOO_LONG, laughter.REASON_FAILED, E.NO_VOICE_LEVEL):
        assert f'"{reason}"' in section, reason
    assert "No audio file on this Mac" not in doc and "Laughter detection failed" not in doc
    assert "one of your lines" in section  # anchored on your segments, not merged turns
    assert "an hour" in section  # the download retry pause
    assert "never the focus" in section  # laughs per hour isn't coached on
    assert "keep_wav" in section  # the detector's WAV is reused
