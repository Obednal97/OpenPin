"""Model identifiers for the cloud and local AI providers.

Update these together when migrating between model versions. Sources checked
2026-11-02, OpenAI summary model and Ollama families re-checked 2026-11-03:

- Claude: Anthropic model table (claude-api skill, cached 2026-10-06) —
  ``claude-sonnet-5-5`` is the current Sonnet.
- OpenAI: https://developers.openai.com/api/docs/deprecations — the
  ``gpt-4o-mini`` snapshot shuts down 2027-01-14; ``whisper-1`` /
  ``gpt-4o-transcribe`` shut down 2027-02-26 (replacement ``gpt-transcribe``).
  Summaries use https://developers.openai.com/api/docs/models/gpt-6.1-sol:
  the tier that matches Sonnet 5.5 in price ($2/$10 per 1M tokens),
  1.05M-token context (922k input), Chat Completions + structured outputs.
  ``gpt-6-luna`` is the cheap high-volume tier.
- Ollama: https://ollama.com/library
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

# Titles for transcripts that aren't summarised (generate_title): a short,
# cheap call, so the smallest current Claude model.
CLAUDE_TITLE = "claude-haiku-5-5"
CLAUDE_TITLE_MAX_TOKENS = 100

OPENAI_SUMMARIZE = "gpt-6.1-sol"
# GPT-6.1 Sol is a reasoning model (default effort "medium"); reasoning tokens
# are billed as output and count against max_completion_tokens.
OPENAI_REASONING_EFFORT = "low"
OPENAI_SUMMARIZE_MAX_TOKENS = 32000

# OpenRouter uses its own namespaced model IDs (provider/model-name format).
# These mirror the above in OpenRouter's naming convention.
OPENROUTER_CLAUDE_SUMMARIZE = "anthropic/claude-sonnet-5.5"
OPENROUTER_OPENAI_SUMMARIZE = "openai/gpt-6.1-sol"

# Local Ollama server (optional). Detected with a short timeout; only models
# the user has already pulled are used — the app never pulls one itself.
OLLAMA_URL = "http://localhost:11434"
# Preferred local model families, best first, matched against the name before
# ":" in `ollama list`; within a family the biggest pulled size wins. "-cloud"
# tags are skipped (they run on Ollama's servers).
OLLAMA_PREFERRED_MODELS = (
    "qwen3.8",
    "qwen3.6",
    "qwen3.5",
    "gemma4",
    "gpt-oss",
    "qwen3",
    "gemma3",
    "llama3.3",
    "llama3.1",
    "mistral",
    "phi4",
    "llama3.2",
)
# Suggested when no chat model is pulled: runs on a 16 GB Mac.
OLLAMA_SUGGESTED_MODEL = "qwen3.5:9b"

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
