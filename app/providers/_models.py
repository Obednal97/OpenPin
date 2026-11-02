"""Model identifiers for the cloud and local AI providers.

Update these together when migrating between model versions. Sources checked
2026-11-02:

- Claude: Anthropic model table (claude-api skill, cached 2026-10-06) —
  ``claude-sonnet-5-5`` is the current Sonnet.
- OpenAI: https://developers.openai.com/api/docs/deprecations — the
  ``gpt-4o-mini`` snapshot shuts down 2027-01-14 (replacement ``gpt-6-luna``);
  ``whisper-1`` / ``gpt-4o-transcribe`` shut down 2027-02-26 (replacement
  ``gpt-transcribe``).
- OpenRouter: https://openrouter.ai/api/v1/models (IDs below are listed there
  with ``structured_outputs`` support).
- Groq: https://console.groq.com/docs/speech-to-text
- Deepgram: https://developers.deepgram.com/docs/language-detection
"""

# --- Summarisation -----------------------------------------------------------

CLAUDE_SUMMARIZE = "claude-sonnet-5-5"
# Summaries are content generation: Anthropic's guidance for Sonnet 5.5 is to
# start at "low" effort for that kind of work (the API default is "high").
CLAUDE_SUMMARIZE_EFFORT = "low"
# Thinking tokens count toward max_tokens, so leave generous headroom. The
# request is streamed, so a large cap doesn't risk an HTTP timeout.
CLAUDE_SUMMARIZE_MAX_TOKENS = 64000

OPENAI_SUMMARIZE = "gpt-6-luna"
# GPT-6 Luna is a reasoning model (default effort "medium"); reasoning tokens
# are billed as output and count against max_completion_tokens.
OPENAI_REASONING_EFFORT = "low"
OPENAI_SUMMARIZE_MAX_TOKENS = 32000

# OpenRouter uses its own namespaced model IDs (provider/model-name format).
# These mirror the above in OpenRouter's naming convention.
OPENROUTER_CLAUDE_SUMMARIZE = "anthropic/claude-sonnet-5.5"
OPENROUTER_OPENAI_SUMMARIZE = "openai/gpt-6-luna"

# Local Ollama server (optional). Detected with a short timeout; only models
# the user has already pulled are used — the app never pulls one itself.
OLLAMA_URL = "http://localhost:11434"
# Preferred local models, best first (prefix match against `ollama list`).
OLLAMA_PREFERRED_MODELS = ("qwen3", "llama3.3", "gemma3", "llama3.2", "llama3.1", "mistral", "phi4")

# Last-resort local summariser (news-style extractive output, ~1.2 GB).
LOCAL_SUMMARIZE = "sshleifer/distilbart-cnn-12-6"

# --- Transcription -----------------------------------------------------------

DEEPGRAM_TRANSCRIBE = "nova-3"
# whisper-large-v3 (10.3% WER) over whisper-large-v3-turbo (12% WER): Groq is a
# fallback here and the accuracy gap matters more than the price gap for
# personal recordings ($0.111 vs $0.04 per audio hour).
GROQ_TRANSCRIBE = "whisper-large-v3"
# gpt-transcribe returns text only (no segment timestamps — those were
# whisper-1 only), so segment times are estimated; see providers/transcribe.py.
OPENAI_TRANSCRIBE = "gpt-transcribe"
