# Frontend contract

What the frontend expects from the backend. The Python backend (`backend/src/huvu/http.py`) serves it. The backend design is in `backend-architecture.md`.

## Connection

The base URL is hard-coded as `http://localhost:3001` in `frontend/src/net/http.ts`. Every response sets CORS headers for any origin, allows `GET, POST, DELETE, OPTIONS`, `Content-Type` and `Authorization`, and exposes `Location`, since WHEP clients read their session URL from it. Every call except login, pairing, media and `/health` needs the device's token (see [Devices and auth](#devices-and-auth)). The frontend sends it on every call, the push stream included. It sends it on a WHEP request only when the feed's `src` is on the backend's own origin, since a source's own media server has no use for it.

An error answers with a JSON `detail` that is safe to show. Its status says what went wrong: `401` for a missing or dead token, `403` for something this device or user may not do, `404` for something that isn't there, `409` for something already done, `410` for something that ran out of time, and `400` for a request that left out what it needs.

### Chat

`POST /api/chat`

```json
{"id": "t_9f2c41a7b0", "ask": "...", "replaces": "t_0a1b2c3d4e", "actions": [{"text": "opened the live feed balcony", "at": "2026-10-03T13:42:00Z"}]}
```

Every device has one conversation, and the token says which device is asking, so the body names no conversation.

1. `ask` is the only required field.
1. `id` is the turn's own id, minted by the client (`t_` and ten hex digits). The backend keeps it, so a stop or a rewind names the turn by an id the client already has. Left out, the backend picks one. An id already used answers `409`.
1. `replaces` is for edit and retry: the turn it names is replaced, with everything after it. The frontend names the first dropped turn the backend has heard of, since a turn that never reached the backend is not there to replace. An unknown id answers `404`.
1. `actions` is what the user did in the app since the last ask. Today that is opening a live feed: the feed turns after the last real turn (`actionsAfterLastAsk` in `chat/thread.ts`). Opening a feed makes no call of its own. Hermes gets each action as a note in front of the ask, marked as context that needs no reply. The turn stores them, and a rewind replays them in its transcript.
1. `wake` sends the ask to one of the user's wakes (see [Wakes](#wakes)) and not to the device's own conversation. It takes no `replaces`: a wake has no edit or retry (`400`). An ask in a wake that has ended answers `409`.
1. `mode` is how the ask was made: `"voice"` (spoken in voice mode) or `"text"` (typed, and the default). The backend puts one line in front of the message telling the model which, so a spoken answer is written to be heard, with no emojis or markdown (`VOICE_NOTE` and `TEXT_NOTE` in `prompts.py`). Edit and retry send the mode the app is in when they are made.

The answer is Server-Sent Events. Every event's data carries `turn` and `conversation` (the device's id, or the wake's), and `wake` on a wake's turn. A status other than 200 ends the turn as failed, and so does a stream that closes before `turn.ended`.

| Event | Data, besides `turn` and `conversation` |
|---|---|
| `turn.started` | `ask`, `at` |
| `answer.delta` | `text`, appended to the answer |
| `thinking.delta` | `text`, and `start` on the first piece of a burst: the model's reasoning summary while the turn runs. A burst replaces the one before. Never kept with the turn, and only some models send it (`gpt-6-sol` does, `gpt-6-luna` doesn't). It reaches the backend from the huvu Hermes plugin (`POST /api/thinking`, with the profile's MCP token), since Hermes's session stream doesn't carry reasoning |
| `part` | `part`: a whole `RichPart`, added to the turn |
| `part.update` | `id`, `result`: a tool part's result, once the tool finishes |
| `turn.ended` | `ending` (`null`, `"failed"` or `"stopped"`) and the whole `answer` |

A comment line (`: keepalive`) comes every 15 s while nothing else does. Dropping this stream doesn't stop the turn; `POST /api/turns/{turn}/stop` does, for a turn of this device's own conversation, or of one of its user's wakes. A stop asks Hermes to stop, and the stream keeps going until Hermes ends: keep reading it, show what arrives, and take `turn.ended` as the answer to how the turn ended. A turn Hermes finished anyway ends with `ending: null`, not `"stopped"`. A new ask while a turn runs stops that turn the same way, and the new turn starts once it has ended (15 s at most). `GET /api/conversation` returns this device's turns, with `id`, `at`, `ask`, `answer`, `parts`, `actions`, `ending` and `running`, for a client that opens later.

### Wakes

A wake is a model instance a trigger started for its person. Each is a conversation of its own: its first turn has no ask and runs the trigger's note, and its person can send it more. A wake ends when its run ends, unless the model calls `wake_wait`: then it waits for its person, for up to a day. What they send runs in the same Hermes session, on the trigger's tier. Ending it from the app stops what runs in it. A waiting wake is compacted 55 minutes after its last run, before the provider's cache of it expires: it writes notes on where it stands, and the next turn starts a fresh session from them. A group's wake has no person, shows nowhere and can't wait. Shared devices see no wakes.

1. `GET /api/wakes` lists the user's open wakes, running or waiting, newest first: `id`, `trigger`, `note`, `tier`, `status` (`running`, `waiting` or `ended`), `waits_for` (what the model said it needs, while it waits), `started_at`, `ran_at`, `until` (when the wait ends by itself) and `answerable`: whether what the user sends continues it. A wake takes a reply while it waits, and for a day after a run that ended it, unless the user ended it; one the user ended shows as `ended` and isn't answerable.
1. `GET /api/wakes/{id}` answers `{"wake", "turns"}`, the turns as `GET /api/conversation` gives them, for an ended wake too. Another user's answers `404`.
1. `POST /api/wakes/{id}/close` ends it.
1. `wake` on the push stream says one changed: the same fields as the list.
1. A notification a wake sends its own person has `opens: "wake:<id>"`. A tap opens that wake on the stage.

### The browser's screen

Only a session on pro or advanced may use Hermes's browser: a conversation's main session, or a wake on either tier. The browser draws on its profile's own screen. The app watches it, and its person can take it over to click, type and hear it, then hand it back. A turn's `browser_*` tool parts are the browser's steps: the stage shows a run of them as one note, with "watch" on a device that has input of its own.

1. `POST /api/screen/ticket` (a personal device) answers `{"ticket"}`, single use, for 30 s. `503` without Hermes's dashboard, `403` on a shared device.
1. `WebSocket /api/screen?ticket=<ticket>` carries the user's screen as raw RFB, binary frames both ways, for noVNC. It closes with `4401` for a ticket missing, used or expired, `4001` when the screen isn't running (watching never starts it), and `4503` when the dashboard can't be reached.
1. `POST /api/screen/lease` `{"ticket", "take"}` (a personal device) takes the screen over for the view that ticket opened, or hands it back, and answers `{"holding"}`. `404` when that view isn't open or isn't the device user's, `409` when the dashboard refuses. While the person holds it, Hermes's browser tools wait, and the view's input reaches the browser. Closing the view hands it back.
1. `GET /api/screen/audio?ticket=<ticket>` is the browser's sound as an MP3 stream (`audio/mpeg`) while that view holds the screen, for an audio element; the ticket stands in for the token, which an audio element can't send. The request waits up to 10 s for the view to take the screen, so the app can start it inside the tap that takes over; `404` otherwise. It ends when the view hands back or closes.

### Speech

`GET /api/speech` says what the backend can do with sound: `{"listen": bool, "speak": bool}`.

`POST /api/transcribe` takes one recorded ask as the request body, with its `Content-Type` (`audio/webm`, `audio/mp4`, `audio/ogg`), and answers `{"text": "..."}`. 503 means no speech to text is set up, 429 that Groq's rate limit hit both models, and 502 any other Groq failure. The client then falls back to its own recognition where it has one.

`GET /api/turns/{turn}/speech` streams huvu saying a turn's answer, for a turn of this device's own conversation, running or ended, as `application/vnd.huvu.speech`: one MP3 clip per sentence (24 kHz mono), each preceded by its length as four big-endian bytes, sent as huvu writes. The client decodes each clip (`decodeAudioData`) and plays them in order. The stream ends after the last sentence.

`GET /api/turns/{turn}/events` streams one turn of this device's own conversation from its start: everything that happened so far, then live until `turn.ended`. A turn that already ended comes as `turn.started`, its parts and `turn.ended`. A client that opens while a turn runs uses this, since a delta doesn't say where its text goes and can't be added to a snapshot of the answer. The frontend also uses it for a turn the push stream mentions that it has no stream of.

The backend builds parts from Hermes's stream:

1. **Hermes's own tools.** `web_search` gets `args.query`; its result stays empty, since Hermes sends only a 500-character preview and a source count from it would be wrong. Any other Hermes tool becomes a part with empty args, which the stage shows as a generic note. Hermes's housekeeping (memory, skills, session search) never shows.
1. **The model's `ui_*` tools.** `ui_choices` becomes a `choices` part, `ui_show_feed` a `video_feed` part with the feed's `src`, `label` and a `caption`, `ui_show_image` an image part with its `caption`, `ui_youtube` a `youtube` part with the video's `id`, `title`, `channel` and `thumbnail`, which the app hands off to YouTube on a tap, and `ui_learn_voice` a `learn_voice` part with no args, which opens the recording screen as it arrives in a running turn, and again on a tap.
1. **`MEDIA:<path>` in the answer.** It is Hermes's own way to attach a file, and its tools tell the model to use it, such as the browser's screenshot. The relay takes each tag out of the answer text and puts an image part where it was. A tag split across deltas is held back until it is whole. A tag whose file is missing or isn't an image stays as text.
1. **huvu's actions** show as notes, such as `home_turn_on`. huvu's reads don't show.


### Feeds

`GET /api/feeds` returns every feed that could stream right now. Listing a feed opens nothing.

```json
[{"id": "doorbell", "label": "doorbell", "kind": "camera", "online": true, "src": "http://localhost:3001/api/feed/doorbell"}]
```

1. `src` is the feed's WHEP endpoint, absolute, since the page is served from another origin.
1. An offline feed stays listed with `online: false`, so huvu can say the camera is down.
1. The frontend fetches the list at boot and each time the list opens. A failed fetch keeps the last list. Entries without a string `id` or `src` are dropped.

The doorbell will leave this list, since the real doorbell is a snapshot per press (`front-door-lock.md`).

### WHEP

1. `POST <src>` with `Content-Type: application/sdp` and the client's SDP offer, with every ICE candidate already gathered.
1. The answer is `201`, with `Content-Type: application/sdp`, the SDP answer as the body, and `Location` naming the session. Anything else counts as a failed feed.
1. `DELETE <Location>` hangs up. The server also closes a session when ICE fails, for clients that vanish.

### Parts

The frontend renders a turn as an `answer` string plus `parts` (`RichPart` in `chat/thread.ts`):

```ts
type RichPart =
  | { kind: "image"; src: string; caption?: string; after?: number }
  | { kind: "tool"; id: string; toolName: string; args: Record<string, unknown>; result: unknown; after?: number };
```

`after` is how much of the answer huvu had said when the part came, counted without leading whitespace. The stage cuts the answer there and shows the part's note between the two pieces, so the words and the tools show in the order they happened. A part without it goes before the answer.

Tool names the stage knows (`frontend/src/stage/parts.ts`):

| `toolName` | Reads | Shows |
|---|---|---|
| `web_search` | `args.query`, `result.sources` (an array, only its length is used) | A note in the answer, where huvu searched |
| `video_feed` | `args.src` (WHEP URL), `args.label`, `args.caption` | A live panel below the answer |
| `choices` | `args.options` (array of strings) | Buttons, last. Picking one sends its text as a new ask, on Fire TV too (D-pad and select) |

Any other tool name gets a chip and a "used <name>" note, so a new tool never breaks the stage. Images sit in the panel slot.

Only the dev seed (`?seed` in a dev build) shows parts without a model sending them.

### Push

`GET /api/push` is one long-lived SSE stream per open client. It carries only what is addressed to the client's device, and nothing from another device's conversation.

| Event | To | Data |
|---|---|---|
| The turn events above | The device whose conversation it is, or every personal device of a wake's person | As above. A client gets its own turns twice and keeps one by turn id. A turn the device didn't start itself (an Alexa ask shown on the TV, a wake at work) arrives only here. |
| `wake` | Every personal device of the wake's person | A wake started, waits, runs again or ended, with the fields of `GET /api/wakes` |
| `notify` | The phones of the user it is for, or the group members' phones for a shared device | `id`, `at`, `text`, `image`, `urgency`, `opens`, `user`, `group`, `from`. `opens` names the app screen that deals with it: `"triggers"` for a trigger the wake limit paused, or for triggers that fire a lot, and `"wake:<id>"` from a wake to its own person. `from` is set on a message one person sent another through huvu: the sender, as the recipient calls them (such as `"mom"`). It is null when huvu sent it. A message to a group never reaches its sender |
| `approval` | The same phones | A pending or resolved approval |
| `identify` | The phones of the person asked | `id`, `device` (`id`, `name`), `user`, `asked_by` (`device`, `model`, `voice` or `trigger`), `reason`, `asked_at`, `expires_at` |
| `identify.done` | The same phones | `id`, `status` (`confirmed`, `denied`, `expired`, or `withdrawn` when someone else said yes first) |
| `attendance` | The shared device, and the phones of the person recognized | `device` (`id`, `name`), and `user`: `{id, name}` once someone is recognized, `null` when that ends |

A device that isn't connected misses these, and the stream replays nothing. So a client catches up each time the stream connects: the conversation, approvals, questions, attendance and notifications.

### Native push

`POST /api/push/token` (phone) with `{"token", "platform"}` keeps this phone's FCM token. `platform` is `android` or `ios`. A device has one token, and logging the device out drops it. A `notify`, a pending `approval` and an `identify` also go to the token as an FCM notification, whether or not the app is open, since a backgrounded app can still hold the stream open for a while. The OS shows the app in front no notification, so it hears these on the stream only. The FCM message carries `data` with `kind` (the push event's name), `id`, and `opens` when the notice has one. On Android it uses the channel `huvu.urgent` for approvals, questions and urgent notices, and `huvu` for the rest; the app creates both. The backend sends these only when `HUVU_FCM_CREDENTIALS` names a Firebase service account key.

### Web Push

huvu added to an iPhone's home screen logs in as a `phone` and takes Web Push in place of FCM. `GET /api/push/web` (phone) answers `{"key"}`, the VAPID public key in base64url, which the browser subscribes with. `POST /api/push/web` (phone) with the subscription's `toJSON()` (`{"endpoint", "keys": {"p256dh", "auth"}}`) keeps it as this device's one token, in place of an FCM token; logging the device out drops it. The same events go to it, encrypted (aes128gcm) and signed with VAPID, as JSON: `title`, `body`, `kind`, `id`, and `image` and `opens` when the notice has them. Approvals, questions and urgent notices are sent with `Urgency: high`. The service worker (`public/sw.js`) shows each push, and closes it at once while huvu is in front, since iOS drops a subscription whose pushes show nothing. A push service that answers 404 or 410 has dropped the subscription, and the backend forgets it. Web Push needs no setup: the backend makes its VAPID key on first start, at `vapid.pem` in its data directory.

### Live updates

`GET /api/app/{target}` (no token) answers `{"version", "native"}` for a native target's latest web build (`phone` today), with `Cache-Control: no-store`, or 404 when the host has none. `GET /api/app/{target}/{version}.zip` is that build as a zip, `index.html` at its root, cached for good, since a version never changes. Only the current version is served. The app downloads it when `version` differs from its own `bundle.json` and `native` equals its APK's version, and switches to it the next time it goes to the background.

### Approvals and actions

Approvals are answered from phones only. An approval belongs to the user it was asked for, or, with `user` `null`, to the members of its `group`: when a shared device nobody is recognized at asked, or when a guest caused it. A guest never approves anything.

`GET /api/approvals` (phone) lists what this phone may answer: its user's pending approvals and those of the groups they're a member of. Each has `id`, `action` (`source.action`), `label` (what the action does, in words, from the driver's description), `args`, `reason`, `requester` (who asked, in words: "huvu, talking with Me on laptop", "a trigger: Turn on the porch light.", "someone on living room"), `asked_by` (the raw caller), `user`, `group`, `status`, `created_at`, `deadline`. `POST /api/approvals/{id}` (phone) with `{"approve": true}` or `false` answers one; the answer runs the action through the backend, and the result comes back. This is the only way to approve, so no model can approve its own request. Another user's approval, or any approval from a device that isn't a phone, answers `403`. One already answered, by a second tap or another phone, answers `409`. An action that needs approval while the same one already waits comes back `{"status": "already_pending"}`, with no approval of its own.

`POST /api/actions/{source}/{action}` with the action's args is a tap in the app. On a member's personal device (phone, web) the tap is the approval, so it runs at once. On a shared device, or from a guest, it goes through the policy like a model's request, so a `requires_human` action waits for a member's phone.

### Notifications

`GET /api/notifications` (phone) lists the notifications of the last 3 days sent to this phone's user, or to the groups they're a member of, oldest first, in the shape of the `notify` push. A phone that wasn't connected finds what it missed here.

### Triggers

`GET /api/triggers` lists the triggers of this device's user (or of whoever is recognized at a shared device): `id`, `note`, `sql`, `fire` (`edge` or `rows`), `once`, `status` (`active`, `unconfirmed`, `awaiting`, `paused`), `tier` (`basic`, `pro` or `advanced`), `paused_by` (`model`, `user` or `limit`), `last_fired_at`, `fire_count` and more. `skip` is how many of its next fires it skips. `POST /api/triggers/{id}/pause` and `POST /api/triggers/{id}/resume` control one, and `POST /api/triggers/{id}/skip` and `POST /api/triggers/{id}/unskip` set it to skip its next fire, or none. A trigger a limit paused (`paused_by: "limit"`: it fired or woke huvu too often) resumes only this way, by a person's tap; the model can't. `POST /api/triggers/{id}/approve` says yes to an `awaiting` trigger: on the person's own device it goes live at once and the trigger comes back; elsewhere it opens an approval like any other.

### The wiki

The agent's memory is a wiki of Markdown pages that the agent writes and organizes itself; the app reads it and lets its person correct it. Only a personal device has one: a shared device gets 403. `GET /api/wiki` lists `pages` (`path`, `title`, `modified`, `size`, newest first) and the `index`, the entries of the profile's `MEMORY.md`, in the agent's order. `GET /api/wiki/page?path=` returns `path`, `text`, `version` (a hash of the text) and `modified`. `PUT /api/wiki/page` with `path`, `text` and the `version` the edit started from saves it; `version: null` makes a new page. Either answers 409 when the page changed since (the agent wrote it meanwhile, or a new page's path is taken), and writes nothing. `DELETE /api/wiki/page?path=&version=` removes one, with the same check. A path is relative, ends in `.md`, and stays inside the wiki; anything else is 400.

### Voice clips

Each person records how they say "hey huvu", for training the wake word (`voice_clips.py`, `docs/wake-word.md`). Only a personal device records: a shared device gets 403. `GET /api/voice-clips` answers `{"steps": {"<step>": n}}`, the clips kept so far by step, so the screen picks up where the person left off. `POST /api/voice-clips/{kind}/{step}` with a WAV body (16 kHz, mono, 16-bit, 0.2 to 30 s) keeps one and answers `{"id"}`; `kind` is `wake` (the phrase) or `other` (what must not wake huvu). Anything else is 400. `DELETE /api/voice-clips/{id}` drops one of the caller's own, or answers 404.

### Triggers on a shared device

A trigger belongs to a person. One set up on a shared device with nobody recognized waits, and everyone in the device's group, members and guests, gets an `identify` question with `asked_by: "trigger"` and the trigger in `reason`. The first yes recognizes that person at the device (see below) and makes the device's waiting triggers theirs. The other questions are `withdrawn`. If everyone says no, or nobody answers within 5 minutes, the waiting triggers are dropped.

### Feeds and media

`GET /api/feeds` lists every source's feeds, in the shape above. Images get URLs as `/api/media/<token>`, such as a decrypted doorbell snapshot or an image file on the host's disk. `<img>` tags can't send a bearer header, so the unguessable token is the permission, and these URLs need no other auth. The model names an image by a source's ref, an absolute path or an https URL (`media.py`). Any path can be named, so a file is served only while its first bytes say it is a JPEG, PNG, GIF, WebP, AVIF or BMP, up to 20 MB; SVG is refused, since opened on its own it runs scripts on huvu's origin. An https URL goes into the part as it is, so the device loads it itself, and plain http is refused.

### Who is at a shared device

A shared device can recognize the person in front of it, if they're in its group (member or guest). The Hermes session stays the group's, so nothing about the conversation changes, but from then on the backend treats it as that person's: their triggers, their approvals and notifications on their phone, and their rows in the history (chat turns, huvu's log), which their own dream reads.

1. `GET /api/people` lists the people in the device's group (`id`, `name`), for a "who's this?" picker.
1. `POST /api/attendance` with `{"user"}`, from a shared device, asks that person on their phones. Asking again before they answer changes nothing and answers `409`. The model can ask the same way, and later voice recognition will.
1. `GET /api/identify` (phone) lists the open questions for this phone's user. `POST /api/identify/{id}` (phone) with `{"yes": true}` or `false` answers one. Questions last 5 minutes. A question that is gone answers `404`, one answered already `409`, and one that ran out `410`.
1. `GET /api/attendance` answers `{"user"}`: who is recognized at this device, or the device's own user on a personal device.
1. `GET /api/attendance/mine` lists the shared devices this device's user is recognized at (`device`, `since`), so their phone can end it from afar.
1. `DELETE /api/attendance/{device}`, from the device or the recognized person's phone, ends it. So does someone else being confirmed, the model being told they left, or 10 minutes with no turn.

### Devices and auth

Every request carries its device's token as `Authorization: Bearer <token>`. Streams also take it as `?token=`, for clients that can't set headers on one. A device is personal (`phone`, `web`) or shared (`tv`, `speaker`). The device list can also hold a `tracker`: OwnTracks on someone's phone, added from the backend's command line. Its token works only on `POST /api/ingest/{source}` and answers `403` everywhere else. It can be logged out like any device. A shared device belongs to a group: a set of people who share devices, as members (who trust each other) or guests (who trust nothing). The household is the default group, `home`. Phones matter most: approvals, "is it you?" questions and notifications go to them, since they are taken to be on their user's person.

1. **Personal devices log in.** `POST /api/login` with `{"username", "password", "device": {"name", "kind"}}` answers `{"token", "device"}`. A wrong password answers `401`, and so does a shared kind. After 10 failed logins from one address within 15 minutes, that address gets `429` until the oldest leaves the window, even with the right password; a login that works clears its count. Behind the tunnel the address is Cloudflare's `CF-Connecting-IP`.
1. **Shared devices pair.** `POST /api/pair` with `{"name", "kind"}` answers `{"pairing", "code", "expires_at"}`. The device shows `code` (`ABCD-EF23`) and polls `GET /api/pair/{pairing}`: `202` while waiting, then `200` with `{"status": "paired", "token", "device"}` once, then `410`. A member confirms from their phone with `POST /api/pair/confirm` and `{"code", "group"}`. `group` is needed only if they're a member of more than one; the device joins it. A code no device shows answers `404`, and a missing `group` answers `400`. A guest can't pair. Codes last 10 minutes.
1. `GET /api/me` answers `{"device", "user", "groups"}`. `user` is `null` on a shared device, whose `device.group` names its group. `groups` maps each of the user's groups to their role there.
1. `GET /api/devices` lists the user's devices and the shared devices of their member groups. `DELETE /api/devices/{id}` logs one out: one's own, or a shared device of a group one is a member of. A shared device can only unpair itself.

Users are added from the backend's command line (`uv run huvu user add`), not from the app. With `auth.mode = "off"`, a request without a valid token comes from a development phone owned by `auth.dev_user`, which is only for localhost.

### Not built

1. The Fire TV launch over ADB, and how an Alexa ask reaches the backend.
1. Live feeds served by the backend itself. Feeds come from sources, each with its own WHEP endpoint.
