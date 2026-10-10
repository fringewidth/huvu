```sh
./setup.sh
./setup.sh hrishik '<password>' Hrishik
```

Start over (stop the backend first):

```sh
./wipe.sh hrishik
./wipe.sh
```

Run it, each in its own terminal:

```sh
hermes gateway run
cd backend && uv run huvu
cd frontend && npm run dev:web
```

Then open http://localhost:5173. The dev server talks to the backend on
http://localhost:3001 (or `VITE_BACKEND_URL`); every build talks to
https://huvu.hrishik.me.

Speech, once per backend machine:

```sh
echo 'GROQ_API_KEY=gsk_...' >> backend/.env
```

huvu speaks with Edge's voices, which need nothing set up.

The router's Clef-flash, once per backend machine: a Cloudflare API token
from the "Workers AI" template, and the account's ID. Without them, asks
that aren't house commands all run on pro.

```sh
echo 'CLOUDFLARE_ACCOUNT_ID=...' >> backend/.env
echo 'CLOUDFLARE_AI_TOKEN=...' >> backend/.env
```

Android, on a phone plugged in over USB with USB debugging on:

```sh
cd frontend && scripts/apk.sh phone
~/Library/Android/sdk/platform-tools/adb install -r android/app/build/outputs/apk/debug/app-debug.apk
```

The build talks to https://huvu.hrishik.me.

Fire TV, with ADB debugging on (Settings, My Fire TV, About: press select
on the device name 7 times, then Developer options, ADB debugging) and the
TV's address from Settings, My Fire TV, About, Network. The TV asks once
to allow the Mac's key. Both must be on the same Wi-Fi.

```sh
cd frontend && scripts/apk.sh firetv
~/Library/Android/sdk/platform-tools/adb connect <tv-ip>:5555
~/Library/Android/sdk/platform-tools/adb install -r android/tv/build/outputs/apk/debug/tv-debug.apk
```

The TV app loads its page from https://huvu.hrishik.me/tv/, so a frontend
change reaches it with the next image, not a new APK. To try a change
before that, serve the TV build from this Mac and point the app at it:

```sh
cd frontend && VITE_BACKEND_URL=https://huvu.hrishik.me npx vite --mode firetv --port 5174 --host
~/Library/Android/sdk/platform-tools/adb shell am start -S -n com.huvu.tv/.MainActivity -e url http://<mac-ip>:5174/
```

The host (Windows laptop, `ssh laptop`, the repo at `D:\huvu`, Docker
Desktop): huvu and Hermes in one container, on http://localhost:27613,
published at https://huvu.hrishik.me by the cloudflared already running
there. That address serves the web client and the backend both.

The tunnel, once: in the Cloudflare dashboard (Zero Trust, Networks,
Tunnels), on the existing tunnel (immich's), add a public hostname
`huvu.hrishik.me` with service `http://host.docker.internal:27613`. That
cloudflared runs in a container, where localhost is the container itself.

Next to `compose.yaml`, a `.env`:

```sh
GROQ_API_KEY=gsk_...
CLOUDFLARE_ACCOUNT_ID=...
CLOUDFLARE_AI_TOKEN=...
```

then:

```sh
docker compose up -d
docker compose exec -u hermes huvu hermes model          # log in to ChatGPT (device code)
docker compose exec -u hermes huvu /opt/huvu/setup.sh hrishik '<password>' Hrishik
docker compose restart huvu
```

From the Mac, everything goes over `ssh laptop "..."`. Its shell is cmd:
single quotes stay in the arguments, so quote with double quotes. On the
laptop itself, drop the wrapper.

Deploy what's on GitHub (push first; the laptop pulls with a read-only
deploy key, `C:\Users\Hrishik\.ssh\huvu_deploy`):

```sh
ssh laptop "cd /d D:\huvu && git pull --ff-only origin main"
ssh laptop "cd /d D:\huvu && docker compose build"
ssh laptop "cd /d D:\huvu && docker compose up -d"
ssh laptop "docker image prune -f"       # anything a build left untagged
```

Asks fail for about 65 seconds after a start, until the backend and then
Hermes are up. Docker can't reach Windows's credential store over SSH, so
a build that names a base image the laptop doesn't have fails with "A
specified logon session does not exist": `docker pull` it first (the
`FROM` lines in the `Dockerfile`). The prune removes untagged images no
container uses. Hermes's pinned image is pulled by digest, so it has no
tag of its own and a prune would take it: keep it tagged
(`docker tag nousresearch/hermes-agent@sha256:<the pin> nousresearch/hermes-agent:huvu-pin`,
again whenever the pin moves). A rebuild on the same Hermes leaves
nothing behind; one that moves the pin leaves the old image, about 3 GB.

Is it up:

```sh
curl -s https://huvu.hrishik.me/health
ssh laptop "docker logs --tail 50 huvu-huvu-1"
ssh laptop "docker exec huvu-huvu-1 curl -s http://127.0.0.1:8642/health"   # Hermes's API server
```

Restart everything, or stop and start the backend alone (a restart also
clears the login limit):

```sh
ssh laptop "docker restart huvu-huvu-1"
ssh laptop "docker exec huvu-huvu-1 /command/s6-svc -d /run/service/huvu"
ssh laptop "docker exec huvu-huvu-1 /command/s6-svc -u /run/service/huvu"
```

Users. Add with a throwaway password and set the real one at the prompt,
so it never shows on a command line; the restart lets Hermes load the
new profile:

```sh
ssh laptop "docker exec -u hermes -w /opt/huvu/backend huvu-huvu-1 .venv/bin/huvu user list"
ssh laptop "docker exec -u hermes -e HOME=/opt/data huvu-huvu-1 /opt/huvu/setup.sh mom TEMPPASS Mom"
ssh -t laptop "docker exec -it -u hermes -w /opt/huvu/backend huvu-huvu-1 .venv/bin/huvu user passwd mom"
ssh laptop "docker restart huvu-huvu-1"
```

setup.sh asks what people call each other only at a terminal, so over
`docker exec` without `-it` it skips that. Set them one at a time (owner,
person, alias; "" goes back to the name), or list one person's:

```sh
ssh laptop "docker exec -u hermes -w /opt/huvu/backend huvu-huvu-1 .venv/bin/huvu user alias hrishik mom Amma"
ssh laptop "docker exec -u hermes -w /opt/huvu/backend huvu-huvu-1 .venv/bin/huvu user aliases hrishik"
```

Wipe someone (no name: the home group and Hermes's default profile). It
refuses while the backend runs:

```sh
ssh laptop "docker exec huvu-huvu-1 /command/s6-svc -d /run/service/huvu"
ssh laptop "docker exec -u hermes -e HOME=/opt/data huvu-huvu-1 /opt/huvu/wipe.sh hrishik --yes"
ssh laptop "docker exec huvu-huvu-1 /command/s6-svc -u /run/service/huvu"
```

Start huvu's data over (a schema change; Hermes's login and profiles
stay), then add every user again:

```sh
ssh laptop "docker exec huvu-huvu-1 /command/s6-svc -d /run/service/huvu"
ssh laptop "docker exec -u hermes huvu-huvu-1 mv /opt/data/huvu/data /opt/data/huvu/data.old"
ssh laptop "docker restart huvu-huvu-1"
```

Log Hermes in to ChatGPT again (asks fail with an auth error):

```sh
ssh -t laptop "docker exec -it -u hermes -e HOME=/opt/data huvu-huvu-1 hermes auth add openai-codex --type oauth --no-browser"
```

Hermes is pinned by digest in the `Dockerfile`. To move it, change the
digest, deploy, and send one ask; "aiohttp not installed" in the
gateway's log means go back.

Where things are: the Groq key in `D:\huvu\.env`; huvu's own secrets,
its data, and Hermes's profiles and login in the `huvu_huvu` volume
(`/opt/data`, huvu's under `/opt/data/huvu`); the tunnel's route on
immich's tunnel, served by the `immich-cloudflared-1` container.
