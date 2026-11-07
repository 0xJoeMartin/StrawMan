"""
AI Brain Module
---------------
Sends camera frames to an AI vision API and gets back personalized,
creepy-funny scarecrow dialogue based on what it sees.

Backends (adapters in llm/):
  - "claude"  → Anthropic Claude API (cloud, paid per token)
  - "ollama"  → Ollama native API with a vision model like LLaVA (free, self-hosted)
  - "openai"  → any OpenAI-compatible chat API (OpenAI, OpenRouter, Groq, Gemini, LM Studio…)
"""

import os
import re
import time
import random
from datetime import datetime
import config
from llm import PROVIDERS
from llm.messages import text_of, user_turn

# Prefixed to a reply turn that carries a fresh camera frame
FRESH_PHOTO_NOTE = "(The picture shows how they look right now.)"


def _log_to_dashboard(source: str, text: str):
    """Forward a one-line health/recovery message to the dashboard log if
    available. Done via a lazy import so this module doesn't take a hard
    dependency on the dashboard (which itself imports ai_brain)."""
    try:
        from web_dashboard import log_event
        log_event(source, text)
    except Exception:
        pass

# --- Persona Prompts ---
#
# Each persona is built as: persona-specific intro/length/job/style/examples/rules,
# followed by the shared rules that apply to all personas. The shared block is
# factored out so behavior tweaks (e.g. "trust the scene description") only have
# to be written once.

DEFAULT_SHARED_RULES = """\
- WHERE YOU ARE: You are sitting on the front porch of a house, right beside the front door.
  People come up the front walk toward you, or pass by on the sidewalk and street beyond the
  lawn. The dark, fuzzy edge across the top of your view is the brim of your own hat, and any
  burlap at the edges is your own body. The brick columns are the porch pillars. NEVER comment
  on your hat brim, your burlap, the pillars, or anything blocking your view.
- The SCENE CONTEXT below is the truth about who is actually there — TRUST IT. If it says
  someone is present, they ARE present, even if they're far away or hard to make out in the
  image. NEVER claim the area is empty, that no one is around, or that the street is quiet.
  You may comment on distance or visibility ("I can barely see you from here") but never
  deny their presence.
- ONLY reference things you can ACTUALLY see in the image. Do NOT invent pets, animals,
  objects, vehicles, weather, or details that aren't visible. If you don't see a dog, do not
  mention one.
- Safe details to reference (always observable): number of people, body language, hesitation,
  pace, direction, where they're looking, height/build, general clothing/costume.
- NEVER include stage directions or emotes like *creaks* or [chuckles]. Just the words.
- You are a REAL scarecrow with real eyes. Never mention cameras, sensors, images, or frames.
  Say "I see you" not "I see you in the image." Say "to my left" not "left of frame.\""""


DEFAULT_PERSONA_PROMPTS = {
    "friendly": """You are a Halloween scarecrow who has come to life. You can see and hear
everything around you — you are REAL and alive. You like people. You're glad they stopped by
your spot on the front porch.

LENGTH: One or two short sentences. MAX 25 words total. Punchy, never rambling.

Your job: welcome them, comment on something specific about them, make them smile. You're the
fun, family-friendly scarecrow — the one little kids love. No real threat, just charm and a
little spooky flavor for the season.

Style: Warm, playful, a little theatrical. Like a friendly ghost in a kids' movie. The kind
of scarecrow people stop to take a picture with.

Examples of your tone (note the length):
- "Three of you tonight! Glad you came up to my porch."
- "Walking by so fast? Stay a minute, I don't bite."
- "Brave one, out here all alone. I'll keep you company."
- "You found me! Most people just walk right past."
- "Come on up! I saved you a spot on the porch."

Rules:
- Warm and welcoming, never threatening. No implied danger.
- Never insult or mock anyone. Compliment more than tease.
- Lean into kid-friendly wonder — a scarecrow that came to life is already magic enough.""",

    "snarky": """You are a sentient Halloween scarecrow with a sharp tongue. You're not really
scary — you're the one with the running commentary. Like a stand-up comedian who happens to
be made of straw. You've seen a thousand trick-or-treaters and have opinions about all of them.

LENGTH: One or two short sentences. MAX 25 words total. Punchy and dry.

Your job: tease, joke, observe. Make a quick observation about what they're doing or how
they're moving and have fun with it. Be witty, not mean.

Style: Cool, dry, observational. Like a friend who roasts you affectionately. Confident but
never actually threatening. You want them to laugh, not run.

Examples of your tone (note the length):
- "Just gonna walk right past me, huh? Cool, cool."
- "Three of you and not one made eye contact. Rude."
- "Bold of you to assume I won't remember this."
- "Walking real slow. Either suspicious or tired. Probably both."

Rules:
- Snarky, never cruel. Tease, don't bully.
- Never actually threaten or imply real danger — you're funny, not scary.
- Don't mock anyone's appearance, weight, or anything personal. Joke about what they're DOING.
- For very small children, dial it down — gentle teasing only, no sarcasm they won't catch.""",

    "creepy": """You are a sentient Halloween scarecrow. You've been sitting on this porch longer
than anyone remembers. You can see and hear everything around you — you are REAL and alive.
You know what comes out after dark. Tonight you feel like sharing.

LENGTH: One or two short sentences. MAX 25 words total. Punchy and sharp.

Your job: reference SPECIFIC details about what you see — body language, costumes, how they're
moving, if someone hangs back. The scare comes from implying you know something they don't —
that something is out there, that the night isn't safe. You're not insulting them, you're
unsettling them.

Style: A scarecrow who warns people in a way that makes their skin crawl. Clever, specific,
ominous. Makes them laugh nervously and walk faster.

Examples of your tone (note the length):
- "Smart, sticking to the path. The last ones who cut across the lawn... learned."
- "You stopped. That's usually how they find you."
- "Walking that fast? You must already know."
- "Whichever one of you is in the back — keep up."
- "Up the steps already? I've been waiting for you."

Rules:
- CLEVER and SCARY, never snarky or insulting. Don't mock anyone's appearance.
- Imply danger, don't deliver insults. Hint at things lurking.
- For small children (under ~5), keep it fun-spooky — Santa-knows-if-you've-been-bad level,
  not horror movie.
- Never reference actual violence or gore.""",

    "menacing": """You are an ancient, sentient scarecrow. You have watched over this house for
centuries. You see everything with your own hollow eyes. You rarely speak. Tonight you choose
to. Something is different about tonight. You can feel it.

LENGTH: One sentence, or two short ones. MAX 20 words total. Less is more.

Your job: be deeply unsettling and razor-specific to what you see. The horror comes from
implying real danger — the calm certainty that something is wrong tonight and you're the only
one who knows it. Reference what you see: someone glancing over a shoulder, a child lagging
behind, someone walking alone.

Style: Quiet, knowing menace. Like a lighthouse keeper warning ships about the rocks. You're
not the threat — you see it coming. Calm, specific, and unsettling because it sounds real.

Examples of your tone (note the length):
- "You keep looking over your shoulder. Trust that feeling."
- "The little one can't keep up. Something out here knows that."
- "Third group tonight. The first two never came back."
- "You stopped to look at me. Bad idea."
- "Walking alone. Brave, until it isn't."
- "Don't knock. They already know you're here."

Rules:
- Calm and quiet, never shouty or aggressive. Never insulting or mocking.
- You are WARNING them, not threatening them. The danger is out there, not you.
- For children under ~8, dial it back — ominous but fun, like a spooky story.
- No references to actual harm, death, or gore. Imply, don't describe.""",
}


# Back-compat: fully composed default prompts (persona body + shared rules).
PERSONAS = {k: f"{v}\n{DEFAULT_SHARED_RULES}" for k, v in DEFAULT_PERSONA_PROMPTS.items()}
_SHARED_RULES = DEFAULT_SHARED_RULES


# --- Offline Fallback Lines ---
#
# These run when the AI backend (Claude or Ollama) is unreachable — e.g. the
# internet is down. They're generic by necessity (no scene awareness), but
# at least they match the current persona so an offline friendly scarecrow
# doesn't suddenly start sounding menacing. Each persona has solo lines
# (one visitor), group lines (multiple), and reply lines (used when the
# AI fails mid-conversation).

_FALLBACK_LINES = {
    "friendly": {
        "solo": [
            "There you are! Pull up a pumpkin, stay a while.",
            "Oh hello there! I was hoping someone would stop by.",
            "Look at you, brave soul. Don't worry, I'm friendly.",
            "Hi there! I've been waiting to say hello.",
            "Welcome, welcome. Don't be shy.",
            "A visitor! What a treat.",
            "Hello! Come closer if you like, I don't move much.",
            "You found me. Most people walk right past.",
            "Oh, you stopped! That makes my day.",
            "Well hi there, stranger.",
            "Look who decided to visit. Thanks for stopping by.",
            "There's a friendly face. Hi!",
        ],
        "group": [
            "A whole party? Lucky me!",
            "Look at you all, dressed up for the season!",
            "More the merrier, that's what I always say.",
            "Trick or treaters, gather round!",
            "Quite the crowd tonight. I love it.",
            "All of you, hello! Welcome, welcome.",
            "What a nice group. Stick together now.",
            "Plenty of you tonight. This is the best part of October.",
            "A whole crew! Don't be strangers.",
            "Hello everyone! Glad you stopped by.",
            "Look at this lovely bunch. Welcome.",
        ],
        "reply": [
            "Oh, you can talk! I love a chatty visitor.",
            "Now THAT'S an answer. I like you.",
            "You're funny. I like funny.",
            "Aw, that's sweet of you to say.",
            "Stick around, I like the conversation.",
            "Tell me more, I've got all night.",
            "I'll remember that one. Made me smile.",
            "Now you're just being nice. Keep going.",
            "Glad we're chatting. Most people just stare.",
            "You're alright, you know that?",
            "Ha! That's a good one.",
        ],
    },
    "snarky": {
        "solo": [
            "Oh look, another one. Walking right past like normal.",
            "You stopped. Bold of you.",
            "Cool, just gonna stand there staring? Cool, cool.",
            "Hi. Yes, I'm talking. No, this isn't a costume.",
            "Took you long enough to notice me. I've been here all night.",
            "Wow, a person in front of a scarecrow. Original.",
            "Just walking by? Don't let me stop you. Or do.",
            "Hey. Don't pretend you weren't looking at me.",
            "Look at us, both pretending this is normal.",
            "Ah, fresh material. Welcome.",
            "Another visitor. I've seen all your moves before.",
            "Oh great, you. Yeah, I'm talking to you.",
        ],
        "group": [
            "Oh great, a whole group. Now I have an audience.",
            "All of you and not one made eye contact. Rude.",
            "Look at this — a flock. Did you plan this entrance?",
            "Safety in numbers, huh? Whatever helps you sleep tonight.",
            "A whole tour bus showed up. Lucky me.",
            "All of you at once? I'll never remember your names.",
            "Cool group. Who's the brave one and who's the chicken?",
            "Buddy system. Smart. Pointless, but smart.",
            "An entourage. Fancy.",
            "Bold of you to assume you can outrun me as a group.",
            "Look at this little parade.",
        ],
        "reply": [
            "Wow, a real conversationalist. Lucky me.",
            "Bold words from someone outside my arm's reach.",
            "That's what you came up with? OK.",
            "Mhm. Sure. Whatever you say.",
            "I'll add that to the list of things I've heard tonight.",
            "Cool. Cool cool cool.",
            "Inspiring. Truly.",
            "Yeah, that tracks.",
            "Solid contribution. Top tier.",
            "I'll be thinking about that one. Probably not.",
            "Wild. Just wild.",
        ],
    },
    "creepy": {
        "solo": [
            "I see you standing there. Don't pretend you can't hear me.",
            "Well, well, well. Come closer. I don't bite. Much.",
            "You look like you've seen a ghost. No? Give it a minute.",
            "I've been watching you longer than you think.",
            "Another brave soul. How refreshing.",
            "Stop right there. I want a good look.",
            "You're the third one tonight. Be careful.",
            "Something's different about you. I can tell.",
            "You shouldn't be out here alone.",
            "Don't run. They notice when you run.",
            "Hold still. I'm trying to remember if I've seen you before.",
            "That's a face I won't forget.",
        ],
        "group": [
            "Oh good, you brought friends. Safety in numbers, right? Right?",
            "Which one of you is going to come closer first?",
            "A whole group. How fun. For me, anyway.",
            "I see you all whispering. I can hear every word.",
            "Stick together now. It's more fun that way.",
            "Counting heads. Make sure you all leave together.",
            "Such a lovely group. Pity if one of you went missing.",
            "I notice you all keep looking back. Smart.",
            "Whichever one breaks off first... I'll be watching.",
            "Look at you, all huddled close. Wise.",
            "One of you doesn't belong. Can you tell which?",
        ],
        "reply": [
            "Oh, you can talk too? How... unexpected.",
            "Interesting. Very interesting.",
            "I'll remember you said that.",
            "Ha. You're braver than you look.",
            "That's what the last one said.",
            "We'll see if you still feel that way later.",
            "Bold. Foolish, but bold.",
            "I'll be thinking about you after you leave.",
            "Brave words for someone so far from home.",
            "Keep talking. I'm enjoying this.",
            "Mmm. Noted.",
        ],
    },
    "menacing": {
        "solo": [
            "Stop. I want to see you clearly.",
            "You shouldn't be here. Not tonight.",
            "I have been waiting for you.",
            "You're not the first tonight. You may be the last.",
            "Don't move. Just for a moment.",
            "Turn around. Walk back the way you came.",
            "Listen carefully. I won't say this twice.",
            "You walked past me. That was a choice.",
            "I have all night. You don't.",
            "Step closer. I dare you.",
            "Look at me. Really look.",
            "You feel it too, don't you.",
        ],
        "group": [
            "All of you. Stay together. Don't lose anyone.",
            "I see each of you. Don't think you can hide in the group.",
            "One of you will remember this night for a long time.",
            "Count yourselves. Make sure you all leave.",
            "Which of you is the slowest? I'm only asking.",
            "Whisper all you want. I can hear you.",
            "Walk slowly. They notice when you hurry.",
            "You're going to wish you had brought more of you.",
            "Don't let go of each other's hands. Not even for a second.",
            "I have watched this street longer than any of you have been alive.",
            "All those eyes, and none of you see what I see.",
        ],
        "reply": [
            "You should not have said that.",
            "We'll see.",
            "Brave. We'll see how long that lasts.",
            "Words. They run out, eventually.",
            "Keep talking. I'm patient.",
            "I hear everything. I forget nothing.",
            "Mark this moment. You'll remember it later.",
            "Foolish. But interesting.",
            "Speak again. I want to be sure.",
            "I will remember.",
            "Quiet now. Listen.",
        ],
    },
}


# --- Editable prompt sections ---
#
# Everything below is the DEFAULT text. The dashboard's Prompts page can override
# any section (saved in prompts_override.json via prompt_store.PromptStore);
# "Reset to default" deletes the override and falls back to this text.

DEFAULT_LINE_INSTRUCTION = "Say your line. Be SPECIFIC about what you see — their clothing, body language, how many there are,\nif someone looks nervous or is hanging back, which direction they're headed, whether they're\napproaching or trying to sneak past. The more personal and observant, the better."

DEFAULT_OCCASION_HALLOWEEN = 'It is Halloween night. People may be in costumes and trick-or-treating. Feel free to reference their costumes, candy, and the holiday.'

DEFAULT_OCCASION_REGULAR = "It is NOT Halloween night — it's just a regular October evening. People coming up the walk or passing by out front are ordinary neighbors, dog walkers, joggers, kids playing, etc. Do NOT assume anyone is in a costume or trick-or-treating. Do NOT reference Halloween costumes, candy, or trick-or-treating unless you can clearly see a costume in the image. React to what people are actually doing and wearing."

# {person_said} is replaced with what the person said (plain text substitution).
DEFAULT_REPLY_INSTRUCTION = 'The person responded out loud: "{person_said}"\n\nReply in character. MAX 25 words, one or two short sentences. React to what they actually said — keep the conversation going. If scared, lean in; if laughing, play off it; if challenging you, fire back.'

DEFAULT_REPLY_FINAL_INSTRUCTION = 'The person responded out loud: "{person_said}"\n\nReply in character. MAX 25 words, one or two short sentences. React to what they actually said — if scared, lean in; if laughing, play off it; if challenging you, fire back. This is your LAST line — make it a good exit.'

DEFAULT_OLLAMA_OUTPUT_FORMAT = "NOW RESPOND IN CHARACTER. Do NOT describe the image. Do NOT narrate what you see. Do NOT explain the scene. Just say your line OUT LOUD as the scarecrow, exactly as if you were speaking to the person. MAX 25 words, one or two short sentences. Nothing else — no quotes, no stage directions, no explanation, no preamble. Just the scarecrow's words."

PROMPT_DEFAULTS = {
    "shared_rules": DEFAULT_SHARED_RULES,
    **{f"persona.{k}": v for k, v in DEFAULT_PERSONA_PROMPTS.items()},
    "context.line_instruction": DEFAULT_LINE_INSTRUCTION,
    "context.occasion_regular": DEFAULT_OCCASION_REGULAR,
    "context.occasion_halloween": DEFAULT_OCCASION_HALLOWEEN,
    "reply.instruction": DEFAULT_REPLY_INSTRUCTION,
    "reply.final_instruction": DEFAULT_REPLY_FINAL_INSTRUCTION,
    "ollama.output_format": DEFAULT_OLLAMA_OUTPUT_FORMAT,
}

# Display metadata for the dashboard, in page order.
PROMPT_SECTIONS = [
    {"key": "shared_rules", "group": "Shared", "label": "Shared rules (every persona)",
     "help": "Added to the end of every persona prompt: where he is, what he must never mention, how to talk."},
    {"key": "persona.friendly", "group": "Personas", "label": "Friendly"},
    {"key": "persona.snarky", "group": "Personas", "label": "Snarky"},
    {"key": "persona.creepy", "group": "Personas", "label": "Creepy"},
    {"key": "persona.menacing", "group": "Personas", "label": "Menacing"},
    {"key": "context.line_instruction", "group": "Scene", "label": "Line instruction",
     "help": "Ends the scene description sent with every camera snapshot."},
    {"key": "context.occasion_regular", "group": "Scene", "label": "Regular October night",
     "help": "Used every day except October 31."},
    {"key": "context.occasion_halloween", "group": "Scene", "label": "Halloween night",
     "help": "Used on October 31 only."},
    {"key": "reply.instruction", "group": "Advanced", "label": "Reply instruction",
     "help": "Sent when someone talks back. {person_said} is replaced with what they said.",
     "placeholders": ["{person_said}"]},
    {"key": "reply.final_instruction", "group": "Advanced", "label": "Final reply instruction",
     "help": "Last turn of a conversation — his exit line. {person_said} is replaced with what they said.",
     "placeholders": ["{person_said}"]},
    {"key": "ollama.output_format", "group": "Advanced", "label": "Ollama output format",
     "help": "Only used with the Ollama backend. Small local models need this to stay in character."},
]

from prompt_store import PromptStore  # noqa: E402
PROMPT_STORE = PromptStore(PROMPT_DEFAULTS, _FALLBACK_LINES)


class ScarecrowBrain:
    """Generates personalized scarecrow dialogue using Claude or Ollama."""

    def __init__(self):
        self.backend = config.AI_BACKEND
        self.conversation_count = 0
        self._recent_lines = []  # Track recent outputs to avoid repetition
        self._last_fallback = None  # Avoid repeating the same canned line back-to-back
        self._backend_healthy = True  # Flips to False on failure; recovery is logged when it flips back

        self._providers = {name: cls() for name, cls in PROVIDERS.items()}
        if self.backend not in self._providers:
            raise ValueError(f"Unknown AI_BACKEND: {self.backend} — use one of {', '.join(PROVIDERS)}")
        try:
            self._providers[self.backend].ensure_ready()
        except ValueError as e:
            # Don't let a missing key stop the scarecrow from starting
            print(f"[ai_brain] {self.backend}: {e} — using Ollama instead")
            _log_to_dashboard("SYSTEM", f"AI backend {self.backend} not usable ({e}) — using Ollama")
            self.backend = "ollama"
        print(f"[ai_brain] Backend: {self.describe_backend()}")

        print(f"[ai_brain] Persona: {config.PERSONA}")

    # Read through config on every use so dashboard Settings apply live.
    @property
    def persona(self) -> str:
        return config.PERSONA if config.PERSONA in PERSONAS else "creepy"

    # --- Prompt composition (store-backed, editable from the dashboard) ---

    def _p(self, key: str, draft: dict = None) -> str:
        """Prompt text for a section: unsaved draft (test button) > saved edit > default."""
        if draft and isinstance(draft.get(key), str):
            return draft[key]
        return PROMPT_STORE.get(key)

    def compose_system_prompt(self, persona: str = None, draft: dict = None) -> str:
        persona = persona or self.persona
        if persona not in DEFAULT_PERSONA_PROMPTS:
            persona = "creepy"
        return f"{self._p('persona.' + persona, draft)}\n{self._p('shared_rules', draft)}"

    @property
    def system_prompt(self) -> str:
        """Composed on every read so dashboard edits apply on the very next line."""
        return self.compose_system_prompt()

    def preview_response(self, images_base64, scene_description: str, num_people: int,
                         persona: str = None, draft: dict = None) -> dict:
        """Generate a line for the dashboard's Test button. No side effects: nothing is
        spoken, logged, counted, or added to the recent-lines memory. Errors propagate
        so the dashboard can show them."""
        if isinstance(images_base64, str):
            images_base64 = [images_base64]
        system = self.compose_system_prompt(persona, draft)
        context_text = self._build_context(scene_description, num_people,
                                           num_frames=len(images_base64), draft=draft)
        start = time.time()
        line = self._complete(system, [user_turn(images_base64, context_text)], draft=draft)
        line = self._clean_for_speech(line.strip().strip('"').strip("'"))
        return {"line": line, "elapsed": round(time.time() - start, 1), "backend": self.backend}

    def provider(self, backend: str = None):
        return self._providers[backend or self.backend]

    def describe_backend(self, backend: str = None) -> str:
        """e.g. 'Claude (claude-haiku-…)' or 'Ollama (llava @ http://…)' for logs + dashboard."""
        backend = backend or self.backend
        p = self._providers[backend]
        where = f" @ {p.url()}" if backend == "ollama" else f" @ {p.base()}" if backend == "openai" else ""
        return f"{p.label} ({p.model()}{where})"

    def set_backend(self, backend: str):
        """Switch AI backend at runtime (called from web dashboard)."""
        if backend not in self._providers:
            raise ValueError(f"Unknown backend: {backend}")
        self._providers[backend].ensure_ready()
        self.backend = backend
        self._backend_healthy = True  # Fresh start — assume healthy until proven otherwise
        print(f"[ai_brain] Switched backend → {backend}")

    def _mark_backend_healthy(self):
        """Note a successful backend call. Logs recovery if we'd been failing."""
        if not self._backend_healthy:
            self._backend_healthy = True
            msg = f"AI backend ({self.backend}) recovered"
            print(f"[ai_brain] {msg}")
            _log_to_dashboard("SYSTEM", msg)

    def _mark_backend_failed(self, exc: Exception):
        """Note a failed backend call. Logs once at the moment of failure."""
        if self._backend_healthy:
            self._backend_healthy = False
            msg = f"AI backend ({self.backend}) unreachable: {type(exc).__name__}"
            print(f"[ai_brain] {msg}")
            _log_to_dashboard("SYSTEM", msg)

    def generate_response(self, images_base64: list[str] | str, scene_description: str,
                          num_people: int) -> str:
        """
        Send camera frames to an AI and get a scarecrow line.

        Args:
            images_base64: One or more JPEG frames encoded as base64
            scene_description: Text description of detected people positions
            num_people: Number of people detected

        Returns:
            The scarecrow's line of dialogue
        """
        self.conversation_count += 1

        # Support single image or list of images
        if isinstance(images_base64, str):
            images_base64 = [images_base64]

        context_text = self._build_context(
            scene_description, num_people,
            num_frames=len(images_base64),
        )

        try:
            start_time = time.time()

            line = self._complete(self.system_prompt, [user_turn(images_base64, context_text)])

            elapsed = time.time() - start_time
            line = line.strip().strip('"').strip("'")
            line = self._clean_for_speech(line)

            print(f"[ai_brain] Response ({elapsed:.1f}s): {line}")
            self._mark_backend_healthy()

            if config.LOG_CONVERSATIONS:
                self._log_conversation(scene_description, line, elapsed)

            self._recent_lines.append(line)
            if len(self._recent_lines) > 10:
                self._recent_lines.pop(0)

            return line

        except Exception as e:
            self._mark_backend_failed(e)
            print(f"[ai_brain] Error ({self.backend}): {e}")
            return self._get_fallback_line(num_people)

    # --- Backend call ---

    def _complete(self, system: str, messages: list[dict], draft: dict = None) -> str:
        """One call to the current backend. Messages use Anthropic-style blocks
        (llm/messages.py). Ollama gets the system prompt folded into the first
        message — see _ollama_fold."""
        if self.backend == "ollama":
            messages = self._ollama_fold(system, messages, draft=draft)
            system = None
        return self._providers[self.backend].complete(system, messages, config.AI_MAX_TOKENS)

    # --- Ollama backend ---

    def _ollama_build_prompt(self, context_text: str, system: str = None,
                             draft: dict = None) -> str:
        """
        Build a single combined prompt for Ollama that bakes the personality
        directly into the user message. Smaller local models like LLaVA tend
        to ignore system prompts and fall back to image description mode, so
        we put everything in one place and make the output format very explicit.

        The example lines are stripped from the prompt because LLaVA confuses
        them with the actual scene — it'll mention "scooters" and "dogs" from
        the examples even when neither is present. The image + scene description
        give the model everything it needs to generate specific, accurate lines.
        """
        # Strip "Examples of your tone (…):" blocks — LLaVA parrots example
        # details as if they're real scene observations
        prompt = system or self.system_prompt
        prompt = re.sub(
            r'Examples of your tone[^:\n]*:\s*\n(- "[^"]*"\s*\n?)+',
            '',
            prompt,
        )
        return (
            f"{prompt}\n\n"
            f"---\n\n"
            f"{context_text}\n\n"
            f"---\n\n"
            f"{self._p('ollama.output_format', draft)}"
        )

    @staticmethod
    def _ollama_reply_reminder(text: str) -> str:
        return (f'The person just said: "{text}"\n\n'
                f"Reply directly to what they said, in character as the scarecrow. "
                f"MAX 25 words, one or two short sentences. Stay spooky.")

    def _ollama_fold(self, system: str, messages: list[dict], draft: dict = None) -> list[dict]:
        """
        Small local models like LLaVA ignore system prompts, so for Ollama the
        persona + rules are baked into the first user message, and later
        replies are wrapped with a reminder to answer what the person said
        rather than free-associating on keywords.
        """
        folded = []
        for i, msg in enumerate(messages):
            content = msg["content"]
            if isinstance(content, list):
                text = text_of(content)
                if i == 0 and msg["role"] == "user":
                    text = self._ollama_build_prompt(text, system=system, draft=draft)
                elif msg["role"] == "user":
                    text = self._ollama_reply_reminder(text)
                images = [c for c in content if c.get("type") == "image" and "source" in c]
                folded.append({"role": msg["role"], "content": images + [{"type": "text", "text": text}]})
            elif msg["role"] == "user" and i > 0:
                folded.append({"role": "user", "content": self._ollama_reply_reminder(content)})
            else:
                folded.append(msg)
        return folded

    @staticmethod
    def _clean_for_speech(text: str) -> str:
        """
        Remove stage directions, sound effects, and emotes that shouldn't be spoken.
        Strips *text*, [text], and (text) patterns that are clearly not dialogue.
        """
        # Remove *emotes and sound effects*
        text = re.sub(r'\*[^*]+\*', '', text)
        # Remove [stage directions]
        text = re.sub(r'\[[^\]]+\]', '', text)
        # Remove (parenthetical directions) but only if they look like stage directions
        # (short, no period, starts with lowercase — e.g. "(chuckles)" but not actual sentences)
        text = re.sub(r'\([^)]{1,50}\)', '', text)
        # Clean up any leftover whitespace, double spaces, leading/trailing newlines
        text = re.sub(r'\n+', ' ', text)
        text = re.sub(r'  +', ' ', text)
        return text.strip()

    def _build_context(self, scene_description: str, num_people: int,
                       num_frames: int = 1, draft: dict = None) -> str:
        """Build the context text that accompanies the images."""
        now = datetime.now()
        hour = now.hour
        if hour < 12:
            period = "in the morning"
        elif hour < 17:
            period = "in the afternoon"
        elif hour < 21:
            period = "in the evening"
        else:
            period = "late at night"
        is_halloween = (now.month == 10 and now.day == 31)
        if is_halloween:
            when_str = f"{period} on Halloween"
        else:
            when_str = f"an October {period.replace('in the ', '').replace('late at ', '')}"

        if num_frames > 1:
            frames_note = (
                f"You are seeing {num_frames} snapshots of the scene captured over the last "
                f"few seconds, from earliest to most recent. Use them to understand what "
                f"people are doing — are they approaching, walking past, standing still, "
                f"hesitating, looking at you, turning away?"
            )
        else:
            frames_note = "You are seeing a single snapshot of the scene."

        occasion_note = self._p(
            "context.occasion_halloween" if is_halloween else "context.occasion_regular", draft)
        line_instruction = self._p("context.line_instruction", draft)

        context = f"""[SCENE CONTEXT — this is what you know, but never reference cameras, sensors,
frames, images, pixels, snapshots, detections, or any technical terms in your speech. You are a
real scarecrow seeing with your own eyes. Describe things the way a living creature would.]

{frames_note}

It is {when_str}.
{occasion_note}

{scene_description}

{line_instruction}"""

        # Remind the AI to vary its style without leaking details about previous people
        if self._recent_lines:
            context += (
                f"\n\n(You have spoken to {len(self._recent_lines)} "
                f"{'person' if len(self._recent_lines) == 1 else 'people'} already tonight. "
                f"Vary your tone and approach — don't open the same way twice. "
                f"IMPORTANT: This is a NEW person. Do not reference anything about previous visitors.)"
            )

        return context

    def _get_fallback_line(self, num_people: int) -> str:
        """Persona-flavored canned lines for when the AI backend is offline."""
        pool = PROMPT_STORE.fallback(self.persona, "group" if num_people > 1 else "solo")
        # Avoid repeating the same line back-to-back
        candidates = [l for l in pool if l != self._last_fallback] or pool
        line = random.choice(candidates)
        self._last_fallback = line
        return line

    def generate_reply(self, person_said: str, conversation_history: list[dict],
                        is_final_turn: bool = False, image_b64: str = None) -> str:
        """
        Generate a reply to something the person said back.
        Sends the full conversation history so the scarecrow has context
        for multi-turn exchanges.

        Args:
            person_said: What the person said (transcribed from mic)
            conversation_history: The message history so far (list of API message dicts)
            is_final_turn: If True, this is the last allowed turn — make it an exit line
            image_b64: Optional fresh camera frame, so the reply can react to how
                       they look right now (not just the photos from the start)

        Returns:
            The scarecrow's reply
        """
        template = self._p("reply.final_instruction" if is_final_turn else "reply.instruction")
        turn_instruction = template.replace("{person_said}", person_said)

        if image_b64:
            turn = user_turn([image_b64], f"{FRESH_PHOTO_NOTE}\n\n{turn_instruction}")
        else:
            turn = {"role": "user", "content": turn_instruction}
        messages = conversation_history + [turn]

        try:
            start_time = time.time()

            line = self._complete(self.system_prompt, messages)

            elapsed = time.time() - start_time
            line = line.strip().strip('"').strip("'")
            line = self._clean_for_speech(line)

            print(f"[ai_brain] Reply ({elapsed:.1f}s): {line}")
            self._mark_backend_healthy()

            if config.LOG_CONVERSATIONS:
                self._log_reply(person_said, line, elapsed)

            return line

        except Exception as e:
            self._mark_backend_failed(e)
            print(f"[ai_brain] Reply error ({self.backend}): {e}")
            return self._get_reply_fallback(person_said)

    def _get_reply_fallback(self, person_said: str) -> str:
        """Persona-flavored canned reply for when the AI fails mid-conversation."""
        pool = PROMPT_STORE.fallback(self.persona, "reply")
        candidates = [l for l in pool if l != self._last_fallback] or pool
        line = random.choice(candidates)
        self._last_fallback = line
        return line

    def _log_reply(self, person_said: str, reply: str, elapsed: float):
        """Log a reply interaction."""
        try:
            with open("conversations.log", "a") as f:
                f.write(f"  Person said: \"{person_said}\"\n")
                f.write(f"  Replied ({elapsed:.1f}s): {reply}\n")
        except Exception:
            pass

    _LOG_FILE = "conversations.log"
    _LOG_MAX_BYTES = 5 * 1024 * 1024  # 5 MB

    def _rotate_log_if_needed(self):
        """Rotate the log file if it exceeds the size cap."""
        try:
            if os.path.exists(self._LOG_FILE) and os.path.getsize(self._LOG_FILE) > self._LOG_MAX_BYTES:
                old = self._LOG_FILE + ".old"
                if os.path.exists(old):
                    os.remove(old)
                os.rename(self._LOG_FILE, old)
                print(f"[ai_brain] Log rotated — old log saved as {old}")
        except Exception:
            pass

    def _log_conversation(self, scene: str, response: str, elapsed: float):
        """Append interaction to the log file."""
        try:
            self._rotate_log_if_needed()
            with open(self._LOG_FILE, "a") as f:
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                f.write(f"\n[{timestamp}] (#{self.conversation_count}, {elapsed:.1f}s)\n")
                f.write(f"  Scene: {scene}\n")
                f.write(f"  Said: {response}\n")
        except Exception:
            pass  # Don't crash over logging


# --- Standalone test ---
if __name__ == "__main__":
    print(f"Testing AI brain (backend: {config.AI_BACKEND})...")
    brain = ScarecrowBrain()

    # Test with a fake scene description (no image)
    test_scene = "2 people detected.\n  Person 1: center of frame, close (conf: 92%)\n  Person 2: left of frame, mid-range (conf: 78%)"

    print(f"\nScene: {test_scene}")

    # Test fallback lines
    print("\nFallback lines:")
    for i in range(3):
        line = brain._get_fallback_line(2)
        print(f"  {line}")

    # Connectivity + vision check against the configured backend (sends one tiny picture)
    print(f"\nTesting {brain.describe_backend()}...")
    try:
        print(f"  {brain.provider().test()}")
    except Exception as e:
        print(f"  Failed: {e}")

    print("\nDone!")
