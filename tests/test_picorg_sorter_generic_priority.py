from picorg_sorter import Identity, is_generic_collection_identity


def test_generic_collection_labels_are_weak_untrusted_evidence():
    assert is_generic_collection_identity(Identity("redheads", "reddit_subreddit", ()))
    assert is_generic_collection_identity(Identity("girlswithglasses", "reddit_follow", ()))


def test_curated_registry_identity_is_not_downgraded_for_descriptive_name():
    assert not is_generic_collection_identity(Identity("redheads", "metadaily", ()))
    assert not is_generic_collection_identity(Identity("freckles", "manual", ()))
