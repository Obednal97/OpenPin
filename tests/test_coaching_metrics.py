"""Tests for app.coaching.metrics (pure computation)."""

from app.coaching.metrics import compute_metrics, owner_turns

OWNER = {"Speaker 1"}


def seg(start, end, text, speaker):
    return {"start": start, "end": end, "text": text, "speaker": speaker}


def words_of(text, start, speaker, step=0.4):
    out = []
    for i, w in enumerate(text.split()):
        out.append(
            {
                "word": w,
                "start": start + i * step,
                "end": start + i * step + step * 0.8,
                "speaker": speaker,
                "confidence": 0.99,
            }
        )
    return out


def run(segments, words=None, owner=OWNER, duration=600.0, estimated=False):
    return compute_metrics(segments, words, set(owner), duration, estimated)


def test_wpm_excludes_silence():
    # 3 segments of 20 s each (50 words each), separated by 30 s gaps: 150 words in 60 s speaking.
    text = " ".join(["word"] * 50)
    segs = [seg(0, 20, text, "Speaker 1"), seg(50, 70, text, "Speaker 1"), seg(100, 120, text, "Speaker 1")]
    assert run(segs, duration=120.0)["owner"]["pace_wpm"] == 150.0


def test_wpm_per_speaker_reported_for_everyone():
    segs = [
        seg(0, 30, " ".join(["a"] * 60), "Speaker 1"),
        seg(30, 60, " ".join(["b"] * 90), "Speaker 2"),
    ]
    per = run(segs, duration=60.0)["per_speaker"]
    assert per["Speaker 1"]["wpm"] == 120.0
    assert per["Speaker 2"]["wpm"] == 180.0
    assert per["Speaker 2"]["turns"] == 1


def test_talk_share_merges_owner_labels():
    segs = [
        seg(0, 10, "hello there", "Speaker 1"),
        seg(10, 20, "hi", "Speaker 2"),
        seg(20, 30, "more", "Speaker 3"),
        seg(30, 40, "ok then", "Speaker 2"),
    ]
    m = run(segs, owner={"Speaker 1", "Speaker 3"}, duration=40.0)
    assert m["owner"]["talk_share"] == 0.5


def test_longest_monologue_merges_consecutive_owner_segments():
    segs = [
        seg(0, 30, "one", "Speaker 1"),
        seg(30, 70, "two", "Speaker 3"),
        seg(70, 80, "other", "Speaker 2"),
        seg(80, 90, "three", "Speaker 1"),
    ]
    m = run(segs, owner={"Speaker 1", "Speaker 3"}, duration=90.0)
    assert m["owner"]["longest_monologue_s"] == 70.0
    assert any(x["kind"] == "monologue" and x["t"] == 0 for x in m["moments"])


def test_owner_turns_returns_both_sides_merged():
    segs = [
        seg(0, 5, "a", "Speaker 1"),
        seg(5, 8, "b", "Speaker 3"),
        seg(8, 12, "c", "Speaker 2"),
        seg(12, 15, "d", None),
        seg(15, 20, "e", "Speaker 1"),
    ]
    turns = owner_turns(segs, {"Speaker 1", "Speaker 3"})
    assert [t["owner"] for t in turns] == [True, False, True]
    assert turns[0] == {"start": 0, "end": 8, "text": "a b", "speaker": "Speaker 1", "owner": True}
    assert turns[1]["speaker"] == "Speaker 2" and turns[1]["text"] == "c d"


def _interrupt_case(other_text, gap, owner_text="Actually I disagree with that."):
    segs = [
        seg(0, 10, other_text, "Speaker 2"),
        seg(10 + gap, 20, owner_text, "Speaker 1"),
    ]
    return run(segs, duration=20.0)


def test_likely_interruption_needs_short_gap_and_cut_off_sentence():
    m = _interrupt_case("so we need the agent and", 0.1)
    assert m["owner"]["interruptions_made"] == 1
    assert m["owner"]["interruption_rate"] == 1.0
    moment = [x for x in m["moments"] if x["kind"] == "interruption"][0]
    assert moment["likely"] is True and moment["speaker"] == "Speaker 1"
    assert _interrupt_case("this works.", 0.1)["owner"]["interruptions_made"] == 0
    assert _interrupt_case("so we need the agent and", 0.5)["owner"]["interruptions_made"] == 0


def test_backchannel_is_not_an_interruption():
    m = _interrupt_case("so we need the agent and", 0.1, owner_text="Yeah.")
    assert m["owner"]["interruptions_made"] == 0


def test_interrupted_by_others_mirrors_rule():
    segs = [
        seg(0, 10, "I was saying that we", "Speaker 1"),
        seg(10.1, 20, "No, wait a second.", "Speaker 2"),
    ]
    m = run(segs, duration=20.0)
    assert m["owner"]["interrupted_by_others"] == 1
    assert m["owner"]["interruptions_made"] == 0


def test_patience_is_median_gap_before_owner_turns():
    segs = [
        seg(0, 10, "q one?", "Speaker 2"),
        seg(10.5, 20, "a one", "Speaker 1"),
        seg(20, 30, "q two?", "Speaker 2"),
        seg(31.5, 40, "a two", "Speaker 1"),
        seg(40, 50, "q three?", "Speaker 2"),
        seg(51.0, 60, "a three", "Speaker 1"),
    ]
    assert run(segs, duration=60.0)["owner"]["patience_s"] == 1.0


def test_questions_count_owner_question_marks():
    segs = [
        seg(0, 10, "What do you think? And why? Fine.", "Speaker 1"),
        seg(10, 20, "Who knows?", "Speaker 2"),
    ]
    o = run(segs, duration=1800.0)["owner"]
    assert o["questions"] == 2
    assert o["questions_per_hour"] == 4.0


def test_fillers_from_words_only():
    segs = [seg(0, 60, "um so like you know it works", "Speaker 1")]
    m = run(segs, words=None, duration=60.0)
    assert m["owner"]["fillers_per_min"] is None
    assert m["owner"]["fillers"] is None
    assert m["availability"]["fillers_per_min"] == "unavailable"


def test_fillers_counted_from_words():
    text = "um so like, you know it is like a house and uh basically fine"
    segs = [seg(0, 30, "so it is like a house and fine", "Speaker 1")]
    words = words_of(text, 0.0, "Speaker 1") + words_of("um um", 40.0, "Speaker 2")
    m = run(segs, words=words, duration=60.0)
    # um, like, (you know), uh, basically = 5; "like a house" is not a filler; other speaker ignored.
    assert m["owner"]["fillers"] == 5
    assert m["availability"]["fillers_per_min"] == "exact"
    assert m["owner"]["fillers_per_min"] is not None


def test_estimated_timestamps_mark_timing_metrics_unavailable():
    segs = [
        seg(0, 10, "so we need the agent and", "Speaker 1"),
        seg(10.1, 20, "hello there my friend", "Speaker 2"),
        seg(20.1, 30, "I think so", "Speaker 1"),
    ]
    m = run(segs, duration=30.0, estimated=True)
    for key in ("pace_wpm", "interruption_rate", "patience_s"):
        assert m["owner"][key] is None
        assert m["availability"][key] == "unavailable"
    assert m["owner"]["questions"] == 0


def test_availability_estimated_without_words_exact_with_words():
    segs = [seg(0, 10, "hello there friend", "Speaker 1")]
    assert run(segs, duration=10.0)["availability"]["pace_wpm"] == "estimated"
    words = words_of("hello there friend", 0.0, "Speaker 1")
    assert run(segs, words=words, duration=10.0)["availability"]["pace_wpm"] == "exact"


def test_interactivity_excludes_backchannels():
    segs = [
        seg(0, 30, "long one", "Speaker 1"),
        seg(30, 31, "Mm.", "Speaker 2"),
        seg(31, 60, "carry on", "Speaker 1"),
        seg(60, 90, "my turn", "Speaker 2"),
        seg(90, 120, "back", "Speaker 1"),
    ]
    # Backchannel ignored: owner, other, owner = 2 switches in 120 s = 5 per 5 min.
    assert run(segs, duration=120.0)["owner"]["interactivity"] == 5.0


def test_fast_stretch_moment():
    fast = " ".join(["w"] * 60)  # 360 wpm over 10 s
    segs = [seg(0, 10, fast, "Speaker 1"), seg(10, 20, "ok", "Speaker 2")]
    m = run(segs, duration=20.0)
    assert any(x["kind"] == "fast" for x in m["moments"])


def test_filler_like_comma_uses_stored_word_shape():
    words = [
        {"word": "so", "start": 0.0, "end": 0.3, "speaker": "Speaker 1", "confidence": 0.9},
        {"word": "like,", "start": 0.4, "end": 0.7, "speaker": "Speaker 1", "confidence": 0.9},
        {"word": "it", "start": 0.8, "end": 1.0, "speaker": "Speaker 1", "confidence": 0.9},
    ]
    segs = [seg(0, 1, "so it", "Speaker 1")]
    assert run(segs, words=words, duration=60.0)["owner"]["fillers"] == 1


def test_per_speaker_wpm_none_when_timestamps_estimated():
    segs = [seg(0, 30, " ".join(["a"] * 60), "Speaker 1"), seg(30, 60, " ".join(["b"] * 12), "Speaker 2")]
    per = run(segs, duration=60.0, estimated=True)["per_speaker"]
    assert per["Speaker 1"]["wpm"] is None and per["Speaker 2"]["wpm"] is None


def test_long_same_side_gap_splits_turn():
    segs = [seg(0, 5, "first", "Speaker 1"), seg(405, 410, "second", "Speaker 1")]
    turns = owner_turns(segs, OWNER)
    assert len(turns) == 2
    m = run(segs, duration=410.0)
    assert m["owner"]["longest_monologue_s"] == 5.0
    assert not any(
        x["kind"] in ("monologue", "fast") and x["t"] == 0 and x["quote"] == "first second" for x in m["moments"]
    )


def test_backchannel_turns_excluded_from_patience_but_not_rate_denominator():
    segs = [
        seg(0, 10, "tell me more about it", "Speaker 2"),
        seg(10.0, 11, "Yeah.", "Speaker 1"),
        seg(11, 20, "and then we went on.", "Speaker 2"),
        seg(21.0, 30, "That sounds good.", "Speaker 1"),
    ]
    o = run(segs, duration=30.0)["owner"]
    assert o["patience_s"] == 1.0
    # the backchannel is not an interruption, the real turn follows a finished sentence
    assert o["interruptions_made"] == 0
    assert o["interruption_rate"] == 0.0


def test_owner_with_zero_turns_gives_all_none():
    segs = [seg(0, 10, "hello", "Speaker 2"), seg(10, 20, "there", "Speaker 3")]
    m = run(segs, duration=20.0)
    assert all(v is None for v in m["owner"].values())
    assert all(v == "unavailable" for v in m["availability"].values())
    assert m["moments"] == []


def test_adjacent_other_speakers_then_owner_cut_in_counts_once():
    segs = [
        seg(0, 10, "we should talk", "Speaker 2"),
        seg(10, 20, "about the plan and", "Speaker 3"),
        seg(20.1, 30, "Let me stop you there.", "Speaker 1"),
    ]
    m = run(segs, duration=30.0)
    assert m["owner"]["interruptions_made"] == 1
    assert [x["likely"] for x in m["moments"] if x["kind"] == "interruption"] == [True]


def test_filler_burst_moment():
    words = words_of("um uh er so then", 5.0, "Speaker 1", step=1.0)
    segs = [seg(0, 20, "so then", "Speaker 1")]
    m = run(segs, words=words, duration=60.0)
    bursts = [x for x in m["moments"] if x["kind"] == "filler_burst"]
    assert len(bursts) == 1 and bursts[0]["t"] == 5.0
