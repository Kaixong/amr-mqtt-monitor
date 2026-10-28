# CLAUDE.md

AMR fleet simulator + Windows PC monitor + alarm service, all over MQTT 5, with a browser
dashboard. Runs in WSL2 Ubuntu 24.04 on a Windows 11 laptop. **README.md is the full
reference** (architecture, topics, alarm rules, security, verified steps) — read it before
larger changes and keep it in sync with what you change.

The user writes in Traditional Chinese (zh-TW); reply in Traditional Chinese.

## Commands (run in the project root)

```bash
./run.sh start | stop | restart | status | logs
./run.sh test     # 58 unit tests (pytest, requirements-dev.txt), no services needed
./run.sh itest    # 16 integration tests against the running broker (start first)
.venv/bin/flake8  # lint (max line 110, .flake8)
```

CI: `.github/workflows/ci.yml` runs flake8 + `./run.sh test`, then `./run.sh start` + `itest` on a
fresh Ubuntu runner (no Windows there: sysmon only logs that it can't sample). Keep `run.sh start`
working from a fresh clone with no `.env` / certs.

- Dashboard http://localhost:8080 , alarm log http://localhost:8080/alarms.html
- After changing Python services: `./run.sh restart`. Dashboard files are served live by nginx.
- Verify changes by actually running them (start → test/itest → check logs), not just by reading.

## Layout

`simulator/` (5 AMRs) · `monitor/sysmon.py` (Windows stats) · `alarm/` (rules.py = pure logic,
store.py = SQLite, alarmd.py = MQTT) · `recorder/` (tracker.py = pure state-interval +
utilization logic, statedb.py = SQLite `data/telemetry.db`, recorder.py = MQTT, publishes retained
`stats/utilization` every 10 s) · `common/mqttutil.py` (shared MQTT 5 + TLS client) ·
`dashboard/` (static HTML/JS, MQTT.js) · `mosquitto/` (conf + acl) · `scripts/` · `tests/`

## Broker / MQTT facts that are easy to get wrong

- **No plain 1883.** Backend + CLI use TLS on **8883** with `--cafile certs/ca.crt`; browser uses
  ws **9001**; wss **9443** is optional. All ports are published on **127.0.0.1 only**.
- Anonymous is off. Users: `simulator` `sysmon` `alarmd` `recorder` `dashboard`; passwords in `.env`
  (`MQTT_PASS_<USER>`), rights in `mosquitto/acl` (least privilege — a new topic usually needs
  an ACL line, then `./scripts/setup-auth.sh --acl-only`).
- CLI helper used throughout the README:
  ```bash
  set -a; . ./.env; set +a
  mq() { local c=$1; shift; "mosquitto_$c" -h localhost -p 8883 --cafile certs/ca.crt -u dashboard -P "$MQTT_PASS_DASHBOARD" "$@"; }
  mq sub -t 'amr/+/status' -C 1 -v
  ```
- MQTT 5 conventions: JSON payloads carry Content Type + Payload Format Indicator
  (`json_props()` in mqttutil); retained telemetry has a 24 h Message Expiry; alarm state is
  retained **without** expiry; commands carry Message Expiry 5 s + Response Topic + Correlation
  Data and AMRs only reply on their own `amr/<id>/cmd/result`.
- Retained messages can be old: judge freshness by the payload `ts`, never by arrival alone.
  A retained `amr/<id>/online = offline` can even arrive *after* a restarted AMR's first status,
  so the recorder ignores retained offline (silence > 3 s is detected separately).
- New MQTT user: add it to `USERS` + the `docker run -e Un/Pn` list in `scripts/setup-auth.sh`,
  to `mosquitto/acl`, and to the `.env` check in `run.sh start_all` (so upgrades create it).
- Command rule (tested in `tests/test_amr.py`): `go` / `charge` start new work and are accepted
  **only while idle** — never preempt a task, a charge or a paused task. `pause` / `resume` /
  `clear` only change how the current work runs.
- `amr/<id>/cmd` `simulate_netloss` is a test hook: that AMR goes silent until `./run.sh restart`.

## Gotchas learned the hard way

- `run.sh start` sleeps 2 s and checks every service before returning: when called as
  `wsl -- ./run.sh start`, WSL kills a just-forked service if the script returns immediately.
- `run.sh stop` waits for each process to exit and stops **alarmd + recorder first** (otherwise a planned
  stop is logged as every AMR going offline, and a fast restart races the old "offline" message).
- sysmon must measure Windows, not WSL: it launches a **detached hidden PowerShell** sampler
  (a Windows exe started directly via interop dies with the wsl.exe session) and talks to it
  through `logs/sysmon/stats.json` + `heartbeat` (content-based, so clock drift doesn't matter).
- From Windows, `localhost` resolves to `::1` first but the ports are IPv4-only: non-browser
  Windows tools must use `127.0.0.1` (browsers fall back on their own).
- Any Windows access to `\\wsl.localhost\...` boots WSL. Prefer working from inside WSL.
- `mosquitto/secrets/` is owned by uid 1883 (the broker) and unreadable for `kuma` — expected.
  `run.sh` uses the marker files `mosquitto/.auth-ready` / `.tls-ready` instead.
- Files edited from Windows lose the exec bit: `chmod +x run.sh scripts/*.sh`.

## Secrets — never commit or print

`.env`, `dashboard/config.js`, `certs/`, `mosquitto/secrets/`, `mosquitto/data/`, `data/`,
`logs/` are git-ignored. Before committing, check `git diff --cached --name-only` for them and
don't echo password values into chat or logs.

## Git

Local repo, no remote. Author is set repo-locally (`kuma`). End commit messages with:

```
Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
```
