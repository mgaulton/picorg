#!/usr/bin/env bash
set -Eeuo pipefail

# Keep long PicOrg jobs attached to a persistent terminal session so an SSH
# disconnect does not terminate extraction, rebuild, or review work.
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SESSION="${PICORG_SCREEN_SESSION:-picorg-job}"

usage() {
    cat <<'EOF'
Usage: ./picorg_screen.sh [--session NAME] MODE [args...]

MODE:
  menu       interactive safe menu
  tui        interactive log/menu TUI
  pipeline   full pipeline (forwards remaining args)
  rebuild    rebuild/validate with automatic unreadable-file recovery
  existing   reuse the validated database and refresh matches/UI
  review     restart the review UI without rebuilding
  benchmark  held-out accuracy calibration (review-only)
  baseline   build canonical identity face baseline (read-only sources)
  scheduler  persistent automatic ingest/reconcile/pipeline scheduler

Convenience aliases:
  (no mode)  open the menu
  full       full safe pipeline (same as pipeline)
  quick      reuse the validated face DB and refresh matches/UI
  ui         restart the review UI only
EOF
}

while (($#)); do
    case "$1" in
        --session)
            (($# >= 2)) || { echo "error: --session requires a name" >&2; exit 2; }
            SESSION="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            break
            ;;
    esac
done

MODE="${1:-menu}"
if (($#)); then
    shift
fi
case "$MODE" in
    menu) COMMAND=("$ROOT_DIR/run_picorg_menu.sh" "$@") ;;
    tui) COMMAND=("$ROOT_DIR/.venv/bin/python" "$ROOT_DIR/picorg_tui.py" "$@") ;;
    pipeline) COMMAND=("$ROOT_DIR/run_picorg.sh" "$@") ;;
    full) COMMAND=("$ROOT_DIR/run_picorg.sh" "$@") ;;
    rebuild) COMMAND=("$ROOT_DIR/rebuild_face_data_recover.sh" "$@") ;;
    existing) COMMAND=("$ROOT_DIR/run_existing_face_db.sh" "$@") ;;
    quick) COMMAND=("$ROOT_DIR/run_existing_face_db.sh" "$@") ;;
    review) COMMAND=("$ROOT_DIR/restart_picorg_review.sh" --no-rebuild "$@") ;;
    ui) COMMAND=("$ROOT_DIR/restart_picorg_review.sh" --no-rebuild "$@") ;;
    benchmark) COMMAND=("$ROOT_DIR/run_accuracy_benchmark.sh" "$@") ;;
    baseline) COMMAND=("$ROOT_DIR/build_canonical_face_baseline.sh" "$@") ;;
    scheduler) COMMAND=("$ROOT_DIR/.venv/bin/python" "$ROOT_DIR/picorg_scheduler.py" daemon "$@") ;;
    *) usage >&2; exit 2 ;;
esac

# If the caller is already persistent, avoid creating a nested session.
if [[ -n "${STY:-}" || -n "${TMUX:-}" ]]; then
    exec "${COMMAND[@]}"
fi

command_text="$(printf '%q ' "${COMMAND[@]}")"
if command -v screen >/dev/null 2>&1; then
    # Remove dead sockets first. A plain substring search can also report a
    # stale/dead name even though `screen -r` cannot attach to it.
    screen -wipe >/dev/null 2>&1 || true
    if screen -S "$SESSION" -Q select . >/dev/null 2>&1; then
        echo "error: screen session already exists: $SESSION" >&2
        echo "attach with: screen -r $SESSION" >&2
        exit 1
    fi
    if screen -dmS "$SESSION" bash -lc "cd $(printf '%q' "$ROOT_DIR") && exec $command_text"; then
        sleep 0.2
        if screen -S "$SESSION" -Q select . >/dev/null 2>&1; then
            echo "started screen session: $SESSION"
            echo "attach: screen -r $SESSION"
            exit 0
        fi
        echo "warning: screen session exited during launch; trying tmux" >&2
    else
        echo "warning: screen could not start (often /run is read-only); trying tmux" >&2
    fi
fi

if command -v tmux >/dev/null 2>&1; then
    # Some hosts mount /run read-only.  Keep the fallback socket in /tmp so
    # detached jobs remain launchable without an operator export.
    TMUX_DIR="${TMUX_TMPDIR:-${PICORG_TMUX_TMPDIR:-/tmp/picorg-tmux}}"
    mkdir -p "$TMUX_DIR"
    chmod 700 "$TMUX_DIR"
    TMUX_SOCKET="$TMUX_DIR/$SESSION.sock"
    if tmux -S "$TMUX_SOCKET" has-session -t "$SESSION" 2>/dev/null; then
        echo "error: tmux session already exists: $SESSION" >&2
        echo "attach with: tmux -S $TMUX_SOCKET attach -t $SESSION" >&2
        exit 1
    fi
    if tmux -S "$TMUX_SOCKET" new-session -d -s "$SESSION" "cd $(printf '%q' "$ROOT_DIR") && exec $command_text"; then
        sleep 0.2
        if tmux -S "$TMUX_SOCKET" has-session -t "$SESSION" 2>/dev/null; then
            echo "started tmux session: $SESSION"
            echo "attach: tmux -S $TMUX_SOCKET attach -t $SESSION"
            exit 0
        fi
    fi
    echo "warning: tmux session exited during launch; using nohup fallback" >&2
fi

# Last-resort detached mode for hosts where screen/tmux cannot allocate a
# session (for example a restricted container or broken /run mount).
LOG_DIR="$ROOT_DIR/.cache/picorg/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="${PICORG_JOB_LOG:-$LOG_DIR/$SESSION-$(date -u +%Y%m%dT%H%M%SZ).log}"
PID_FILE="${PICORG_JOB_PID_FILE:-/tmp/$SESSION.pid}"
nohup "${COMMAND[@]}" >"$LOG_FILE" 2>&1 < /dev/null &
JOB_PID=$!
printf '%s\n' "$JOB_PID" > "$PID_FILE"
sleep 0.2
if kill -0 "$JOB_PID" 2>/dev/null; then
    echo "started detached job: pid=$JOB_PID"
    echo "log: $LOG_FILE"
    echo "pid file: $PID_FILE"
    echo "watch: tail -f $LOG_FILE"
    exit 0
fi
echo "error: detached job exited during launch; inspect $LOG_FILE" >&2
exit 1
