import json
from pathlib import Path

from run_artifact_store import publish_current, resolve_current


def write_artifact(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_publish_and_resolve_current_is_atomic_and_verified(tmp_path: Path) -> None:
    audit = tmp_path / "20260917T120000Z.json"
    face = tmp_path / "20260917T120000Z.face-clusters.json"
    reconciled = tmp_path / "20260917T120000Z.reconciled.json"
    pointer = tmp_path / "current-run.json"
    write_artifact(audit, {"results": []})
    write_artifact(face, {"results": []})
    write_artifact(reconciled, {"schema_version": 2, "cluster_policy": "face-only", "results": []})

    published = publish_current(pointer, audit=audit, face_audit=face, reconciled=reconciled)

    assert published["run_id"] == audit.stem
    assert resolve_current(pointer) == audit.resolve()

    audit.write_text("changed", encoding="utf-8")
    assert resolve_current(pointer) is None


def test_publish_rejects_name_only_reconciled_snapshot(tmp_path: Path) -> None:
    audit = tmp_path / "audit.json"
    face = tmp_path / "face.json"
    reconciled = tmp_path / "reconciled.json"
    pointer = tmp_path / "current.json"
    for path in (audit, face):
        write_artifact(path, {})
    write_artifact(reconciled, {"schema_version": 2, "cluster_policy": "name-first"})

    try:
        publish_current(pointer, audit=audit, face_audit=face, reconciled=reconciled)
    except ValueError as exc:
        assert "face-only" in str(exc)
    else:
        raise AssertionError("name-only snapshots must not become current")
