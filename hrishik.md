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

For the phone on Wi-Fi without USB, build with the laptop's address instead:
`VITE_BACKEND_URL=http://<laptop ip>:3001 npm run build:phone`.
