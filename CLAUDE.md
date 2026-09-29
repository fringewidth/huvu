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
1. **Chat UI: assistant-ui** (`@assistant-ui/react`). Headless primitives (`ThreadPrimitive`, `MessagePrimitive`, `ComposerPrimitive`), ships through a shadcn-style CLI so the component source lands in the repo rather than staying a black-box dependency. Current UI is intentionally generic and unstyled, real styling waits for the canvas palette.
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

1. One persistent chat session per conversation (`useLocalRuntime` from assistant-ui), not recreated when switching between text and voice.
1. Mode (`text` | `voice`) is app state, not a route. Switching mode must never remount the thread or drop the connection, that's what makes it feel seamless.
1. The canvas is meant to stay mounted underneath both modes at all times (not yet built).
1. Platform/UI-shape config lives in one file, `frontend/src/config/interaction.ts`: a `Target` (`web` | `phone` | `firetv`) read from a build-time `VITE_TARGET` env var, with `defaultMode` and `modeSwitchable` maps per target. Web and phone default to text and allow switching; Fire TV is voice-only, no toggle rendered at all.
1. `Target` is intentionally not the same thing as OS platform (`Capacitor.getPlatform()`, `ios` | `android` | `web`). `Target` answers "what does this screen look like" (phone collapses iOS and Android together, since they need the same UI default). OS platform answers "what can this device actually do" (wake word being Android-only is gated on platform, not on `Target`). Keep these two checks separate, don't fold one into the other.
1. `VITE_TARGET` has no default and no fallback. Missing or invalid values throw at runtime (`resolveTarget()` in `interaction.ts`). Every dev/build script names its target explicitly: `dev:web`, `dev:phone`, `dev:firetv`, `build:web`, `build:phone`, `build:firetv`.
1. The phone home-screen widget's quick-voice button is meant to deep-link into the app with a `?mode=voice` query param, read once on launch by `initialMode()`. Not yet wired to a real widget, that part still needs the native widget extension above.

## Repo layout

```
huvu/                       (git root, github.com/fringewidth/huvu, private)
  frontend/                 Vite + React + TypeScript, wrapped by Capacitor
    src/config/interaction.ts       target/mode config, see above
    src/chatRuntime.ts              ChatModelAdapter, currently points at the mock backend
    src/components/                 Chat, Message, VoiceOrb (placeholder), ModeToggle
    src/styles/                     one CSS file per component, co-located here not next to the component
    android/                        generated Capacitor native project
    .env.web / .env.phone / .env.firetv   gitignored, each sets VITE_TARGET
    .env.sample                     tracked, lists all three VITE_TARGET values as a reference
  backend/                  mock server only right now
    server.js               plain Node http, no deps, POST /api/chat always streams back "huvu huvu huvu" regardless of input
```

## Known gaps / not yet done

1. The canvas and flower-growth visuals, deliberately last.
1. Real backend gateway (Node/Bun), Hermes connection, auth, streaming protocol. Only the throwaway mock server exists today.
1. Wake word, background location, push notifications, widgets: all scoped and libraries picked, none implemented.
1. Android Studio and a JDK aren't installed on the dev laptop yet, needed before `npx cap open android` or any on-device Android testing works.
1. Fire TV: `LEANBACK_LAUNCHER` manifest entry and D-pad focus handling not added yet. Mic access (`getUserMedia`) inside a Fire TV WebView hasn't been verified on real hardware, this is a real unknown, not an assumption to build on.
1. iOS build not attempted yet.
