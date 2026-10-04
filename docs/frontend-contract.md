# Frontend contract

What the frontend expects from the backend. The Python backend (`backend/src/huvu/http.py`) serves it. The backend design is in `backend-architecture.md`.

## Connection

The base URL is hard-coded as `http://localhost:3001` in `frontend/src/chat/backend.ts`. Every response sets CORS headers for any origin, allows `GET, POST, DELETE, OPTIONS`, `Content-Type` and `Authorization`, and exposes `Location`, since WHEP clients read their session URL from it. Every call except login, pairing, media and `/health` needs the device's token (see [Devices and auth](#devices-and-auth)). The frontend sends none yet, so today it only works with `auth.mode = "off"`.

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

The answer is Server-Sent Events. Every event's data carries `turn` and `conversation` (the device's id). A status other than 200 ends the turn as failed, and so does a stream that closes before `turn.ended`.

| Event | Data, besides `turn` and `conversation` |
|---|---|
| `turn.started` | `ask`, `at` |
| `answer.delta` | `text`, appended to the answer |
| `part` | `part`: a whole `RichPart`, added to the turn |
| `part.update` | `id`, `result`: a tool part's result, once the tool finishes |
| `turn.ended` | `ending` (`null`, `"failed"` or `"stopped"`) and the whole `answer` |

A comment line (`: keepalive`) comes every 15 s while nothing else does. Dropping this stream doesn't stop the turn; `POST /api/turns/{turn}/stop` does, for a turn of this device's own conversation. `GET /api/conversation` returns this device's turns, with `id`, `at`, `ask`, `answer`, `parts`, `actions`, `ending` and `running`, for a client that opens later.

The backend builds parts from Hermes's stream:

1. **Hermes's own tools.** `web_search` gets `args.query`; its result stays empty, since Hermes sends only a 500-character preview and a source count from it would be wrong. Any other Hermes tool becomes a part with empty args, which the stage shows as a generic note. Hermes's housekeeping (memory, skills, session search) never shows.
1. **The model's `ui_*` tools.** `ui_choices` becomes a `choices` part, `ui_show_feed` a `video_feed` part with the feed's `src`, `label` and a `caption`, and `ui_show_image` an image part.
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
  | { kind: "image"; src: string }
  | { kind: "tool"; id: string; toolName: string; args: Record<string, unknown>; result: unknown };
```

Tool names the stage knows (`frontend/src/stage/parts.ts`):

| `toolName` | Reads | Shows |
|---|---|---|
| `web_search` | `args.query`, `result.sources` (an array, only its length is used) | A note above the answer |
| `video_feed` | `args.src` (WHEP URL), `args.label`, `args.caption` | A live panel below the answer |
| `choices` | `args.options` (array of strings) | Buttons, last. Picking one sends its text as a new ask, on Fire TV too (D-pad and select) |

Any other tool name gets a chip and a "used <name>" note, so a new tool never breaks the stage. Images sit in the panel slot.

Only the dev seed (`?seed` in a dev build) shows parts without a model sending them.

### Push

`GET /api/push` is one long-lived SSE stream per open client. It carries only what is addressed to the client's device, and nothing from another device's conversation.

| Event | To | Data |
|---|---|---|
| The turn events above | The device whose conversation it is | As above. A client gets its own turns twice and keeps one by turn id. A turn the device didn't start itself (a future Alexa ask shown on the TV) arrives only here. |
| `notify` | The phones of the user it is for, or every user's phones for the household | `text`, `image`, `urgency`, `user` |
| `approval` | The same phones | A pending or resolved approval |
| `claim` | Every user's phones | `trigger`, `note`, `choices` (three two-digit numbers), `expires_at` |
| `claim.done` | Every user's phones | `trigger`, `user` (who claimed it, or `null` if it was dropped or expired) |
| `identify` | The phones of the person asked | `id`, `device` (`id`, `name`), `user`, `asked_by` (`device`, `model` or `voice`), `asked_at`, `expires_at` |
| `identify.done` | The same phones | `id`, `status` (`confirmed`, `denied` or `expired`) |
| `attendance` | The shared device | `user`: `{id, name}` once someone is recognized, `null` when that ends |

A device that isn't connected misses these. Native push notifications are not built.

### Approvals and actions

Approvals are answered from phones only. An approval belongs to the user it was asked for, or to nobody when a household device asked.

`GET /api/approvals` (phone) lists what this phone may answer: its user's pending approvals and the household's. Each has `id`, `action`, `args`, `reason`, `asked_by`, `user`, `status`, `created_at`, `deadline`. `POST /api/approvals/{id}` (phone) with `{"approve": true}` or `false` answers one; the answer runs the action through the backend, and the result comes back. This is the only way to approve, so no model can approve its own request. Another user's approval, or any approval from a device that isn't a phone, answers `403`.

`POST /api/actions/{source}/{action}` with the action's args is a tap in the app. On a personal device (phone, web) the tap is the approval, so it runs at once. On a household device it goes through the policy like a model's request, so a `requires_human` action waits for someone's phone.

### Trigger claims

A trigger set up from a household device has no user, so it waits to be claimed. The model tells the room a two-digit number, and every phone gets a `claim` push. `GET /api/claims` (phone) lists the open ones. `POST /api/claims/{trigger}` (phone) with `{"number": 42}` claims it for the phone's user. A wrong number drops the trigger and answers `409`. An unclaimed trigger expires after 10 minutes.

### Feeds and media

`GET /api/feeds` lists every source's feeds, in the shape above. Images get URLs as `/api/media/<token>`, such as a decrypted doorbell snapshot. `<img>` tags can't send a bearer header, so the unguessable token is the permission, and these URLs need no other auth.

### Who is at a shared device

A device without a user can recognize the person in front of it. The Hermes session stays the household's, so nothing about the conversation changes, but from then on the backend treats it as that person's: their triggers, their approvals and notifications on their phone, and their entries in the event log, which their own dream reads.

1. `GET /api/people` lists the family (`id`, `name`), for a "who's this?" picker.
1. `POST /api/attendance` with `{"user"}`, from a device without a user, asks that person on their phones. The model can ask the same way, and later voice recognition will.
1. `GET /api/identify` (phone) lists the open questions for this phone's user. `POST /api/identify/{id}` (phone) with `{"yes": true}` or `false` answers one. Questions last 5 minutes.
1. `GET /api/attendance` answers `{"user"}`: who is recognized at this device, or the device's own user on a personal device.
1. `DELETE /api/attendance/{device}`, from the device or the recognized person's phone, ends it. So does someone else being confirmed, the model being told they left, or 10 minutes with no turn.

### Devices and auth

Every request carries its device's token as `Authorization: Bearer <token>`. Streams also take it as `?token=`, for clients that can't set headers on one. A device is personal (`phone`, `web`) or household (`tv`, `speaker`). Phones matter most: approvals, claims and notifications go to them, since they are taken to be on their user's person.

1. **Personal devices log in.** `POST /api/login` with `{"username", "password", "device": {"name", "kind"}}` answers `{"token", "device"}`. A wrong password answers `401`, and so does a household kind.
1. **Household devices pair.** `POST /api/pair` with `{"name", "kind"}` answers `{"pairing", "code", "expires_at"}`. The device shows `code` (`ABCD-EF23`) and polls `GET /api/pair/{pairing}`: `202` while waiting, then `200` with `{"status": "paired", "token", "device"}` once, then `410`. A user confirms from their phone with `POST /api/pair/confirm` and `{"code"}`. Codes last 10 minutes.
1. `GET /api/me` answers `{"device", "user"}`, with `user` `null` on a household device.
1. `GET /api/devices` lists the user's devices and the household's. `DELETE /api/devices/{id}` logs one out: one's own, or a household device. A household device can only unpair itself.

Users are added from the backend's command line (`uv run huvu user add`), not from the app. With `auth.mode = "off"`, a request without a valid token comes from a development phone owned by `auth.dev_user`, which is only for localhost.

### Not built

1. The Fire TV launch over ADB, and how an Alexa ask reaches the backend.
1. Live feeds served by the backend itself. Feeds come from sources, each with its own WHEP endpoint.
