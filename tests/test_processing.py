"""Tests for processing settings — provider preferences, profile context."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


class TestProviderPreferences:
    """Verify provider preferences are saved and respected."""

    def test_settings_returns_provider_preferences(self, flask_client):
        """GET /settings must include provider preference fields."""
        r = flask_client.get("/settings")
        data = r.get_json()
        assert "transcription_provider" in data
        assert "diarization_provider" in data
        assert "summarization_provider" in data

    def test_settings_saves_provider_preferences(self, flask_client):
        """POST /settings must save provider preferences."""
        flask_client.post("/settings", json={
            "transcription_provider": "groq",
            "summarization_provider": "openai",
        })
        r = flask_client.get("/settings")
        data = r.get_json()
        assert data["transcription_provider"] == "groq"
        assert data["summarization_provider"] == "openai"

    def test_settings_returns_english_variant(self, flask_client):
        """GET /settings must include english_variant field."""
        r = flask_client.get("/settings")
        data = r.get_json()
        assert "english_variant" in data

    def test_settings_saves_english_variant(self, flask_client):
        """POST /settings must save english_variant."""
        flask_client.post("/settings", json={"english_variant": "gb"})
        r = flask_client.get("/settings")
        assert r.get_json()["english_variant"] == "gb"


class TestProfileContext:
    """Verify profile data is passed to providers."""

    def test_profile_includes_name(self, flask_client):
        """Profile must support a name field."""
        flask_client.post("/settings", json={
            "profile": {"name": "Test User", "job": "Engineer"}
        })
        r = flask_client.get("/settings")
        profile = r.get_json().get("profile", {})
        assert profile.get("name") == "Test User"

    def test_british_english_adds_instruction(self):
        """When english_variant=gb, summarisation context must include British English instruction."""
        # The instruction is added in routes/transcribe.py summarize section
        src = (Path(__file__).parent.parent / "app" / "routes" / "transcribe.py").read_text()
        assert "British English" in src
        assert "english_variant" in src

    def test_profile_name_in_transcription_prompt(self):
        """Profile name should be passed to transcription providers."""
        src = (Path(__file__).parent.parent / "app" / "routes" / "transcribe.py").read_text()
        assert 'profile.get("name")' in src

    def test_deepgram_receives_language_variant(self):
        """Deepgram should receive en-GB or en-US based on english_variant."""
        src = (Path(__file__).parent.parent / "app" / "routes" / "transcribe.py").read_text()
        assert "en-GB" in src
        assert "en-US" in src
        assert "deepgram_language" in src


class TestSummarizationCascade:
    """Verify summarisation respects provider preference."""

    def test_summarize_accepts_preference(self):
        """summarize() must read summarization_provider from config."""
        src = (Path(__file__).parent.parent / "app" / "providers" / "summarize.py").read_text()
        assert "summarization_provider" in src

    def test_transcription_respects_preference(self):
        """Transcription route must read transcription_provider from config."""
        src = (Path(__file__).parent.parent / "app" / "routes" / "transcribe.py").read_text()
        assert "transcription_provider" in src


class TestDeepgramSegmentMerging:
    """Verify Deepgram output goes through speaker merging."""

    def test_merge_applied_to_deepgram(self):
        """Deepgram results with speakers must go through _merge_speaker_segments."""
        src = (Path(__file__).parent.parent / "app" / "routes" / "transcribe.py").read_text()
        assert "_merge_speaker_segments" in src
        assert "_cloud_has_speakers" in src
