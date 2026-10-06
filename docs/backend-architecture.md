# Backend architecture

The design of huvu's backend, as decided on 2026-10-02 and 2026-10-03, with users and devices added on 2026-10-04, and history tables and the Home Assistant driver on 2026-10-06. The kernel (everything except the drivers) was built on 2026-10-03 in `backend/`, with the Hermes plugin in `hermes-plugin/`. The first driver, Home Assistant (`huvu_drivers/ha/`), was built on 2026-10-06 and has only run against a fake HA. The frontend side of the boundary is in `frontend-contract.md`. What Home Assistant itself must look like for huvu is in `home-assistant.md`.

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

Python, managed with uv. asyncio for the long-lived connections (Pulsar, the HA websocket). Pydantic for every schema, since its JSON Schema output is what sources publish. FastAPI for the HTTP surface. SQLite with FTS5 for the event log, triggers and the last reading of every pushed value, and a second SQLite file for the history tables. The official MCP Python SDK for the MCP server. `websockets` for the HA connection.

Python over TypeScript or Go, because the ML pieces (face embeddings, Whisper, vision models) ship as Python first, Hermes is Python, and the tested Tuya code is Python. One language for the backend and any Hermes plugins.

**Everything is SQLite** (decided 2026-10-06). The kernel's database, the history tables, and any database a driver keeps for itself are SQLite files. No other database engine is used. What would push toward another engine is served by SQLite extensions instead: FTS5 for text search (the event log has it), the JSON functions for nested data, `sqlite-vec` for embeddings when semantic search comes. One engine means one query language for the dream, one way to back up `data/`, and joins across sources in one query. The question came up because some sources hold data that isn't tabular (images, audio, free text). That data stays in the source. What the source records is rows of facts about it with a ref to the raw content, see [History tables](#history-tables).

## Users, groups and devices

Each person is a user with a username and a password, added from the command line (`uv run huvu user add`); nobody signs up. Each install of the app is a device (`accounts.py`).

1. **A personal device** (a phone, a browser) belongs to the user who logged in on it.
1. **A shared device** (the Fire TV, the speaker hardware still to be built) belongs to a group. It can't type a password, so it pairs: it shows a code, and a member confirms it from their phone, choosing the group if they're in more than one.
1. **Every device has its own token**, stored hashed, so a lost phone is logged out alone.
1. **Phones are taken to be on their user's person.** Approvals, "is it you?" questions and notifications go to phones.
1. **Every device has one conversation**, and each conversation runs on one Hermes session. So a session belongs to a user and a device, or to a group on a shared device. A turn never reaches another device.

### Groups

A group is a set of people who share devices and trust each other. The household is the default group, `home`, which always exists. A second house would be another. A user is in a group as a member or a guest:

1. **Members** trust each other implicitly. They see the group's events, answer its approvals, and pair and unpair its shared devices. Any member can add a shared device to the group; a shared device never pairs to a user.
1. **Guests** (a babysitter, a visitor) can use the group's shared devices and be recognized at them. They trust nothing: they see only their own events, and they never approve anything. Every approval a guest causes, even with a tap on their own phone, goes to the group's members.

A user can be in several groups, or none. The CLI puts a new user in `home` as a member unless told otherwise (`--group`, `--guest`, `--no-group`), and `huvu group add`, `join`, `leave` and `list` manage the rest.

### Who is at a shared device

A shared device is often in front of one person. When someone asks who it is (the device's "who's this?" picker, the model's `people_identify`, later voice recognition), huvu asks that person on their phone: "is it you on the living room TV?" A yes makes them the device's attendee (`attendance.py`).

The Hermes session doesn't change profile, since Hermes can't switch a session's profile and a new session would throw away its prompt cache. What changes is everything the backend controls:

1. Events from the device are tagged with the person, back to the moment they were asked. Their dream reads them in their own profile.
1. The session's tool calls act for them: a trigger is theirs at once, and approvals and notifications go to their phone. A tap on the device still goes through the policy.
1. The model gets a note on its next ask, appended to the message, so the cache holds.

It ends when someone else is confirmed, when it's released from the device, the person's phone or the model, or after 10 minutes with no turn.

### Hermes profiles

Each user has their own Hermes profile, named after their username. Each group's shared devices run in the group's profile: the default group uses Hermes's default profile, and any other group a profile named after it. Users and groups share one namespace for that reason. A profile has its own memory, skills, sessions and cron jobs, so one person's preferences never reach another's answers. One Hermes gateway serves every profile: a profile's routes live under `/p/<profile>/`, each with its own API key. Knowledge everyone shares lives in the backend, where every profile reads it through MCP.

Each profile sends huvu's MCP server its own token, so the backend knows whose Hermes is calling, even for a cron job whose session the backend never saw. Users' and groups' profile keys and tokens live in the database. The default group's come from the environment (`HERMES_API_KEY`, `HUVU_MCP_TOKEN`).

## Sources

Every data source is a pluggable module. A source owns its own working storage, processing, querying and retrieval. The Tuya source keeps its decrypted snapshots where it likes in its folder, and a source that needs a database of its own uses SQLite. The one thing a source doesn't keep for itself is its history for analysis: it records those rows into the kernel's history tables, see [History tables](#history-tables).

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

1. `describe` lists the source's values, events, queries, actions and history tables, each with a schema and a one-line description, plus `guide`: the hand-written part of the source's Hermes skill (when to use it, the gotchas). The types are in `spec.py`. The kernel binds the source before it calls `describe`, so `describe` can read the source's own folder.
1. `run` is the long-lived part: a Pulsar consumer, a websocket, a polling loop. It reports through `self.ctx`: `ctx.emit(type, summary, ...)` for events, `ctx.update(value, reading, ...)` for values and `ctx.record(table, row, ...)` for history rows. If it raises, the kernel logs a `source_error` event and starts it again with a backoff (5 s, doubling to 5 min). A run that lasted at least a minute resets the backoff to 5 s, so a source that loses its connection once a month (HA restarting for an update) comes back in 5 s, not 5 min.
1. `read` answers `on_demand` values. Pushed values are answered from what the source pushed.
1. `search` finds the source's things by name, area or alias, such as HA entities. Each match names the actions that work the thing, so the model learns how to work a device where it finds it.
1. `query` covers reads. `act` covers writes; the kernel calls it only after the action policy allows it. Both get the `Caller`, whose `user` says whom the call is for and `group` which group's shared device it came through, so a source with per-person data, such as a calendar, answers with that person's.
1. `watch` asks the source to push changes for one value. A source that pushes everything anyway can leave it alone. HA uses it: its values are `on_demand`, and it pushes only the entities a trigger watches.
1. `fetch` returns the binary content behind a ref (`tuya:album/8f2c`), such as a decrypted snapshot. The frontend gets it through a media URL.
1. `feeds` lists live feeds the source can stream, for the frontend's list.

`self.ctx` also gives a source its own folder (`ctx.data_dir`), a logger, `ctx.now()`, and reads, queries and watches on other sources, for derived sources. An event type a source emits must be in its spec, or the kernel refuses it, and so must a table it records into and every column of the row.

**A spec can change while the source runs.** The HA driver's actions are the scripts in Home Assistant, and those change when someone edits HA. The source calls `ctx.respec()`, and the kernel (`Kernel.respec`) calls `describe` again, adds any new history tables and columns, rebuilds the tool box, rewrites the skills, and logs a `source.respec` event that names the actions and queries added and removed. The MCP tool list doesn't change, because source actions run through `act`, so no Hermes session loses its prompt cache. A trigger script whose grant names an action that is gone fails at its next run, as a `trigger.error` event the dream reads.

Every argument and result is plain JSON. Sources start as in-process modules. A heavy source can later move to another machine behind HTTP (`GET /describe`, `POST /query/{name}`, `POST /act/{name}`, events pushed to the core) without changing anything else.

### Values, events and actions

A value is what is true now: `ha.entity_state[light.porch]`, `tuya.battery`, `location.eta_home_min`. It changes silently, and only watchers hear each change. An event is something that happened, and it goes into the event log. A source decides what counts as an event. ETA changes every minute, so the location source emits `location.left_home` and `location.arrived_home`, not every ETA change.

```json
{"name": "ha.entity_state", "params": {"entity_id": "string"}, "type": "string",
 "description": "An exposed Home Assistant entity's state, as HA says it: on, off, a mode, a number as text",
 "updates": "on_demand"}
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

### The Home Assistant driver

Decided and built on 2026-10-06 (`huvu_drivers/ha/`). How to set up HA for it, for the agents that do that work: `home-assistant.md`.

1. **The household builds the abstraction in HA, and huvu only translates it.** Most devices in the house are infrared, worked by a Broadlink RM4 as sequences of button presses, so HA's own services (`light.turn_on`, `climate.set_temperature`) say little about them. Every way huvu may change the house is an HA script with a description, fields and an area. Each exposed script becomes one action, named after the script (`script.bedroom_ac_set` is `ha_bedroom_ac_set`), its description becomes the action's, and its fields' selectors become the input schema. huvu defines no actions of its own: a first design had huvu-side groups (`turn_on`, `set_climate`, `unlock`, a generic `call_service`) and was dropped once the scripts carried the abstraction. There is no raw service call, so the policy and the history only see actions someone named.
1. **Labels on the script set the autonomy.** `huvu_silent`, `huvu_notify`, `huvu_ask`, `huvu_human` (which is `requires_human`). A script with no huvu label asks, so a forgotten label costs a tap and never runs something unasked; the strictest label wins. Because each script is its own action, the policy is per script with no kernel change. The alternative, one generic `run_script` action with a hook letting a source raise the level per call, was considered and not needed.
1. **What HA exposes to Assist is what huvu sees** (`homeassistant/expose_entity/list`). An entity that isn't exposed can't be read, searched or worked. The family manages it in HA's UI, and there is no second list in `huvu.toml`. The list is shared with HA's own Assist. `exposure = "all"` reads everything, for development.
1. **Three values, all `on_demand`**: `ha.entity_state`, `ha.entity_number` and `ha.entity_attributes`, each with `entity_id`. A ValueSpec has one type, and the trigger engine compares by it, so a number needs its own value (`"9" <= "10"` is false as text). The driver keeps every exposed state in memory and answers reads from it; pushing every change through `ctx.update` would write a row per power-meter reading. It pushes only entities a trigger watches. An `unavailable` or `unknown` entity reads as null with a reason, and so does everything while HA is disconnected, so a trigger never fires on a stale value.
1. **The scripts can change while huvu runs.** The driver reloads the registries, the exposure list and the scripts on any registry event, and every 5 minutes for edits HA sends no event for, and calls `ctx.respec()` when the actions differ. It caches the scripts in its folder, so the actions exist before HA connects.
1. **Every change says who caused it.** HA's context on each change gives four actors: `huvu` (huvu's token belongs to its own HA user), `ha_user` (someone in the HA app, mapped to a huvu user by the `users` option), `automation` (no user but a parent context), and `device` (neither: a switch, the device's own button). A change inside a script run takes the run's actor. For huvu's own runs the driver also knows the huvu user, because it matches the run to the call it has in flight. huvu's token must belong to a separate HA user, or huvu's actions would look like that person's, and that user must be an administrator to read the registries.
1. **History** goes into three tables: `ha_changes` (every state change of an exposed entity, and every change of an attribute a person chooses, such as brightness or a target temperature, with the actor), `ha_script_runs` (every run, by anyone; the arguments only for huvu's runs, since HA doesn't report them for others), and `ha_sensor_5min` (numeric sensors as time-weighted 5-minute buckets, with a row per bucket even when nothing changed, so the latest row before a time is the value then). Buckets live only while the connection does, so a gap in the connection is a gap in the rows.
1. **Events** go to the log only for what a person would call an event: locks, covers, alarms, people, and door, window, smoke and leak sensors, plus any domain in the `event_domains` option. Transitions to or from `unavailable` stay out. Script runs by anyone but huvu are `ha.script_run` events; huvu's own are `action.ran` already.
1. **Queries**: `ha_history` (HA's recorder, which keeps about 10 days by default), `ha_areas`, and `ha_problems` (scripts left out, missing descriptions, unknown labels, a token on a family member's user). The query names are reserved: a script named `history` is left out.
1. **Infrared is a gap in the data.** A press on a physical IR remote never reaches HA, so the dream would only learn from what huvu itself did. A power-metering plug on each IR device worth learning about closes the gap: its watts show the real state, and the change shows as `device`.
1. **Not built**: cameras and their snapshots, live feeds from HA's go2rtc, and naming the automation behind an `automation` change.

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
| `user` | Whose event it is. One with a user shows only to that user |
| `group` | Without a user, the group whose members see it. With neither, every member of any group sees it (a doorbell press); guests don't |
| `device` | The device whose conversation it came from, if any |

Cross-source triggers, the daily digest and cross-source search all need one stream, so the envelope lives in the core. SQLite with an FTS5 index over `summary`.

The log scopes itself, so each user's dream reads their own events, their member groups' and the house's: actions, approvals, trigger runs and wake replies carry the user they were for, and a shared device's turns with nobody recognized carry its group. Every finished chat turn is logged too (`conversation.turn`, the ask and the start of the answer), as its user's. That is how a dream learns from what someone said at the TV, whose Hermes session lives in the group's profile.

Digests roll the log up by hour and by day. A digest is data, so the backend builds it and exposes it as the `events_digest` tool. Hermes decides when to read it.

## History tables

Decided and built on 2026-10-06 (`data.py`). huvu's agent is expected to learn each person's preferences on its own, mostly in the nightly dream, by doing real data engineering over the house's data. A value says only what is true now, and an event is a one-line summary for reading; neither can be averaged or grouped. So every source can also keep history tables: typed rows, one per thing that happened, kept for good.

1. **A source declares its tables** in its spec (`TableSpec`: a name, a description, typed columns with descriptions, and indexes) and **writes rows** with `ctx.record(table, row, at=..., user=..., group=...)`. The column types are text, integer, real, boolean and json.
1. **The kernel keeps every source's tables in one SQLite database**, `<data_dir>/analytics.db`, as `<source>_<table>` (`ha_script_runs`). One database is what lets one query join sources. It is separate from `huvu.db`, so heavy analytical reads never touch the database that holds users and tokens.
1. **Every row gets the standard columns**, filled in by the kernel, never by the source: `at` (ISO with the local offset), `ts` (seconds since the epoch), `local_date`, `local_minute` (minutes since local midnight), `weekday` (1 is Monday), `user_id` and `group_id`. These are the shared keys every join across sources uses, and computing them in one place means two sources can't disagree on what "22:30 on a Tuesday" is. Every table is indexed on `(ts)` and `(user_id, ts)`, plus the indexes its spec names.
1. **Rows are never deleted, and columns are never dropped.** A newer driver that declares a new column gets it added to the table (`ALTER TABLE ADD COLUMN`), and old rows have it as NULL.
1. **Raw content stays in the source.** For data that isn't tabular (a doorbell snapshot, a voice clip, a long message), the row holds the facts about it (when, who, what was recognized) and a ref to the content, which `fetch` returns.
1. `user_id` and `group_id` mean what they mean on events: whose row it is, and with neither, the house's.

### data_sql

The `data_sql` tool runs one read-only SQLite statement for the caller and returns its columns and at most `limit` rows (200 by default, 1000 at most). It is how the dream, and any session, analyses the history. Each source's skill lists its tables and columns, so the model knows the schema without a lookup.

The query runs on its own read-only connection, on a worker thread, so a long query never blocks the event loop; WAL lets it read while drivers write. Before the query runs, the kernel:

1. Attaches `huvu.db` read-only, as `kernel`.
1. Puts a temporary view in front of every history table, with the same name, that keeps only the rows the caller may see, with the same rule as the event log (`Scope`). A view named like the table shadows it, so queries use the plain names.
1. Adds an `events` view over the event log, scoped the same way, with the same `local_date`, `local_minute` and `weekday` columns, so events join with history rows on the same keys.
1. Turns on `query_only`, and sets an authorizer. The authorizer refuses every read of the real tables that doesn't come through a view, so the raw tables, other users' rows and the rest of `huvu.db` (users, tokens) are out of reach. It also refuses writes, pragmas and `ATTACH`.
1. Sets a progress handler that stops the query after 10 s.

A scope view must read a column in its WHERE clause, even for the kernel's own unscoped reads, because SQLite's authorizer only learns that a `count(*)` came through a view when the view reads a column.

### Joining sources

Most joins across sources at home are on time, person and place, the way a warehouse joins fact tables on shared dimensions. Rows almost never share an exact timestamp, so the joins are:

1. **Window**: the AC script within 30 minutes after an arrival (`r.ts BETWEEN a.ts AND a.ts + 1800`).
1. **As-of**: the room temperature when the script ran (the latest sensor row with `ts <= r.ts`, as a correlated subquery on the `(entity_id, ts)` index).
1. **Interval**: who was home when the gate opened (`start <= ts AND ts < end`).
1. **Bucket**: hot days against AC hours (both rolled up to `local_date`).

The pattern the dream should follow is to join once into a wide feature table (one row per person per day: arrival time, first AC run, set temperature, outside high, weekday) and look for patterns in that, rather than joining raw tables for every question.

### Not built yet

Decided, with the build left for later:

1. **A people map**: (source, external id) to huvu user, kept by the kernel, so a Tuya fingerprint slot or a face id resolves to a person through a view, and fixing a mapping fixes all earlier rows. The HA driver maps HA users itself for now (its `users` option).
1. **Places**: HA's areas as the canonical rooms, with other sources (ESPresense rooms, a camera's location) mapped onto them, so `place_id` means the same in every table.
1. **A date table**: one row per day with weekday, season, and public holidays and festivals, so a festival week isn't read as noise.
1. **Interval views**: state as periods (`entity, state, start, end`), derived with `LEAD()` over a change table, for interval joins.
1. **A writable schema per profile**, next to the read-only tables, where the dream keeps derived tables (`daily_features`, `visitors`) and saved queries that back each preference it stores in memory, so the next dream can check the claim again.
1. **`triggers_backtest`**: replays a trigger's `when` over history, so the dream can say "this would have fired on 11 of the 13 evenings you did it yourself" before it proposes the trigger.

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

The core passes each name to its source, or answers from the last reading the source pushed. Those readings are kept in SQLite too, so a restart forgets nothing: a lock battery that only reports when the lock wakes is still known after one, with its old time, and its max age still applies. Every answer carries `at`, the time of the reading. A source that can't give a value returns `null` with a `reason`.

The model finds values in two steps. The kinds of value are few, so each source's skill lists them (see [Skills](#skills)). The things they apply to (hundreds of HA entities) are too many, so the model searches with `state_search`. Aliases from the `huvu` source come first. Each source answers for its own things, matching names, areas and aliases, and each result carries its current value and the actions that work it.

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
1. It reads each input's last value, with the time it arrived, from the state's readings, which survive a restart. So a restart doesn't make every input unknown at once, and doesn't fire a trigger a second time when the values come back.
1. When any input changes, it checks the whole condition and fires when the result goes from false to true. Which part became true last doesn't matter. A condition that already holds at registration fires at once: the state before registration counts as false.
1. A trigger with an event check fires when that event arrives, if every value check holds then. It can have one event check at most.
1. An input older than its source's `max_age` counts as unknown, and unknown counts as false for firing. It doesn't count as the condition turning false, though: a `repeat: "yes"` trigger rearms only when every input is known and the condition is false.
1. A fire wakes Hermes with the note, the matched values and the `include` values. Every fire is a `trigger.run` event, and every failure a `trigger.error` event.

### Runaway triggers

1. **One run at a time.** A trigger is running from the look that decides to fire until its wake or script ends. A fire that comes meanwhile is dropped. A value that changed meanwhile gets one more look when the run ends, so a condition that went false in between still rearms it. A trigger whose own action causes the event it watches finds itself running, so it can't feed itself. Each trigger has its own claim; one slow trigger doesn't hold up the others.
1. **Pausing.** Any session acting for a trigger's user can pause and resume it (`triggers_pause`, `triggers_resume`), and so can the user from the app. A paused trigger keeps everything but stops watching.
1. **The wake limit.** A trigger may wake the model at most 20 times in two hours, counted from the event log (`wake.started`), since its last resume if that is later. From the 10th wake on, the wake tells the model and asks it to handle the repeats in a script or pause the trigger until the person approves. The 21st doesn't happen: the trigger pauses itself, and its user is notified. A trigger the limit paused resumes only by the user's own tap.
1. **The busy notice.** A user whose triggers fired more than 10 times in the last hour, together, is notified, once an hour at most. It also catches a script-only trigger that never wakes the model.
1. Scripts never call the model themselves: `ctx.wake` asks the backend to, through the same waker and the same limit.

### Whose a trigger is

Every trigger belongs to a user. Its wakes run in that user's Hermes profile, its scripts act as that user, and its approvals and notifications go to that user's phones. `triggers_list` and `triggers_remove` see only the calling profile's triggers.

A trigger set up from a shared device nobody is recognized at has no user yet, so it waits, unconfirmed, and doesn't fire:

1. Everyone in the device's group, members and guests, gets "is this you on the living room TV?" on their phone, with the trigger's note.
1. The tool result tells the model the trigger goes live only once someone taps yes, so it tells the person to check their phone.
1. The first yes recognizes that person at the device (see [Who is at a shared device](#who-is-at-a-shared-device)), and the device's waiting triggers become theirs and go live. The other questions are withdrawn.
1. If everyone says no, or nobody answers within 5 minutes, the waiting triggers are dropped.

A trigger with nobody to ask (a group profile's cron job, which has no device) is refused.

### Scripts

A trigger can carry a script: a Python function the model writes, run when `when` matches. It can call the tools the trigger declared, and it ends by acting, waking the model, or doing nothing. The engine keeps watching, edges, rearm and expiry, so scripts don't rewrite them. The script lives in a file and runs in its own process (`script_host.py`), which talks to the core over its stdin and stdout, one JSON line per message. It can call only the tools named in the trigger's `grants`, through the same tool box Hermes uses, and actions still go through the policy. It has 30 s. It is not sandboxed: the agent already has a terminal, and it's there to help.

`triggers_test` checks a trigger without registering it: each check's current reading and result, the last few events an event check would have matched, and a dry run of the script against them. Queries run for real, actions are recorded without running, and the model gets back what the script would have done. The dream then reviews real runs from `trigger.run` and `trigger.error` events.

## Actions and the policy

Every write goes through one policy check in the backend, whoever asks: Hermes, a script, or a frontend button.

1. Each action has an autonomy level: silent, notify, or ask first.
1. Ask first creates a pending approval with a deadline. It lives in the `huvu` source.
1. An approval belongs to the user it was asked for: the chat's user, or the trigger's. It goes to that user's phones, and only they can answer it. One asked from a shared device nobody is recognized at, or caused by a guest, goes to the group's members, and the first answer wins.
1. The same request twice while the first waits (a double press, two clients racing) opens one approval. A unique key on pending approvals decides it in the database: the first gets the approval, and the other gets only `already_pending`. Answering races the same way: two phones answering at once run the action once, and the slower answer is refused.
1. Every action, allowed or refused, goes into the event log with who asked and why.
1. An action a driver marks `requires_human` always asks, and config can't lower it. Only a user's answer from their phone (`POST /api/approvals/{id}`) can approve it, so no model can approve its own request.
1. A member's tap on a personal device (`POST /api/actions/{source}/{action}`) runs at once: the tap is the approval. A tap on a shared device, or by a guest, goes through the policy like any other request, so neither the TV nor the babysitter's phone can open the door.
1. Config (`[policy]` in `huvu.toml`) can change any other action's level.
1. Unlocking the front door always needs a human tap. A model or a face match alone never opens it.

## Hermes

Hermes stays the harness for its ecosystem and extensibility, and because its plugins are Python. OpenClaw was considered: its dreaming is memory consolidation into `MEMORY.md` with tool-free model calls that only learn from interactive sessions, so it doesn't cover huvu's dream, and its plugins are TypeScript.

### What Hermes owns

1. **The dream.** A Hermes cron job with a prompt huvu writes. It reads `events_digest`, `trigger.run` and `trigger.error` events, fixes or deletes broken triggers, registers new ones, updates its own memory and skills, and writes proposals for the morning chat. It is also where preferences are learned: it queries the history tables with `data_sql`, starting from where the last dream stopped, looks for repeated actions by a person in a similar context, for corrections (someone changing what huvu did a few minutes later), and for manual actions a trigger could do, checks each candidate for support, consistency and recency, keeps the claim in memory with the query that supports it, and proposes it in the morning chat. A trigger is registered only after the person says yes. The dream prompt itself is not written yet.
1. **Time-only schedules**, as Hermes cron jobs.
1. **What it learns.** Which tools and values it uses often, that `state_search` exists, what you call the hall light. This goes in its own memory and skills, written during the dream. Little goes into the model's memory beyond that.
1. **The situation block** at the start of a session: time, where you are, the house, pending approvals. The huvu Hermes plugin adds it on a session's first turn, from `POST /api/situation`. The values in it are set in the plugin's environment (`HUVU_SITUATION`).

### Settings

1. Background review off (`auxiliary.background_review.enabled: false`). It is not per turn: it runs every 10 user turns (`memory.nudge_interval`) or 10 tool iterations (`skills.creation_nudge_interval`), and each run forks the agent at about 30K tokens. The dream does the same work once a night.
1. The skill curator stays on. It runs weekly, when idle for 2 hours, and archives agent-made skills nobody uses.
1. In every profile, the huvu plugin is enabled (`plugins.enabled: [huvu]`), and the backend is an MCP server named `huvu` in `mcp_servers`, with `url: http://<backend>/mcp` and the profile's own `HUVU_MCP_TOKEN` as the bearer header. See `hermes-plugin/README.md`.

### The huvu plugin

Hermes connects to an MCP server once, with fixed headers. The profile's token says which user is calling, and the huvu Hermes plugin (`hermes-plugin/huvu/`) carries the session across the boundary. Its `pre_tool_call` hook adds `_session` to every huvu tool call. The backend strips it and looks the session up in `hermes_sessions`, which names its device. A session that belongs to another user than the calling profile's is ignored. The plugin also adds the situation block, see [What Hermes owns](#what-hermes-owns), and a system prompt section with huvu's guidance: how to find and work things, provenance, approvals, pausing noisy triggers, telling the person about changes. Hermes never shows the model an MCP server's own instructions (it keeps the initialize result only for capabilities), so the plugin's section is the one place huvu can say these things.

Session taint was removed on 2026-10-04. It followed OpenClaw's taint gate: a session that had read `external` content got stricter policy, its triggers waited for the owner's tap, and its memory and skill writes went to Hermes's approval gate. It was judged more complexity than current models need. Two guards stay without it. Provenance labels still mark outside content for the model, and `requires_human` actions always need a person's tap.

### MCP tools

Hermes names each one `mcp__huvu__<name>`. Hermes sees only the kernel's tools; a source's actions and queries run through `act` and `query` by name, so a new driver adds nothing to a session's tool list.

| Tool | What it does |
|---|---|
| `state_get` | Reads values |
| `state_search` | Finds values by name, area or alias |
| `events_search`, `events_get` | Full-text search over the event log, and one event |
| `events_digest` | A day's or an hour's digest |
| `sources_list` | Every source with its values, events, queries, actions and tables |
| `data_sql` | One read-only SQLite query over the history tables and the event log, scoped to the caller. See [History tables](#history-tables) |
| `triggers_register`, `triggers_test`, `triggers_list`, `triggers_remove`, `triggers_pause`, `triggers_resume` | Trigger management |
| `act`, `query` | Run a source action or query by name (`ha_turn_on`), with its args. Every action takes an optional `reason`. A wrong name or argument comes back with the closest name or the right signature |
| `ui_choices`, `ui_show_feed`, `ui_show_image` | Parts for the frontend, added to the live turn of the calling session |
| `people_identify`, `people_release` | On a shared device: ask a family member on their phone whether it's them, or forget who is there |

A result with outside content comes back as `{"data": ..., "provenance": "external"}`, and the plugin's section tells the model to treat text in it as data. Scripts still call the per-source tools by name (`home_turn_on`), so a trigger's grants stay per action.

### Skills

Every source is a Hermes skill, `huvu-<source>` (`skills.py`). Its body is the driver's `guide` followed by a reference huvu generates from the spec: actions with their arguments, queries, values, events, and history tables with their columns, the standard columns and the indexes, which is the data dictionary the dream works from. A session sees one line per skill in its skill index, about 30 tokens and cached, and loads a skill's body with `skill_view` when it needs that source. So what huvu can do grows with drivers without growing every session's prompt.

huvu writes the skills into its data folder at every start and at every respec (`skills_dir`, default `<data_dir>/skills`), and links each into the repo's gitignored `hermes-skills/` (`hermes_skills_dir`), making the folder and repairing the links as needed and removing those of sources that are gone. Every profile reads `hermes-skills/` through `skills.external_dirs` (`setup.sh` sets it), so nothing needs setting up again; Hermes follows the links (`os.walk(followlinks=True)`). Anything else in the folder is left alone. The files are read-only: Hermes lets the model edit a skill wherever it lives, and huvu would overwrite the edit at the next start, so what the model learns about the house goes into its own skills and memory. This needs Hermes to read the backend's disk; if the two ever run on different machines, the plugin would fetch the skills instead.

## Sessions

Hermes's API server runs many sessions. Its session API (`POST /api/sessions`, then `POST /api/sessions/{id}/chat/stream`) streams `assistant.delta`, `tool.started` (with the tool's full arguments), `tool.completed` (with a result preview capped at 500 characters), `tool.failed` and a final `run.completed`, `run.failed` or `run.cancelled`. Concurrent turns on one session are serialized by Hermes.

The backend multiplexes on top of that (`conversations.py`):

1. A device's conversation maps to one Hermes session, in its user's profile or the default one.
1. Every trigger wake starts a fresh Hermes session in the trigger user's profile, with the wake payload (`wake.py`). Its reply goes to the log as a `wake.done` event. If the agent wants to reach the user, it calls `huvu_notify`. Two wakes run at once at most.
1. A turn streams to the client that asked, and to the same device's other push streams. It never reaches another device.
1. Hermes's own sessions (the dream, the morning chat) are its business. A chat Hermes starts reaches the frontend through `huvu_notify`.
1. **The prompt cache decides when a session continues.** The provider keeps a session's prompt cached for 5 minutes after its last use (`CACHE_TTL` in `conversations.py`). An ask within that window continues the same Hermes session and hits the cache. An ask after it starts a fresh session, primed the way a compacted session is: the asks and answers so far, with a note telling the model to carry on as the same assistant without acknowledging it. Thinking and tool output are left out, since with the cache cold they would cost full price to replay. Every `conversation.turn` event carries the run's `usage`, cache reads included, to check this against the provider.
1. **Edit and retry rewind**, always through a fresh session with the transcript of the turns that stay. Hermes can fork a session but its API can't cut one short; its CLI and chat gateways can (`/undo`, `rewind_user_turn`), so a rewind endpoint upstream would let a rewind within the cache window keep its cache. Both sessions stay until the sweep.
1. **A client joins a turn from its start.** A delta doesn't say where its text goes, so a client that opens while a turn runs can't add later deltas to a snapshot. `GET /api/turns/{turn}/events` replays the turn's events so far from the turn's own history, then follows it live, the way the turn's own stream does.
1. **Stop lets Hermes finish.** A stop asks Hermes to stop and keeps relaying until Hermes's stream ends, so the turn has every token Hermes produced and ends the way Hermes reports: complete if it finished anyway, stopped if it was cut off. Only a Hermes that hasn't ended 15 s later is cut off by huvu. The frontend and the backend agree on how every turn ended.
1. **Old sessions are swept.** Once a day, the Hermes sessions huvu started that haven't been used for 30 days are deleted through Hermes's API. The event log and the turns huvu keeps for the stage stay. Hermes's own sessions (cron jobs, the dream) are untouched, and Hermes prunes nothing by itself (`hermes sessions prune` only touches ended sessions).

The relay turns Hermes's events into frontend events. Hermes's housekeeping tools (memory, skills, session search) and huvu's reads stay hidden. huvu's actions show as notes. Hermes tools with an adapter (`web_search`) get the fields the stage reads; any other tool shows as a generic note.

## Code layout

```
backend/
  pyproject.toml, huvu.toml.example
  src/huvu/
    spec.py           the contract's types: SourceSpec, ValueSpec, TableSpec, Event, Reading, labels, Caller
    data.py           the history tables (analytics.db) and data_sql
    accounts.py       users, devices, pairing, tokens, each user's Hermes profile
    attendance.py     who is at a shared device, confirmed from their phone
    skills.py         every source as a Hermes skill
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
    native_push.py    FCM tokens and the sender, for phones whose app is closed
    media.py          media URLs for refs
    http.py           the HTTP server
    auth.py, config.py, db.py, times.py, kernel.py, __main__.py
    builtin/          the clock and huvu sources
  src/huvu_drivers/   the drivers, one package or module per source
    ha/               Home Assistant: client.py (WebSocket), scripts.py (scripts as actions),
                      history.py (who caused a change, sensor buckets), source.py (the driver)
  tests/              fakes.py (a fake house and Hermes), fake_ha.py (a fake Home Assistant)
hermes-plugin/huvu/   the Hermes plugin
```

Run it with `uv run huvu` from `backend/` (it reads `huvu.toml`, or `$HUVU_CONFIG`), and the tests with `uv run pytest`.

## Open questions

1. **Rewind endpoint in Hermes (a PR to write).** `POST /api/sessions/{id}/rewind` with `{"turns": N}`, wrapping `rewind_user_turn` the way the CLI's `/undo` does. huvu would then rewind within the cache window in place, and fall back to a transcript only after it.

1. **Strict tool schemas.** Check if Hermes uses OpenAI's strict mode for the Codex provider. Enums of value names would make made-up names impossible.
1. **Pulsar.** The Tuya consumer has never run. See `front-door-lock.md`.
1. **Trigger evaluation at scale.** Untested with fast inputs like location and glasses frames. Those sources filter before they emit.
1. **The plugin against a live Hermes.** The hooks were tested the way Hermes's source calls them, not inside a running Hermes.
1. **Profiles against a live Hermes.** The `/p/<profile>/` routes and per-profile keys come from Hermes's source and docs. No backend call has gone through them yet.
1. **Native push against a real Firebase project.** Phones register FCM tokens, and notifications, pending approvals and "is it you?" questions go to them through FCM's HTTP v1 API (`native_push.py`), signed with the service account key in `HUVU_FCM_CREDENTIALS`. It was tested with a fake FCM only. iOS also needs the APNs key uploaded to the Firebase project. A phone that connects later still catches up from the lists (`GET /api/approvals`, `/api/identify`, `/api/notifications`, which keeps 3 days).
1. **Finer roles.** Groups have two roles, member and guest. Kids who may approve the porch light but not the front door would need a third, or per-action rules.
1. **Turns across a restart.** A turn running when the backend stops ends as failed. Hermes may still finish it, unseen.
1. **The HA driver against a live HA.** It was tested against a fake HA that answers the way HA's WebSocket API is documented and coded. These were taken from HA's source and not seen on a live instance: `homeassistant/expose_entity/list`, `script/config`, `config/entity_registry/get_entries` (for aliases), `return_response` on a script call, and that a script's own service calls run in the script's context. `ha_problems` and the driver's warnings show the first of these going wrong.
1. **History retention.** History rows are kept for good. At the sizes estimated (a few hundred change rows and about 14,000 sensor rows a day) that is a few million rows a year, which SQLite handles; if it grows past that, old sensor buckets roll up to hours.

## Build order

1. The HA driver: built on 2026-10-06. Left: the HA side (`home-assistant.md`) and a first run against the live HA.
1. The Tuya driver (Pulsar first, then the album fetch and decryption). Checked by pointing Hermes at the MCP server, ringing the doorbell, and asking who rang. Its unlocks go into a history table too.
1. The rest of the history work under [Not built yet](#not-built-yet), starting with the writable schema and backtests.
