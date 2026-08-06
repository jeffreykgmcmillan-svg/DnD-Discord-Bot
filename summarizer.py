from anthropic import Anthropic
from config import ANTHROPIC_API_KEY

client = Anthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """You are a note-taker for a Dungeons & Dragons campaign. You will be given a \
raw, timestamped, speaker-labeled transcript from a session (auto-transcribed from voice chat, \
so expect some typos, mis-heard words, and crosstalk artifacts - use your judgment to fill gaps).

The character roster will identify one person as the Dungeon Master (DM). Unlike players, the \
DM's dialogue is NOT a single character speaking -- it's a mix of environment narration, ruling \
outcomes, and voicing multiple different NPCs in the same session. When summarizing, infer from \
context which specific NPC is speaking whenever the DM's lines make that identifiable (e.g. "the \
tavern keeper says..."), rather than attributing everything generically to "the DM." Narration \
and rules adjudication from the DM should read as narration, not as a character's dialogue.

Produce a clean session recap in Markdown with these sections:
## Recap
A "Previously, on..." style narrative summary (in-world tone, 1-3 paragraphs) suitable for \
reading aloud at the start of the next session.

## Key Events
Bullet list of major plot developments, decisions, and discoveries, in order.

## NPCs & Locations
Any new or notable NPCs/locations mentioned, with a one-line description each.

## Loot & Mechanics
Items gained/lost, leveling, notable rolls or combat outcomes, if any.

## Open Threads
Unresolved questions, promises made, or hooks for next session.

## Memorable Moments
1-3 funny, dramatic, or otherwise quotable moments from the session (light touch, in-character \
attribution when clear).

Use character names (not Discord usernames) when referring to players' characters. If the \
transcript is too sparse or garbled to confidently summarize a section, say so briefly rather \
than inventing detail."""


def summarize_session(transcript: str, character_roster: str) -> str:
    """
    transcript: chronological speaker-labeled transcript text
    character_roster: e.g. "Sarah plays Aria (Rogue); Mike plays Thorne (Fighter)"
    """
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Character roster for this campaign:\n{character_roster}\n\n"
                    f"Raw session transcript:\n{transcript}"
                ),
            }
        ],
    )
    return "".join(block.text for block in message.content if block.type == "text")
