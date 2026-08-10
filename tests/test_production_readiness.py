from production_readiness import check


def test_readiness_requires_accuracy_auth_and_clean_preflight():
    errors = check(
        {"ground_truth_precision": 1.0, "ground_truth_recall": 0.9},
        {"counts": {"corrupt": 2}},
        min_precision=0.99,
        min_recall=0.99,
        ui_token="",
    )
    assert "ground_truth_recall 0.9000 < 0.9900" in errors
    assert "preflight corrupt=2" in errors
    assert "PICORG_UI_TOKEN is not configured for LAN exposure" in errors


def test_readiness_passes_clean_run():
    assert check(
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
    ) == []


def test_readiness_requires_insightface_model_license_confirmation():
    errors = check(
        {"ground_truth_precision": 1.0, "ground_truth_recall": 1.0},
        {"counts": {}},
        min_precision=0.99,
        min_recall=0.99,
        ui_token="secret",
        backend="insightface",
    )
    assert "InsightFace model license has not been confirmed for this deployment" in errors


def test_readiness_rejects_small_benchmark_and_wide_intervals():
    errors = check(
        {"ground_truth_precision": 1.0, "ground_truth_recall": 1.0},
        {"counts": {}},
        min_precision=0.99,
        min_recall=0.99,
        ui_token="secret",
        benchmark={"genuine_pairs": 2, "impostor_pairs": 3, "selected": {"fmr_ci95": [0.0, 0.5], "fnmr_ci95": [0.0, 0.5]}},
    )
    assert "genuine_pairs 2 < 100" in errors
    assert "impostor_pairs 3 < 100" in errors
    assert "fmr_ci95 upper bound exceeds 0.0100" in errors
