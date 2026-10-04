# Backend architecture

The design of huvu's backend, as decided on 2026-10-02 and 2026-10-03, with users and devices added on 2026-10-04. The kernel (everything except the drivers) was built on 2026-10-03 in `backend/`, with the Hermes plugin in `hermes-plugin/`. No driver exists yet. The frontend side of the boundary is in `frontend-contract.md`.

The backend is an interface to data, a holder of state, and the link to the frontend. It owns nothing about the model. Hermes is the agent, and everything to do with prompts, memory, schedules and sessions belongs to Hermes.

## The boundary

| Backend | Hermes |
|---|---|
| Sources, state, the event log, digests | The model, prompts, the system prompt |
| Data triggers (conditions over sources) | Time-only schedules: the dream, the morning and evening chats, "check in 2 hours" |
| The action policy and pending approvals | Memory and skills |
| Provenance labels on data | What it trusts, and its own memory writes |
| The MCP server and its formatting for the model | What to look up, and when |
| Frontend sessions, and turning Hermes output into frontend parts | Its own sessions |
| Users, devices, and which profile serves whom | One profile per user, with that user's memory, skills and cron jobs |

Hermes reaches the backend through the backend's MCP server. The backend reaches Hermes through Hermes's API server: it starts sessions for chats and for trigger wakes there.

## Stack

Python, managed with uv. asyncio for the long-lived connections (Pulsar, the HA websocket). Pydantic for every schema, since its JSON Schema output is what sources publish. FastAPI for the HTTP surface. SQLite with FTS5 for the event log, triggers and the watch cache. The official MCP Python SDK for the MCP server.

Python over TypeScript or Go, because the ML pieces (face embeddings, Whisper, vision models) ship as Python first, Hermes is Python, and the tested Tuya code is Python. One language for the backend and any Hermes plugins.

## Users and devices

huvu serves one family. Each member is a user with a username and a password, added from the command line (`uv run huvu user add`); nobody signs up. Each install of the app is a device (`accounts.py`).

1. **A personal device** (a phone, a browser) belongs to the user who logged in on it.
1. **A household device** (the Fire TV, the speaker hardware still to be built) belongs to nobody. It can't type a password, so it pairs: it shows a code, and a user confirms it from their phone.
1. **Every device has its own token**, stored hashed, so a lost phone is logged out alone.
1. **Phones are taken to be on their user's person.** Approvals, trigger claims and notifications go to phones.
1. **Every device has one conversation**, and each conversation runs on one Hermes session. So a session belongs to a user and a device, or to no user on a household device. A turn never reaches another device.

### Who is at a shared device

A shared device is often in front of one person. When someone asks who it is (the device's "who's this?" picker, the model's `people_identify`, later voice recognition), huvu asks that person on their phone: "is it you on the living room TV?" A yes makes them the device's attendee (`attendance.py`).

The Hermes session doesn't change profile, since Hermes can't switch a session's profile and a new session would throw away its prompt cache. What changes is everything the backend controls:

1. Events from the device are tagged with the person, back to the moment they were asked. Their dream reads them in their own profile.
1. The session's tool calls act for them: a trigger is theirs with no number to claim, and approvals and notifications go to their phone. A tap on the device still goes through the policy.
1. The model gets a note on its next ask, appended to the message, so the cache holds.

It ends when someone else is confirmed, when it's released from the device, the person's phone or the model, or after 10 minutes with no turn.

### Hermes profiles

Each user has their own Hermes profile, named after their username, and the household uses the default profile. A profile has its own memory, skills, sessions and cron jobs, so one person's preferences never reach another's answers. One Hermes gateway serves every profile: a profile's routes live under `/p/<profile>/`, each with its own API key. Household knowledge that everyone shares lives in the backend, where every profile reads it through MCP.

Each profile sends huvu's MCP server its own token, so the backend knows whose Hermes is calling, even for a cron job whose session the backend never saw. A user's profile key and token live in the database. The household's come from the environment (`HERMES_API_KEY`, `HUVU_MCP_TOKEN`).

## Sources

Every data source is a pluggable module. A source owns its own storage, processing, querying and retrieval. The core never tells a source where to keep things. The Tuya source keeps its decrypted snapshots where it likes, and HA keeps its own history.

### The contract

A driver subclasses `huvu.source.Source` (`backend/src/huvu/source.py`). Every method has a default, so a driver implements only what it has.

```python
class Source:
    def __init__(self, options: dict): ...                    # from the config file
    def describe(self) -> SourceSpec: ...                       # required
    async def run(self) -> None: ...                            # long-lived part
    async def read(self, value: str, params: dict) -> Reading: ...
    async def search(self, text: str) -> list[Match]: ...
    async def query(self, name: str, args: dict, caller: Caller) -> Result: ...
    async def act(self, name: str, args: dict, caller: Caller) -> Result: ...
    async def watch(self, value: str, params: dict) -> None: ...
    async def fetch(self, ref: str) -> Blob: ...
    async def feeds(self) -> list[Feed]: ...
```

1. `describe` lists the source's values, events, queries and actions, each with a JSON Schema and a one-line description. The types are in `spec.py`.
1. `run` is the long-lived part: a Pulsar consumer, a websocket, a polling loop. It reports through `self.ctx`: `ctx.emit(type, summary, ...)` for events and `ctx.update(value, reading, ...)` for values. If it raises, the kernel logs a `source_error` event and starts it again with a backoff (5 s, doubling to 5 min).
1. `read` answers `on_demand` values. Pushed values are answered from what the source pushed.
1. `search` finds the source's things by name, area or alias, such as HA entities.
1. `query` covers reads. `act` covers writes; the kernel calls it only after the action policy allows it. Both get the `Caller`, whose `user` says whom the call is for (None for the household), so a source with per-person data, such as a calendar, answers with that person's.
1. `watch` asks the source to push changes for one value. A source that pushes everything anyway (HA) can leave it alone.
1. `fetch` returns the binary content behind a ref (`tuya:album/8f2c`), such as a decrypted snapshot. The frontend gets it through a media URL.
1. `feeds` lists live feeds the source can stream, for the frontend's list.

`self.ctx` also gives a source its own folder (`ctx.data_dir`), a logger, `ctx.now()`, and reads, queries and watches on other sources, for derived sources. An event type a source emits must be in its spec, or the kernel refuses it.

Every argument and result is plain JSON. Sources start as in-process modules. A heavy source can later move to another machine behind HTTP (`GET /describe`, `POST /query/{name}`, `POST /act/{name}`, events pushed to the core) without changing anything else.

### Values, events and actions

A value is what is true now: `ha.entity_state[light.porch]`, `tuya.battery`, `location.eta_home_min`. It changes silently, and only watchers hear each change. An event is something that happened, and it goes into the event log. A source decides what counts as an event. ETA changes every minute, so the location source emits `location.left_home` and `location.arrived_home`, not every ETA change.

```json
{"name": "ha.entity_state", "params": {"entity_id": "string"}, "type": "string",
 "description": "Current state of a Home Assistant entity: on/off for lights, locked/unlocked for locks, a number for sensors.",
 "updates": "pushed"}
{"name": "tuya.battery", "type": "number", "unit": "%",
 "description": "Front door lock battery. Updates only when the lock wakes, so it can be hours old.",
 "updates": "on wake", "max_age": "48h"}
```

### Provenance labels

Every output a source gives carries a provenance label:

1. `owner`: something the user said or did.
1. `house`: device and sensor data, such as event times and device states.
1. `external`: content a stranger controls, such as what a doorbell snapshot shows, what the glasses see, a web page, or a message from someone else.

A doorbell event's time is `house`. The snapshot's content is `external`. The labels are facts about the data, so they belong to the backend. A tool result with `external` content reaches the model marked as such, see [MCP tools](#mcp-tools).

### Derived and built-in sources

A source can read other sources through the core's query and watch API. "Who's home" is a `presence` source that combines phone location, ESPresense rooms through HA and the last unlock. Sources are code the repo owner reviews and commits, so this costs nothing in trust.

State that no device owns lives in a built-in source named `huvu`, behind the same contract: the house mode (normal, away, guests, sleeping), pending approvals, aliases. `huvu.mode` works in a trigger condition the same way `ha.entity_state` does.

## The event log

The core keeps a thin envelope for every event. The full detail stays in the source, reached through refs.

| Field | Meaning |
|---|---|
| `id` | Unique, sortable |
| `at` | When it happened (ISO 8601, local zone) |
| `source` | The source's name |
| `type` | `tuya.doorbell`, `ha.state_changed`, `trigger.run`, `trigger.error`, ... |
| `entity` | What it is about, when there is one |
| `summary` | One line a model can read |
| `refs` | Source-scoped refs to the detail (`tuya:album/8f2c`) |
| `labels` | Provenance labels |
| `user` | Whose event it is. Empty is the household's, which every user sees; one with a user shows only to that user |
| `device` | The device whose conversation it came from, if any |

Cross-source triggers, the daily digest and cross-source search all need one stream, so the envelope lives in the core. SQLite with an FTS5 index over `summary`.

The log scopes itself per user, so each user's dream reads their own events and the household's: actions, approvals, trigger runs and wake replies carry the user they were for. Every finished chat turn is logged too (`conversation.turn`, the ask and the start of the answer), as its user's. That is how a dream learns from what someone said at the TV, whose Hermes session lives in the household's profile.

Digests roll the log up by hour and by day. A digest is data, so the backend builds it and exposes it as the `events_digest` tool. Hermes decides when to read it.

## State

State is the set of values the sources declare. The model reads any value through one tool, with the same names triggers use:

```json
{"tool": "state_get", "args": {"values": ["ha.entity_state[light.porch]", "tuya.battery"]}}
```

```json
{
  "ha.entity_state[light.porch]": {"value": "off", "at": "00:04, 6 min ago", "labels": ["house"]},
  "tuya.battery": {"value": 70, "at": "yesterday 19:55, 4 h ago", "labels": ["house"]}
}
```

The core passes each name to its source, or answers from the watch cache when it holds a fresh copy. Every answer carries `at`, the time of the reading. A source that can't give a value returns `null` with a `reason`.

The model finds values in two steps. The kinds of value are few, so the core joins every source's `describe` into a short index for the model. The things they apply to (hundreds of HA entities) are too many, so the model searches with `state_search`. Aliases from the `huvu` source come first. Each source answers for its own things, matching names, areas and aliases, and each result carries its current value.

### Times for the model

The core, sources, triggers and the frontend use ISO timestamps. The MCP layer turns every timestamp the model sees into a local time with its age:

1. Under an hour: `00:04, 6 min ago`
1. Earlier today: `14:30, 3 h ago`
1. Yesterday: `yesterday 19:55, 4 h ago`
1. Within the last week: `Wed 19:55, 3 days ago`
1. Older: `2026-09-24, 9 days ago`

The absolute time stays next to the age, because a tool result stays in the context after its age is out of date.

## Triggers

A trigger is a condition over source data that wakes Hermes when it comes true. Hermes registers triggers through MCP. The backend evaluates them. Time-only schedules are Hermes cron jobs, not triggers. The backend's clock exists only as a condition inside data triggers.

```json
{
  "note": "Turn on the porch light. User asked for this on 2026-10-02.",
  "when": [
    {"value": "clock.time", "op": "between", "arg": ["00:00", "05:00"]},
    {"value": "location.eta_home_min", "op": "<=", "arg": 10},
    {"value": "ha.entity_state", "params": {"entity_id": "light.porch"}, "op": "==", "arg": "off"}
  ],
  "repeat": "daily",
  "include": ["presence.home", "huvu.mode"],
  "expires": null
}
```

1. `note` comes first. It says why the trigger exists and what to do. It goes back to the model in the wake.
1. `when` is a flat list, and all of it must hold. A predicate is a value check (`value`, optional `params`, `op`, `arg`) or an event check (`{"event": "tuya.doorbell"}`). For "any of these", register two triggers.
1. `repeat` is `"no"` (fire once, then delete), `"yes"` (rearm once `when` turns false) or `"daily"` (at most once a day). A custom `reset` condition covers the rare case none of these fit.
1. `include` lists values the core fetches and puts in the wake, so the model wakes with what it needs and makes no lookups.
1. `expires` takes ISO times or relative ones (`"2h"`, `"tomorrow 09:00"`), resolved by the engine. A `"no"` trigger without one gets 7 days, so a condition that never comes true doesn't stay forever.

Unknown value names are rejected with an error that names the nearest real one, so a small model can fix its call on the next try.

### Evaluation

1. On registration, the engine collects the trigger's inputs and calls `watch` on each source. The clock source pushes every minute, which opens and closes time windows.
1. It keeps the last value of each input with the time it arrived. This is a cache of what live triggers need, not a copy of all state.
1. When any input changes, it checks the whole condition and fires when the result goes from false to true. Which part became true last doesn't matter. A condition that already holds at registration fires at once: the state before registration counts as false.
1. A trigger with an event check fires when that event arrives, if every value check holds then. It can have one event check at most.
1. An input older than its source's `max_age` counts as unknown, and unknown counts as false.
1. A fire wakes Hermes with the note, the matched values and the `include` values. Every fire is a `trigger.run` event, and every failure a `trigger.error` event.

### Users and claims

Every trigger belongs to a user. Its wakes run in that user's Hermes profile, its scripts act as that user, and its approvals and notifications go to that user's phones. `triggers_list` and `triggers_remove` see only the calling profile's triggers.

A trigger set up from a household device has no user yet, so it waits, unclaimed, and doesn't fire:

1. The tool result gives the model a two-digit number, and the device shows or says it.
1. Every user's phone gets the trigger's note and three numbers, one of them right.
1. Whoever taps the right number owns the trigger, and it goes live.
1. A wrong tap drops it. Nobody claiming it within 10 minutes expires it.

### Scripts

A trigger can carry a script: a Python function the model writes, run when `when` matches. It can call the tools the trigger declared, and it ends by acting, waking the model, or doing nothing. The engine keeps watching, edges, rearm and expiry, so scripts don't rewrite them. The script lives in a file and runs in its own process (`script_host.py`), which talks to the core over its stdin and stdout, one JSON line per message. It can call only the tools named in the trigger's `grants`, through the same tool box Hermes uses, and actions still go through the policy. It has 30 s. It is not sandboxed: the agent already has a terminal, and it's there to help.

`triggers_test` checks a trigger without registering it: each check's current reading and result, the last few events an event check would have matched, and a dry run of the script against them. Queries run for real, actions are recorded without running, and the model gets back what the script would have done. The dream then reviews real runs from `trigger.run` and `trigger.error` events.

## Actions and the policy

Every write goes through one policy check in the backend, whoever asks: Hermes, a script, or a frontend button.

1. Each action has an autonomy level: silent, notify, or ask first.
1. Ask first creates a pending approval with a deadline. It lives in the `huvu` source.
1. An approval belongs to the user it was asked for: the chat's user, or the trigger's. It goes to that user's phones, and only they can answer it. One asked from a household device goes to every user's phones, and the first answer wins.
1. Every action, allowed or refused, goes into the event log with who asked and why.
1. An action a driver marks `requires_human` always asks, and config can't lower it. Only a user's answer from their phone (`POST /api/approvals/{id}`) can approve it, so no model can approve its own request.
1. A user's tap on a personal device (`POST /api/actions/{source}/{action}`) runs at once: the tap is the approval. A tap on a household device goes through the policy like any other request, so the TV can't open the door.
1. Config (`[policy]` in `huvu.toml`) can change any other action's level.
1. Unlocking the front door always needs a human tap. A model or a face match alone never opens it.

## Hermes

Hermes stays the harness for its ecosystem and extensibility, and because its plugins are Python. OpenClaw was considered: its dreaming is memory consolidation into `MEMORY.md` with tool-free model calls that only learn from interactive sessions, so it doesn't cover huvu's dream, and its plugins are TypeScript.

### What Hermes owns

1. **The dream.** A Hermes cron job with a prompt huvu writes. It reads `events_digest`, `trigger.run` and `trigger.error` events, fixes or deletes broken triggers, registers new ones, updates its own memory and skills, and writes proposals for the morning chat.
1. **Time-only schedules**, as Hermes cron jobs.
1. **What it learns.** Which tools and values it uses often, that `state_search` exists, what you call the hall light. This goes in its own memory and skills, written during the dream. Little goes into the model's memory beyond that.
1. **The situation block** at the start of a session: time, where you are, the house, pending approvals. The huvu Hermes plugin adds it on a session's first turn, from `POST /api/situation`. The values in it are set in the plugin's environment (`HUVU_SITUATION`).

### Settings

1. Background review off (`auxiliary.background_review.enabled: false`). It is not per turn: it runs every 10 user turns (`memory.nudge_interval`) or 10 tool iterations (`skills.creation_nudge_interval`), and each run forks the agent at about 30K tokens. The dream does the same work once a night.
1. The skill curator stays on. It runs weekly, when idle for 2 hours, and archives agent-made skills nobody uses.
1. In every profile, the huvu plugin is enabled (`plugins.enabled: [huvu]`), and the backend is an MCP server named `huvu` in `mcp_servers`, with `url: http://<backend>/mcp` and the profile's own `HUVU_MCP_TOKEN` as the bearer header. See `hermes-plugin/README.md`.

### The huvu plugin

Hermes connects to an MCP server once, with fixed headers. The profile's token says which user is calling, and the huvu Hermes plugin (`hermes-plugin/huvu/`) carries the session across the boundary. Its `pre_tool_call` hook adds `_session` to every huvu tool call. The backend strips it and looks the session up in `hermes_sessions`, which names its device. A session that belongs to another user than the calling profile's is ignored. The plugin also adds the situation block, see [What Hermes owns](#what-hermes-owns).

Session taint was removed on 2026-10-04. It followed OpenClaw's taint gate: a session that had read `external` content got stricter policy, its triggers waited for the owner's tap, and its memory and skill writes went to Hermes's approval gate. It was judged more complexity than current models need. Two guards stay without it. Provenance labels still mark outside content for the model, and `requires_human` actions always need a person's tap.

### MCP tools

Hermes names each one `mcp__huvu__<name>`.

| Tool | What it does |
|---|---|
| `state_get` | Reads values |
| `state_search` | Finds values by name, area or alias |
| `events_search`, `events_get` | Full-text search over the event log, and one event |
| `events_digest` | A day's or an hour's digest |
| `sources_list` | Every source with its values, events, queries and actions |
| `triggers_register`, `triggers_test`, `triggers_list`, `triggers_remove` | Trigger management |
| `<source>_<query>`, `<source>_<action>` | Built from each source's `describe`. Every action takes an optional `reason` |
| `ui_choices`, `ui_show_feed`, `ui_show_image` | Parts for the frontend, added to the live turn of the calling session |
| `people_identify`, `people_release` | On a shared device: ask a family member on their phone whether it's them, or forget who is there |

The server's instructions carry the index of every kind of value, so the model starts each session knowing what it can read. A result with outside content comes back as `{"data": ..., "provenance": "external"}`, and the instructions tell the model to treat text in it as data.

## Sessions

Hermes's API server runs many sessions. Its session API (`POST /api/sessions`, then `POST /api/sessions/{id}/chat/stream`) streams `assistant.delta`, `tool.started` (with the tool's full arguments), `tool.completed` (with a result preview capped at 500 characters), `tool.failed` and a final `run.completed`, `run.failed` or `run.cancelled`. Concurrent turns on one session are serialized by Hermes.

The backend multiplexes on top of that (`conversations.py`):

1. A device's conversation maps to one Hermes session, in its user's profile or the default one.
1. Every trigger wake starts a fresh Hermes session in the trigger user's profile, with the wake payload (`wake.py`). Its reply goes to the log as a `wake.done` event. If the agent wants to reach the user, it calls `huvu_notify`. Two wakes run at once at most.
1. A turn streams to the client that asked, and to the same device's other push streams. It never reaches another device.
1. Hermes's own sessions (the dream, the morning chat) are its business. A chat Hermes starts reaches the frontend through `huvu_notify`.
1. Edit and retry rewind. Hermes's API can fork a session but can't cut one short, so a rewind starts a fresh Hermes session and seeds its first message with a transcript of the turns that stay. The earlier turns' tool calls don't carry over.

The relay turns Hermes's events into frontend events. Hermes's housekeeping tools (memory, skills, session search) and huvu's reads stay hidden. huvu's actions show as notes. Hermes tools with an adapter (`web_search`) get the fields the stage reads; any other tool shows as a generic note.

## Code layout

```
backend/
  pyproject.toml, huvu.toml.example
  src/huvu/
    spec.py           the contract's types: SourceSpec, ValueSpec, Event, Reading, labels, Caller
    accounts.py       users, devices, pairing, tokens, each user's Hermes profile
    attendance.py     who is at a shared device, confirmed from their phone
    source.py         Source (what a driver subclasses) and SourceContext
    registry.py       loaded sources, name resolution with suggestions, supervision
    names.py          value keys
    state.py          readings, watchers, search
    events.py         the event log and digests
    triggers.py       the trigger format, validation and the engine
    scripts.py        trigger scripts, run by script_host.py in their own process
    policy.py         autonomy levels, approvals, audit
    tools.py          the tool box MCP and scripts share
    mcp_server.py     the MCP server
    hermes.py         the Hermes API client
    conversations.py  frontend turns on Hermes sessions, and the relay
    wake.py           trigger wakes
    sessions.py       what the kernel knows about Hermes sessions
    push.py           the push channel
    media.py          media URLs for refs
    http.py           the HTTP server
    auth.py, config.py, db.py, times.py, kernel.py, __main__.py
    builtin/          the clock and huvu sources
  src/huvu_drivers/   the drivers, one module per source
  tests/
hermes-plugin/huvu/   the Hermes plugin
```

Run it with `uv run huvu` from `backend/` (it reads `huvu.toml`, or `$HUVU_CONFIG`), and the tests with `uv run pytest`.

## Open questions

1. **Strict tool schemas.** Check if Hermes uses OpenAI's strict mode for the Codex provider. Enums of value names would make made-up names impossible.
1. **Pulsar.** The Tuya consumer has never run. See `front-door-lock.md`.
1. **Trigger evaluation at scale.** Untested with fast inputs like location and glasses frames. Those sources filter before they emit.
1. **The plugin against a live Hermes.** The hooks were tested the way Hermes's source calls them, not inside a running Hermes.
1. **Profiles against a live Hermes.** The `/p/<profile>/` routes and per-profile keys come from Hermes's source and docs. No backend call has gone through them yet.
1. **Native push.** Approvals, claims and notifications reach a phone only while its app holds the push stream open. FCM and APNs need a token registry and a sender.
1. **Groups (low priority).** "No user" means the household today. Groups would generalize that: the household is one group, and a device or event belongs to a group. Members get implicit trust: they see the group's events and answer its approvals. This only matters once trust differs between people (a guest or babysitter who can't approve an unlock, kids, a second house). Open: is trust per group or per role inside it? The change goes through `accounts.phones`, `policy.pending_for`, `events._visible` and the household's Hermes profile.
1. **Turns across a restart.** A turn running when the backend stops ends as failed. Hermes may still finish it, unseen.

## Build order

1. Drivers, starting with Tuya (Pulsar first, then the album fetch and decryption). Checked by pointing Hermes at the MCP server, ringing the doorbell, and asking who rang.
1. The frontend's side of users and devices, the push channel and approvals (`frontend-changes.md`).
1. The HA driver.
