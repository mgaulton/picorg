#!/usr/bin/env bash
set -euo pipefail

# Keep unrelated globally installed pytest plugins from changing this repo's
# test environment (for example, a broken system pytest_httpbin plugin).
export PYTEST_DISABLE_PLUGIN_AUTOLOAD="${PYTEST_DISABLE_PLUGIN_AUTOLOAD:-1}"
if [[ -x .venv/bin/pytest ]]; then
  exec .venv/bin/pytest "$@"
elif command -v pytest >/dev/null 2>&1; then
  exec pytest "$@"
else
  exec python3 -m pytest "$@"
fi
