#!/usr/bin/env bash
# Разворачивает ВЫХОД (зарубежный сервер, принимает трафик от входов).
#   curl -fsSL https://raw.githubusercontent.com/knyazevi81/strangling-hand-deploy/main/deploy-exit.sh | sudo bash
# То же самое, что install.sh, но проверяет, что ключ выдан именно для выхода.
set -euo pipefail
REPO_RAW="${SAVEBIT_REPO_RAW:-https://raw.githubusercontent.com/knyazevi81/strangling-hand-deploy/main}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [ -n "$DIR" ] && [ -f "$DIR/install.sh" ]; then
  exec bash "$DIR/install.sh" --role exit "$@"
fi
exec bash <(curl -fsSL "$REPO_RAW/install.sh") --role exit "$@"
