from pipeline_safety_gate import validate


def test_gate_rejects_low_recall():
    errors = validate({"ground_truth_precision": 1.0, "ground_truth_recall": 0.8}, 0.99, 0.99)
    assert errors == ["ground_truth_recall 0.8000 < 0.9900"]


def test_gate_accepts_thresholds():
    assert validate({"ground_truth_precision": 0.995, "ground_truth_recall": 0.99}, 0.99, 0.99) == []
