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

huvu() { (cd "$root/backend" && uv run --quiet huvu "$@"); }

# A secret kept in backend/.env, made the first time.
secret() {
  local value
  value=$(grep -s "^$1=" "$env_file" | cut -d= -f2- || true)
  if [ -z "$value" ]; then
    value=${2:-$(openssl rand -hex 32)}
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
  hermes $p config set API_SERVER_ENABLED true >/dev/null
  hermes $p config set HUVU_URL "$huvu_url" >/dev/null
  hermes $p config set mcp_servers.huvu.url "$huvu_url/mcp" >/dev/null
  hermes $p config set mcp_servers.huvu.headers.Authorization 'Bearer ${HUVU_MCP_TOKEN}' >/dev/null
  hermes $p config set auxiliary.background_review.enabled false >/dev/null
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

key=$(openssl rand -hex 32)
token=$(cd "$root/backend" && HUVU_PASSWORD=$password HUVU_HERMES_KEY=$key uv run --quiet huvu user add "$user" --name "$name" --quiet)
[ -d "$home" ] || hermes profile create "$user" --no-alias >/dev/null
configure "$user" "$home"
hermes -p "$user" config set API_SERVER_KEY "$key" >/dev/null
hermes -p "$user" config set HUVU_MCP_TOKEN "$token" >/dev/null
echo "Set up $user. Restart Hermes: hermes gateway restart"
