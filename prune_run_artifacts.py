#!/usr/bin/env python3
"""Conservatively prune superseded PicOrg JSON run snapshots.

Only runs with a generated ``*.run-manifest.json`` are candidates.  Legacy
audits without manifests are never removed.  The default is a dry run;
``--apply`` is required for deletion, and the current pointer plus runs whose
IDs appear in review ledgers are always protected.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Iterable

from run_artifact_store import resolve_current


def _run_id(manifest: Path) -> str:
    suffix = ".run-manifest.json"
    return manifest.name[: -len(suffix)]


def discover_runs(root: Path) -> list[tuple[str, Path, list[Path]]]:
    runs: list[tuple[str, Path, list[Path]]] = []
    for manifest in root.glob("*.run-manifest.json"):
        run_id = _run_id(manifest)
        files = sorted(root.glob(f"{run_id}*"))
        runs.append((run_id, manifest, files))
    return sorted(runs, key=lambda item: item[1].stat().st_mtime, reverse=True)


def _ledger_text(paths: Iterable[Path]) -> str:
    chunks: list[str] = []
    for path in paths:
        try:
            chunks.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return "\n".join(chunks)


def prune(
    root: Path,
    *,
    keep: int = 30,
    pointer: Path | None = None,
    ledgers: Iterable[Path] = (),
    apply: bool = False,
) -> dict[str, object]:
    if keep < 1:
        raise ValueError("keep must be at least 1")
    runs = discover_runs(root)
    current = resolve_current(pointer) if pointer else None
    current_run = current.stem if current else None
    ledger = _ledger_text(ledgers)
    protected: set[str] = {run_id for run_id, _manifest, _files in runs[:keep]}
    if current_run:
        protected.add(current_run)
    for run_id, _manifest, _files in runs:
        if run_id in ledger:
            protected.add(run_id)
    removed: list[str] = []
    candidates: list[str] = []
    for run_id, _manifest, files in runs:
        if run_id in protected:
            continue
        candidates.append(run_id)
        if apply:
            for path in files:
                path.unlink(missing_ok=True)
            removed.append(run_id)
    return {
        "root": str(root),
        "keep": keep,
        "protected": sorted(protected),
        "candidates": candidates,
        "removed": removed,
        "dry_run": not apply,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(".cache/picorg/audits"))
    parser.add_argument("--keep", type=int, default=30)
    parser.add_argument("--pointer", type=Path, default=Path(".cache/picorg/current-run.json"))
    parser.add_argument("--ledger", type=Path, action="append", default=[])
    parser.add_argument("--apply", action="store_true", help="delete eligible artifacts")
    args = parser.parse_args()
    report = prune(args.root, keep=args.keep, pointer=args.pointer, ledgers=args.ledger, apply=args.apply)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
