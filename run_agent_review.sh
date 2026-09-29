#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"
AUDIT="${AGENT_AUDIT:-$(find .cache/picorg/audits -maxdepth 1 -type f -name '*.face-clusters.json' -printf '%T@ %p\n' 2>/dev/null | sort -nr | sed -n '1s/^[^ ]* //p')}"
if [[ -z "$AUDIT" || ! -s "$AUDIT" ]]; then
    echo "ERROR: no face-cluster audit found; run a face review first or set AGENT_AUDIT=/path/audit.json" >&2
    exit 2
fi
OUT="${AGENT_REVIEW_OUTPUT:-.cache/picorg/agent-review.json}"
cmd=("$ROOT_DIR/.venv/bin/python" "$ROOT_DIR/agent_review.py" --audit "$AUDIT" --output "$OUT" --provider "${AGENT_PROVIDER:-mock}" --model "${AGENT_MODEL:-${OLLAMA_MODEL:-gemma3:4b}}" --limit "${AGENT_LIMIT:-50}" --timeout "${AGENT_TIMEOUT:-30}")
if [[ -n "${AGENT_ENDPOINT:-}" ]]; then
    cmd+=(--endpoint "$AGENT_ENDPOINT")
fi
if [[ "${AGENT_ALLOW_REMOTE:-0}" == "1" ]]; then
    cmd+=(--allow-remote)
fi
"${cmd[@]}"
echo "agent quality report: $OUT"
