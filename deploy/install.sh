#!/bin/sh
# Example: copy Polar to a Mac that stays on, build the venv, install a LaunchAgent.
# Run it from your laptop: deploy/install.sh <ssh-host>
# The agent loads in the GUI session, so it can read the login keychain.
# Edit deploy/com.example.polar.plist first (owner id, URLs, label).
set -eu
HOST="${1:?usage: deploy/install.sh <ssh-host>}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
LABEL=com.example.polar
PLIST=$LABEL.plist

rsync -a --delete \
  --exclude '.venv/' --exclude 'data/' --exclude '.git/' --exclude '__pycache__/' \
  --exclude '.pytest_cache/' --exclude '.env*' \
  "$HERE/" "$HOST:polar/"

ssh "$HOST" LABEL="$LABEL" PLIST="$PLIST" 'sh -s' <<'REMOTE'
set -eu
cd "$HOME/polar"
mkdir -p data
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install --quiet -r requirements.txt
sed "s|__HOME__|$HOME|g" "deploy/$PLIST" > "$HOME/Library/LaunchAgents/$PLIST"
UID_NUM="$(id -u)"
launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
# bootout ends a moment later: a bootstrap in that moment can fail with «5: Input/output error». Try a few times.
n=0
until launchctl bootstrap "gui/$UID_NUM" "$HOME/Library/LaunchAgents/$PLIST" 2>/dev/null; do
  n=$((n + 1))
  if launchctl print "gui/$UID_NUM/$LABEL" >/dev/null 2>&1; then break; fi
  [ "$n" -lt 10 ] || { echo "Polar did not load: launchctl bootstrap failed" >&2; exit 1; }
  sleep 1
done
launchctl kickstart -k "gui/$UID_NUM/$LABEL"
echo "Polar loaded. Check: tail -f $HOME/polar/data/polar.log"
REMOTE
