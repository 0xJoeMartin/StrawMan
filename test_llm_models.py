#!/usr/bin/env python3
"""
Model-list parsing checks (canned provider responses, no network).

    python test_llm_models.py
"""

from llm.models import parse_ollama_show, parse_openai_models


def check(name, cond, extra=""):
    print(f"  {'ok ' if cond else 'FAIL'}  {name}{'' if cond else '  ' + str(extra)}")
    if not cond:
        raise SystemExit(1)


def ids(models):
    return [m["id"] for m in models]


def main():
    print("OpenAI")
    body = {"data": [{"id": i} for i in [
        "gpt-4.1-mini", "gpt-5-mini", "gpt-5-chat-latest", "o4-mini", "text-embedding-3-small", "whisper-1",
        "tts-1", "dall-e-3", "gpt-image-1", "gpt-4o-realtime-preview", "gpt-4o-mini-transcribe",
        "omni-moderation-latest", "gpt-4o", "davinci-002", "gpt-3.5-turbo"]]}
    ms = parse_openai_models(body, "https://api.openai.com/v1")
    by = {m["id"]: m for m in ms}
    check("non-chat models dropped", set(by) == {"gpt-4.1-mini", "gpt-5-mini", "gpt-5-chat-latest", "o4-mini",
                                                   "gpt-4o", "gpt-3.5-turbo"}, ids(ms))
    check("reasoning models flagged", by["gpt-5-mini"]["thinking"] and by["o4-mini"]["thinking"]
          and not by["gpt-4.1-mini"]["thinking"] and not by["gpt-5-chat-latest"]["thinking"])
    check("vision known for 4o/4.1/5, unknown for 3.5", by["gpt-4.1-mini"]["vision"] is True
          and by["gpt-3.5-turbo"]["vision"] is None)
    check("non-thinking vision models sort first", ids(ms)[:3] == ["gpt-4.1-mini", "gpt-4o", "gpt-5-chat-latest"], ids(ms))

    print("OpenRouter")
    body = {"data": [
        {"id": "openai/gpt-4.1-mini", "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]}},
        {"id": "meta/llama-text", "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]}},
        {"id": "some/image-gen", "architecture": {"input_modalities": ["text"], "output_modalities": ["image"]}}]}
    ms = parse_openai_models(body, "https://openrouter.ai/api/v1")
    check("vision from input_modalities, image generators dropped",
          [(m["id"], m["vision"]) for m in ms] == [("openai/gpt-4.1-mini", True), ("meta/llama-text", False)], ms)

    print("Gemini")
    body = {"data": [{"id": "models/gemini-3.8-flash"}, {"id": "models/gemini-embedding-2-preview"},
                     {"id": "models/imagen-4"}, {"id": "models/veo-3.1-generate-preview"}]}
    check("models/ prefix stripped, non-chat dropped",
          ids(parse_openai_models(body, "https://generativelanguage.googleapis.com/v1beta/openai")) == ["gemini-3.8-flash"])

    print("Ollama")
    check("capabilities", parse_ollama_show("qwen3-vl:8b", {"capabilities": ["completion", "vision", "thinking"]})
          == {"id": "qwen3-vl:8b", "vision": True, "thinking": True})
    check("text model", parse_ollama_show("qwen3-coder", {"capabilities": ["completion", "tools"]})["vision"] is False)
    check("old Ollama without capabilities", parse_ollama_show("llava", {})["vision"] is None)

    print("Claude thinking handling")
    import types
    from llm.anthropic_provider import THINKING_HEADROOM, text_of, thinking_args
    check("Haiku 5.5: thinking off", thinking_args("claude-haiku-5-5", 60) == {"max_tokens": 60, "thinking": {"type": "disabled"}})
    check("Sonnet 5.5: between_tools (its 'off')", thinking_args("claude-sonnet-5-5", 60)["thinking"] == {"type": "between_tools"})
    check("Opus 5 / Sonnet 5: off", all(thinking_args(m, 60).get("thinking") == {"type": "disabled"} for m in ("claude-opus-5", "claude-sonnet-5")))
    o = thinking_args("claude-opus-5-5", 60)
    check("Opus 5.5 (can't turn off): low effort + headroom", o == {"max_tokens": 60 + THINKING_HEADROOM, "output_config": {"effort": "low"}})
    check("Fable: low effort", "output_config" in thinking_args("claude-fable-5-1", 60))
    check("older models: nothing extra", all(thinking_args(m, 60) == {"max_tokens": 60} for m in
                                              ("claude-haiku-4-5-20251001", "claude-sonnet-4-6", "claude-opus-4-8")))
    blk = lambda t, **kw: types.SimpleNamespace(type=t, **kw)
    resp = types.SimpleNamespace(content=[blk("thinking", thinking=""), blk("text", text="Boo.")], stop_reason="end_turn")
    check("thinking block before the text is skipped", text_of(resp) == "Boo.")
    try:
        text_of(types.SimpleNamespace(content=[blk("thinking", thinking="")], stop_reason="max_tokens"))
        check("no text -> error (so a fallback line is used)", False)
    except RuntimeError:
        check("no text -> error (so a fallback line is used)", True)

    print("\nAll model-list checks passed.")


if __name__ == "__main__":
    main()
