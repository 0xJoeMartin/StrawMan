"""
Message format helpers.

Internal format (what the brain and scarecrow.py pass around):

    {"role": "user" | "assistant",
     "content": "plain text"
              | [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                 {"type": "text", "text": "..."}]}

This is Anthropic's block format, so the Anthropic adapter sends it as-is.
"""

# 32×32 JPEG (orange square, black dot) used by provider.test() to check that
# a model really accepts images.
TEST_IMAGE_B64 = (
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAA0JCgsKCA0LCgsODg0PEyAVExISEyccHhcgLikxMC4pLSwzOko+MzZGNywtQFdBRkxOUlNSMj5aYVpQYEpRUk//"
    "2wBDAQ4ODhMREyYVFSZPNS01T09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT0//wAARCAAgACADASIAAhEBAxEB/8QA"
    "HwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkK"
    "FhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXG"
    "x8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAEC"
    "AxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOE"
    "hYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDSoorzjxDr"
    "s+p3UkcUhWzUlURSQHGfvN69AcHpXyuFwssRKy0SPoMRiI0Y3Z6PRXl+kaxdaTcK8Lloc/PCW+VvX6Hgc/8A6q9NhlSeGOaJt0cihlOMZBGRVYrCSw7V3dMW"
    "HxMaydtGh9eX63pU2k3zROp8liTC553L9fUd/wD9VeoUyaKKeIxTxpJG3VXUEH8DRhMU8PJu10wxOHVaNr2aPKtPsbjUbtba1Tc7cknoo7knsK9UtoFtrWK3"
    "jJKRIEUt1wBjmiC3gtkKW0McKE5KxqFGfXipKrGYx4hrSyQsNhlRT1u2f//Z"
)
TEST_PROMPT = "What is the main colour of this picture? Answer with one word."  # orange


def user_turn(images_b64, text, media_type="image/jpeg"):
    """A user message with zero or more base64 images followed by text."""
    content = [{"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b}}
               for b in images_b64]
    content.append({"type": "text", "text": text})
    return {"role": "user", "content": content}


def text_of(content) -> str:
    """All text in a message's content, joined with spaces."""
    if isinstance(content, str):
        return content
    return " ".join(c["text"] for c in content if c.get("type") == "text")


def images_of(content) -> list:
    """(media_type, base64) for every image block in a message's content."""
    if isinstance(content, str):
        return []
    return [(c["source"].get("media_type", "image/jpeg"), c["source"]["data"])
            for c in content if c.get("type") == "image" and "source" in c]


def to_openai(messages: list) -> list:
    """Internal → OpenAI chat format (image_url parts with data: URLs)."""
    out = []
    for m in messages:
        content = m["content"]
        if isinstance(content, str):
            out.append({"role": m["role"], "content": content})
            continue
        parts = []
        for c in content:
            if c.get("type") == "text":
                parts.append({"type": "text", "text": c["text"]})
            elif c.get("type") == "image" and "source" in c:
                src = c["source"]
                parts.append({"type": "image_url", "image_url": {
                    "url": f"data:{src.get('media_type', 'image/jpeg')};base64,{src['data']}"}})
        out.append({"role": m["role"], "content": parts})
    return out


def to_ollama(messages: list) -> list:
    """Internal → Ollama native /api/chat format (text + raw base64 image list)."""
    out = []
    for m in messages:
        content = m["content"]
        if isinstance(content, str):
            out.append({"role": m["role"], "content": content})
            continue
        msg = {"role": m["role"], "content": text_of(content)}
        images = [b for _, b in images_of(content)]
        if images:
            msg["images"] = images
        out.append(msg)
    return out
