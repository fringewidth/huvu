# huvu's Hermes plugin

The Hermes side of the boundary with huvu's backend. What it does and why is in `docs/backend-architecture.md`, under The huvu plugin.

## Profiles

huvu runs one Hermes profile per user, named after their huvu username, and one per group for its shared devices (the TV, speakers). The default group, `home`, uses Hermes's default profile; any other group gets a profile named after it (`uv run huvu group add <name>` prints its MCP token). Each profile keeps its own memory, skills and cron jobs. One Hermes gateway serves them all at `/p/<profile>/`. See Hermes's docs on profiles and multi-profile gateways.

Every profile needs the setup below. Two values differ per profile:

| | Default profile (the `home` group) | A user's or another group's profile |
|---|---|---|
| `API_SERVER_KEY` in the profile's `.env` | The backend's `HERMES_API_KEY` | Typed in at `uv run huvu user add <name>` or `group add <name>` |
| `HUVU_MCP_TOKEN` in the profile's `.env` | The backend's `HUVU_MCP_TOKEN` | Printed once by the same command |

The backend tells profiles apart by the MCP token, so never give two profiles the same one.

## Setup, for each profile

For a user, `./add-user.sh <username> <password> [display name]` at the repo root does all of this and adds the huvu user too.

1. Create the profile, for a user: `hermes profile create <name>`. Leave out `--clone`, which copies the current profile's memory into the new one. The profile's home is `~/.hermes/profiles/<name>/`. The default profile already exists, at `~/.hermes/`.
1. Link the plugin into the profile: `ln -s "$PWD/huvu" <profile home>/plugins/huvu`.
1. Enable it and add the backend as an MCP server in the profile's `config.yaml`:

   ```yaml
   plugins:
     enabled: [huvu]

   mcp_servers:
     huvu:
       url: "http://localhost:3001/mcp"
       headers:
         Authorization: "Bearer ${HUVU_MCP_TOKEN}"

   auxiliary:
     background_review:
       enabled: false
   ```

1. Put the plugin's settings in the profile's `.env`:

   | Variable | Meaning | Default |
   |---|---|---|
   | `HUVU_URL` | The backend | `http://localhost:3001` |
   | `HUVU_MCP_TOKEN` | The profile's own token, which the backend checks on `/mcp` and `/api/situation` | none |
   | `HUVU_MCP_SERVER` | The server's name under `mcp_servers` | `huvu` |
   | `HUVU_SITUATION` | Comma-separated value keys for the situation block | `huvu.mode` |

1. Turn on the API server with the profile's `API_SERVER_KEY`, since the backend starts sessions there.

The MCP server's name must match `HUVU_MCP_SERVER`, because the plugin recognizes huvu's tools by their `mcp__<server>__` prefix.
