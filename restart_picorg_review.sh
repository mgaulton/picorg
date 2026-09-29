#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

SESSION="${PICORG_REVIEW_SCREEN:-picorg-review}"
AUDIT="${AUDIT:-}"
HOST="${PICORG_UI_HOST:-${HOST:-0.0.0.0}}"
PORT="${PORT:-8787}"
LOCK_FILE="${LOCK_FILE:-/tmp/picorg-runweb.lock}"
DECISIONS="${DECISIONS:-$ROOT_DIR/review_decisions.json}"
IMAGE_DECISIONS="${IMAGE_DECISIONS:-$ROOT_DIR/review_image_decisions.json}"
REVIEW_IDENTITIES="${REVIEW_IDENTITIES:-$ROOT_DIR/review_identities.json}"
REVIEW_LEDGER="${REVIEW_LEDGER:-$ROOT_DIR/review_decision_ledger.jsonl}"
UI_SERVER="${PICORG_UI_SERVER:-waitress}"
# Avoid inheriting an unresolved shell hostname into the Flask bind address.
if [[ -z "${PICORG_UI_HOST:-}" ]]; then
    if [[ "$HOST" == "$(hostname)" || "$HOST" == "server6" ]] || { [[ "$HOST" != "0.0.0.0" && "$HOST" != "127.0.0.1" && "$HOST" != "localhost" ]] && ! getent ahostsv4 "$HOST" >/dev/null 2>&1; }; then
        HOST="0.0.0.0"
    fi
fi
REBUILD=1

usage() {
    cat <<'EOF'
Usage: restart_picorg_review.sh [--no-rebuild] [--audit PATH] [--screen-name NAME]

Stops the PicOrg review UI, optionally rebuilds/validates the face database,
then starts the LAN-bound UI in a detached screen session.
EOF
}

while (($#)); do
    case "$1" in
        --no-rebuild) REBUILD=0 ;;
        --audit) AUDIT="$2"; shift ;;
        --screen-name) SESSION="$2"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

if [[ -z "$AUDIT" ]]; then
    AUDIT="$(find "$ROOT_DIR/.cache/picorg/audits" -maxdepth 1 -type f -name '20*.json' \
        ! -name '*.preflight.json' ! -name '*.face-clusters.json' ! -name '*.clusters.json' \
        ! -name '*.cluster-purity*.json' ! -name '*.face-cluster-purity.json' \
        ! -name '*.reconciled*.json' ! -name '*.run-manifest.json' \
        ! -name '*.identity-candidates.json' -printf '%T@ %p\n' 2>/dev/null \
        | sort -nr | sed -n '1s/^[^ ]* //p')"
fi
[[ -s "$AUDIT" ]] || { echo "error: audit not found or empty: $AUDIT" >&2; exit 2; }

if command -v screen >/dev/null 2>&1; then
    # ``screen -S name`` is ambiguous when an earlier restart left multiple
    # sessions with the same name.  Resolve every matching numeric session ID
    # so stale UI processes do not retain the runweb lock.
    while read -r screen_session; do
        [[ -n "$screen_session" ]] || continue
        screen -S "$screen_session" -X quit >/dev/null 2>&1 || true
    done < <(screen -ls 2>/dev/null | awk -v name="$SESSION" '$1 ~ ("\\." name "$") {print $1}')
fi

# Match only the review UI bound to the requested port. A broad review_ui.py
# kill can terminate an unrelated review instance on another port.
UI_PATTERN="(^|/|[[:space:]])review_ui\\.py([[:space:]]|$).*--port[=[:space:]]${PORT}([[:space:]]|$)"
mapfile -t UI_PIDS < <(pgrep -f -- "$UI_PATTERN" || true)
if ((${#UI_PIDS[@]})); then
    kill "${UI_PIDS[@]}" 2>/dev/null || true
    for _ in {1..20}; do
        sleep 0.25
        pgrep -f -- "$UI_PATTERN" >/dev/null || break
    done
fi

# Resolve the port only after the previous UI has stopped.  This keeps the
# requested LAN port stable across restarts instead of selecting 8789, 8790,
# and so on while the old listener is still shutting down.
REQUESTED_PORT="$PORT"
PORT="$("$ROOT_DIR/picorg_resolve_port.sh" "$REQUESTED_PORT")"
if [[ "$PORT" != "$REQUESTED_PORT" ]]; then
    echo "warning: PORT=$REQUESTED_PORT unavailable; using open port $PORT"
fi

exec {lock_fd}>"$LOCK_FILE"
if ! flock -n "$lock_fd"; then
    echo "error: another runweb.sh process still owns $LOCK_FILE" >&2
    exit 1
fi
# This is only a stale-lock probe. Release it before runweb.sh starts and
# acquires its own lock for the lifetime of the UI process.
flock -u "$lock_fd"
eval "exec ${lock_fd}>&-"

FACE_AUDIT="${FACE_AUDIT:-${AUDIT%.json}.face-clusters.json}"
PREFLIGHT="${PREFLIGHT:-${AUDIT%.json}.preflight.json}"
# Reuse mode must still pass through runweb's face-backed audit validation.
# Opening a reconciled/name-only JSON directly made it possible to review (and
# previously mutate) stale or non-face clusters.
RUN_CMD=(env HOST="$HOST" PORT="$PORT" AUDIT="$AUDIT" LOCK_FILE="$LOCK_FILE" USE_EXISTING_FACE_AUDIT=1 REUSE_EXISTING_PREFLIGHT=1 ./runweb.sh)
if ((REBUILD)); then
    RUN_CMD=(bash -lc "set -o pipefail; ./rebuild_face_data_recover.sh 2>&1 | tee /tmp/picorg-face-rebuild-final.log && exec env HOST=\"$HOST\" PORT=\"$PORT\" AUDIT=\"$AUDIT\" LOCK_FILE=\"$LOCK_FILE\" USE_EXISTING_FACE_AUDIT=1 ./runweb.sh")
fi

if command -v screen >/dev/null 2>&1; then
    printf -v RUN_STRING '%q ' "${RUN_CMD[@]}"
    screen -dmS "$SESSION" bash -lc "cd '$ROOT_DIR' && $RUN_STRING"
    ready_url="http://127.0.0.1:${PORT}/readyz"
    startup_timeout="${PICORG_UI_START_TIMEOUT:-30}"
    if [[ ! "$startup_timeout" =~ ^[1-9][0-9]*$ ]]; then
        echo "error: PICORG_UI_START_TIMEOUT must be a positive integer" >&2
        exit 2
    fi
    deadline=$((SECONDS + startup_timeout))
    while (( SECONDS < deadline )); do
        if curl -fsS --max-time 1 "$ready_url" >/dev/null 2>&1; then
            echo "started screen session: $SESSION"
            echo "ready: $ready_url"
            echo "attach: screen -r $SESSION"
            exit 0
        fi
        if ! screen -ls 2>/dev/null | awk -v name="$SESSION" '$1 ~ ("\\." name "$")' | grep -q .; then
            break
        fi
        sleep 0.5
    done
    if ! screen -ls 2>/dev/null | awk -v name="$SESSION" '$1 ~ ("\\." name "$")' | grep -q .; then
        echo "error: screen session '$SESSION' exited immediately; inspect screen availability and the rebuild log" >&2
        exit 1
    fi
    echo "error: PicOrg UI did not become ready within ${startup_timeout}s at $ready_url" >&2
    echo "attach: screen -r $SESSION" >&2
    exit 1
else
    echo "screen is unavailable; run the following command in the foreground:" >&2
    printf '%q ' "${RUN_CMD[@]}"; printf '\n'
    exit 1
fi
