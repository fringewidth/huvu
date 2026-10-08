#!/usr/bin/env bash
# Sets Hermes up for huvu. Safe to run again: it only fills in what's missing
# and re-applies the settings.
#
#   ./setup.sh                                  Hermes's default profile, for shared devices
#   ./setup.sh <username> <password> [name]     a user and their profile (an existing
#                                               user's password is left as it is)
set -euo pipefail

root=$(cd "$(dirname "$0")" && pwd)
hermes_home=${HERMES_HOME:-$HOME/.hermes}
env_file=${HUVU_ENV_FILE:-$root/backend/.env}
huvu_url=${HUVU_URL:-http://localhost:3001}

# The backend's own command: its virtualenv's, or through uv in a checkout.
huvu() {
  if [ -x "$root/backend/.venv/bin/huvu" ]; then (cd "$root/backend" && .venv/bin/huvu "$@")
  else (cd "$root/backend" && uv run --quiet huvu "$@"); fi
}
random() { python3 -c 'import secrets; print(secrets.token_hex(32))'; }

# A secret kept in backend/.env, made the first time.
secret() {
  local value
  value=$(grep -s "^$1=" "$env_file" | cut -d= -f2- || true)
  if [ -z "$value" ]; then
    value=${2:-$(random)}
    echo "$1=$value" >> "$env_file"
  fi
  echo "$value"
}

# Adds the repo's hermes-skills/ to a profile's skills.external_dirs, keeping
# whatever else is there.
skills_dir() {
  local current
  current=$(hermes ${1:+-p "$1"} config get skills.external_dirs --json 2>/dev/null || echo null)
  hermes ${1:+-p "$1"} config set skills.external_dirs "$(python3 -c '
import json, sys
dirs = json.loads(sys.argv[1] or "null") or []
dirs = dirs if isinstance(dirs, list) else [dirs]
print(json.dumps(dirs if sys.argv[2] in dirs else [*dirs, sys.argv[2]]))
' "$current" "$root/hermes-skills")" >/dev/null
}

# What every huvu profile needs. $1 is the profile, empty for the default.
configure() {
  local p=${1:+-p $1} home=$2
  mkdir -p "$home/plugins"
  ln -sfn "$root/hermes-plugin/huvu" "$home/plugins/huvu"
  # huvu's identity. Copied, not linked: an agent may choose to change it.
  # Run again after editing the repo's SOUL.md.
  cp "$root/SOUL.md" "$home/SOUL.md"
  hermes $p config set API_SERVER_ENABLED true >/dev/null
  hermes $p config set HUVU_URL "$huvu_url" >/dev/null
  hermes $p config set mcp_servers.huvu.url "$huvu_url/mcp" >/dev/null
  hermes $p config set mcp_servers.huvu.headers.Authorization 'Bearer ${HUVU_MCP_TOKEN}' >/dev/null
  # Hermes's background review: after a turn, a forked review saves what it
  # learned about the person to memory and skills, which load into every new
  # session. Its "memory updated" line stays off the API server, so it never
  # lands in an answer on the stage.
  hermes $p config set auxiliary.background_review.enabled true >/dev/null
  hermes $p config set display.platforms.api_server.memory_notifications off >/dev/null
  # The model's reasoning summary reaches plugins only on this opt-in. The
  # huvu plugin forwards it to the backend, and the stage shows it faintly
  # while a turn runs.
  hermes $p config set plugins.stream_reasoning_deltas true >/dev/null
  # Hermes's coding-agent prompt blocks: build-run-verify, "run date in the
  # terminal", and the rest. huvu is no coding agent, and they cost about
  # 1,200 tokens on every request. Tool-use enforcement stays: GPT models
  # tend to promise an action and end the turn without calling the tool.
  hermes $p config set agent.execution_guidance false >/dev/null
  hermes $p config set agent.task_completion_guidance false >/dev/null
  # Hermes's bundled skills (Apple Notes, Manim, arXiv and 60 more) cost about
  # 1,700 tokens of index on every request. huvu's own skills come from
  # skills.external_dirs, and skills huvu writes itself are never removed.
  hermes $p skills opt-out --remove --yes >/dev/null
  # huvu's triggers are the only scheduler: the clock is a value like any
  # other, a script handles what needs no thought, and a wake handles what
  # does. A Hermes cron job made from the API server has nowhere to deliver,
  # costs a model turn per run, and never shows in the app.
  hermes $p tools disable --platform api_server cronjob >/dev/null
  # Every huvu-* skill is pinned fully loaded, so the model never spends a
  # round trip on skill_view before acting. The list is what the backend
  # wrote at its last start: run this again after adding a source.
  hermes $p config set skills.auto_load "$(python3 -c '
import json, os, sys
print(json.dumps(sorted(n for n in os.listdir(sys.argv[1]) if n.startswith("huvu-"))))
' "$root/hermes-skills")" >/dev/null
  # A new profile is blank, so it takes the default profile's model. OAuth
  # logins (ChatGPT, Claude) stay in the default's auth.json and are shared.
  if [ -n "$1" ]; then
    local k
    for k in model.provider model.default model.base_url agent.reasoning_effort; do
      hermes $p config set "$k" "$(hermes config get "$k" --raw | tail -1)" >/dev/null
    done
  fi
  skills_dir "$1"
  hermes $p plugins enable huvu >/dev/null
}

if [ $# -eq 0 ]; then
  # Keep the default profile's API key if it already has one.
  existing=$(hermes config get API_SERVER_KEY --raw 2>/dev/null | tail -1 | grep -Ex '[A-Za-z0-9._~+/=-]{16,}' || true)
  key=$(secret HERMES_API_KEY "$existing")
  token=$(secret HUVU_MCP_TOKEN)
  configure "" "$hermes_home"
  hermes config set API_SERVER_KEY "$key" >/dev/null
  hermes config set HUVU_MCP_TOKEN "$token" >/dev/null
  echo "Hermes's default profile is set up for huvu's shared devices. Restart Hermes: hermes gateway restart"
  exit 0
fi

[ $# -ge 2 ] || { sed -n 2,8p "$0" >&2; exit 1; }
user=$1
password=$2
name=${3:-$1}
home=$hermes_home/profiles/$user

if huvu user list | cut -f1 | grep -qx "$user"; then
  [ -d "$home" ] || { echo "$user exists in huvu but has no Hermes profile; remove them (huvu user remove $user) and run this again" >&2; exit 1; }
  configure "$user" "$home"
  echo "$user was already set up; re-applied their profile's settings."
  exit 0
fi

key=$(random)
token=$(HUVU_PASSWORD=$password HUVU_HERMES_KEY=$key huvu user add "$user" --name "$name" --quiet)
[ -d "$home" ] || hermes profile create "$user" --no-alias >/dev/null
configure "$user" "$home"
hermes -p "$user" config set API_SERVER_KEY "$key" >/dev/null
hermes -p "$user" config set HUVU_MCP_TOKEN "$token" >/dev/null
echo "Set up $user. Restart Hermes: hermes gateway restart"
