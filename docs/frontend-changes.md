# Frontend changes to make

The frontend caught up with the backend on 2026-10-05. This file lists what is still left. The contract is in `frontend-contract.md`.

## Done on 2026-10-05

1. **Devices and auth.** Login on phone and web (`components/Login.tsx`), pairing on Fire TV (`components/Pairing.tsx`), and pairing a shared device from the phone's menu. The token is kept in secure storage and sent on every call (`net/http.ts`). A `401` sends the device back to login or pairing. `VITE_TARGET` decides the device kind (`deviceKind` in `config/interaction.ts`). With `auth.mode = "off"`, the app finds that the backend answers without a token and skips login.
1. **Conversation.** Each turn has its own stream. Stop asks the backend and keeps reading until `turn.ended`. A new ask doesn't end the turn before it, because the backend stops that one. On launch, and each time the push stream connects, the store catches up from `GET /api/conversation`. A running turn is rebuilt from `GET /api/turns/{turn}/events`. At launch the stage shows the greeting, and the earlier turns wait in history.
1. **Push.** `account/push.ts` keeps `GET /api/push` open as a fetch, with backoff, and tries again at once when the app comes back to the front. Turn events for a turn the client has no stream of start a follow from the turn's start.
1. **Notifications.** A `notify` becomes a turn with no ask, a notice (`Turn.notice`). A notice that arrives while a reply streams goes in under the reply, so the reply keeps the stage. Missed ones come from `GET /api/notifications`. One sent while the app was closed and not seen since takes the stage at launch. A notice with `opens: "triggers"` has a button that opens the menu at the triggers.
1. **Approvals and "is it you?"** One card at a time, pinned above the composer on phones (`components/Waiting.tsx`), with a countdown, and with the action's `label`, `reason` and `requester`. An answer that comes back `409` or `410` goes quietly.
1. **The menu** under the wordmark on web and phone (`components/Menu.tsx`, `MenuPanel.tsx`): who you are, where you're recognized (with "not there"), log out, triggers with pause and resume, devices with log out, and pairing a device. A mark by the wordmark says a trigger the wake limit paused waits for a tap.
1. **Who is at the TV.** The Menu list on Fire TV has a "who's here" section next to the feeds. The wordmark says who is recognized, or whose phone to check.

## Done on 2026-10-06

1. **Native push.** `account/nativePush.ts` asks for permission on a native phone, creates the two Android channels, and registers the FCM token with `POST /api/push/token`, again whenever FCM gives a new one. The backend sends notifications, pending approvals and "is it you?" questions to it (`backend/src/huvu/native_push.py`). Only `@capacitor-firebase/messaging` is used, without the official Push Notifications plugin: its README presents the two as alternatives, and each registers its own FCM service on Android. The plugin loads lazily, so web and the TV never fetch it. Back in front, the app clears the tray, since it shows everything itself. A tap opens the app, where approvals and questions wait in their card and an unseen notice takes the stage. A notice with `opens: "triggers"` opens the menu there.
1. **Secure token storage.** The device token is kept with `@aparajita/capacitor-secure-storage`: the Android Keystore, the iOS Keychain, and `localStorage` on web. A browser logged in before this has to log in once more. `@capacitor/preferences` stays, for the notices-seen mark, which is no secret.
1. **`npx cap sync android`** ran, so the Android project has all four plugins. It needs no JDK.

## Still to do

1. **A Firebase project.** Create it, add the Android app `com.huvu.app`, and put its `google-services.json` in `frontend/android/app/`; the Gradle build applies the plugin once the file is there. Then create a service account key with the Firebase Cloud Messaging API enabled, and set `HUVU_FCM_CREDENTIALS` to its path for the backend. iOS also needs `GoogleService-Info.plist` and the APNs key uploaded to Firebase.
1. **Taps that wait.** Nothing in the app calls `POST /api/actions/{source}/{action}` yet. Once something does, the TV's taps go through the policy, even while someone is recognized at it. An answer of `pending_approval` on a device that isn't a phone (the TV, a laptop) or from a guest should say it waits on a member's phone, and `already_pending` should say the same approval already waits.
