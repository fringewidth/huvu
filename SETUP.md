# Setting up huvu

This file is for an agent that sets huvu up for its user. Read `CLAUDE.md` first: it says why each part is built the way it is. Do the steps in order. Check each one before you go to the next.

huvu is one Docker container. It holds Hermes (the agent) and huvu's backend, which also serves the web app. The container listens on the host machine only. A tunnel gives it one public HTTPS address, and every app (web, Android, iPhone, Fire TV) talks to that address. Each family member is a user with their own Hermes profile, so each person has their own agent, memory and wiki.

## Rules for you

1. Ask before you change anything outside the repo and the container: DNS records, a domain's mail (MX) records, a Cloudflare account, the user's Google or Amazon accounts. Show the user the current state before you overwrite it, and keep a copy of it.
1. Never put a secret in the repo. Secrets go in `.env` next to `compose.yaml`, or in the container's volume (`/opt/data/huvu/.env`).
1. A password, an OAuth login or a device-code login is the user's to do. Give them the exact command, and let them run it.
1. Never let huvu pay for anything. The Hermes plugin blocks Swiggy's order and payment tools. Keep that block when you add a service with a checkout.

## 1. Ask the user

Get these answers before you start:

1. **The host.** A machine that stays on: Linux, macOS, or Windows with Docker Desktop. It needs about 6 GB of disk and 4 GB of free memory. The browser's virtual screen needs 1.5 GB of that.
1. **The address.** A domain on Cloudflare, and the hostname huvu will have, such as `huvu.example.com`. Any other way to get public HTTPS works too, but this file uses a Cloudflare Tunnel.
1. **The model login.** A ChatGPT plan or a Claude plan that Hermes logs in to. The tiers in `huvu.toml` must name models that login can use.
1. **The family.** For each person: a username, a display name, and what each person calls the others (such as "mom").
1. **The house's timezone**, as an IANA name, such as `Europe/Berlin`.
1. **The parts they want.** All are optional: voice (a free Groq key), the router (a free Cloudflare Workers AI token), Android push (their own Firebase project), location (OwnTracks), browsing (huvu's Chrome extension), mail (Cloudflare Email Routing), Home Assistant, Fire TV and Alexa, Swiggy.

## 2. Set their address

No address is written in the code. The user's huvu address goes in a `.env` next to `compose.yaml` (gitignored), and every part reads it from there:

```sh
HUVU_PUBLIC_URL=https://huvu.example.com
```

1. `compose.yaml` passes it to the backend, and to the image's build, whose phone bundle carries it.
1. The phone app's build (`vite.config.ts`) and the Fire TV app's build (`frontend/android/tv/build.gradle`) read it from the same file, or from the environment. Each fails with a clear error without it.
1. The web app and the TV's page use the origin they're served from, so they need nothing.
1. The Chrome extension has no address of its own. The user types it in its options.

Two things in the repo still belong to the owner, and a new install replaces both: `frontend/android/app/google-services.json` (the owner's Firebase project, step 9), and the default of `HUVU_ALEXA_SKILL_ID` in `compose.yaml` (the owner's Alexa skill; set it in `.env`).

## 3. Build and start the container

On the host, in the repo:

```sh
docker compose build
docker compose up -d
```

Optional settings go in a `.env` next to `compose.yaml`:

```sh
GROQ_API_KEY=gsk_...          # voice: speech to text (console.groq.com, free)
YOUTUBE_API_KEY=...           # YouTube: a Google Cloud API key with the YouTube Data API v3 on (free, about 100 searches a day)
CLOUDFLARE_ACCOUNT_ID=...     # the router: Clef-flash on Workers AI (free tier);
CLOUDFLARE_AI_TOKEN=...       # without these, every ask runs on the pro tier
HUVU_PORT=27613               # only to move the port on the host
```

The `Dockerfile` pins Hermes's image by digest. Don't move the pin: a newer Hermes build once shipped without its API server. Docker Desktop on Windows can't reach its credential store over SSH, so a build started over SSH fails with "A specified logon session does not exist" for a base image the host lacks. Run `docker pull` for each `FROM` image once, at the host itself.

The first start takes about three minutes: `setup.sh` runs for every profile. Hermes's API server comes up about 20 seconds after the backend, and an ask in that window fails. Check:

```sh
docker compose exec huvu curl -s http://127.0.0.1:3001/health     # huvu
docker compose exec huvu curl -s http://127.0.0.1:8642/health     # Hermes
```

## 4. Publish it

In Cloudflare (Zero Trust, Networks, Tunnels), create a tunnel, or use one the host already runs. Add a public hostname: the user's huvu hostname, with service `http://localhost:27613`. If `cloudflared` itself runs in a container, use `http://host.docker.internal:27613`: in a container, localhost is the container. Never publish the port any other way. The container binds it to `127.0.0.1` on purpose.

Check from outside the house: `curl -s https://<their hostname>/health` answers, the web app loads, and every API route answers 401 without a token.

## 5. Log Hermes in and set the timezone

The user runs the login, at a terminal on the host:

```sh
docker compose exec -it -u hermes huvu hermes model
```

They pick their provider and finish its device-code login. Then set the timezone and the profiles up:

```sh
docker compose exec -u hermes huvu /opt/huvu/setup.sh --timezone <IANA name>
```

If they log in to Claude, edit the tiers in `/opt/data/huvu/huvu.toml`, for example `pro = { model = "<a Claude model>", provider = "anthropic" }`. The defaults name ChatGPT models.

## 6. Add each person

For each family member, at a terminal (it asks what each person calls the others):

```sh
docker compose exec -it -u hermes huvu /opt/huvu/setup.sh <username> '<password>' '<Name>'
docker compose restart huvu
```

Each person logs in to the web app with their username and password. Change an alias later with `huvu user alias <owner> <person> <alias>`. huvu's own command in the container is `/opt/huvu/backend/.venv/bin/huvu`, run as `hermes` from `/opt/huvu/backend`.

`setup.sh` makes each person's wiki folder and raises Hermes's memory cap on every start, so the wiki needs no setup. Each person's dream runs every night at `[dream] at` (04:00) and fills their wiki.

## 7. huvu's config

The config is `/opt/data/huvu/huvu.toml` in the volume. The first start copies it from `docker/huvu.toml`. `backend/huvu.toml.example` explains every setting. Each driver is one `[[sources]]` entry. Restart the backend after an edit:

```sh
docker compose exec huvu /command/s6-svc -r /run/service/huvu
```

## 8. Drivers

Add only the ones the user wants. Each person's data is theirs alone: nobody else, family included, sees it through huvu.

1. **Location (OwnTracks).** Add the `location` source with the home's coordinates (`backend/huvu.toml.example`). For each person: `huvu tracker add <username>` prints a QR code that sets OwnTracks up on their phone. Waypoints such as "office" are made by asking huvu.
1. **Browsing (desktop Chrome).** Add the `browser` source. `huvu tracker add <username> --browser` prints a token. In Chrome, load `browser-extension/` unpacked, and paste the token and the address in its options. It sends page titles only, never addresses.
1. **Mail.** Add the `mail` source. In Cloudflare, enable Email Routing for a subdomain only, such as `mail.example.com`. Enabling it on the zone apex replaces the apex's MX records, so check those first. Then:
   1. `huvu tracker add <username> --mail` prints `HUVU_URL` and `HUVU_TOKEN`.
   1. In `mail-worker/`, run `npx wrangler login` (the user does this), then `npx wrangler deploy`. Set the secrets: `npx wrangler secret put HUVU_URL`, `HUVU_TOKEN`, and `FORWARD_TO` (the person's own inbox, which gets a copy of every mail). `FORWARD_TO` must be a destination address verified in Email Routing.
   1. Add a routing rule: `npx wrangler email routing rules create <zone> --match-type literal --match-field to --match-value <address> --action-type worker --action-value huvu-mail`.
   1. The user points the services they want at that address (a shop's account email), or forwards mail to it with a filter. Never both a filter to the address and `FORWARD_TO` to the same inbox: each mail would go round forever.
   1. Check: a mail sent to the address shows in `mail_messages` within seconds, with `signed_by` naming the sender's domain.
1. **Home Assistant.** Follow `docs/home-assistant.md`, which is written for agents. huvu needs its own HA user, an administrator, and its long-lived token as `HA_TOKEN` in `/opt/data/huvu/.env`.
1. **Swiggy (India).** The user logs in once for each server. huvu blocks every Swiggy tool that orders or pays, so the user always checks out in the Swiggy app. Run, in a real terminal:
   ```sh
   docker compose exec -it -u hermes -e HOME=/opt/data huvu hermes -p <username> mcp add swiggy_food --url https://mcp.swiggy.com/food --auth oauth --connect-timeout 600
   ```
   Hermes prints a login URL. The user opens it, logs in, and pastes back the `127.0.0.1` address their browser lands on. Do the same for `swiggy_instamart` at `https://mcp.swiggy.com/im`. Keep the server names: the plugin's block matches names that start with `swiggy`. Then limit each server to the tools huvu allows (`SWIGGY_ALLOWED` in `hermes-plugin/huvu/__init__.py`) with `hermes -p <username> config set mcp_servers.<server>.tools.include '[...]'`, and restart the container.
1. **Any other MCP server.** `hermes -p <username> mcp add <name> ...` adds it to that person's profile only. Restart the container afterwards.

## 9. The apps

1. **Web.** The address itself. It's served from the container, so nothing needs building.
1. **iPhone.** Open the address in Safari, Share, Add to Home Screen, then log in from the home screen app. Turn notifications on from a tap in its menu. iOS allows push only to the home screen app.
1. **Android.** Build the APK on a machine with Android Studio. In `frontend/`, run `npm install`, and create `.env.phone` holding `VITE_TARGET=phone` (gitignored; a build fails without its target). Then run `scripts/apk.sh phone`, with `JAVA_HOME` set to Android Studio's JDK. Run one Gradle build at a time: two at once corrupt the SDK's build tools. For push, the user makes a Firebase project with an Android app `com.huvu.app`, and you replace `frontend/android/app/google-services.json` with its file. Put the project's service account key in the volume (`/opt/data/huvu/fcm.json`, readable by `hermes` only), and set `HUVU_FCM_CREDENTIALS=/opt/data/huvu/fcm.json` in `/opt/data/huvu/.env`. Install over USB or wireless debugging. Later frontend changes reach the phone by themselves through live updates. Bump `frontend/native.json` and build a new APK only when the native side changes.
1. **Fire TV.** `frontend/scripts/apk.sh firetv`, installed over ADB. The TV shows a code, and a family member confirms it on their phone to pair it. Fire TV gives apps no microphone, so asks come through a custom Alexa skill: build one from `docs/alexa-interaction-model.json` in the user's Alexa developer console, leave it in development stage, set its endpoint to `https://<their hostname>/api/alexa` with the SSL certificate type set, and set `HUVU_ALEXA_SKILL_ID` in `.env`. Pick its invocation name by what Alexa's voice history shows it heard.

## 10. Check everything

Go through `docs/sanity-check.md` with the user. It lists, for each feature, what to do and what they should see. Fix what fails before the family relies on it.

## Running it

1. **Logs:** `docker logs --tail 50 <container>`.
1. **Restart everything:** `docker compose restart huvu`. Only the backend: `docker compose exec huvu /command/s6-svc -r /run/service/huvu`.
1. **Update:** pull the repo, `docker compose build`, `docker compose up -d`, then `docker image prune -f`. Keep Hermes's pinned image tagged, or the prune deletes it: `docker tag nousresearch/hermes-agent@sha256:<the pin> nousresearch/hermes-agent:huvu-pin`.
1. **Wipe a person's data:** their account stays, and it refuses while the backend runs.
   ```sh
   docker compose exec huvu /command/s6-svc -d /run/service/huvu
   docker compose exec -u hermes -e HOME=/opt/data huvu /opt/huvu/wipe.sh <username> --yes
   docker compose exec huvu /command/s6-svc -u /run/service/huvu
   ```
1. **All of huvu's state** is in the `huvu` Docker volume (`/opt/data`): Hermes's profiles and logins, huvu's config, data and secrets. Back it up as a whole.
