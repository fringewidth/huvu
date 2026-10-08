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

Then open http://localhost:5173.

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
~/Library/Android/sdk/platform-tools/adb reverse tcp:3001 tcp:3001
```

For the real thing, build against the host:
`VITE_BACKEND_URL=https://huvu.hrishik.me npm run build:phone`.

The host (Windows, Docker Desktop): huvu and Hermes in one container, on
http://localhost:27613, published at https://huvu.hrishik.me by the
cloudflared already running there.

The tunnel, once: in the Cloudflare dashboard (Zero Trust, Networks,
Tunnels), on the existing tunnel, add a public hostname `huvu.hrishik.me`
with service `http://localhost:27613`. (A tunnel run from a local
config.yml instead gets an ingress rule with the same two values.)

Next to `compose.yaml`, a `.env`:

```sh
HUVU_PUBLIC_URL=https://huvu.hrishik.me
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
