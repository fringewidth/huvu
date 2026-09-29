# huvu frontend

Every script names its build target. There is no default.

```sh
npm run dev:web      # or dev:phone, dev:firetv
npm run build:web    # or build:phone, build:firetv
npm run lint
```

The mock backend (`../backend/server.js`) runs with `node ../backend/server.js` on port 3001. Add `?seed` to a dev URL for a few seeded rich turns.

See `../CLAUDE.md` for the architecture.
