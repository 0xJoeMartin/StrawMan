"""Anthropic (Claude) adapter — official SDK, internal messages sent as-is."""

import config

from .messages import TEST_IMAGE_B64, TEST_PROMPT, user_turn

# Extra room for models whose thinking can't be switched off: thinking tokens
# count against max_tokens, and his lines stay short because the prompt says so.
THINKING_HEADROOM = 1024


def thinking_args(model: str, max_tokens: int) -> dict:
    """Request fields that keep thinking out of a one-line reply.

    Newer Claude models think by default, and the thinking eats the small
    max_tokens budget (so the line comes back empty or cut short). Turn it off
    where the model allows, otherwise ask for low effort and leave headroom.
    Older models (Haiku 4.5, Sonnet/Opus 4.x) don't think unless asked: nothing extra.
    """
    m = model.lower()
    if m.startswith("claude-haiku-5") or m in ("claude-opus-5", "claude-sonnet-5"):
        return {"max_tokens": max_tokens, "thinking": {"type": "disabled"}}
    if m.startswith("claude-sonnet-5-5"):
        return {"max_tokens": max_tokens, "thinking": {"type": "between_tools"}}  # its way of saying "off"
    if m.startswith(("claude-opus-5", "claude-fable-", "claude-mythos-")):
        return {"max_tokens": max_tokens + THINKING_HEADROOM, "output_config": {"effort": "low"}}
    return {"max_tokens": max_tokens}


def text_of(response) -> str:
    """The reply text, skipping thinking (and any other non-text) blocks."""
    text = "".join(b.text for b in response.content if getattr(b, "type", "text") == "text")
    if not text.strip():
        raise RuntimeError(f"no text in the reply (stop_reason={getattr(response, 'stop_reason', None)})")
    return text


class AnthropicProvider:
    name = "claude"
    label = "Claude"

    def __init__(self):
        self._client = None
        self._client_key = None

    def model(self) -> str:
        return config.AI_MODEL

    def ensure_ready(self):
        """Create the SDK client. Raises ValueError without an API key."""
        key = config.ANTHROPIC_API_KEY
        if not key:
            raise ValueError("ANTHROPIC_API_KEY not set")
        if self._client is None or key != self._client_key:
            import anthropic
            self._client = anthropic.Anthropic(api_key=key)
            self._client_key = key
        return self._client

    def complete(self, system, messages, max_tokens) -> str:
        client = self.ensure_ready()  # picks up a key changed on the dashboard
        extra = thinking_args(config.AI_MODEL, max_tokens)
        response = client.messages.create(
            model=config.AI_MODEL,
            max_tokens=extra.pop("max_tokens"),
            system=system,
            messages=messages,
            **extra,
        )
        return text_of(response)

    def list_models(self) -> list:
        """Every current Claude model accepts pictures."""
        import requests
        key = config.ANTHROPIC_API_KEY
        if not key:
            raise ValueError("ANTHROPIC_API_KEY not set")
        r = requests.get("https://api.anthropic.com/v1/models", params={"limit": 100}, timeout=10,
                         headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
        if r.status_code != 200:
            raise RuntimeError(f"Anthropic HTTP {r.status_code}: {r.text[:200]}")
        return [{"id": m["id"], "label": m.get("display_name"), "vision": True, "thinking": False}
                for m in r.json().get("data", [])]

    def test(self) -> str:
        reply = self.complete("You are a test.", [user_turn([TEST_IMAGE_B64], TEST_PROMPT)], 10)
        return f"{config.AI_MODEL} sees the test picture as {reply.strip()[:40]!r} (it's orange)"
