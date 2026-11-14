#!/bin/bash
# The container's front door. The demo and trials run as root and play the person as uid 10001.
set -e
WAKE_DIR=${WAKE_DIR:-/opt/wake}
cmd=${1:-demo}
[ $# -gt 0 ] && shift
case "$cmd" in
  demo)
    exec "$WAKE_DIR/demo/demo.sh" ;;
  trials)
    cd "$WAKE_DIR"
    echo "== tests";            setpriv --reuid=10001 --regid=10001 --clear-groups env HOME=/tmp PYTHONPATH="$WAKE_DIR" python3 -m unittest discover -s tests
    echo "== Law 1";            python3 trials/law1.py
    echo "== Law 2";            python3 trials/law2.py
    echo "== incidents";        python3 trials/incidents.py ;;
  shell)
    exec setpriv --reuid=10001 --regid=10001 --init-groups env HOME=/home/person bash -l ;;
  run)
    exec setpriv --reuid=10001 --regid=10001 --init-groups env HOME=/home/person wake run "$@" ;;
  *)
    exec "$cmd" "$@" ;;
esac
