#!/usr/bin/env bash
# hoagie.radar launcher — starts the local server (if not running) and opens the browser.
# Optional: set GOOGLE_PLACES_KEY / YELP_API_KEY for live inline results, e.g.:
#   export GOOGLE_PLACES_KEY=xxx
#   export YELP_API_KEY=yyy
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
PORT=5117
URL="http://127.0.0.1:$PORT"
LOG=/tmp/hoagie-radar.log

# Resolve a Python that has Flask. Prefer the Hermes python; fall back to system.
PY=""
for cand in /home/gramps/.hermes/tools/python-3.14*/bin/python3 \
            /home/gramps/.hermes/installs/*/venv/bin/python3; do
  [ -x "$cand" ] && "$cand" -c "import flask" >/dev/null 2>&1 && { PY="$cand"; break; }
done
[ -z "$PY" ] && for cand in python3.14 python3; do
  command -v "$cand" >/dev/null 2>&1 && "$cand" -c "import flask" >/dev/null 2>&1 && { PY="$cand"; break; }
done
if [ -z "$PY" ]; then
  (zenity --error --text="hoagie.radar: no Python with Flask found." 2>/dev/null \
    || echo "hoagie.radar: no Python with Flask found.")
  exit 1
fi

# already up? just open the browser
if curl -sf -o /dev/null "$URL/api/health" 2>/dev/null; then
  (xdg-open "$URL" >/dev/null 2>&1 || true)
  exit 0
fi

# start the server detached from this launcher's session
nohup "$PY" "$DIR/main.py" --port "$PORT" >>"$LOG" 2>&1 &
disown 2>/dev/null || true

# wait for readiness (up to ~10s), then open the browser
for i in $(seq 1 40); do
  if curl -sf -o /dev/null "$URL/api/health" 2>/dev/null; then
    (xdg-open "$URL" >/dev/null 2>&1 || true)
    exit 0
  fi
  sleep 0.25
done

# never came up — surface it
(zenity --error --text="hoagie.radar failed to start. See $LOG" 2>/dev/null \
  || echo "hoagie.radar failed to start. See $LOG")
exit 1
