#!/usr/bin/env bash
set -Eeuo pipefail

# Resilient wrapper for long face-database rebuilds.  It retries only when the
# underlying rebuild reports an unreadable media path, adds that exact source
# path to the coalescer skip list, and never deletes or moves the source file.
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MAX_RETRIES="${PICORG_FACE_RECOVERY_RETRIES:-3}"
HEALTH_INTERVAL="${PICORG_FACE_HEALTH_INTERVAL:-60}"
SKIP_FILE="${PICORG_FACE_RECOVERY_SKIP_FILE:-$ROOT_DIR/.cache/picorg/rebuild-recovery-skips.txt}"
LOG_FILE="${PICORG_FACE_RECOVERY_LOG:-${TMPDIR:-/tmp}/picorg-face-rebuild-recovery.log}"
REPORT_DIR="${PICORG_FACE_RECOVERY_REPORT_DIR:-$ROOT_DIR/.cache/picorg/rebuild-reports}"
REBUILD_SCRIPT="${PICORG_FACE_REBUILD_SCRIPT:-$ROOT_DIR/rebuild_face_data.sh}"
SKIP_ROOTS="${PICORG_COALESCE_SKIP_ROOTS:-}"

if ! [[ "$MAX_RETRIES" =~ ^[0-9]+$ ]] || ((MAX_RETRIES < 1)); then
    echo "error: PICORG_FACE_RECOVERY_RETRIES must be a positive integer" >&2
    exit 2
fi
if ! [[ "$HEALTH_INTERVAL" =~ ^[0-9]+$ ]] || ((HEALTH_INTERVAL < 1)); then
    echo "error: PICORG_FACE_HEALTH_INTERVAL must be a positive integer" >&2
    exit 2
fi

mkdir -p -- "$(dirname "$SKIP_FILE")" "$REPORT_DIR" "$(dirname "$LOG_FILE")"
touch -- "$SKIP_FILE"

declare -a SKIP_PATHS=()
append_skip() {
    local value="$1"
    [[ -n "$value" ]] || return 0
    local existing
    for existing in "${SKIP_PATHS[@]}"; do
        [[ "$existing" == "$value" ]] && return 0
    done
    SKIP_PATHS+=("$value")
}

if [[ "${PICORG_FACE_RECOVERY_REUSE_SKIPS:-1}" == "1" ]]; then
    while IFS= read -r path; do
        [[ -n "$path" && "$path" != \#* ]] && append_skip "$path"
    done < "$SKIP_FILE"
fi
if [[ -n "${PICORG_FACE_SKIP_PATHS:-}" ]]; then
    IFS=: read -r -a configured_skips <<< "$PICORG_FACE_SKIP_PATHS"
    for path in "${configured_skips[@]}"; do
        append_skip "$path"
    done
fi

joined_skips() {
    local joined="" path
    for path in "${SKIP_PATHS[@]}"; do
        [[ -n "$joined" ]] && joined+=:
        joined+="$path"
    done
    printf '%s' "$joined"
}

latest_progress() {
    # Read only bounded, non-sensitive progress lines from this attempt.  Do
    # not surface unreadable-path warnings (which may contain private paths).
    local line=""
    if [[ -n "${attempt_log:-}" && -f "$attempt_log" ]]; then
        line="$(grep -a -E 'face-rebuild\] folder [0-9]+/[0-9]+|face extraction: [0-9]+/|^\[[0-9]+/[0-9]+\]|^validated face database:|^rebuild report:|^face database has ' "$attempt_log" | tail -n 1 || true)"
        if [[ "$line" =~ \[face-rebuild\]\ folder\ [0-9]+/[0-9]+:\ [^|]+ ]]; then
            line="${BASH_REMATCH[0]}"
        fi
    fi
    line="${line//$'\r'/ }"
    printf '%s' "${line:0:240}"
}

emit_health() {
    # Persist wrapper heartbeats as well as streaming them to screen so the UI
    # can report progress after a detached-session restart.
    printf '%s\n' "$1" | tee -a "$LOG_FILE" "$attempt_log"
}

extract_bad_path() {
    # Inspect only the current attempt. The durable wrapper log contains old
    # failures; searching it can re-add a stale path and abort recovery with
    # "repeated the same unreadable path" after an unrelated root timeout.
    .venv/bin/python - "$attempt_log" "$SKIP_FILE" <<'PY'
import re
import sys
from pathlib import Path

lines = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace").splitlines()
skip_file = Path(sys.argv[2])
skipped = {line.strip() for line in skip_file.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()} if skip_file.exists() else set()
seen = set()
patterns = (
    re.compile(r"Input/output error:\s*'([^']+)'"),
    re.compile(r"Could not load image:\s*(.+)$"),
)
for line in reversed(lines):
    for pattern in patterns:
        match = pattern.search(line)
        if not match:
            continue
        candidate = match.group(1).strip()
        if not candidate.startswith("/"):
            continue
        if candidate in skipped or candidate in seen:
            continue
        seen.add(candidate)
        path = Path(candidate)
        # Coalesced references are symlinks; persist the real source path so
        # the next coalescing pass can skip it even with a new temp root.
        try:
            is_link = path.is_symlink()
        except OSError:
            # The source may be on a degraded mount. Keep the exact path so
            # the retry can pass it to the coalescer's skip list instead of
            # aborting while trying to inspect the damaged inode.
            is_link = False
        if is_link:
            try:
                target = Path(path.readlink())
                if not target.is_absolute():
                    target = path.parent / target
                candidate = str(target)
            except OSError:
                pass
        if candidate in skipped:
            continue
        print(candidate)
        raise SystemExit(0)
raise SystemExit(1)
PY
}

extract_timed_out_root() {
    .venv/bin/python - "$attempt_log" <<'PY'
import re
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
match = re.search(r'"timed_out_roots"\s*:\s*\[\s*"([^"]+)"', text)
if match:
    print(match.group(1))
    raise SystemExit(0)
raise SystemExit(1)
PY
}

record_recovery() {
    local path="$1" attempt="$2"
    printf '%s\n' "$path" >> "$SKIP_FILE"
.venv/bin/python - "$ROOT_DIR/.cache/picorg/rebuild-recovery.jsonl" "$path" "$attempt" "$LOG_FILE" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

output = Path(sys.argv[1])
path = sys.argv[2]
attempt = int(sys.argv[3])
log = sys.argv[4]
output.parent.mkdir(parents=True, exist_ok=True)
entry = {
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "source_path": str(path),
    "attempt": attempt,
    "log": log,
    "action": "skip_for_rebuild_and_retry",
}
with output.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(entry, sort_keys=True) + "\n")
PY
}

for ((attempt=1; attempt<=MAX_RETRIES; attempt++)); do
    report="$REPORT_DIR/rebuild-$(date -u +%Y%m%dT%H%M%SZ)-attempt${attempt}.json"
    attempt_log="$REPORT_DIR/rebuild-$(date -u +%Y%m%dT%H%M%SZ)-attempt${attempt}.log"
    : > "$attempt_log"
    echo "[recovery] attempt $attempt/$MAX_RETRIES; skips=${#SKIP_PATHS[@]}"
    echo "[recovery] report=$report"
    echo "[recovery] attempt_log=$attempt_log"
    skip_env="$(joined_skips)"
    env_args=("REBUILD_REPORT=$report")
    [[ -n "$skip_env" ]] && env_args+=("PICORG_FACE_SKIP_PATHS=$skip_env")
    [[ -n "$SKIP_ROOTS" ]] && env_args+=("PICORG_COALESCE_SKIP_ROOTS=$SKIP_ROOTS")

    set +e
    # Process substitution keeps the rebuild PID (and its exit status) rather
    # than returning tee's status, while still streaming output to screen and
    # the persistent log.
    env "${env_args[@]}" "$REBUILD_SCRIPT" > >(tee -a "$LOG_FILE" "$attempt_log") 2>&1 &
    child=$!
    while state="$(ps -o stat= -p "$child" 2>/dev/null || true)" &&
          [[ -n "$state" && "$state" != Z* ]]; do
        progress="$(latest_progress)"
        if [[ -n "$progress" ]]; then
            emit_health "[health] rebuild attempt $attempt alive pid=$child log=$(date -u +%H:%M:%S) progress=$progress"
        else
            emit_health "[health] rebuild attempt $attempt alive pid=$child log=$(date -u +%H:%M:%S) progress=waiting for stage output"
        fi
        sleep "$HEALTH_INTERVAL"
    done
    wait "$child"
    status=$?
    set -e

    if ((status == 0)); then
        echo "[recovery] rebuild completed successfully"
        if ((${#SKIP_PATHS[@]})); then
            echo "[recovery] retained skipped paths in $SKIP_FILE"
        fi
        exit 0
    fi

    if ((attempt == MAX_RETRIES)); then
        echo "error: rebuild failed after $MAX_RETRIES attempts; see $LOG_FILE" >&2
        exit "$status"
    fi

    if ! bad_path="$(extract_bad_path)"; then
        if ((status == 75)) && [[ "${PICORG_ALLOW_DEGRADED_REBUILD:-0}" == "1" ]] &&
           timed_out_root="$(extract_timed_out_root)"; then
            case ":$SKIP_ROOTS:" in
                *":$timed_out_root:"*) ;;
                *)
                    SKIP_ROOTS="${SKIP_ROOTS:+$SKIP_ROOTS:}$timed_out_root"
                    echo "[recovery] skipping timed-out source root for degraded retry: $timed_out_root"
                    continue
                    ;;
            esac
        fi
        echo "error: rebuild failed without a recoverable unreadable-path error; see $LOG_FILE" >&2
        exit "$status"
    fi
    before=${#SKIP_PATHS[@]}
    append_skip "$bad_path"
    if ((${#SKIP_PATHS[@]} == before)); then
        echo "error: rebuild repeated the same unreadable path: $bad_path" >&2
        exit "$status"
    fi
    record_recovery "$bad_path" "$attempt"
    echo "[recovery] skipping unreadable source for retry: $bad_path"
done
