#!/usr/bin/env bash
set -Eeuo pipefail

# Review-only experiment suite. It never moves media, changes identities, or
# replaces a face database. Optional experiments are skipped when their input
# artifacts or dependencies are unavailable.
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"
PYTHON="${PYTHON:-$ROOT_DIR/.venv/bin/python}"
AUDIT_ROOT="${AUDIT_ROOT:-$ROOT_DIR/.cache/picorg/audits}"
REPORT_ROOT="${REPORT_ROOT:-$ROOT_DIR/.cache/picorg/ai-experiments}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_DIR="$REPORT_ROOT/$STAMP"

# Do not read a cache/database while a rebuild is replacing it.
if pgrep -f '[f]ace_cluster_unmatched.py|[r]ebuild_face_database.py|[p]icorg_face_database.py' >/dev/null 2>&1; then
    echo "error: a face extraction/database rebuild is active; rerun this experiment suite after it completes" >&2
    exit 2
fi
mkdir -p "$RUN_DIR"

PAIRS="${PAIRS:-$ROOT_DIR/.cache/picorg/image-face-heldout.json}"
EMBEDDINGS="${EMBEDDINGS:-$ROOT_DIR/.cache/picorg/face-embeddings-native.json}"
DB="${DB:-$ROOT_DIR/.cache/picorg/face_database.sqlite3}"
LEGACY_EMBEDDINGS="${LEGACY_EMBEDDINGS:-$ROOT_DIR/.cache/picorg/face-embeddings.json}"
PICORG_EMBEDDINGS="${PICORG_EMBEDDINGS:-$ROOT_DIR/.cache/picorg/face-embeddings-native.json}"
UNIFACE_PYTHON="${UNIFACE_PYTHON:-/tmp/picorg-uniface-venv/bin/python}"

run_step() {
    local name="$1"; shift
    local log="$RUN_DIR/$name.log"
    echo "[$name] $*"
    if "$@" > >(tee "$log") 2>&1; then
        printf '%s\tpassed\t%s\n' "$name" "$log" >> "$RUN_DIR/status.tsv"
    else
        local code=$?
        printf '%s\tfailed:%s\t%s\n' "$name" "$code" "$log" >> "$RUN_DIR/status.tsv"
        echo "[$name] failed with exit $code (continuing optional experiments)" >&2
    fi
}

if [[ -x "$ROOT_DIR/run_accuracy_benchmark.sh" && -s "$PAIRS" && -s "$EMBEDDINGS" ]]; then
    run_step image_calibration env PAIR_FILE="$PAIRS" EMBEDDINGS="$EMBEDDINGS" REPORT="$RUN_DIR/image-calibration.json" "$ROOT_DIR/run_accuracy_benchmark.sh"
else
    echo "[image_calibration] skipped: held-out pairs or embedding cache unavailable"
    printf '%s\t skipped\t\n' image_calibration >> "$RUN_DIR/status.tsv"
fi

if [[ -s "$PAIRS" && -s "$LEGACY_EMBEDDINGS" && -s "$PICORG_EMBEDDINGS" ]]; then
    run_step backend_parity "$PYTHON" "$ROOT_DIR/backend_parity_benchmark.py" \
        --pairs "$PAIRS" --legacy-embeddings "$LEGACY_EMBEDDINGS" \
        --picorg-embeddings "$PICORG_EMBEDDINGS" \
        --output "$RUN_DIR/backend-parity.json"
else
    echo "[backend_parity] skipped: both backend caches and held-out pairs are required"
    printf '%s\t skipped\t\n' backend_parity >> "$RUN_DIR/status.tsv"
fi

if [[ -s "$DB" ]] && "$PYTHON" -c 'import faiss' >/dev/null 2>&1; then
    run_step faiss_identity "$PYTHON" "$ROOT_DIR/faiss_identity_benchmark.py" \
        --db "$DB" --queries "${FAISS_QUERIES:-256}" --top-k "${FAISS_TOP_K:-10}" \
        --output "$RUN_DIR/faiss-identity.json"
else
    echo "[faiss_identity] skipped: database or faiss-cpu is unavailable"
    printf '%s\t skipped\t\n' faiss_identity >> "$RUN_DIR/status.tsv"
fi

if [[ "${RUN_UNIFACE_BENCHMARK:-0}" == "1" && -s "$PAIRS" && -x "$UNIFACE_PYTHON" ]] && "$UNIFACE_PYTHON" -c 'import uniface' >/dev/null 2>&1; then
    uniface_args=(
        --pairs "$PAIRS" --backend adaface --model-license-status research-only
        --output "$RUN_DIR/uniface-adaface.json"
    )
    if [[ -n "${UNIFACE_SEARCH_ROOT:-}" ]]; then
        # Permit moved held-out fixtures to be resolved by unique basename.
        uniface_args+=(--search-root "$UNIFACE_SEARCH_ROOT")
    fi
    run_step uniface_adaface "$UNIFACE_PYTHON" "$ROOT_DIR/uniface_pair_benchmark.py" \
        "${uniface_args[@]}"
else
    echo "[uniface_adaface] disabled or unavailable (set RUN_UNIFACE_BENCHMARK=1 with an isolated UniFace venv)"
    printf '%s\t skipped\t\n' uniface_adaface >> "$RUN_DIR/status.tsv"
fi

if [[ "${RUN_AGENT_REVIEW:-0}" == "1" ]]; then
    if [[ -z "${AGENT_AUDIT:-}" || ! -s "$AGENT_AUDIT" ]]; then
        echo "[agent_review] skipped: set AGENT_AUDIT to a face-cluster audit"
        printf '%s\t skipped\t\n' agent_review >> "$RUN_DIR/status.tsv"
    else
        agent_cmd=("$PYTHON" "$ROOT_DIR/agent_review.py" --audit "$AGENT_AUDIT" --output "$RUN_DIR/agent-review.json" --provider "${AGENT_PROVIDER:-mock}" --model "${AGENT_MODEL:-${OLLAMA_MODEL:-gemma3:4b}}" --limit "${AGENT_LIMIT:-50}" --timeout "${AGENT_TIMEOUT:-30}")
        if [[ -n "${AGENT_ENDPOINT:-}" ]]; then
            agent_cmd+=(--endpoint "$AGENT_ENDPOINT")
        fi
        if [[ "${AGENT_ALLOW_REMOTE:-0}" == "1" ]]; then
            agent_cmd+=(--allow-remote)
        fi
        run_step agent_review "${agent_cmd[@]}"
    fi
else
    echo "[agent_review] disabled (set RUN_AGENT_REVIEW=1 for report-only quality triage)"
    printf '%s\t skipped\t\n' agent_review >> "$RUN_DIR/status.tsv"
fi

printf '%s\n' "--- experiment summary ---"
cat "$RUN_DIR/status.tsv"
echo "results: $RUN_DIR"
