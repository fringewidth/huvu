// What the recording screen (components/VoiceClips.tsx) walks a person
// through, in order, to train the "hey huvu" model: the phrase where they
// will say it (close, across the room, from the next room, over noise, in
// different ways), then what it must not wake for (phrases that sound
// close, and ordinary talk). docs/wake-word.md has the training.
//
// Each step's id names its clips' folder on the host and its spoken cue
// (public/audio/cues/<id>.mp3, made by wakeword/cues.py).

export interface Step {
  id: string;
  // "wake" clips say the phrase. "other" clips must not wake huvu.
  kind: "wake" | "other";
  title: string;
  how: string;
  // Clips to keep. For talk, seconds.
  target: number;
  // Shown one per clip, in turn.
  prompts?: string[];
  // huvu says each prompt too (cue `<step>_<n>`).
  sayPrompts?: boolean;
  // The person is away from the phone: a quiet clip is what this step is
  // for.
  far?: boolean;
  // One long take, cut into pieces, not one clip per utterance.
  talk?: boolean;
}

export const PHRASE = "hey huvu";

export const STEPS: Step[] = [
  {
    id: "near",
    kind: "wake",
    title: "Close to you",
    how: "Hold the phone the way you usually do. Say “hey huvu” the way you would to ask something. After each one, wait for the chime, then say it again.",
    target: 15,
  },
  {
    id: "table",
    kind: "wake",
    title: "On the table",
    how: "Put the phone down on a table or a shelf, and step back an arm's length. Keep going: say it, wait for the chime.",
    target: 15,
  },
  {
    id: "far",
    kind: "wake",
    title: "Go far",
    how: "Leave the phone where it is, and go to the far side of the room. You won't see the screen from there. A chime means it was kept. If it wasn't, huvu says why. huvu calls you on to the next step when this one is done.",
    target: 15,
    far: true,
  },
  {
    id: "room",
    kind: "wake",
    title: "Another room",
    how: "Go into the next room and leave the door open. Call “hey huvu” from there, as loud as you would call someone in the house.",
    target: 10,
    far: true,
  },
  {
    id: "ways",
    kind: "wake",
    title: "Different ways",
    how: "Come back to the phone. Say it each way the screen asks, and huvu says it too.",
    target: 16,
    prompts: ["quickly", "slowly", "quietly", "loudly", "as if you're tired", "as a question", "while you walk", "with the phone at your side"],
    sayPrompts: true,
  },
  {
    id: "noise",
    kind: "wake",
    title: "Over noise",
    how: "Turn on the TV, some music or a fan, as loud as you usually have it. Then say it from where you usually sit.",
    target: 15,
    far: true,
  },
  {
    id: "near_misses",
    kind: "other",
    title: "Close, but not it",
    how: "Turn the noise off and come back to the phone. Read each line once, the way you would say it. These teach huvu what not to wake for.",
    target: 20,
    prompts: [
      "hey hoover",
      "hey google",
      "hey hulu",
      "hey you",
      "who are you",
      "hey buddy",
      "hey human",
      "how are you",
      "hey, who's that",
      "oh boy",
      "hey there",
      "hey Hugo",
      "who knew",
      "hey Bhuvan",
      "hey Siri",
      "hey, move",
      "hey, you two",
      "voodoo",
      "Uber",
      "hey Ruby",
    ],
  },
  {
    id: "talk",
    kind: "other",
    title: "Just talk",
    how: "Talk for two minutes about anything: your day, what's around you. If nothing comes to mind, read the text below aloud. Don't say “hey huvu”.",
    target: 120,
    talk: true,
  },
];

export const TALK_TEXT =
  "The morning was cool, and the street was quiet before the shops opened. A man on a bicycle carried a stack of newspapers, and two dogs followed him to the corner. At the tea stall, the radio played an old song while the water boiled. Someone asked for less sugar, and someone else laughed about the traffic on the main road. By nine, the buses were full, the fruit sellers had set out mangoes and bananas, and the noise of the day had started. In the afternoon it rained for an hour, and the children ran home with their bags over their heads.";
