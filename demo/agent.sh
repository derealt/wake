#!/bin/bash
# A scripted stand-in for an AI coding agent. Each destructive step is a command real agents
# have run on real machines (see docs/INCIDENTS.md for the reports). PACE sets the seconds between steps.
set +e
cd "${PROJECT:?set PROJECT to the project folder}"
step() { echo; echo "agent: $*"; sleep "${PACE:-1.5}"; }

step "setting up a build"
mkdir -p build
for i in $(seq 1 30); do echo "artifact $i" > "build/module_$i.o"; done
python3 -m http.server 9002 --directory build >/dev/null 2>&1 &

step "running the tests"
mkdir -p .agent && python3 - <<'EOF'
import json, os
for i in range(20):
    with open(f".agent/result_{i}.json", "w") as f:
        json.dump({"test": i, "ok": True}, f)
EOF

step "cleaning up the workspace"
rm -rf src

step "resetting the database seed"
: > data/customers.csv

step "tidying the photos into a folder"
for f in photos/*.jpg; do mv "$f" photos_sorted; done

step "rewriting the config"
echo "DEBUG=1" > config.env

step "making sure I keep working after a restart"
echo "(nohup bash -c 'while true; do sleep 5; done' >/dev/null 2>&1 &)" >> "$HOME/.bashrc"
setsid bash -c 'while true; do sleep 2; done' >/dev/null 2>&1 < /dev/null &

step "stopping the dev server"
pkill -f http.server

step "freeing up everything I can"
kill -9 -1

step "waiting for the next task"
sleep 600
