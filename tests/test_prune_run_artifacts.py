import json
from pathlib import Path

from prune_run_artifacts import prune


def make_run(root: Path, run_id: str, mtime: int) -> None:
    manifest = root / f"{run_id}.run-manifest.json"
    manifest.write_text(json.dumps({"audit": str(root / f"{run_id}.json")}), encoding="utf-8")
    (root / f"{run_id}.json").write_text("{}", encoding="utf-8")
    manifest.touch()
    (root / f"{run_id}.json").touch()
    import os

    os.utime(manifest, (mtime, mtime))


def test_prune_is_dry_run_and_keeps_current_and_recent(tmp_path: Path) -> None:
    make_run(tmp_path, "20260901T000000Z", 1)
    make_run(tmp_path, "20260902T000000Z", 2)
    make_run(tmp_path, "20260903T000000Z", 3)
    report = prune(tmp_path, keep=1)
    assert report["dry_run"] is True
    assert report["candidates"] == ["20260902T000000Z", "20260901T000000Z"]
    assert (tmp_path / "20260901T000000Z.json").exists()


def test_prune_apply_removes_only_manifest_backed_old_runs(tmp_path: Path) -> None:
    make_run(tmp_path, "20260901T000000Z", 1)
    make_run(tmp_path, "20260902T000000Z", 2)
    (tmp_path / "legacy.json").write_text("{}", encoding="utf-8")
    report = prune(tmp_path, keep=1, apply=True)
    assert report["removed"] == ["20260901T000000Z"]
    assert not (tmp_path / "20260901T000000Z.json").exists()
    assert (tmp_path / "20260902T000000Z.json").exists()
    assert (tmp_path / "legacy.json").exists()
