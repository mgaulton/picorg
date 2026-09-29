import json
import fcntl
import hashlib
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import reconcile_confirmed
import review_ui
from identity_evidence_store import list_assignment_queue, queue_assignment


@pytest.fixture(autouse=True)
def isolate_face_rebuild_lock(tmp_path, monkeypatch):
    """Keep UI tests independent of any live operator rebuild lock."""
    monkeypatch.setenv("PICORG_FACE_REBUILD_LOCK", str(tmp_path / "face-rebuild.lock"))
    monkeypatch.setenv("PICORG_EVIDENCE_DB", str(tmp_path / "evidence.sqlite3"))


def test_html_page_closes_style_before_body():
    """CSS patches must re-emit </style>; otherwise browsers treat the page as CSS (blank UI)."""
    html = review_ui.HTML_PAGE
    assert html.count("</style>") == 1
    assert html.index("</style>") < html.index("</head>") < html.index("<body")
    assert "<script>" in html[html.index("</style>") :]


def test_review_page_has_responsive_review_contract():
    html = review_ui.HTML_PAGE
    assert 'name="viewport"' in html
    assert "selectionSummary" in html
    assert 'id="systemStatus"' in html
    assert "refreshSystemStatus" in html
    assert "moveHistory" in html
    assert "Needs attention" in html
    assert "URLSearchParams" in html
    assert "Image-level review required" in html
    assert "assignmentQueue" in html
    assert "/api/assignment-queue" in html
    assert "Recently used" in html
    assert "modalRecentIdentityList" in html
    assert "modal-recent-identities" in html
    assert "Create new identity: ${typed}" in html
    assert "postNewIdentityWithPrompt" in html
    assert "max-height:100%" in html
    assert "picorg.recent-identities.v1" in html
    assert "rememberIdentityUsed(identity,family)" in html
    assert "loadSelectedClusterImages" in html
    assert "Loading all images" in html
    assert "Assorted folders" in html
    assert "/api/assorted-folders" in html

def test_assorted_folders_pagination_loads_more_folders_not_clusters():
    html = review_ui.HTML_PAGE
    assert "Load more assorted folders" in html
    assert "assortedVisibleCount+=50" in html
    assert "next.onclick=()=>loadPage(false)" in html
    assert "next.hidden=true" in html
    assert "Save association (no moves)" in html
    assert "window.picorgNavigate" in html
    assert "Queue selected assignment" in html
    assert "Queued for ${esc(identity)}" in html
    assert 'class="image-state" role="status"' in html

def test_scheduler_settings_refresh_uses_exported_scoped_callback():
    html = review_ui.HTML_PAGE
    assert "window.picorgRefreshSchedulerSettings=loadSchedulerSettings" in html
    assert "await window.picorgRefreshSchedulerSettings?.()" in html
    assert "window.saveSchedulerSettings=saveSchedulerSettings" in html
    assert "window.launchScheduler=launchScheduler" in html
    assert "window.stopScheduler=stopScheduler" in html
    assert "window.rejectQueuedAssignment=rejectQueuedAssignment" in html

def test_attention_interface_paginates_and_counts_rendered_items():
    html = review_ui.HTML_PAGE
    assert "page=${page}&page_size=500" in html
    assert "Load more attention items" in html
    assert "list.insertAdjacentHTML('beforeend',cards)" in html
    assert "showing ${visible} of ${data.filtered} filtered" in html


def test_evidence_health_endpoint_reads_bounded_report(tmp_path):
    audit = audit_payload(tmp_path)
    report_path = tmp_path / "evidence-health.json"
    report_path.write_text(json.dumps({"healthy": True, "runtime_sqlite": "3.53.2"}), encoding="utf-8")
    app = writable_app(audit, evidence_health_path=report_path)
    response = app.test_client().get("/api/evidence-health")
    assert response.status_code == 200
    assert response.get_json()["healthy"] is True
    assert response.get_json()["available"] is True


def test_attention_queue_collects_preflight_and_quality_failures():
    payload = {"report": {"multi_face_deferred": 2, "no_face": 3, "error_samples": [{"path": "/bad.jpg", "message": "read"}]}}
    records = [{"path": "/missing.jpg", "status": "missing"}, {"path": "/ok.jpg", "status": "ok"}]
    queue = review_ui.build_attention_queue(payload, records)
    assert queue["total"] == 7
    assert queue["counts"]["missing"] == 1
    assert queue["counts"]["deferred"] == 2
    assert queue["counts"]["no-face"] == 3
    assert any(item["path"] == "/bad.jpg" and item["category"] == "unreadable" for item in queue["items"])


def audit_payload(tmp_path: Path) -> Path:
    path = tmp_path / "audit.json"
    for media_name in ("A (1).jpg", "A (2).jpg", "known.jpg"):
        (tmp_path / media_name).touch()
    path.write_text(
        json.dumps(
            {
                "report": {"scanned": 2},
                "results": [
                    {"path": str(tmp_path / "A (1).jpg"), "title": "A", "canonical": None, "source_root": str(tmp_path)},
                    {"path": str(tmp_path / "A (2).jpg"), "title": "A", "canonical": None, "source_root": str(tmp_path)},
                    {"path": str(tmp_path / "known.jpg"), "title": "Known", "canonical": "known", "source_root": str(tmp_path)},
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def writable_app(*args, **kwargs):
    """Explicitly opt tests into the unsafe no-token mutation mode."""
    kwargs["allow_unauthenticated_writes"] = True
    return review_ui.create_app(*args, **kwargs)


def test_build_clusters_groups_unmatched_gallery(tmp_path):
    clusters = review_ui.build_clusters(json.loads(audit_payload(tmp_path).read_text()))
    assert len(clusters) == 1
    assert clusters[0]["count"] == 2
    assert clusters[0]["title"] == "A"


def test_relink_path_records_does_not_resolve_paths_on_offline_mount(tmp_path, monkeypatch):
    source = str(tmp_path / "offline" / "source.jpg")
    destination = str(tmp_path / "organized" / "source.jpg")
    records = {"legacy-key": {"path": source, "identity": "creator"}}

    def fail_resolve(self, *args, **kwargs):
        raise AssertionError("path resolution must not touch the filesystem")

    monkeypatch.setattr(Path, "resolve", fail_resolve)
    relinked = review_ui._relink_path_records(
        [{"source": source, "destination": destination}], records
    )

    assert relinked == 1
    assert records[review_ui._image_decision_key(destination)]["path"] == destination


def test_cluster_purity_flags_require_ack_for_bulk_confirmation(tmp_path):
    clusters = review_ui.build_clusters(json.loads(audit_payload(tmp_path).read_text()))
    assert "no_expected_identity" in review_ui._cluster_purity_flags(clusters[0])
    assert "no_face_labels" in review_ui._cluster_purity_flags(clusters[0])
    safe = {"count": 12, "expected_identities": ["creator"], "face_cluster_labels": ["fbunknown001"], "families": ["review"]}
    assert review_ui._cluster_purity_flags(safe) == []
    client = writable_app(audit_payload(tmp_path), tmp_path / "decisions.json").test_client()
    cluster_id = client.get("/api/clusters?page=1&page_size=1").get_json()["clusters"][0]["cluster_id"]
    blocked = client.post("/api/decisions", json={"cluster_id": cluster_id, "identity": "creator", "family": "review", "status": "confirmed"})
    assert blocked.status_code == 409
    assert "purity_flags" in blocked.get_json()


def test_latest_audit_ignores_derived_json_reports(tmp_path):
    primary = tmp_path / "primary.json"
    primary.write_text(json.dumps({"results": []}), encoding="utf-8")
    (tmp_path / "primary.preflight.json").write_text(json.dumps({"counts": {}}), encoding="utf-8")
    (tmp_path / "primary.reconciled.json").write_text(json.dumps({"results": "not-an-audit"}), encoding="utf-8")
    assert review_ui.latest_audit(tmp_path) == primary

def test_cluster_gallery_rechecks_files_and_completed_move_history(tmp_path):
    audit = audit_payload(tmp_path)
    source = tmp_path / "A (1).jpg"
    destination = tmp_path / "moved" / source.name
    destination.parent.mkdir()
    source.replace(destination)
    (tmp_path / "A (2).jpg").unlink()
    history = tmp_path / "move_history.json"
    history.write_text(json.dumps({"operations": [{"id": "move-1", "undone": False, "moves": [{"source": str(source), "destination": str(destination)}]}]}))
    client = writable_app(audit, tmp_path / "decisions.json", move_history_path=history).test_client()
    cluster_id = client.get("/api/clusters?page=1&page_size=1").get_json()["clusters"][0]["cluster_id"]

    result = client.get(f"/api/clusters/{cluster_id}").get_json()

    assert result["paths"] == []
    assert result["count"] == 0


def test_decision_is_saved_and_media_is_allowlisted(tmp_path, monkeypatch):
    monkeypatch.setattr(review_ui.sorter, "load_identity_catalog", lambda: ([], {}, {}, {}, {}))
    audit = audit_payload(tmp_path)
    (tmp_path / "A (1).jpg").write_bytes(b"not-an-image")
    decisions = tmp_path / "decisions.json"
    client = writable_app(
        audit,
        decisions,
        image_decisions_path=tmp_path / "image_decisions.json",
        face_markers_path=tmp_path / "face_markers.json",
        review_identities_path=tmp_path / "review_identities.json",
    ).test_client()
    health = client.get("/healthz")
    assert health.status_code == 200
    assert client.get("/health").status_code == 200
    assert health.headers["X-Content-Type-Options"] == "nosniff"
    assert client.get("/favicon.ico").status_code == 204
    assert client.get("/missing-page").status_code == 404
    summary = client.get("/api/summary").get_json()
    assert "gallery_sets" not in summary["report"]
    cluster = client.get("/api/clusters").get_json()["clusters"][0]
    response = client.post(
        "/api/decisions",
        json={"cluster_id": cluster["cluster_id"], "identity": "creator_a", "family": "manual", "notes": "reviewed"},
    )
    assert response.status_code == 201
    assert json.loads(decisions.read_text())["decisions"][0]["identity"] == "creator_a"
    assert json.loads(decisions.read_text())["decisions"][0]["status"] == "pending"
    assert client.get("/media", query_string={"path": str(tmp_path / "A (1).jpg")}).status_code == 200
    assert client.get("/media", query_string={"path": "/etc/passwd"}).status_code == 403

    bulk = client.post(
        "/api/decisions/bulk",
        json={"cluster_ids": [cluster["cluster_id"]], "identity": "creator_a", "family": "manual", "status": "confirmed"},
    )
    assert bulk.status_code == 201
    assert json.loads(decisions.read_text())["decisions"][0]["status"] == "confirmed"
    oversized = client.post("/api/decisions", data="x" * 70000, content_type="application/json")
    assert oversized.status_code == 413
    image_path = review_ui.build_clusters(json.loads(audit.read_text()))[0]["paths"][0]
    assert client.post("/api/identities", json={"canonical": "new_creator"}).status_code == 201
    image_assignment = client.post("/api/image-decisions", json={"paths": [image_path], "identity": "new_creator", "family": "review"})
    assert image_assignment.status_code == 201
    detail = client.get(f"/api/clusters/{cluster['cluster_id']}").get_json()
    assert detail["image_decisions"][image_path]["identity"] == "new_creator"


def test_confirmed_image_marker_hash_is_captured_before_move(tmp_path, monkeypatch):
    audit = audit_payload(tmp_path)
    image_path = tmp_path / "A (1).jpg"
    image_path.write_bytes(b"reviewed-image")
    destination = tmp_path / "sorted"
    monkeypatch.setattr(review_ui, "DEFAULT_REVIEW_DEST_ROOT", destination)
    markers = tmp_path / "face_markers.json"
    ledger = tmp_path / "review-ledger.jsonl"
    client = writable_app(
        audit,
        tmp_path / "decisions.json",
        image_decisions_path=tmp_path / "image_decisions.json",
        face_markers_path=markers,
        ledger_path=ledger,
    ).test_client()
    response = client.post(
        "/api/image-decisions",
        json={"paths": [str(image_path)], "identity": "creator_a", "family": "manual", "status": "confirmed"},
    )
    assert response.status_code == 201
    marker = json.loads(markers.read_text(encoding="utf-8"))["markers"][0]
    assert marker["sha256"] == hashlib.sha256(b"reviewed-image").hexdigest()
    event = json.loads(ledger.read_text(encoding="utf-8").splitlines()[0])
    assert event["event"] == "image_decision"
    assert event["sha256"][str(image_path)] == marker["sha256"]


def test_applied_queued_assignment_can_be_undone_from_move_history(tmp_path, monkeypatch):
    audit = audit_payload(tmp_path)
    source = tmp_path / "A (1).jpg"
    source.write_bytes(b"queued media")
    destination = tmp_path / "sorted"
    evidence_db = tmp_path / "evidence.sqlite3"
    image_decisions = tmp_path / "image-decisions.json"
    decisions = tmp_path / "decisions.json"
    markers = tmp_path / "markers.json"
    pending = tmp_path / "pending.json"
    history = tmp_path / "move-history.json"
    monkeypatch.setattr(review_ui, "DEFAULT_REVIEW_DEST_ROOT", destination)
    monkeypatch.setattr(reconcile_confirmed, "DEFAULT_REVIEW_DEST_ROOT", destination)
    monkeypatch.setattr(review_ui.sorter, "DEFAULT_INTAKE_ROOTS", (tmp_path,))
    assignment_id = queue_assignment(
        evidence_db,
        path=str(source),
        identity="creator",
        expected_sha256=hashlib.sha256(b"queued media").hexdigest(),
        provenance={"family": "manual"},
    )
    image_decisions.write_text(json.dumps({"decisions": []}), encoding="utf-8")
    decisions.write_text(json.dumps({"decisions": []}), encoding="utf-8")
    client = writable_app(
        audit,
        decisions,
        image_decisions_path=image_decisions,
        face_markers_path=markers,
        move_history_path=history,
        evidence_db_path=evidence_db,
    ).test_client()

    result = reconcile_confirmed.reconcile(
        image_decisions_path=image_decisions,
        decisions_path=decisions,
        markers_path=markers,
        audit_path=audit,
        pending_assignments_path=pending,
        evidence_db_path=evidence_db,
        apply=True,
        allow_partial_health=True,
    )
    assert result["moved"] == 1
    assignment = client.get("/api/assignment-queue?status=applied").get_json()["assignments"][0]
    assert assignment["assignment_id"] == assignment_id
    move_id = assignment["move_id"]
    assert client.get("/api/moves").get_json()["operations"][-1]["id"] == move_id

    undo = client.post(f"/api/moves/{move_id}/undo")

    assert undo.status_code == 200
    assert undo.get_json()["restored_paths"] == [str(source)]
    assert source.is_file()
    assert list_assignment_queue(evidence_db, statuses=("undone",))[0]["assignment_id"] == assignment_id
    operation = next(item for item in client.get("/api/moves").get_json()["operations"] if item["id"] == move_id)
    assert operation["undone"] is True


def test_durable_face_markers_are_visible_and_hideable(tmp_path):
    audit = audit_payload(tmp_path)
    image_path = tmp_path / "A (1).jpg"
    markers = tmp_path / "face_markers.json"
    markers.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "markers": [
                    {
                        "key": review_ui._image_decision_key(str(image_path)),
                        "path": str(image_path),
                        "identity": "creator",
                        "family": "manual",
                        "status": "confirmed",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    client = writable_app(
        audit,
        tmp_path / "decisions.json",
        face_markers_path=markers,
    ).test_client()
    cluster_id = client.get("/api/clusters?page=1&page_size=1&hide_confirmed=1").get_json()["clusters"][0]["cluster_id"]
    detail = client.get(f"/api/clusters/{cluster_id}?hide_confirmed=1").get_json()
    assert detail["hidden_confirmed"] == 1
    assert detail["image_decisions"][str(image_path)]["identity"] == "creator"
    assert detail["image_decisions"][str(image_path)]["status"] == "confirmed"
    assert str(image_path) not in detail["paths"]
    groups = client.get("/api/identity-groups").get_json()
    creator = next(group for group in groups if group["identity"] == "creator")
    assert creator["confirmed"] == 1


def test_cluster_list_hides_cluster_when_all_images_are_confirmed(tmp_path):
    audit = audit_payload(tmp_path)
    paths = [str(tmp_path / "A (1).jpg"), str(tmp_path / "A (2).jpg")]
    markers = tmp_path / "face_markers.json"
    markers.write_text(
        json.dumps({"schema_version": 1, "markers": [
            {"key": review_ui._image_decision_key(path), "path": path,
             "identity": "creator", "family": "manual", "status": "confirmed"}
            for path in paths
        ]}),
        encoding="utf-8",
    )
    client = writable_app(audit, tmp_path / "decisions.json", face_markers_path=markers).test_client()

    hidden = client.get("/api/clusters?hide_confirmed=1").get_json()
    visible = client.get("/api/clusters?hide_confirmed=0").get_json()
    assert hidden["clusters"] == []
    assert visible["clusters"][0]["count"] == 2


def test_cluster_list_filter_matches_identity_aliases(tmp_path, monkeypatch):
    rows = [review_ui.sorter.Identity("wgp_niki", "metadaily", ("Niki", "WGP Niki"))]
    monkeypatch.setattr(review_ui.sorter, "load_identity_catalog", lambda: (rows, {}, {}, {}, {}))
    audit = audit_payload(tmp_path)
    payload = json.loads(audit.read_text(encoding="utf-8"))
    payload["results"][0]["expected_identity"] = "wgp_niki"
    audit.write_text(json.dumps(payload), encoding="utf-8")
    client = writable_app(audit, tmp_path / "decisions.json").test_client()

    response = client.get("/api/clusters?q=niki&hide_confirmed=0").get_json()
    assert response["total"] == 1
    assert response["clusters"][0]["identity_alias_match"] is True


def test_new_identity_assignment_is_confirmed_and_persisted(tmp_path, monkeypatch):
    monkeypatch.setattr(review_ui.sorter, "load_identity_catalog", lambda: ([], {}, {}, {}, {}))
    audit = audit_payload(tmp_path)
    image_path = tmp_path / "A (1).jpg"
    image_path.write_bytes(b"new-identity-image")
    destination = tmp_path / "sorted"
    monkeypatch.setattr(review_ui, "DEFAULT_REVIEW_DEST_ROOT", destination)
    image_decisions = tmp_path / "image_decisions.json"
    client = writable_app(
        audit,
        tmp_path / "decisions.json",
        image_decisions_path=image_decisions,
        face_markers_path=tmp_path / "face_markers.json",
        review_identities_path=tmp_path / "review_identities.json",
    ).test_client()
    response = client.post(
        "/api/identities",
        json={"canonical": "new_creator", "family": "manual", "paths": [str(image_path)]},
    )
    assert response.status_code == 201
    cluster_id = review_ui.build_clusters(json.loads(audit.read_text(encoding="utf-8")))[0]["cluster_id"]
    detail = client.get(f"/api/clusters/{cluster_id}").get_json()
    assert detail["image_decisions"][str(image_path)]["status"] == "confirmed"
    assert detail["image_decisions"][str(image_path)]["identity"] == "new_creator"
    assert response.get_json()["moved"]
    move_id = response.get_json()["move_id"]
    undo = client.post(f"/api/moves/{move_id}/undo")
    assert undo.status_code == 200
    assert image_path.is_file()
    assert client.post(f"/api/moves/{move_id}/undo").status_code == 409
    detail = client.get(f"/api/clusters/{cluster_id}").get_json()
    assert detail["image_decisions"][str(image_path)]["status"] == "pending"


def test_new_identity_name_collisions_return_promptable_conflict(tmp_path, monkeypatch):
    monkeypatch.setattr(
        review_ui.sorter,
        "load_identity_catalog",
        lambda: ([SimpleNamespace(canonical="existing_creator", family="review", aliases=["creator_alias"])], {}, {}, {}, {}),
    )
    assorted = tmp_path / "assorted"
    folder = assorted / "another_creator"
    folder.mkdir(parents=True)
    client = writable_app(
        audit_payload(tmp_path),
        tmp_path / "decisions.json",
        review_identities_path=tmp_path / "review-identities.json",
        assorted_root=assorted,
        assorted_associations_path=tmp_path / "associations.json",
    ).test_client()

    for name in ("existing_creator", "creator_alias"):
        response = client.post("/api/identities", json={"canonical": name, "family": "review"})
        assert response.status_code == 409
        assert response.get_json()["collision"] is True

    response = client.post(
        "/api/assorted-folder-associations",
        json={"folder": str(folder), "identity": "creator_alias", "family": "review", "create_identity": True},
    )
    assert response.status_code == 409
    assert response.get_json()["collision"] is True
    assert folder.is_dir()


def test_review_page_contains_thumbnail_confirmation_controls(tmp_path):
    client = writable_app(audit_payload(tmp_path), tmp_path / "decisions.json").test_client()
    page = client.get("/")
    assert page.status_code == 200
    assert b"Confirm" in page.data
    assert b"Undo last move" in page.data
    assert b"Use last identity" in page.data
    assert b"Recently used identities" in page.data
    assert b"click to assign and move" in page.data
    assert b"await assignModalImage()" in page.data
    assert b"Enter a different identity name" in page.data
    assert b"height:calc(100dvh - 16px)" in page.data
    assert b"undoModalMove" in page.data
    assert b"useLastModalIdentity" in page.data
    assert b"Hide confirmed" in page.data
    assert b"Face groups (face match)" in page.data
    assert b"Name-only groups" not in page.data
    assert b"assignment-state" in page.data
    assert b"Unassign" in page.data
    assert b"Reassign" in page.data
    assert b"Previous" in page.data
    assert b"Next" in page.data
    assert b"Assign & confirm" in page.data
    assert b"Confirm the identity for this cluster" in page.data
    assert b"move them into the identity folder" in page.data
    assert b"Preview unavailable" not in page.data


def test_cluster_confirmation_persists_image_decisions_and_moves_files(tmp_path, monkeypatch):
    audit = audit_payload(tmp_path)
    for name in ("A (1).jpg", "A (2).jpg"):
        (tmp_path / name).write_bytes(name.encode("utf-8"))
    monkeypatch.setattr(review_ui, "DEFAULT_REVIEW_DEST_ROOT", tmp_path / "sorted")
    decisions = tmp_path / "decisions.json"
    image_decisions = tmp_path / "image_decisions.json"
    client = writable_app(
        audit,
        decisions,
        image_decisions_path=image_decisions,
        face_markers_path=tmp_path / "face_markers.json",
        move_history_path=tmp_path / "move_history.json",
    ).test_client()
    cluster = review_ui.build_clusters(json.loads(audit.read_text(encoding="utf-8")))[0]

    response = client.post(
        "/api/decisions",
        json={
            "cluster_id": cluster["cluster_id"],
            "identity": "creator_a",
            "family": "manual",
                "status": "confirmed",
                "purity_ack": True,
                "notes": "Approved after identity prompt",
        },
    )

    assert response.status_code == 201
    assert len(response.get_json()["moved"]) == 2
    persisted = json.loads(image_decisions.read_text(encoding="utf-8"))["decisions"]
    assert {item["path"] for item in persisted} == set(cluster["paths"])
    assert all(item["identity"] == "creator_a" and item["status"] == "confirmed" for item in persisted)
    assert all((tmp_path / "sorted" / "manual" / "creator_a" / Path(path).name).is_file() for path in cluster["paths"])


def test_cluster_confirmation_rejects_unsafe_identity(tmp_path):
    audit = audit_payload(tmp_path)
    cluster = review_ui.build_clusters(json.loads(audit.read_text(encoding="utf-8")))[0]
    client = writable_app(audit, tmp_path / "decisions.json").test_client()

    response = client.post(
        "/api/decisions",
        json={"cluster_id": cluster["cluster_id"], "identity": "../outside", "family": "review", "status": "confirmed"},
    )

    assert response.status_code == 400
    assert "safe folder name" in response.get_json()["error"]


def test_unassign_image_restores_path_and_clears_identity(tmp_path, monkeypatch):
    audit = audit_payload(tmp_path)
    image_path = tmp_path / "A (1).jpg"
    image_path.write_bytes(b"unassign-me")
    monkeypatch.setattr(review_ui, "DEFAULT_REVIEW_DEST_ROOT", tmp_path / "sorted")
    client = writable_app(
        audit,
        tmp_path / "decisions.json",
        image_decisions_path=tmp_path / "image_decisions.json",
        face_markers_path=tmp_path / "face_markers.json",
        move_history_path=tmp_path / "move_history.json",
    ).test_client()
    assigned = client.post("/api/image-decisions", json={"paths": [str(image_path)], "identity": "creator_a", "family": "manual", "status": "confirmed"})
    assert assigned.status_code == 201
    unassigned = client.post("/api/image-decisions/unassign", json={"path": str(image_path)})
    assert unassigned.status_code == 200
    assert image_path.is_file()
    assert unassigned.get_json()["unassigned"] is True
    record = json.loads((tmp_path / "image_decisions.json").read_text(encoding="utf-8"))["decisions"][0]
    assert record["identity"] == ""
    assert record["status"] == "pending"


def test_unassign_accepts_canonical_destination_and_relinks_exemplar(tmp_path, monkeypatch):
    audit = audit_payload(tmp_path)
    image_path = tmp_path / "A (1).jpg"
    image_path.write_bytes(b"destination-unassign")
    destination_root = tmp_path / "sorted"
    monkeypatch.setattr(review_ui, "DEFAULT_REVIEW_DEST_ROOT", destination_root)
    markers_path = tmp_path / "face_markers.json"
    client = writable_app(
        audit,
        tmp_path / "decisions.json",
        image_decisions_path=tmp_path / "image_decisions.json",
        face_markers_path=markers_path,
        move_history_path=tmp_path / "move_history.json",
    ).test_client()
    assigned = client.post(
        "/api/image-decisions",
        json={"paths": [str(image_path)], "identity": "creator_a", "family": "manual", "status": "confirmed"},
    )
    canonical_path = assigned.get_json()["moved"][0]["destination"]
    marker = json.loads(markers_path.read_text(encoding="utf-8"))["markers"][0]
    assert marker["path"] == canonical_path
    unassigned = client.post("/api/image-decisions/unassign", json={"path": canonical_path})
    assert unassigned.status_code == 200
    assert image_path.is_file()
    record = json.loads((tmp_path / "image_decisions.json").read_text(encoding="utf-8"))["decisions"][0]
    assert record["status"] == "pending"


def test_ui_token_protects_non_health_endpoints(tmp_path):
    client = review_ui.create_app(audit_payload(tmp_path), tmp_path / "decisions.json", ui_token="secret").test_client()
    client.environ_base["REMOTE_ADDR"] = "203.0.113.10"
    assert client.get("/healthz").status_code == 200
    assert client.get("/health").status_code == 200
    assert client.get("/api/summary").status_code == 401
    assert client.get("/api/summary", headers={"X-Picorg-Token": "secret"}).status_code == 200
    assert client.get("/api/summary", headers={"Authorization": "Bearer secret"}).status_code == 200


def test_environment_token_enables_auth_without_extra_switch(tmp_path, monkeypatch):
    monkeypatch.setenv("PICORG_UI_TOKEN", "secret")
    monkeypatch.delenv("PICORG_UI_AUTH", raising=False)
    client = review_ui.create_app(
        audit_payload(tmp_path),
        tmp_path / "decisions.json",
        image_decisions_path=tmp_path / "image_decisions.json",
        face_markers_path=tmp_path / "face_markers.json",
    ).test_client()
    client.environ_base["REMOTE_ADDR"] = "203.0.113.10"
    assert client.get("/api/summary").status_code == 401
    assert client.get("/api/summary", headers={"X-Picorg-Token": "secret"}).status_code == 200


def test_remote_mode_requires_token(tmp_path):
    client = review_ui.create_app(audit_payload(tmp_path), tmp_path / "decisions.json").test_client()
    client.environ_base["REMOTE_ADDR"] = "203.0.113.10"
    assert client.get("/healthz").status_code == 200
    assert client.get("/api/summary").status_code == 401
    response = client.post("/api/identities", json={"canonical": "creator"})
    assert response.status_code == 401
    assert "remote access requires" in response.get_json()["error"]


def test_lan_mode_allows_reads_and_mutations_without_token(tmp_path, monkeypatch):
    monkeypatch.setattr(review_ui.sorter, "load_identity_catalog", lambda: ([], {}, {}, {}, {}))
    client = review_ui.create_app(
        audit_payload(tmp_path),
        tmp_path / "decisions.json",
        review_identities_path=tmp_path / "review_identities.json",
    ).test_client()
    client.environ_base["REMOTE_ADDR"] = "192.168.2.5"
    assert client.get("/api/summary").status_code == 200
    response = client.post("/api/identities", json={"canonical": "creator"})
    assert response.status_code == 201


def test_assorted_folder_association_is_metadata_only(tmp_path):
    assorted = tmp_path / "assorted"
    candidate = assorted / "some_creator"
    generic = assorted / "#topic"
    candidate.mkdir(parents=True)
    generic.mkdir(parents=True)
    image = candidate / "one.jpg"
    image.write_bytes(b"placeholder")
    (generic / "topic.jpg").write_bytes(b"placeholder")
    associations = tmp_path / "assorted-associations.json"
    client = writable_app(
        audit_payload(tmp_path),
        tmp_path / "decisions.json",
        assorted_root=assorted,
        assorted_associations_path=associations,
        review_identities_path=tmp_path / "review-identities.json",
    ).test_client()
    inventory = client.get("/api/assorted-folders")
    assert inventory.status_code == 200
    folders = inventory.get_json()["folders"]
    assert [item["label"] for item in folders] == ["some_creator"]
    response = client.post(
        "/api/assorted-folder-associations",
        json={"folder": str(candidate), "identity": "some_creator", "family": "review", "create_identity": True},
    )
    assert response.status_code == 201
    payload = response.get_json()
    assert payload["moved"] == []
    assert payload["applied"] is False
    assert image.is_file()
    saved = json.loads(associations.read_text(encoding="utf-8"))["associations"]
    assert saved[0]["identity"] == "some_creator"
    assert client.get("/api/assorted-folders").get_json()["folders"][0]["associated"]["identity"] == "some_creator"


def test_face_rebuild_forces_read_only_mode(tmp_path):
    lock_path = tmp_path / "face-rebuild.lock"
    with lock_path.open("w+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        app = review_ui.create_app(
            audit_payload(tmp_path),
            tmp_path / "decisions.json",
            face_rebuild_lock_path=lock_path,
        )
        client = app.test_client()
        client.environ_base["REMOTE_ADDR"] = "192.168.2.5"
        summary = client.get("/api/summary")
        assert summary.status_code == 200
        assert summary.get_json()["read_only"] is True
        assert client.get("/api/runtime").get_json()["read_only"] is True
        status = client.get("/api/rebuild-status").get_json()
        assert status["running"] is True
        assert status["read_only"] is True
        response = client.post("/api/identities", json={"canonical": "creator"})
        assert response.status_code == 423
        assert response.get_json()["read_only"] is True


def test_face_rebuild_queues_assignments_without_applying_them(tmp_path):
    lock_path = tmp_path / "face-rebuild.lock"
    pending_path = tmp_path / "pending.json"
    image_decisions_path = tmp_path / "image-decisions.json"
    with lock_path.open("w+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        app = writable_app(
            audit_payload(tmp_path),
            tmp_path / "decisions.json",
            image_decisions_path=image_decisions_path,
            pending_assignments_path=pending_path,
            face_rebuild_lock_path=lock_path,
        )
        client = app.test_client()
        client.environ_base["REMOTE_ADDR"] = "192.168.2.5"
        image_path = str(tmp_path / "A (1).jpg")
        queued = client.post("/api/image-decisions/pending", json={
            "paths": [image_path], "identity": "creator", "family": "manual", "status": "confirmed",
        })
        assert queued.status_code == 202
        assert queued.get_json()["applied"] is False
        assert json.loads(pending_path.read_text())["assignments"][review_ui._image_decision_key(image_path)]["identity"] == "creator"
        assert not image_decisions_path.exists()
        blocked = client.post("/api/image-decisions", json={"paths": [image_path], "identity": "creator", "family": "manual", "status": "confirmed"})
        assert blocked.status_code == 423
        status = client.get("/api/pending-assignments").get_json()
        assert status["count"] == 1


def test_pending_assignments_are_safe_and_durable_without_active_rebuild(tmp_path):
    app = writable_app(audit_payload(tmp_path), tmp_path / "decisions.json", pending_assignments_path=tmp_path / "pending.json").test_client()
    image_path = str(tmp_path / "A (1).jpg")
    response = app.post("/api/image-decisions/pending", json={"paths": [image_path], "identity": "creator", "family": "manual", "status": "confirmed"})
    assert response.status_code == 202
    assert response.get_json()["read_only"] is False
    assert json.loads((tmp_path / "pending.json").read_text())["assignments"]
    queue = app.get("/api/assignment-queue").get_json()
    assert queue["count"] == 1
    assignment_id = queue["assignments"][0]["assignment_id"]
    rejected = app.post(f"/api/assignment-queue/{assignment_id}/reject")
    assert rejected.status_code == 200
    assert app.get("/api/assignment-queue?status=rejected").get_json()["count"] == 1


def test_applied_durable_assignment_is_hidden_from_cluster_gallery(tmp_path):
    audit = audit_payload(tmp_path)
    moved_path = str(tmp_path / "A (1).jpg")
    evidence_db = tmp_path / "evidence.sqlite3"
    app = writable_app(audit, tmp_path / "decisions.json", evidence_db_path=evidence_db).test_client()
    assignment_id = review_ui.queue_durable_assignment(
        evidence_db, path=moved_path, identity="creator", source="test", created_by="test"
    )
    review_ui.update_durable_assignment_status(evidence_db, assignment_id, "applied")

    cluster = app.get("/api/clusters?page=1&page_size=10&hide_confirmed=1").get_json()["clusters"][0]
    detail = app.get(f"/api/clusters/{cluster['cluster_id']}?hide_confirmed=1").get_json()
    assert cluster["count"] == detail["count"] == 1
    assert moved_path not in cluster["sample_paths"]
    assert moved_path not in detail["sample_paths"]
    assert app.get(f"/api/clusters/{cluster['cluster_id']}?hide_confirmed=0").get_json()["image_decisions"][moved_path]["status"] == "confirmed"


def test_durable_queued_assignment_is_visible_on_cluster_image(tmp_path):
    audit = audit_payload(tmp_path)
    image_path = str(tmp_path / "A (1).jpg")
    evidence_db = tmp_path / "evidence.sqlite3"
    app = writable_app(audit, tmp_path / "decisions.json", evidence_db_path=evidence_db).test_client()
    review_ui.queue_durable_assignment(
        evidence_db,
        path=image_path,
        identity="creator",
        source="review_ui_async",
        created_by="test",
        provenance={"family": "metadaily"},
    )

    cluster = app.get("/api/clusters?page=1&page_size=10").get_json()["clusters"][0]
    detail = app.get(f"/api/clusters/{cluster['cluster_id']}").get_json()
    decision = detail["image_decisions"][image_path]
    assert decision["identity"] == "creator"
    assert decision["family"] == "metadaily"
    assert decision["status"] == "queued"
    assert decision["queued_status"] == "pending"

def test_deleted_media_is_removed_but_recreated_path_returns(tmp_path):
    audit = audit_payload(tmp_path)
    deleted_path = str(tmp_path / "A (1).jpg")
    ledger = tmp_path / "review_ledger.jsonl"
    ledger.write_text(json.dumps({"event": "media_deleted", "path": deleted_path}) + "\n", encoding="utf-8")
    (tmp_path / "A (1).jpg").unlink()
    app = writable_app(audit, tmp_path / "decisions.json", ledger_path=ledger).test_client()

    cluster = app.get("/api/clusters?page=1&page_size=10&hide_confirmed=0").get_json()["clusters"][0]
    assert cluster["count"] == 1
    assert deleted_path not in cluster["sample_paths"]

    (tmp_path / "A (1).jpg").write_bytes(b"reintroduced media")
    cluster = app.get("/api/clusters?page=1&page_size=10&hide_confirmed=0").get_json()["clusters"][0]
    assert cluster["count"] == 2
    assert deleted_path in cluster["sample_paths"]

def test_rebuild_status_summarizes_append_only_repair_ledger(tmp_path):
    ledger = tmp_path / "repair_ledger.jsonl"
    ledger.write_text(
        "\n".join([
            json.dumps({"status": "repaired", "timestamp": 10}),
            json.dumps({"status": "repair_failed", "timestamp": 12}),
        ]) + "\n",
        encoding="utf-8",
    )
    app = review_ui.create_app(
        audit_payload(tmp_path),
        tmp_path / "decisions.json",
        repair_ledger_path=ledger,
    )
    status = app.test_client().get("/api/rebuild-status").get_json()
    assert status["running"] is False
    assert status["repair_ledger"]["records"] == 2
    assert status["repair_ledger"]["status_counts"] == {"repaired": 1, "repair_failed": 1}
    # Polling after EOF must retain the last ledger timestamp.
    second = app.test_client().get("/api/rebuild-status").get_json()
    assert second["repair_ledger"]["latest_update"] == status["repair_ledger"]["latest_update"]


def test_rebuild_status_reports_latest_bounded_progress(tmp_path):
    rebuild_log = tmp_path / "rebuild.log"
    rebuild_log.write_text(
        "[health] rebuild attempt 1 alive pid=123 log=12:34:56 progress=face extraction: 120/400 selected, embedded=90\n"
        "warning: skipped unreadable path=/private/media.jpg\n",
        encoding="utf-8",
    )
    lock_path = tmp_path / "rebuild.lock"
    with lock_path.open("w+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        app = review_ui.create_app(
            audit_payload(tmp_path),
            tmp_path / "decisions.json",
            face_rebuild_lock_path=lock_path,
            rebuild_log_path=rebuild_log,
        )
        status = app.test_client().get("/api/rebuild-status").get_json()
    assert status["running"] is True
    assert status["progress"]["message"].startswith("face extraction: 120/400")
    assert "/private/media.jpg" not in status["progress"]["message"]


def test_rebuild_status_reports_terminal_timeout_when_idle(tmp_path):
    rebuild_log = tmp_path / "rebuild.log"
    rebuild_log.write_text(
        "error: source root timed out after 3600s: /private/redditdaily\n"
        "restored previous face database after failed rebuild: /private/faces.db\n",
        encoding="utf-8",
    )
    app = review_ui.create_app(
        audit_payload(tmp_path),
        tmp_path / "decisions.json",
        face_rebuild_lock_path=tmp_path / "rebuild.lock",
        rebuild_log_path=rebuild_log,
    )
    status = app.test_client().get("/api/rebuild-status").get_json()
    assert status["running"] is False
    assert status["progress"] == {
        "message": "last rebuild failed: source root timeout",
        "outcome": "failed",
        "reason": "source root timeout",
    }


def test_scheduler_settings_are_persisted_and_reported(tmp_path):
    config = tmp_path / "scheduler.json"
    status = tmp_path / "scheduler-status.json"
    app = review_ui.create_app(
        audit_payload(tmp_path),
        tmp_path / "decisions.json",
        scheduler_config_path=config,
        scheduler_status_path=status,
    )
    client = app.test_client()
    saved = client.post("/api/scheduler/config", json={"enabled": True, "interval_minutes": 15, "apply_high_confidence": True})
    assert saved.status_code == 200
    assert saved.get_json()["config"]["interval_minutes"] == 15
    current = client.get("/api/scheduler/status")
    assert current.status_code == 200
    assert current.get_json()["config"]["enabled"] is True


def test_identity_catalog_picks_up_review_identities_added_after_startup(tmp_path):
    review_identities = tmp_path / "review_identities.json"
    review_identities.write_text(json.dumps({"decisions": []}), encoding="utf-8")
    app = review_ui.create_app(audit_payload(tmp_path), tmp_path / "decisions.json", review_identities_path=review_identities)
    review_identities.write_text(
        json.dumps({"decisions": [{"cluster_id": "ktennins", "identity": "ktennins", "family": "review"}]}),
        encoding="utf-8",
    )
    identities = app.test_client().get("/api/identities").get_json()
    assert any(item["canonical"] == "ktennins" for item in identities)


def test_baseline_identity_options_include_reddit_follow_accounts(tmp_path, monkeypatch):
    def fake_catalog():
        rows = [
            review_ui.sorter.Identity("katdennings", "reddit_follow", ("Kat Dennings",)),
            review_ui.sorter.Identity("stoyadoll", "imdb", ("stoya",)),
            review_ui.sorter.Identity("stoya", "imdb", ()),
            review_ui.sorter.Identity("example_subreddit", "reddit_subreddit", ()),
        ]
        return rows, {}, {}, {}, {"stoya": "stoyadoll"}

    monkeypatch.setattr(review_ui.sorter, "load_identity_catalog", fake_catalog)
    client = writable_app(audit_payload(tmp_path), tmp_path / "decisions.json").test_client()
    identities = client.get("/api/identities?scope=baseline").get_json()
    assert any(item["canonical"] == "katdennings" for item in identities)
    assert not any(item["canonical"] == "example_subreddit" for item in identities)
    assert any(item["canonical"] == "stoyadoll" for item in client.get("/api/identities").get_json())
    canonical = client.get("/api/identities?scope=canonical").get_json()
    assert {"katdennings", "stoyadoll", "example_subreddit"}.issubset({item["canonical"] for item in canonical})
    assert not any(item["canonical"] == "stoya" for item in client.get("/api/identities").get_json())


def test_canonical_picker_keeps_registry_and_md_rd_baseline(tmp_path, monkeypatch):
    def fake_catalog():
        rows = [
            review_ui.sorter.Identity("registered", "manual", ()),
            review_ui.sorter.Identity("md_person", "metadaily", ()),
            review_ui.sorter.Identity("rd_person", "redditdaily", ()),
            review_ui.sorter.Identity("follow_person", "reddit_follow", ()),
            review_ui.sorter.Identity("subreddit_person", "reddit_subreddit", ()),
        ]
        return rows, {}, {}, {}, {}

    monkeypatch.setattr(review_ui.sorter, "load_identity_catalog", fake_catalog)
    client = writable_app(audit_payload(tmp_path), tmp_path / "decisions.json").test_client()
    identities = client.get("/api/identities?scope=canonical").get_json()
    names = {item["canonical"] for item in identities}
    assert {"registered", "md_person", "rd_person"}.issubset(names)
    assert {"follow_person", "subreddit_person"}.issubset(names)


def test_identity_groups_merge_case_variants_and_mark_generic_empty_entries(tmp_path, monkeypatch):
    def fake_catalog():
        rows = [
            review_ui.sorter.Identity("Creator", "manual", ()),
            review_ui.sorter.Identity("creator", "manual", ()),
            review_ui.sorter.Identity("vagina", "manual", ()),
        ]
        return rows, {}, {}, {}, {}

    monkeypatch.setattr(review_ui.sorter, "load_identity_catalog", fake_catalog)
    monkeypatch.setattr(review_ui.sorter, "PROJECT_BLOCKED_TOKENS", frozenset({"vagina"}))
    monkeypatch.setattr(review_ui.sorter, "PROJECT_AMBIGUOUS_TOKENS", frozenset())
    client = review_ui.create_app(
        audit_payload(tmp_path),
        tmp_path / "decisions.json",
        review_identities_path=tmp_path / "review_identities.json",
    ).test_client()

    groups = client.get("/api/identity-groups").get_json()
    creators = [item for item in groups if item["identity"].casefold() == "creator"]
    generic = next(item for item in groups if item["identity"].casefold() == "vagina")
    page = client.get("/").data

    assert len(creators) == 1
    assert creators[0]["identity"] == "Creator"
    assert creators[0]["disk_scanned"] is False
    assert generic["generic"] is True
    assert b"!g.generic" in page
    assert b"showView('clusters')" in page
    assert b"/api/identity-groups?include_disk=1" in page
    assert b"Scanning identity folders" in page
    assert b"['manual','review','metadaily','redditdaily']" in page
    assert b"hidden=['reddit_follow','reddit_subreddit','reddit_friends','pscrape','imdb']" in page
    assert b"identityOptionMatches(item,filter)" in page
    assert b"function identityOptionMatches(item,query)" in page
    assert b"(item.aliases||[]).some(alias=>String(alias).toLocaleLowerCase().includes(query))" in page
    assert b"(g.identity+' '+(g.aliases||[]).join(' ')).toLowerCase().includes(q)" in page
    assert b"(group.identity+' '+(group.aliases||[]).join(' ')).toLowerCase().includes(query)" in page
    assert b"<b>${esc(group.identity)}</b>" in page
    assert b"Undo this assignment / move" in page
    assert b"button.onclick=()=>window.picorgUndoMove(moveId)" in page
    assert "if(!document.body.classList.contains('read-only'))return originalAssignModalImage()".encode() in page
    assert "let endpoint=newIdentity?'/api/identities':'/api/image-decisions/async'".encode() in page
    assert "let endpoint='/api/image-decisions/pending'".encode() in page
    assert "canonical:identity,family,paths:[path]".encode() in page
    assert b"button.disabled=deleted||assignmentQueued" in page
    assert b"status=pending,applying,applied,error,conflict,rejected" in page
    assert b"['applied','error','conflict','rejected'].includes(item.status)" in page
    assert b'data-loaded="sample"' in page
    assert b"Load all ${x.count} images" in page
    assert b"if(grid&&!grid.dataset.loaded)loadClusterImages()" not in page
    assert b"await new Promise(resolve=>setTimeout(resolve,0));" in page
    assert b"lazyDetailObserver.observe(document.querySelector('#detail'),{childList:true});" in page
    assert b"detailEnhancementObserver.observe(document.querySelector('#detail'),{childList:true});" in page
    assert b"selected=id;document.querySelectorAll('.cluster').forEach" in page
    assert b"media.decoding='async';media.loading='lazy';" in page
    assert b"content-visibility:auto;contain:layout paint style;" in page
    assert "Assign selected…".encode() in page
    assert b"Assign ${paths.length} selected image" in page
    assert b"Confirm this image as" in page


def test_identity_groups_skips_sorted_tree_scan_by_default(tmp_path, monkeypatch):
    calls = []

    def boom(root):
        calls.append(str(root))
        if False:
            yield Path("/dev/null")
        return
        yield  # pragma: no cover — make this a generator

    monkeypatch.setattr(review_ui, "iter_media_files", boom)
    client = review_ui.create_app(
        audit_payload(tmp_path),
        tmp_path / "decisions.json",
        review_identities_path=tmp_path / "review_identities.json",
    ).test_client()

    default_groups = client.get("/api/identity-groups").get_json()
    assert calls == []
    assert default_groups
    assert all(item.get("disk_scanned") is False for item in default_groups)

    disk_groups = client.get("/api/identity-groups?include_disk=1").get_json()
    assert calls, "include_disk=1 should scan identity folders"
    assert all(item.get("disk_scanned") is True for item in disk_groups)


def test_accuracy_benchmark_endpoint_runs_fixed_review_only_script(tmp_path, monkeypatch):
    calls = []

    class FakeProcess:
        stdout = iter(("[1/3] building confirmed face pairs\n", "benchmark summary: pairs=2\n"))

        def wait(self):
            return 0

    def fake_popen(command, **kwargs):
        calls.append((command, kwargs))
        return FakeProcess()

    monkeypatch.setattr(review_ui.subprocess, "Popen", fake_popen)
    client = review_ui.create_app(audit_payload(tmp_path), tmp_path / "decisions.json").test_client()
    started = client.post("/api/accuracy-benchmark")
    assert started.status_code == 202
    for _ in range(20):
        status = client.get("/api/accuracy-benchmark").get_json()
        if not status["running"]:
            break
        time.sleep(0.005)
    assert status["exit_code"] == 0
    assert any(str(item).endswith("run_accuracy_benchmark.sh") for item in calls[0][0])
    assert "benchmark summary: pairs=2" in status["lines"][-1]


def test_export_promotes_confirmed_only(tmp_path):
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"entries": []}), encoding="utf-8")
    decisions = tmp_path / "decisions.json"
    decisions.write_text(
        json.dumps(
            {
                "decisions": [
                    {"cluster_id": "confirmed", "identity": "creator_a", "family": "manual", "status": "confirmed", "aliases": ["Creator A"], "notes": "two sources"},
                    {"cluster_id": "pending", "identity": "creator_b", "family": "manual", "status": "pending", "aliases": ["Creator B"]},
                ]
            }
        ),
        encoding="utf-8",
    )

    assert review_ui.export_confirmed_decisions(decisions, registry) == 1
    entries = json.loads(registry.read_text())["entries"]
    assert [item["canonical"] for item in entries] == ["creator_a"]


def test_review_family_is_not_exportable(tmp_path):
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"entries": []}), encoding="utf-8")
    decisions = tmp_path / "decisions.json"
    decisions.write_text(json.dumps({"decisions": [{"cluster_id": "review", "identity": "creator", "family": "review", "status": "confirmed"}]}), encoding="utf-8")

    assert review_ui.export_confirmed_decisions(decisions, registry) == 0
    assert json.loads(registry.read_text())["entries"] == []


def test_create_manual_identity_persists_local_registry_entry_and_aliases(tmp_path):
    registry = tmp_path / "project-registry.json"
    registry.write_text(json.dumps({"blocked_tokens": [], "ambiguous_tokens": [], "preferred_alias_targets": {}, "entries": []}))
    client = writable_app(
        audit_payload(tmp_path),
        tmp_path / "decisions.json",
        review_identities_path=tmp_path / "review-identities.json",
        registry_path=registry,
    ).test_client()

    response = client.post("/api/identities", json={
        "canonical": "manual_subject_984",
        "family": "manual",
        "aliases": ["subject_alias_984", "Friendly Subject"],
        "notes": "Created during review",
    })

    assert response.status_code == 201, response.get_json()
    assert response.get_json()["registry_added"] is True
    saved = json.loads(registry.read_text())["entries"]
    assert saved == [{
        "family": "manual",
        "canonical": "manual_subject_984",
        "aliases": ["Friendly Subject", "manual_subject_984", "subject_alias_984"],
        "notes": "[review-ui] Created during review",
    }]
    decisions = json.loads((tmp_path / "review-identities.json").read_text())["decisions"]
    assert decisions[0]["aliases"] == ["subject_alias_984", "Friendly Subject"]


def test_create_provisional_review_identity_does_not_register_it(tmp_path):
    registry = tmp_path / "project-registry.json"
    registry.write_text(json.dumps({"entries": []}))
    client = writable_app(
        audit_payload(tmp_path),
        tmp_path / "decisions.json",
        review_identities_path=tmp_path / "review-identities.json",
        registry_path=registry,
    ).test_client()

    response = client.post("/api/identities", json={"canonical": "provisional_subject_984", "family": "review"})

    assert response.status_code == 201, response.get_json()
    assert response.get_json()["registry_added"] is False
    assert json.loads(registry.read_text())["entries"] == []


def test_identity_options_preserve_distinct_canonicals_when_aliases_collide():
    rows = [
        {"canonical": "girlsplay", "family": "metadaily", "aliases": ["Niki", "Watch Girls Play"]},
        {"canonical": "wgp_niki", "family": "metadaily", "aliases": ["Niki", "wgp Niki"]},
        {"canonical": "nikinapalm", "family": "metadaily", "aliases": ["Watch Girls Play"]},
    ]
    options = review_ui._aggregate_identity_options(rows)
    assert {item["canonical"] for item in options} == {"girlsplay", "wgp_niki", "nikinapalm"}
    niki = next(item for item in options if item["canonical"] == "wgp_niki")
    assert "Niki" in niki["aliases"]


def test_identity_api_includes_wgp_member_with_aggregate_alias_collision(tmp_path, monkeypatch):
    rows = [
        review_ui.sorter.Identity("girlsplay", "metadaily", ("Niki", "Watch Girls Play")),
        review_ui.sorter.Identity("wgp_niki", "metadaily", ("Niki", "wgp Niki")),
        review_ui.sorter.Identity("nikinapalm", "metadaily", ("Watch Girls Play",)),
    ]
    monkeypatch.setattr(review_ui.sorter, "load_identity_catalog", lambda: (rows, {}, {}, {}, {}))
    client = writable_app(
        audit_payload(tmp_path),
        tmp_path / "decisions.json",
        review_identities_path=tmp_path / "review_identities.json",
    ).test_client()

    identities = client.get("/api/identities?scope=canonical").get_json()
    assert "wgp_niki" in {item["canonical"] for item in identities}


def test_paginated_cached_queue_and_member_edits(tmp_path):
    audit = audit_payload(tmp_path)
    decisions = tmp_path / "decisions.json"
    overrides = tmp_path / "overrides.json"
    app = writable_app(
        audit,
        decisions,
        overrides,
        image_decisions_path=tmp_path / "image_decisions.json",
        face_markers_path=tmp_path / "face_markers.json",
    )
    client = app.test_client()

    first_page = client.get("/api/clusters?page=1&page_size=1").get_json()
    assert first_page["page_size"] == 1
    assert first_page["has_next"] is False
    cluster_id = first_page["clusters"][0]["cluster_id"]
    path = review_ui.build_clusters(json.loads(audit.read_text()))[0]["paths"][0]
    assert audit.with_suffix(".clusters.json").exists()

    removed = client.post(f"/api/clusters/{cluster_id}/members", json={"path": path, "action": "remove"})
    assert removed.status_code == 201
    assert path not in client.get(f"/api/clusters/{cluster_id}").get_json()["paths"]
    added = client.post(f"/api/clusters/{cluster_id}/members", json={"path": path, "action": "add"})
    assert added.status_code == 201
    assert path in client.get(f"/api/clusters/{cluster_id}").get_json()["paths"]

    saved = client.post("/api/decisions", json={"cluster_id": cluster_id, "identity": "creator_a", "family": "review"})
    assert saved.status_code == 201
    assert client.delete(f"/api/decisions/{cluster_id}").status_code == 200
