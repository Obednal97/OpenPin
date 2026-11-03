"""Pages made by older app versions (no Session ID): recognised by content,
adopted instead of duplicated, or left for the owner to decide.

No live Notion calls: ``notion.mcp_call`` is a FakeNotion.
"""

import json
import types
from unittest.mock import patch

import pytest

from tests.test_notion import PAGE_URL, SID, FakeNotion, _cfg, _raise, _schema, _text_result, _write_transcript

# ---------------------------------------------------------------------------
# A recording as transcribed now (Deepgram-style segments) and as the old app
# pushed it (Whisper-style: other punctuation, digits, no hesitations, a few
# words heard differently, other segment boundaries and speaker names).
# ---------------------------------------------------------------------------

# fmt: off
DEEPGRAM_SEGMENTS = [
    (0, "Okay, so let's get started. Um, the first thing on the agenda is the launch date for the new onboarding flow."),
    (9, "We said two weeks ago that we'd ship on the fifteenth, but the design review slipped by about four days."),
    (18, "Sarah, do you wanna give us a quick update on where the designs are at?"),
    (24, "Yeah, sure. So the main screens are done. We've got the welcome screen, the permissions screen and the account setup."),  # noqa: E501
    (34, "The thing that's still open is the empty state for the recordings list. I've got three options and I'd like feedback by Thursday."),  # noqa: E501
    (45, "Great. And engineering, are we on track for the API changes?"),
    (50, "Mostly. The sync endpoint is finished, but we found a bug with the token refresh that only shows up after an hour."),  # noqa: E501
    (60, "Tom's looking at it now. I think it's maybe a day of work, two at most."),
    (67, "Okay. What about testing? Do we have enough devices?"),
    (72, "We've got six test phones and two of the older models. I'd like one more Android tablet if we can get it."),
    (82, "I can order one today. Anything else blocking?"),
    (86, "Not really. The only risk is the app store review, which took nine days last time."),
    (94, "Right, so if we submit on the tenth we should be fine even with a slow review."),
    (101, "Let's do that. Submit on the tenth, launch on the fifteenth, and we'll have a go, no-go meeting on the twelfth."),  # noqa: E501
    (111, "Sounds good. I'll send the notes out after this. Thanks, everyone."),
]
DURATION = 118.0

WHISPER_LINES = [
    ("00:00", "Speaker 1", "OK so let's get started. The first thing on the agenda is the launch date for the new onboarding flow. We said 2 weeks ago that we would ship on the 15th,"),  # noqa: E501
    ("00:11", "Speaker 1", "but the design review slipped by about 4 days. Sarah, do you want to give us a quick update on where the design is at?"),  # noqa: E501
    ("00:23", "Speaker 2", "Yeah sure. So the main screens are done, we've got the welcome screen, the permission screen and the account set up. The thing that's still open is the empty state for the recording list."),  # noqa: E501
    ("00:38", "Speaker 2", "I've got 3 options and I'd like feedback by Thursday."),
    ("00:44", "Speaker 1", "Great, and engineering, are we on track for the API changes?"),
    ("00:49", "Speaker 3", "Mostly. The sync end point is finished but we found a bug with the token refresh that only shows up after an hour. Tom's looking at it now. I think it's maybe a day of work, 2 at most."),  # noqa: E501
    ("01:06", "Speaker 1", "OK, what about testing? Do we have enough devices?"),
    ("01:11", "Speaker 3", "We've got 6 test phones and 2 of the older models. I'd like one more Android tablet if we can get it."),  # noqa: E501
    ("01:21", "Speaker 1", "I can order one today. Anything else blocking?"),
    ("01:25", "Speaker 3", "Not really, the only risk is the App Store review which took 9 days last time."),
    ("01:33", "Speaker 1", "Right. So if we submit on the 10th, we should be fine, even with a slow review. Let's do that. Submit on the 10th, launch on the 15th, and we'll have a go/no-go meeting on the 12th."),  # noqa: E501
    ("01:50", "Speaker 2", "Sounds good, I'll send the notes out after this. Thanks everyone."),
]

# Same team, same project, a different meeting a week later.
SAME_TOPIC_LINES = [
    ("00:00", "Speaker 1", "Okay, let's get started. So the launch of the new onboarding flow is now next week."),
    ("00:07", "Speaker 1", "Sarah, where are the designs at? Is the empty state for the recordings list done?"),
    ("00:14", "Speaker 2", "Yes, we went with option two, the one with the illustration. Feedback was good."),
    ("00:22", "Speaker 1", "Great. Engineering, is the token refresh bug fixed? Tom found it, it was the clock on the server."),
    ("00:34", "Speaker 3", "What about testing? We got the extra Android tablet, so we've got nine devices now."),
    ("00:45", "Speaker 3", "The app store review took two days this time, so we're fine. We submitted on the tenth."),
    ("01:00", "Speaker 1", "Let's have the go no-go meeting tomorrow then. I'll send the notes out after this."),
    ("01:52", "Speaker 1", "Thanks everyone, see you on Thursday."),
]

UNRELATED_LINES = [
    ("00:00", "Speaker 1", "Alright, thanks for joining. Today I want to go through the quarterly numbers and then hiring."),
    ("00:10", "Speaker 1", "Revenue was up about twelve percent on last quarter, mostly from the annual plans."),
    ("00:20", "Speaker 2", "Churn is flat, but monthly users drop off after the second month. Can someone look at cohorts?"),
    ("00:40", "Speaker 1", "On hiring, we've got two open roles, a senior backend engineer and a designer."),
    ("01:00", "Speaker 2", "For the backend role we've had forty applicants and five of them look strong."),
    ("01:50", "Speaker 1", "The office lease is up in March. Let's take that offline and talk on Friday."),
]

# fmt: on

SID2 = "1715600000"
DS_UUID = "ds-id"
LEGACY_ID = "1111aaaa2222bbbb3333cccc4444dddd"
LEGACY_URL = f"https://www.notion.so/myteam/Launch-planning-{LEGACY_ID}"
OTHER_ID = "5555eeee6666ffff7777aaaa8888bbbb"
OTHER_URL = f"https://www.notion.so/myteam/Quarterly-numbers-{OTHER_ID}"
THIRD_ID = "9999aaaa0000bbbb1111cccc2222dddd"
THIRD_URL = f"https://www.notion.so/myteam/Launch-planning-copy-{THIRD_ID}"
APP_ID = "abcdabcdabcdabcdabcdabcdabcdabcd"
APP_URL = f"https://www.notion.so/myteam/Standup-{APP_ID}"
NEW_URL = "https://www.notion.so/myteam/New-0123456789abcdef0123456789abcdef"

CREATED_AFTER = "2024-05-06T14:00:00.000Z"  # SID is 2024-05-06 12:53 UTC


def _dashed(h):
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def deepgram_transcript(**extra):
    t = {
        "text": " ".join(text for _, text in DEEPGRAM_SEGMENTS),
        "segments": [{"start": s, "end": s + 5, "text": text} for s, text in DEEPGRAM_SEGMENTS],
        "duration": DURATION,
        "auto_title": "Onboarding launch plan",
    }
    t.update(extra)
    return t


def legacy_body(lines, summary="We agreed to submit on the 10th and launch on the 15th."):
    """A page body as v2.3 and earlier wrote it (_build_notion_content)."""
    parts = [summary, "\n---\n"] if summary else []
    parts.append("## Full Transcript\n")
    parts += [f"[{ts}] **{who}**: {text}" for ts, who, text in lines]
    return "\n".join(parts)


def plain_legacy_body(lines):
    return "Summary\n\n---\n\n## Full Transcript\n\n" + " ".join(text for _, _, text in lines)


def page_fetch(title, content, props=None, url=PAGE_URL):
    props = props if props is not None else {"title": title}
    text = f'<page url="{url}"><properties>{json.dumps(props)}</properties><content>\n{content}\n</content></page>'
    return _text_result({"metadata": {"type": "page"}, "title": title, "url": url, "text": text})


def row(page_hex, url, title, session="", created=CREATED_AFTER, edited="2024-05-06T14:00:00.000Z"):
    return {
        "id": _dashed(page_hex),
        "url": url,
        "created_time": created,
        "last_edited_time": edited,
        "properties": {"title": title, "Session ID": session},
    }


class Notion(FakeNotion):
    """FakeNotion with a database of pages: rows listing and fetch by id."""

    def __init__(self):
        super().__init__()
        self.pages = {}  # dashed id -> (title, content, props)
        self.rows = []
        self.handlers["notion-query-data-sources"] = lambda p: _text_result({"results": self.rows, "has_more": False})
        self.handlers["notion-fetch"] = self._fetch
        self.handlers["notion-create-pages"] = _text_result({"pages": [{"url": NEW_URL}]})

    def add(self, page_hex, url, title, content, session="", tags=None, **row_kw):
        props = {"title": title, "Session ID": session}
        if tags is not None:
            props["Tags"] = json.dumps(tags)
        self.pages[_dashed(page_hex)] = (title, content, props, url)
        self.rows.append(row(page_hex, url, title, session=session, **row_kw))

    def _fetch(self, params):
        pid = params["id"]
        if pid not in self.pages:
            raise ValueError(f"object_not_found: Could not find page with ID: {pid}")
        title, content, props, url = self.pages[pid]
        return page_fetch(title, content, props, url)

    def fetched_ids(self):
        return [p["id"] for p in self.of("notion-fetch")]


@pytest.fixture
def lenv(flask_client, tmp_path):
    """Push plumbing with the real legacy lookup; schema and Session ID
    lookup stubbed; the fingerprint cache in a temp file."""
    from app.routes import notion as rn
    from app.utils import notion_legacy

    trn = tmp_path / "transcripts"
    trn.mkdir()
    fake = Notion()
    rn.reset_rows_cache()
    with (
        patch("app.routes.notion.TRANSCRIPTS_DIR", trn),
        patch.object(notion_legacy, "CACHE_PATH", tmp_path / ".notion_legacy_cache.json"),
        patch("app.state.notion.is_connected", return_value=True),
        patch("app.state.notion.get_database_id", return_value="db-id"),
        patch("app.routes.notion._ensure_schema", return_value=_schema(data_source_id=DS_UUID)),
        patch("app.routes.notion._find_page_by_session_id", return_value=None),
        patch("app.routes.notion._sleep"),
        patch("app.state.notion.mcp_call", side_effect=fake),
    ):
        yield types.SimpleNamespace(client=flask_client, trn=trn, root=tmp_path, fake=fake, cache=tmp_path)
    rn.reset_rows_cache()


@pytest.fixture
def queue_state(tmp_path):
    from app.utils import notion_queue

    st = notion_queue._State(tmp_path / "notion_queue.json")
    with patch.object(notion_queue, "_state", st):
        yield st


def _push(lenv, **body):
    return lenv.client.post("/notion/push", json={"session_id": SID, **body})


def _updates(fake, command=None):
    return [p for p in fake.of("notion-update-page") if command is None or p["command"] == command]


# ---------------------------------------------------------------------------
# The text scorer
# ---------------------------------------------------------------------------


def test_scorer_matches_engine_variants_and_not_other_meetings():
    from app.utils import notion_legacy as nl

    local = nl.fingerprint_local(deepgram_transcript())
    same = nl.fingerprint_page(legacy_body(WHISPER_LINES))
    plain = nl.fingerprint_page(plain_legacy_body(WHISPER_LINES))
    same_topic = nl.fingerprint_page(legacy_body(SAME_TOPIC_LINES))
    unrelated = nl.fingerprint_page(legacy_body(UNRELATED_LINES))

    assert nl.text_score(local, same) >= nl.CONFIDENT_SCORE + 0.2
    assert nl.text_score(local, plain) >= nl.CONFIDENT_SCORE + 0.2  # no timestamps or speakers
    assert nl.text_score(local, same_topic) < nl.PLAUSIBLE_SCORE
    assert nl.text_score(local, unrelated) < 0.05


def _garble(lines, rate, seed):
    """Drop, swap or insert about ``rate`` of the words (a weaker engine)."""
    import random

    rnd = random.Random(seed)
    out = []
    for ts, who, text in lines:
        words = []
        for w in text.split():
            r = rnd.random()
            if r < rate / 3:
                continue
            if r < 2 * rate / 3:
                w = rnd.choice(["the", "a", "and", "so", "it", "that"])
            elif r < rate:
                words.append(rnd.choice(["like", "yeah", "you know"]))
            words.append(w)
        out.append((ts, who, " ".join(words)))
    return out


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_scorer_still_matches_a_much_noisier_transcript(seed):
    from app.utils import notion_legacy as nl

    local = nl.fingerprint_local(deepgram_transcript())
    noisy = nl.fingerprint_page(legacy_body(_garble(WHISPER_LINES, 0.15, seed)))
    assert nl.text_score(local, noisy) >= nl.CONFIDENT_SCORE


def test_scorer_uses_only_the_transcript_part_of_a_page():
    from app.utils import notion_legacy as nl

    long_summary = " ".join(text for _, _, text in UNRELATED_LINES) * 3
    body = legacy_body(WHISPER_LINES, summary=long_summary)
    assert "quarterly" not in nl.transcript_part(body)
    local = nl.fingerprint_local(deepgram_transcript())
    assert nl.text_score(local, nl.fingerprint_page(body)) >= nl.CONFIDENT_SCORE


def test_normalisation_smooths_engine_differences():
    from app.utils import notion_legacy as nl

    a = nl.normalise_words("Okay, um, we'll ship on the fifteenth — two weeks, 50%.")
    b = nl.normalise_words("[00:03] **Speaker 1**: OK we’ll ship on the 15th. 2 weeks, fifty percent")
    assert a == b


def test_last_timestamp_and_duration_check():
    from app.utils import notion_legacy as nl

    assert nl.last_timestamp(legacy_body(WHISPER_LINES)) == 110
    assert nl.last_timestamp("[00:00] only one line") is None
    assert nl.last_timestamp("[1:02:03] a\n[0:00:00] b") == 3723
    assert nl.duration_consistent(110, [111, 118]) is True
    assert nl.duration_consistent(600, [111, 118]) is False
    assert nl.duration_consistent(4000, [3700]) is True  # 10% of an hour
    assert nl.duration_consistent(None, [111]) is None
    assert nl.duration_consistent(110, [None, 0]) is None


def test_classify_rules():
    from app.utils import notion_legacy as nl

    def c(score, ok=True):
        return {"score": score, "duration_ok": ok}

    assert nl.classify([c(0.7), c(0.05)])[0] == nl.MATCH_CONFIDENT
    assert nl.classify([c(0.7, None)])[0] == nl.MATCH_REVIEW  # no usable timestamps
    assert nl.classify([c(0.7), c(0.65)])[0] == nl.MATCH_REVIEW  # two close candidates
    assert len(nl.classify([c(0.7), c(0.65)])[1]) == 2
    assert nl.classify([c(0.25)])[0] == nl.MATCH_REVIEW
    assert nl.classify([c(0.1), c(0.05)]) == (nl.MATCH_NONE, [])
    assert nl.classify([]) == (nl.MATCH_NONE, [])


def test_long_fingerprints_are_sampled_consistently():
    from app.utils import notion_legacy as nl

    words = [f"w{i}" for i in range(nl.MAX_SHINGLES * 3)]
    big = nl.fingerprint_text(" ".join(words))
    assert big["mod"] > 1 and big["count"] > nl.MAX_SHINGLES
    half = nl.fingerprint_text(" ".join(words[: len(words) // 2]))
    assert 0.4 < nl.text_score(big, half) < 0.6
    assert nl.text_score(big, half, b_truncated=True) > 0.95


# ---------------------------------------------------------------------------
# Push: confident match is adopted
# ---------------------------------------------------------------------------


def _setup_recording(lenv, sid=SID, **extra):
    _write_transcript(lenv.trn, sid, deepgram_transcript(**extra))


def test_confident_match_is_adopted_not_duplicated(lenv):
    _setup_recording(lenv)
    lenv.fake.add(LEGACY_ID, LEGACY_URL, "Launch planning", legacy_body(WHISPER_LINES), tags=["Group Meeting", "Q2"])
    lenv.fake.add(OTHER_ID, OTHER_URL, "Quarterly numbers", legacy_body(UNRELATED_LINES))

    r = _push(lenv)
    body = r.get_json()
    assert r.status_code == 200, body
    assert body["adopted"] is True and body["url"] == LEGACY_URL and body["content"] == "kept"
    assert "notion-create-pages" not in lenv.fake.methods()

    # Only properties were written: Session ID and the merged tags; no title,
    # and the body was never replaced or appended to.
    upd = _updates(lenv.fake)
    assert [p["command"] for p in upd] == ["update_properties"]
    assert upd[0]["page_id"] == _dashed(LEGACY_ID)
    props = upd[0]["properties"]
    assert props["Session ID"] == SID and "title" not in props
    assert json.loads(props["Tags"]) == ["Group Meeting", "Q2"]

    cfg = _cfg()
    assert cfg["notion_page_urls"][SID] == LEGACY_URL
    assert cfg["notion_legacy_pages"][SID]["page_id"] == _dashed(LEGACY_ID)
    assert cfg["notion_legacy_pages"][SID]["how"] == "auto"
    assert cfg["notion_pushed_titles"][SID] == "Launch planning"
    # The page's tags came back into the app (add-only).
    assert cfg["recording_tags"][SID] == ["Group Meeting", "Q2"]
    assert "Q2" in cfg["custom_tags"] and "Group Meeting" not in cfg["custom_tags"]
    t = json.loads((lenv.trn / f"{SID}.json").read_text())
    assert t["notion_page_url"] == LEGACY_URL and t["tags"] == ["Group Meeting", "Q2"]


def test_adopted_page_later_pushes_never_add_a_second_transcript(lenv):
    from app.utils.config import update_config

    _setup_recording(lenv)
    lenv.fake.add(LEGACY_ID, LEGACY_URL, "Launch planning", legacy_body(WHISPER_LINES))
    assert _push(lenv).get_json()["adopted"] is True
    lenv.fake.calls.clear()

    # Re-push (e.g. after re-summarising): properties only, title kept.
    body = _push(lenv).get_json()
    assert body["updated"] is True and body["content"] == "kept"
    upd = _updates(lenv.fake)
    assert [p["command"] for p in upd] == ["update_properties"]
    assert "title" not in upd[0]["properties"] and upd[0]["properties"]["Session ID"] == SID

    # Renamed in the app: now the title follows (Notion still has the old one).
    update_config(lambda c: c.setdefault("recording_names", {}).__setitem__(SID, "Launch go/no-go"))
    lenv.fake.calls.clear()
    _push(lenv)
    upd = _updates(lenv.fake)
    assert [p["command"] for p in upd] == ["update_properties"]
    assert upd[0]["properties"]["title"] == "Launch go/no-go"
    assert "app_title" not in _cfg()["notion_legacy_pages"][SID]


def test_adopted_page_with_app_section_is_replaced_in_place(lenv):
    from app.routes import notion as rn

    _setup_recording(lenv)
    lenv.fake.add(LEGACY_ID, LEGACY_URL, "Launch planning", legacy_body(WHISPER_LINES))
    _push(lenv)
    # Someone later put an app section on the page: it's updated in place.
    old = rn._build_app_section({"text": "old"}, SID)
    title, content, props, url = lenv.fake.pages[_dashed(LEGACY_ID)]
    lenv.fake.pages[_dashed(LEGACY_ID)] = (title, content + "\n\n" + old, props, url)
    lenv.fake.calls.clear()
    assert _push(lenv).get_json()["content"] == "replaced"
    op = _updates(lenv.fake, "update_content")[0]["content_updates"][0]
    assert op["old_str"] == old


def test_queue_adopts_too(lenv, queue_state):
    from app.utils import notion_queue

    _setup_recording(lenv)
    lenv.fake.add(LEGACY_ID, LEGACY_URL, "Launch planning", legacy_body(WHISPER_LINES))
    notion_queue.enqueue(SID, notion_queue.KIND_FULL)
    notion_queue._process_one(queue_state.pop_due())
    assert queue_state.entries == {} and queue_state.failed == [] and queue_state.needs_review == {}
    assert _cfg()["notion_page_urls"][SID] == LEGACY_URL


# ---------------------------------------------------------------------------
# Ambiguous: needs review, nothing created, queue doesn't loop
# ---------------------------------------------------------------------------


def test_page_without_timestamps_needs_review(lenv):
    _setup_recording(lenv)
    lenv.fake.add(LEGACY_ID, LEGACY_URL, "Launch planning", plain_legacy_body(WHISPER_LINES))

    r = _push(lenv)
    body = r.get_json()
    assert r.status_code == 200
    assert body["needs_review"] is True and body["pushed"] is False
    assert [c["url"] for c in body["candidates"]] == [LEGACY_URL]
    assert body["candidates"][0]["score"] >= 0.35 and body["candidates"][0]["duration_ok"] is None
    assert "notion-create-pages" not in lenv.fake.methods()
    assert _updates(lenv.fake) == []
    assert SID not in _cfg().get("notion_page_urls", {})

    # The transcript view can ask again later.
    info = lenv.client.get(f"/notion/page-url/{SID}").get_json()
    assert info["needs_review"] is True and info["candidates"][0]["url"] == LEGACY_URL


def test_two_close_candidates_need_review(lenv):
    _setup_recording(lenv)
    lenv.fake.add(LEGACY_ID, LEGACY_URL, "Launch planning", legacy_body(WHISPER_LINES))
    lenv.fake.add(THIRD_ID, THIRD_URL, "Launch planning (2)", legacy_body(WHISPER_LINES[:-1]))
    body = _push(lenv).get_json()
    assert body["needs_review"] is True
    assert {c["url"] for c in body["candidates"]} == {LEGACY_URL, THIRD_URL}
    assert "notion-create-pages" not in lenv.fake.methods()


def test_queue_parks_needs_review_without_retrying_or_pausing(lenv, queue_state):
    from app.utils import notion_queue

    _setup_recording(lenv)
    lenv.fake.add(LEGACY_ID, LEGACY_URL, "Launch planning", plain_legacy_body(WHISPER_LINES))
    notion_queue.enqueue(SID, notion_queue.KIND_FULL)
    notion_queue._process_one(queue_state.pop_due())

    assert queue_state.entries == {}  # not retried
    assert queue_state.failed == []  # not a failure
    assert queue_state.paused is False  # not an auth problem
    assert queue_state.pop_due() is None
    assert queue_state.needs_review[SID]["candidates"][0]["url"] == LEGACY_URL
    snap = lenv.client.get("/notion/queue-status").get_json()
    assert snap["needs_review_count"] == 1 and snap["failed_count"] == 0
    # Survives a restart.
    reloaded = notion_queue._State(queue_state.path)
    assert SID in reloaded.needs_review and reloaded.entries == {}
    # "Retry" doesn't re-run it either (it needs the owner's choice).
    assert queue_state.retry_failed() == 0 and queue_state.entries == {}

    # The owner picks the page: the review is cleared.
    body = _push(lenv, link_url=LEGACY_URL).get_json()
    assert body["adopted"] is True
    assert queue_state.needs_review == {}


# ---------------------------------------------------------------------------
# No plausible candidate: created as before
# ---------------------------------------------------------------------------


def test_no_candidate_creates_as_before(lenv):
    _setup_recording(lenv)
    lenv.fake.add(OTHER_ID, OTHER_URL, "Quarterly numbers", legacy_body(UNRELATED_LINES))
    lenv.fake.add(THIRD_ID, THIRD_URL, "Launch follow-up", legacy_body(SAME_TOPIC_LINES))
    body = _push(lenv).get_json()
    assert body == {"pushed": True, "updated": False, "url": NEW_URL}
    assert lenv.fake.methods().count("notion-create-pages") == 1


def test_different_duration_is_not_matched_even_with_similar_text(lenv):
    _setup_recording(lenv)
    # Same words, but the page runs to 10 minutes: another recording.
    long_lines = [(f"{i:02d}:00", who, text) for i, (_, who, text) in enumerate(WHISPER_LINES)]
    lenv.fake.add(LEGACY_ID, LEGACY_URL, "Launch planning", legacy_body(long_lines))
    body = _push(lenv).get_json()
    assert body["url"] == NEW_URL and "adopted" not in body
    assert lenv.fake.methods().count("notion-create-pages") == 1


def test_page_created_before_the_recording_is_not_matched(lenv):
    _setup_recording(lenv)
    lenv.fake.add(LEGACY_ID, LEGACY_URL, "Launch planning", legacy_body(WHISPER_LINES), created="2024-04-01T09:00:00Z")
    assert _push(lenv).get_json()["url"] == NEW_URL


def test_pages_with_a_session_id_or_linked_elsewhere_are_not_candidates(lenv):
    from app.routes import notion as rn
    from app.utils.config import save_config

    _setup_recording(lenv)
    save_config({"notion_page_urls": {SID2: THIRD_URL}})
    lenv.fake.add(LEGACY_ID, LEGACY_URL, "Other recording", legacy_body(WHISPER_LINES), session=SID2)
    lenv.fake.add(THIRD_ID, THIRD_URL, "Linked", legacy_body(WHISPER_LINES))
    lenv.fake.add(APP_ID, APP_URL, "App page", rn._build_app_section(deepgram_transcript(), "1715700000"))
    assert _push(lenv).get_json()["url"] == NEW_URL
    # Rows with a Session ID or already linked aren't even fetched.
    assert _dashed(LEGACY_ID) not in lenv.fake.fetched_ids()
    assert _dashed(THIRD_ID) not in lenv.fake.fetched_ids()


def test_short_transcript_skips_the_check(lenv):
    _write_transcript(
        lenv.trn, SID, {"text": "Testing one two.", "segments": [{"start": 0, "text": "Testing one two."}]}
    )
    lenv.fake.add(LEGACY_ID, LEGACY_URL, "Test", legacy_body([("00:00", "A", "Testing one two.")]))
    assert _push(lenv).get_json()["url"] == NEW_URL
    assert "notion-query-data-sources" not in lenv.fake.methods()


def test_transient_listing_failure_does_not_create(lenv):
    _setup_recording(lenv)
    lenv.fake.handlers["notion-query-data-sources"] = _raise("rate_limited: slow down")
    r = _push(lenv)
    assert r.status_code == 500
    assert "notion-create-pages" not in lenv.fake.methods()


def test_unsupported_listing_falls_back_to_create(lenv):
    _setup_recording(lenv)
    lenv.fake.handlers["notion-query-data-sources"] = _raise("validation_error: unknown mode")
    assert _push(lenv).get_json()["url"] == NEW_URL


def test_transient_page_fetch_failure_does_not_create(lenv):
    _setup_recording(lenv)
    lenv.fake.add(LEGACY_ID, LEGACY_URL, "Launch planning", legacy_body(WHISPER_LINES))
    lenv.fake.handlers["notion-fetch"] = _raise("503 service unavailable")
    assert _push(lenv).status_code == 500
    assert "notion-create-pages" not in lenv.fake.methods()


# ---------------------------------------------------------------------------
# Manual resolution: link_url and force_new
# ---------------------------------------------------------------------------


def test_link_url_adopts_the_chosen_page(lenv):
    _setup_recording(lenv)
    # Not even a plausible match by content: the owner knows better.
    lenv.fake.add(OTHER_ID, OTHER_URL, "Notes", "My own notes about the launch", tags=["Memo"])
    body = _push(lenv, link_url=OTHER_URL).get_json()
    assert body["adopted"] is True and body["url"] == OTHER_URL
    assert "notion-create-pages" not in lenv.fake.methods()
    assert "notion-query-data-sources" not in lenv.fake.methods()
    upd = _updates(lenv.fake)
    assert [p["command"] for p in upd] == ["update_properties"]
    assert upd[0]["properties"]["Session ID"] == SID
    cfg = _cfg()
    assert cfg["notion_page_urls"][SID] == OTHER_URL
    assert cfg["notion_legacy_pages"][SID]["how"] == "manual"


def test_link_url_refuses_another_recordings_page(lenv):
    from app.routes import notion as rn

    _setup_recording(lenv)
    lenv.fake.add(APP_ID, APP_URL, "Standup", rn._build_app_section({"text": "x"}, SID2), session=SID2)
    r = _push(lenv, link_url=APP_URL)
    assert r.status_code == 400 and "another recording" in r.get_json()["error"]
    assert _updates(lenv.fake) == []


def test_link_url_must_be_a_notion_link(lenv):
    _setup_recording(lenv)
    r = _push(lenv, link_url="https://example.com/page")
    assert r.status_code == 400
    assert lenv.fake.calls == []


def test_link_url_to_this_recordings_own_page_is_a_normal_update(lenv):
    from app.routes import notion as rn

    _setup_recording(lenv)
    section = rn._build_app_section(deepgram_transcript(), SID)
    lenv.fake.add(APP_ID, APP_URL, "Standup", section, session=SID)
    body = _push(lenv, link_url=APP_URL).get_json()
    assert body["updated"] is True and "adopted" not in body
    assert SID not in _cfg().get("notion_legacy_pages", {})


def test_force_new_creates_even_with_a_candidate(lenv, queue_state):
    from app.utils import notion_queue

    _setup_recording(lenv)
    lenv.fake.add(LEGACY_ID, LEGACY_URL, "Launch planning", legacy_body(WHISPER_LINES))
    notion_queue.mark_needs_review(SID, [{"url": LEGACY_URL, "score": 0.5}])
    body = _push(lenv, force_new=True).get_json()
    assert body == {"pushed": True, "updated": False, "url": NEW_URL}
    assert "notion-query-data-sources" not in lenv.fake.methods()
    assert _updates(lenv.fake) == []
    assert queue_state.needs_review == {}


def test_page_gone_after_adoption_forgets_the_adoption(lenv):
    _setup_recording(lenv)
    lenv.fake.add(LEGACY_ID, LEGACY_URL, "Launch planning", legacy_body(WHISPER_LINES))
    _push(lenv)
    # The owner deleted the old page in Notion.
    del lenv.fake.pages[_dashed(LEGACY_ID)]
    lenv.fake.rows.clear()
    lenv.fake.handlers["notion-update-page"] = _raise("object_not_found: Could not find page with ID")
    from app.routes import notion as rn

    rn.reset_rows_cache()
    body = _push(lenv).get_json()
    assert body["url"] == NEW_URL
    assert SID not in _cfg().get("notion_legacy_pages", {})


# ---------------------------------------------------------------------------
# Cache and pagination
# ---------------------------------------------------------------------------


def test_cache_avoids_refetching_pages(lenv):
    from app.routes import notion as rn
    from app.utils import notion_legacy as nl

    lenv.fake.add(OTHER_ID, OTHER_URL, "Quarterly numbers", legacy_body(UNRELATED_LINES))
    lenv.fake.add(THIRD_ID, THIRD_URL, "Launch follow-up", legacy_body(SAME_TOPIC_LINES))
    # Three recordings pushed in a row: the database is listed and each page
    # fetched once.
    for i, sid in enumerate(("1715000000", "1715000100", "1715000200")):
        _write_transcript(lenv.trn, sid, deepgram_transcript(auto_title=f"R{i}"))
        r = lenv.client.post("/notion/push", json={"session_id": sid})
        assert r.get_json()["url"] == NEW_URL
    assert lenv.fake.methods().count("notion-query-data-sources") == 1
    assert sorted(lenv.fake.fetched_ids()) == sorted([_dashed(OTHER_ID), _dashed(THIRD_ID)])
    cache = json.loads(nl.CACHE_PATH.read_text())
    assert set(cache["pages"]) == {_dashed(OTHER_ID), _dashed(THIRD_ID)}
    assert "transcript" not in json.dumps(cache).lower()  # fingerprints, not text

    # After the listing expires the rows are listed again, but unchanged pages
    # come from the file cache; an edited page is fetched again.
    rn.reset_rows_cache()
    lenv.fake.rows[1]["last_edited_time"] = "2024-06-01T10:00:00.000Z"
    lenv.fake.calls.clear()
    _write_transcript(lenv.trn, "1715000300", deepgram_transcript())
    lenv.client.post("/notion/push", json={"session_id": "1715000300"})
    assert lenv.fake.methods().count("notion-query-data-sources") == 1
    assert lenv.fake.fetched_ids() == [_dashed(THIRD_ID)]


def test_rows_are_paginated(lenv):
    _setup_recording(lenv)
    lenv.fake.add(OTHER_ID, OTHER_URL, "Quarterly numbers", legacy_body(UNRELATED_LINES))
    lenv.fake.add(LEGACY_ID, LEGACY_URL, "Launch planning", legacy_body(WHISPER_LINES))
    first, second = lenv.fake.rows

    def query(params):
        if params["data"].get("start_cursor") == "c2":
            return _text_result({"results": [second], "has_more": False, "next_cursor": None})
        return _text_result({"results": [first], "has_more": True, "next_cursor": "c2"})

    lenv.fake.handlers["notion-query-data-sources"] = query
    assert _push(lenv).get_json()["adopted"] is True
    q = [p["data"] for p in lenv.fake.of("notion-query-data-sources")]
    assert len(q) == 2 and q[0]["mode"] == "rows" and q[0]["data_source_url"] == "collection://ds-id"
    assert "start_cursor" not in q[0] and q[1]["start_cursor"] == "c2"


def test_pagination_stops_on_a_repeated_cursor(lenv):
    from app.routes import notion as rn

    lenv.fake.handlers["notion-query-data-sources"] = lambda p: _text_result(
        {"results": [], "has_more": True, "next_cursor": "same"}
    )
    rows, complete = rn._list_db_rows("ds-id")
    assert rows == [] and complete is True
    assert len(lenv.fake.of("notion-query-data-sources")) == 2


# ---------------------------------------------------------------------------
# Duplicates already created
# ---------------------------------------------------------------------------


def _app_page_body(t, sid):
    from app.routes import notion as rn

    return "My notes\n\n" + rn._build_app_section(t, sid)


def test_duplicates_lists_app_pages_matching_a_legacy_page(lenv):
    with patch("app.routes.notion._fetch_db_schema", return_value=_schema(data_source_id=DS_UUID)):
        lenv.fake.add(LEGACY_ID, LEGACY_URL, "Launch planning", legacy_body(WHISPER_LINES))
        lenv.fake.add(OTHER_ID, OTHER_URL, "Quarterly numbers", legacy_body(UNRELATED_LINES))
        lenv.fake.add(
            APP_ID, APP_URL, "Onboarding launch plan", _app_page_body(deepgram_transcript(), SID), session=SID
        )
        unrelated_t = {
            "segments": [{"start": int(ts[:2]) * 60 + int(ts[3:]), "text": x} for ts, _, x in SAME_TOPIC_LINES]
        }
        lenv.fake.add(THIRD_ID, THIRD_URL, "Follow-up", _app_page_body(unrelated_t, SID2), session=SID2)

        r = lenv.client.get("/notion/duplicates")
    body = r.get_json()
    assert r.status_code == 200, body
    assert body["checked"] == 4
    assert len(body["pairs"]) == 1
    pair = body["pairs"][0]
    assert pair["session_id"] == SID
    assert pair["older"]["url"] == LEGACY_URL and pair["older"]["title"] == "Launch planning"
    assert pair["newer"]["url"] == APP_URL and pair["newer"]["title"] == "Onboarding launch plan"
    # Read-only: nothing written, nothing deleted.
    assert set(lenv.fake.methods()) == {"notion-query-data-sources", "notion-fetch"}


def test_duplicates_none_found(lenv):
    with patch("app.routes.notion._fetch_db_schema", return_value=_schema(data_source_id=DS_UUID)):
        lenv.fake.add(OTHER_ID, OTHER_URL, "Quarterly numbers", legacy_body(UNRELATED_LINES))
        lenv.fake.add(APP_ID, APP_URL, "Onboarding", _app_page_body(deepgram_transcript(), SID), session=SID)
        body = lenv.client.get("/notion/duplicates").get_json()
    assert body["pairs"] == [] and body["checked"] == 2


def test_duplicates_needs_a_connection(flask_client):
    with patch("app.state.notion.is_connected", return_value=False):
        assert flask_client.get("/notion/duplicates").status_code == 401


def test_candidate_deleted_before_adoption_is_retried_not_given_up(lenv, queue_state):
    from app.utils import notion_queue

    _setup_recording(lenv)
    lenv.fake.add(LEGACY_ID, LEGACY_URL, "Launch planning", legacy_body(WHISPER_LINES))
    fetches = []

    def fetch(params):
        fetches.append(params["id"])
        if len(fetches) > 1:  # scored, then deleted before the adoption read
            raise ValueError("object_not_found: Could not find page with ID")
        return lenv.fake._fetch(params)

    lenv.fake.handlers["notion-fetch"] = fetch
    notion_queue.enqueue(SID, notion_queue.KIND_FULL)
    notion_queue._process_one(queue_state.pop_due())
    assert queue_state.failed == [] and queue_state.entries[SID]["failures"] == 1
    assert "notion-create-pages" not in lenv.fake.methods()

    # Next attempt: the page is gone from the database too, so it's created.
    del lenv.fake.pages[_dashed(LEGACY_ID)]
    lenv.fake.rows.clear()
    queue_state.entries[SID]["next_retry_at"] = 0
    notion_queue._process_one(queue_state.pop_due())
    assert queue_state.entries == {}
    assert _cfg()["notion_page_urls"][SID] == NEW_URL


def test_duplicates_scan_prunes_pages_no_longer_in_the_database(lenv):
    from app.utils import notion_legacy as nl

    with patch("app.routes.notion._fetch_db_schema", return_value=_schema(data_source_id=DS_UUID)):
        lenv.fake.add(LEGACY_ID, LEGACY_URL, "Launch planning", legacy_body(WHISPER_LINES))
        lenv.fake.add(OTHER_ID, OTHER_URL, "Quarterly numbers", legacy_body(UNRELATED_LINES))
        lenv.client.get("/notion/duplicates")
        assert set(json.loads(nl.CACHE_PATH.read_text())["pages"]) == {_dashed(LEGACY_ID), _dashed(OTHER_ID)}
        lenv.fake.rows.pop(0)
        lenv.client.get("/notion/duplicates")
    assert set(json.loads(nl.CACHE_PATH.read_text())["pages"]) == {_dashed(OTHER_ID)}
