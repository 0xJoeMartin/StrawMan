"""Ollama native /api/chat adapter (stdlib urllib, no extra deps)."""

import json
import urllib.error
import urllib.request

import config

from .messages import TEST_IMAGE_B64, TEST_PROMPT, to_ollama, user_turn
from .models import parse_ollama_show, sort_models


class OllamaProvider:
    name = "ollama"
    label = "Ollama"

    def url(self) -> str:
        return config.OLLAMA_BASE_URL.rstrip("/")

    def model(self) -> str:
        return config.OLLAMA_MODEL

    def ensure_ready(self):
        pass  # nothing to set up; reachability shows up on the first call

    def complete(self, system, messages, max_tokens) -> str:
        msgs = to_ollama(messages)
        if system:
            msgs = [{"role": "system", "content": system}] + msgs
        payload = {
            "model": self.model(),
            "messages": msgs,
            "stream": False,
            "options": {
                "num_predict": max_tokens,  # Keep it short — ~1-2 sentences
                "temperature": 0.9,
                "top_p": 0.9,
                "repeat_penalty": 1.2,      # Discourage repetitive phrasing
            },
        }
        result = self._request("/api/chat", payload)
        try:
            return result["message"]["content"]
        except (KeyError, TypeError) as e:
            raise RuntimeError(f"Unexpected Ollama response: {e}")

    def _request(self, endpoint: str, payload: dict = None, timeout: int = 60) -> dict:
        url = f"{self.url()}{endpoint}"
        if payload is None:
            req = urllib.request.Request(url)
        else:
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as e:
            raise ConnectionError(f"Ollama unreachable at {self.url()}: {e}")
        except (KeyError, json.JSONDecodeError) as e:
            raise RuntimeError(f"Unexpected Ollama response: {e}")

    def list_models(self) -> list:
        """Pulled models, with Ollama's own vision / thinking capability flags."""
        tags = self._request("/api/tags", timeout=10).get("models", [])
        names = list(dict.fromkeys(m["name"] for m in tags))  # some Ollama builds list a tag twice
        out = []
        for name in names:
            try:
                out.append(parse_ollama_show(name, self._request("/api/show", {"model": name}, timeout=10)))
            except Exception:
                out.append({"id": name, "vision": None, "thinking": None})
        return sort_models(out)

    def test(self) -> str:
        tags = self._request("/api/tags", timeout=10)
        names = [m["name"] for m in tags.get("models", [])]
        if self.model() not in names and self.model() not in [n.split(":")[0] for n in names]:
            raise ValueError(f"Model '{self.model()}' isn't pulled on {self.url()} "
                             f"(run: ollama pull {self.model()})")
        reply = self.complete(None, [user_turn([TEST_IMAGE_B64], TEST_PROMPT)], 10)
        return f"{self.model()} sees the test picture as {reply.strip()[:40]!r} (it's orange)"
