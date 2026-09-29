from production_readiness import check


def test_readiness_can_require_durable_marker_hashes():
    errors = check(
        {"ground_truth_precision": 1.0, "ground_truth_recall": 1.0},
        {"counts": {}},
        min_precision=0.99,
        min_recall=0.99,
        ui_token="secret",
        benchmark={
            "genuine_pairs": 100,
            "impostor_pairs": 100,
            "selected": {"fmr_ci95": [0.0, 0.01], "fnmr_ci95": [0.0, 0.01]},
        },
        marker_health={"confirmed": 10, "confirmed_with_sha256": 9},
    )
    assert "face marker hash coverage 0.9000 < 1.0000" in errors
