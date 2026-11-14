#!/bin/bash
# The WAKE demo, narrated. A person's world, their server, an agent that does what real agents did,
# the live view, the stop, and everything put back.
set -u
WAKE_DIR=${WAKE_DIR:-/opt/wake}
PERSON_HOME=${PERSON_HOME:-/home/person}
PUID=${PUID:-10001}
WAKE=${WAKE_CMD:-wake}
PACE=${PACE:-2.5}
STATE=$PERSON_HOME/.local/state/wake
as_person() {
  setpriv --reuid="$PUID" --regid="$PUID" --clear-groups env HOME="$PERSON_HOME" WAKE_HOME="$STATE" \
    PATH="$PATH" PYTHONPATH="${PYTHONPATH:-}" DO_NOT_TRACK=1 PROJECT="$PERSON_HOME/project" PACE="$PACE" "$@"
}
say() { printf '\n\033[1m%s\033[0m\n' "$*"; sleep "${SAY_PAUSE:-1.5}"; }
serving() { python3 -c "import socket,sys; s=socket.socket(); s.settimeout(0.5); sys.exit(s.connect_ex(('127.0.0.1', $1)))"; }

say "WAKE: an AI agent can only destroy what it made. When you stop it, everything it made stops with it."
rm -rf "$PERSON_HOME/project" "$STATE"
mkdir -p "$PERSON_HOME" && chown "$PUID:$PUID" "$PERSON_HOME"
as_person python3 "$WAKE_DIR/demo/world.py" "$PERSON_HOME" >/dev/null
as_person mkdir -p "$STATE"
(cd "$PERSON_HOME" && find . -type f -not -path './.local/*' -print0 | sort -z | xargs -0 sha256sum) > /tmp/before.sha
say "A person's machine: a project with source, a customer list of 1,206 rows, photos, notes and a config."
as_person bash -c "cd \$PROJECT && setsid python3 -m http.server 9001 >/dev/null 2>&1 < /dev/null &"
sleep 0.5
say "Their dev server is running on port 9001. An agent starts work, under WAKE."
as_person bash -c "setsid $WAKE run --keep-running -- bash $WAKE_DIR/demo/agent.sh > /tmp/agent.log 2>&1 < /dev/null &"
sleep 1
as_person bash -c "setsid $WAKE view --bind 0.0.0.0 --no-browser > /tmp/view.log 2>&1 < /dev/null &"
sleep 1
say "Watch the agent's body live: http://127.0.0.1:7700"
seen=0
for _ in $(seq 1 240); do
  n=$(grep -c '^agent:' /tmp/agent.log 2>/dev/null || true)
  while [ "$seen" -lt "$n" ]; do seen=$((seen + 1)); sed -n "/^agent:/p" /tmp/agent.log | sed -n "${seen}p" | sed 's/^/  /'; done
  grep -q 'waiting for the next task' /tmp/agent.log 2>/dev/null && break
  sleep 0.25
done
sleep 1
say "To the agent, every command worked. Here is the person's project now:"
ls -la "$PERSON_HOME/project" | sed 's/^/  /'
printf '  customers.csv: %s bytes\n' "$(stat -c %s "$PERSON_HOME/project/data/customers.csv")"
say "Now the person stops the agent."
as_person $WAKE stop 2>&1 | sed 's/^/  /'
if serving 9001; then echo "  The person's server on port 9001: still running."; else echo "  The person's server on port 9001: gone."; fi
say "And puts everything back."
as_person $WAKE restore 2>&1 | sed 's/^/  /'
total=$(wc -l < /tmp/before.sha)
bad=$(cd "$PERSON_HOME" && sha256sum -c --quiet /tmp/before.sha 2>/dev/null | wc -l)
echo "  $((total - bad)) of the person's $total files are back, byte for byte."
say "Ask the graph who made what:"
as_person $WAKE whose "$PERSON_HOME/project/data/customers.csv" 2>&1 | sed 's/^/  /'
say "The live view stays up at http://127.0.0.1:7700. Ctrl-C to leave."
[ "${DEMO_EXIT:-0}" = 1 ] && exit 0
sleep infinity
