#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

SORTED_ROOT="${SORTED_ROOT:-/mnt/elements16/@mixedpics_sorted}"
METADAILY_ROOT="${METADAILY_ROOT:-/mnt/elements16a/Pron/metadaily/downloads}"
REDDITDAILY_ROOT="${REDDITDAILY_ROOT:-/mnt/elements16a/Pron/redditdaily/downloads}"
REFERENCE_ROOT="${REFERENCE_ROOT:-$ROOT_DIR/.cache/picorg/face-references-coalesced-$(date -u +%Y%m%dT%H%M%SZ)-$$}"
REPAIR_RUN_ID="${PICORG_REPAIR_RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)-$$}"
REPAIR_SCAN_CACHE="${PICORG_REPAIR_SCAN_CACHE:-$ROOT_DIR/.cache/picorg/repair-scan-cache.json}"
IDENTITY_STORE="${PICORG_IDENTITY_STORE:-$ROOT_DIR/.cache/picorg/identity_evidence.sqlite3}"
COALESCE_ROOT_TIMEOUT="${PICORG_COALESCE_ROOT_TIMEOUT:-3600}"
ALLOW_DEGRADED_REBUILD="${PICORG_ALLOW_DEGRADED_REBUILD:-0}"
if [[ "$ALLOW_DEGRADED_REBUILD" != "0" && "$ALLOW_DEGRADED_REBUILD" != "1" ]]; then
    echo "error: PICORG_ALLOW_DEGRADED_REBUILD must be 0 or 1" >&2
    exit 2
fi
ALLOW_FACE_ERRORS="${PICORG_ALLOW_FACE_ERRORS:-$ALLOW_DEGRADED_REBUILD}"
if [[ "$ALLOW_FACE_ERRORS" != "0" && "$ALLOW_FACE_ERRORS" != "1" ]]; then
    echo "error: PICORG_ALLOW_FACE_ERRORS must be 0 or 1" >&2
    exit 2
fi
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
BACKUP="${BACKUP:-${TMPDIR:-/tmp}/high_accuracy_faces.db.before-rebuild-$(date -u +%Y%m%dT%H%M%SZ)}"
MIN_FACE_ENCODINGS="${MIN_FACE_ENCODINGS:-100}"
MIN_FACE_IDENTITIES="${MIN_FACE_IDENTITIES:-10}"
REBUILD_REPORT="${REBUILD_REPORT:-${TMPDIR:-/tmp}/picorg-face-database-rebuild.json}"
FACE_CACHE="${FACE_CACHE:-$ROOT_DIR/.cache/picorg/face-embeddings-native.json}"
HASH_CACHE="${HASH_CACHE:-$ROOT_DIR/.cache/picorg/priority-dedupe-hashes.json}"
EXEMPLAR_MANIFEST="${EXEMPLAR_MANIFEST:-$ROOT_DIR/.cache/picorg/reference-gallery.json}"

atomic_copy() {
    local source="$1" destination="$2" temporary="${2}.tmp.$$"
    rm -f -- "$temporary"
    if ! cp -p -- "$source" "$temporary"; then
        rm -f -- "$temporary"
        return 1
    fi
    if ! mv -f -- "$temporary" "$destination"; then
        rm -f -- "$temporary"
        return 1
    fi
}

# Hardware errors on removable/reference volumes can block a stat indefinitely.
# Pass a colon-separated list of known bad files or directories so coalescing
# can skip them before touching the damaged entry.  Paths are never deleted.
COALESCE_SKIP_ARGS=()
if [[ -n "${PICORG_FACE_SKIP_PATHS:-}" ]]; then
    IFS=: read -r -a _picorg_face_skip_paths <<< "$PICORG_FACE_SKIP_PATHS"
    for _picorg_face_skip_path in "${_picorg_face_skip_paths[@]}"; do
        [[ -n "$_picorg_face_skip_path" ]] && COALESCE_SKIP_ARGS+=(--skip-path "$_picorg_face_skip_path")
    done
fi
MAX_EXEMPLARS_PER_IDENTITY="${MAX_EXEMPLARS_PER_IDENTITY:-24}"
MIN_EXEMPLAR_QUALITY="${MIN_EXEMPLAR_QUALITY:-0.35}"
# Coalescing uses os.walk's directory entries by default; face extraction
# remains the authoritative read/decode validation. Set to 0 for legacy
# per-file pre-stat validation when diagnosing a filesystem.
export PICORG_FAST_MEDIA_SCAN="${PICORG_FAST_MEDIA_SCAN:-1}"
COALESCE_REPAIR_ARGS=()
COALESCE_PARTIAL_ARGS=()
# Organized media remains the broad local reference set.  MetaDaily and
# RedditDaily are read-only external exemplars and are limited to identities
# explicitly confirmed by the shared MD registry or PicOrg face markers.
IDENTITY_REGISTRY="${PICORG_IDENTITY_REGISTRY:-/opt/shared/identity_aliases.json}"
CONFIRMED_IDENTITIES_FILES="${PICORG_CONFIRMED_IDENTITIES_FILES:-$ROOT_DIR/identity_face_markers.json:$IDENTITY_REGISTRY}"
COALESCE_CONFIRMED_ARGS=(--confirmed-only-external)
IFS=: read -r -a confirmed_identity_files <<< "$CONFIRMED_IDENTITIES_FILES"
for confirmed_identity_file in "${confirmed_identity_files[@]}"; do
    [[ -n "$confirmed_identity_file" ]] && COALESCE_CONFIRMED_ARGS+=(--confirmed-identities-file "$confirmed_identity_file")
done
if [[ "$ALLOW_DEGRADED_REBUILD" == "1" ]]; then
    COALESCE_PARTIAL_ARGS+=(--allow-partial-roots)
fi
COALESCE_SKIP_ROOT_ARGS=()
if [[ -n "${PICORG_COALESCE_SKIP_ROOTS:-}" ]]; then
    IFS=: read -r -a configured_skip_roots <<< "$PICORG_COALESCE_SKIP_ROOTS"
    for skip_root in "${configured_skip_roots[@]}"; do
        if [[ -n "$skip_root" ]]; then
            COALESCE_SKIP_ROOT_ARGS+=(--skip-root "$skip_root")
        fi
    done
fi
if [[ "${PICORG_REPAIR_HTML:-1}" == "1" ]]; then
    COALESCE_REPAIR_ARGS+=(
        --repair-html
        --repair-search-root "$SORTED_ROOT"
        --repair-search-root "$METADAILY_ROOT"
        --repair-search-root "$REDDITDAILY_ROOT"
        --repair-quarantine "$ROOT_DIR/.cache/picorg/repair-quarantine"
        --repair-ledger "$ROOT_DIR/.cache/picorg/repair_ledger.jsonl"
        --repair-output-root "$ROOT_DIR/.cache/picorg/repair-staging-$(date -u +%Y%m%dT%H%M%SZ)-$$"
        --repair-run-id "$REPAIR_RUN_ID"
        --repair-scan-cache "$REPAIR_SCAN_CACHE"
        --root-timeout "$COALESCE_ROOT_TIMEOUT"
    )
    if [[ "${PICORG_REPAIR_CORRUPT:-1}" == "1" ]]; then
        COALESCE_REPAIR_ARGS+=(--repair-corrupt)
    fi
fi
LOCK_FILE="${LOCK_FILE:-/tmp/picorg-face-rebuild.lock}"

exec 8>"$LOCK_FILE"
if ! flock -n 8; then
    echo "error: another face reference/database rebuild is already running (lock: $LOCK_FILE)" >&2
    exit 1
fi

if [[ "$FACE_DATABASE_BACKEND" == "photo_reorg" ]] && pgrep -f '/opt/photo_reorg/rebuild_face_database\.py([[:space:]]|$)' >/dev/null; then
    # Match the known photo_reorg builder path only; a broad basename search
    # can mistake an operator's inspection command for an active rebuild.
    echo "error: a face database rebuild is already running; stop it before restarting" >&2
    exit 1
fi
# Root existence/readability is checked inside coalesce_face_references.py's
# bounded watchdog; do not stat a degraded FUSE mount from the shell first.
if [[ "$FACE_DATABASE_BACKEND" == "photo_reorg" ]]; then
    # face_recognition_models imports pkg_resources and can otherwise call
    # quit() successfully, leaving an empty database with exit status 0.
    if ! /opt/photo_reorg/venv/bin/python - <<'PY'
try:
    import face_recognition
except BaseException as exc:
    print(f"face_recognition dependency unavailable: {type(exc).__name__}: {exc}")
    raise SystemExit(1)
print("face_recognition dependency ready")
PY
    then
        echo "error: photo_reorg face-recognition dependencies are unavailable; install setuptools<81 in /opt/photo_reorg/venv" >&2
        exit 2
    fi
fi

echo "[1/5] resetting face database (backup: $BACKUP)"
mkdir -p -- "$(dirname "$FACE_DB")"
DB_EXISTED=0
if [[ -e "$FACE_DB" ]]; then
    DB_EXISTED=1
    if [[ "$FACE_DATABASE_BACKEND" == "picorg" ]]; then
        # The native builder writes a temporary SQLite file and promotes it
        # atomically after validation. Keep the live DB readable if extraction
        # is interrupted or the host runs out of memory.
        echo "preserving existing native database until replacement validates"
    else
        # photo_reorg clears its own tables when extraction starts. Keep the
        # validated live DB intact through coalescing so a hung FUSE root or a
        # killed wrapper cannot leave the active database empty.
        .venv/bin/python verify_face_database.py \
            --db "$FACE_DB" \
            --min-faces "$MIN_FACE_ENCODINGS" \
            --min-identities "$MIN_FACE_IDENTITIES" \
            --allow-generic
        atomic_copy "$FACE_DB" "$BACKUP"
        echo "preserving validated photo_reorg database until extraction (backup: $BACKUP)"
    fi
else
    echo "no existing database; photo_reorg will create a fresh one"
fi
restore_on_failure() {
    local status=$?
    if ((status != 0)) && ((DB_EXISTED)) && [[ -s "$BACKUP" ]]; then
        atomic_copy "$BACKUP" "$FACE_DB"
        echo "restored previous face database after failed rebuild: $FACE_DB" >&2
    fi
    return "$status"
}
trap restore_on_failure EXIT
echo "[1.5/5] syncing durable identity evidence store: $IDENTITY_STORE"
if [[ -e "$REFERENCE_ROOT" ]]; then
    if [[ ! -f "$REFERENCE_ROOT/.picorg-managed" &&
          "$REFERENCE_ROOT" != "${TMPDIR:-/tmp}"/picorg-face-references-* ]]; then
        echo "error: refusing to remove unmarked reference directory: $REFERENCE_ROOT" >&2
        exit 3
    fi
    rm -rf -- "$REFERENCE_ROOT"
fi
.venv/bin/python identity_evidence_store.py \
    --db "$IDENTITY_STORE" \
    --md-registry "$IDENTITY_REGISTRY" \
    --markers "$ROOT_DIR/identity_face_markers.json" \
    --assignments "$ROOT_DIR/review_image_decisions.json"
echo "[2/5] coalescing canonical references"
.venv/bin/python coalesce_face_references.py \
    --sorted-root "$SORTED_ROOT" \
    --metadaily-root "$METADAILY_ROOT" \
    --redditdaily-root "$REDDITDAILY_ROOT" \
    --output "$REFERENCE_ROOT" \
    "${COALESCE_REPAIR_ARGS[@]}" \
    "${COALESCE_PARTIAL_ARGS[@]}" \
    "${COALESCE_CONFIRMED_ARGS[@]}" \
    --confirmed-identities-db "$IDENTITY_STORE" \
    "${COALESCE_SKIP_ROOT_ARGS[@]}" \
    "${COALESCE_SKIP_ARGS[@]}"
touch -- "$REFERENCE_ROOT/.picorg-managed"
if [[ "$ALLOW_DEGRADED_REBUILD" == "1" && -s "$REFERENCE_ROOT/.coalesce-report.json" ]]; then
    echo "warning: degraded rebuild enabled; database will exclude timed-out/unavailable roots" >&2
fi
echo "[3/5] rebuilding $FACE_DATABASE_BACKEND face database"
# Count the flat canonical identity folders once after coalescing. The count
# is local to the generated reference root, so it does not repeatedly touch
# the source volumes during the long face extraction stage.
FACE_FOLDER_TOTAL="$(find "$REFERENCE_ROOT" -mindepth 1 -maxdepth 1 -type d ! -name '.*' -print 2>/dev/null | wc -l | tr -d '[:space:]')"
if [[ ! "$FACE_FOLDER_TOTAL" =~ ^[0-9]+$ ]] || ((FACE_FOLDER_TOTAL == 0)); then
    echo "error: no canonical identity folders found under $REFERENCE_ROOT" >&2
    exit 2
fi
echo "[face-rebuild] folder 0/$FACE_FOLDER_TOTAL: starting"
# Never allow a stale or truncated report from an earlier run to validate a
# new rebuild.  photo_reorg can emit its own error and still return success;
# the report gate below must therefore see only this invocation's output.
rm -f -- "$REBUILD_REPORT"
if [[ "$FACE_DATABASE_BACKEND" == "picorg" ]]; then
    .venv/bin/python picorg_face_database.py \
        --reference-root "$REFERENCE_ROOT" \
        --output "$FACE_DB" \
        --workers "${PICORG_FACE_WORKERS:-2}" \
        --hash-workers "${PICORG_FACE_HASH_WORKERS:-2}" \
        --hash-cache "$HASH_CACHE" \
        --cache "$FACE_CACHE" \
        --backup "$BACKUP" >"$REBUILD_REPORT"
elif [[ "$FACE_DATABASE_BACKEND" == "photo_reorg" ]]; then
    # Prefix every streamed photo_reorg line with the current bounded folder
    # counter. This keeps progress visible even when the terminal scrolls, while
    # the external process remains the source of truth for exit/report status.
    set +e
    PYTHONUNBUFFERED=1 /opt/photo_reorg/venv/bin/python /opt/photo_reorg/rebuild_face_database.py \
        --config /opt/photo_reorg/config_enhanced_accurate.json \
        --source-dirs "$REFERENCE_ROOT" \
        --export-report "$REBUILD_REPORT" 2>&1 | \
        awk -v total="$FACE_FOLDER_TOTAL" '
            /Processing person:/ {
                count += 1
                person = $0
                sub(/^.*Processing person:[[:space:]]*/, "", person)
            }
            { printf "[face-rebuild] folder %d/%d: %s | %s\n", count, total, person, $0; fflush() }
        '
    photo_reorg_status=${PIPESTATUS[0]}
    set -e
    if ((photo_reorg_status != 0)); then
        exit "$photo_reorg_status"
    fi
else
    echo "error: FACE_DATABASE_BACKEND must be photo_reorg or picorg" >&2
    exit 2
fi
if [[ -s "$REBUILD_REPORT" ]]; then
    .venv/bin/python - "$REBUILD_REPORT" "$ALLOW_FACE_ERRORS" <<'PY'
import json, sys
from pathlib import Path
p = Path(sys.argv[1])
allow_face_errors = sys.argv[2] == "1"
d = json.loads(p.read_text(encoding="utf-8"))
stats = d.get("stats") or d.get("rebuild_stats") or d
keys = ("total_images", "processed_images", "faces_extracted", "errors", "persons_created")
print("rebuild report: " + ", ".join(f"{k}={stats.get(k, 'n/a')}" for k in keys))
errors = int(stats.get("errors", 0) or 0)
if not stats.get("end_time"):
    raise SystemExit("rebuild report is incomplete or contains errors; refusing partial database")
if errors and not allow_face_errors:
    raise SystemExit("rebuild report contains image errors; refusing partial database")
if errors:
    print(f"warning: retaining database with {errors} unreadable image error(s) in degraded mode", file=sys.stderr)
PY
elif [[ "$FACE_DATABASE_BACKEND" == "photo_reorg" ]]; then
    echo "error: photo_reorg did not produce a rebuild report; refusing unvalidated database" >&2
    exit 2
fi
echo "[4/5] validating rebuilt face database"
.venv/bin/python verify_face_database.py \
    --db "$FACE_DB" \
    --min-faces "$MIN_FACE_ENCODINGS" \
    --min-identities "$MIN_FACE_IDENTITIES"
echo "[5/5] writing quality-diverse exemplar manifest"
.venv/bin/python select_reference_gallery.py \
    --db "$FACE_DB" \
    --max-per-person "$MAX_EXEMPLARS_PER_IDENTITY" \
    --min-quality "$MIN_EXEMPLAR_QUALITY" \
    --markers "$ROOT_DIR/identity_face_markers.json" \
    --output "$EXEMPLAR_MANIFEST"
echo "face database rebuild complete"
