#!/usr/bin/env bash
if [ -z "${BASH_VERSION:-}" ]; then
    exec /usr/bin/env bash "$0" "$@"
fi
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

if [[ ! -t 0 || ! -t 1 ]]; then
    echo "This menu requires an interactive terminal." >&2
    exec "$ROOT_DIR/run_picorg.sh" --help
fi

# The split-pane curses UI owns the interactive terminal. The historical shell
# menu below is unreachable after exec and is retained only for old references.
# Optional FACE_BENCHMARK_REPORT enables the stronger held-out apply gate.
exec /usr/bin/python3 "$ROOT_DIR/picorg_tui.py"

confirm() {
    local answer
    read -r -p "$1 [y/N] " answer
    [[ "$answer" == "y" || "$answer" == "Y" ]]
}

while true; do
    clear 2>/dev/null || true
    cat <<'EOF'
PicOrg workflow menu
====================

  1) New intake + safe review       Ingest, match, face groups, UI; no library moves
  2) Safe scan (no intake)          Match current files; no intake or applies; UI
  3) Review existing face groups    Reuse newest complete face audit; UI quickly
  4) Apply high-confidence moves    Safety-gated names + dedupe, then face review
  5) Quarantine exact duplicates    Hash dedupe and quarantine target duplicates
  6) Stop face rebuild   Stop an active face database rebuild
  7) Rebuild identity gallery (slow) Reset/filter references and rebuild face records
  8) Incremental DB refresh          Match only new/changed images, clusters, UI
  9) Full accuracy rebuild + review Rebuild DB, force face groups, launch UI
  g) Canonical face baseline         Build hash-keyed face evidence from MD/RD + organized identities
  f) Full pipeline                  Ingest, names, face DB, match, clusters, UI
  b) Benchmark confirmed faces       Held-out calibration; no moves
  h) Help and command details
  0) Exit

Library moves and duplicate quarantine require confirmation.
EOF
    read -r -p "Choose an option [1]: " choice
    choice="${choice:-1}"
    case "$choice" in
        1)
            "$ROOT_DIR/run_picorg.sh"
            ;;
        2)
            "$ROOT_DIR/run_picorg.sh" --dry-run
            ;;
        3)
            "$ROOT_DIR/run_picorg.sh" --no-ingest --reuse-faces
            ;;
        4)
            if confirm "Run the safety-gated high-confidence workflow?"; then
                "$ROOT_DIR/run_picorg.sh" --apply-high-confidence
            else
                echo "Cancelled."
            fi
            ;;
        5)
            if confirm "Quarantine exact duplicates from @mixedpics and /mnt/desktop?"; then
                "$ROOT_DIR/run_picorg.sh" --no-ingest --dedupe-apply --reuse-faces
            else
                echo "Cancelled."
            fi
            ;;
        6)
            FACE_PIDS="$(pgrep -f '[r]ebuild_face_database.py' || true)"
            if [[ -z "$FACE_PIDS" ]]; then
                echo "No active face rebuild found."
            elif confirm "Stop active face rebuild (PID $FACE_PIDS)?"; then
                read -r -a FACE_PID_ARRAY <<< "$FACE_PIDS"
                kill "${FACE_PID_ARRAY[@]}"
                echo "Stop signal sent."
            else
                echo "Cancelled."
            fi
            ;;
        7)
            if confirm "Reset and rebuild face data from sorted + MD/RD references? This can take hours."; then
                "$ROOT_DIR/rebuild_face_data.sh"
            else
                echo "Cancelled."
            fi
            ;;
        8)
            "$ROOT_DIR/run_existing_face_db.sh"
            ;;
        9)
            if confirm "Run the full accuracy workflow? It resets face data and rebuilds face clusters."; then
                "$ROOT_DIR/rebuild_face_data.sh"
                FORCE_FACE_REBUILD=1 USE_EXISTING_FACE_AUDIT=0 "$ROOT_DIR/runweb.sh"
            else
                echo "Cancelled."
            fi
            ;;
        f|F)
            if confirm "Run the complete ingest/name/face/UI pipeline?"; then
                "$ROOT_DIR/run_picorg.sh" --apply-high-confidence
            else
                echo "Cancelled."
            fi
            ;;
        b|B)
            bash "$ROOT_DIR/run_accuracy_benchmark.sh"
            ;;
        g|G)
            "$ROOT_DIR/build_canonical_face_baseline.sh"
            ;;
        h|H|10)
            "$ROOT_DIR/run_picorg.sh" --help
            ;;
        0)
            exit 0
            ;;
        *)
            echo "Invalid option: $choice" >&2
            ;;
    esac
    printf '\nPress Enter to return to the menu...'
    read -r
done
