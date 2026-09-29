#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

# Face extraction and reconciliation status must stream through the log tee.
export PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}"

AUDIT_ROOT="${AUDIT_ROOT:-$ROOT_DIR/.cache/picorg/audits}"
USE_EXISTING_FACE_AUDIT="${USE_EXISTING_FACE_AUDIT:-0}"
CURRENT_RUN_POINTER="${PICORG_CURRENT_RUN_POINTER:-$ROOT_DIR/.cache/picorg/current-run.json}"
CURRENT_POINTER_USED=0
if [[ -z "${AUDIT:-}" ]]; then
    if [[ -s "$CURRENT_RUN_POINTER" ]]; then
        CURRENT_AUDIT="$(.venv/bin/python run_artifact_store.py resolve --pointer "$CURRENT_RUN_POINTER" 2>/dev/null || true)"
        if [[ -n "$CURRENT_AUDIT" && -s "$CURRENT_AUDIT" ]]; then
            AUDIT="$CURRENT_AUDIT"
            CURRENT_POINTER_USED=1
            echo "using published current run: $AUDIT"
        fi
    fi
fi
if [[ -z "${AUDIT:-}" ]]; then
    AUDIT_CANDIDATES="$(find "$AUDIT_ROOT" -maxdepth 1 -type f -name '*.json' \
        ! -name '*.preflight.json' ! -name '*.face-clusters.json' \
        ! -name '*.cluster-purity*.json' ! -name '*.face-cluster-purity.json' ! -name '*.reconciled*.json' \
        ! -name '*.run-manifest.json' ! -name '*.identity-candidates.json' \
        ! -name '*.face-matches.json' ! -name '*.identity-first*.json' \
        -printf '%T@ %p\n' 2>/dev/null | sort -nr)"
    if [[ "$USE_EXISTING_FACE_AUDIT" == "1" ]]; then
        while read -r _ candidate; do
            [[ -s "${candidate%.json}.face-clusters.json" ]] && { AUDIT="$candidate"; break; }
        done <<< "$AUDIT_CANDIDATES"
    else
        AUDIT="$(sed -n '1s/^[^ ]* //p' <<< "$AUDIT_CANDIDATES")"
    fi
    AUDIT="${AUDIT:-/tmp/picorg_sorted_audit/20260731T170645Z.json}"
fi
if ((CURRENT_POINTER_USED)) && [[ -z "${FACE_AUDIT:-}" ]]; then
    FACE_AUDIT="$(.venv/bin/python run_artifact_store.py resolve --pointer "$CURRENT_RUN_POINTER" --field face-audit 2>/dev/null || true)"
fi
FACE_AUDIT="${FACE_AUDIT:-${AUDIT%.json}.face-clusters.json}"
if ((CURRENT_POINTER_USED)) && [[ -z "${RECONCILED_AUDIT:-}" ]]; then
    RECONCILED_AUDIT="$(.venv/bin/python run_artifact_store.py resolve --pointer "$CURRENT_RUN_POINTER" --field reconciled 2>/dev/null || true)"
fi
RECONCILED_AUDIT="${RECONCILED_AUDIT:-${AUDIT%.json}.reconciled.json}"
IDENTITY_MATCH_OUTPUT="${IDENTITY_MATCH_OUTPUT:-${AUDIT%.json}.identity-candidates.json}"
CACHE_ROOT="${CACHE_ROOT:-$ROOT_DIR/.cache/picorg}"
mkdir -p "$CACHE_ROOT"
FACE_CACHE="${FACE_CACHE:-$CACHE_ROOT/$(basename "${AUDIT%.json}").face-embeddings.json}"
FACE_BACKEND="${FACE_BACKEND:-dlib}"
# Review clustering remains separate from identity assignment, but the default
# is conservative enough to avoid giant mixed-person groups. Raise explicitly
# only for an exploratory review run; it never changes move/match gates.
FACE_CLUSTER_THRESHOLD="${FACE_CLUSTER_THRESHOLD:-0.52}"
# Face clusters use an explicit cosine-similarity floor.  This is separate
# from the legacy Euclidean threshold and is deliberately conservative.
FACE_CLUSTER_SIMILARITY="${FACE_CLUSTER_SIMILARITY:-0.90}"
FACE_CLUSTER_MAX_REPRESENTATIVES="${FACE_CLUSTER_MAX_REPRESENTATIVES:-12}"
FACE_CLUSTER_STRICT_ALL_MEMBERS="${FACE_CLUSTER_STRICT_ALL_MEMBERS:-1}"
LEGACY_FACE_CACHE="${AUDIT%.json}.face-embeddings.json"
PREFLIGHT="${PREFLIGHT:-${AUDIT%.json}.preflight.json}"
REUSE_EXISTING_PREFLIGHT="${REUSE_EXISTING_PREFLIGHT:-$USE_EXISTING_FACE_AUDIT}"
PREFLIGHT_TIMEOUT="${PICORG_PREFLIGHT_TIMEOUT:-1800}"
if [[ ! "$PREFLIGHT_TIMEOUT" =~ ^[0-9]+$ ]]; then
    echo "error: PICORG_PREFLIGHT_TIMEOUT must be a non-negative integer (seconds)" >&2
    exit 2
fi
if [[ -z "${SKIP_PATHS:-}" ]]; then
    SKIP_PATHS="$CACHE_ROOT/skip_paths.json"
fi
DECISIONS="${DECISIONS:-$ROOT_DIR/review_decisions.json}"
IMAGE_DECISIONS="${IMAGE_DECISIONS:-$ROOT_DIR/review_image_decisions.json}"
REVIEW_LEDGER="${REVIEW_LEDGER:-$ROOT_DIR/review_decision_ledger.jsonl}"
REVIEW_IDENTITIES="${REVIEW_IDENTITIES:-$ROOT_DIR/review_identities.json}"
EVIDENCE_DB="${PICORG_EVIDENCE_DB:-$CACHE_ROOT/identity_evidence.sqlite3}"
HOST="${PICORG_UI_HOST:-${HOST:-0.0.0.0}}"
# Default 8787 matches /opt/service_configurations.json (Readarr retired; port reclaimed).
PORT="${PORT:-8787}"
UI_SERVER="${PICORG_UI_SERVER:-waitress}"

# Shell environments on this host commonly export HOST=server6, but that name
# is not guaranteed to resolve. Fall back to the LAN wildcard unless the
# operator supplied the explicit PICORG_UI_HOST override.
if [[ -z "${PICORG_UI_HOST:-}" ]]; then
    if [[ "$HOST" == "$(hostname)" || "$HOST" == "server6" ]] || { [[ "$HOST" != "0.0.0.0" && "$HOST" != "127.0.0.1" && "$HOST" != "localhost" ]] && ! getent ahostsv4 "$HOST" >/dev/null 2>&1; }; then
        echo "warning: HOST=$HOST is not a usable bind address; binding LAN UI to 0.0.0.0"
        HOST="0.0.0.0"
    fi
fi

REQUESTED_PORT="$PORT"
PORT="$("$ROOT_DIR/picorg_resolve_port.sh" "$REQUESTED_PORT")"
if [[ "$PORT" != "$REQUESTED_PORT" ]]; then
    echo "warning: PORT=$REQUESTED_PORT unavailable; using open port $PORT"
fi

LOCK_FILE="${LOCK_FILE:-/tmp/picorg-runweb.lock}"
export PICORG_UI_PIPELINE_LOCK="${PICORG_UI_PIPELINE_LOCK:-${PICORG_PIPELINE_LOCK:-/tmp/picorg-pipeline.lock}}"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "another runweb.sh instance is already active (lock: $LOCK_FILE)" >&2
    exit 1
fi
LOG_ROOT="${LOG_ROOT:-$ROOT_DIR/.cache/picorg/logs}"
mkdir -p "$LOG_ROOT"
LOG_FILE="${LOG_FILE:-$LOG_ROOT/runweb-$(date -u +%Y%m%dT%H%M%SZ).log}"
exec > >(tee -a "$LOG_FILE") 2>&1

if [[ "$UI_SERVER" == "waitress" ]] && ! .venv/bin/python -c 'import waitress' >/dev/null 2>&1; then
    echo "error: Waitress is required for the production UI; install requirements-review.txt or set PICORG_UI_SERVER=flask for local development" >&2
    exit 2
fi

if [[ ! -f "$AUDIT" ]]; then
    echo "error: audit not found: $AUDIT" >&2
    exit 2
fi

if [[ ! -s "$FACE_CACHE" && -s "$LEGACY_FACE_CACHE" ]]; then
    cp -p -- "$LEGACY_FACE_CACHE" "$FACE_CACHE"
    echo "migrated embedding cache to $FACE_CACHE"
fi

if [[ "$REUSE_EXISTING_PREFLIGHT" == "1" && -s "$PREFLIGHT" ]]; then
    echo "[1/5] reusing existing preflight report: $PREFLIGHT"
else
    echo "[1/5] classifying unmatched media inputs"
    run_preflight() {
        local -a preflight_command=(
            .venv/bin/python media_preflight.py "$AUDIT"
            --output "$PREFLIGHT"
            --verify-images
            --skip-paths "$SKIP_PATHS"
        )
        if (( PREFLIGHT_TIMEOUT == 0 )); then
            "${preflight_command[@]}"
            return
        fi
        if ! command -v timeout >/dev/null 2>&1; then
            echo "error: timeout(1) is required for bounded preflight scans; set PICORG_PREFLIGHT_TIMEOUT=0 only for diagnostics" >&2
            return 127
        fi
        timeout --foreground --kill-after=10s "${PREFLIGHT_TIMEOUT}s" "${preflight_command[@]}"
    }
    if run_preflight; then
        :
    else
        preflight_status=$?
        if (( preflight_status == 124 )); then
            echo "error: media preflight timed out after ${PREFLIGHT_TIMEOUT}s; use the existing-audit path or repair the source mount" >&2
        else
            echo "error: media preflight failed (exit ${preflight_status})" >&2
        fi
        exit "$preflight_status"
    fi
fi

echo "[2/5] validating face-matching dependency"
if [[ "$USE_EXISTING_FACE_AUDIT" == "1" ]]; then
    if [[ ! -s "$FACE_AUDIT" ]]; then
        echo "error: USE_EXISTING_FACE_AUDIT=1 but face audit is missing: $FACE_AUDIT" >&2
        exit 2
    fi
    echo "reusing existing face audit: $FACE_AUDIT"
elif ! .venv/bin/python -c 'import face_recognition' >/dev/null 2>&1; then
    if [[ "${INSTALL_FACE_DEPS:-0}" == "1" ]]; then
        .venv/bin/pip install -r requirements-face.txt
    else
        echo "error: face_recognition is not installed" >&2
        echo "run INSTALL_FACE_DEPS=1 $0 once, or install requirements-face.txt manually" >&2
        exit 2
    fi
fi

# When a fresh face-to-identity pass is available, exclude its confident
# matches from the generic clustering input. This makes the execution order
# real (identity first, generic grouping second), rather than merely hiding
# known matches after a broad cluster was built.
FACE_CLUSTER_INPUT="$AUDIT"
IDENTITY_MATCH_FRESH=0
if [[ -s "$IDENTITY_MATCH_OUTPUT" && "$IDENTITY_MATCH_OUTPUT" -nt "$AUDIT" ]]; then
    IDENTITY_MATCH_FRESH=1
fi

if [[ "$USE_EXISTING_FACE_AUDIT" != "1" && (! -s "$FACE_AUDIT" || "${FORCE_FACE_REBUILD:-0}" == "1") ]]; then
    echo "[3/5] building face clusters from $AUDIT (large collections may take time)"
    if [[ "${FORCE_FACE_REBUILD:-0}" == "1" ]]; then
        rm -f -- "$FACE_AUDIT"
        if [[ "${FORCE_FACE_CACHE_RESET:-0}" == "1" ]]; then
            rm -f -- "$FACE_CACHE"
        fi
    fi
    if [[ "$IDENTITY_MATCH_FRESH" == "1" ]]; then
        FACE_CLUSTER_INPUT="${AUDIT%.json}.identity-first-input.json"
        .venv/bin/python - "$AUDIT" "$IDENTITY_MATCH_OUTPUT" "$FACE_CLUSTER_INPUT" <<'PY'
import json
import sys
import tempfile
from pathlib import Path

audit_path, matches_path, output_path = map(Path, sys.argv[1:])
audit = json.loads(audit_path.read_text(encoding="utf-8"))
matches = json.loads(matches_path.read_text(encoding="utf-8"))
matched = {
    str(row.get("path"))
    for row in matches.get("results", [])
    if isinstance(row, dict)
    and row.get("status") == "matched"
    and row.get("path")
}
results = [
    row for row in audit.get("results", [])
    if isinstance(row, dict) and str(row.get("path") or "") not in matched
]
payload = dict(audit)
payload["results"] = results
payload["identity_first_excluded"] = len(matched)
payload["identity_match_audit"] = str(matches_path)
output_path.parent.mkdir(parents=True, exist_ok=True)
with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=output_path.parent, delete=False) as handle:
    json.dump(payload, handle, indent=2, sort_keys=True)
    handle.write("\n")
    temporary = Path(handle.name)
temporary.replace(output_path)
print(f"identity-first input: excluded={len(matched)} remaining={len(results)}")
PY
    fi
    FACE_COMMAND=(
        .venv/bin/python face_cluster_unmatched.py
        --audit "$FACE_CLUSTER_INPUT"
        --preflight "$PREFLIGHT"
        --output "$FACE_AUDIT"
        --cache "$FACE_CACHE"
        --backend "$FACE_BACKEND"
        --threshold "$FACE_CLUSTER_THRESHOLD"
        --min-similarity "$FACE_CLUSTER_SIMILARITY"
        --max-representatives "$FACE_CLUSTER_MAX_REPRESENTATIVES"
        --workers "${PICORG_FACE_WORKERS:-2}"
    )
    if [[ "$FACE_CLUSTER_STRICT_ALL_MEMBERS" == "1" ]]; then
        FACE_COMMAND+=(--strict-all-members)
        echo "review face-cluster rule: cosine similarity >= $FACE_CLUSTER_SIMILARITY against every cluster member (representative cap $FACE_CLUSTER_MAX_REPRESENTATIVES; legacy distance threshold $FACE_CLUSTER_THRESHOLD; identity assignment unchanged)"
    else
        echo "review face-cluster rule: cosine similarity >= $FACE_CLUSTER_SIMILARITY with $FACE_CLUSTER_MAX_REPRESENTATIVES representatives (strict all-member check disabled; identity assignment unchanged)"
    fi
    "${FACE_COMMAND[@]}"
fi

# Promote confident face-identity candidates before generic clustering.  The
# composite remains review-only; it only prevents known matches from being
# re-presented as unknown descriptive groups.
if [[ "$IDENTITY_MATCH_FRESH" == "1" && -s "$FACE_AUDIT" ]]; then
    # Anchor the derived artifact to the primary audit. The current-run
    # pointer may already reference an identity-first face audit; deriving
    # from FACE_AUDIT would append this suffix on every service restart.
    IDENTITY_FIRST_FACE_AUDIT="${AUDIT%.json}.face-clusters.identity-first.json"
    .venv/bin/python compose_identity_first_audit.py \
        --primary "$AUDIT" \
        --face "$FACE_AUDIT" \
        --identity-matches "$IDENTITY_MATCH_OUTPUT" \
        --output "$IDENTITY_FIRST_FACE_AUDIT"
    FACE_AUDIT="$IDENTITY_FIRST_FACE_AUDIT"
fi

# Existing face audits are intentionally reusable, but make their operating
# point and coverage visible. A report built at a looser threshold can contain
# very large mixed-person groups; it must be rebuilt before treating the UI as
# an accuracy pass. This is a warning rather than an automatic mutation so an
# operator can still inspect a historical audit deliberately.
.venv/bin/python - "$AUDIT" "$FACE_AUDIT" "$FACE_CLUSTER_THRESHOLD" "$FACE_CLUSTER_SIMILARITY" "$FACE_CLUSTER_STRICT_ALL_MEMBERS" <<'PY'
import json
import os
import sys
from collections import Counter

audit_path, path, requested, requested_similarity, requested_strict = sys.argv[1], sys.argv[2], float(sys.argv[3]), float(sys.argv[4]), sys.argv[5] == "1"
try:
    payload = json.load(open(path, encoding="utf-8"))
    report = payload.get("report") or {}
    threshold = payload.get("threshold")
    min_similarity = payload.get("min_similarity")
    strict_all_members = bool(payload.get("strict_all_members", False))
    results = payload.get("results") or []
    counts = Counter(str(row.get("face_cluster_id") or "") for row in results if isinstance(row, dict))
    largest = max(counts.values(), default=0)
    if threshold is not None and abs(float(threshold) - requested) > 1e-9:
        print(f"warning: face audit threshold={threshold} differs from configured threshold={requested}; rebuild for a comparable accuracy run")
    if min_similarity is not None and abs(float(min_similarity) - requested_similarity) > 1e-9:
        print(f"warning: face audit similarity floor={min_similarity} differs from configured floor={requested_similarity}; rebuild for a comparable accuracy run")
    if strict_all_members != requested_strict:
        print(f"warning: face audit strict_all_members={strict_all_members} differs from configured value={requested_strict}; rebuild for a comparable accuracy run")
    if os.path.getmtime(audit_path) > os.path.getmtime(path):
        print("warning: primary name audit is newer than the face audit; clusters may reference moved/new files and should be rebuilt")
    if largest > 100:
        print(f"warning: face audit contains a cluster of {largest} images; keep image-level review and rebuild with a stricter threshold before bulk actions")
    if int(report.get("errors") or 0) or int(report.get("multi_face_deferred") or 0):
        print("warning: face audit has deferred/error inputs; coverage is incomplete and cannot establish cluster accuracy")
except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
    print(f"warning: could not inspect face-audit metadata: {exc}")
PY

echo "[4/5] building face-only review clusters (names are context only)"
if [[ "$USE_EXISTING_FACE_AUDIT" == "1" && -s "$RECONCILED_AUDIT" && "$RECONCILED_AUDIT" -nt "$AUDIT" && "$RECONCILED_AUDIT" -nt "$FACE_AUDIT" ]] \
    && .venv/bin/python -c 'import json,sys; p=json.load(open(sys.argv[1], encoding="utf-8")); raise SystemExit(0 if p.get("schema_version") == 2 and p.get("cluster_policy") == "face-only" else 1)' "$RECONCILED_AUDIT"; then
    echo "reusing existing reconciled audit: $RECONCILED_AUDIT"
else
    .venv/bin/python reconcile_review_clusters.py \
        --name-audit "$AUDIT" \
        --face-audit "$FACE_AUDIT" \
        --output "$RECONCILED_AUDIT"
fi

echo "[5/5] starting LAN review UI at http://${HOST}:${PORT}/"
if [[ "${PICORG_UI_AUTH:-0}" == "1" && -z "${PICORG_UI_TOKEN:-}" ]]; then
    echo "error: PICORG_UI_AUTH=1 requires PICORG_UI_TOKEN" >&2
    exit 2
fi
if [[ "${PICORG_UI_ALLOW_UNAUTH_WRITES:-0}" == "1" && "$UI_SERVER" != "flask" ]]; then
    echo "error: PICORG_UI_ALLOW_UNAUTH_WRITES=1 is permitted only with the explicit Flask development server" >&2
    exit 2
fi
RUN_MANIFEST="${RUN_MANIFEST:-${AUDIT%.json}.run-manifest.json}"
PUBLISH_ARGS=(
    publish
    --pointer "$CURRENT_RUN_POINTER"
    --audit "$AUDIT"
    --face-audit "$FACE_AUDIT"
    --reconciled "$RECONCILED_AUDIT"
)
if [[ -s "$IDENTITY_MATCH_OUTPUT" ]]; then
    PUBLISH_ARGS+=(--identity-matches "$IDENTITY_MATCH_OUTPUT")
fi
if [[ -s "$RUN_MANIFEST" ]]; then
    PUBLISH_ARGS+=(--manifest "$RUN_MANIFEST")
fi
.venv/bin/python run_artifact_store.py "${PUBLISH_ARGS[@]}"
echo "published current run pointer: $CURRENT_RUN_POINTER"
if [[ "${PICORG_UI_START:-1}" != "1" ]]; then
    echo "map artifacts prepared; UI launch suppressed"
    exit 0
fi
if [[ -n "${PICORG_UI_TOKEN:-}" ]]; then
    if [[ "${PICORG_UI_AUTH:-0}" == "1" ]]; then
        echo "UI access requires the configured token (PICORG_UI_AUTH=1)"
    else
        echo "LAN UI access is unrestricted; non-LAN clients require the configured token"
    fi
else
    echo "LAN UI access is unrestricted; non-LAN clients are denied (set PICORG_UI_TOKEN for remote access)"
fi
# 0.0.0.0 is the bind address, not a browser URL.  Publish a concrete LAN
# address so operators do not accidentally open an unrelated service on the
# requested port when the resolver selected a fallback port.
LAN_UI_HOST="${PICORG_UI_LAN_HOST:-}"
if [[ -z "$LAN_UI_HOST" ]]; then
    LAN_UI_HOST="$(hostname -I 2>/dev/null | awk '{for (i=1; i<=NF; i++) if ($i ~ /^192\.168\./) {print $i; exit} if (NF) print $1}')"
fi
LAN_UI_HOST="${LAN_UI_HOST:-127.0.0.1}"
echo "LAN UI URL: http://${LAN_UI_HOST}:${PORT}/"
UI_ARGS=( \
    --audit "$RECONCILED_AUDIT" \
    --decisions "$DECISIONS" \
    --image-decisions "$IMAGE_DECISIONS" \
    --review-identities "$REVIEW_IDENTITIES" \
    --review-ledger "$REVIEW_LEDGER" \
    --evidence-db "$EVIDENCE_DB" \
    --host "$HOST" \
    --port "$PORT" \
    --server "$UI_SERVER" \
)
exec .venv/bin/python review_ui.py "${UI_ARGS[@]}"
