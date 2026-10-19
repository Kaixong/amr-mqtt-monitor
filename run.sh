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
  if ! is_running "$1"; then echo "$1 not running"; rm -f "logs/$1.pid"; return; fi
  local pid; pid=$(cat "logs/$1.pid")
  kill "$pid"
  # wait for a clean exit: otherwise a quick restart races the old process (e.g. its final
  # retained "offline" could land after the new process's "online")
  for _ in $(seq 1 50); do kill -0 "$pid" 2>/dev/null || break; sleep 0.2; done
  kill -0 "$pid" 2>/dev/null && { kill -9 "$pid"; echo "$1 killed (did not exit in 10 s)"; } || echo "$1 stopped"
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
  # Don't return right away: when run as `wsl -- ./run.sh start`, wsl.exe exits with us and
  # WSL tears the session down, killing a just-forked service before it has detached (setsid).
  sleep 2
  local failed=0
  for n in "${SERVICES[@]}"; do
    is_running "$n" || { echo "$n FAILED to start, see logs/$n.log"; failed=1; }
  done
  echo "dashboard: http://localhost:8080   alarm log: http://localhost:8080/alarms.html"
  return $failed
}

# alarmd first, so a planned stop isn't recorded as every AMR going offline
stop_all() { for n in alarmd simulator sysmon; do stop_one "$n"; done; }

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
