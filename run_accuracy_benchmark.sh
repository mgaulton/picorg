#!/usr/bin/env bash
set -Eeuo pipefail

# Review-only accuracy calibration. This intentionally never moves files,
# changes the face database, or writes review decisions.
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-$ROOT_DIR/.venv/bin/python}"
DECISIONS="${DECISIONS:-$ROOT_DIR/review_decisions.json}"
IMAGE_DECISIONS="${IMAGE_DECISIONS:-$ROOT_DIR/review_image_decisions.json}"
EMBEDDINGS="${EMBEDDINGS:-$ROOT_DIR/.cache/picorg/face-embeddings-native.json}"
PAIR_FILE="${PAIR_FILE:-$ROOT_DIR/.cache/picorg/image-face-labelled-pairs.json}"
TRAIN_FILE="${TRAIN_FILE:-$ROOT_DIR/.cache/picorg/image-face-train.json}"
HELDOUT_FILE="${HELDOUT_FILE:-$ROOT_DIR/.cache/picorg/image-face-heldout.json}"
REPORT="${REPORT:-$ROOT_DIR/.cache/picorg/image-face-calibration.json}"
FRACTION="${FRACTION:-0.20}"
SEED="${SEED:-picorg-image-benchmark-v1}"
MAX_FMR="${MAX_FMR:-0.001}"

for required in "$PYTHON" "$DECISIONS" "$EMBEDDINGS"; do
  if [[ ! -f "$required" ]]; then
    echo "error: required file not found: $required" >&2
    exit 2
  fi
done
if [[ ! -f "$IMAGE_DECISIONS" ]]; then
  echo "warning: $IMAGE_DECISIONS not found; using cluster confirmations" >&2
  IMAGE_DECISIONS=""
fi
mkdir -p "$(dirname "$PAIR_FILE")" "$(dirname "$TRAIN_FILE")" "$(dirname "$HELDOUT_FILE")" "$(dirname "$REPORT")"

pair_args=(--decisions "$DECISIONS" --embeddings "$EMBEDDINGS" --output "$PAIR_FILE")
if [[ -n "$IMAGE_DECISIONS" ]]; then
  pair_args+=(--image-decisions "$IMAGE_DECISIONS")
fi
echo "[1/3] building confirmed face pairs"
"$PYTHON" "$ROOT_DIR/build_face_pairs.py" "${pair_args[@]}"
echo "[2/3] creating image-disjoint held-out split"
"$PYTHON" "$ROOT_DIR/split_face_pairs.py" --pairs "$PAIR_FILE" \
  --train-output "$TRAIN_FILE" --heldout-output "$HELDOUT_FILE" \
  --fraction "$FRACTION" --seed "$SEED"
echo "[3/3] calibrating held-out operating point (review-only)"
"$PYTHON" "$ROOT_DIR/face_match_benchmark.py" --pairs "$HELDOUT_FILE" \
  --embeddings "$EMBEDDINGS" --max-fmr "$MAX_FMR" --output "$REPORT"

"$PYTHON" - "$REPORT" <<'PY'
import json
import sys
from pathlib import Path

report = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
selected = report.get("selected") or {}
if not selected:
    print(
        "benchmark failed: status={} pairs={} genuine={} impostor={}".format(
            report.get("calibration_status", "no_selected_point"),
            report.get("pair_count", 0),
            report.get("genuine_pairs", 0),
            report.get("impostor_pairs", 0),
        )
    )
    print(f"report: {sys.argv[1]}")
    raise SystemExit(1)
print(
    "benchmark summary: pairs={pairs} genuine={genuine} impostor={impostor} "
    "threshold={threshold:.4f} FMR={fmr:.4%} FNMR={fnmr:.4%}".format(
        pairs=report.get("pair_count", 0),
        genuine=report.get("genuine_pairs", 0),
        impostor=report.get("impostor_pairs", 0),
        threshold=float(selected.get("threshold", 0.0)),
        fmr=float(selected.get("fmr", 0.0)),
        fnmr=float(selected.get("fnmr", 0.0)),
    )
)
print(f"report: {sys.argv[1]}")
PY
