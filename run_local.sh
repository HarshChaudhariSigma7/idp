#!/usr/bin/env bash
# Run Sereno Volante on your Mac (or Linux) with one command.
#
#   ./run_local.sh            demo workspace + app at http://localhost:8000
#   ./run_local.sh --eval     real accuracy run on 25 real web documents (needs ANTHROPIC_API_KEY)
#   ./run_local.sh --reset    delete local data (./var) and start fresh
#
# Options: --port N   --no-open   --yes (skip confirmations)
# Needs Python 3.11+ (macOS ships 3.9: `brew install python@3.12`). Everything stays in this folder.
set -euo pipefail
cd "$(dirname "$0")"

PORT=8000
OPEN=1
MODE=app
YES=0
while [ $# -gt 0 ]; do
  case "$1" in
    --eval) MODE=eval ;;
    --reset) MODE=reset ;;
    --port) PORT="$2"; shift ;;
    --no-open) OPEN=0 ;;
    --yes|-y) YES=1 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1 (try --help)"; exit 2 ;;
  esac
  shift
done

say() { printf '\033[1m%s\033[0m\n' "$*"; }
confirm() {  # confirm "question"  -> returns 0 on yes
  [ "$YES" -eq 1 ] && return 0
  printf '%s [y/N] ' "$1"
  read -r answer
  case "$answer" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

# --- 1. Python 3.11+ -------------------------------------------------------------------------
PY=""
for c in python3.13 python3.12 python3.11 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    PY="$c"
    break
  fi
done
if [ -z "$PY" ]; then
  echo "Python 3.11 or newer is required (macOS ships 3.9)."
  echo "Install it with Homebrew:   brew install python@3.12      (Homebrew itself: https://brew.sh)"
  exit 1
fi

if [ "$MODE" = reset ]; then
  confirm "Delete all local data in ./var (documents, users, demo workspace)?" || exit 1
  rm -rf var
  say "Local data deleted."
  MODE=app
fi

# --- 2. Virtual environment + dependencies (reinstalled only when pyproject.toml changes) ------
if [ ! -x .venv/bin/python ] || ! .venv/bin/python -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
  say "Creating a virtual environment with $("$PY" --version)..."
  rm -rf .venv
  "$PY" -m venv .venv
fi
STAMP=".venv/.deps-$(cksum < pyproject.toml | cut -d' ' -f1)"
if [ ! -f "$STAMP" ]; then
  say "Installing dependencies (first run only, 1-3 minutes)..."
  .venv/bin/python -m pip install -q --upgrade pip
  .venv/bin/python -m pip install -q -e ".[dev]"
  rm -f .venv/.deps-*
  touch "$STAMP"
fi

# --- 3. Local settings -------------------------------------------------------------------------
if [ ! -f .env ]; then
  cat > .env <<'ENV'
# Local settings for run_local.sh. Never commit this file (it is git-ignored).
# To read your own documents, remove the # and paste your key:
# ANTHROPIC_API_KEY=sk-ant-...
# Gemini is also supported (sereno/extraction/gemini_llm.py): set SERENO_LLM_BACKEND=gemini
# and GEMINI_API_KEY=... instead.
SERENO_ENV=dev
# http://localhost has no TLS; Safari rejects "secure" cookies without it
SERENO_COOKIE_SECURE=false
SERENO_DATABASE_URL=sqlite:///./var/sereno.db
SERENO_DATA_DIR=./var/data
ENV
  chmod 600 .env
fi
set -a
# shellcheck disable=SC1091
. ./.env
set +a
mkdir -p var

HAS_KEY=0
if [ -n "${ANTHROPIC_API_KEY:-}" ] || [ -n "${ANTHROPIC_AUTH_TOKEN:-}" ] || [ -d "$HOME/.config/anthropic" ] \
   || [ -n "${GEMINI_API_KEY:-}" ] || [ -n "${GOOGLE_API_KEY:-}" ]; then HAS_KEY=1; fi

# --- 4a. Real accuracy run ---------------------------------------------------------------------
if [ "$MODE" = eval ]; then
  if [ "$HAS_KEY" -eq 0 ]; then
    echo "No Anthropic API key found. Add it to .env (see the commented line) or run: export ANTHROPIC_API_KEY=sk-ant-..."
    exit 1
  fi
  confirm "This sends 25 real documents to the Claude API (roughly \$10-15 of usage, about 10-20 minutes). Continue?" || exit 1
  PYTHON=.venv/bin/python bash datasets/web_v1/fetch.sh
  .venv/bin/python -m sereno.eval.offline_checks eval_data/web/labelled --out reports/web_v1/offline_checks.json > /dev/null
  .venv/bin/python -m sereno.eval.run_eval eval_data/web/labelled --out reports/web_v1
  say "Done. Per-bucket report (accuracy, escape rate, traps): reports/web_v1/eval_*.md"
  exit 0
fi

# --- 4b. Demo workspace + app --------------------------------------------------------------------
say "Preparing the local demo workspace..."
.venv/bin/python scripts/manage.py demo-setup

# first free port from $PORT upward (a plain -c script: macOS bash 3.2 mis-parses heredocs inside $(...))
PORT=$(.venv/bin/python -c '
import socket, sys
start = int(sys.argv[1])
for port in range(start, start + 20):
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # same as uvicorn: a just-stopped server does not block
    try:
        s.bind(("127.0.0.1", port))
        print(port)
        break
    except OSError:
        pass
    finally:
        s.close()
' "$PORT")
URL="http://localhost:$PORT"

if [ -n "${ANTHROPIC_API_KEY:-}" ] || [ -n "${ANTHROPIC_AUTH_TOKEN:-}" ] || [ -d "$HOME/.config/anthropic" ]; then
  say "Anthropic key found: documents you upload will be read by Claude."
elif [ -n "${GEMINI_API_KEY:-}" ] || [ -n "${GOOGLE_API_KEY:-}" ]; then
  say "Gemini key found: documents you upload will be read by Gemini (SERENO_LLM_BACKEND=gemini)."
else
  say "No Anthropic key: demo mode (sample documents only). Add ANTHROPIC_API_KEY to .env to read your own."
fi

if [ "$OPEN" -eq 1 ]; then
  (
    sleep 2
    if [ "$(uname)" = "Darwin" ]; then
      [ -f var/.qr_opened ] || { open var/demo_mfa_qr.png && touch var/.qr_opened; }
      open "$URL"
    elif command -v xdg-open >/dev/null 2>&1; then
      xdg-open "$URL" >/dev/null 2>&1 || true
    fi
  ) &
fi

say "Sereno Volante is running at $URL   (press Ctrl+C to stop)"
exec .venv/bin/uvicorn sereno.api.app:app --host 127.0.0.1 --port "$PORT" --log-level warning
