# huvu

An agent chat app. The core idea: the whole UI is a canvas built with p5.brush. As the agent talks, flowers grow in. The canvas is the last thing to build, since it needs a palette decided first, and it's the most creative part.

Talks to an agent called Hermes on the backend. Single user (the repo owner). Hosted on the web, and installed as an app on phone and Fire TV.

## Platforms and priorities

1. Phone (Android): the daily driver. Needs the most native flexibility: wake word, widgets, background location, fast cold start. This is the platform to optimize hardest for.
1. Web: hosted, full-featured, same codebase as phone.
1. Phone (iOS): minimal, chat only. Built for the owner's mom. Platform limitations here (see wake word below) are accepted, not worked around.
1. Fire TV: canvas plus one giant mic button. Voice only, no chat, no keyboard input. Treated as its own build target.

## Stack decisions and why

1. **Frontend shell: Capacitor, not React Native.** p5.brush only exists as a browser Canvas2D library, and the flower visuals must look identical across web, phone, and Fire TV. Porting p5.brush's stroke/noise rendering to a native engine (react-native-skia) was priced out as a multi-month job with ongoing visual-drift risk, worse than the cost of any Capacitor limitation. Capacitor wraps the same web app for every target, so there's one implementation of the canvas, not several that have to be kept in sync.
1. **Chat state: a small hand-written store, not assistant-ui.** `frontend/src/chat/thread.ts` holds the conversation as turns (one ask and what huvu did with it), since the stage shows turns and never a chat log. assistant-ui was used at first, for its thread runtime only; it was dropped because it was more than half of the startup bundle (602 KB down to 335 KB) and every turn had to be derived from its messages on each change. Edit is rewind: it replaces the turn and everything after it, with no branches kept. The backend call lives in `chat/backend.ts`.
1. **Backend gateway: Node or Bun, not FastAPI.** FastAPI was floated early as an option; the owner picked JS/TS everywhere specifically to avoid a language context-switch when reviewing code. Leaning Bun for native TypeScript execution, not fixed yet. The gateway's job: hold chat/session state, forward messages to Hermes, stream responses back, and (once built) handle auth and location pings.
1. **Auth: JWT, planned, not built.** This is a single-user app. The explicit goal is to lock the backend down to the owner's own email, not to support sign-ups. No free API usage for anyone else.
1. **Background location: coarse, low priority, native plugin required.** Browser `geolocation.watchPosition` gets suspended when the app backgrounds, so this needs a native background-location plugin, not JS. Given the "don't really care, keep it coarse" requirement, use the platform's native significant-location-change mode (large distance filter, low accuracy) rather than Transistorsoft's full paid engine. `@capgo/background-geolocation` is the free option that covers this.
1. **Wake word: Android only, by platform policy, not by choice.** `@capacitor-community/porcupine-wake-word` (Picovoice Porcupine) gives on-device, power-efficient always-listening detection via a foreground service. iOS does not allow arbitrary apps continuous background microphone access the way Android does, this is a hard platform limit, not something to build around. Given the iOS build is chat-only for the owner's mom, this gap doesn't matter there.
1. **Push notifications:** official Capacitor Push Notifications plugin plus `@capacitor-firebase/messaging` for FCM/APNs.
1. **Camera and gallery:** `@capacitor/camera`, native picker, no custom work needed.
1. **Widgets:** genuinely native code on both platforms (SwiftUI WidgetKit on iOS, Glance/RemoteViews on Android), no library replaces this. A Capacitor plugin (e.g. `capacitor-widgetsbridge-plugin` on iOS) only bridges data into a widget extension that still has to be hand-written. Not started.

## The Capacitor ceiling, and where it doesn't apply

Capacitor's WebView layer caps UI fidelity somewhat (cold start costs roughly 100-300ms of engine init, gesture/scroll feel takes deliberate CSS work to match native). This was accepted as the cost of one shared canvas implementation across every target.

On Android specifically, this ceiling does not extend to native capability. A Capacitor plugin compiles into the same APK and runs with the same OS permissions as fully native code, so wake word, widgets, and background services are exactly as capable through a plugin as they'd be in a from-scratch native app. The only real ceiling is the WebView UI surface itself.

Worth knowing if a native rewrite ever gets reconsidered for speed: Chromium's Canvas2D (used by Android's WebView and by Chrome generally) is itself implemented on top of Skia, the same rasterizer Android's native Canvas API uses. So a future native Android port of the flower canvas would be a much smaller, lower-risk job than a generic cross-platform Skia port, since it's largely the same rendering engine accessed a different way, not a different one. Not needed unless real, measured device testing says the WebView canvas isn't smooth enough.

## Chat/voice mode architecture

1. One persistent conversation (the module-level store in `chat/thread.ts`), never recreated when switching between text and voice.
1. Mode (`text` | `voice`) is app state, not a route. Switching mode must never remount the thread or drop the connection, that's what makes it feel seamless.
1. The garden canvas (`GardenCanvas`) stays mounted underneath both modes and every view at all times.
1. The UI over the canvas is a stage, not a chat log (`components/Stage.tsx`). It shows one turn (an ask and what huvu did with it) at a time, large. Scrolling up (D-pad up on Fire TV) folds it into a history of every turn at one size, with chips standing in for images, live feeds and other parts. Picking a row makes it the stage again, and any scroll folds it back. Turns are the store's own model (`chat/thread.ts`): each change replaces only the turn it touches, so a streaming reply re-renders only its own turn.
1. The user's words are always sans serif (Syne), huvu's are Bodoni italic. A sent ask flies from the field up into its YOU cue, and an edited ask flies back down (`stage/flight.ts`). No element is swapped with a hard cut, everything tweens.
1. Edit on a turn is also rewind: the stage hides that turn and everything after it, and sending goes through that message's edit composer, which replaces them.
1. Rich parts (images, tool calls) render through a registry in `stage/parts.ts`: each tool name maps to a chip and a hero view. Unknown tools still get a chip and a one-line note. A live video feed (huvu's browser, a camera, a screen) is one generic part, `video_feed`, whose `src` is a WHEP endpoint: the client POSTs a WebRTC offer there, plays the answer's video in a muted `<video>`, and DELETEs the session's Location to hang up (`stage/useWhepFeed.ts`). WebRTC was picked over MJPEG (a whole image per frame, several times the bandwidth, software decode) and over HLS/DASH (seconds of latency), since a live view has one viewer who wants it now, the way a CCTV live view does. WHEP keeps the part generic: any WHEP server works, including a media server such as mediamtx relaying a camera's RTSP. Only the turn on the stage holds its feed open.
1. Voice mode moves the flower (`components/VoiceFlower.tsx`) to the middle of the screen and hides the text field. On phone it also fades out huvu's answer text above the flower (`voiceAnswerText`), since huvu says it anyway; the ask, rich parts and a stopped/failed note stay. The flower grows with the user's mic level; huvu's voice drives the garland. Hands-free from the flower, push-to-talk by holding space (web) or select (Fire TV). Enter sends on web, phone sends only with its send button. These per-target input rules live in `interaction.ts` next to the mode maps.
1. Speech to text uses the browser's Web Speech API where it exists (desktop Chrome) and a mock of fixed phrases elsewhere (`voice/transcriber.ts`), until a real STT service sits behind the gateway.
1. Audio is latency-first. One `AudioContext` for the whole app (`voice/audio.ts`), resumed inside the gesture that enters voice. One shared mic stream (`voice/mic.ts`) serves the flower's level meter and barge-in, and lingers 8s after its last user so the next listen or reply skips `getUserMedia`; leaving voice mode closes it at once. huvu's voice (`voice/speaker.ts`) is decoded once and cached, preloaded when voice starts. `speak()` resolves when the clip ends or the user talks over it.
1. The garden engine is plain imperative code in `canvas/garden.ts`, outside React; `GardenCanvas` only mounts it. It reads huvu's voice from `speaker.sampleVoice()` each frame, and written replies feed it through `growForWords()`. The loop sleeps once nothing is left to paint or fade, and wakes on either input. p5.brush starts loading at boot, in parallel with React's first render.
1. Motion loads its animation features lazily (`LazyMotion` in `App.tsx`, `motionFeatures.ts`), so components use `m.*` from `motion/react-m`, never `motion.*`. `strict` mode throws if a `motion.*` component slips in.
1. Platform/UI-shape config lives in one file, `frontend/src/config/interaction.ts`: a `Target` (`web` | `phone` | `firetv`) read from a build-time `VITE_TARGET` env var, and one `Profile` per target in a single table (default mode, whether mode is switchable, Enter/send-button rules, push-to-talk key, hint copy, flower size, whether voice mode shows the answer text, garden pixel density). Code reads the resolved profile as `ui`, never indexes by `TARGET`. Web and phone default to text and allow switching; Fire TV is voice-only, no toggle rendered at all.
1. `Target` is intentionally not the same thing as OS platform (`Capacitor.getPlatform()`, `ios` | `android` | `web`). `Target` answers "what does this screen look like" (phone collapses iOS and Android together, since they need the same UI default). OS platform answers "what can this device actually do" (wake word being Android-only is gated on platform, not on `Target`). Keep these two checks separate, don't fold one into the other.
1. `VITE_TARGET` has no default and no fallback. Missing or invalid values throw at runtime (`resolveTarget()` in `interaction.ts`). Every dev/build script names its target explicitly: `dev:web`, `dev:phone`, `dev:firetv`, `build:web`, `build:phone`, `build:firetv`.
1. The phone home-screen widget's quick-voice button is meant to deep-link into the app with a `?mode=voice` query param, read once on launch by `initialMode()`. Not yet wired to a real widget, that part still needs the native widget extension above.

## Repo layout

```
huvu/                       (git root, github.com/fringewidth/huvu, private)
  frontend/                 Vite + React + TypeScript, wrapped by Capacitor
    src/config/interaction.ts       target/mode config, see above
    src/config/palette.ts           every fixed color; index.html, the CSS tokens and the favicon get them through vite.config.ts
    src/chat/                       turn store (thread.ts), backend stream (backend.ts, points at the mock), dev seed
    src/components/                 Stage, TurnView, Composer, VoiceFlower, GardenCanvas
    src/stage/                      rich-part registry, text flight, stage hooks
    src/voice/                      audio context, shared mic, huvu's voice, transcriber, voice session hook
    src/canvas/                     p5.brush garden engine (garden.ts), flower generator, garland, species roster
    src/index.css                   fonts, Tailwind theme, the few keyframes Tailwind can't express
    android/                        generated Capacitor native project
    .env.web / .env.phone / .env.firetv   gitignored, each sets VITE_TARGET
    .env.sample                     tracked, lists all three VITE_TARGET values as a reference
  backend/                  mock server only right now
    server.js               plain Node http, no deps, POST /api/chat streams a fixed lorem ipsum reply word by word, regardless of input; POST /api/feed/demo is a WHEP endpoint streaming a test pattern over WebRTC, through `@roamhq/wrtc` (a native addon, the mock's only dependency)
```

## Known gaps / not yet done

1. Real backend gateway (Node/Bun), Hermes connection, auth, streaming protocol. Only the throwaway mock server exists today. Whatever protocol it uses must carry tool calls with the tool names in `frontend/src/stage/parts.ts` (unknown names fall back to a generic note), and images, into the `answer` and `parts` that `chat/backend.ts` yields. The request body today is `{ messages: [{ role, text }] }`, a placeholder until the gateway defines its own. Live feeds go out as a `video_feed` tool part pointing at a WHEP URL; the WHEP POST is a plain `fetch`, so it can carry the same bearer token as chat. The gateway's media stack is not chosen yet: `@roamhq/wrtc` is a native Node addon and hasn't been tried under Bun, while werift is pure TypeScript.
1. Real speech: STT is Web Speech or a mock (`voice/transcriber.ts`), and huvu's voice is one bundled test clip (`public/audio/test-voice.m4a`).
1. Wake word, background location, push notifications, widgets: all scoped and libraries picked, none implemented.
1. Android Studio and a JDK aren't installed on the dev laptop yet, needed before `npx cap open android` or any on-device Android testing works. `@capacitor/app` (hardware back button) is installed but needs `npx cap sync android` before it exists in the native project.
1. Phone and Fire TV builds have only been exercised in a desktop browser (`dev:phone`, `dev:firetv`), never on hardware. Unverified: the keyboard resizing the phone WebView, Fire TV's select button arriving as Enter, D-pad focus rings at TV distance.
1. Fire TV: `LEANBACK_LAUNCHER` manifest entry not added yet. Mic access (`getUserMedia`) inside a Fire TV WebView hasn't been verified on real hardware, this is a real unknown, not an assumption to build on. The same goes for WebRTC playback of live feeds there.
1. iOS build not attempted yet.
