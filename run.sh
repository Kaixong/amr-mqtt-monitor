#!/usr/bin/env bash
# Usage: ./run.sh start | stop | restart | status | logs | test
cd "$(dirname "$0")"
PY="$PWD/.venv/bin/python"
SERVICES=(simulator sysmon alarmd)
mkdir -p logs

is_running() { [ -f "logs/$1.pid" ] && kill -0 "$(cat "logs/$1.pid")" 2>/dev/null; }

start_one() { # name dir script args...
  local name=$1 dir=$2; shift 2
  if is_running "$name"; then echo "$name already running"; return; fi
  (cd "$dir" || exit 1
   setsid nohup "$PY" -u "$@" > "../logs/$name.log" 2>&1 &
   echo $! > "../logs/$name.pid")
  echo "$name started"
}

stop_one() {
  if is_running "$1"; then kill "$(cat "logs/$1.pid")"; echo "$1 stopped"; else echo "$1 not running"; fi
  rm -f "logs/$1.pid"
}

start_all() {
  # first run: create MQTT users/passwords and the broker's secrets
  [ -f .env ] && [ -f mosquitto/.auth-ready ] && [ -f dashboard/config.js ] || ./scripts/setup-auth.sh
  set -a; . ./.env; set +a
  docker compose up -d
  start_one alarmd alarm alarmd.py
  start_one simulator simulator main.py
  start_one sysmon monitor sysmon.py
  echo "dashboard: http://localhost:8080   alarm log: http://localhost:8080/alarms.html"
}

stop_all() { for n in "${SERVICES[@]}"; do stop_one "$n"; done; }

case "${1:-}" in
  start) start_all ;;
  stop) stop_all ;;
  restart) stop_all; sleep 1; start_all ;;
  status)
    docker compose ps --format 'table {{.Service}}\t{{.Status}}'
    for n in "${SERVICES[@]}"; do is_running "$n" && echo "$n: running" || echo "$n: stopped"; done ;;
  logs)
    tail -n 20 logs/*.log ;;
  test)
    "$PY" -m unittest discover -s tests -v ;;
  *)
    echo "usage: $0 start|stop|restart|status|logs|test"; exit 1 ;;
esac
