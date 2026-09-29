from pipeline_safety_gate import validate, validate_benchmark, validate_name_moves


def test_gate_rejects_low_recall():
    errors = validate({"ground_truth_precision": 1.0, "ground_truth_recall": 0.8}, 0.99, 0.99)
    assert errors == ["ground_truth_recall 0.8000 < 0.9900"]


def test_gate_accepts_thresholds():
    assert validate({"ground_truth_precision": 0.995, "ground_truth_recall": 0.99}, 0.99, 0.99) == []


def test_name_move_gate_allows_high_precision_low_coverage():
    assert validate_name_moves({"ground_truth_precision": 1.0, "ground_truth_recall": 0.5, "high_confidence": 3}, 0.99) == []


def test_name_move_gate_rejects_empty_high_confidence_set():
    assert validate_name_moves({"ground_truth_precision": 1.0, "high_confidence": 0}, 0.99) == ["high_confidence 0 is empty"]


def test_benchmark_gate_rejects_missing_sample_counts_and_wide_fnmr():
    errors = validate_benchmark(
        {"genuine_pairs": 10, "impostor_pairs": 20, "selected": {"fmr": 0.0, "fmr_ci95": [0.0, 0.001], "fnmr_ci95": [0.1, 0.2]}},
        max_fmr=0.001,
        max_fmr_upper=0.001,
        max_fnmr_upper=0.05,
        min_genuine=100,
        min_impostor=1000,
    )
    assert errors == [
        "benchmark genuine_pairs 10 < 100",
        "benchmark impostor_pairs 20 < 1000",
        "benchmark selected fnmr_ci95 upper 0.200000 > 0.050000",
    ]


def test_benchmark_gate_accepts_valid_report():
    assert validate_benchmark(
        {"genuine_pairs": 100, "impostor_pairs": 1000, "selected": {"fmr": 0.0005, "fmr_ci95": [0.0, 0.001], "fnmr_ci95": [0.01, 0.04]}},
        max_fmr=0.001,
        max_fmr_upper=0.001,
        max_fnmr_upper=0.05,
        min_genuine=100,
        min_impostor=1000,
    ) == []
