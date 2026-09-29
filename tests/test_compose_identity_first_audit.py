from compose_identity_first_audit import compose


def test_identity_matches_are_promoted_and_removed_from_generic_groups():
    payload = compose(
        {"results": [{"path": "/x/a.jpg", "title": "A"}, {"path": "/x/b.jpg", "title": "B"}]},
        {
            "results": [
                {"path": "/x/a.jpg", "face_cluster_id": "fbunknown1"},
                {"path": "/x/b.jpg", "face_cluster_id": "fbunknown2"},
            ],
            "report": {"errors": 0},
        },
        {"audit": "name.json", "results": [{"path": "/x/a.jpg", "status": "matched", "matched_identity": "Alice", "face_count": 2, "confident_face_count": 1, "multi_face_policy": "one_known_other_unresolved"}]},
    )

    assert payload["cluster_policy"] == "identity-first-face-then-generic"
    assert payload["report"]["identity_first_matches"] == 1
    assert payload["report"]["generic_face_results"] == 1
    assert payload["results"][0]["review_method"] == "face-identity"
    assert payload["results"][0]["expected_identity"] == "Alice"
    assert payload["results"][0]["multi_face_policy"] == "one_known_other_unresolved"
    assert payload["results"][1]["path"] == "/x/b.jpg"
