#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
LOCKED_REQUIREMENTS="$ROOT_DIR/requirements-locked.txt"
UV_BIN="${UV_BIN:-uv}"
UV_CACHE_DIR="${UV_CACHE_DIR:-$ROOT_DIR/.cache/picorg/uv}"

if ! command -v "$UV_BIN" >/dev/null 2>&1; then
  printf 'error: uv is required to verify the dependency lock\n' >&2
  exit 2
fi
if [[ ! -f "$LOCKED_REQUIREMENTS" ]]; then
  printf 'error: missing %s\n' "$LOCKED_REQUIREMENTS" >&2
  exit 2
fi

tmp_requirements="$(mktemp "${TMPDIR:-/tmp}/picorg-requirements-locked.XXXXXX")"
trap 'rm -f "$tmp_requirements"' EXIT

mkdir -p "$UV_CACHE_DIR"
export UV_CACHE_DIR

"$UV_BIN" lock --check
"$UV_BIN" export --frozen --all-extras --format requirements.txt \
  --no-header --no-annotate --output-file "$tmp_requirements" >/dev/null

if ! cmp -s "$LOCKED_REQUIREMENTS" "$tmp_requirements"; then
  printf 'error: requirements-locked.txt is stale; regenerate with:\n' >&2
  printf '  uv export --frozen --all-extras --format requirements.txt --no-header --no-annotate --output-file requirements-locked.txt\n' >&2
  diff -u "$LOCKED_REQUIREMENTS" "$tmp_requirements" | sed -n '1,80p' >&2 || true
  exit 1
fi

printf 'dependency lock verified: uv.lock and requirements-locked.txt are consistent\n'
