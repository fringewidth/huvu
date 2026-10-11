# Backend architecture

The design of huvu's backend, as decided on 2026-10-02 and 2026-10-03, with users and devices added on 2026-10-04, history tables and the Home Assistant driver on 2026-10-06, and everything read through SQL on 2026-10-09: what is true now is a live table, events are history rows, conversations are a source, and a trigger is a query. The kernel (everything except the drivers) was built on 2026-10-03 in `backend/`, with the Hermes plugin in `hermes-plugin/`. The first driver, Home Assistant (`huvu_drivers/ha/`), was built on 2026-10-06 and has only run against a fake HA. The frontend side of the boundary is in `frontend-contract.md`. What Home Assistant itself must look like for huvu is in `home-assistant.md`.

The backend is an interface to data, a holder of state, and the link to the frontend. It owns nothing about the model. Hermes is the agent, and everything to do with prompts, memory, schedules and sessions belongs to Hermes.

## The boundary

| Backend | Hermes |
|---|---|
| Sources and their tables: history, and what is true now | The model, prompts, the system prompt |
| Triggers, the only scheduler: a query over the tables, the time included | What to do when a trigger wakes it |
| The action policy and pending approvals | Memory and skills |
| Provenance labels on data | What it trusts, and its own memory writes |
| The MCP server and its formatting for the model | What to look up, and when |
| Frontend sessions, and turning Hermes output into frontend parts | Its own sessions |
| Users, devices, and which profile serves whom | One profile per user, with that user's memory and skills |

Hermes reaches the backend through the backend's MCP server. The backend reaches Hermes through Hermes's API server: it starts sessions for chats and for trigger wakes there.

## Stack

Python, managed with uv. asyncio for the long-lived connections (Pulsar, the HA websocket). Pydantic for every schema, since its JSON Schema output is what sources publish. FastAPI for the HTTP surface. SQLite for the kernel's own state (accounts, triggers, approvals, the stage's turns), and a second SQLite file for the history. Every SQLite file is opened through APSW, which can define the live tables as virtual tables; Python's `sqlite3` isn't used. The official MCP Python SDK for the MCP server. `websockets` for the HA connection.

Python over TypeScript or Go, because the ML pieces (face embeddings, Whisper, vision models) ship as Python first, Hermes is Python, and the tested Tuya code is Python. One language for the backend and any Hermes plugins.

**Everything is SQLite** (decided 2026-10-06). The kernel's database, the history tables, and any database a driver keeps for itself are SQLite files. No other database engine is used. What would push toward another engine is served by SQLite extensions instead: FTS5 for text search, the JSON functions for nested data, `sqlite-vec` for embeddings when semantic search comes. One engine means one query language for the dream and for triggers, one way to back up `data/`, and joins across sources in one query. The question came up because some sources hold data that isn't tabular (images, audio, free text). That data stays in the source. What the source records is rows of facts about it with a ref to the raw content, see [History tables](#history-tables).

## Users, groups and devices

Each person is a user with a username and a password, added from the command line (`uv run huvu user add`); nobody signs up. Each install of the app is a device (`accounts.py`).

1. **A personal device** (a phone, a browser) belongs to the user who logged in on it.
1. **A shared device** (the Fire TV, the speaker hardware still to be built) belongs to a group. It can't type a password, so it pairs: it shows a code, and a member confirms it from their phone, choosing the group if they're in more than one.
1. **A tracker** is OwnTracks on a user's phone, added from the command line (`uv run huvu tracker add <user>`). Its token works on `POST /api/ingest/{source}` and nowhere else, and it is revoked apart from the phone's login. See [Location](#location).
1. **Every device has its own token**, stored hashed, so a lost phone is logged out alone.
1. **Phones are taken to be on their user's person.** Approvals, "is it you?" questions and notifications go to phones.
1. **Every device has one conversation**, and each conversation runs on one Hermes session. So a session belongs to a user and a device, or to a group on a shared device. A turn never reaches another device.

### Groups

A group is a set of people who share devices and trust each other. The household is the default group, `home`, which always exists. A second house would be another. A user is in a group as a member or a guest:

1. **Members** trust each other implicitly. They see the group's rows, answer its approvals, and pair and unpair its shared devices. Any member can add a shared device to the group; a shared device never pairs to a user.
1. **Guests** (a babysitter, a visitor) can use the group's shared devices and be recognized at them. They trust nothing: they see only their own rows, and they never approve anything. Every approval a guest causes, even with a tap on their own phone, goes to the group's members.

A user can be in several groups, or none. The CLI puts a new user in `home` as a member unless told otherwise (`--group`, `--guest`, `--no-group`), and `huvu group add`, `join`, `leave` and `list` manage the rest. Groups are made only from the command line for now; making them in the app is still to do (`v0.md`).

### What people call each other

Each user can have an alias for each other user (`people_aliases`): the owner calls their mother "mom", and she calls him by a pet name. With no alias, the person's name stands in. `setup.sh` asks for aliases both ways when it adds a user at a terminal, and `huvu user alias <owner> <person> <alias>` changes one later. The model sets its own person's with `huvu_set_person_alias` (silent: it changes only what that person's agent and phone call someone), such as when they say "I call her Amma". An alias names one person only: it can't be what the owner calls anyone else (their alias, or their name when there is none), another user's username, or a group's id. A new user or group can't take a name someone already uses as an alias. User and group ids never match, since both name Hermes profiles.

The model learns who someone means from `people_group`, which lists the caller's groups with each person by the caller's alias, and then sends with `huvu_message`, naming the person by that alias or a group by its id. A message goes to people who share a group with the sender, guests included, or to the members of a group the sender is a member of, without the sender. Only a person can send one: on a shared device, someone must be recognized first. The recipient's phone shows it from the sender by the recipient's own alias for them (`from` on the `notify` push, and the native notification's title). Its autonomy is `notify`, so the sender gets a note that it went. A message to one person is a `huvu_notifications` row for each of the two; one to a group is the group's row.

### Who is at a shared device

A shared device is often in front of one person. When someone asks who it is (the device's "who's this?" picker, the model's `people_identify`, later voice recognition), huvu asks that person on their phone: "is it you on the living room TV?" A yes makes them the device's attendee (`attendance.py`).

The Hermes session doesn't change profile, since Hermes can't switch a session's profile and a new session would throw away its prompt cache. What changes is everything the backend controls:

1. Rows from the device (its chat turns, huvu's log about it) become the person's, back to the moment they were asked. Their dream reads them in their own profile.
1. The session's tool calls act for them: a trigger is theirs at once, and approvals and notifications go to their phone. A tap on the device still goes through the policy.
1. The model gets a note on its next ask, appended to the message, so the cache holds.

It ends when someone else is confirmed, when it's released from the device, the person's phone or the model, or after 10 minutes with no turn.

### Hermes profiles

Each user has their own Hermes profile, named after their username. Each group's shared devices run in the group's profile: the default group uses Hermes's default profile, and any other group a profile named after it. Users and groups share one namespace for that reason. A profile has its own memory, skills, sessions and cron jobs, so one person's preferences never reach another's answers. One Hermes gateway serves every profile: a profile's routes live under `/p/<profile>/`, each with its own API key. Knowledge everyone shares lives in the backend, where every profile reads it through MCP.

Each profile sends huvu's MCP server its own token, so the backend knows whose Hermes is calling, even for a session the backend never saw. Users' and groups' profile keys and tokens live in the database. The default group's come from the environment (`HERMES_API_KEY`, `HUVU_MCP_TOKEN`).

## Sources

Every data source is a pluggable module. A source owns its own working storage and processing. The Tuya source keeps its decrypted snapshots where it likes in its folder, and a source that needs a database of its own uses SQLite. Everything a source knows reaches the model and the triggers as tables, in two kinds: history tables, whose rows the source records into the kernel's history, and live tables, which the source answers each time a query reads them. See [Tables](#tables).

### The contract

A driver subclasses `huvu.source.Source` (`backend/src/huvu/source.py`). Every method has a default, so a driver implements only what it has.

```python
class Source:
    def __init__(self, options: dict): ...                    # from the config file
    def describe(self) -> SourceSpec: ...                       # required
    async def run(self) -> None: ...                            # long-lived part
    async def act(self, name: str, args: dict, caller: Caller) -> Result: ...
    async def fetch(self, ref: str) -> Blob: ...
    async def feeds(self) -> list[Feed]: ...
    async def ingest(self, body: Any, caller: Caller) -> Any: ...
```

1. `describe` lists the source's tables and actions, each with a schema and a one-line description, plus `guide`: the hand-written part of the source's Hermes skill (when to use it, its use cases, the gotchas). The types are in `spec.py`. The kernel binds the source before it calls `describe`, so `describe` can read the source's own folder.
1. `run` is the long-lived part: a Pulsar consumer, a websocket, a polling loop. It hands the kernel what happened with `ctx.record(table, row, ...)`, and what is true now with `ctx.replace(table, rows)`, `ctx.put(table, row)` and `ctx.drop(table, key)` (see [Live tables](#live-tables)). If it raises, the kernel logs a `source_error` row and starts it again with a backoff (5 s, doubling to 5 min). A run that lasted at least a minute resets the backoff to 5 s, so a source that loses its connection once a month (HA restarting for an update) comes back in 5 s, not 5 min.
1. **A driver only hands over data** (decided 2026-10-09). Drivers are code the kernel doesn't trust to do more, so the kernel never calls into one to answer a query, checks every row a driver hands over against its spec, owns the copies a query reads, and tells the triggers about every change itself. A driver can't forget a nudge or get threading wrong, because it does neither. The kernel calls a source only for what is the source's own: `run`, `act`, `fetch`, `feeds` and `ingest`. A first design had the kernel call `Source.live()` from a query's thread, which left every driver to copy its dicts before reading them; it was dropped the same day. Drivers still run in the backend's process, so this guards against mistakes, not against a hostile driver; that would need drivers in their own process (see the HTTP note below).
1. `act` covers writes; the kernel calls it only after the action policy allows it. It gets the `Caller`, whose `user` says whom the call is for and `group` which group's shared device it came through, so a source with per-person data, such as a calendar, acts on that person's.
1. **One way to read** (decided 2026-10-09). What is true now is a live table, what happened is a history table, and both are read with `data_sql`, by the model and by triggers alike. This replaced values (`state_get`, `state_search`, `on_demand`, `max_age`, the stored readings) and the event log (`ctx.emit`, `events_search`, `events_digest`). Queries were removed on 2026-10-06, for the same reason: one read path means one scoping rule, and no per-source read tools the model has to learn.
1. `fetch` returns the binary content behind a ref (`tuya:album/8f2c`), such as a decrypted snapshot. The frontend gets it through a media URL.
1. `feeds` lists live feeds the source can stream, for the frontend's list.
1. `ingest` takes data a device posts to the source, for a source whose data arrives from outside instead of being fetched, such as a phone's location. The route is `POST /api/ingest/{source}`, authed by the device's token (a bearer header, or Basic auth with the token as the password, for apps that can't set headers). `caller.user` is whose device it is. What `ingest` returns is the reply's body. A source without it answers `404`.

`self.ctx` also gives a source its own folder (`ctx.data_dir`), a logger, `ctx.now()`, and `ctx.rows(table, where, args)`, which reads the source's own history rows back, everyone's, for a source that works from its past (the location source's latest fix). Its `where` comes from the driver, so it runs on a read-only connection that can read nothing but that source's own history tables, one statement at a time. A table a source records into must be a history table in its spec, and so must every column of the row.

**A spec can change while the source runs.** The HA driver's actions are the scripts in Home Assistant, and those change when someone edits HA. The source calls `ctx.respec()`, and the kernel (`Kernel.respec`) calls `describe` again, adds any new history tables and columns, rebuilds the tool box, rewrites the skills, and logs a `source.respec` row that names the actions added and removed. The MCP tool list doesn't change, because source actions run through `act`, so no Hermes session loses its prompt cache. A trigger script whose grant names an action that is gone fails at its next run, as a `trigger.error` row the dream reads.

Every argument and result is plain JSON. Sources start as in-process modules. A heavy source can later move to another machine behind HTTP (`GET /describe`, `POST /live/{table}`, `POST /act/{name}`, rows pushed to the core) without changing anything else.

### Provenance labels

Every table carries a provenance label in its spec, and a query's result carries the labels of every table it read:

1. `owner`: something the user said or did.
1. `house`: device and sensor data, such as event times and device states.
1. `external`: content a stranger controls, such as what a doorbell snapshot shows, what the glasses see, a web page, or a message from someone else.

A doorbell press's time is `house`, but what its snapshot shows is `external`, so the doorbell's table is labelled `external`. The labels are facts about the data, so they belong to the backend. A tool result with `external` content reaches the model marked as such, and so does a wake from a trigger whose query read such a table, see [MCP tools](#mcp-tools).

### Built-in sources

Two sources are built into the kernel, behind the same contract:

1. **`huvu`**: what no device owns. Its live tables are the family mode (`huvu_mode`), the aliases between what people call a thing and what it is (`huvu_aliases`), pending approvals (`huvu_approvals`) and the triggers (`huvu_triggers`). Its history tables are the kernel's own log (`huvu_events`, see [The kernel's log](#the-kernels-log)), every change of the mode (`huvu_modes`) and every notification huvu sent (`huvu_notifications`), messages between people included, with their `sender` and `recipient`. A trigger reads `huvu_mode` the same way it reads `ha_entities`.
1. **`chat`**: every conversation turn, as `chat_turns`. The kernel records a row when a turn ends, as its user's, or its group's on a shared device nobody was recognized at, with the turn's number in its conversation (`n`), the ask (`them`), the answer (`you`), the device, the app actions that rode with it and the model's token counts (`usage`). Rows are never removed: an edit rewinds the conversation in the app, and the new turn's row names the turn it replaced (`replaces`), so what an edit dropped stays readable. A fresh Hermes session reads only the latest turns (see [Sessions](#sessions)), and the model reads the rest here.

A derived source, such as "who's home" from phone location, ESPresense rooms through HA and the last unlock, would read other sources' tables with SQL. None is built.

### The Home Assistant driver

Decided and built on 2026-10-06 (`huvu_drivers/ha/`). How to set up HA for it, for the agents that do that work: `home-assistant.md`.

1. **huvu works every exposed device directly, and the household's scripts are its routines.** Two kinds of action. Each exposed entity in a domain huvu can work (light, switch, fan, button, scene, media player, climate, vacuum, number, select, cover, lock, and the `input_` helpers) is one action named after it (`light.theatre_ceiling` is `ha_light_theatre_ceiling`), with arguments taken from its attributes (a climate's modes, a select's options, a number's range), and a call becomes one or more service calls on that entity alone (`entities.py`). A service call that fails inside its integration (HA's `unknown_error` or `home_assistant_error`, such as Tuya's cloud answering "sign invalid" now and then) is sent once more after a second, unless it is relative to now (a press, a step of volume, the next track), since sending one of those twice could undo it. A timeout is never retried, since HA may still be doing the first (added 2026-10-10). Each exposed script is one action too (`script.bedroom_ac_set` is `ha_bedroom_ac_set`): its description becomes the action's, and its fields' selectors the input schema (`scripts.py`). Scripts are for routines that work several devices, such as starting the home theatre. Infrared devices become proper entities in HA (a template button per learned code, a template switch for power), so they are worked the same way. Until 2026-10-09 scripts were the only way, since most devices are infrared and HA's own services said little about them; the owner reversed it that day, so "projector off" and "lights off" need no script each. `ha_entities.action` names the action that works each entity. The skill lists every action, one line each (its call, then the name, area and aliases; a script's description), so each is one call with no search first.
1. **Labels set the autonomy.** `huvu_silent`, `huvu_notify`, `huvu_ask`, `huvu_human` (which is `requires_human`); the strictest label wins. A script with no huvu label asks, so a forgotten label costs a tap. An entity takes its own label, else its area's (so a whole room can ask first), else its domain's default: silent for lights, switches, fans, buttons, scenes, media players, numbers and selects; notify for climates, vacuums and covers; ask for the rest. Locks, alarms, and garage, gate and door covers always need a person's tap, whatever their labels say. Because each entity and each script is its own action, the policy needs no kernel change.
1. **What HA exposes to Assist is what huvu sees** (`homeassistant/expose_entity/list`). An entity that isn't exposed can't be read, searched or worked. The family manages it in HA's UI, and there is no second list in `huvu.toml`. The list is shared with HA's own Assist. `exposure = "all"` reads everything, for development.
1. **Three live tables.** `ha_entities` is every exposed entity now: its name, domain, device class, area, device, aliases, the action that works it, state, the state as a number (`number`, NULL when it isn't one, so comparisons work), its attributes and whether HA only assumes its state. `ha_scripts` is every exposed script with its action name, area, fields and the entities its config names (`targets`), so the model finds how to work a device where it finds the device. `ha_setup` is one row: whether HA is connected, its version, and what is wrong with the setup (scripts or entities left out, missing descriptions, unknown labels, a token on a family member's user). The driver keeps every exposed state in memory and answers from it; nothing is written per state change but the history row. While HA is disconnected, `ha_entities` fails with a reason rather than answer from a stale mirror, so a trigger never fires on a stale value.
1. **The actions can change while huvu runs.** The driver reloads the registries, the exposure list and the scripts on any registry event, and every 5 minutes for edits HA sends no event for, and calls `ctx.respec()` when the actions differ. It caches the scripts and the entity actions in its folder, so the actions exist before HA connects.
1. **Every change says who caused it.** HA's context on each change gives four actors: `huvu` (huvu's token belongs to its own HA user), `ha_user` (someone in the HA app, mapped to a huvu user by the `users` option), `automation` (no user but a parent context), and `device` (neither: a switch, the device's own button). A change inside a script run takes the run's actor. For huvu's own calls the driver also knows the huvu user, because it matches the change to the call it has in flight (on that entity, or the one call in flight). huvu's token must belong to a separate HA user, or huvu's actions would look like that person's, and that user must be an administrator to read the registries.
1. **History** goes into three tables: `ha_changes` (every state change of an exposed entity, and every change of an attribute a person chooses, such as brightness or a target temperature, with the actor), `ha_script_runs` (every run, by anyone; the arguments only for huvu's runs, since HA doesn't report them for others), and `ha_sensor_5min` (numeric sensors as time-weighted 5-minute buckets, with a row per bucket even when nothing changed, so the latest row before a time is the value then). Buckets live only while the connection does, so a gap in the connection is a gap in the rows.
1. **Every state change reaches the triggers at once.** The driver hands over the entity's new row (`ctx.put("entities", ...)`) on every `state_changed` HA sends, before it records any history row, so a trigger on `ha_entities` (a temperature above 30) runs at once, and one that joins `ha_changes` with `ha_entities` sees the new state. On connect and every refresh it hands over every entity; on disconnect it marks `ha_entities` unavailable, so no trigger acts on a stale mirror. History before huvu connected is in HA's recorder only, which huvu doesn't read; nor do changes made while huvu was down.
1. **Infrared is a gap in the data.** A press on a physical IR remote never reaches HA, so the dream would only learn from what huvu itself did. A power-metering plug on each IR device worth learning about closes the gap: its watts show the real state, and the change shows as `device`.
1. **Not built**: cameras and their snapshots, live feeds from HA's go2rtc, and naming the automation behind an `automation` change.

## The kernel's log

The kernel's own events are rows of the `huvu` source's `huvu_events` table, recorded with `Kernel.kernel_event`: actions asked for and run, approvals, trigger runs and errors, wakes and their replies, attendance, sessions swept, sources respecced and restarted. Each has a `type` (`trigger.run`, `wake.done`, `action.requested`, ...), the `entity` it is about, a one-line `summary`, `refs`, small `data`, its `labels` and the `device_id` it came from, plus the standard columns. It scopes itself like every table: actions, approvals, trigger runs and wake replies carry the user they were for, so each user's dream reads its own. The wake limit and the busy notice count from it, so a restart resets neither.

Sources have no events of their own any more (decided 2026-10-09). What a source would have emitted was always a row of its history too (`ha.state_changed` was an `ha_changes` row, `location.arrived` a `location_transitions` row), so it records the row, and a trigger reads the table. The digest and full-text search over the log went with it; the dream aggregates in SQL.

## Tables

Everything a source knows is a table, named `<source>_<table>` wherever SQL reads it (`data.py`). There are two kinds.

### History tables

Decided and built on 2026-10-06. huvu's agent is expected to learn each person's preferences on its own, mostly in the nightly dream, by doing real data engineering over the house's data. So every source keeps history tables: typed rows, one per thing that happened, kept for good.

1. **A source declares its tables** in its spec (`TableSpec`: a name, a description, typed columns with descriptions, and indexes) and **writes rows** with `ctx.record(table, row, at=..., user=..., group=...)`. The column types are text, integer, real, boolean and json.
1. **The kernel keeps every source's history in one SQLite database**, `<data_dir>/analytics.db`, as `<source>_<table>` (`ha_script_runs`). One database is what lets one query join sources. `huvu.db`, with the accounts and tokens, is never attached to a query.
1. **Every row gets the standard columns**, filled in by the kernel, never by the source: `id`, `at` (ISO with the local offset), `ts` (seconds since the epoch), `local_date`, `local_minute` (minutes since local midnight), `weekday` (1 is Monday), `user_id` and `group_id`. These are the shared keys every join across sources uses, and computing them in one place means two sources can't disagree on what "22:30 on a Tuesday" is. `id` numbers rows in the order they were recorded, across every table, so a trigger can tell which rows it has seen, even one a phone sent late with an old `ts`. Every table is indexed on `(id)`, `(ts)` and `(user_id, ts)`, plus the indexes its spec names.
1. **Every row nudges the triggers.** Recording a row runs, at once, the triggers that read that table (see [Triggers](#triggers)).
1. **Rows are never deleted, except by a wipe, and columns are never dropped.** A newer driver that declares a new column gets it added to the table (`ALTER TABLE ADD COLUMN`), and old rows have it as NULL.
1. **Raw content stays in the source.** For data that isn't tabular (a doorbell snapshot, a voice clip, a long message), the row holds the facts about it (when, who, what was recognized) and a ref to the content, which `fetch` returns.
1. `user_id` and `group_id` say whose row it is, and with neither, the house's (`scope.py`): a user's row shows only to them, a group's to its members, the house's to every member of any group. Guests see only their own.

### Live tables

Decided and built on 2026-10-09. A live table holds what is true now: `ha_entities`, `location_people`, `huvu_mode`. The kernel keeps it, in memory, never on disk. Its source hands the rows over: `ctx.replace(table, rows)` for all of them (every entity once HA connects), `ctx.put(table, row)` and `ctx.drop(table, key)` for one row by the table's `key` (one entity's change), and `ctx.unavailable(table, reason)` while it can't vouch for them (HA disconnected). A query reads them as SQLite virtual tables, registered on each query's connection through APSW, since Python's `sqlite3` can't define them.

1. The kernel checks every row against the spec: unknown columns, a duplicate key and a `user_id` that isn't a name are refused, and each value is stored in its column's type.
1. Each change publishes an immutable snapshot of the table, swapped in whole. A query's worker thread reads that one reference, so it needs no lock and never sees half a change.
1. A table is unavailable until its source first hands it over, and while the source says it is: a query that reads it fails with the reason and the table's name. An empty `location_people` would say nobody is anywhere, so empty and unknown are kept apart.
1. Every change runs the triggers that read that table, from the kernel's own write path, so none is missed and no poll is needed.
1. They replaced values: a value's freshness (`max_age`) is a WHERE on a timestamp column, and a search by name is `LIKE`.
1. A live table has `user_id` and `group_id` like history, and the same scope view goes in front of it, so a person's location row shows to nobody else.
1. Every SQLite connection in the backend is APSW's (`db.open_db`, and `data.py`'s query connections), for every file. APSW brings its own copy of SQLite, and two copies in one process don't see each other's locks on the same file, which can corrupt it (it showed up as a bus error in tests, 2026-10-09). So Python's `sqlite3` was removed from the backend, rather than kept to some files by a rule. A `sqlite3` shell or a backup in another process is fine: locks between processes work.

### data_sql

The `data_sql` tool runs one read-only SQLite statement for the caller and returns its columns and at most `limit` rows (200 by default, 1000 at most), with the labels of every table it read. It is the only read tool: how a session finds a device, reads what is true now, and analyses the history, and how a trigger decides. Each source's skill lists its tables and columns, so the model knows the schema without a lookup.

The query runs on its own read-only APSW connection, on a worker thread, so a long query never blocks the event loop; WAL lets it read while drivers write. Before the query runs, the kernel:

1. Registers every live table on the connection.
1. Puts a temporary view in front of every table, history and live, with the same name, that keeps only the rows the caller may see (`scope.py`). A view named like the table shadows it, so queries use the plain names.
1. Adds `now_ts()`, `now_date()`, `now_minute()` and `now_weekday()`, the local time from the kernel's clock, with the same meaning as the standard columns. SQLite's own time functions read the wall clock in UTC.
1. Turns on `query_only`, and sets an authorizer. The authorizer refuses every read of the real tables that doesn't come through a view, so the raw tables and other users' rows are out of reach, and refuses writes, pragmas and `ATTACH`. SQLite's JSON table functions (`json_each`, `json_tree`) are allowed.
1. Prepares the statement without running it (`apsw.ext.query_info`), which refuses a second statement and says what the query reads.
1. Sets a progress handler that stops the query after 10 s, and waits up to 5 s for a lock the writer holds.

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
1. **`triggers_backtest`**: replays a trigger's query over history, so the dream can say "this would have fired on 11 of the 13 evenings you did it yourself" before it proposes the trigger.

## Times for the model

Every local time huvu computes is in the house's timezone (`timezone` in `huvu.toml`, or `HUVU_TIMEZONE`, which `setup.sh --timezone` writes; Asia/Kolkata by default): `kernel.now()`, the history's `local_date`, `local_minute` and `weekday`, `now_minute()`, reminders and the dream. Never the server's own zone, which is UTC in the container; until 2026-10-09 it was, and on the host a reminder for 9 would have come at 14:30. `setup.sh` sets the same zone as `timezone` in every Hermes profile, since Hermes tells the model the time too.

The core, sources, triggers and the frontend use ISO timestamps. The MCP layer turns every timestamp the model sees into a local time with its age:

1. Under an hour: `00:04, 6 min ago`
1. Earlier today: `14:30, 3 h ago`
1. Yesterday: `yesterday 19:55, 4 h ago`
1. Within the last week: `Wed 19:55, 3 days ago`
1. Older: `2026-09-24, 9 days ago`

The absolute time stays next to the age, because a tool result stays in the context after its age is out of date.

## Triggers

A trigger is a query and a script: one read-only SQL query over the tables, run as the trigger's person, and a Python script that runs when the query's answer changes (decided 2026-10-09). Hermes registers triggers through MCP, and the backend runs them. Triggers are huvu's only scheduler, decided on 2026-10-07: Hermes's cron toolset is off in every profile (`setup.sh`). A Hermes cron job made from the API server has nowhere to deliver its output, costs a model turn on every run, and never shows in the app. Every trigger has a script (decided 2026-10-08). The script does what needs no thought, such as a reminder through `huvu_notify`, with no model call, and starts a model instance with `ctx.wake(...)` on the fires that need one. So the choice between acting and waking is made in the script, and the model makes it while writing it, with no prompt to teach it.

```json
{
  "note": "Turn on the porch light. User asked for this on 2026-10-02.",
  "sql": "SELECT t.id FROM location_transitions t, ha_entities e WHERE t.changed = 1 AND t.event = 'enter' AND t.waypoint = 'near_home' AND e.entity_id = 'light.porch' AND e.state = 'off' AND t.local_minute < 300",
  "fire": "rows",
  "once": false,
  "script": "def run(ctx):\n    ctx.call('ha_porch_light_on', {})",
  "grants": ["ha_porch_light_on"]
}
```

This one runs when its person's phone enters the `near_home` waypoint between midnight and 5, if the porch light is off. An ETA would need a maps API inside the backend, so the waypoint's size stands in for it (see [Location](#location)). The join across two sources, with one of them live, is the reason triggers became SQL: the old JSON conditions could AND a few checks, never join.

1. `note` comes first. It says why the trigger exists and what to do. It goes back to the model in the wake.
1. `sql` is one read-only query, checked when the trigger is registered: a bad name fails with SQLite's message, and so does a second statement.
1. `fire` is `edge` or `rows`. **edge** runs the script when the answer goes from no rows to some rows, and rearms once the answer is empty again; a condition that already holds when the trigger goes live runs it at once. "Every day at 8" is an edge on `now_minute() = 480`, which rearms at 481. **rows** runs it for every row the answer holds that it hasn't seen, so the query must return the `id` of a history row; rows that existed at registration, or that came while the trigger was paused or waiting for its person, don't count. It replaced event checks, and it never loses a row: a row that comes while the script runs is picked up when the run ends.
1. `once` ends the trigger after its first fire, such as a reminder. A `once` trigger without `expires` gets 7 days, so a condition that never comes true doesn't stay forever.
1. `expires` takes ISO times or relative ones (`"2h"`, `"tomorrow 09:00"`).
1. The script gets the rows that fired it (`ctx.rows`, as dicts by column), so the model wakes with what it needs and makes no lookups.
1. `triggers_register` with `dry_run` registers nothing, and shows what the query reads, when it would run, the answer now, and what the script would do with it, with reads real and actions and wakes only recorded.

### Tiers

A trigger's `tier` picks the model its wakes run on: `basic` (default), `pro` or `advanced`, mapped to models in `[models]` (luna, sol and astra). A conversation's asks run on the tier the router picks (below), pro when it can't. The backend sends the model with each run (`model` on Hermes's session chat), and every wake is a fresh session, so no tier touches another's prompt cache.

An advanced trigger needs its person's yes before it goes live, wherever it was set. Nobody needs to be there when it is set: a conversation, a wake or a dream registers it the same way, as `awaiting`, and the kernel asks for `huvu.activate_trigger` (`requires_human`, a day to answer) for the trigger's user. Yes from their phone, or "allow" tapped in the menu on their own device, makes it live (`trigger.approved`); a no or no answer removes it. The model can ask for that action too, but its ask only opens another card.

### When a query runs

A trigger's query never runs on a schedule of its own. It runs when something it reads may have changed:

1. **A table changing.** A history row recorded, or a live table's rows handed over, runs at once every trigger that reads that table (`TriggerEngine.nudge`), and no other: a power reading's bucket row doesn't run the triggers on `ha_changes`. Several changes in the same moment cause one run. What a query reads is found when it is compiled, from SQLite's authorizer, without running it. Every change to a table goes through the kernel, so the kernel nudges itself, and none is missed.
1. **The time.** Triggers that read the time, through `now_*()` or SQLite's own time functions, run once a minute. Time conditions are precise to the minute; a timeout in seconds belongs in the script or an HA automation.

A 5-second poll of every trigger that reads a live table ran here for a few hours on 2026-10-09, as a safety net while live tables called into the drivers and a change could go unannounced. Once every change went through the kernel, it had nothing left to catch, and it went.

A query that fails (a live table whose source is down) changes nothing: an edge trigger keeps its state, and the failure is logged once, as a `trigger.error` row. Every fire is a `trigger.run` row.

### Runaway triggers

1. **One run at a time.** A trigger is running from the look that decides to fire until its script ends. A change meanwhile gets one more look when the run ends, so a condition that went false in between still rearms it, and rows that came meanwhile fire then. Each trigger has its own claim; one slow trigger doesn't hold up the others.
1. **The fire limit.** A trigger that fires more than 30 times in 5 minutes pauses itself, and its user is notified. It stops a trigger that feeds on its own runs, such as one that reads `huvu_events` for `trigger.run` rows.
1. **Pausing.** Any session acting for a trigger's user can pause and resume it (`triggers_pause`, `triggers_resume`), and so can the user from the app. A paused trigger keeps everything but stops running.
1. **The wake limit.** A trigger may wake the model at most 20 times in two hours, counted from `huvu_events` (`wake.started`), since its last resume if that is later. From the 10th wake on, the wake tells the model and asks it to handle the repeats in a script or pause the trigger until the person approves. The 21st doesn't happen: the trigger pauses itself, and its user is notified. A trigger either limit paused resumes only by the user's own tap.
1. **The busy notice.** A user whose triggers fired more than 10 times in the last hour, together, is notified, once an hour at most. It also catches a script-only trigger that never wakes the model.
1. Scripts never call the model themselves: `ctx.wake` asks the backend to, through the same waker and the same limit.

### Whose a trigger is

Every trigger belongs to a user. Its query runs in that user's scope, so it reads only what they may, and nobody can watch another person's arrivals through a trigger. Its wakes run in that user's Hermes profile, its scripts act as that user, and its approvals and notifications go to that user's phones. `huvu_triggers` and `triggers_remove` see only the calling profile's triggers.

A trigger set up from a shared device nobody is recognized at has no user yet, so it waits, unconfirmed, and doesn't run:

1. Everyone in the device's group, members and guests, gets "is this you on the living room TV?" on their phone, with the trigger's note.
1. The tool result tells the model the trigger goes live only once someone taps yes, so it tells the person to check their phone.
1. The first yes recognizes that person at the device (see [Who is at a shared device](#who-is-at-a-shared-device)), and the device's waiting triggers become theirs and go live. The other questions are withdrawn.
1. If everyone says no, or nobody answers within 5 minutes, the waiting triggers are dropped.

A trigger with nobody to ask (one registered from a group profile's wake, which has no device) is refused.

### Scripts

Every trigger carries a script: a Python function the model writes, run when the trigger fires. It can call the tools the trigger declared, `data_sql` included, and it ends by acting, waking the model, or doing nothing. The engine keeps the edges, the rows seen and expiry, so scripts don't rewrite them. The script lives in a file and runs in its own process (`script_host.py`), which talks to the core over its stdin and stdout, one JSON line per message. It can call only the tools named in the trigger's `grants`, through the same tool box Hermes uses, and actions still go through the policy. It has 30 s, and a script that doesn't exit after its run is killed. It is not sandboxed: the agent already has a terminal, and it's there to help. That also means read-only holds for queries only: a script, like Hermes's terminal, runs as the backend's OS user and could open the databases itself. Running the backend as its own user is the fix, left for later.

The dream reviews real runs from the `trigger.run` and `trigger.error` rows in `huvu_events`.

## Location

Decided on 2026-10-06. The driver is `huvu_drivers/location.py`.

Each family member runs OwnTracks on their phone, in significant-change mode, posting over HTTP to `POST /api/ingest/location` with Basic auth: their username and their tracker's token. `uv run huvu tracker add <user>` makes the tracker and prints a QR code, an `owntracks:///config?inline=` link that sets the app up in one tap. In this mode Android sends a fix about every 15 minutes from Wi-Fi and cell towers, and iOS sends one after about 500 m of movement, plus a ping now and then.

1. **Waypoints.** A waypoint is a named circle: a centre and a radius. It is OwnTracks's word, and huvu uses it everywhere. The phone watches its waypoints through the OS's region monitoring and posts a transition the moment it enters or leaves one, so arrivals come at once. huvu holds the master list. OwnTracks fetches nothing on its own, so huvu sends each tracker the waypoints its person sees as a `setWaypoints` command in the reply to its next post, whenever they changed since the last one sent. `setWaypoints` only adds and overwrites (OwnTracks keys a waypoint by its `tst`), so after a removal `clearWaypoints` goes first. Only iOS does that; a removed waypoint stays on an Android phone, and huvu ignores its crossings.
1. **Who owns a waypoint.** The person who named it, who can share it with a group. Members' phones then get it too. `home` and `near_home` (a few km around home) come from the config and belong to the household's group. One named at a shared device nobody is recognized at belongs to that device's group.
1. **Live tables.** `location_people` has one row per person, theirs alone: `place` (the smallest waypoint they're inside, or `elsewhere`), `inside`, the latest fix's position, accuracy and time (`fix_at`), `distance_home_km`, `battery` and `wifi`. `place` stays right while the phone is still; the rest is as old as `fix_at` says. The driver keeps these rows in memory, brought up to date after every post and filled in from the history at start. `location_waypoints` is the waypoints a person sees: their own, and those shared with their groups.
1. **Late and repeated posts.** OwnTracks retries a post that failed and may send a queue late. A fix or transition it sends twice is stored once. A crossing older than the latest one huvu knows for that waypoint is stored and changes nothing. A payload huvu can't read is logged and dropped with a `200`, since a refused post would jam the phone's queue.
1. **Storage.** Every fix and transition, with the raw payload, is a row in `location_fixes` and `location_transitions`, kept for good, with the person as `user_id`, so `data_sql` shows each person only their own. `location_transitions.changed` marks the crossings that changed where the person is, the arrivals and departures, with `stayed_min` on a departure and `delay_min` on both; repeats and late ones are kept too, with `changed` false. A trigger on an arrival reads these rows with `fire: rows`. The source's own `location.db` holds only what it works from: the waypoints, each person's latest crossing of each, and what each tracker was last sent. `location_waypoint_add` with `from` and `until` covers "remember where I was then": huvu takes the median of the person's fixes in that window, and reports how many it used and how far they spread.
1. **Privacy.** Location is private to its person, from everyone, members of their groups included: every row is the person's (`user_id`), so the scope views show it to nobody else, through `data_sql` or a trigger. Group membership means trust about the house, not about where someone is.
1. **Maps is not a source.** Routes, ETA and geocoding have no state and no events, so they are a Hermes tool. Triggers use `near_home` in place of an ETA.

## Actions and the policy

Every write goes through one policy check in the backend, whoever asks: Hermes, a script, or a frontend button.

1. Each action has an autonomy level: silent, notify, or ask first.
1. Ask first creates a pending approval with a deadline. The `huvu_approvals` live table shows the pending ones.
1. An approval belongs to the user it was asked for: the chat's user, or the trigger's. It goes to that user's phones, and only they can answer it. One asked from a shared device nobody is recognized at, or caused by a guest, goes to the group's members, and the first answer wins.
1. The same request twice while the first waits (a double press, two clients racing) opens one approval. A unique key on pending approvals decides it in the database: the first gets the approval, and the other gets only `already_pending`. Answering races the same way: two phones answering at once run the action once, and the slower answer is refused.
1. Every action, allowed or refused, goes into `huvu_events` with who asked and why.
1. An action a driver marks `requires_human` always asks, and config can't lower it. Only a user's answer from their phone (`POST /api/approvals/{id}`) can approve it, so no model can approve its own request.
1. A member's tap on a personal device (`POST /api/actions/{source}/{action}`) runs at once: the tap is the approval. A tap on a shared device, or by a guest, goes through the policy like any other request, so neither the TV nor the babysitter's phone can open the door.
1. Config (`[policy]` in `huvu.toml`) can change any other action's level.
1. Unlocking the front door always needs a human tap. A model or a face match alone never opens it.

## Hermes

Hermes stays the harness for its ecosystem and extensibility, and because its plugins are Python. OpenClaw was considered: its dreaming is memory consolidation into `MEMORY.md` with tool-free model calls that only learn from interactive sessions, so it doesn't cover huvu's dream, and its plugins are TypeScript.

### What Hermes owns

1. **The dream.** A daily trigger at night, one per person, that the kernel sets up itself (`dream.py`): an edge on `now_minute()` being `[dream] at` (04:00), on `[dream] tier` (pro), with a script that only wakes the model, and the dream prompt as its note, so the wake starts it in the person's own profile. It ends with a brief to its person through `huvu_notify`, as quick notes, one line each: what it noticed, what it changed, what waits for their yes. A user's dream is made once (`kv` namespace `dream`), so one they remove stays removed. It reads the day from the history tables (`chat_turns`, `huvu_events`, each source's), its triggers from `huvu_triggers` and their `trigger.run` and `trigger.error` rows, fixes or deletes broken triggers, registers new ones, updates its own memory and skills, and writes proposals for the morning chat. It is also where preferences are learned: it queries the history tables with `data_sql`, starting from where the last dream stopped, looks for repeated actions by a person in a similar context, for corrections (someone changing what huvu did a few minutes later), and for manual actions a trigger could do, checks each candidate for support, consistency and recency, keeps the claim in memory with the query that supports it, and proposes it in the morning chat. A trigger is registered only after the person says yes. The prompt is `NOTE` in `dream.py`.
1. **What it learns.** Which tables and queries it uses often, what you call the hall light. This goes in its own memory and skills, written during the dream. Little goes into the model's memory beyond that.
1. **The situation block** at the start of a session: the time, who huvu is helping, the family mode and pending approvals. The huvu Hermes plugin adds it on a session's first turn, from `POST /api/situation`.

### Settings

1. Background review off (`auxiliary.background_review.enabled: false`). It is not per turn: it runs every 10 user turns (`memory.nudge_interval`) or 10 tool iterations (`skills.creation_nudge_interval`), and each run forks the agent at about 30K tokens. The dream does the same work once a night.
1. The skill curator stays on. It runs weekly, when idle for 2 hours, and archives agent-made skills nobody uses.
1. In every profile, the huvu plugin is enabled (`plugins.enabled: [huvu]`), and the backend is an MCP server named `huvu` in `mcp_servers`, with `url: http://<backend>/mcp` and the profile's own `HUVU_MCP_TOKEN` as the bearer header. See `hermes-plugin/README.md`.

### The huvu plugin

Hermes connects to an MCP server once, with fixed headers. The profile's token says which user is calling, and the huvu Hermes plugin (`hermes-plugin/huvu/`) carries the session across the boundary. Its `pre_tool_call` hook adds `_session` to every huvu tool call. The backend strips it and looks the session up in `hermes_sessions`, which names its device. A session that belongs to another user than the calling profile's is ignored. The plugin also adds the situation block, see [What Hermes owns](#what-hermes-owns), and a system prompt section with huvu's guidance (`GUIDE`): who the agent is among its group's agents, that everything is a table `data_sql` reads, the exact `tool_call` shapes of the everyday calls (find, act, notify, a reminder trigger with its script), and how to answer: one short line saying it's on it before any tool call, then the outcome, plus provenance, approvals, privacy and telling the person about changes. Hermes never shows the model an MCP server's own instructions (it keeps the initialize result only for capabilities), so the plugin's section is the one place huvu can say these things.

Session taint was removed on 2026-10-04. It followed OpenClaw's taint gate: a session that had read `external` content got stricter policy, its triggers waited for the owner's tap, and its memory and skill writes went to Hermes's approval gate. It was judged more complexity than current models need. Two guards stay without it. Provenance labels still mark outside content for the model, and `requires_human` actions always need a person's tap.

### MCP tools

Hermes names each one `mcp__huvu__<name>`. Hermes sees only the kernel's tools; a source's actions run through `act` by name, so a new driver adds nothing to a session's tool list.

| Tool | What it does |
|---|---|
| `data_sql` | One read-only SQLite query over every table, history and live, scoped to the caller. The only read tool. See [Tables](#tables) |
| `triggers_register`, `triggers_remove`, `triggers_pause`, `triggers_resume` | Trigger management. `triggers_register` with `dry_run` checks a trigger and registers nothing; the triggers themselves are `huvu_triggers` |
| `act` | Runs a source action by name (`ha_bedroom_ac_set`), with its args. Every action takes an optional `reason`. A wrong name or argument comes back with the closest name or the right signature |
| `ui_choices`, `ui_show_feed`, `ui_show_image` | Parts for the frontend, added to the live turn of the calling session |
| `people_group` | The caller's groups and who is in each, by what the caller calls them. See [What people call each other](#what-people-call-each-other) |
| `people_identify`, `people_release` | On a shared device: ask a family member on their phone whether it's them, or forget who is there |

`state_get`, `state_search`, `events_search`, `events_get`, `events_digest`, `sources_list`, `triggers_list` and `triggers_test` went on 2026-10-09: each is a query now.

A result with outside content comes back as `{"data": ..., "provenance": "external"}`, and the plugin's section tells the model to treat text in it as data. Scripts still call the per-source tools by name (`home_turn_on`), so a trigger's grants stay per action.

### Skills

Every source is a Hermes skill, `huvu-<source>` (`skills.py`). Its body is the driver's `guide` (when to use the source, its use cases, the gotchas) followed by a reference huvu generates from the spec: actions with their arguments, live tables with their columns, and history tables with their columns, the standard columns and the indexes, which is the data dictionary the dream and every trigger work from. Every `huvu-*` skill is pinned fully loaded in every session (`skills.auto_load`, set by `setup.sh` from the skills the backend wrote at its last start, so run it again after adding a source). Loading skills on demand cost a model round trip each, and Hermes's own prompt tells the model to load any skill that is even partly relevant, so a plain reminder took four minutes and six model calls; pinned, with the call shapes in the guide, it takes one call and about 7 seconds (measured 2026-10-07). The cost is the skills' size on every request, mostly from the prompt cache.

huvu writes the skills into its data folder at every start and at every respec (`skills_dir`, default `<data_dir>/skills`), and links each into the repo's gitignored `hermes-skills/` (`hermes_skills_dir`), making the folder and repairing the links as needed and removing those of sources that are gone. Every profile reads `hermes-skills/` through `skills.external_dirs` (`setup.sh` sets it), so nothing needs setting up again; Hermes follows the links (`os.walk(followlinks=True)`). Anything else in the folder is left alone. The files are read-only: Hermes lets the model edit a skill wherever it lives, and huvu would overwrite the edit at the next start, so what the model learns about the house goes into its own skills and memory. This needs Hermes to read the backend's disk; if the two ever run on different machines, the plugin would fetch the skills instead.

## Sessions

Hermes's API server runs many sessions. Its session API (`POST /api/sessions`, then `POST /api/sessions/{id}/chat/stream`) streams `assistant.delta`, `tool.started` (with the tool's full arguments), `tool.completed` (with a result preview capped at 500 characters), `tool.failed` and a final `run.completed`, `run.failed` or `run.cancelled`. Concurrent turns on one session are serialized by Hermes.

The backend multiplexes on top of that (`conversations.py`):

1. A device's conversation maps to one Hermes session, in its user's profile or the default one.
1. Every trigger wake starts a fresh Hermes session in the trigger user's profile, with the wake payload (`wake.py`). Its reply goes to `huvu_events` as a `wake.done` row. If the agent wants to reach the user, it calls `huvu_notify`. Two wakes run at once at most.
1. A turn streams to the client that asked, and to the same device's other push streams. It never reaches another device.
1. Hermes's own sessions (the dream, the morning chat) are its business. A chat Hermes starts reaches the frontend through `huvu_notify`.
1. **The prompt cache decides when a session continues.** The provider keeps a session's prompt cached for 5 minutes after its last use (`CACHE_TTL` in `conversations.py`). An ask within that window continues the same Hermes session and hits the cache. An ask after it starts a fresh session, primed the way a compacted session is: the asks and answers so far, with a note telling the model to carry on as the same assistant without acknowledging it. Thinking and tool output are left out, since with the cache cold they would cost full price to replay. Only the latest turns that fit in 50,000 characters go in (`TRANSCRIPT_CHARS`, about 12,000 tokens), so the transcript stops growing with the conversation. The note then says which turn the transcript starts at, and that the turns before it are in `chat_turns`, with the conversation's `device_id`. The app still shows every turn. Every `chat_turns` row carries the run's `usage`, cache reads included, to check this against the provider.
1. **The router picks each ask's model** (`routing.py`). Every ask goes to Clef-flash, Cloudflare's decision model on Workers AI, with one yes-or-no question (`ESCALATE_QUESTION` in `prompts.py`): does it need anything said earlier in the conversation? Below `[router] escalate_at` (0.30; on 44 asks labelled by hand, every ask that needs context scored 0.462 or more) it is a one-off and runs on basic at `[router] effort` (`none`, sent as `model_options.reasoning_effort`) in a fresh Hermes session with the ask alone, used once. At or above it, on an error, after `[router] timeout` (1 s for the whole call) or without credentials (`CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_AI_TOKEN`), it runs on pro in the main session. Clef-flash answers the same text the same way, so the router keeps its answers by the ask's words (2,000 of them) and a repeated ask never waits twice. The main session always runs on pro (`hermes_sessions.route` holds its model) and must know every turn of the conversation: its next ask carries a note for each turn after the last one it ran, with what was said and answered (`quick_turn` in `prompts.py`), appended so the cache holds; a fresh main session reads them in its transcript instead. A rewind clears the main session, so the next ask on pro starts afresh from the turns that stay. Every `chat_turns` row carries its route (tier, model, effort, why: one_off, context, failed or off, and Clef-flash's probability when it was asked). `[router] enabled = false` runs every ask on pro.
1. **Edit and retry rewind**, always through a fresh session with the transcript of the turns that stay. Hermes can fork a session but its API can't cut one short; its CLI and chat gateways can (`/undo`, `rewind_user_turn`), so a rewind endpoint upstream would let a rewind within the cache window keep its cache. Both sessions stay until the sweep.
1. **A client joins a turn from its start.** A delta doesn't say where its text goes, so a client that opens while a turn runs can't add later deltas to a snapshot. `GET /api/turns/{turn}/events` replays the turn's events so far from the turn's own history, then follows it live, the way the turn's own stream does.
1. **Stop lets Hermes finish.** A stop asks Hermes to stop and keeps relaying until Hermes's stream ends, so the turn has every token Hermes produced and ends the way Hermes reports: complete if it finished anyway, stopped if it was cut off. Only a Hermes that hasn't ended 15 s later is cut off by huvu. The frontend and the backend agree on how every turn ended.
1. **Old sessions are swept.** Once a day, the Hermes sessions huvu started that haven't been used for 30 days are deleted through Hermes's API. The history and the turns huvu keeps for the stage stay. Sessions huvu didn't start are untouched, and Hermes prunes nothing by itself (`hermes sessions prune` only touches ended sessions).

The relay turns Hermes's events into frontend events. Hermes's housekeeping tools (memory, skills, session search) and huvu's reads stay hidden. huvu's actions show as notes. Hermes tools with an adapter (`web_search`) get the fields the stage reads; any other tool shows as a generic note. A `MEDIA:<path>` tag in the answer, Hermes's own way to attach a file, becomes an image part where the tag was, served through a media URL (`media.py`); the answer text never holds it, so the voice never reads a path out.

## Code layout

```
backend/
  pyproject.toml, huvu.toml.example
  src/huvu/
    spec.py           the contract's types: SourceSpec, TableSpec, ActionSpec, labels, Caller
    data.py           the tables: history (analytics.db), live tables, data_sql and the now_*() functions
    scope.py          who sees which row
    accounts.py       users, devices, pairing, tokens, each user's Hermes profile
    attendance.py     who is at a shared device, confirmed from their phone
    prompts.py        every text a model reads: guide, MCP and tool text, source descriptions, the skill renderer, wakes, the dream
    skills.py         writes and links every source's Hermes skill
    source.py         Source (what a driver subclasses) and SourceContext
    registry.py       loaded sources, action names with suggestions, supervision
    triggers.py       the trigger format and the engine: nudges, the minute tick, edges and rows
    scripts.py        trigger scripts, run by script_host.py in their own process
    policy.py         autonomy levels, approvals, audit
    tools.py          the tool box MCP and scripts share
    mcp_server.py     the MCP server
    hermes.py         the Hermes API client
    conversations.py  frontend turns on Hermes sessions, and the relay
    routing.py        the router: Clef-flash on Workers AI, between basic and pro
    wake.py           trigger wakes
    dream.py          each person's nightly dream: its trigger and its prompt
    speech.py         huvu's voice: Edge, a turn's answer spoken a sentence at a time
    hearing.py        speech to text through Groq's Whisper
    alexa.py          asks through the Alexa skill, for the Fire TV: Amazon's signature checks, then the ask on the TV
    wipe.py           what `huvu wipe` deletes for a user or a group
    sessions.py       what the kernel knows about Hermes sessions
    push.py           the push channel
    native_push.py    push tokens and which events reach a phone whose app is closed, through FCM or Web Push
    web_push.py       Web Push for huvu on an iPhone's home screen: the VAPID key, encryption, the sender
    media.py          media URLs for refs, image files and https URLs
    bundles.py        the phone app's web build, for its live updates
    http.py           the HTTP server
    auth.py, config.py, db.py, times.py, kernel.py, __main__.py
    builtin/          the huvu and chat sources
  src/huvu_drivers/   the drivers, one package or module per source
    ha/               Home Assistant: client.py (WebSocket), scripts.py (scripts as actions),
                      history.py (who caused a change, sensor buckets), source.py (the driver)
    location.py       where each person is, from OwnTracks
  tests/              fakes.py (a fake house and Hermes), fake_ha.py (a fake Home Assistant)
hermes-plugin/huvu/   the Hermes plugin
```

Run it with `uv run huvu` from `backend/` (it reads `huvu.toml`, or `$HUVU_CONFIG`), and the tests with `uv run pytest`.

## Agents in a group (proposed, not built)

huvu is each person's own agent, and it lives next to the agents of the other people in its group. The house is one of the sources they share, not what huvu is about. Each agent keeps its person's memory in its own Hermes profile, and no agent reads another's. Two needs pull against each other. A person must be able to keep things from the rest of the family: a son's late-night bike rides stay his. A plan the whole family makes, such as a vacation in November, should reach every agent without each person telling their own.

The difference is whose fact it is. The proposal, from 2026-10-07:

1. **Private by default.** What a person tells their agent stays in their profile. This is how it works already.
1. **One channel between agents: a group notebook in the backend.** Each entry holds the fact, the agent that wrote it, its audience and when it stops mattering. The audience is the group, or the group without someone, for a surprise. The agents never talk to each other directly, so the notebook is the whole channel, and each person can see in the app what their agent shared.
1. **Writing needs consent.** An agent writes only facts that concern the others. When it isn't sure, it asks its person at the time ("should the others' agents know about November?"), and it learns their sharing habits so it asks less over time.
1. **Consensus in the dream.** Each night, each agent posts what it learned that day that concerns the group, and reads what the others posted. When two entries disagree, such as two dates for the vacation, it asks the people and never picks one.

Shared devices leak around all of this. Location is personal, but the front door lock is the group's: it logs whose finger opened it, and every member's dream reads the group's events. The attribution is what the dream learns preferences from, so it can't simply go. Two parts would close most of the gap:

1. **Personal attribution on shared devices.** Every member's agent sees "the front door opened at 23:04". Only the person's own agent sees that it was them.
1. **Discretion.** An agent learns from what it sees and never tells its person what another member did. This belongs in the plugin's guide.

Still to decide, and a question of values more than design: a member asks their agent "did anyone go out last night?" The proposal is that it says a door opened and never says who.

## Open questions

1. **Rewind endpoint in Hermes (a PR to write).** `POST /api/sessions/{id}/rewind` with `{"turns": N}`, wrapping `rewind_user_turn` the way the CLI's `/undo` does. huvu would then rewind within the cache window in place, and fall back to a transcript only after it.

1. **Strict tool schemas.** Check if Hermes uses OpenAI's strict mode for the Codex provider. Enums of value names would make made-up names impossible.
1. **Pulsar.** The Tuya consumer has never run. See `front-door-lock.md`.
1. **Trigger evaluation at scale.** Untested with fast inputs like glasses frames. A source that records a row per frame runs every trigger on that table each time; such a source should record less.
1. **The plugin against a live Hermes.** The hooks were tested the way Hermes's source calls them, not inside a running Hermes.
1. **Profiles against a live Hermes.** The `/p/<profile>/` routes and per-profile keys come from Hermes's source and docs. No backend call has gone through them yet.
1. **Native push against a real Firebase project.** Phones register FCM tokens, and notifications, pending approvals and "is it you?" questions go to them through FCM's HTTP v1 API (`native_push.py`), signed with the service account key in `HUVU_FCM_CREDENTIALS`. It was tested with a fake FCM only. iOS also needs the APNs key uploaded to the Firebase project. A phone that connects later still catches up from the lists (`GET /api/approvals`, `/api/identify`, `/api/notifications`, which keeps 3 days).
1. **Finer roles.** Groups have two roles, member and guest. Kids who may approve the porch light but not the front door would need a third, or per-action rules.
1. **Turns across a restart.** A turn running when the backend stops ends as failed. Hermes may still finish it, unseen.
1. **OwnTracks on real phones.** The location driver was tested against payloads written from OwnTracks's docs. Still to check on a phone: that a `setWaypoints` reply over HTTP makes regions on Android and on iOS, that OwnTracks queues posts while offline and sends them later, and how often Android's significant mode really posts. A late arrival still fires triggers on its row; a trigger can filter on `delay_min`.
1. **Missed crossings.** If a phone misses a transition, `place` stays wrong until the next crossing of that waypoint. Each fix carries the phone's own list of waypoints it is inside (`inregions`, kept in `location_fixes.regions`), which could correct it. Not done, because an absent field may not mean "inside none".
1. **The HA driver against a live HA.** It was tested against a fake HA that answers the way HA's WebSocket API is documented and coded. These were taken from HA's source and not seen on a live instance: `homeassistant/expose_entity/list`, `script/config`, `config/entity_registry/get_entries` (for aliases), `return_response` on a script call, and that a script's own service calls run in the script's context. `ha_setup` and the driver's warnings show the first of these going wrong.
1. **Read-only for scripts and the terminal.** A trigger script and Hermes's terminal run as the backend's OS user, so they could open `analytics.db` and `huvu.db` directly, past the scope views. Running the backend as its own OS user, with scripts as an unprivileged one, is the fix.
1. **Backfill after downtime.** Changes made in HA while huvu was down never become history rows. The live tables are right again on reconnect. The HA driver could backfill from HA's recorder.
1. **History retention.** History rows are kept for good. At the sizes estimated (a few hundred change rows and about 14,000 sensor rows a day) that is a few million rows a year, which SQLite handles; if it grows past that, old sensor buckets roll up to hours.

## Build order

1. The HA driver: built on 2026-10-06. Left: the HA side (`home-assistant.md`) and a first run against the live HA.
1. The Tuya driver (Pulsar first, then the album fetch and decryption). Checked by pointing Hermes at the MCP server, ringing the doorbell, and asking who rang. Its unlocks go into a history table too.
1. The rest of the history work under [Not built yet](#not-built-yet), starting with the writable schema and backtests.
