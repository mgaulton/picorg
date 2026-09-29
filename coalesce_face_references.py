#!/usr/bin/env python3
"""Coalesce sorted, Metadaily, and Redditdaily media into identity folders.

The output contains symlinks only.  It is shaped for photo_reorg's face
database builder, which expects ``<source>/<identity>/<image>`` and does not
understand PicOrg's nested ``<family>/<identity>`` destination layout.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
from html import unescape
from html.parser import HTMLParser
import json
import os
import re
import shutil
import signal
import sqlite3
import stat
import sys
import tempfile
import time
import warnings
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from pathlib import Path

import picorg_sorter as sorter


EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic", ".tif", ".tiff"}
THUMBNAIL_PATTERN = re.compile(r"(?:^|[_ .-])(?:thumb(?:nail)?|preview|teaser|lowres|small|\d{2,4}px|\d{2,4}x\d{2,4})(?:$|[_ .-])", re.I)
FAST_MEDIA_SCAN = os.environ.get("PICORG_FAST_MEDIA_SCAN", "1") == "1"
REPAIR_IMAGE_HOSTS = {"preview.redd.it", "i.redd.it", "external-preview.redd.it"}
MAX_REPAIR_BYTES = 25 * 1024 * 1024
DEFAULT_ROOT_TIMEOUT_SECONDS = 3600
DEFAULT_PROGRESS_SECONDS = 15.0


class RootScanTimeout(RuntimeError):
    """Raised when a source root stops responding during coalescing."""


class CoalesceProgress:
    """Emit bounded heartbeats while a source tree is being traversed."""

    def __init__(self, interval: float = DEFAULT_PROGRESS_SECONDS) -> None:
        self.interval = max(1.0, float(interval))
        self.started = time.monotonic()
        self.last_report = self.started
        self.files = 0

    def note(self, root: Path, identity: str) -> None:
        self.files += 1
        now = time.monotonic()
        if self.files == 1 or now - self.last_report >= self.interval:
            elapsed = max(0.1, now - self.started)
            rate = self.files / elapsed
            print(
                f"[coalesce] heartbeat root={root.name} identity={identity} "
                f"files={self.files} elapsed={elapsed:.0f}s rate={rate:.1f}/s",
                file=sys.stderr,
                flush=True,
            )
            self.last_report = now

    def done(self, root: Path, identity: str) -> None:
        elapsed = max(0.1, time.monotonic() - self.started)
        print(
            f"[coalesce] identity complete root={root.name} identity={identity} "
            f"files={self.files} elapsed={elapsed:.0f}s",
            file=sys.stderr,
            flush=True,
        )


@contextmanager
def root_scan_timeout(seconds: int, label: str):
    """Bound one source-root scan; never silently publish partial references."""
    if seconds <= 0:
        yield
        return

    def _alarm(_signum, _frame):  # type: ignore[no-untyped-def]
        raise RootScanTimeout(f"source root timed out after {seconds}s: {label}")

    previous = signal.signal(signal.SIGALRM, _alarm)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


class _ImageMetaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.image_url: str | None = None

    def handle_starttag(self, tag: str, attrs) -> None:  # type: ignore[no-untyped-def]
        if tag.lower() != "meta" or self.image_url:
            return
        values = {str(key).lower(): str(value) for key, value in attrs if key and value}
        name = values.get("property", values.get("name", "")).lower()
        if name in {"og:image", "twitter:image"} and values.get("content"):
            self.image_url = unescape(values["content"]).strip()


def repair_html_image(path: Path, *, destination: Path | None = None) -> str:
    """Stage an allow-listed Reddit image for a local HTML error page.

    ``destination`` is mandatory so repair cannot overwrite a protected
    source file by accident. The source is only read and checked for races;
    the verified image is atomically installed in the staging tree.
    """
    if destination is None:
        return "repair_destination_required"
    destination = Path(destination)
    try:
        same_path = destination.resolve() == path.resolve()
    except OSError:
        same_path = destination == path
    if same_path or destination.is_symlink():
        return "repair_destination_invalid"
    if path.is_symlink() or not path.is_file():
        return "not_file"
    try:
        original = path.stat()
        with path.open("rb") as stream:
            prefix = stream.read(64 * 1024)
    except OSError:
        return "unreadable"
    normalized = prefix.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if not (normalized.startswith(b"<!doctype html") or normalized.startswith(b"<html") or b"<meta" in normalized[:4096]):
        return "not_html"
    parser = _ImageMetaParser()
    try:
        parser.feed(prefix.decode("utf-8", "replace"))
    except Exception:
        return "invalid_html"
    raw_url = parser.image_url
    if not raw_url:
        return "no_image_url"
    parsed = urlparse(raw_url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in REPAIR_IMAGE_HOSTS:
        return "url_not_allowed"
    temporary: str | None = None
    try:
        request = Request(raw_url, headers={"Accept": "image/*", "User-Agent": "PicOrg/1.0 local repair"})
        with urlopen(request, timeout=20) as response:
            final_url = str(response.geturl()) if hasattr(response, "geturl") else raw_url
            final = urlparse(final_url)
            if final.scheme != "https" or (final.hostname or "").lower() not in REPAIR_IMAGE_HOSTS:
                return "redirect_not_allowed"
            response_status = getattr(response, "status", None)
            if response_status is not None and int(response_status) >= 400:
                return "http_error"
            headers = getattr(response, "headers", None)
            content_type = headers.get_content_type() if headers is not None and hasattr(headers, "get_content_type") else ""
            if content_type and not content_type.startswith("image/"):
                return "content_type_not_image"
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary_file = tempfile.NamedTemporaryFile(prefix=f".{destination.name}.picorg-repair-", dir=destination.parent, delete=False)
            temporary = temporary_file.name
            total = 0
            with temporary_file:
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_REPAIR_BYTES:
                        return "download_too_large"
                    temporary_file.write(chunk)
        from PIL import Image
        with Image.open(temporary) as image:
            image.verify()
        current = path.stat()
        if (current.st_size, current.st_mtime_ns) != (original.st_size, original.st_mtime_ns):
            return "source_changed"
        os.chmod(temporary, stat.S_IMODE(original.st_mode))
        os.replace(temporary, destination)
        temporary = None
        return "repaired"
    except Exception:
        return "repair_failed"
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _valid_image(path: Path) -> bool:
    try:
        from PIL import Image
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always")
            with Image.open(path) as image:
                image.verify()
            with Image.open(path) as image:
                image.load()
            for warning in captured:
                message = str(warning.message).lower()
                if any(token in message for token in ("corrupt", "truncated", "premature end", "broken")):
                    return False
        return True
    except Exception:
        return False


def _find_local_copy(path: Path, search_roots: tuple[Path, ...]) -> Path | None:
    """Return a unique valid same-name copy before using the network."""
    candidates: list[Path] = []
    for root in search_roots:
        for candidate in (root / path.parent.name / path.name, root / path.name):
            try:
                if candidate != path and candidate.is_file() and _valid_image(candidate):
                    candidates.append(candidate)
            except OSError:
                continue
        if candidates:
            break
    return candidates[0] if len(candidates) == 1 else None


def _append_repair_ledger(path: Path | None, record: dict[str, object]) -> None:
    if not path:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError:
        pass


def repair_html_image_with_policy(
    path: Path,
    *,
    repair_corrupt: bool = False,
    search_roots: tuple[Path, ...] = (),
    quarantine_root: Path | None = None,
    ledger_path: Path | None = None,
    repair_output_root: Path | None = None,
    repair_run_id: str | None = None,
    repair_scan_cache: dict[str, dict[str, int | str]] | None = None,
) -> str:
    """Repair HTML media with local-copy recovery, quarantine, and a ledger."""
    if repair_output_root is None:
        return "repair_destination_required"
    try:
        if path.is_symlink() or not path.is_file():
            return "not_file"
    except OSError as exc:
        _append_repair_ledger(
            ledger_path,
            {
                "path": str(path),
                "status": "unreadable_stat",
                "error": str(exc),
                "errno": exc.errno,
                "timestamp": time.time(),
                **({"run_id": repair_run_id} if repair_run_id else {}),
            },
        )
        return "unreadable_stat"
    try:
        original = path.stat()
        cached = repair_scan_cache.get(str(path)) if repair_scan_cache is not None else None
        if (
            isinstance(cached, dict)
            and not repair_corrupt
            and cached.get("status") == "not_html"
            and all(cached.get(key) == getattr(original, key) for key in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns"))
        ):
            return "not_html_cached"
        prefix = path.read_bytes()[:64 * 1024]
    except OSError as exc:
        _append_repair_ledger(
            ledger_path,
            {
                "path": str(path),
                "status": "unreadable",
                "error": str(exc),
                "errno": exc.errno,
                "timestamp": time.time(),
                **({"run_id": repair_run_id} if repair_run_id else {}),
            },
        )
        return "unreadable"
    normalized = prefix.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    is_html = normalized.startswith(b"<!doctype html") or normalized.startswith(b"<html") or b"<meta" in normalized[:4096]
    source_kind = "html" if is_html else "corrupt"
    if not is_html and not repair_corrupt:
        if repair_scan_cache is not None:
            repair_scan_cache[str(path)] = {
                "status": "not_html",
                "st_dev": original.st_dev,
                "st_ino": original.st_ino,
                "st_size": original.st_size,
                "st_mtime_ns": original.st_mtime_ns,
                "st_ctime_ns": original.st_ctime_ns,
            }
        return "not_html"
    if not is_html:
        try:
            if _valid_image(path):
                if repair_scan_cache is not None:
                    repair_scan_cache[str(path)] = {
                        "st_size": original.st_size,
                        "st_mtime_ns": original.st_mtime_ns,
                        "st_ctime_ns": original.st_ctime_ns,
                    }
                return "not_corrupt"
        except OSError as exc:
            _append_repair_ledger(ledger_path, {"path": str(path), "status": "unreadable", "error": str(exc), "errno": exc.errno, "timestamp": time.time(), **({"run_id": repair_run_id} if repair_run_id else {})})
            return "unreadable"
    # Hash only confirmed HTML candidates. Normal images are the common case;
    # hashing them before classification caused a full extra read per file.
    try:
        old_sha256 = _sha256_file(path)
    except OSError as exc:
        _append_repair_ledger(
            ledger_path,
            {
                "path": str(path),
                "status": "unreadable",
                "error": str(exc),
                "errno": exc.errno,
                "timestamp": time.time(),
                **({"run_id": repair_run_id} if repair_run_id else {}),
            },
        )
        return "unreadable"
    destination = repair_output_root / f"{hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:20]}_{safe_name(path.name)}"
    record: dict[str, object] = {
        "path": str(path),
        "output_path": str(destination),
        "old_sha256": old_sha256,
        "timestamp": time.time(),
    }
    if repair_run_id:
        record["run_id"] = repair_run_id
    if quarantine_root:
        try:
            quarantine_root.mkdir(parents=True, exist_ok=True)
            backup = quarantine_root / f"{old_sha256}_{safe_name(path.name)}.{source_kind}"
            if not backup.exists():
                shutil.copy2(path, backup)
            record["quarantine"] = str(backup)
        except OSError:
            pass
    local_copy = _find_local_copy(path, search_roots)
    status = ""
    if local_copy:
        temporary: str | None = None
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary_file = tempfile.NamedTemporaryFile(prefix=f".{destination.name}.picorg-repair-", dir=destination.parent, delete=False)
            temporary = temporary_file.name
            with temporary_file:
                shutil.copyfile(local_copy, temporary)
            if _valid_image(Path(temporary)):
                current = path.stat()
                if (current.st_size, current.st_mtime_ns) != (original.st_size, original.st_mtime_ns):
                    return "source_changed"
                os.replace(temporary, destination)
                temporary = None
                status = "repaired_local" if source_kind == "html" else "repaired_corrupt_local"
                record["source_path"] = str(local_copy)
            else:
                status = "local_copy_invalid"
        except OSError:
            status = "local_copy_failed"
        finally:
            if temporary:
                Path(temporary).unlink(missing_ok=True)
    if not status:
        status = repair_html_image(path, destination=destination) if source_kind == "html" else "corrupt_no_replacement"
    if status.startswith("repaired"):
        try:
            record["new_sha256"] = _sha256_file(destination)
        except OSError:
            pass
    record["status"] = status
    _append_repair_ledger(ledger_path, record)
    return status


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._") or "unknown"


def _confirmed_values(payload: object) -> list[str]:
    """Return names explicitly confirmed in a marker or identity registry."""
    if not isinstance(payload, dict):
        return []
    values: list[str] = []
    for row in payload.get("markers", []):
        if isinstance(row, dict) and row.get("status") == "confirmed" and row.get("identity"):
            values.append(str(row["identity"]))
    for row in payload.get("decisions", []):
        if isinstance(row, dict) and row.get("status") == "confirmed" and row.get("identity"):
            values.append(str(row["identity"]))
    for row in payload.get("identities", []):
        if not isinstance(row, dict) or row.get("status") != "confirmed":
            continue
        for key in ("id", "primary_folder"):
            if row.get(key):
                values.append(str(row[key]))
        for key in ("display_names", "search_terms"):
            values.extend(str(value) for value in (row.get(key) or []) if value)
        reddit = row.get("reddit")
        if isinstance(reddit, dict):
            for key in ("users", "subreddits"):
                values.extend(str(value) for value in (reddit.get(key) or []) if value)
    return values


def load_confirmed_identity_keys(paths: list[Path], canonical_index, alias_index) -> tuple[set[str], set[str], list[str]]:
    """Load confirmed keys without modifying any source tree."""
    raw_keys: set[str] = set()
    canonical_keys: set[str] = set()
    loaded: list[str] = []
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"warning: cannot read confirmed identity file {path}: {exc}", file=sys.stderr)
            continue
        values = _confirmed_values(payload)
        if values:
            loaded.append(str(path))
        for value in values:
            key = sorter.normalize_key(value)
            if not key:
                continue
            raw_keys.add(key)
            identity = canonical_index.get(key)
            if identity is None:
                aliases = alias_index.get(key, set())
                identity = next(iter(aliases)) if len(aliases) == 1 else None
            if identity is not None and not sorter.is_generic_identity_token(identity.canonical):
                canonical_keys.add(sorter.normalize_key(identity.canonical))
    return raw_keys, canonical_keys, loaded


def load_confirmed_reference_paths(
    paths: list[Path], canonical_index, alias_index, covered_roots: tuple[Path, ...] = ()
) -> dict[str, set[Path]]:
    """Return existing image paths explicitly assigned by a human decision."""
    grouped: dict[str, set[Path]] = {}
    for source in paths:
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"warning: cannot read confirmed references {source}: {exc}", file=sys.stderr)
            continue
        if not isinstance(payload, dict):
            continue
        rows = [*payload.get("markers", []), *payload.get("decisions", [])]
        for row in rows:
            if not isinstance(row, dict) or str(row.get("status") or "") != "confirmed":
                continue
            identity_key = sorter.normalize_key(str(row.get("identity") or ""))
            if not identity_key:
                continue
            identity = canonical_index.get(identity_key)
            if identity is None:
                aliases = alias_index.get(identity_key, set())
                identity = next(iter(aliases)) if len(aliases) == 1 else None
            if identity is None or sorter.is_generic_identity_token(identity.canonical):
                continue
            candidates = [row.get("canonical_path"), row.get("path")]
            image_path = None
            for candidate in candidates:
                if not candidate:
                    continue
                path = Path(str(candidate))
                if path.suffix.lower() not in EXTENSIONS or THUMBNAIL_PATTERN.search(path.stem):
                    continue
                absolute = os.path.abspath(path)
                if any(
                    os.path.commonpath((absolute, os.path.abspath(root))) == os.path.abspath(root)
                    for root in covered_roots
                ):
                    continue
                try:
                    if path.is_file():
                        image_path = path
                        break
                except OSError:
                    continue
            if image_path is not None:
                grouped.setdefault(identity.canonical, set()).add(image_path)
    return grouped


def load_confirmed_identity_db(path: Path) -> tuple[set[str], set[str], dict[str, str]]:
    """Read confirmed aliases/canonicals and their mapping from the durable store."""
    connection = sqlite3.connect(path)
    try:
        canonical = {
            str(row[0]) for row in connection.execute(
                "SELECT canonical FROM identities WHERE status = 'confirmed'"
            )
        }
        aliases = {}
        for alias_key, identity in connection.execute(
            "SELECT alias_key, identity FROM aliases WHERE identity IN (SELECT canonical FROM identities WHERE status = 'confirmed')"
        ):
            alias = sorter.normalize_key(str(alias_key))
            target = sorter.normalize_key(str(identity))
            if alias and target:
                aliases.setdefault(alias, target)
        return set(aliases), canonical, aliases
    finally:
        connection.close()


def media_files(
    root: Path,
    skipped: set[str],
    repair_html: bool = False,
    repair_stats: dict[str, int] | None = None,
    repair_search_roots: tuple[Path, ...] = (),
    repair_quarantine: Path | None = None,
    repair_ledger: Path | None = None,
    repair_output_root: Path | None = None,
    repair_run_id: str | None = None,
    repair_scan_cache: dict[str, dict[str, int | str]] | None = None,
    repair_corrupt: bool = False,
    progress: CoalesceProgress | None = None,
):
    try:
        available = root.is_dir()
    except OSError as exc:
        print(f"warning: skipping unreadable directory: {root}: {exc.strerror}", file=sys.stderr)
        return
    if not available:
        return
    def report_error(exc: OSError) -> None:
        print(f"warning: skipping unreadable directory: {exc.filename}: {exc.strerror}", file=sys.stderr)

    for current, directories, files in os.walk(root, topdown=True, followlinks=False, onerror=report_error):
        directories[:] = [name for name in directories if not name.startswith(".")]
        for name in files:
            if Path(name).suffix.lower() not in EXTENSIONS:
                continue
            if THUMBNAIL_PATTERN.search(Path(name).stem):
                continue
            path = Path(current) / name
            if progress is not None:
                progress.note(root, root.name)
            if str(path) in skipped:
                continue
            if repair_html or repair_corrupt:
                repair_status = repair_html_image_with_policy(
                    path,
                    repair_corrupt=repair_corrupt,
                    search_roots=repair_search_roots,
                    quarantine_root=repair_quarantine,
                    ledger_path=repair_ledger,
                    repair_output_root=repair_output_root,
                    repair_run_id=repair_run_id,
                    repair_scan_cache=repair_scan_cache,
                )
                if repair_stats is not None:
                    repair_stats[repair_status] = repair_stats.get(repair_status, 0) + 1
                if repair_status.startswith("repaired"):
                    print(f"[repair] staged replacement for {repair_status.removeprefix('repaired_')}: {path}")
                elif repair_status in {"unreadable", "unreadable_stat", "not_file", "corrupt_no_replacement"}:
                    print(f"warning: skipping unreadable media: {path} ({repair_status})", file=sys.stderr)
                    skipped.add(str(path))
                    continue
            # os.walk already classified this entry as a file. Avoid a second
            # network stat by default; photo_reorg performs the authoritative
            # decode/read check and reports unreadable media. Set
            # PICORG_FAST_MEDIA_SCAN=0 to retain the stricter pre-stat pass.
            if not FAST_MEDIA_SCAN:
                try:
                    readable = path.is_file()
                except OSError as exc:
                    print(f"warning: skipping unreadable file: {path}: {exc.strerror}", file=sys.stderr)
                    skipped.add(str(path))
                    continue
                if not readable:
                    skipped.add(str(path))
                    continue
            if repair_html and repair_status.startswith("repaired"):
                yield repair_output_root / f"{hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:20]}_{safe_name(path.name)}"
            else:
                yield path
    if progress is not None:
        progress.done(root, root.name)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sorted-root", type=Path, required=True)
    parser.add_argument("--metadaily-root", type=Path, required=True)
    parser.add_argument("--redditdaily-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--skip-path", action="append", default=[])
    parser.add_argument("--repair-html", action="store_true", help="repair HTML error pages saved with image extensions from allow-listed Reddit image URLs")
    parser.add_argument("--repair-corrupt", action="store_true", help="replace invalid/corrupt images from a verified same-name local copy")
    parser.add_argument("--repair-search-root", type=Path, action="append", default=[], help="priority root to search for a valid same-name local copy before downloading")
    parser.add_argument("--repair-quarantine", type=Path, help="directory for quarantined HTML originals")
    parser.add_argument("--repair-ledger", type=Path, help="append-only JSONL repair ledger")
    parser.add_argument("--repair-output-root", type=Path, help="staging directory for repaired images; source roots remain read-only")
    parser.add_argument("--repair-run-id", help="stable identifier written to repair-ledger records")
    parser.add_argument("--repair-scan-cache", type=Path, help="metadata cache for unchanged non-HTML media classification")
    parser.add_argument("--confirmed-only-external", action="store_true", help="use MD/RD folders only for explicitly confirmed identities")
    parser.add_argument("--confirmed-identities-file", type=Path, action="append", default=[], help="confirmed marker or identity registry JSON")
    parser.add_argument("--confirmed-reference-file", type=Path, action="append", default=[], help="human-confirmed marker/decision JSON whose existing images should seed face references")
    parser.add_argument("--confirmed-identities-db", type=Path, help="durable PicOrg identity-evidence SQLite store")
    parser.add_argument("--root-timeout", type=int, default=DEFAULT_ROOT_TIMEOUT_SECONDS, help="fail closed if one source root stops responding (0 disables)")
    parser.add_argument("--progress-seconds", type=float, default=DEFAULT_PROGRESS_SECONDS, help="emit coalescing heartbeats at least this often")
    parser.add_argument(
        "--skip-root",
        action="append",
        default=[],
        help="explicitly omit a source root; requires --allow-partial-roots and is recorded in the report",
    )
    parser.add_argument(
        "--allow-partial-roots",
        action="store_true",
        help="write usable references from completed roots while recording timed-out/unavailable roots",
    )
    args = parser.parse_args()

    repair_scan_cache: dict[str, dict[str, int | str]] = {}
    if args.repair_scan_cache and args.repair_scan_cache.is_file():
        try:
            loaded = json.loads(args.repair_scan_cache.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                repair_scan_cache = {str(key): value for key, value in loaded.items() if isinstance(value, dict)}
        except (OSError, json.JSONDecodeError):
            repair_scan_cache = {}

    catalog, alias_index, canonical_index, _, _ = sorter.load_identity_catalog()
    del catalog
    confirmed_raw_keys, confirmed_canonical_keys, confirmed_files = load_confirmed_identity_keys(
        args.confirmed_identities_file, canonical_index, alias_index
    )
    if args.confirmed_identities_db:
        try:
            db_raw, db_canonical, db_aliases = load_confirmed_identity_db(args.confirmed_identities_db)
        except sqlite3.Error as exc:
            raise SystemExit(f"cannot read confirmed identity store {args.confirmed_identities_db}: {exc}") from exc
        confirmed_raw_keys |= db_raw
        confirmed_canonical_keys |= db_canonical
        # Locally confirmed identities may not yet exist in the shared
        # registry. Add a lightweight confirmed identity to the resolver so
        # their organized folders can still seed the face gallery.
        for alias_key, canonical_key in db_aliases.items():
            identity = canonical_index.get(canonical_key)
            if identity is None:
                identity = sorter.Identity(canonical_key, "review", (alias_key,))
                canonical_index[canonical_key] = identity
            alias_index.setdefault(alias_key, set()).add(identity)
        confirmed_files.append(str(args.confirmed_identities_db))
    if args.confirmed_only_external and not confirmed_files:
        raise SystemExit("confirmed-only external mode requires at least one readable confirmed identity file")
    roots = (args.sorted_root, args.metadaily_root, args.redditdaily_root)
    skip_roots = {os.path.normpath(str(root)) for root in args.skip_root}
    skipped_roots: list[str] = []
    repair_stats: dict[str, int] = {}

    if args.output.exists():
        marker = args.output / ".picorg-managed"
        temp_root = Path(os.environ.get("TMPDIR", tempfile.gettempdir())).resolve()
        legacy_tmp = args.output.parent.resolve() == temp_root and args.output.name.startswith("picorg-face-references-")
        if not marker.is_file() and not legacy_tmp:
            raise SystemExit(f"refusing to remove unmarked output directory: {args.output}")
        shutil.rmtree(args.output)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / ".picorg-managed").touch()
    grouped: dict[str, set[Path]] = {}

    skipped = {str(Path(path)) for path in args.skip_path}

    def child_dirs(root: Path):
        try:
            children = list(root.iterdir())
        except OSError as exc:
            print(f"warning: skipping unreadable root: {root}: {exc.strerror}", file=sys.stderr)
            return []
        result = []
        for item in children:
            if str(item) in skipped:
                print(f"warning: skipping configured path: {item}", file=sys.stderr)
                continue
            try:
                if item.is_dir():
                    result.append(item)
            except OSError as exc:
                print(f"warning: skipping unreadable entry: {item}: {exc.strerror}", file=sys.stderr)
        return result

    external_stats = {"folders_seen": 0, "folders_included": 0, "folders_excluded_unconfirmed": 0}
    progress = CoalesceProgress(args.progress_seconds)

    def resolve_identity(folder_name: str):
        key = sorter.normalize_key(folder_name)
        identity = canonical_index.get(key)
        if identity is None:
            aliases = alias_index.get(key, set())
            identity = next(iter(aliases)) if len(aliases) == 1 else None
        return key, identity

    def add(folder_name: str, files, *, external: bool = False) -> None:
        key, identity = resolve_identity(folder_name)
        # Unknown folder names are usually captions, subreddit topics, or
        # generic buckets. They are not safe face-reference identities.
        if identity is None or sorter.is_generic_identity_token(identity.canonical):
            return
        canonical = identity.canonical
        if external:
            external_stats["folders_seen"] += 1
            if args.confirmed_only_external and key not in confirmed_raw_keys and sorter.normalize_key(canonical) not in confirmed_canonical_keys:
                external_stats["folders_excluded_unconfirmed"] += 1
                return
            external_stats["folders_included"] += 1
        grouped.setdefault(canonical, set()).update(files)

    timed_out_roots: list[str] = []
    unavailable_roots: list[str] = []

    # PicOrg's organized tree is family/identity; coalesce by identity only.
    try:
        print(f"[coalesce] starting root: {args.sorted_root}", file=sys.stderr, flush=True)
        with root_scan_timeout(args.root_timeout, str(args.sorted_root)):
            if not args.sorted_root.is_dir():
                raise FileNotFoundError(f"reference root is unavailable: {args.sorted_root}")
            for family_dir in child_dirs(args.sorted_root):
                for identity_dir in child_dirs(family_dir):
                    add(identity_dir.name, media_files(identity_dir, skipped, args.repair_html, repair_stats, tuple(args.repair_search_root), args.repair_quarantine, args.repair_ledger, args.repair_output_root, args.repair_run_id or args.output.name, repair_scan_cache, repair_corrupt=args.repair_corrupt, progress=progress))
        print(f"[coalesce] completed root: {args.sorted_root}", file=sys.stderr, flush=True)
    except RootScanTimeout as exc:
        timed_out_roots.append(str(args.sorted_root))
        print(f"error: {exc}", file=sys.stderr, flush=True)
    except OSError as exc:
        unavailable_roots.append(str(args.sorted_root))
        print(f"error: {exc}", file=sys.stderr, flush=True)

    # MD/RD download trees are identity/image (with nested provider folders).
    for root in (args.metadaily_root, args.redditdaily_root):
        root_text = os.path.normpath(str(root))
        if root_text in skip_roots:
            skipped_roots.append(root_text)
            print(f"warning: explicitly skipping source root: {root}", file=sys.stderr, flush=True)
            continue
        try:
            print(f"[coalesce] starting root: {root}", file=sys.stderr, flush=True)
            with root_scan_timeout(args.root_timeout, str(root)):
                if not root.is_dir():
                    raise FileNotFoundError(f"reference root is unavailable: {root}")
                for identity_dir in (item for item in child_dirs(root) if not item.name.startswith(".")):
                    add(identity_dir.name, media_files(identity_dir, skipped, args.repair_html, repair_stats, tuple(args.repair_search_root), args.repair_quarantine, args.repair_ledger, args.repair_output_root, args.repair_run_id or args.output.name, repair_scan_cache, repair_corrupt=args.repair_corrupt, progress=progress), external=True)
            print(f"[coalesce] completed root: {root}", file=sys.stderr, flush=True)
        except RootScanTimeout as exc:
            timed_out_roots.append(str(root))
            print(f"error: {exc}", file=sys.stderr, flush=True)
        except OSError as exc:
            unavailable_roots.append(str(root))
            print(f"error: {exc}", file=sys.stderr, flush=True)

    confirmed_references = load_confirmed_reference_paths(
        args.confirmed_reference_file, canonical_index, alias_index, roots
    )
    confirmed_reference_additions: list[dict[str, str]] = []
    for canonical, paths in confirmed_references.items():
        identity_sources = grouped.setdefault(canonical, set())
        for path in sorted(paths):
            if path not in identity_sources:
                confirmed_reference_additions.append({"identity": canonical, "path": str(path)})
                identity_sources.add(path)

    partial = bool(timed_out_roots or unavailable_roots or skipped_roots)
    if partial:
        status = {
            "timed_out_roots": timed_out_roots,
            "unavailable_roots": unavailable_roots,
            "skipped_roots": skipped_roots,
        }
        print(json.dumps(status, sort_keys=True), file=sys.stderr, flush=True)
        if not args.allow_partial_roots:
            raise SystemExit(75)
        print("warning: writing a degraded reference set; missing roots remain stale", file=sys.stderr, flush=True)

    references = 0
    for canonical, sources in sorted(grouped.items()):
        target_dir = args.output / safe_name(canonical)
        target_dir.mkdir(parents=True, exist_ok=True)
        for index, source in enumerate(sorted(sources)):
            target = target_dir / f"{index:07d}_{safe_name(source.name)}"
            os.symlink(source, target)
            references += 1

    report = {
        "identities": len(grouped),
        "references": references,
        "references_by_identity": {identity: len(sources) for identity, sources in sorted(grouped.items())},
        "confirmed_reference_additions": confirmed_reference_additions,
        "output": str(args.output),
        "roots": [str(root) for root in roots],
        "timed_out_roots": timed_out_roots,
        "unavailable_roots": unavailable_roots,
        "skipped_roots": skipped_roots,
        "complete": not partial,
        "confirmed_only_external": args.confirmed_only_external,
        "confirmed_identity_files": confirmed_files,
        "confirmed_identity_keys": len(confirmed_canonical_keys),
        "external_reference_policy": external_stats,
    }
    if args.repair_html:
        report["repair"] = repair_stats
        if args.repair_scan_cache:
            args.repair_scan_cache.parent.mkdir(parents=True, exist_ok=True)
            temporary = args.repair_scan_cache.with_suffix(args.repair_scan_cache.suffix + ".tmp")
            temporary.write_text(json.dumps(repair_scan_cache, sort_keys=True, separators=(",", ":")), encoding="utf-8")
            os.replace(temporary, args.repair_scan_cache)
    report_path = args.output / ".coalesce-report.json"
    temporary_report = report_path.with_suffix(report_path.suffix + ".tmp")
    temporary_report.write_text(json.dumps(report, sort_keys=True, indent=2), encoding="utf-8")
    os.replace(temporary_report, report_path)
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
