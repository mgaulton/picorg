#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

# Preserve live stage/heartbeat output when the pipeline runs detached.
export PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}"

# Default mode stops at manual review; live apply requires an explicit option.
HOST="${PICORG_UI_HOST:-${HOST:-0.0.0.0}}"
PORT="${PORT:-8787}"
# Do not propagate a shell hostname that is unsuitable for binding or LAN
# links (common shells export HOST=server6 even when that name is not DNS).
if [[ -z "${PICORG_UI_HOST:-}" ]]; then
    if [[ "$HOST" == "$(hostname)" || "$HOST" == "server6" ]] || { [[ "$HOST" != "0.0.0.0" && "$HOST" != "127.0.0.1" && "$HOST" != "localhost" ]] && ! getent ahostsv4 "$HOST" >/dev/null 2>&1; }; then
        HOST="0.0.0.0"
    fi
fi
REQUESTED_PORT="$PORT"
PORT="$("$ROOT_DIR/picorg_resolve_port.sh" "$REQUESTED_PORT")"
if [[ "$PORT" != "$REQUESTED_PORT" ]]; then
    echo "warning: PORT=$REQUESTED_PORT unavailable; using open port $PORT"
fi
AUDIT_ROOT="${AUDIT_ROOT:-$ROOT_DIR/.cache/picorg/audits}"
INSTALL_FACE_DEPS="${INSTALL_FACE_DEPS:-1}"
REFERENCE_ROOT="${REFERENCE_ROOT:-/mnt/elements16/@mixedpics_sorted}"
REFERENCE_ROOTS="${REFERENCE_ROOTS:-$REFERENCE_ROOT:/mnt/elements16a/Pron/metadaily/downloads:/mnt/elements16a/Pron/redditdaily/downloads}"
IDENTITY_REGISTRY="${PICORG_IDENTITY_REGISTRY:-/opt/shared/identity_aliases.json}"
CONFIRMED_IDENTITIES_FILES="${PICORG_CONFIRMED_IDENTITIES_FILES:-$ROOT_DIR/identity_face_markers.json:$IDENTITY_REGISTRY}"
IDENTITY_STORE="${PICORG_IDENTITY_STORE:-$ROOT_DIR/.cache/picorg/identity_evidence.sqlite3}"
COALESCE_CONFIRMED_ARGS=(--confirmed-only-external)
IFS=: read -r -a confirmed_identity_files <<< "$CONFIRMED_IDENTITIES_FILES"
for confirmed_identity_file in "${confirmed_identity_files[@]}"; do
    [[ -n "$confirmed_identity_file" ]] && COALESCE_CONFIRMED_ARGS+=(--confirmed-identities-file "$confirmed_identity_file")
done
FACE_DATABASE_BACKEND="${FACE_DATABASE_BACKEND:-photo_reorg}"
case "$FACE_DATABASE_BACKEND" in
    photo_reorg|picorg) ;;
    *) echo "error: FACE_DATABASE_BACKEND must be photo_reorg or picorg" >&2; exit 2 ;;
esac
if [[ -z "${FACE_DB:-}" && "$FACE_DATABASE_BACKEND" == "picorg" ]]; then
    FACE_DB="$ROOT_DIR/.cache/picorg/face_database.sqlite3"
else
    FACE_DB="${FACE_DB:-/opt/photo_reorg/data/high_accuracy_faces.db}"
fi
COALESCED_REFERENCE_ROOT="${COALESCED_REFERENCE_ROOT:-${TMPDIR:-/tmp}/picorg-face-references-coalesced}"
FACE_CACHE="${FACE_CACHE:-$ROOT_DIR/.cache/picorg/face-embeddings-native.json}"
HASH_CACHE="${HASH_CACHE:-$ROOT_DIR/.cache/picorg/priority-dedupe-hashes.json}"
EXEMPLAR_MANIFEST="${EXEMPLAR_MANIFEST:-$ROOT_DIR/.cache/picorg/reference-gallery.json}"
IDENTITY_MATCH_THRESHOLD="${IDENTITY_MATCH_THRESHOLD:-0.45}"
IDENTITY_MATCH_MARGIN="${IDENTITY_MATCH_MARGIN:-0.08}"
MIN_FACE_ENCODINGS="${MIN_FACE_ENCODINGS:-100}"
MIN_FACE_IDENTITIES="${MIN_FACE_IDENTITIES:-10}"
REBUILD_LOCK_FILE="${REBUILD_LOCK_FILE:-/tmp/picorg-face-rebuild.lock}"
LIVE_HIGH_CONFIDENCE=0
RUN_INGEST=1
RUN_REBUILD=1
RUN_UI=1

usage() {
    cat <<'EOF'
Usage: run_face_review_pipeline.sh [--ingest|--no-ingest] [--apply-high-confidence] [--reuse-face-db] [--no-ui]

Default: ingest, picorg dry-run, complete face-database rebuild, face grouping, and LAN review UI; no moves.
--ingest: run move_downloads_remote.sh before the dry-run/apply stages.
--no-ingest: skip move_downloads_remote.sh when a caller already completed intake.
--reuse-face-db: skip face extraction/rebuild and reuse the existing complete face audit.
--apply-high-confidence: apply picorg's >=0.95 name matches first, rebuild
                         face references from the sorted tree plus Metadaily
                         and Redditdaily downloads, then match/group/UI.
--no-ui: complete the selected pipeline stages and leave the existing review UI untouched.
EOF
}

while (($#)); do
    case "$1" in
        --ingest) RUN_INGEST=1 ;;
        --no-ingest) RUN_INGEST=0 ;;
        --reuse-face-db|--no-rebuild) RUN_REBUILD=0 ;;
        --apply-high-confidence) LIVE_HIGH_CONFIDENCE=1 ;;
        --no-ui) RUN_UI=0 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

if ((LIVE_HIGH_CONFIDENCE)); then
    echo "[1/5] applying picorg high-confidence name matches (>=0.95)"
    export PICORG_AUDIT_FINGERPRINTS=1
    if ((RUN_INGEST)); then
        ./run_media_pipeline.sh --ingest --skip-photo
    else
        ./run_media_pipeline.sh --skip-photo
    fi
    AUDIT_GATE="$(find "$AUDIT_ROOT" -maxdepth 1 -type f -name '*.json' ! -name '*.preflight.json' ! -name '*.face-clusters.json' ! -name '*.reconciled*.json' ! -name '*.run-manifest.json' ! -name '*.identity-candidates.json' -printf '%T@ %p\n' 2>/dev/null | sort -nr | sed -n '1s/^[^ ]* //p')"
    if [[ -z "$AUDIT_GATE" || ! -s "$AUDIT_GATE" ]]; then
        echo "error: no primary dry-run audit found for the safety gate" >&2
        exit 2
    fi
    GATE_ARGS=(
        --audit "$AUDIT_GATE"
        --min-precision "${MIN_GROUND_TRUTH_PRECISION:-0.99}"
        --min-recall "${MIN_GROUND_TRUTH_RECALL:-0.99}"
        --name-move-only
    )
    if [[ -n "${FACE_BENCHMARK_REPORT:-}" ]]; then
        GATE_ARGS+=(
            --benchmark-report "$FACE_BENCHMARK_REPORT"
            --max-fmr "${MAX_BENCHMARK_FMR:-0.001}"
            --max-fmr-upper "${MAX_BENCHMARK_FMR_UPPER:-0.001}"
            --max-fnmr-upper "${MAX_BENCHMARK_FNMR_UPPER:-0.05}"
            --min-genuine "${MIN_BENCHMARK_GENUINE:-100}"
            --min-impostor "${MIN_BENCHMARK_IMPOSTOR:-1000}"
        )
    fi
    .venv/bin/python pipeline_safety_gate.py "${GATE_ARGS[@]}"
    echo "[2/5] applying picorg high-confidence name matches"
    ./picorg_manual.sh apply --audit-input "$AUDIT_GATE"
else
    echo "[1/3] running picorg dry-run pipeline (no apply, photo_reorg skipped)"
    if ((RUN_INGEST)); then
        ./run_media_pipeline.sh --ingest --skip-photo
    else
        ./run_media_pipeline.sh --skip-photo
    fi
fi

AUDIT="$(find "$AUDIT_ROOT" -maxdepth 1 -type f -name '*.json' \
    ! -name '*.preflight.json' ! -name '*.face-clusters.json' ! -name '*.reconciled*.json' \
    ! -name '*.run-manifest.json' ! -name '*.identity-candidates.json' \
    -printf '%T@ %p\n' 2>/dev/null | sort -nr | sed -n '1s/^[^ ]* //p')"
if [[ -z "$AUDIT" || ! -f "$AUDIT" ]]; then
    echo "error: no audit found under $AUDIT_ROOT" >&2
    exit 2
fi

if ((RUN_REBUILD)); then
    exec 8>"$REBUILD_LOCK_FILE"
    if ! flock -n 8; then
        echo "error: another face reference/database rebuild is already running (lock: $REBUILD_LOCK_FILE)" >&2
        exit 2
    fi
    IFS=: read -r -a REFERENCE_DIRS <<< "$REFERENCE_ROOTS"
    if ((${#REFERENCE_DIRS[@]} != 3)); then
        echo "error: REFERENCE_ROOTS must contain exactly sorted, metadaily, and redditdaily roots separated by ':'" >&2
        exit 2
    fi
    echo "[3/6] coalescing sorted tree and MD/RD downloads by canonical identity"
    for reference_dir in "${REFERENCE_DIRS[@]}"; do
        if [[ ! -d "$reference_dir" ]]; then
            echo "error: face reference directory is unavailable: $reference_dir" >&2
            exit 2
        fi
    done
    if [[ "$FACE_DATABASE_BACKEND" == "photo_reorg" && ! -x /opt/photo_reorg/venv/bin/python ]]; then
        echo "error: photo_reorg venv is unavailable" >&2
        exit 2
    fi
    if [[ "$FACE_DATABASE_BACKEND" == "photo_reorg" ]] && ! /opt/photo_reorg/venv/bin/python -c 'import face_recognition_models' >/dev/null 2>&1; then
        echo "error: photo_reorg venv lacks face_recognition_models; install its face requirements before rebuilding" >&2
        exit 2
    fi
    if [[ "$FACE_DATABASE_BACKEND" == "photo_reorg" ]] && pgrep -f '[r]ebuild_face_database.py' >/dev/null; then
        echo "error: a face database rebuild is already running; stop it before restarting" >&2
        exit 2
    fi
    FACE_DB_BACKUP="${FACE_DB}.before-review-rebuild-$(date -u +%Y%m%dT%H%M%SZ)"
    if [[ -e "$FACE_DB" ]]; then
        if [[ "$FACE_DATABASE_BACKEND" == "picorg" ]]; then
            echo "[3/6] preserving native face database until replacement validates"
        else
            echo "[3/6] resetting existing face database (backup: $FACE_DB_BACKUP)"
            .venv/bin/python clean_face_database.py --db "$FACE_DB" --reset \
                --backup "$FACE_DB_BACKUP"
        fi
        restore_review_db_on_failure() {
            local status=$?
            if ((status != 0)) && [[ -s "$FACE_DB_BACKUP" ]]; then
                cp -p -- "$FACE_DB_BACKUP" "$FACE_DB"
                echo "restored previous face database after failed review rebuild: $FACE_DB" >&2
            fi
            return "$status"
        }
        trap restore_review_db_on_failure EXIT
    fi
    .venv/bin/python identity_evidence_store.py \
        --db "$IDENTITY_STORE" \
        --md-registry "$IDENTITY_REGISTRY" \
        --markers "$ROOT_DIR/identity_face_markers.json" \
        --assignments "$ROOT_DIR/review_image_decisions.json"
    .venv/bin/python coalesce_face_references.py \
        --sorted-root "${REFERENCE_DIRS[0]}" \
        --metadaily-root "${REFERENCE_DIRS[1]}" \
        --redditdaily-root "${REFERENCE_DIRS[2]}" \
        --output "$COALESCED_REFERENCE_ROOT" \
        --confirmed-reference-file "$ROOT_DIR/identity_face_markers.json" \
        --confirmed-reference-file "$ROOT_DIR/review_image_decisions.json" \
        "${COALESCE_CONFIRMED_ARGS[@]}" \
        --confirmed-identities-db "$IDENTITY_STORE"
    echo "[4/6] rebuilding $FACE_DATABASE_BACKEND face database from coalesced identity folders"
    if [[ "$FACE_DATABASE_BACKEND" == "picorg" ]]; then
        .venv/bin/python picorg_face_database.py \
            --reference-root "$COALESCED_REFERENCE_ROOT" \
            --output "$FACE_DB" \
            --workers "${PICORG_FACE_WORKERS:-2}" \
            --hash-workers "${PICORG_FACE_HASH_WORKERS:-2}" \
            --hash-cache "$HASH_CACHE" \
            --cache "$FACE_CACHE" \
            --backup "$FACE_DB_BACKUP"
    elif [[ "$FACE_DATABASE_BACKEND" == "photo_reorg" ]]; then
        (cd /opt/photo_reorg && venv/bin/python rebuild_face_database.py \
            --config /opt/photo_reorg/config_enhanced_accurate.json \
            --source-dirs "$COALESCED_REFERENCE_ROOT" --verbose)
    else
        echo "error: FACE_DATABASE_BACKEND must be photo_reorg or picorg" >&2
        exit 2
    fi
    .venv/bin/python verify_face_database.py \
        --db "$FACE_DB" \
        --min-faces "$MIN_FACE_ENCODINGS" \
        --min-identities "$MIN_FACE_IDENTITIES"
    .venv/bin/python select_reference_gallery.py \
        --db "$FACE_DB" \
        --max-per-person "${MAX_EXEMPLARS_PER_IDENTITY:-24}" \
        --min-quality "${MIN_EXEMPLAR_QUALITY:-0.35}" \
        --markers "$ROOT_DIR/identity_face_markers.json" \
        --output "$EXEMPLAR_MANIFEST"
    echo "[5/6] matching remaining audit items (threshold=$IDENTITY_MATCH_THRESHOLD, margin=$IDENTITY_MATCH_MARGIN, quality-weighted)"
    .venv/bin/python face_group_unmatched.py --audit "$AUDIT" --db "$FACE_DB" \
        --gallery-manifest "$EXEMPLAR_MANIFEST" \
        --output "${AUDIT%.json}.face-matches.json" \
        --threshold "$IDENTITY_MATCH_THRESHOLD" \
        --margin "$IDENTITY_MATCH_MARGIN" \
        --quality-weighted \
        --adaptive-jitters
    echo "[6/6] selected audit: $AUDIT"
else
    export USE_EXISTING_FACE_AUDIT=1
    echo "[2/3] selected audit and existing face audit: $AUDIT"
fi
MANIFEST="${AUDIT%.json}.run-manifest.json"
.venv/bin/python pipeline_run_manifest.py --audit "$AUDIT" --output "$MANIFEST" \
    --backend "${FACE_BACKEND:-dlib}" \
    --model-id "${FACE_MODEL_ID:-${FACE_BACKEND:-dlib}}" \
    --detector "${FACE_DETECTOR:-unknown}" \
    --threshold "${FACE_THRESHOLD:-0.48}" \
    --license-status "${FACE_MODEL_LICENSE_STATUS:-unverified}" \
    --cache-root "${CACHE_ROOT:-$ROOT_DIR/.cache/picorg}"
if (( ! RUN_UI )); then
    if ((RUN_REBUILD)); then
        echo "[7/7] pipeline complete; review UI unchanged (--no-ui)"
    else
        echo "[3/3] pipeline complete; review UI unchanged (--no-ui)"
    fi
    exit 0
fi
if ((RUN_REBUILD)); then
    echo "[7/7] starting LAN review UI at http://${HOST}:${PORT}/"
else
    echo "[3/3] starting LAN review UI at http://${HOST}:${PORT}/"
fi
exec env AUDIT="$AUDIT" AUDIT_ROOT="$AUDIT_ROOT" IDENTITY_MATCH_OUTPUT="${AUDIT%.json}.face-matches.json" HOST="$HOST" PORT="$PORT" INSTALL_FACE_DEPS="$INSTALL_FACE_DEPS" ./runweb.sh
