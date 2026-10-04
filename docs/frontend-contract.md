# Frontend contract

What the frontend expects from the backend. The Python backend (`backend/src/huvu/http.py`) serves it. The backend design is in `backend-architecture.md`.

## Connection

The base URL is hard-coded as `http://localhost:3001` in `frontend/src/chat/backend.ts`. Every response sets CORS headers for any origin, allows `GET, POST, DELETE, OPTIONS` and `Content-Type`, and exposes `Location`, since WHEP clients read their session URL from it. Nothing is authenticated yet.

### Chat

`POST /api/chat`

```json
{"conversation": "c1", "id": "t_9f2c41a7b0", "ask": "...", "replaces": "t_0a1b2c3d4e", "actions": [{"text": "opened the live feed balcony", "at": "2026-10-03T13:42:00Z"}]}
```

1. `ask` is the only required field. `conversation` defaults to `default`.
1. `id` is the turn's own id, minted by the client (`t_` and ten hex digits). The backend keeps it, so a stop or a rewind names the turn by an id the client already has. Left out, the backend picks one. An id already used answers `409`.
1. `replaces` is for edit and retry: the turn it names is replaced, with everything after it. The frontend names the first dropped turn the backend has heard of, since a turn that never reached the backend is not there to replace. An unknown id answers `404`.
1. `actions` is what the owner did in the app since the last ask. Today that is opening a live feed: the feed turns after the last real turn (`actionsAfterLastAsk` in `chat/thread.ts`). Opening a feed makes no call of its own. Hermes gets each action as a note in front of the ask, marked as context that needs no reply. The turn stores them, and a rewind replays them in its transcript.

The answer is Server-Sent Events. Every event's data carries `turn` and `conversation`. A status other than 200 ends the turn as failed, and so does a stream that closes before `turn.ended`.

| Event | Data, besides `turn` and `conversation` |
|---|---|
| `turn.started` | `ask`, `at` |
| `answer.delta` | `text`, appended to the answer |
| `part` | `part`: a whole `RichPart`, added to the turn |
| `part.update` | `id`, `result`: a tool part's result, once the tool finishes |
| `turn.ended` | `ending` (`null`, `"failed"` or `"stopped"`) and the whole `answer` |

A comment line (`: keepalive`) comes every 15 s while nothing else does. Dropping this stream doesn't stop the turn, since other clients may be showing it; `POST /api/turns/{turn}/stop` does. `GET /api/conversations/{id}` returns the conversation's turns, with `id`, `at`, `ask`, `answer`, `parts`, `actions`, `ending` and `running`, for a client that opens later.

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

`GET /api/push?client=<id>&kind=<web|phone|firetv>` is one long-lived SSE stream per open client. It carries every turn event above, from every conversation, so the TV shows a turn the phone started; a client gets its own turns twice and keeps one by turn id. It also carries `notify` (`text`, `image`, `urgency`) and `approval` (a pending or resolved approval).

### Approvals and actions

`GET /api/approvals` lists pending approvals: `id`, `action`, `args`, `reason`, `asked_by`, `status`, `created_at`, `deadline`. `POST /api/approvals/{id}` with `{"approve": true}` or `false` answers one; the answer runs the action through the backend, and the result comes back. This is the only way to approve, so no model can approve its own request.

`POST /api/actions/{source}/{action}` with the action's args runs one of the owner's own taps. The tap is the approval, so it runs at once.

### Feeds and media

`GET /api/feeds` lists every source's feeds, in the shape above. Images get URLs as `/api/media/<token>`, such as a decrypted doorbell snapshot. `<img>` tags can't send a bearer header, so the unguessable token is the permission, and these URLs need no other auth.

### Auth

With `auth.mode = "jwt"`, every endpoint except media and `/health` needs a bearer JWT signed with `HUVU_JWT_SECRET` whose `email` (or `sub`) is the configured owner. The push stream also takes the token as `?token=`, for clients that can't set headers on a stream. With `auth.mode = "off"`, everything passes, which is only for localhost.

### Not built

1. The Fire TV launch over ADB, and how an Alexa ask reaches the backend.
1. Live feeds served by the backend itself. Feeds come from sources, each with its own WHEP endpoint.
