#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

ENV_FILE="${LIVIUM_ENV_FILE:-$ROOT_DIR/.env}"
PYTHON="${ROOT_DIR}/.venv/bin/python"
ACTION="${1:-help}"
if (($#)); then shift; fi

make_env_file() {
  if [[ -e "$ENV_FILE" ]]; then
    return 0
  fi
  mkdir -p -- "$(dirname -- "$ENV_FILE")"
  cp -- "$ROOT_DIR/.env.example" "$ENV_FILE"
  chmod 600 "$ENV_FILE" 2>/dev/null || true
  printf 'Created %s from .env.example. Add your API key, then run this command again.\n' \
    "$ENV_FILE"
  return 0
}

case "$ACTION" in
  setup)
    command -v python3 >/dev/null || {
      printf 'python3 is required. Install python3 and python3-venv in Kali.\n' >&2
      exit 2
    }
    python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' || {
      printf 'Livium requires Python 3.11 or newer.\n' >&2
      exit 2
    }
    make_env_file
    python3 -m venv "$ROOT_DIR/.venv"
    chmod 600 "$ENV_FILE" 2>/dev/null || true
    printf 'Ready. Edit %s, then run: bash scripts/dev-wsl.sh doctor\n' "$ENV_FILE"
    exit 0
    ;;
  help|-h|--help)
    cat <<'HELP'
Livium Kali/WSL development helper

  bash scripts/dev-wsl.sh setup       Create .venv and a private env template if absent
  bash scripts/dev-wsl.sh doctor      Probe the configured provider and runtime
  bash scripts/dev-wsl.sh test        Run the offline Python suite
  bash scripts/dev-wsl.sh ui-setup   Install optional Studio dependencies with npm ci
  bash scripts/dev-wsl.sh ui-test     Run Studio build and tests (requires Node/npm)
  bash scripts/dev-wsl.sh smoke       Run demo A against the real provider, then labelled A2
  bash scripts/dev-wsl.sh <command>   Pass a Livium CLI command, e.g. run, status, serve

Set LIVIUM_ENV_FILE to use a different env file, preferably inside Kali's home.
HELP
    exit 0
    ;;
esac

if [[ "$ACTION" != "test" && "$ACTION" != "ui-setup" && "$ACTION" != "ui-test" ]]; then
  if [[ ! -e "$ENV_FILE" ]]; then
    make_env_file
    exit 2
  fi
  # The operator-authored env file is shell assignments from .env.example.
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

if [[ "$ACTION" != "ui-setup" && ! -x "$PYTHON" ]]; then
  printf 'Virtual environment missing. Run bash scripts/dev-wsl.sh setup first.\n' >&2
  exit 2
fi

case "$ACTION" in
  test)
    exec "$PYTHON" -m unittest discover -s tests -t tests
    ;;
  ui-test)
    command -v npm >/dev/null || {
      printf 'npm is required for the optional Studio test suite.\n' >&2
      exit 2
    }
    cd "$ROOT_DIR/ui"
    exec npm test
    ;;
  ui-setup)
    command -v npm >/dev/null || {
      printf 'npm is required for the optional Studio dependencies.\n' >&2
      exit 2
    }
    cd "$ROOT_DIR/ui"
    exec npm ci
    ;;
  smoke)
    exec "$PYTHON" demos/run_demonstrations.py --only A \
      --home "$ROOT_DIR/.livium/live-smoke"
    ;;
  *)
    exec "$PYTHON" -m livium "$ACTION" "$@"
    ;;
esac
