"""Makes the recording screen's spoken cues, in huvu's own voice.

The screen (frontend/src/components/VoiceClips.tsx) says these while a person
records clips for the wake word, so one away from the phone still knows what
to do. They are MP3s in frontend/public/audio/cues/, made once and kept in the
repo. Run it again after changing a line here or huvu's voice:

    cd backend && uv run python ../wakeword/cues.py
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import edge_tts

# huvu's voice, as in huvu.toml's [speech].
VOICE = "en-US-AvaNeural"
OUT = Path(__file__).resolve().parent.parent / "frontend" / "public" / "audio" / "cues"

# Each step's id (frontend/src/voice/clipSteps.ts) says how it starts. The
# voice reads huvu's name as written, so it is spelled as said.
CUES = {
    "near": "Let's begin. Hold the phone the way you usually do, and say, hey hoo voo. Wait for the chime each time.",
    "table": "Now put the phone down on a table, and step back an arm's length.",
    "far": "Now go far. Leave the phone here, and go to the other side of the room. Listen for the chime.",
    "room": "Now go to the next room. Leave the door open, and call me from there.",
    "ways": "Come back to the phone. Now say it in different ways. I'll tell you how.",
    "ways_0": "Quickly.",
    "ways_1": "Slowly.",
    "ways_2": "Quietly.",
    "ways_3": "Loudly.",
    "ways_4": "As if you're tired.",
    "ways_5": "As a question.",
    "ways_6": "While you walk.",
    "ways_7": "With the phone at your side.",
    "noise": "Now turn on the TV, some music, or a fan. Then say it from where you usually sit.",
    "near_misses": "Turn the noise off, and come back to the phone. Now read each line on the screen, once.",
    "talk": "Last one. Talk for two minutes, about anything at all. Don't say my name.",
    "too_loud": "Too loud. Step back a little.",
    "too_quiet": "I barely heard that. A little louder.",
    "too_long": "Just the two words, please.",
    "too_long_line": "Just the line on the screen, please.",
    "try_again": "Try again.",
    "five_more": "Five more.",
    "done": "That's everything. Thank you.",
}


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, text in CUES.items():
        await edge_tts.Communicate(text, VOICE).save(str(OUT / f"{name}.mp3"))
        print(name)


if __name__ == "__main__":
    asyncio.run(main())
