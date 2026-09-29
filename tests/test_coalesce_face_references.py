import io
import time
import json
from types import SimpleNamespace

import pytest

from PIL import Image

import coalesce_face_references as coalescer


def test_confirmed_external_keys_require_explicit_confirmation(tmp_path):
    registry = tmp_path / "confirmed.json"
    registry.write_text(
        json.dumps(
            {
                "markers": [{"status": "confirmed", "identity": "Alice"}],
                "identities": [
                    {"status": "confirmed", "id": "bob", "primary_folder": "BobFolder"},
                    {"status": "pending", "id": "pending"},
                ],
            }
        ),
        encoding="utf-8",
    )
    canonical = {
        "alice": SimpleNamespace(canonical="alice"),
        "bob": SimpleNamespace(canonical="bob"),
        "bobfolder": SimpleNamespace(canonical="bob"),
    }
    raw, canonical_keys, loaded = coalescer.load_confirmed_identity_keys([registry], canonical, {})
    assert {"alice", "bob", "bobfolder"} <= raw
    assert canonical_keys == {"alice", "bob"}
    assert loaded == [str(registry)]


def test_confirmed_assignment_images_seed_canonical_reference_paths(tmp_path):
    image = tmp_path / "unsorted" / "new-image.jpg"
    image.parent.mkdir()
    image.write_bytes(b"image")
    missing = tmp_path / "missing.jpg"
    decisions = tmp_path / "decisions.json"
    decisions.write_text(
        json.dumps({"decisions": [
            {"status": "confirmed", "identity": "AliceAlias", "path": str(image)},
            {"status": "pending", "identity": "AliceAlias", "path": str(image)},
            {"status": "confirmed", "identity": "AliceAlias", "path": str(missing)},
        ]}),
        encoding="utf-8",
    )
    identity = coalescer.sorter.Identity("alice", "review", ("AliceAlias",))
    canonical = {"alice": identity}
    aliases = {"alicealias": {identity}}

    references = coalescer.load_confirmed_reference_paths([decisions], canonical, aliases, (tmp_path / "covered",))

    assert references == {"alice": {image}}


def test_confirmed_references_already_covered_by_source_roots_are_not_restated(tmp_path):
    image = tmp_path / "sorted" / "alice" / "image.jpg"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"image")
    decisions = tmp_path / "decisions.json"
    decisions.write_text(json.dumps({"decisions": [
        {"status": "confirmed", "identity": "Alice", "path": str(image)}
    ]}), encoding="utf-8")
    identity = coalescer.sorter.Identity("alice", "review", ())

    references = coalescer.load_confirmed_reference_paths(
        [decisions], {"alice": identity}, {}, (tmp_path / "sorted",)
    )

    assert references == {}


def test_root_scan_timeout_fails_closed():
    with pytest.raises(coalescer.RootScanTimeout):
        with coalescer.root_scan_timeout(1, "test-root"):
            time.sleep(1.1)


def test_repair_html_image_replaces_only_allowlisted_reddit_html(tmp_path, monkeypatch):
    source = tmp_path / "bad.jpg"
    source.write_text(
        '<!doctype html><meta property="og:image" content="https://preview.redd.it/fixed.jpg">',
        encoding="utf-8",
    )
    image_bytes = io.BytesIO()
    Image.new("RGB", (2, 2), "red").save(image_bytes, format="JPEG")

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    monkeypatch.setattr(coalescer, "urlopen", lambda *_args, **_kwargs: Response(image_bytes.getvalue()))
    destination = tmp_path / "staging" / "fixed.jpg"
    assert coalescer.repair_html_image(source, destination=destination) == "repaired"
    assert source.read_text(encoding="utf-8").startswith("<!doctype html")
    with Image.open(destination) as image:
        assert image.size == (2, 2)


def test_repair_html_image_rejects_untrusted_url(tmp_path):
    source = tmp_path / "bad.jpg"
    source.write_text('<html><meta property="og:image" content="https://example.com/x.jpg">', encoding="utf-8")
    assert coalescer.repair_html_image(source, destination=tmp_path / "staging" / "rejected.jpg") == "url_not_allowed"


def test_repair_html_image_rejects_untrusted_redirect(tmp_path, monkeypatch):
    source = tmp_path / "bad.jpg"
    source.write_text('<html><meta property="og:image" content="https://preview.redd.it/x.jpg">', encoding="utf-8")

    class Response(io.BytesIO):
        def geturl(self):
            return "https://evil.example/x.jpg"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    monkeypatch.setattr(coalescer, "urlopen", lambda *_args, **_kwargs: Response(b"ignored"))
    assert coalescer.repair_html_image(source, destination=tmp_path / "staging" / "redirect.jpg") == "redirect_not_allowed"


def test_policy_prefers_local_copy_and_records_quarantine(tmp_path):
    source = tmp_path / "downloads" / "bad.jpg"
    source.parent.mkdir()
    source.write_text('<html><meta property="og:image" content="https://preview.redd.it/unused.jpg">', encoding="utf-8")
    local_root = tmp_path / "priority"
    local_root.mkdir()
    local = local_root / "bad.jpg"
    Image.new("RGB", (3, 3), "blue").save(local)
    ledger = tmp_path / "repair.jsonl"
    quarantine = tmp_path / "quarantine"
    staging = tmp_path / "staging"
    status = coalescer.repair_html_image_with_policy(
        source,
        search_roots=(local_root,),
        quarantine_root=quarantine,
        ledger_path=ledger,
        repair_output_root=staging,
        repair_run_id="test-run",
    )
    assert status == "repaired_local"
    assert (quarantine).is_dir()
    assert ledger.read_text(encoding="utf-8").find('"status": "repaired_local"') >= 0
    assert ledger.read_text(encoding="utf-8").find('"run_id": "test-run"') >= 0
    with Image.open(next(staging.iterdir())) as image:
        assert image.size == (3, 3)
    assert source.read_text(encoding="utf-8").startswith("<html")


def test_policy_does_not_hash_normal_media(tmp_path, monkeypatch):
    source = tmp_path / "valid.jpg"
    Image.new("RGB", (2, 2), "green").save(source)
    monkeypatch.setattr(coalescer, "_sha256_file", lambda _path: (_ for _ in ()).throw(AssertionError("unexpected hash")))
    assert (
        coalescer.repair_html_image_with_policy(
            source,
            repair_output_root=tmp_path / "staging",
        )
        == "not_html"
    )


def test_policy_repairs_corrupt_media_from_verified_local_copy(tmp_path):
    source = tmp_path / "source" / "person" / "bad.jpg"
    replacement = tmp_path / "search" / "person" / "bad.jpg"
    source.parent.mkdir(parents=True)
    replacement.parent.mkdir(parents=True)
    source.write_bytes(b"not-an-image")
    Image.new("RGB", (4, 4), "blue").save(replacement)
    staging = tmp_path / "staging"
    quarantine = tmp_path / "quarantine"
    ledger = tmp_path / "repair.jsonl"

    status = coalescer.repair_html_image_with_policy(
        source,
        repair_corrupt=True,
        search_roots=(tmp_path / "search",),
        quarantine_root=quarantine,
        ledger_path=ledger,
        repair_output_root=staging,
        repair_run_id="corrupt-test",
    )

    assert status == "repaired_corrupt_local"
    staged = next(staging.iterdir())
    with Image.open(staged) as image:
        assert image.size == (4, 4)
    assert source.read_bytes() == b"not-an-image"
    assert any(quarantine.iterdir())
    assert '"status": "repaired_corrupt_local"' in ledger.read_text(encoding="utf-8")


def test_policy_scan_cache_skips_unchanged_normal_media(tmp_path, monkeypatch):
    source = tmp_path / "valid.jpg"
    Image.new("RGB", (2, 2), "green").save(source)
    cache = {}
    assert coalescer.repair_html_image_with_policy(
        source,
        repair_output_root=tmp_path / "staging",
        repair_scan_cache=cache,
    ) == "not_html"
    monkeypatch.setattr(coalescer.Path, "read_bytes", lambda _path: (_ for _ in ()).throw(AssertionError("unexpected rescan")))
    assert coalescer.repair_html_image_with_policy(
        source,
        repair_output_root=tmp_path / "staging",
        repair_scan_cache=cache,
    ) == "not_html_cached"


def test_policy_records_and_skips_stat_io_error(tmp_path, monkeypatch):
    source = tmp_path / "damaged.jpg"
    source.write_bytes(b"not readable")
    ledger = tmp_path / "repair.jsonl"
    real_is_symlink = coalescer.Path.is_symlink

    def raise_io_error(path):
        if path == source:
            raise OSError(5, "Input/output error", str(path))
        return real_is_symlink(path)

    monkeypatch.setattr(coalescer.Path, "is_symlink", raise_io_error)
    status = coalescer.repair_html_image_with_policy(
        source,
        ledger_path=ledger,
        repair_output_root=tmp_path / "staging",
        repair_run_id="io-test",
    )
    assert status == "unreadable_stat"
    record = ledger.read_text(encoding="utf-8")
    assert '"status": "unreadable_stat"' in record
    assert '"run_id": "io-test"' in record


def test_media_files_skips_unreadable_repair_status(tmp_path, monkeypatch):
    root = tmp_path / "identity"
    root.mkdir()
    source = root / "damaged.jpg"
    source.write_bytes(b"placeholder")
    monkeypatch.setattr(coalescer, "repair_html_image_with_policy", lambda *_args, **_kwargs: "unreadable_stat")
    skipped = set()
    files = list(
        coalescer.media_files(
            root,
            skipped,
            repair_html=True,
            repair_stats={},
            repair_output_root=tmp_path / "staging",
        )
    )
    assert files == []
    assert str(source) in skipped
