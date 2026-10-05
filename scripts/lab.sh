#!/usr/bin/env bash
# lab.sh: check the machine, set up, run and tear down the Agentic Incident Triage lab.
#
#   scripts/lab.sh check                 read-only: CPU, RAM, disk, ports, tools, internet, key
#   scripts/lab.sh setup                 install what can be installed WITHOUT root; print the rest
#   scripts/lab.sh engine                start Camunda 8 (c8run) and wait until it serves
#   scripts/lab.sh deploy                deploy the BPMN model to the engine
#   scripts/lab.sh worker                start the job worker in the background
#   scripts/lab.sh ui                    start the live viewer (foreground) on http://127.0.0.1:8099
#   scripts/lab.sh all                   check, setup, engine, deploy, worker, then the viewer
#   scripts/lab.sh demo                  no engine, no key: the recorded replay on http://127.0.0.1:8000/
#   scripts/lab.sh status                what is running right now
#   scripts/lab.sh teardown [flags]      dry run by default. Flags:
#                                          --apply          really do it
#                                          --purge-data     also wipe the engine's runtime data
#                                          --remove-profile also remove the c8ctl profile this lab added
#                                          --remove-env     also delete .env (your key lives there)
#                                          --remove-engine  also delete the cached engine download
#
# Never runs sudo. If a step needs root it prints the exact command for you to run.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN="$ROOT/.run"
cd "$ROOT"

# ---- settings (environment first, then .env, then defaults) ---------------
envget() { # envget NAME [default]
  local v="${!1:-}"
  if [ -z "$v" ] && [ -f "$ROOT/.env" ]; then
    v="$(grep -E "^$1=" "$ROOT/.env" | tail -1 | cut -d= -f2- | sed -e "s/^['\"]//" -e "s/['\"]$//")"
  fi
  echo "${v:-${2:-}}"
}
C8_VERSION="$(envget C8_VERSION 8.10.0-alpha5)"   # the version this lab was tested on
C8_PORT="$(envget C8_PORT 8080)"
UI_PORT="$(envget UI_PORT 8099)"
PROFILE="$(envget C8CTL_PROFILE agentic-lab)"
ENGINE_DIR="$HOME/.cache/c8run/c8run-$C8_VERSION/c8run-$C8_VERSION"

# Minimums, from a measured run: the engine idles near 0.8 GB resident and the
# download is about 1.1 GB. We ask for headroom on top of that.
MIN_CPUS=2
MIN_RAM_MB=3072      # available, not total
MIN_DISK_MB=4096     # free where the engine cache lives
NODE_MIN=20
PY_MIN="3.9"
JAVA_MIN=21

FAILS=0; WARNS=0
pass() { printf '  \033[32mPASS\033[0m  %s\n' "$*"; }
warn() { printf '  \033[33mWARN\033[0m  %s\n' "$*"; WARNS=$((WARNS+1)); }
fail() { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; FAILS=$((FAILS+1)); }
say()  { printf '\n== %s\n' "$*"; }
port_busy() { ss -tln 2>/dev/null | awk '{print $4}' | grep -Eq "[:.]$1\$" || (command -v lsof >/dev/null && lsof -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1); }
vge() { [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -1)" = "$2" ]; }  # vge have need

# ---- check -----------------------------------------------------------------
cmd_check() {
  FAILS=0; WARNS=0
  say "Machine ($(uname -sm))"
  local cpus ram_mb disk_mb
  if [ -r /proc/meminfo ]; then
    cpus="$(nproc)"; ram_mb="$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo)"
  else  # macOS, best effort and untested
    cpus="$(sysctl -n hw.ncpu 2>/dev/null || echo 0)"
    ram_mb="$(( $(sysctl -n hw.memsize 2>/dev/null || echo 0) / 1048576 ))"
  fi
  mkdir -p "$HOME/.cache"
  disk_mb="$(df -Pm "$HOME/.cache" | awk 'NR==2 {print $4}')"
  [ "$cpus" -ge "$MIN_CPUS" ] && pass "CPU cores: $cpus (need $MIN_CPUS)" || fail "CPU cores: $cpus (need $MIN_CPUS)"
  [ "$ram_mb" -ge "$MIN_RAM_MB" ] && pass "RAM available: ${ram_mb} MB (need $MIN_RAM_MB)" \
    || fail "RAM available: ${ram_mb} MB (need $MIN_RAM_MB). Close something heavy, or stop any local model server first."
  [ "$disk_mb" -ge "$MIN_DISK_MB" ] && pass "Disk free for the engine cache: ${disk_mb} MB (need $MIN_DISK_MB)" \
    || fail "Disk free for the engine cache: ${disk_mb} MB (need $MIN_DISK_MB)"
  if [ -r /proc/meminfo ]; then
    local swap_used; swap_used="$(awk '/SwapTotal/ {t=$2} /SwapFree/ {f=$2} END {print int((t-f)/1024)}' /proc/meminfo)"
    [ "$swap_used" -lt 2048 ] && pass "Swap in use: ${swap_used} MB" || warn "Swap in use: ${swap_used} MB. The machine is already swapping; the engine will be slow."
  fi

  say "Tools"
  if command -v node >/dev/null; then
    local nv; nv="$(node -p 'process.versions.node')"
    vge "$nv" "$NODE_MIN" && pass "node $nv (need >= $NODE_MIN)" || fail "node $nv (need >= $NODE_MIN)"
  else fail "node not found (needed for c8ctl). Install Node $NODE_MIN+ from https://nodejs.org or with nvm."; fi
  command -v npm >/dev/null && pass "npm $(npm --version)" || fail "npm not found"
  if command -v python3 >/dev/null; then
    local pv; pv="$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
    vge "$pv" "$PY_MIN" && pass "python3 $pv (standard library only; no pip installs)" || fail "python3 $pv (need >= $PY_MIN)"
  else fail "python3 not found"; fi
  if command -v java >/dev/null; then
    local jv; jv="$(java -version 2>&1 | awk -F'"' '/version/ {print $2}' | cut -d. -f1)"
    [ "${jv:-0}" -ge "$JAVA_MIN" ] 2>/dev/null && pass "java $jv (need >= $JAVA_MIN)" || fail "java ${jv:-?} (need >= $JAVA_MIN)"
  else fail "java not found (needed by the engine). Debian/Ubuntu: sudo apt install openjdk-21-jre-headless  (run it yourself; this script never uses sudo)"; fi
  command -v c8ctl >/dev/null && pass "c8ctl $(c8ctl --version 2>/dev/null | head -1)" || warn "c8ctl not installed yet (scripts/lab.sh setup installs it)"
  command -v curl >/dev/null && pass "curl present" || fail "curl not found"

  say "Ports (this lab uses $C8_PORT engine REST, $UI_PORT viewer)"
  for p in "$C8_PORT" 26500 9600; do
    if port_busy "$p"; then
      if engine_up; then pass "port $p busy: a Camunda engine is already answering on $C8_PORT, so the lab will reuse it"
      else fail "port $p is in use by something that is not a Camunda engine. Free it, or set C8_PORT=<free port> in .env (26500 and 9600 are fixed engine ports)"; fi
    else pass "port $p free"; fi
  done
  port_busy "$UI_PORT" && warn "port $UI_PORT in use (set UI_PORT in .env)" || pass "port $UI_PORT free"

  say "Network"
  curl -fsS -m 8 -o /dev/null -I https://api.typesafe.ai 2>/dev/null && pass "TypeSafe API reachable" \
    || { curl -sS -m 8 -o /dev/null -I https://api.typesafe.ai 2>&1 | grep -qi "resolve\|connect\|timed" \
         && fail "TypeSafe API not reachable (live runs need it; the replay demo does not)" || pass "TypeSafe API reachable"; }
  [ -d "$HOME/.cache/c8run/c8run-$C8_VERSION" ] && pass "engine $C8_VERSION already downloaded" || {
    curl -fsS -m 8 -o /dev/null -I https://github.com 2>/dev/null && pass "internet OK for the engine download (about 1.1 GB)" || warn "no internet: cannot download the engine"; }

  say "Your TypeSafe key"
  if [ -n "$(envget TYPESAFE_API_KEY)" ]; then pass "TYPESAFE_API_KEY is set (value not shown)"
  else warn "TYPESAFE_API_KEY is not set. Copy .env.example to .env and add it. See README, 'Give the lab your TypeSafe key'."; fi

  echo
  if [ "$FAILS" -gt 0 ]; then echo "Result: FAIL ($FAILS failed, $WARNS warnings). Fix the FAIL lines before 'setup' or 'all'."; return 1; fi
  echo "Result: OK ($WARNS warnings)."; return 0
}

# ---- setup -----------------------------------------------------------------
cmd_setup() {
  cmd_check || { echo "Not setting up on a machine that failed the check."; return 1; }
  say "Setup (no root, nothing outside your home directory or this repo)"
  if ! command -v c8ctl >/dev/null; then
    local prefix; prefix="$(npm config get prefix 2>/dev/null)"
    if [ -w "$prefix" ] || [ -w "$prefix/lib" ]; then
      echo "Installing c8ctl (npm global, into $prefix)..."; npm install -g @camunda8/cli || { fail "npm install failed"; return 1; }
    else
      echo "npm's global prefix ($prefix) is not writable by you, and this script never uses sudo."
      echo "Run ONE of these yourself, then re-run setup:"
      echo "  npm install -g --prefix \"\$HOME/.local\" @camunda8/cli   # then make sure \$HOME/.local/bin is on PATH"
      return 1
    fi
  else pass "c8ctl already installed"; fi
  if [ ! -d "$HOME/.cache/c8run/c8run-$C8_VERSION" ]; then
    echo "Downloading Camunda 8 $C8_VERSION (about 1.1 GB, into ~/.cache/c8run)..."
    c8ctl cluster install "$C8_VERSION" || { fail "engine download failed"; return 1; }
  else pass "engine $C8_VERSION already downloaded"; fi
  if [ ! -f "$ROOT/.env" ]; then
    cp "$ROOT/.env.example" "$ROOT/.env"; chmod 600 "$ROOT/.env"
    echo "Created .env from .env.example (mode 600, gitignored). Add your TYPESAFE_API_KEY to it."
  else pass ".env already exists (left untouched)"; fi
  local url; url="$(profile_url)"
  if [ -z "$url" ]; then
    if c8ctl add profile "$PROFILE" --baseUrl "http://localhost:$C8_PORT" >/dev/null 2>&1; then
      mkdir -p "$RUN"; echo "$PROFILE" > "$RUN/profile.added"
      echo "Added c8ctl profile '$PROFILE' -> http://localhost:$C8_PORT (your active profile is not changed)."
    else fail "could not add c8ctl profile '$PROFILE'"; return 1; fi
  elif [ "${url%/v2}" != "http://localhost:$C8_PORT" ]; then
    warn "c8ctl profile '$PROFILE' already exists and points at $url, not http://localhost:$C8_PORT. Leaving it alone. Set C8CTL_PROFILE=<a new name> in .env, then run setup again."
  else pass "c8ctl profile '$PROFILE' exists and matches port $C8_PORT"; fi
  echo; echo "Setup done. Next: add your key to .env, then scripts/lab.sh all"
}

# ---- engine, deploy, worker, ui -----------------------------------------------
engine_up() { curl -s -m 3 -o /dev/null -w '%{http_code}' "http://localhost:$C8_PORT/v2/topology" | grep -qE '^(200|401)$'; }

cmd_engine() {
  if engine_up; then pass "engine already answering on port $C8_PORT"; return 0; fi
  if port_busy "$C8_PORT"; then fail "port $C8_PORT is in use by something that is not a Camunda engine. Set C8_PORT in .env."; return 1; fi
  [ -x "$ENGINE_DIR/c8run" ] || { fail "engine not downloaded. Run scripts/lab.sh setup"; return 1; }
  mkdir -p "$RUN"
  echo "Starting Camunda 8 $C8_VERSION on port $C8_PORT (first start takes about 30 to 60 seconds)..."
  ( cd "$ENGINE_DIR" && nohup ./c8run start --port "$C8_PORT" --disable-connectors > "$RUN/engine.log" 2>&1 & )
  echo "$C8_PORT" > "$RUN/engine.started"
  for _ in $(seq 1 36); do sleep 5; engine_up && { pass "engine is serving on http://localhost:$C8_PORT"; return 0; }; done
  fail "engine did not answer within 3 minutes. See $RUN/engine.log"; return 1
}

c8() { c8ctl "$@" --profile "$PROFILE"; }

profile_url() { # the base URL of c8ctl profile $PROFILE, or empty if there is no such profile
  c8ctl list profiles --json 2>/dev/null | python3 -c "
import sys, json
try: print(next((p['URL'] for p in json.load(sys.stdin) if p['Name'] == sys.argv[1]), ''))
except Exception: print('')" "$PROFILE"
}
proc_is() { ps -p "$1" -o args= 2>/dev/null | grep -q -- "$2"; }   # is PID $1 really the process we started?

cmd_deploy() { c8 deploy "$ROOT/bpmn/agentic-incident-triage.bpmn"; }

cmd_worker() {
  [ -n "$(envget TYPESAFE_API_KEY)" ] || { fail "TYPESAFE_API_KEY is not set. See README, 'Give the lab your TypeSafe key'."; return 1; }
  mkdir -p "$RUN"
  if [ -f "$RUN/worker.pid" ] && kill -0 "$(cat "$RUN/worker.pid")" 2>/dev/null; then pass "worker already running (pid $(cat "$RUN/worker.pid"))"; return 0; fi
  C8CTL_PROFILE="$PROFILE" nohup python3 -u "$ROOT/workers/agentic_worker.py" > "$RUN/worker.log" 2>&1 &
  echo $! > "$RUN/worker.pid"; sleep 2
  kill -0 "$(cat "$RUN/worker.pid")" 2>/dev/null && pass "worker started (pid $(cat "$RUN/worker.pid"), log .run/worker.log)" || { fail "worker exited. See .run/worker.log"; return 1; }
}

cmd_ui() {
  echo "Live viewer on http://127.0.0.1:$UI_PORT (Ctrl+C to stop)"
  mkdir -p "$RUN"; echo $$ > "$RUN/ui.pid"     # exec keeps this PID, so teardown can stop exactly this process
  CAMUNDA_REST="$(envget CAMUNDA_REST "http://localhost:$C8_PORT/v2")" C8CTL_PROFILE="$PROFILE" exec python3 "$ROOT/ui/server.py" --port "$UI_PORT"
}

cmd_demo() {
  echo "Replay demo (no engine, no key) on http://127.0.0.1:8000/  (Ctrl+C to stop)"
  # Serve ONLY ui/. Serving the repo root would expose .env (your TypeSafe key) and .git.
  exec python3 -m http.server 8000 --bind 127.0.0.1 --directory "$ROOT/ui"
}

cmd_all() { cmd_setup && cmd_engine && cmd_deploy && cmd_worker && cmd_ui; }

cmd_status() {
  say "Status"
  engine_up && pass "engine answering on port $C8_PORT" || echo "  --    engine not answering on port $C8_PORT"
  if [ -f "$RUN/worker.pid" ] && kill -0 "$(cat "$RUN/worker.pid")" 2>/dev/null; then pass "worker running (pid $(cat "$RUN/worker.pid"))"; else echo "  --    worker not running"; fi
  port_busy "$UI_PORT" && pass "something is listening on viewer port $UI_PORT" || echo "  --    viewer not running"
}

# ---- teardown --------------------------------------------------------------
cmd_teardown() {
  local apply=0 purge=0 prof=0 env=0 eng=0
  for a in "$@"; do case "$a" in
    --apply) apply=1;; --purge-data) purge=1;; --remove-profile) prof=1;;
    --remove-env) env=1;; --remove-engine) eng=1;;
    *) echo "unknown flag $a"; return 2;; esac; done
  say "Teardown ($([ $apply = 1 ] && echo APPLY || echo 'dry run: nothing will be changed'))"
  local did=0
  step() { # step "description" command...
    local d="$1"; shift; did=1
    if [ $apply = 1 ]; then echo "  doing:  $d"; "$@" || echo "    (failed, continuing)"; else echo "  would:  $d"; fi
  }
  local wpid upid ours=0 running=0
  wpid="$(cat "$RUN/worker.pid" 2>/dev/null || true)"; upid="$(cat "$RUN/ui.pid" 2>/dev/null || true)"
  [ -f "$RUN/engine.started" ] && ours=1
  engine_up && running=1
  if [ -n "$wpid" ] && proc_is "$wpid" agentic_worker.py; then step "stop the worker (pid $wpid)" kill "$wpid"; fi
  if [ -n "$upid" ] && proc_is "$upid" ui/server.py; then step "stop the live viewer (pid $upid)" kill "$upid"; fi
  if [ $ours = 1 ]; then
    step "stop the engine this lab started (c8run stop in $ENGINE_DIR)" bash -c "cd '$ENGINE_DIR' && ./c8run stop"
  elif [ $running = 1 ]; then echo "  skip:   an engine is running that this lab did not start; leaving it alone"
  fi
  [ -d "$ROOT/workers/postmortems" ] && step "delete generated postmortems: $(ls "$ROOT/workers/postmortems" | wc -l) file(s) in workers/postmortems/" rm -rf "$ROOT/workers/postmortems"
  [ -d "$ROOT/ui/recordings" ] && step "delete generated recordings: ui/recordings/" rm -rf "$ROOT/ui/recordings"
  # Deep clean touches the engine's files, so it needs an engine that is not running, or one this lab is stopping above.
  local deep_ok=0; { [ $ours = 1 ] || [ $running = 0 ]; } && deep_ok=1
  if [ $purge = 1 ]; then
    if [ $deep_ok = 1 ]; then step "wipe engine runtime data (c8ctl cluster purge $C8_VERSION; keeps the binary)" c8ctl cluster purge "$C8_VERSION"
    else echo "  skip:   --purge-data: a running engine that this lab did not start owns that data"; fi
  fi
  if [ $prof = 1 ]; then
    if [ "$(cat "$RUN/profile.added" 2>/dev/null)" = "$PROFILE" ]; then step "remove c8ctl profile '$PROFILE' (this lab added it)" c8ctl remove profile "$PROFILE"
    else echo "  skip:   --remove-profile: this lab did not add c8ctl profile '$PROFILE' (no marker), so it is not ours to remove"; fi
  fi
  [ $env = 1 ] && [ -f "$ROOT/.env" ] && step "delete .env (this removes your saved TypeSafe key from this machine)" rm -f "$ROOT/.env"
  if [ $eng = 1 ]; then
    if [ $deep_ok = 1 ]; then
      echo "  note:   ~/.cache/c8run may be shared with other projects ($(du -sh "$HOME/.cache/c8run" 2>/dev/null | cut -f1))"
      step "delete the cached engine download ~/.cache/c8run/c8run-$C8_VERSION" rm -rf "$HOME/.cache/c8run/c8run-$C8_VERSION"
    else echo "  skip:   --remove-engine: a running engine that this lab did not start is using those files"; fi
  fi
  # .run/ goes last: the markers above are read from it.
  [ -d "$RUN" ] && step "delete run state and logs: .run/" bash -c "sleep 1; rm -rf '$RUN'"
  echo
  echo "  never touched: c8ctl itself, Node, Java, Python, your TypeSafe account, your other c8ctl profiles."
  [ $did = 0 ] && echo "  Nothing to tear down."
  [ $apply = 0 ] && [ $did = 1 ] && echo "  Re-run with --apply to do it. Add --purge-data / --remove-profile / --remove-env / --remove-engine for a deeper clean."
  return 0
}

case "${1:-help}" in
  check) cmd_check;; setup) cmd_setup;; engine) cmd_engine;; deploy) cmd_deploy;;
  worker) cmd_worker;; ui) cmd_ui;; demo) cmd_demo;; all) cmd_all;; status) cmd_status;;
  teardown) shift; cmd_teardown "$@";;
  *) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//';;
esac
