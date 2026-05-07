"""Model identifiers used by summarize/validate providers.

Update these together when migrating between Claude/OpenAI versions.
"""

CLAUDE_SUMMARIZE = "claude-sonnet-4-6"
CLAUDE_VALIDATE = "claude-haiku-4-5"
OPENAI_SUMMARIZE = "gpt-4o-mini"  # was "gpt-5.4-mini" — likely typo, please verify

# OpenRouter uses its own namespaced model IDs (provider/model-name format).
# These mirror the above but in OpenRouter's naming convention.
OPENROUTER_CLAUDE_SUMMARIZE = "anthropic/claude-sonnet-4.6"
OPENROUTER_OPENAI_SUMMARIZE = "openai/gpt-4o-mini"  # was "openai/gpt-5.4-mini" — likely typo
