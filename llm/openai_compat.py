"""
OpenAI-compatible adapter (POST {base}/chat/completions, `requests` only).

Works with OpenAI itself and anything that speaks its chat API: OpenRouter,
Groq, Gemini's OpenAI endpoint, LM Studio, Ollama's /v1, ... Images go as
image_url parts with data: URLs.

Key: OPENAI_COMPAT_API_KEY. If that's empty and the base URL is OpenAI's, the
OpenAI key from the API keys panel (OPENAI_API_KEY) is used. Local servers
usually need no key.
"""

import config

from .messages import TEST_IMAGE_B64, TEST_PROMPT, to_openai, user_turn
from .models import parse_openai_models

# Filled into Settings by the page's preset picker. "tested" = checked against
# the real service with a picture while building this; others follow the
# provider's documentation.
# model None = keep whatever is set (you pick the model you've loaded).
PRESETS = [
    {"name": "OpenAI", "base_url": "https://api.openai.com/v1", "model": "gpt-4.1-mini",
     "key": "OpenAI key — leave empty to reuse the OpenAI key", "tested": True},
    {"name": "Ollama (OpenAI mode)", "base_url": "http://localhost:11434/v1", "model": "llava",
     "key": "no key needed", "tested": True},
    {"name": "LM Studio", "base_url": "http://localhost:1234/v1", "model": None,
     "key": "no key needed — set the model to the vision model you've loaded", "tested": False},
    {"name": "OpenRouter", "base_url": "https://openrouter.ai/api/v1", "model": "openai/gpt-4.1-mini",
     "key": "OpenRouter key", "tested": False},
    {"name": "Groq", "base_url": "https://api.groq.com/openai/v1", "model": "qwen/qwen3.8-27b",
     "key": "Groq key", "tested": False},
    {"name": "Google Gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
     "model": "gemini-3.8-flash", "key": "Gemini API key (Google AI Studio)", "tested": False},
]

TIMEOUT = 30


class OpenAICompatProvider:
    name = "openai"
    label = "OpenAI-compatible"

    def __init__(self):
        # Newer OpenAI models reject max_tokens and want max_completion_tokens;
        # remembered per (base URL, model) after the first rejection.
        self._completion_tokens = set()

    def base(self) -> str:
        return config.OPENAI_COMPAT_BASE_URL.rstrip("/")

    def model(self) -> str:
        return config.OPENAI_COMPAT_MODEL

    def key(self) -> str:
        key = getattr(config, "OPENAI_COMPAT_API_KEY", "") or ""
        if not key and "api.openai.com" in self.base():
            key = config.OPENAI_API_KEY or ""
        return key

    def ensure_ready(self):
        if "api.openai.com" in self.base() and not self.key():
            raise ValueError("No OpenAI API key set (Settings → API keys)")

    def _headers(self):
        h = {"Content-Type": "application/json"}
        if self.key():
            h["Authorization"] = f"Bearer {self.key()}"
        return h

    def complete(self, system, messages, max_tokens) -> str:
        import requests
        msgs = to_openai(messages)
        if system:
            msgs = [{"role": "system", "content": system}] + msgs
        ident = (self.base(), self.model())
        param = "max_completion_tokens" if ident in self._completion_tokens else "max_tokens"
        body = {"model": self.model(), "messages": msgs, param: max_tokens}
        r = requests.post(f"{self.base()}/chat/completions", json=body,
                          headers=self._headers(), timeout=TIMEOUT)
        if r.status_code == 400 and param == "max_tokens" and "max_completion_tokens" in r.text:
            self._completion_tokens.add(ident)
            return self.complete(system, messages, max_tokens)
        if r.status_code != 200:
            raise RuntimeError(f"{self.base()} HTTP {r.status_code}: {_error_text(r)}")
        try:
            content = r.json()["choices"][0]["message"].get("content")
        except (KeyError, IndexError, TypeError, ValueError) as e:
            raise RuntimeError(f"Unexpected response from {self.base()}: {e}")
        if isinstance(content, list):  # some servers return content parts
            content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
        if not content or not content.strip():
            raise RuntimeError(f"{self.model()} returned no text — if it's a reasoning model it "
                               f"may have spent the whole token budget thinking; pick a non-reasoning model")
        return content

    def list_models(self) -> list:
        import requests
        self.ensure_ready()
        r = requests.get(f"{self.base()}/models", headers=self._headers(), timeout=15)
        if r.status_code != 200:
            raise RuntimeError(f"{self.base()} HTTP {r.status_code}: {_error_text(r)}")
        return parse_openai_models(r.json(), self.base())

    def test(self) -> str:
        self.ensure_ready()
        reply = self.complete(None, [user_turn([TEST_IMAGE_B64], TEST_PROMPT)], 10)
        return f"{self.model()} sees the test picture as {reply.strip()[:40]!r} (it's orange)"


def _error_text(r) -> str:
    try:
        body = r.json()
        if isinstance(body, list) and body:
            body = body[0]
        err = body.get("error", body) if isinstance(body, dict) else body
        msg = err.get("message") if isinstance(err, dict) else str(err)
        return (msg or r.text)[:200]
    except Exception:
        return r.text[:200]
