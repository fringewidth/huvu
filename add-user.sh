#!/usr/bin/env bash
# Adds a huvu user and their Hermes profile.
# Usage: ./add-user.sh <username> <password> [display name]
set -euo pipefail

[ $# -ge 2 ] || { echo "usage: $0 <username> <password> [display name]" >&2; exit 1; }
user=$1
password=$2
name=${3:-$1}
root=$(cd "$(dirname "$0")" && pwd)
home=~/.hermes/profiles/$user
key=$(openssl rand -hex 32)

token=$(cd "$root/backend" && HUVU_PASSWORD=$password HUVU_HERMES_KEY=$key uv run --quiet huvu user add "$user" --name "$name" --quiet)

hermes profile create "$user" --no-alias
mkdir -p "$home/plugins"
ln -sfn "$root/hermes-plugin/huvu" "$home/plugins/huvu"
hermes -p "$user" config set API_SERVER_ENABLED true
hermes -p "$user" config set API_SERVER_KEY "$key"
hermes -p "$user" config set HUVU_MCP_TOKEN "$token"
hermes -p "$user" config set HUVU_URL "${HUVU_URL:-http://localhost:3001}"
hermes -p "$user" config set mcp_servers.huvu.url "${HUVU_URL:-http://localhost:3001}/mcp"
hermes -p "$user" config set mcp_servers.huvu.headers.Authorization 'Bearer ${HUVU_MCP_TOKEN}'
hermes -p "$user" config set auxiliary.background_review.enabled false
hermes -p "$user" plugins enable huvu
