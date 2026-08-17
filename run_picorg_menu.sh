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

  1) Full safe run       Intake, hash dedupe report, audit, face groups, UI
  2) Full dry-run        No intake or applies; produce reports and start UI
  3) Review existing     Reuse newest complete face audit; start UI quickly
  4) High-confidence     Safety-gated name moves, duplicate quarantine, face review
  5) Quarantine dupes    Hash dedupe and quarantine exact target duplicates
  6) Show CLI help
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
