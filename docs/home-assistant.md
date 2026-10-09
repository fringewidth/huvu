# Home Assistant for huvu

For agents. This doc says what Home Assistant (HA) must look like for huvu's HA driver, and how to get it there and check it. Read it all before you change anything in HA. The design behind it, with the reasons, is in `backend-architecture.md` (sections "The Home Assistant driver" and "History tables"). The driver's code is `backend/src/huvu_drivers/ha/`.

Status on 2026-10-06: the driver is built and tested against a fake HA only. Nothing on the HA side is done yet. Your job is the HA side, then a first run of the driver against it.

## How huvu uses HA

Know this before you design anything in HA. Every rule below follows from it.

1. huvu keeps one WebSocket connection to HA, logged in with a long-lived access token of its own HA user.
1. **huvu sees only what is exposed to Assist** (Settings, Voice assistants, Expose). It can't read, search or work anything else.
1. **huvu changes the house only by running HA scripts.** Each exposed script becomes one huvu action: `script.bedroom_ac_set` becomes `ha_bedroom_ac_set`. huvu has no other way to work a device: no `light.turn_on`, no raw service call. If a device can't be worked through a script, huvu can't work it.
1. The model sees, for each script: its description, its area, and its fields as arguments with types taken from their selectors. That's all it knows about what the script does. The script's description is the documentation.
1. **A label on each script decides whether huvu may run it alone.** See [Labels](#step-6-labels).
1. The model reads every exposed entity's state and attributes from `ha_entities`, a live table, and finds entities by name, area, device and alias with SQL. Every change and script run lands in the history tables (`ha_changes`, `ha_script_runs`, `ha_sensor_5min`), and a trigger that reads one runs the moment a row lands.
1. huvu records history for its nightly "dream", which learns what each person prefers: every change of an exposed entity with who caused it, every script run, and numeric sensors every 5 minutes. A setup that hides who did what, or that leaves devices without a state, gives the dream nothing to learn from.
1. huvu reloads the scripts, labels, areas and the exposure list on every registry change in HA, and every 5 minutes. A script you add or edit appears in huvu within 5 minutes, with no restart.

## Hard rules

1. Never write a script that opens the front door, a gate, or a garage door, or disarms an alarm, without the `huvu_human` label.
1. Don't expose lock, alarm or garage door entities' own services through any script without `huvu_human`. Exposing them for reading is fine.
1. huvu's token must belong to a separate HA user named `huvu`, never to a family member's user. huvu tells its own actions apart by that user.
1. Every script huvu may run has a description written for a model (see [Descriptions](#descriptions)).
1. Every script id is lower case letters, digits and underscores, starting with a letter.
1. Don't build behavior in HA automations, such as "turn the AC on at 22:30". huvu's triggers own behavior, and the dream learns from what people do. HA automations are for device glue only: syncing a state, a power sensor, a debounce. See [Automations](#step-9-automations).
1. Never remove or rename an exposed script without checking the `ha_setup` table afterwards (ask huvu "what is wrong with the Home Assistant setup?"). A renamed script is a new action, and huvu triggers that used the old name fail.
1. Quote `"on"`, `"off"`, `"yes"` and `"no"` in YAML. HA's YAML reads them unquoted as booleans, and a select option `off` would turn into `false`.

## Tools

Run these from `backend/`. Both read `HA_URL` (default `http://homeassistant.local:8123`) and `HA_TOKEN` from the environment, or else from `backend/.env`.

```sh
# Send one WebSocket command and print the result.
uv run python -m huvu_drivers.ha.cli ws '{"type": "config/label_registry/list"}'

# Run the real driver once and print what huvu sees: exposed entities,
# actions with their autonomy, and every setup problem. Exit code 1 on a
# problem or no connection.
uv run python -m huvu_drivers.ha.cli check
```

Useful `ws` commands:

| Command | What it returns |
|---|---|
| `{"type": "get_states"}` | Every entity's state and attributes |
| `{"type": "config/entity_registry/list"}` | Entities with area, device, labels |
| `{"type": "config/area_registry/list"}` | Areas |
| `{"type": "config/device_registry/list"}` | Devices with area |
| `{"type": "config/label_registry/list"}` | Labels, with `label_id` |
| `{"type": "homeassistant/expose_entity/list"}` | What is exposed to which assistant (`conversation` is Assist) |
| `{"type": "script/config", "entity_id": "script.x"}` | A script's config, as huvu reads it |
| `{"type": "config/auth/list"}` | HA users with their ids |
| `{"type": "call_service", "domain": "script", "service": "x", "service_data": {...}, "return_response": true}` | Runs a script the way huvu does |

To expose or hide entities without the UI:

```json
{"type": "homeassistant/expose_entity", "assistants": ["conversation"], "entity_ids": ["script.bedroom_ac_set"], "should_expose": true}
```

The REST API works too, with the same token: `curl -H "Authorization: Bearer $HA_TOKEN" $HA_URL/api/states/climate.bedroom_ac`.

These commands come from HA's source. If one is refused on this HA version, say so in the [Inventory](#inventory) and tell the owner; the driver may need a change.

## Step 1: take stock

Before you change anything, find out what exists and write it into the [Inventory](#inventory):

1. HA's version (`check` prints it), and the integrations in use: Broadlink, Xiaomi Miot Auto (`xiaomi_miot`), SmartIR, Tuya, ESPresense, anything else.
1. Every Broadlink remote entity (`remote.*`), its area, and the devices and commands learned on it. HA has no API that lists learned commands. They are in `.storage/broadlink_remote_<mac>_codes` on the HA host. Read that file if you have access to the host, or ask the owner.
1. Every device the household uses, and whether HA can see its real state (a sensor, a power plug, a cloud integration) or only assumes it.
1. The scripts and automations that exist, and what each does.
1. The HA users and which family member each one is.

Don't delete anything you find. Ask the owner before you remove an automation or script.

## Step 2: huvu's HA user and token

1. Settings, People, Add person: name `huvu`, allow login, a username and password, **administrator** on. huvu reads the registries and the exposure list, which needs an administrator.
1. Log in as `huvu` (a private browser window), open the profile (bottom left), Security tab, Long-lived access tokens, Create token. Copy it once; HA never shows it again.
1. Put it in `backend/.env` as `HA_TOKEN=...` on the machine that runs huvu. Never commit it.
1. Find each family member's HA user id (`config/auth/list`) for the `users` map in `huvu.toml` (Step 10).

## Step 3: areas

HA's areas are huvu's rooms. The model finds things by area, and the dream joins data by area.

1. One area per real room. Use the names the family uses ("Bedroom", "Living room", "Amma's room").
1. Put every device in its area. Put every script in the area of the device it works (the script's entity settings, Area).
1. Give entities names people say: "Bedroom AC", not "Climate 2". Add aliases for other names people use (entity settings, Voice assistants, Aliases). huvu searches aliases.

## Step 4: infrared devices (Broadlink)

Most devices here are infrared, worked by a Broadlink RM4. IR goes one way: HA sends a code and never learns whether the device obeyed. Plan for that.

### Learning codes

1. Learn with `remote.learn_command`: `entity_id` the RM4, `device` the device's name, `command` the button names, `command_type: ir` (or `rf` on an RM4 Pro).
1. Name devices `<area>_<device>` (`bedroom_ac`, `living_tv`) and commands after the button (`power`, `power_on`, `power_off`, `volume_up`, `input_hdmi1`, `netflix`). Use the same names in every script.
1. Prefer discrete codes (`power_on`, `power_off`) over a toggle (`power`). Many remotes only have a toggle; some devices accept discrete codes anyway (search the model's IR code lists). A toggle sent at the wrong time turns the device on instead of off.

### Giving devices a state

A device with no state gives huvu nothing to read, and the dream nothing to learn from. A press on the physical remote never reaches HA at all, so without a sensor the dream only sees what huvu itself did.

1. **Power plugs.** For each IR device worth learning about (the ACs and the TV first), a power-metering smart plug (or the AC's own power monitor) gives the real state. Make a template binary sensor from it:

```yaml
template:
  - binary_sensor:
      - name: Living TV power
        unique_id: living_tv_power
        device_class: power
        state: "{{ states('sensor.living_tv_plug_power') | float(0) > 15 }}"
        delay_on: 5
        delay_off: 20
```

Pick the threshold from the device's standby and on wattage (read the sensor in both states). Expose the binary sensor and the power sensor.

1. **ACs and fans as entities.** Use SmartIR (HACS) so an IR AC becomes a `climate` entity with modes, target temperature and fan speed. Give it the power sensor, so SmartIR corrects its state when someone uses the physical remote:

```yaml
climate:
  - platform: smartir
    name: Bedroom AC
    unique_id: bedroom_ac
    device_code: 1000          # from SmartIR's code list for this AC model
    controller_data: remote.bedroom_rm4
    temperature_sensor: sensor.bedroom_temperature
    humidity_sensor: sensor.bedroom_humidity
    power_sensor: binary_sensor.bedroom_ac_power
```

SmartIR's options differ between versions and forks; check the installed one's README. If no code file matches the AC, learn its codes and write a custom code file in SmartIR's format (Broadlink codes are base64 in both).

1. **Other IR devices** (TV, speakers, lights on IR) don't need an entity of their own. Their scripts are enough, plus the power sensor where there is one.

An entity HA only assumes has `assumed_state: true`. huvu tells the model not to claim such a device did anything, only that the signal was sent. Don't hide this attribute.

### One sender per blaster

Two scripts that send IR through the same RM4 at once can interleave their codes. Route every send through one helper script per blaster in `queued` mode, so codes for one blaster go out one at a time:

```yaml
ir_living_send:
  alias: IR living room send
  mode: queued
  max: 20
  fields:
    device: {selector: {text: {}}}
    command: {selector: {text: {}}}
    repeats: {selector: {number: {min: 1, max: 10}}}
  sequence:
    - action: remote.send_command
      target: {entity_id: remote.living_rm4}
      data:
        device: "{{ device }}"
        command: "{{ command }}"
        num_repeats: "{{ repeats | default(1) }}"
        delay_secs: 0.4
```

Don't expose these helpers. Device scripts call them as an action (`action: script.ir_living_send`), which waits for the send to finish.

## Step 5: scripts as actions

Every way huvu may change the house is one script.

### Shape

1. **One script per intent on one device or room**, with fields for what varies: `bedroom_ac_set` with `mode` and `temperature`, not `bedroom_ac_cool_24`. `living_tv_app` with an `app` select, not one script per app.
1. **Id**: `<area>_<device>_<verb>`: `bedroom_ac_set`, `bedroom_ac_off`, `living_tv_on`, `living_tv_app`, `mop_clean_rooms`. The id is the action name the model sees and the name the dream's history uses. Keep it stable.
1. **Idempotent**: running it twice gives the same result. With a power sensor, check it before sending a toggle. Without one, prefer discrete codes.
1. **Mode `queued`** (`max: 5`). In the default `single` mode, a second call while the first runs is dropped, and huvu would report success for a call that did nothing.
1. **Short.** huvu waits up to 120 s for a script to finish. A longer job (a mop run) should start the work and return.
1. **Return what happened**, when it helps the model: end with `stop` and `response_variable`. huvu passes the response back to the model.
1. **Fixed targets.** The script names its device itself. Don't give a field an `entity`, `area`, `device` or `target` selector; the model would be picking any entity in the house. Use a `select` of fixed options when a script covers a few devices.

### Descriptions

The description is the only documentation the model gets. Write it for a model that has never seen the house:

1. First sentence: what it does, in plain words. "Sets the bedroom AC's mode and target temperature."
1. The state it leaves: "The AC stays on until something turns it off."
1. Preconditions and side effects: "Turns the TV on first if it is off. Switches the TV's input to HDMI 1."
1. Timing: "Takes about 10 s."
1. What it can't know: "IR only: it can't tell whether the TV obeyed."

Field descriptions say the unit and the range: "Target temperature in °C, 16 to 30."

### Selectors

How each field's selector reaches the model:

| Selector | What the model gets |
|---|---|
| `number` (`min`, `max`, `unit_of_measurement`) | a number, with its range and unit |
| `select` (`options`) | one of the options; with `multiple: true`, a list of them |
| `boolean` | true or false |
| `text` | a string |
| `time` | a string `HH:MM:SS` |
| `date` | a string `YYYY-MM-DD` |
| `duration` | hours, minutes and seconds |
| `color_temp` | a number in kelvin or mireds |
| `color_rgb` | `[red, green, blue]` |
| anything else, or no selector | free text, and a setup problem |

`required: true` makes a field required. `default` and `example` are shown to the model in the field's description.

### Examples

An AC behind SmartIR:

```yaml
bedroom_ac_set:
  alias: Bedroom AC set
  description: >-
    Sets the bedroom AC's mode and target temperature. Mode "off" turns it
    off and ignores the temperature. The AC stays as set until something
    changes it. IR: the power sensor confirms it within about 20 s.
  mode: queued
  max: 5
  fields:
    mode:
      description: Mode.
      required: true
      selector:
        select:
          options: ["off", "cool", "dry", "fan_only"]
    temperature:
      description: Target temperature in °C, 16 to 30. Default 24.
      default: 24
      selector:
        number: {min: 16, max: 30, step: 1, unit_of_measurement: "°C"}
  sequence:
    - action: climate.set_hvac_mode
      target: {entity_id: climate.bedroom_ac}
      data: {hvac_mode: "{{ mode }}"}
    - if: "{{ mode != 'off' }}"
      then:
        - action: climate.set_temperature
          target: {entity_id: climate.bedroom_ac}
          data: {temperature: "{{ temperature | default(24) }}"}
```

A TV on IR with a power plug:

```yaml
living_tv_on:
  alias: Living room TV on
  description: >-
    Turns the living room TV on. Does nothing if it is already on. Takes
    about 8 s before the TV takes other buttons.
  mode: queued
  max: 5
  sequence:
    - if:
        - condition: state
          entity_id: binary_sensor.living_tv_power
          state: "off"
      then:
        - action: script.ir_living_send
          data: {device: living_tv, command: power}
        - delay: 8
    - variables:
        result: {is_on: "{{ is_state('binary_sensor.living_tv_power', 'on') }}"}
    - stop: done
      response_variable: result
```

The mop, through Xiaomi Miot Auto. Room cleaning differs per model. Find the action's `siid` and `aiid`, and the room ids, from the model's MIoT spec (home.miot-spec.com) and the integration's attributes, and test with `ws` before you write the script:

```yaml
mop_clean_rooms:
  alias: Mop clean rooms
  description: >-
    Starts the mop on the given rooms and returns at once; the run takes
    15 to 60 minutes. The mop docks itself when done.
  mode: queued
  max: 2
  fields:
    rooms:
      description: The rooms to clean.
      required: true
      selector:
        select:
          multiple: true
          options: ["bedroom", "living_room", "kitchen", "hall"]
  sequence:
    - variables:
        # Room name -> the mop's own room id, read from its map.
        room_ids: {"bedroom": 10, "living_room": 11, "kitchen": 12, "hall": 13}
        segments: >-
          {% set ns = namespace(ids=[]) %}
          {% for room in rooms %}{% set ns.ids = ns.ids + [room_ids[room]] %}{% endfor %}
          {{ ns.ids }}
    - action: xiaomi_miot.call_action
      data:
        entity_id: vacuum.mop
        siid: 0      # from the MIoT spec
        aiid: 0      # from the MIoT spec
        params: "{{ segments }}"   # the shape of params differs per model; test it with ws first
```

Simple vacuum controls (`mop_start`, `mop_dock`) use HA's `vacuum.start` and `vacuum.return_to_base`, which work for every vacuum.

## Step 6: labels

Create these four labels (Settings, Areas, labels and zones, Labels), with exactly these names, so their ids are the same:

| Label | huvu runs the script | Use it for |
|---|---|---|
| `huvu_silent` | at once | lights, TV, fans, AC, media, the mop |
| `huvu_notify` | at once, then tells the person | things someone should know about: the geyser, the water pump |
| `huvu_ask` | after the person taps yes on their phone | anything costly or disruptive: a long run, loud things at night |
| `huvu_human` | only after a person's tap, whatever anyone configures | doors, gates, garage, alarms, anything about safety |

1. A script without one of these labels asks first (`huvu_ask`).
1. A script with two of them gets the strictest.
1. A label that starts with `huvu` but isn't one of these four is a setup problem (a typo), and the script asks first.
1. Put the label on the script (Settings, Automations and scenes, Scripts, select it, Add label). Check with `config/entity_registry/list` that the script's entity has it.

## Step 7: exposure

Expose (Settings, Voice assistants, Expose):

1. Every script huvu may run. Not the IR helper scripts.
1. Every device and sensor huvu may read: the climate entities, power sensors and binary sensors, lights, media players, the mop, door and window sensors, locks, people.
1. The sensors the dream needs for context (Step 8).

Turn off "Expose new entities" for Assist, so nothing reaches huvu by accident:

```json
{"type": "homeassistant/expose_new_entities/set", "assistant": "conversation", "expose_new": false}
```

The exposure list is shared with HA's own Assist. If the family uses Assist, anything you hide from huvu is hidden from Assist too. Note it in the inventory.

## Step 8: data for the dream

The dream learns from history. Give it the context people react to. Expose:

1. **Temperature and humidity in each room** that has an AC or fan, and outside (a weather integration, or an outdoor sensor). Numeric sensors need a `unit_of_measurement`; with a `state_class` they also get HA's long-term statistics.
1. **Power sensors** on IR devices (Step 4).
1. **Presence**: `person.*` entities, and room presence (ESPresense) if it exists.
1. **Doors and windows** (`binary_sensor` with `device_class` door or window).
1. **Light level**, if there is a sensor.

Don't expose high-rate sensors nobody acts on (signal strength, uptime, voltages). Each numeric sensor costs a row every 5 minutes.

## Step 9: automations

1. Keep HA automations for glue: syncing a state, debouncing a sensor, keeping SmartIR in step with a power sensor.
1. Don't add automations that decide for people ("AC on at 22:30", "lights off when nobody is home"). huvu learns those from what people do and proposes them; an HA automation doing it silently hides the preference from the dream and fights with huvu's triggers.
1. Existing behavior automations: list them in the inventory and ask the owner which to keep. Changes they make show in huvu's history as `automation`, so they stay visible.

## Step 10: huvu's config

In `backend/huvu.toml` (gitignored) on the machine that runs huvu:

```toml
[[sources]]
name = "ha"
driver = "huvu_drivers.ha:HomeAssistant"
options = { url = "http://homeassistant.local:8123", users = { "<HA user id of mom>" = "mom", "<HA user id of hrishik>" = "hrishik" } }
```

1. `users` maps HA user ids to huvu usernames (`uv run huvu user list`). A change someone makes in the HA app then counts as theirs.
1. `exposure = "all"` makes huvu read every entity, ignoring the exposure list. For development only.
1. `[policy]` in the same file can override a script's level by its action name: `"ha.bedroom_ac_set" = "notify"`. It can't lower `huvu_human`.

## Step 11: check

1. `uv run python -m huvu_drivers.ha.cli check` prints no problems, and every script you meant appears with the right level.
1. For each script: run it once with `ws` and `call_service` the way huvu does, and confirm the device did what the description says.
1. Use the physical remote on an IR device with a power sensor, and confirm the power sensor and the SmartIR entity follow within a minute.
1. Start huvu (`uv run huvu` in `backend/`) and look at `data/skills/huvu-ha/SKILL.md`: every action has a clear description and the right arguments.
1. Ask huvu to do one silent thing ("turn the bedroom AC to 24") and one human thing (the gate). The first runs; the second waits for a tap on a phone.
1. After a day, ask huvu: `data_sql` with `SELECT actor, count(*) FROM ha_changes GROUP BY actor`. Every actor class you expect shows up. If all changes are `device`, the HA users or the token are wrong.

Write what you checked, and what failed, into the inventory.

## Inventory

Keep this up to date as you work. It is the record of what HA looks like for huvu.

### HA

| Item | Value |
|---|---|
| HA version | |
| Integrations used | |
| huvu's HA user | |
| Family HA users (id, person) | |
| Does the family use Assist? | |

### Devices

| Device | Area | How HA works it (IR, cloud, local) | Real state from | Scripts |
|---|---|---|---|---|
| | | | | |

### Scripts exposed to huvu

| Script | Label | Fields | Checked on |
|---|---|---|---|
| | | | |

### Sensors exposed for the dream

| Entity | Area | What |
|---|---|---|
| | | |

### Open problems

1. (none yet)
