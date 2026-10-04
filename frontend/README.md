# huvu frontend

Every script names its build target. There is no default.

```sh
npm run dev:web      # or dev:phone, dev:firetv
npm run build:web    # or build:phone, build:firetv
npm run lint
```

Chat needs the Python backend on port 3001 (`uv run huvu` in `../backend`) with Hermes behind it. Add `?seed` to a dev URL for a few seeded rich turns.

See `../CLAUDE.md` for the architecture.
