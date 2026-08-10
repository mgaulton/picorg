from split_face_pairs import split_pairs


def test_split_is_deterministic_and_image_disjoint():
    pairs = [
        {"path_a": "a1", "path_b": "a2", "label": "genuine", "identity": "alice"},
        {"path_a": "b1", "path_b": "b2", "label": "genuine", "identity": "bob"},
        {"path_a": "a1", "path_b": "b1", "label": "impostor", "identity_a": "alice", "identity_b": "bob"},
    ]
    train, heldout = split_pairs(pairs, fraction=0.5, seed="test")
    assert split_pairs(pairs, fraction=0.5, seed="test") == (train, heldout)
    heldout_paths = {path for pair in heldout for path in (pair["path_a"], pair["path_b"])}
    assert all(path not in heldout_paths for pair in train for path in (pair["path_a"], pair["path_b"]))
