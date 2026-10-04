# Frontend changes to make

The backend has moved ahead of the frontend. This is the list of what the frontend needs, to be done in one pass at the end. The contract is in `frontend-contract.md`.

Today the frontend calls only `POST /api/chat`, `POST /api/turns/{id}/stop` and `GET /api/feeds`, sends no token, and opens no push stream. It works only against a backend with `auth.mode = "off"`.

## Devices and auth

1. **Login screen** for phone and web: username, password, and a device name (default it from the platform). Keep the token in secure storage (Capacitor Preferences at least, or a secure-storage plugin), not in plain `localStorage` on the phone.
1. **Send the token everywhere.** `Authorization: Bearer` on every `fetch` in `chat/backend.ts` and `chat/feeds.ts`, and on the WHEP `POST` and `DELETE` in `stage/useWhepFeed.ts`. Streams that can't set headers use `?token=`.
1. **A `401` sends the user back to login (or pairing)**, since it means the device was logged out.
1. **Pairing screen** for Fire TV and later the speaker hardware. On first launch, call `POST /api/pair`, show the code large, and poll `GET /api/pair/{pairing}` every 2 s. On `410`, start again.
1. **Pair-a-device screen** on the phone: enter the code a TV shows (`POST /api/pair/confirm`). Only phones may do this.
1. **Pairing picks a group** when the member confirming is in more than one (`groups` from `GET /api/me`, sent as `group` to `POST /api/pair/confirm`).
1. **Devices screen**: the user's devices and their groups' shared devices (`GET /api/devices`), each with a log-out (`DELETE /api/devices/{id}`).
1. **Guests** (`role: "guest"` in `GET /api/me`'s `groups`) get no approvals and no pairing. Hide those screens for a user who is a guest everywhere, and say a tap on something that needs approval went to the group's members.
1. `VITE_TARGET` decides the device kind it reports: `phone` for phone, `web` for web, `tv` for firetv.

## Conversation

1. **One conversation per device.** The chat body has no `conversation` field anymore. The backend ignores it, so dropping it is cleanup only.
1. **Restore on launch** from `GET /api/conversation` (this device's turns), if the stage should come back after a restart. Today the store starts empty.
1. **Drop "the phone asks, the TV shows."** No turn reaches another device. Nothing in the frontend depends on it yet, but `CLAUDE.md` and any TV plans that assume it need to change.

## Push

1. **Open `GET /api/push?token=...`** on launch and keep it open, with reconnect and backoff. Nothing opens it today.
1. **Turn events** arrive for this device's own conversation. Keep one copy by turn id, since a turn the device started also comes back over its own chat stream. A turn the device didn't start (a future Alexa ask on the TV) arrives only here, so the store needs a way to take in a turn it didn't mint.
1. **`notify`**: show it in the app. Phones only.
1. **`approval`** and **`identify`** / **`identify.done`**: see below.

## Notifications (not built at all)

1. **In-app**: a banner or toast for `notify`, with its image (`/api/media/...`) and urgency.
1. **Native push** so a phone hears about approvals, "is it you?" questions and notifications while the app is closed. Wire up the official Capacitor Push Notifications plugin plus `@capacitor-firebase/messaging` (FCM on Android, APNs on iOS). This also needs backend work that doesn't exist yet: an endpoint to register a phone's push token, and a sender that uses it when the phone has no open push stream.
1. A tap on a notification should open the app on the right screen: the approval, the question, or the stage.

## Approvals (phones only)

1. **An approvals screen or sheet**: `GET /api/approvals`, and live updates from `approval` pushes. Show the action, `reason`, who asked (`asked_by`: the user, the device, or the trigger), and a countdown to `deadline`.
1. **Approve or deny** with `POST /api/approvals/{id}`. `403` means another user's approval, or not a phone.
1. **A group's approval** (`user: null`, with `group`) shows on every member's phone. The first answer wins, so the others should drop it when the resolved `approval` push arrives.
1. **On a non-phone device**, an action that comes back `pending_approval` (a tap on the TV, a chat on the laptop) should say it is waiting on a phone.

## Triggers set up on a shared device

No screen of their own. A trigger set up on a shared device with nobody recognized asks everyone in the group "is this you?", through the same phone prompt as below (`identify`, with `asked_by: "trigger"` and a `reason` such as "setting up: Turn on the porch light."). Show the `reason` in the prompt. A `withdrawn` status in `identify.done` means someone else said yes first.

## Who is at a shared device

A device without a user (the TV, a speaker) can recognize the person at it. The person confirms on their phone, and the device's session then acts and logs as them. See "Who is at a shared device" in `frontend-contract.md`.

1. **Show who is recognized.** On a shared device, show the recognized person clearly: name, and a way to tell it's them, not a guess. Read `GET /api/attendance` on launch and follow `attendance` pushes. Show nothing extra when nobody is recognized.
1. **"Who's this?" picker** on shared devices: the family from `GET /api/people`, and picking one calls `POST /api/attendance`. After that, say "check your phone" until the `attendance` push arrives or 5 minutes pass. On Fire TV this is a D-pad list, reachable like the feeds list.
1. **"Not me" / end it**: `DELETE /api/attendance/{device}` from the device itself.
1. **Phone prompt** on an `identify` push: "Is it you on the living room TV?" with yes and no (`POST /api/identify/{id}`). Dismiss on `identify.done`. Check `GET /api/identify` on launch for questions that arrived while closed. This is one more reason for native push notifications.
1. **Phone: end it from afar**: while you're recognized somewhere, offer "I'm not at the living room TV anymore" (`DELETE /api/attendance/{device}`). Needs the phone to know where it's recognized, which no endpoint lists yet; add one when building this.
1. The model can ask too (`people_identify`), so the phone prompt can arrive without anyone touching the TV.

## Fire TV

1. Pairing replaces login (see above). The TV never answers approvals or "is it you?" questions, and it can still send a choice as an ask.
1. While someone is recognized, its taps still go through the policy, and approvals go to that person's phone.
1. Its taps on actions go through the policy, so expect `pending_approval` back for anything that needs a human.
