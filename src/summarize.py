"""Generate summaries and notes from transcripts using LLMs.

Supports multiple providers: OpenAI, Anthropic, Ollama (local).

Usage:
    python -m src.summarize path/to/transcript.json
    python -m src.summarize path/to/transcript.json --provider ollama --model llama3.2
"""

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from .transcribe import Transcript


@dataclass
class Summary:
    title: str
    summary: str
    key_points: list[str]
    action_items: list[str]
    model: str = ""
    raw: str = ""

    def save(self, path: str):
        with open(path, "w") as f:
            json.dump(
                {
                    "title": self.title,
                    "summary": self.summary,
                    "key_points": self.key_points,
                    "action_items": self.action_items,
                    "model": self.model,
                },
                f,
                indent=2,
                ensure_ascii=False,
            )

    @property
    def formatted(self) -> str:
        parts = [f"# {self.title}\n"]
        parts.append(f"## Summary\n{self.summary}\n")
        if self.key_points:
            parts.append("## Key Points")
            for kp in self.key_points:
                parts.append(f"- {kp}")
            parts.append("")
        if self.action_items:
            parts.append("## Action Items")
            for ai in self.action_items:
                parts.append(f"- [ ] {ai}")
            parts.append("")
        return "\n".join(parts)


SUMMARY_PROMPT = """Analyze the following transcript and provide:

1. A concise title (max 10 words)
2. A summary (2-3 paragraphs)
3. Key points (bullet list, 3-7 items)
4. Action items (if any, otherwise empty list)

Respond in JSON format:
{
  "title": "...",
  "summary": "...",
  "key_points": ["...", "..."],
  "action_items": ["...", "..."]
}

TRANSCRIPT:
"""


def summarize_with_openai(transcript_text: str, model: str = "gpt-4o-mini") -> Summary:
    """Summarize using OpenAI API."""
    try:
        from openai import OpenAI
    except ImportError:
        print("Installing openai...")
        import subprocess

        subprocess.check_call([sys.executable, "-m", "pip", "install", "--break-system-packages", "openai"])
        from openai import OpenAI

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY environment variable not set")

    client = OpenAI(api_key=api_key)

    # Truncate long transcripts to fit context
    max_chars = 100_000
    text = transcript_text[:max_chars]
    if len(transcript_text) > max_chars:
        text += "\n\n[TRANSCRIPT TRUNCATED]"

    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": "You are a helpful assistant that analyzes meeting transcripts and recordings.",
            },
            {"role": "user", "content": SUMMARY_PROMPT + text},
        ],
        response_format={"type": "json_object"},
        temperature=0.3,
    )

    raw = response.choices[0].message.content
    data = json.loads(raw)

    return Summary(
        title=data.get("title", "Untitled"),
        summary=data.get("summary", ""),
        key_points=data.get("key_points", []),
        action_items=data.get("action_items", []),
        model=model,
        raw=raw,
    )


def summarize_with_anthropic(transcript_text: str, model: str = "claude-sonnet-4-20250514") -> Summary:
    """Summarize using Anthropic API."""
    try:
        import anthropic
    except ImportError:
        print("Installing anthropic...")
        import subprocess

        subprocess.check_call([sys.executable, "-m", "pip", "install", "--break-system-packages", "anthropic"])
        import anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY environment variable not set")

    client = anthropic.Anthropic(api_key=api_key)

    max_chars = 150_000
    text = transcript_text[:max_chars]

    response = client.messages.create(
        model=model,
        max_tokens=4096,
        messages=[
            {
                "role": "user",
                "content": SUMMARY_PROMPT + text + "\n\nRespond with ONLY the JSON object, no other text.",
            },
        ],
    )

    raw = response.content[0].text
    # Extract JSON from response
    start = raw.find("{")
    end = raw.rfind("}") + 1
    if start >= 0 and end > start:
        data = json.loads(raw[start:end])
    else:
        data = {"title": "Untitled", "summary": raw}

    return Summary(
        title=data.get("title", "Untitled"),
        summary=data.get("summary", ""),
        key_points=data.get("key_points", []),
        action_items=data.get("action_items", []),
        model=model,
        raw=raw,
    )


def summarize_with_ollama(transcript_text: str, model: str = "llama3.2") -> Summary:
    """Summarize using local Ollama."""
    import requests

    max_chars = 50_000  # Smaller context for local models
    text = transcript_text[:max_chars]

    try:
        response = requests.post(
            "http://localhost:11434/api/generate",
            json={
                "model": model,
                "prompt": SUMMARY_PROMPT + text + "\n\nRespond with ONLY the JSON object.",
                "stream": False,
                "format": "json",
            },
            timeout=300,
        )
        response.raise_for_status()
    except requests.ConnectionError:
        raise ConnectionError("Ollama not running. Start with: ollama serve")

    raw = response.json().get("response", "")
    start = raw.find("{")
    end = raw.rfind("}") + 1
    if start >= 0 and end > start:
        data = json.loads(raw[start:end])
    else:
        data = {"title": "Untitled", "summary": raw}

    return Summary(
        title=data.get("title", "Untitled"),
        summary=data.get("summary", ""),
        key_points=data.get("key_points", []),
        action_items=data.get("action_items", []),
        model=model,
        raw=raw,
    )


def summarize(
    transcript: Transcript,
    provider: str = "ollama",
    model: str | None = None,
) -> Summary:
    """Summarize a transcript using the specified provider.

    Providers: "openai", "anthropic", "ollama"
    """
    text = transcript.formatted if transcript.segments else transcript.text

    if provider == "openai":
        return summarize_with_openai(text, model=model or "gpt-4o-mini")
    elif provider == "anthropic":
        return summarize_with_anthropic(text, model=model or "claude-sonnet-4-20250514")
    elif provider == "ollama":
        return summarize_with_ollama(text, model=model or "llama3.2")
    else:
        raise ValueError(f"Unknown provider: {provider}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Summarize transcripts using LLMs")
    parser.add_argument("path", help="Transcript JSON file path")
    parser.add_argument(
        "--provider", choices=["openai", "anthropic", "ollama"], default="ollama", help="LLM provider (default: ollama)"
    )
    parser.add_argument("--model", default=None, help="Model name")

    args = parser.parse_args()
    path = Path(args.path)

    if not path.exists():
        print(f"File not found: {path}")
        sys.exit(1)

    transcript = Transcript.load(str(path))
    print(f"Transcript: {len(transcript.segments)} segments, {transcript.duration:.0f}s")

    summary = summarize(transcript, provider=args.provider, model=args.model)

    # Save summary
    summary_path = path.with_suffix(".summary.json")
    summary.save(str(summary_path))

    # Save formatted markdown
    md_path = path.with_suffix(".summary.md")
    md_path.write_text(summary.formatted, encoding="utf-8")

    print(f"\nSaved: {summary_path}")
    print(f"Saved: {md_path}")
    print(f"\n{summary.formatted}")
