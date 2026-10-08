#!/usr/bin/env bash
# Wipes what huvu and Hermes keep for one person, or for the shared devices of
# the default group, so they start over. Stop the backend first.
#
#   ./wipe.sh <username>     a user: huvu's records and their Hermes profile
#   ./wipe.sh                the default group (home) and Hermes's default profile
#   add --yes to skip the question
#
# In huvu: triggers, conversations, Hermes session records, notifications,
# approvals, attendance and the event log (backend/src/huvu/wipe.py). In
# Hermes: every session, memory (MEMORY.md, USER.md) and every skill the
# model wrote. What stays: the account, devices, trackers, the sources'
# history tables, and the profile's settings and SOUL.md.
set -euo pipefail

root=$(cd "$(dirname "$0")" && pwd)
hermes_home=${HERMES_HOME:-$HOME/.hermes}

yes=""
args=()
for a in "$@"; do
  if [ "$a" = "--yes" ]; then yes=1; else args+=("$a"); fi
done
user=${args[0]:-}

if [ -n "$user" ]; then
  home=$hermes_home/profiles/$user
  p="-p $user"
  who="$user"
  wipe_args=("$user")
else
  home=$hermes_home
  p=""
  who="the default group (home) and Hermes's default profile"
  wipe_args=(--group home)
fi
[ -d "$home" ] || { echo "no Hermes profile at $home" >&2; exit 1; }

if [ -z "$yes" ]; then
  read -r -p "Wipe everything huvu and Hermes keep for $who? [y/N] " answer
  [ "$answer" = "y" ] || [ "$answer" = "Y" ] || exit 1
fi

# huvu's side. It refuses while the backend runs.
if [ -x "$root/backend/.venv/bin/huvu" ]; then (cd "$root/backend" && .venv/bin/huvu wipe "${wipe_args[@]}")
else (cd "$root/backend" && uv run --quiet huvu wipe "${wipe_args[@]}"); fi

# Hermes's sessions. Prune skips open ones, so each is deleted by id.
sessions=0
if [ -f "$home/state.db" ]; then
  for id in $(python3 -c 'import sqlite3, sys; [print(r[0]) for r in sqlite3.connect(sys.argv[1]).execute("SELECT id FROM sessions")]' "$home/state.db"); do
    hermes $p sessions delete --yes "$id" >/dev/null
    sessions=$((sessions + 1))
  done
fi

# Memory, and the skills the model wrote. Bundled skills are opted out, so a
# skill folder (one with a SKILL.md) is the model's own; huvu's huvu-* skills
# live in the repo, not here.
rm -f "$home/memories/MEMORY.md" "$home/memories/USER.md"
skills=0
if [ -d "$home/skills" ]; then
  while IFS= read -r skill; do
    rm -rf "$(dirname "$skill")"
    skills=$((skills + 1))
  done < <(find "$home/skills" -name SKILL.md -not -path '*/.*')
fi

echo "Hermes: $sessions sessions, memory, $skills skills."
