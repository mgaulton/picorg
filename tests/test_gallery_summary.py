import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import picorg_sorter as ps
from picorg_sorter import (
    MatchResult,
    build_gallery_summary,
    best_identity_match,
    dry_run_state,
    extract_downloader_hints,
    extract_reddit_context,
    gallery_base_title,
    load_dry_run_cache,
    write_dry_run_cache,
    match_cache_signature,
)


def make_result(path: str, title: str) -> MatchResult:
    return MatchResult(
        path=path,
        source_root="/mnt/desktop",
        family="redditdaily",
        canonical="example",
        confidence=1.0,
        rule="exact",
        title=title,
        source_family="manual",
    )


def test_gallery_base_title_collapses_post_download_numeric_suffixes() -> None:
    assert gallery_base_title("Scene00001") == "Scene"
    assert gallery_base_title("Scene00002") == "Scene"


def test_gallery_summary_groups_numeric_suffix_series_together() -> None:
    results = [
        make_result("/mnt/desktop/Scene00001.jpg", "Scene00001"),
        make_result("/mnt/desktop/Scene00002.jpg", "Scene00002"),
    ]

    summary = build_gallery_summary(results)

    assert len(summary) == 1
    assert summary[0]["base_title"] == "Scene"
    assert summary[0]["count"] == 2


def test_load_identity_catalog_uses_directory_entries_without_files(tmp_path, monkeypatch) -> None:
    redditdaily_root = tmp_path / "redditdaily"
    pscrape_root = tmp_path / "pscrape"
    redditdaily_root.mkdir()
    pscrape_root.mkdir()
    (redditdaily_root / "alpha_user").mkdir()
    (redditdaily_root / "ignore.txt").write_text("not a directory", encoding="utf-8")
    (pscrape_root / "beta_user").mkdir()

    registry_file = tmp_path / "project_registry.json"
    registry_file.write_text(
        json.dumps({"blocked_tokens": [], "preferred_alias_targets": {}, "entries": []}),
        encoding="utf-8",
    )
    empty_file = tmp_path / "empty.txt"
    empty_file.write_text("", encoding="utf-8")
    cache_file = tmp_path / "catalog-cache.json"

    monkeypatch.setattr(ps, "REDDITDAILY_ROOT", redditdaily_root)
    monkeypatch.setattr(ps, "PSCRAPE_ROOT", pscrape_root)
    monkeypatch.setattr(ps, "PROJECT_REGISTRY_FILE", registry_file)
    monkeypatch.setattr(ps, "FRIENDS_FILE", empty_file)
    monkeypatch.setattr(ps, "PSCRAPE_FILE", empty_file)
    monkeypatch.setattr(ps, "IMDB_FILE", empty_file)
    monkeypatch.setattr(ps, "METADAILY_ACCOUNTS_FILE", empty_file)
    monkeypatch.setattr(ps, "STRONG_TEXT_SOURCE_FILES", [])
    monkeypatch.setattr(ps, "WEAK_TEXT_SOURCE_FILES", [])
    monkeypatch.setenv("PICORG_CATALOG_CACHE", str(cache_file))

    catalog, alias_index, _, _, _ = ps.load_identity_catalog()

    assert ps.normalize_key("alpha_user") in alias_index
    assert ps.normalize_key("beta_user") in alias_index
    assert any(identity.canonical == "alpha_user" for identity in catalog)
    assert any(identity.canonical == "beta_user" for identity in catalog)

    original_scandir = ps.os.scandir

    def fail_scandir(*_args, **_kwargs):
        raise AssertionError("catalog cache should avoid rescanning the source roots")

    monkeypatch.setattr(ps.os, "scandir", fail_scandir)
    cached_catalog, cached_alias_index, _, _, _ = ps.load_identity_catalog()

    assert ps.normalize_key("alpha_user") in cached_alias_index
    assert ps.normalize_key("beta_user") in cached_alias_index
    assert [identity.canonical for identity in cached_catalog] == [identity.canonical for identity in catalog]

    monkeypatch.setattr(ps.os, "scandir", original_scandir)
    (redditdaily_root / "gamma_user").mkdir()
    refreshed_catalog, refreshed_alias_index, _, _, _ = ps.load_identity_catalog()

    assert ps.normalize_key("gamma_user") in refreshed_alias_index
    assert any(identity.canonical == "gamma_user" for identity in refreshed_catalog)


def test_aggregate_metadaily_entries_do_not_import_member_names(tmp_path, monkeypatch) -> None:
    registry_file = tmp_path / "project_registry.json"
    registry_file.write_text(
        json.dumps({"blocked_tokens": [], "preferred_alias_targets": {}, "entries": []}),
        encoding="utf-8",
    )
    aliases_file = tmp_path / "identity_aliases.json"
    aliases_file.write_text(
        json.dumps(
            {
                "identities": [
                    {
                        "id": "aggregate_folder",
                        "primary_folder": "aggregate_folder",
                        "display_names": ["Member Name"],
                        "search_terms": ["Member Name"],
                        "notes": "Aggregate identity for routing",
                        "status": "confirmed",
                        "reddit": {"users": ["aggregate_folder"], "subreddits": []},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    empty_file = tmp_path / "empty.txt"
    empty_file.write_text("", encoding="utf-8")
    monkeypatch.setattr(ps, "PROJECT_REGISTRY_FILE", registry_file)
    monkeypatch.setattr(ps, "METADAILY_IDENTITY_ALIASES_FILE", aliases_file)
    monkeypatch.setattr(ps, "FRIENDS_FILE", empty_file)
    monkeypatch.setattr(ps, "PSCRAPE_FILE", empty_file)
    monkeypatch.setattr(ps, "IMDB_FILE", empty_file)
    monkeypatch.setattr(ps, "METADAILY_ACCOUNTS_FILE", empty_file)
    monkeypatch.setattr(ps, "STRONG_TEXT_SOURCE_FILES", [])
    monkeypatch.setattr(ps, "WEAK_TEXT_SOURCE_FILES", [])
    monkeypatch.setenv("PICORG_CATALOG_CACHE", str(tmp_path / "catalog-cache.json"))

    _catalog, alias_index, *_rest = ps.load_identity_catalog()

    assert ps.normalize_key("aggregate_folder") in alias_index
    assert ps.normalize_key("Member Name") not in alias_index


def test_dry_run_cache_round_trip_and_invalidation(tmp_path) -> None:
    cache_file = tmp_path / "dry-run-cache.json"
    roots = [Path("/mnt/a"), Path("/mnt/b")]
    state = dry_run_state(roots, False)
    results = [
        make_result("/mnt/desktop/Scene00001.jpg", "Scene00001"),
        make_result("/mnt/desktop/Scene00002.jpg", "Scene00002"),
    ]
    report = {"scanned": 2, "matched": 2, "unmatched": 0, "ground_truth_accuracy": 1.0}

    write_dry_run_cache(cache_file, state, results, report)

    cached = load_dry_run_cache(cache_file, state)
    assert cached is not None
    cached_results, cached_report = cached
    assert [item.path for item in cached_results] == [item.path for item in results]
    assert cached_report["matched"] == 2

    invalid = load_dry_run_cache(cache_file, dry_run_state([Path("/mnt/a")], False))
    assert invalid is None


def test_match_cache_signature_collapses_gallery_number_variants() -> None:
    root = Path("/mnt/desktop")
    first = root / "27, mom from Iowa, would you wife me up or just a one night stand️ (1).jpg"
    second = root / "27, mom from Iowa, would you wife me up or just a one night stand️ (2).jpg"
    context = {"subreddits": [], "users": [], "context": []}

    assert match_cache_signature(first, root, context, "") == match_cache_signature(second, root, context, "")


def test_downloader_hints_extract_name_after_post_id(tmp_path) -> None:
    root = tmp_path / "jdownloaderscomplete"
    path = root / "2026-05-02_InfluencerNSFW_global_1t1c8u7_morgan_vera_00001.jpg"

    assert extract_downloader_hints(path)[:1] == ["morgan vera 00001"]


def test_reddit_context_does_not_treat_ordinary_from_or_in_as_metadata(tmp_path) -> None:
    root = tmp_path / "mixedpics"
    path = root / "A creator from Toronto in summer.jpg"

    context = extract_reddit_context(path)

    assert context["subreddits"] == []
    assert context["users"] == []


def test_ambiguous_exact_word_abstains_without_reddit_context(tmp_path, monkeypatch) -> None:
    root = tmp_path / "mixedpics"
    root.mkdir()
    path = root / "pov.jpg"
    path.write_bytes(b"")
    identity = ps.Identity("pov", "manual", ())
    alias_index = {ps.normalize_key("pov"): {identity}}
    token_index = {"pov": {identity}}
    monkeypatch.setattr(ps, "PROJECT_AMBIGUOUS_TOKENS", {"pov"})
    ps.build_identity_scoring_cache([identity])

    matched, confidence, rule = best_identity_match(
        path, root, [identity], alias_index, token_index
    )

    assert matched is None
    assert confidence == 0.0
    assert rule == "unmatched"


def test_short_profile_alias_does_not_override_caption_identity(tmp_path, monkeypatch) -> None:
    root = Path("/mnt/picorg-test-mixedpics")
    path = root / "Emma.jpg"
    identity = ps.Identity("celebswearingglasses", "metadaily", ("Emma",))
    alias_index = {
        ps.normalize_key("celebswearingglasses"): {identity},
        ps.normalize_key("Emma"): {identity},
    }
    token_index = {"celebswearingglasses": {identity}, "emma": {identity}}
    monkeypatch.setattr(ps, "PROJECT_AMBIGUOUS_TOKENS", set())
    ps.build_identity_scoring_cache([identity])

    matched, confidence, rule = best_identity_match(
        path, root, [identity], alias_index, token_index
    )

    assert matched is None
    assert confidence == 0.0
    assert rule == "unmatched"


def test_short_manual_alias_matches_exact_gallery_title(tmp_path, monkeypatch) -> None:
    root = tmp_path / "mixedpics"
    path = root / "Jameliz (1).jpg"
    identity = ps.Identity("jameliz", "manual", ())
    alias_index = {ps.normalize_key("jameliz"): {identity}}
    token_index = {"jameliz": {identity}}
    monkeypatch.setattr(ps, "PROJECT_AMBIGUOUS_TOKENS", set())
    ps.build_identity_scoring_cache([identity])

    matched, confidence, rule = best_identity_match(
        path, root, [identity], alias_index, token_index
    )

    assert matched == identity
    assert confidence == 1.0
    assert rule == "exact:jameliz"
