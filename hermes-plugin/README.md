# huvu's Hermes plugin

The Hermes side of the boundary with huvu's backend. What it does and why is in `docs/backend-architecture.md`, under Session taint.

## Setup

1. Link the plugin into Hermes: `ln -s "$PWD/huvu" ~/.hermes/plugins/huvu`.
1. Enable it and add the backend as an MCP server in `~/.hermes/config.yaml`:

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

1. Give Hermes's environment the plugin's settings:

   | Variable | Meaning | Default |
   |---|---|---|
   | `HUVU_URL` | The backend | `http://localhost:3001` |
   | `HUVU_MCP_TOKEN` | The token the backend checks on `/mcp` and `/api/situation` | none |
   | `HUVU_MCP_SERVER` | The server's name under `mcp_servers` | `huvu` |
   | `HUVU_SITUATION` | Comma-separated value keys for the situation block | `huvu.mode` |

1. Turn on Hermes's API server, since the backend starts its sessions there, and give the backend the same key as `HERMES_API_KEY`.

The MCP server's name must match `HUVU_MCP_SERVER`, because the plugin recognizes huvu's tools by their `mcp__<server>__` prefix.
