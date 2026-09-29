#!/usr/bin/env bash
# Resolve a free TCP bind port for the PicOrg review UI.
# Usage:
#   PORT="$(./picorg_resolve_port.sh "${PORT:-8787}")"
#   # or: source this file and call picorg_resolve_port 8787
set -Eeuo pipefail

picorg_resolve_port() {
    local preferred="${1:-8787}"
    local max_tries="${2:-50}"
    local root_dir python_bin
    root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    if [[ -x "$root_dir/.venv/bin/python" ]]; then
        python_bin="$root_dir/.venv/bin/python"
    else
        python_bin="python3"
    fi
    "$python_bin" - "$preferred" "$max_tries" <<'PY'
import socket
import sys

start = int(sys.argv[1])
limit = int(sys.argv[2])
if start < 1 or start > 65535:
    raise SystemExit(f"error: invalid preferred port: {start}")
if limit < 1:
    raise SystemExit("error: max_tries must be >= 1")

end = min(start + limit, 65536)
for port in range(start, end):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", port))
        except OSError:
            continue
        print(port)
        raise SystemExit(0)
raise SystemExit(f"error: no free TCP port in [{start}, {end})")
PY
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    picorg_resolve_port "${1:-${PORT:-8787}}" "${2:-50}"
fi
