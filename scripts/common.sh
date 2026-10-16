# Sourced by the other scripts. Usage: script.sh [ENV_FILE]   (default: <repo>/.env)
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$(realpath -m "${1:-${BRIDGE_ENV_FILE:-$ROOT/.env}}")"
[ -f "$ENV_FILE" ] || { echo "config not found: $ENV_FILE (copy .env.example)" >&2; exit 2; }
set -a; . "$ENV_FILE"; set +a
export PATH="$HOME/.local/bin:$PATH"
LARK="${LARK_CLI:-lark-cli}"
WORK="${BRIDGE_WORK_DIR:-run}"
case "$WORK" in /*) ;; *) WORK="$ROOT/$WORK";; esac
mkdir -p "$WORK"
PY="${PYTHON:-python3}"
