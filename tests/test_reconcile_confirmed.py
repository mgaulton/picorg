from __future__ import annotations

import json
import hashlib
import sys

import reconcile_confirmed
from reconcile_confirmed import _confirmed_rows, reconcile
from identity_evidence_store import queue_assignment


def test_cli_argument_names_match_reconcile_keywords(monkeypatch, tmp_path, capsys):
    captured = {}

    def fake_reconcile(**kwargs):
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(reconcile_confirmed, "reconcile", fake_reconcile)
    monkeypatch.setattr(sys, "argv", [
        "reconcile_confirmed.py",
        "--image-decisions", str(tmp_path / "images.json"),
        "--decisions", str(tmp_path / "clusters.json"),
        "--markers", str(tmp_path / "markers.json"),
        "--pending-assignments", str(tmp_path / "pending.json"),
        "--evidence-db", str(tmp_path / "evidence.sqlite3"),
        "--audit", str(tmp_path / "audit.json"),
    ])
    assert reconcile_confirmed.main() == 0
    assert captured["image_decisions_path"] == tmp_path / "images.json"
    assert captured["decisions_path"] == tmp_path / "clusters.json"
    assert captured["markers_path"] == tmp_path / "markers.json"
    assert captured["pending_assignments_path"] == tmp_path / "pending.json"
    assert captured["evidence_db_path"] == tmp_path / "evidence.sqlite3"
    assert captured["audit_path"] == tmp_path / "audit.json"
    assert json.loads(capsys.readouterr().out) == {"ok": True}


def test_reconcile_confirmed_empty_run_is_idempotent(tmp_path):
    image_decisions = tmp_path / "image-decisions.json"
    decisions = tmp_path / "decisions.json"
    markers = tmp_path / "markers.json"
    image_decisions.write_text(json.dumps({"decisions": []}), encoding="utf-8")
    decisions.write_text(json.dumps({"decisions": []}), encoding="utf-8")
    result = reconcile(image_decisions_path=image_decisions, decisions_path=decisions, markers_path=markers)
    assert result["confirmed_records"] == 0
    assert result["moved"] == 0
    assert json.loads(markers.read_text(encoding="utf-8"))["markers"] == []


def test_reconcile_reports_queued_assignment_without_writing_marker(tmp_path):
    image_decisions = tmp_path / "image-decisions.json"
    decisions = tmp_path / "decisions.json"
    markers = tmp_path / "markers.json"
    pending = tmp_path / "pending.json"
    media = tmp_path / "queued.jpg"
    media.write_bytes(b"image fixture")
    image_decisions.write_text(json.dumps({"decisions": []}), encoding="utf-8")
    decisions.write_text(json.dumps({"decisions": []}), encoding="utf-8")
    pending.write_text(json.dumps({"assignments": {"queued": {"key": "queued", "path": str(media), "identity": "creator", "family": "manual", "status": "confirmed", "sha256": hashlib.sha256(b"image fixture").hexdigest()}}}), encoding="utf-8")
    result = reconcile(
        image_decisions_path=image_decisions,
        decisions_path=decisions,
        markers_path=markers,
        pending_assignments_path=pending,
    )
    assert result["pending_records"] == 1
    assert result["candidates"] == 1
    assert json.loads(markers.read_text(encoding="utf-8"))["markers"] == []


def test_queued_reassignment_overrides_older_confirmed_decision(tmp_path):
    image_decisions = tmp_path / "image-decisions.json"
    decisions = tmp_path / "decisions.json"
    markers = tmp_path / "markers.json"
    pending = tmp_path / "pending.json"
    media = tmp_path / "queued.jpg"
    media.write_bytes(b"image fixture")
    image_decisions.write_text(json.dumps({"decisions": [{"key": str(media), "path": str(media), "identity": "old", "family": "manual", "status": "confirmed"}]}), encoding="utf-8")
    decisions.write_text(json.dumps({"decisions": []}), encoding="utf-8")
    pending.write_text(json.dumps({"assignments": {str(media): {"key": str(media), "path": str(media), "identity": "new", "family": "manual", "status": "confirmed", "sha256": hashlib.sha256(b"image fixture").hexdigest()}}}), encoding="utf-8")
    rows = _confirmed_rows(image_decisions, decisions, None, pending)
    assert rows[0]["identity"] == "new"
    assert rows[0]["queued"] is True
    reconcile(image_decisions_path=image_decisions, decisions_path=decisions, markers_path=markers, pending_assignments_path=pending)
    assert json.loads(markers.read_text(encoding="utf-8"))["markers"] == []


def test_reconcile_refuses_changed_queued_file(tmp_path):
    image_decisions = tmp_path / "image-decisions.json"
    decisions = tmp_path / "decisions.json"
    markers = tmp_path / "markers.json"
    pending = tmp_path / "pending.json"
    media = tmp_path / "queued.jpg"
    media.write_bytes(b"new content")
    image_decisions.write_text(json.dumps({"decisions": []}), encoding="utf-8")
    decisions.write_text(json.dumps({"decisions": []}), encoding="utf-8")
    pending.write_text(json.dumps({"assignments": {"queued": {"key": "queued", "path": str(media), "identity": "creator", "family": "manual", "status": "confirmed", "sha256": hashlib.sha256(b"old content").hexdigest()}}}), encoding="utf-8")
    result = reconcile(image_decisions_path=image_decisions, decisions_path=decisions, markers_path=markers, pending_assignments_path=pending)
    assert result["candidates"] == 0
    assert "fingerprint changed" in result["errors"][0]["error"]


def test_reconcile_reads_durable_sqlite_assignment_queue(tmp_path):
    image_decisions = tmp_path / "image-decisions.json"
    decisions = tmp_path / "decisions.json"
    markers = tmp_path / "markers.json"
    pending = tmp_path / "pending.json"
    evidence_db = tmp_path / "evidence.sqlite3"
    media = tmp_path / "queued.jpg"
    media.write_bytes(b"image fixture")
    image_decisions.write_text(json.dumps({"decisions": []}), encoding="utf-8")
    decisions.write_text(json.dumps({"decisions": []}), encoding="utf-8")
    assignment_id = queue_assignment(
        evidence_db,
        path=str(media),
        identity="creator",
        expected_sha256=hashlib.sha256(b"image fixture").hexdigest(),
    )
    result = reconcile(
        image_decisions_path=image_decisions,
        decisions_path=decisions,
        markers_path=markers,
        pending_assignments_path=pending,
        evidence_db_path=evidence_db,
    )
    assert result["pending_records"] == 1
    assert result["candidates"] == 1
    assert result["applied_pending"] == 0
    assert assignment_id > 0


def test_apply_fails_closed_when_protected_reference_root_is_unhealthy(tmp_path, monkeypatch):
    monkeypatch.setattr(reconcile_confirmed, "health_report", lambda roots: {
        "healthy": False,
        "unavailable_paths": [str(next(iter(roots)))],
    })
    result = reconcile(apply=True, markers_path=tmp_path / "markers.json")
    assert result["blocked"] == "protected_reference_health"
    assert result["apply"] is False
    assert result["moved"] == 0
