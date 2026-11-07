"""
Model lists for the Settings page picker.

Each adapter's list_models() returns entries like
    {"id": "gpt-4.1-mini", "vision": True | False | None, "thinking": True | False | None}
None = the provider doesn't say. The page groups by `vision` and marks
`thinking` models, which spend the short token budget thinking and say nothing.
"""

import re

# Things that show up in /models lists but can't hold a conversation
_NOT_CHAT = re.compile(
    r"embed|tts|whisper|transcrib|dall-e|gpt-image|imagen|veo|sora|moderation|realtime|"
    r"audio|davinci|babbage|search|guard|aqa|computer-use|codex|lyria|speech", re.I)

# OpenAI reasoning models: o1/o3/o4…, gpt-5* — except the gpt-5*-chat-latest ones
_OPENAI_THINKING = re.compile(r"^(o\d|gpt-5)", re.I)


def is_chat_model(model_id: str) -> bool:
    return not _NOT_CHAT.search(model_id)


def sort_models(models: list) -> list:
    """Pictures-OK first, then unknown, then text-only; non-thinking before thinking."""
    rank = {True: 0, None: 1, False: 2}
    return sorted(models, key=lambda m: (rank[m.get("vision")], bool(m.get("thinking")), m["id"]))


def parse_openai_models(body: dict, base_url: str) -> list:
    """Parse a {base}/models response from any OpenAI-compatible server."""
    out, seen = [], set()
    for m in body.get("data", []) or []:
        mid = str(m.get("id", ""))
        if mid.startswith("models/"):          # Gemini
            mid = mid[len("models/"):]
        if not mid or mid in seen or not is_chat_model(mid):
            continue
        seen.add(mid)
        vision = thinking = None
        arch = m.get("architecture") or {}
        if "input_modalities" in arch:          # OpenRouter
            if "text" not in (arch.get("output_modalities") or ["text"]):
                continue
            vision = "image" in arch["input_modalities"]
        if "api.openai.com" in base_url:
            vision = True if re.match(r"^(gpt-4o|gpt-4\.1|gpt-5|o\d)", mid) else None
            thinking = bool(_OPENAI_THINKING.match(mid)) and "chat-latest" not in mid
        out.append({"id": mid, "vision": vision, "thinking": thinking})
    return sort_models(out)


def parse_ollama_show(name: str, show: dict) -> dict:
    caps = show.get("capabilities")
    if caps is None:                           # older Ollama: no capability list
        return {"id": name, "vision": None, "thinking": None}
    return {"id": name, "vision": "vision" in caps, "thinking": "thinking" in caps}
