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

Android, on a phone plugged in over USB with USB debugging on:

```sh
cd frontend && npm run build:phone && npx cap sync android
cd android && JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home" ./gradlew assembleDebug
~/Library/Android/sdk/platform-tools/adb install -r app/build/outputs/apk/debug/app-debug.apk
```

The build talks to https://huvu.hrishik.me.

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
```

then:

```sh
docker compose up -d
docker compose exec -u hermes huvu hermes model          # log in to ChatGPT (device code)
docker compose exec -u hermes huvu /opt/huvu/setup.sh hrishik '<password>' Hrishik
docker compose restart huvu
```

Wipe someone there: `docker compose exec -u hermes huvu /opt/huvu/wipe.sh hrishik`
(it stops nothing for you: stop the backend first with
`docker compose exec huvu s6-svc -d /run/service/huvu`, and bring it back with
`s6-svc -u`). Update: `docker compose pull && docker compose up -d`.
