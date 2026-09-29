#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

# Keep heartbeat/progress lines visible when output is captured by screen, tee,
# or the TUI instead of waiting for Python's block buffer to fill.
export PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}"

# Direct name-only runs share the pipeline lock with existing-DB refreshes.
# run_existing_face_db.sh exports PICORG_PIPELINE_LOCK_HELD when it invokes
# this script, so the nested call remains re-entrant without permitting
# concurrent top-level jobs.
PIPELINE_LOCK="${PICORG_PIPELINE_LOCK:-/tmp/picorg-pipeline.lock}"
if [[ "${PICORG_PIPELINE_LOCK_HELD:-0}" != "1" ]]; then
    exec 8>"$PIPELINE_LOCK"
    if ! flock -n 8; then
        echo "error: another PicOrg pipeline is already active (lock: $PIPELINE_LOCK)" >&2
        exit 75
    fi
    export PICORG_PIPELINE_LOCK_HELD=1
fi

AUDIT_ROOT="${AUDIT_ROOT:-$ROOT_DIR/.cache/picorg/audits}"
PYTHON="${PYTHON:-$ROOT_DIR/.venv/bin/python}"
PRIORITY_DEDUPE_REPORT="${PICORG_PRIORITY_DEDUPE_REPORT:-$ROOT_DIR/.cache/picorg/priority-dedupe.json}"
PRIORITY_DEDUPE_CACHE="${PICORG_PRIORITY_DEDUPE_CACHE:-$ROOT_DIR/.cache/picorg/priority-dedupe-hashes.json}"
PRIORITY_QUARANTINE="${PICORG_PRIORITY_DEDUPE_QUARANTINE:-$ROOT_DIR/.cache/picorg/priority-dedupe-quarantine}"
RUN_INGEST=0
DRY_RUN_CACHE="${PICORG_DRY_RUN_CACHE:-$ROOT_DIR/.cache/picorg/dry-run-cache.json}"
SOURCE_HEALTH="${PICORG_SOURCE_HEALTH:-$ROOT_DIR/.cache/picorg/source-health.json}"
INTAKE_ROOTS=(
    /mnt/elements16/@mixedpics
    /mnt/elements16a/Pron/jdownloaderscomplete
    /mnt/desktop/Pictures
)
PROTECTED_ROOTS=(
    /mnt/elements16a/Pron/redditdaily/downloads
    /mnt/elements16a/Pron/metadaily/downloads
)

usage() {
    cat <<'EOF'
Usage: run_name_org.sh [--ingest]

Runs only PicOrg's name/alias organization workflow:
  1. Optionally ingest completed downloads and priority-quarantine duplicates.
  2. Create a fresh fingerprinted dry-run audit.
  3. Apply the precision-only high-confidence safety gate.
  4. Move unchanged >=0.95 name matches into the canonical tree.

No face extraction, face matching, broad fdupes deletion, or web UI is run.
When --ingest is used, exact duplicates in intake roots are moved to the
recoverable priority-dedupe quarantine; Metadaily/Redditdaily are never moved.
EOF
}

case "${1:-}" in
    "") ;;
    --ingest) RUN_INGEST=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
esac

mkdir -p "$AUDIT_ROOT" "$(dirname "$DRY_RUN_CACHE")"
RUN_LOG="$(mktemp "${TMPDIR:-/tmp}/picorg-name-org.XXXXXX.log")"
cleanup() { rm -f "$RUN_LOG"; }
trap cleanup EXIT

ALL_ROOTS=("${INTAKE_ROOTS[@]}" "${PROTECTED_ROOTS[@]}")
HEALTH_ARGS=()
for root in "${ALL_ROOTS[@]}"; do HEALTH_ARGS+=(--root "$root"); done
if ! "$PYTHON" "$ROOT_DIR/picorg_health.py" "${HEALTH_ARGS[@]}" --output "$SOURCE_HEALTH" >/dev/null; then
    echo "error: no healthy PicOrg source roots; see $SOURCE_HEALTH" >&2
    exit 75
fi
INTAKE_ARGS=()
for root in "${INTAKE_ROOTS[@]}"; do INTAKE_ARGS+=(--root "$root"); done
mapfile -t HEALTHY_INTAKE_ROOTS < <("$PYTHON" "$ROOT_DIR/picorg_health.py" "${INTAKE_ARGS[@]}" --healthy-paths --require-writable)
if ((${#HEALTHY_INTAKE_ROOTS[@]} == 0)); then
    echo "error: no healthy intake roots; refusing to create an empty audit" >&2
    exit 75
fi
PICORG_INTAKE_ROOTS_VALUE="$(IFS=:; echo "${HEALTHY_INTAKE_ROOTS[*]}")"
export PICORG_INTAKE_ROOTS="$PICORG_INTAKE_ROOTS_VALUE"
echo "healthy intake roots: ${PICORG_INTAKE_ROOTS}"

if ((RUN_INGEST)); then
    echo "[1/4] ingesting completed downloads"
    # Disable the remote helper's non-priority fdupes deletion.  PicOrg's
    # priority-aware pass below preserves Metadaily/Redditdaily copies.
    if "$PYTHON" "$ROOT_DIR/picorg_health.py" --root /mnt/elements16/@mixedpics --require-writable >/dev/null; then
        set +e
        SKIP_FDUPES=1 /opt/move_downloads_remote.sh
        remote_status=$?
        set -e
        if ((remote_status != 0 && remote_status != 23)); then
            echo "error: remote download intake failed (exit ${remote_status})" >&2
            exit "$remote_status"
        fi
        if ((remote_status == 23)); then
            echo "warning: remote intake returned rsync code 23; continuing after metadata-only transfer warnings" >&2
        fi
    else
        echo "intake destination is read-only; skipping remote download intake"
    fi
    echo "[2/5] priority-quarantining exact intake duplicates"
    DEDUPE_ARGS=(
        --priority-root /mnt/elements16a/Pron/redditdaily \
        --priority-root /mnt/elements16a/Pron/metadaily/downloads \
        --target-root /mnt/elements16/@mixedpics \
        --target-root /mnt/desktop \
        --output "$PRIORITY_DEDUPE_REPORT" \
        --cache "$PRIORITY_DEDUPE_CACHE" \
        --quarantine-root "$PRIORITY_QUARANTINE"
    )
    PROTECTED_ARGS=()
    for root in "${PROTECTED_ROOTS[@]}"; do PROTECTED_ARGS+=(--root "$root"); done
    TARGET_ARGS=(--root /mnt/elements16/@mixedpics --root /mnt/desktop)
    if "$PYTHON" "$ROOT_DIR/picorg_health.py" "${PROTECTED_ARGS[@]}" --require-all >/dev/null && \
       "$PYTHON" "$ROOT_DIR/picorg_health.py" "${TARGET_ARGS[@]}" --require-all --require-writable >/dev/null; then
        DEDUPE_ARGS+=(--apply)
        echo "protected roots healthy: priority duplicate quarantine enabled"
    else
        echo "protected root unavailable: priority duplicate quarantine is report-only"
    fi
    "$PYTHON" "$ROOT_DIR/dedupe_priority.py" "${DEDUPE_ARGS[@]}"
else
    echo "[1/4] intake skipped (use --ingest to ingest and priority-dedupe)"
fi

if ((RUN_INGEST)); then
    echo "[3/5] generating fingerprinted name-match audit"
else
    echo "[2/4] generating fingerprinted name-match audit"
fi
set -o pipefail
PICORG_AUDIT_FINGERPRINTS=1 \
    PICORG_DRY_RUN_CACHE="$DRY_RUN_CACHE" \
    PYTHON="$PYTHON" \
    "$ROOT_DIR/picorg_manual.sh" dry-run 2>&1 | tee "$RUN_LOG"

AUDIT_PATH="$(sed -n 's/^audit: //p' "$RUN_LOG" | tail -1)"
if [[ -z "$AUDIT_PATH" || ! -s "$AUDIT_PATH" ]]; then
    echo "error: dry-run did not produce a usable audit" >&2
    exit 1
fi

if ((RUN_INGEST)); then
    echo "[4/5] validating precision-only name-move gate"
else
    echo "[3/4] validating precision-only name-move gate"
fi
"$PYTHON" "$ROOT_DIR/pipeline_safety_gate.py" \
    --audit "$AUDIT_PATH" \
    --name-move-only

if ((RUN_INGEST)); then
    echo "[5/5] applying unchanged high-confidence name matches"
else
    echo "[4/4] applying unchanged high-confidence name matches"
fi
PYTHON="$PYTHON" "$ROOT_DIR/picorg_manual.sh" apply --audit-input "$AUDIT_PATH"
echo "name organization complete; audit: $AUDIT_PATH"
