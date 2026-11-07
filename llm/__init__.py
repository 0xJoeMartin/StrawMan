"""
LLM providers
-------------
Thin adapters, one per API style, all with the same interface:

    provider.complete(system, messages, max_tokens) -> str
    provider.test() -> str          # short "it works" message, or raises
    provider.ensure_ready()         # raise ValueError if it can't be used yet

Messages use Anthropic-style content blocks — the format scarecrow.py already
builds for conversation history (see llm/messages.py). Each adapter converts
to its own wire format. Images travel inside the messages.

Adapters read their settings (URL, model, key) from `config` on every call, so
dashboard Settings changes apply on the next line.

Prompt strategy (what goes in the system prompt, how replies are framed) is
the brain's job, not the adapter's — ai_brain.py folds the system prompt into
the first message for Ollama, for example.
"""

from .anthropic_provider import AnthropicProvider
from .ollama_provider import OllamaProvider
from .openai_compat import OpenAICompatProvider, PRESETS

PROVIDERS = {
    "claude": AnthropicProvider,
    "ollama": OllamaProvider,
    "openai": OpenAICompatProvider,
}

__all__ = ["PROVIDERS", "PRESETS", "AnthropicProvider", "OllamaProvider", "OpenAICompatProvider"]
