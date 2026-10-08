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
