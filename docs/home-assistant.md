# Home Assistant for huvu

For agents. This doc says what Home Assistant (HA) must look like for huvu's HA driver, and how to get it there and check it. Read it all before you change anything in HA. The design behind it, with the reasons, is in `backend-architecture.md` (sections "The Home Assistant driver" and "History tables"). The driver's code is `backend/src/huvu_drivers/ha/`.

Status on 2026-10-06: the driver is built and tested against a fake HA only. Nothing on the HA side is done yet. Your job is the HA side, then a first run of the driver against it.

## How huvu uses HA

Know this before you design anything in HA. Every rule below follows from it.

1. huvu keeps one WebSocket connection to HA, logged in with a long-lived access token of its own HA user.
1. **huvu sees only what is exposed to Assist** (Settings, Voice assistants, Expose). It can't read, search or work anything else.
1. **huvu works every exposed device directly.** Each exposed light, switch, fan, button, scene, media player, climate, vacuum, number, select, cover or lock (and the `input_` helpers) is one huvu action: `light.theatre_ceiling` becomes `ha_light_theatre_ceiling`, with arguments from its attributes. So a device needs a proper entity: an infrared device gets template entities (a button per learned code, a switch for power), not just learned codes.
1. **Scripts are routines.** Each exposed script becomes one huvu action too: `script.bedroom_ac_set` becomes `ha_bedroom_ac_set`. Write one for what works several devices at once, such as starting the home theatre, or for what needs steps in order. The model sees, for each script, its description, its area, and its fields as arguments with types taken from their selectors. The script's description is the documentation.
1. For an entity, the model sees its name, area, device and aliases. Good names and aliases are what make "turn the projector off" work.
1. **Labels decide whether huvu may act alone.** On a script, on an entity, or on an area for everything in it. See [Labels](#step-6-labels).
1. The model reads every exposed entity's state and attributes from `ha_entities`, a live table, and finds entities by name, area, device and alias with SQL. Every change and script run lands in the history tables (`ha_changes`, `ha_script_runs`, `ha_sensor_5min`), and a trigger that reads one runs the moment a row lands.
1. huvu records history for its nightly "dream", which learns what each person prefers: every change of an exposed entity with who caused it, every script run, and numeric sensors every 5 minutes. A setup that hides who did what, or that leaves devices without a state, gives the dream nothing to learn from.
1. huvu reloads the scripts, labels, areas and the exposure list on every registry change in HA, and every 5 minutes. A script you add or edit appears in huvu within 5 minutes, with no restart.

## Hard rules

1. Never write a script that opens the front door, a gate, or a garage door, or disarms an alarm, without the `huvu_human` label.
1. Don't work lock, alarm or garage door entities through any script without `huvu_human`. An exposed lock, alarm, or garage, gate or door cover is an action huvu can run itself, always behind a person's tap (the driver enforces it, whatever the labels say). Expose one only if the family wants that.
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
1. **Fail with a reason** by stopping with a response that has `error`: set `failure: {error: "The AV unit is off."}` in `variables`, then `stop` with `response_variable: failure`. huvu fails the action with that text. `stop` with `error: true` doesn't work: HA marks the run aborted and still tells the caller it succeeded.
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

| Label | huvu runs the action | Use it for |
|---|---|---|
| `huvu_silent` | at once | lights, TV, fans, AC, media, the mop |
| `huvu_notify` | at once, then tells the person | things someone should know about: the geyser, the water pump |
| `huvu_ask` | after the person taps yes on their phone | anything costly or disruptive: a long run, loud things at night |
| `huvu_human` | only after a person's tap, whatever anyone configures | doors, gates, garage, alarms, anything about safety |

1. A script without one of these labels asks first (`huvu_ask`).
1. An entity takes its own label, else its area's label, else its domain's default: lights, switches, fans, buttons, scenes, media players, numbers and selects run at once; climates, vacuums and covers run and tell the person; anything else asks. Put a label on the entity, or on the area, only to change that.
1. Locks, alarms, and garage, gate and door covers always need a tap, whatever their labels say.
1. Two of them on one thing: the strictest wins.
1. A label that starts with `huvu` but isn't one of these four is a setup problem (a typo).
1. Put the label on the script or entity (its settings, Add label), or on the area. Check with `config/entity_registry/list` and `config/area_registry/list` that it is there.

## Step 7: exposure

Expose (Settings, Voice assistants, Expose):

1. Every script huvu may run. Not the IR helper scripts.
1. Every device and sensor huvu may read or work: the climate entities, power sensors and binary sensors, lights, media players, the mop, door and window sensors, people. An exposed device that huvu can work is an action, so expose a lock or a garage door only if huvu may work it (always behind a tap). Not the IR helper scripts, and not the raw `remote` entity: huvu works an IR device through its template entities.
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
| HA version | 2026.7.2, in Docker on the Windows host (container `homeassistant`), at `http://host.docker.internal:8123` for huvu and `https://ha.hrishik.me` through the tunnel |
| Config | `configuration.yaml` loads `packages/` (one file per room or job) and trusts the Docker networks as proxies (`http:`), both added 2026-10-09, and loads `alexa_history:` (added 2026-10-10; the file before it is `/config/configuration.yaml.before-alexa-history`) |
| Integrations used | Broadlink (RM4 Pro "Broadlink (HT)", IR), Alexa Devices (Dad's Amazon account: Echo devices, the Fire TV, Alexa routines as buttons, `alexa_devices.send_text_command`), Alexa history (huvu's own, from `home-assistant/custom_components/alexa_history` in the repo, copied to `/config/custom_components/alexa_history`: `alexa_history.fetch` reads Alexa's voice history now, see below), Xiaomi Miot (vacuum), Tuya (HA's own, linked to the Smart Life account by QR on 2026-10-10: the theatre's switchboards; the front door lock shows as unsupported, with no entities) |
| huvu's HA user | `huvu`, id `4a96ccac5e3a4c31a7cc1a0aa7ca63b2`, administrator, local only; its long-lived token is `HA_TOKEN` in huvu's env file on the volume |
| Family HA users (id, person) | `45c09fc9fc484f049ae31dced034cb82`, hrishik (the owner) |
| Labels | `huvu_silent`, `huvu_notify`, `huvu_ask`, `huvu_human` |
| Does the family use Assist? | No |

### Devices

The home theatre is primitives, all in `packages/home_theatre.yaml` (rebuilt 2026-10-10): each thing a person asks for is one exposed entity or script, so huvu does it in one call, with nothing to learn about how it works. huvu's skill lists every exposed action, one line each. The projector's and the amplifier's power buttons toggle, so two helpers track them (as last sent, unknown until then), and the scripts press power only when it changes something. Every Tuya command in the package (the AV power, and the lights in `home_theatre_on` and `home_theatre_off`) goes through `script.home_theatre_tuya` (added 2026-10-10), since Tuya's cloud now and then refuses a command ("sign invalid", or 1106) and a script can't catch that error: `continue_on_error` passes only HA's own errors. The helper sends the command in a run of its own (`script.home_theatre_tuya_send`), waits up to 5 s for the state, sends again to whatever isn't there, and after three tries returns `{"error": ...}`, which its caller checks, since a script that stops with an error doesn't stop the script that called it. huvu's own calls on an entity (the lights as `ha_light_*`) don't go through it: the driver sends one of those once more when it fails inside its integration. The package before `home_theatre_alexa` read Alexa's reply is `/config/home_theatre.v8.yaml.bak` on HA, the one before `home_theatre_volume` was added `/config/home_theatre.v7.yaml.bak`, the one before Tuya's retries were added `/config/home_theatre.v6.yaml.bak`, the one before `home_theatre_off` was added `/config/home_theatre.v5.yaml.bak` on HA, the one before `home_theatre_on` was added `/config/home_theatre.v4.yaml.bak`, the one before `home_theatre_alexa` was added `/config/home_theatre.v3.yaml.bak`, the one before the rebuild `/config/home_theatre.v2.yaml.bak`, and the first `/config/home_theatre.v1.yaml.bak`.

| Device | Area | How HA works it | Exposed | Internal |
|---|---|---|---|---|
| Lights | Home Theatre | Tuya Main Switchboard gangs 1 to 3, shown as lights (`switch_as_x`) | `light.home_theatre_strip_lights`, `_night_sky`, `_focus_lights`, and the group `light.home_theatre_focus_and_strip_lights` (alias "all lights"; the night sky is left out on purpose) | the three switches (hidden) |
| AV unit: the projector (Optoma HD300) and the amplifier | Home Theatre | Tuya AV mains (Main Switchboard gang 4) and the Broadlink's learned codes | the scripts below, and one button per learned code (`button.projector_*`, 18; `button.amplifier_*`, 4) | `script.home_theatre_av_on_steps`, `script.home_theatre_av_off_steps`, `input_select.home_theatre_av` (cooling while the projector cools), `input_select.home_theatre_projector`, `switch.home_theatre_av_mains` (hidden) |
| AC: a Blue Star, "AC667C" in Alexa, "home theatre AC" in the Blue Star app (thing id `d0cf13b4667c`) | Home Theatre | Blue Star's cloud, through huvu's `bluestar_ac` custom component (`home-assistant/custom_components/bluestar_ac/` in huvu's repo; written and tested 2026-10-10, not installed yet). Its state comes over MQTT the moment anyone changes it, and a command returns once the AC reports it | not yet: `climate.home_theatre_ac` once installed | `switch.home_theatre_ac_mains` (AC Switch gang 1, hidden) |
| Fire TV Stick (Dad's), with Alexa | Home Theatre | Alexa Devices, `alexa_devices.send_text_command` to its device (`a1a68fc0cd25ae52ee73428443881e53`), and its voice history through `alexa_history.fetch`; on the AV mains | `script.home_theatre_alexa` | `event.dad_s_fire_tv_stick_voice_event` (Alexa's latest record: `voice_command`, `intent`, `voice_reply`) |
| AC Switch gang 2 | Home Theatre | a dummy | no | `switch.home_theatre_ac_board_switch_2` (hidden) |
| Xiaomi Robot Vacuum-Mop 2 Pro | ground floor | cloud (Xiaomi Miot) | the vacuum | |

### Scripts exposed to huvu

All in Home Theatre, labelled `huvu_silent`. Only `home_theatre_alexa` has a field.

| Script | What it does | Checked on |
|---|---|---|
| `home_theatre_av_on` | Starts the steps and returns: waits out a running off, AV power on (and 10 s for the gear to listen), the amp's power, the projector's power, then 5 s and the amp's volume up 40 presses. Each press only when that part was off | not yet run |
| `home_theatre_on` | `home_theatre_av_on`, then the strip lights and the focus lights. Returns once the lights are on; the AV unit's steps run on after it. Added 2026-10-10 | `ha_setup` check passed 2026-10-10; not yet run |
| `home_theatre_off` | The strip lights and the focus lights off, then `home_theatre_av_off`, so the lights don't wait on the projector's 2 minutes of cooling. Returns once the lights are off; the AV unit's steps run on after it. Added 2026-10-10 | `ha_setup` check passed 2026-10-10; not yet run |
| `home_theatre_av_off` | Starts the steps and returns: stops a running on, the projector's power twice 3 s apart, the amp's power, then the AV power 2 minutes after the projector went off | not yet run |
| `home_theatre_projector_on` | The projector's power once. Fails when the AV unit is off, or the projector is on already | not yet run |
| `home_theatre_projector_off` | The projector's power twice, 3 s apart. Fails when the AV unit is off, or the projector is off already | not yet run |
| `home_theatre_mute` | Mute on the projector and on the amp, which toggles both. Fails when the AV unit is off | not yet run |
| `home_theatre_alexa` | Sends `command`, any text the model writes, to the Fire TV's Alexa as if spoken ("open YouTube", "play Dune on Prime Video"). Then it fetches the Fire TV's voice history (`alexa_history.fetch`) up to 8 times, a second apart, until a record from the send on shows, and returns it as `{heard, intent, reply}`: what Alexa heard, its intent, and what it said back. Fails when the AV mains is off, or when no record shows. Alexa's reply is often empty for music, so the intent is then the only sign it understood. Added 2026-10-10 in place of v2's `home_theatre_play`; reads Alexa's reply since later that day | `ha_setup` check passed 2026-10-10; not yet run with the reply |

### Sensors exposed for the dream

None of their own: the lights and the scripts' runs carry the state.

### Open problems

1. Model-facing text stays out of the package: entities get names, areas and aliases only, and scripts a one-line description of what they do. How a unit works and its waits belong in HA, never in a description.
1. A helper that holds state nobody can read starts as unknown, never as a guessed value.
1. On 2026-10-10 the AC's mains switched off 3 s after Alexa's "turn off", with no HA context, and the AC dropped off Blue Star's cloud 79 s after. The Blue Star app's "off" leaves the mains on (checked the same day: still online 2.5 minutes after), so something on Alexa's side (a routine or a group) works the Tuya switch. Find it and remove it before huvu works the AC: with its mains off, the AC is offline and no command reaches it.
1. The Blue Star component logs in with the Blue Star app's phone number and password. Blue Star's API is undocumented and could change; the Broadlink, learning the remote's codes, is the fallback. The AC takes whole degrees only, and its room temperature comes in half degrees. Its own commands go out as the app's (`src` `anmq`), so `last_change_by` reads "app" for them too; HA's context still tells huvu's commands apart. Whether a login from HA logs the phone's app out is not checked yet.
1. The 10 s wait for the gear to wake and the 5 s before the amp takes volume are guesses, not measured. The volume's 40 presses add to wherever the amp was, if it keeps its volume across power.
1. The theatre has no fans yet.
1. Renaming an entity's id drops its Assist exposure, and `customize` applies to an entity only when it is added: reload templates after `reload_core_config`. Expose again after every rename, and check `ha_setup`.
1. The Alexa routines (movie time, pause movie, movie lights out, play movie, home theatre off, daily vacuum, text) are buttons in HA but not exposed: what each does isn't written down yet.
1. Alexa Devices (with `aioamazondevices` 14.1.9) fetches the voice history only at startup and when an Echo pushes an equalizer change, which a spoken ask does and a typed command doesn't, and it keeps only each device's latest record. So a typed command's record showed only when someone next spoke to an Echo (17 minutes late on 2026-10-10), and two in a row lost the first. `alexa_history.fetch` calls the integration's own `sync_history_state()` on its coordinator, so an HA update that changes Alexa Devices' internals can break it. Check `home_theatre_alexa` after every HA update. How long Amazon takes to log a typed command isn't measured yet, so the 8 tries are a guess.
1. The Broadlink's Wi-Fi link is weak (70 to 90 ms pings, timeouts): HA lost it once on 2026-10-09 and came back on a reload. Its send errors ("Network timeout: No response received within 10s", seen 2026-10-10) are logged and swallowed by HA's Broadlink integration, so a script never sees them, and the power codes toggle, so they get no retry.
1. `configuration.yaml` has a `xiaomi_cloud_map_extractor` camera whose integration isn't installed, with a cloud password in plain text.
