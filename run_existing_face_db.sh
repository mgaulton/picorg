#!/usr/bin/env bash
set -Eeuo pipefail

# Use a completed face database without resetting or rebuilding it.  This is
# intentionally separate from rebuild_face_data.sh so a quick review pass
# cannot accidentally erase the current database.
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"
# Serialize all long-running PicOrg pipeline modes.  This prevents a second
# ingest/match run from contending for the same FUSE roots or overwriting
# shared audits while an existing run is still active.
PIPELINE_LOCK="${PICORG_PIPELINE_LOCK:-/tmp/picorg-pipeline.lock}"
exec 8>"$PIPELINE_LOCK"
if ! flock -n 8; then
    echo "error: another PicOrg pipeline is already active (lock: $PIPELINE_LOCK)" >&2
    exit 75
fi
export PICORG_PIPELINE_LOCK_HELD=1
# Keep progress/ETA lines visible when this script is run through screen/tmux
# and tee; otherwise Python block-buffers stdout behind the pipe.
export PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}"

HOST="${PICORG_UI_HOST:-${HOST:-0.0.0.0}}"
PORT="${PORT:-8787}"
DB="${PICORG_FACE_DB:-/opt/photo_reorg/data/high_accuracy_faces.db}"
AUDIT="${AUDIT:-}"
GALLERY="${PICORG_GALLERY_MANIFEST:-$ROOT_DIR/.cache/picorg/reference-gallery.json}"
THRESHOLD="${IDENTITY_MATCH_THRESHOLD:-0.45}"
MARGIN="${IDENTITY_MATCH_MARGIN:-0.08}"
FACE_CACHE="${FACE_CACHE:-$ROOT_DIR/.cache/picorg/20260827T102640Z.face-embeddings.json}"
TRUST_FACE_CACHE="${PICORG_TRUST_FACE_CACHE:-0}"
EMBEDDING_STORE="${PICORG_EMBEDDING_STORE:-$ROOT_DIR/.cache/picorg/face_embeddings.sqlite3}"
IDENTITY_STORE="${PICORG_IDENTITY_STORE:-$ROOT_DIR/.cache/picorg/identity_evidence.sqlite3}"
VERIFY_MARKERS="${PICORG_VERIFY_MARKERS:-0}"
REFRESH_AUDIT="${PICORG_REFRESH_AUDIT:-1}"

usage() {
    cat <<'EOF'
Usage: run_existing_face_db.sh [--ingest]

Refresh the name audit, match only new/changed unmatched images against the
validated face database, rebuild review clusters, and start the LAN UI.
--ingest runs the intake/priority-dedupe/name-organization stage first.
Set PICORG_REFRESH_AUDIT=0 to reuse an explicitly selected existing audit.
EOF
}

while (($#)); do
    case "$1" in
        --ingest) RUN_INGEST=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "error: unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done
RUN_INGEST="${RUN_INGEST:-0}"
if ((RUN_INGEST)); then
    REFRESH_AUDIT=0
    AUDIT=""
    echo "[0/4] ingesting and applying the name-organization safety gate"
    "$ROOT_DIR/run_name_org.sh" --ingest
fi

if [[ "${PICORG_SYNC_CONFIRMATIONS:-1}" == "1" ]]; then
    echo "[0/4] reconciling queued/manual confirmations before matching"
    .venv/bin/python reconcile_confirmed.py --apply --allow-partial-health --evidence-db "$IDENTITY_STORE"
fi
if [[ "${PICORG_UPDATE_BASELINE:-1}" == "1" && "${PICORG_SCHEDULER_MANAGED:-0}" != "1" && -s "$IDENTITY_STORE" ]]; then
    echo "[0/4] refreshing canonical face baseline (hash/cache incremental)"
    PICORG_IDENTITY_STORE="$IDENTITY_STORE" "$ROOT_DIR/build_canonical_face_baseline.sh"
fi

if [[ -z "$AUDIT" ]]; then
    if [[ "$REFRESH_AUDIT" == "1" ]]; then
        echo "[0/4] generating a fresh fingerprinted name audit (cached unchanged paths)"
        PICORG_AUDIT_FINGERPRINTS=1 "$ROOT_DIR/picorg_manual.sh" dry-run
    fi
    AUDIT="$(find "$ROOT_DIR/.cache/picorg/audits" -maxdepth 1 -type f -name '20*.json' \
        ! -name '*.preflight.json' ! -name '*.face-clusters.json' ! -name '*.clusters.json' \
        ! -name '*.cluster-purity*.json' ! -name '*.reconciled*.json' \
        ! -name '*.run-manifest.json' ! -name '*.identity-candidates.json' \
        -printf '%T@ %p\n' 2>/dev/null | sort -nr | sed -n '1s/^[^ ]* //p')"
fi
if [[ -z "$AUDIT" || ! -s "$AUDIT" ]]; then
    echo "error: no primary audit found; run the intake/name audit first" >&2
    exit 2
fi
if [[ ! -s "$DB" ]]; then
    echo "error: face database not found: $DB" >&2
    exit 2
fi
if pgrep -af '[r]ebuild_face_database.py' >/dev/null 2>&1; then
    echo "error: a face database rebuild is still running; wait for it to validate" >&2
    exit 2
fi

echo "[1/4] validating existing face database: $DB"
.venv/bin/python verify_face_database.py \
    --db "$DB" \
    --min-faces "${MIN_FACE_ENCODINGS:-100}" \
    --min-identities "${MIN_FACE_IDENTITIES:-10}"

echo "[2/4] generating quality-diverse gallery from existing database"
mkdir -p "$(dirname "$GALLERY")"
GALLERY_ARGS=(
    --db "$DB"
    --max-per-person "${MAX_EXEMPLARS_PER_IDENTITY:-24}"
    --min-quality "${MIN_EXEMPLAR_QUALITY:-0.35}"
    --markers "$ROOT_DIR/identity_face_markers.json"
    --output "$GALLERY"
)
if [[ "$VERIFY_MARKERS" == "1" ]]; then
    echo "marker hash verification enabled (may be slow on offline/network roots)"
    GALLERY_ARGS+=(--verify-markers)
else
    echo "marker hash verification skipped (set PICORG_VERIFY_MARKERS=1 to verify)"
fi
.venv/bin/python select_reference_gallery.py "${GALLERY_ARGS[@]}"

echo "[3/4] matching audit against existing identity gallery"
OUT="${IDENTITY_MATCH_OUTPUT:-${AUDIT%.json}.identity-candidates.json}"
if [[ -s "$FACE_CACHE" ]]; then
    .venv/bin/python face_embedding_store.py --db "$EMBEDDING_STORE" --import-json "$FACE_CACHE"
fi
MATCH_ARGS=(
    --audit "$AUDIT"
    --previous "$OUT"
    --db "$DB"
    --gallery-manifest "$GALLERY"
    --output "$OUT"
    --threshold "$THRESHOLD"
    --margin "$MARGIN"
    --quality-weighted
    --adaptive-jitters
    --embedding-cache "$FACE_CACHE"
    --embedding-store "$EMBEDDING_STORE"
    --evidence-db "$IDENTITY_STORE"
    # Incremental mode must eventually cover every unmatched image.  Set a
    # positive LIMIT_PER_CLUSTER only for a bounded pilot/sample run.
    --limit-per-cluster "${LIMIT_PER_CLUSTER:-0}"
)
if [[ "$TRUST_FACE_CACHE" == "1" ]]; then
    echo "trusted embedding-cache reuse enabled (use only when source paths are unchanged)"
    MATCH_ARGS+=(--trust-embedding-cache)
else
    echo "embedding cache reuse requires matching audit fingerprints (set PICORG_TRUST_FACE_CACHE=1 to trust unchanged paths)"
fi
.venv/bin/python incremental_face_match.py "${MATCH_ARGS[@]}"

echo "[4/4] rebuilding face-only review clusters and starting LAN UI"
# Re-cluster the audit, but preserve the extraction cache.  This never resets
# the identity database; it only refreshes the review grouping and reconciliation.
if [[ "${PICORG_RESTART_UI:-1}" == "1" && -e /tmp/picorg-runweb.lock ]] && command -v screen >/dev/null 2>&1; then
    while read -r session; do
        [[ -n "$session" ]] || continue
        screen -S "$session" -X quit >/dev/null 2>&1 || true
    done < <(screen -ls 2>/dev/null | awk '$1 ~ /\.picorg-review$/ {print $1}')
    for _ in {1..20}; do
        if ! fuser -s /tmp/picorg-runweb.lock 2>/dev/null; then
            break
        fi
        sleep 0.2
    done
fi
# The pipeline lock protects audit/database construction, not the long-lived
# review server. Release it before starting the UI so the scheduler can run a
# later cycle while the UI remains available. runweb.sh receives the path and
# makes the UI queue-only if another pipeline acquires it.
exec 8>&-
unset PICORG_PIPELINE_LOCK_HELD
HOST="$HOST" PORT="$PORT" AUDIT="$AUDIT" IDENTITY_MATCH_OUTPUT="$OUT" \
    PICORG_UI_PIPELINE_LOCK="$PIPELINE_LOCK" \
    FACE_CACHE="$FACE_CACHE" FORCE_FACE_REBUILD=1 \
    USE_EXISTING_FACE_AUDIT=0 ./runweb.sh
