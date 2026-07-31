#!/usr/bin/env python3
"""Deterministic media organizer for mixed Reddit/MetaDaily/IMDb sources.

This tool does three things:
1. Build a canonical identity registry from the local source lists.
2. Run a dry matching pass over intake roots.
3. Report coverage and a proxy accuracy score against paths that already
   encode an expected identity.

It does not move files unless --apply is provided.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass, asdict
from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple


DEFAULT_INTAKE_ROOTS = [
    Path("/mnt/elements16/@mixedpics"),
    Path("/mnt/elements16a/Pron/jdownloaderscomplete"),
    Path("/mnt/desktop/Pictures"),
]

# These are independent social source stores. They may inform catalog/profile
# construction, but apply mode must never move anything from either tree.
PROTECTED_SOURCE_ROOTS = (
    Path("/mnt/elements16a/Pron/metadaily"),
    Path("/mnt/elements16a/Pron/redditdaily"),
)

DEST_ROOT = Path("/mnt/elements16/@mixedpics_sorted")
DEFAULT_AUDIT_ROOT = Path("/tmp/picorg_sorted_audit")
DEFAULT_CATALOG_CACHE = Path("/tmp/picorg_identity_catalog_cache.json")
DEFAULT_DRY_RUN_CACHE = Path("/tmp/picorg_dry_run_cache.json")
DEFAULT_RESOLVER_VERSION = "2026-07-31.13"
DEFAULT_OCR_TIMEOUT_SECONDS = 20
DEFAULT_OCR_TRIGGER_CONFIDENCE = 0.85
DEFAULT_APPLY_MIN_CONFIDENCE = 0.95
OCR_SUPPORTED_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}

FRIENDS_FILE = Path("/opt/redditgrab/friend.txt")
PSCRAPE_FILE = Path("/opt/pscrape/redditors.txt")
IMDB_FILE = Path("/opt/list.imdburl")
METADAILY_ACCOUNTS_FILE = Path("/opt/metadaily/social_accounts.txt")
METADAILY_IDENTITY_ALIASES_FILE = Path("/opt/metadaily/data/identity_aliases.json")
PROJECT_REGISTRY_FILE = Path("/opt/picorg/project_registry.json")
REDDITDAILY_ROOT = Path("/mnt/elements16a/Pron/redditdaily")
PSCRAPE_ROOT = Path("/mnt/elements16a/Pron/pscrape")
PROJECT_BLOCKED_TOKENS: Set[str] = set()
PROJECT_AMBIGUOUS_TOKENS: Set[str] = set()
IDENTITY_SCORING_CACHE: Dict[Identity, Tuple[str, str, Tuple[Tuple[str, str], ...]]] = {}

DEFAULT_BLOCKED_TOKENS = {
    "cum",
    "ngl",
    "nsfw",
    "porn",
    "pics",
    "ginger",
    "redhead",
    "tor",
    "iss",
    "slut",
    "pussy",
    "tits",
    "eyes",
    "glasses",
    "milf",
    "spicy",
    "legs",
    "dillionharper",
    "monalita",
}

DEFAULT_AMBIGUOUS_TOKENS = {
    "anal",
    "bukkake",
    "chubby",
    "curvy",
    "daddy",
    "freckle",
    "hangers",
    "hotwife",
    "nudes",
    "pov",
    "redditors",
    "sabrina",
    "snapchat",
    "stacked",
    "toronto",
}

IGNORED_DIR_NAMES = {
    ".git",
    ".github",
    ".pytest_cache",
    ".venv",
    ".cursor",
    "__pycache__",
    "cache",
    "downloads",
    "downloads_backup",
    "legacy_backups",
    "backups",
    "backup",
    "gallery-dl",
    "tools",
    "AppData",
    "ARCHIVE",
}

FAMILY_PRIORITY = {
    "redditdaily": 50,
    "metadaily": 45,
    "reddit_friends": 40,
    "pscrape": 35,
    "imdb": 30,
    "manual": 10,
    "reddit_subreddit": 5,
    "reddit_follow": 5,
}

ARTIFACT_TOKENS = {
    "thumb",
    "thumbnail",
    "story",
    "copy",
    "final",
    "large",
    "small",
    "gallery",
    "reddit",
    "ig",
    "insta",
    "instagram",
    "jpg",
    "jpeg",
    "png",
    "webp",
    "gif",
    "mp4",
    "mov",
    "video",
}

MONTH_TOKENS = {
    "jan",
    "january",
    "feb",
    "february",
    "mar",
    "march",
    "apr",
    "april",
    "may",
    "jun",
    "june",
    "jul",
    "july",
    "aug",
    "august",
    "sep",
    "sept",
    "september",
    "oct",
    "october",
    "nov",
    "november",
    "dec",
    "december",
}

DATE_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}[_ -]*")
DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
NUMERIC_FOLDER = re.compile(r"^\d+[a-z0-9]*(?:[ _-].*)?$", re.I)
REDDIT_SUBREDDIT_PATTERN = re.compile(
    r"(?i)(?:^|[^a-z0-9])(?:r|subreddit)[/_ -]+([a-z0-9][a-z0-9_+-]{1,})"
)
REDDIT_USER_PATTERN = re.compile(
    r"(?i)(?:^|[^a-z0-9])(?:u|user|author)[/_ -]+([a-z0-9][a-z0-9_+-]{1,})"
)
REDDIT_CONTEXT_PATTERN = re.compile(
    r"(?i)\b(?:posted in|subreddit|reddit)\b[:\s_-]+([a-z0-9][a-z0-9_+-]{1,})"
)
REDDIT_POST_ID_PATTERN = re.compile(
    r"(?i)(?:t3[_-]?|redd\.it/|reddit\.com/(?:r/[^/]+/)?(?:comments|gallery)/)([a-z0-9]{4,})"
)
METADATA_USER_KEYS = {"author", "author_name", "username", "user", "uploader"}
METADATA_SUBREDDIT_KEYS = {"subreddit", "community", "subreddit_name"}


@dataclass(frozen=True)
class Identity:
    canonical: str
    family: str
    aliases: Tuple[str, ...]


@dataclass
class MatchResult:
    path: str
    source_root: str
    family: Optional[str]
    canonical: Optional[str]
    confidence: float
    rule: str
    expected_identity: Optional[str] = None
    expected_available: bool = False
    reddit_post_ids: Tuple[str, ...] = ()
    metadata_users: Tuple[str, ...] = ()
    metadata_subreddits: Tuple[str, ...] = ()
    title: Optional[str] = None
    source_family: Optional[str] = None
    source_detail: Optional[str] = None
    ocr_used: bool = False

    @property
    def predicted(self) -> Optional[str]:
        if self.family and self.canonical:
            return f"{self.family}/{self.canonical}"
        return None


@lru_cache(maxsize=16384)
def normalize(text: str) -> str:
    text = text.lower().strip()
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


@lru_cache(maxsize=16384)
def normalize_key(text: str) -> str:
    return normalize(text).replace(" ", "")


def slugify(text: str) -> str:
    key = normalize(text).replace(" ", "_")
    return key or "unknown"


def catalog_cache_path() -> Path:
    return Path(os.environ.get("PICORG_CATALOG_CACHE", str(DEFAULT_CATALOG_CACHE)))


def dry_run_cache_path() -> Path:
    return Path(os.environ.get("PICORG_DRY_RUN_CACHE", str(DEFAULT_DRY_RUN_CACHE)))


def path_snapshot(path: Path, kind: str) -> Dict[str, object]:
    snapshot: Dict[str, object] = {"path": str(path), "kind": kind}
    try:
        stat_result = path.stat()
    except OSError:
        snapshot["exists"] = False
        return snapshot
    snapshot["exists"] = True
    snapshot["mtime_ns"] = stat_result.st_mtime_ns
    snapshot["size"] = stat_result.st_size
    return snapshot


def catalog_source_state() -> Dict[str, object]:
    files = [
        PROJECT_REGISTRY_FILE,
        FRIENDS_FILE,
        PSCRAPE_FILE,
        IMDB_FILE,
        METADAILY_ACCOUNTS_FILE,
        METADAILY_IDENTITY_ALIASES_FILE,
    ]
    files.extend(STRONG_TEXT_SOURCE_FILES)
    files.extend(WEAK_TEXT_SOURCE_FILES)
    roots = [REDDITDAILY_ROOT, PSCRAPE_ROOT]
    return {
        "files": [path_snapshot(path, "file") for path in files],
        "roots": [path_snapshot(path, "dir") for path in roots],
    }


def rebuild_catalog_indexes(
    identities: Sequence[Identity],
) -> Tuple[List[Identity], Dict[str, Set[Identity]], Dict[str, Identity], Dict[str, Set[Identity]]]:
    alias_index: Dict[str, Set[Identity]] = defaultdict(set)
    canonical_index: Dict[str, Identity] = {}
    token_index: Dict[str, Set[Identity]] = defaultdict(set)

    for identity in identities:
        canonical_index[identity.canonical] = identity
        for alias in {identity.canonical, *identity.aliases}:
            alias_key = normalize_key(alias)
            if not alias_key:
                continue
            alias_index[alias_key].add(identity)
            for token in tokenize(normalize(alias)):
                token_index[token].add(identity)

    return list(identities), alias_index, canonical_index, token_index


def build_identity_scoring_cache(identities: Sequence[Identity]) -> None:
    global IDENTITY_SCORING_CACHE
    cache: Dict[Identity, Tuple[str, str, Tuple[Tuple[str, str], ...]]] = {}
    for identity in identities:
        canonical_norm = normalize(identity.canonical)
        canonical_key = normalize_key(identity.canonical)
        alias_pairs = tuple(
            (normalize(alias), normalize_key(alias))
            for alias in identity.aliases
            if normalize(alias)
        )
        cache[identity] = (canonical_norm, canonical_key, alias_pairs)
    IDENTITY_SCORING_CACHE = cache


def load_cached_catalog(cache_file: Path, current_state: Dict[str, object]) -> Optional[Tuple[
    List[Identity],
    Dict[str, Set[Identity]],
    Dict[str, Identity],
    Dict[str, Set[Identity]],
    Dict[str, str],
]]:
    try:
        payload = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("schema_version") != 2:
        return None
    if payload.get("resolver_version") != DEFAULT_RESOLVER_VERSION:
        return None
    if payload.get("source_state") != current_state:
        return None
    identity_records = payload.get("identities") or []
    if not isinstance(identity_records, list):
        return None
    identities: List[Identity] = []
    for item in identity_records:
        if not isinstance(item, dict):
            return None
        canonical = str(item.get("canonical", "")).strip()
        family = str(item.get("family", "")).strip()
        aliases_raw = item.get("aliases") or []
        if not canonical or not family or not isinstance(aliases_raw, list):
            return None
        aliases = tuple(
            dict.fromkeys(
                str(alias).strip()
                for alias in aliases_raw
                if str(alias).strip() and str(alias).strip() != canonical
            )
        )
        identities.append(Identity(canonical=canonical, family=family, aliases=aliases))
    preferred_alias_targets = {
        normalize_key(alias): normalize_key(target)
        for alias, target in (payload.get("preferred_alias_targets") or {}).items()
        if normalize_key(alias) and normalize_key(target)
    }
    global PROJECT_BLOCKED_TOKENS, PROJECT_AMBIGUOUS_TOKENS
    PROJECT_BLOCKED_TOKENS = {
        normalize_key(token)
        for token in payload.get("project_blocked_tokens", [])
        if normalize_key(token)
    }
    PROJECT_BLOCKED_TOKENS |= {normalize_key(token) for token in DEFAULT_BLOCKED_TOKENS}
    PROJECT_AMBIGUOUS_TOKENS = {
        normalize_key(token)
        for token in payload.get("project_ambiguous_tokens", [])
        if normalize_key(token)
    }
    PROJECT_AMBIGUOUS_TOKENS |= {normalize_key(token) for token in DEFAULT_AMBIGUOUS_TOKENS}
    catalog, alias_index, canonical_index, token_index = rebuild_catalog_indexes(identities)
    build_identity_scoring_cache(catalog)
    return catalog, alias_index, canonical_index, token_index, preferred_alias_targets


def write_catalog_cache(
    cache_file: Path,
    current_state: Dict[str, object],
    catalog: Sequence[Identity],
    preferred_alias_targets: Dict[str, str],
) -> None:
    payload = {
        "schema_version": 2,
        "resolver_version": DEFAULT_RESOLVER_VERSION,
        "source_state": current_state,
        "project_blocked_tokens": sorted(PROJECT_BLOCKED_TOKENS),
        "project_ambiguous_tokens": sorted(PROJECT_AMBIGUOUS_TOKENS),
        "preferred_alias_targets": dict(sorted(preferred_alias_targets.items())),
        "identities": [
            {
                "canonical": identity.canonical,
                "family": identity.family,
                "aliases": list(identity.aliases),
            }
            for identity in catalog
        ],
    }
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def dry_run_state(root_paths: Sequence[Path], ocr_enabled_flag: bool) -> Dict[str, object]:
    state: Dict[str, object] = {
        "schema_version": 1,
        "resolver_version": DEFAULT_RESOLVER_VERSION,
        "roots": [str(path) for path in root_paths],
        "catalog_state": catalog_source_state(),
        "ocr_enabled": ocr_enabled_flag,
    }
    if ocr_enabled_flag:
        state["ocr_image"] = os.environ.get("PICORG_OCR_IMAGE", "")
        state["ocr_command_json"] = os.environ.get("PICORG_OCR_COMMAND_JSON", "")
        state["ocr_timeout_seconds"] = os.environ.get(
            "PICORG_OCR_TIMEOUT_SECONDS",
            str(DEFAULT_OCR_TIMEOUT_SECONDS),
        )
    return state


def dry_run_root_state(root: Path, ocr_enabled_flag: bool) -> Dict[str, object]:
    state: Dict[str, object] = {
        "schema_version": 2,
        "resolver_version": DEFAULT_RESOLVER_VERSION,
        "root": str(root),
        "root_snapshot": path_snapshot(root, "dir"),
        "catalog_state": catalog_source_state(),
        "ocr_enabled": ocr_enabled_flag,
    }
    if ocr_enabled_flag:
        state["ocr_image"] = os.environ.get("PICORG_OCR_IMAGE", "")
        state["ocr_command_json"] = os.environ.get("PICORG_OCR_COMMAND_JSON", "")
        state["ocr_timeout_seconds"] = os.environ.get(
            "PICORG_OCR_TIMEOUT_SECONDS",
            str(DEFAULT_OCR_TIMEOUT_SECONDS),
        )
    return state


def load_dry_run_cache_payload(cache_file: Path) -> Optional[Dict[str, object]]:
    try:
        payload = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _results_from_payload(items: object) -> Optional[List[MatchResult]]:
    if not isinstance(items, list):
        return None
    results: List[MatchResult] = []
    for item in items:
        if not isinstance(item, dict):
            return None
        try:
            results.append(MatchResult(**item))
        except TypeError:
            return None
    return results


def load_dry_run_cache(cache_file: Path, state: Dict[str, object]) -> Optional[Tuple[List[MatchResult], Dict[str, object]]]:
    payload = load_dry_run_cache_payload(cache_file)
    if not payload:
        return None
    if payload.get("schema_version") == 1 and payload.get("state") == state:
        results = _results_from_payload(payload.get("results") or [])
        report = payload.get("report")
        if results is not None and isinstance(report, dict) and "scanned" in report:
            return results, report
        return None
    if payload.get("schema_version") == 2 and payload.get("whole_state") == state:
        results = _results_from_payload(payload.get("whole_results") or [])
        report = payload.get("whole_report")
        if results is not None and isinstance(report, dict) and "scanned" in report:
            return results, report
    return None


def write_dry_run_cache(
    cache_file: Path,
    state: Dict[str, object],
    results: Sequence[MatchResult],
    report: Dict[str, object],
    *,
    root_cache: Optional[Dict[str, Dict[str, object]]] = None,
) -> None:
    payload = {
        "schema_version": 2,
        "whole_state": state,
        "whole_results": [asdict(result) for result in results],
        "whole_report": report,
        "roots": root_cache or {},
    }
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def is_generic_identity_token(value: str) -> bool:
    normalized = normalize(value)
    if not normalized:
        return True
    key = normalize_key(normalized)
    if key in PROJECT_BLOCKED_TOKENS or key in DEFAULT_BLOCKED_TOKENS:
        return True
    return False


def should_import_weak_identity(canonical: str, aliases: Iterable[str]) -> bool:
    if is_generic_identity_token(canonical):
        return False
    alias_keys = {normalize_key(alias) for alias in aliases if normalize_key(alias)}
    if alias_keys & (PROJECT_BLOCKED_TOKENS | DEFAULT_BLOCKED_TOKENS):
        return False
    return True


def is_media_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in {
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".gif",
        ".mp4",
        ".mov",
        ".m4v",
        ".webm",
        ".bmp",
        ".tiff",
    }


def should_skip_dir(path: Path) -> bool:
    return any(part in IGNORED_DIR_NAMES or part.startswith(".") for part in path.parts)


def walk_media_files(root: Path) -> Iterator[Path]:
    if not root.exists():
        return
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        dirnames[:] = [
            name
            for name in dirnames
            if name not in IGNORED_DIR_NAMES and not name.startswith(".")
        ]
        if should_skip_dir(current):
            continue
        for filename in filenames:
            path = current / filename
            if is_media_file(path):
                yield path


def parse_url_slug(line: str) -> Optional[str]:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if "://" not in line:
        return line
    stripped = line.rstrip("/")
    tail = stripped.rsplit("/", 1)[-1]
    tail = tail.split("?")[0]
    if tail:
        return tail
    return None


def parse_imdb_actor(path: Path) -> List[str]:
    aliases: List[str] = []
    if not path.exists():
        return aliases
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            candidate = line.lstrip("#").strip()
            if candidate and candidate.isascii():
                aliases.append(candidate)
            continue
        # URLs in this file are title pages and not strong actor labels.
    return aliases


WEAK_TEXT_SOURCE_FILES = [
    Path("/opt/redditgrab/subreddits.txt"),
    Path("/opt/redditgrab/users.txt"),
    Path("/opt/reddit/otherfriends.txt"),
    Path("/opt/redditgrab/otherfriends.txt"),
    Path("/opt/grabplaylist/reddit/otherfriends.txt"),
    Path("/opt/grabplaylist/reddit/friend.txt"),
    Path("/opt/grabplaylist/reddit/friends.txt"),
    Path("/opt/grabplaylist/reddittargetted/friends.txt"),
    Path("/opt/photo_reorg/reddit_friends_list.txt"),
]

STRONG_TEXT_SOURCE_FILES = [
    Path("/opt/redditdaily/redditsubs.txt"),
]

PROJECT_REGISTRY_DEFAULT = {
    "blocked_tokens": [
        "cum",
        "ngl",
    ],
    "preferred_alias_targets": {},
    "entries": [
        {
            "family": "reddit_follow",
            "canonical": "bailey_spinn",
            "aliases": ["Bailey Spinn", "bailey spinn", "baileyspinn", "bailey_spinn"],
            "notes": "Project-local identity for Bailey Spinn.",
        },
        {
            "family": "pscrape",
            "canonical": "hailstorm93",
            "aliases": ["ahhhey", "AhhHey", "hailstorm93"],
            "notes": "Project-local alias overlay; ahhhey is an alias of hailstorm93.",
        },
        {
            "family": "reddit_follow",
            "canonical": "brookemonkthesecond",
            "aliases": ["BrookeMonkTheSecond", "brooke monk the second", "brooke monk", "brooke monk second"],
            "notes": "Project-local alias overlay for Brooke Monk variant names.",
        },
        {
            "family": "reddit_follow",
            "canonical": "pennypax",
            "aliases": ["PennyPax", "penny pax"],
            "notes": "Project-local alias overlay for Penny Pax variants.",
        },
        {
            "family": "reddit_follow",
            "canonical": "camilamendes",
            "aliases": ["CamilaMendes", "Camila Mendes", "camila mendes"],
            "notes": "Project-local alias overlay for Camila Mendes variants.",
        },
        {
            "family": "reddit_follow",
            "canonical": "laurenphillips",
            "aliases": ["LaurenPhillips", "Lauren Phillips", "lauren phillips"],
            "notes": "Project-local alias overlay for Lauren Phillips variants.",
        },
        {
            "family": "reddit_follow",
            "canonical": "jia_lissa",
            "aliases": ["Jia_Lissa", "Jia Lissa", "jia lissa"],
            "notes": "Project-local alias overlay for Jia Lissa variants.",
        },
        {
            "family": "reddit_follow",
            "canonical": "emma_wallbank",
            "aliases": ["Emma Wallbank", "Emma wallbank", "emma wallbank"],
            "notes": "Project-local alias overlay for Emma Wallbank variants.",
        },
    ]
}


def load_identity_catalog() -> Tuple[
    List[Identity],
    Dict[str, Set[Identity]],
    Dict[str, Identity],
    Dict[str, Set[Identity]],
    Dict[str, str],
]:
    cache_file = catalog_cache_path()
    current_state = catalog_source_state()
    cached = load_cached_catalog(cache_file, current_state)
    if cached is not None:
        return cached

    identities: List[Identity] = []
    alias_index: Dict[str, Set[Identity]] = defaultdict(set)
    canonical_index: Dict[str, Identity] = {}
    token_index: Dict[str, Set[Identity]] = defaultdict(set)
    global PROJECT_BLOCKED_TOKENS, PROJECT_AMBIGUOUS_TOKENS
    PROJECT_BLOCKED_TOKENS = set()

    def register_alias(alias: str, identity: Identity) -> None:
        alias_key = normalize_key(alias)
        if not alias_key:
            return
        alias_index[alias_key].add(identity)
        for token in tokenize(normalize(alias)):
            if len(token) >= 5 or any(char.isdigit() for char in token):
                token_index[token].add(identity)

    def add_identity(
        canonical: str,
        family: str,
        aliases: Iterable[str],
        *,
        source_kind: str = "strong",
    ) -> None:
        canonical = canonical.strip()
        if not canonical:
            return
        if source_kind != "registry" and is_generic_identity_token(canonical):
            return
        if source_kind == "weak" and not should_import_weak_identity(canonical, aliases):
            return
        if canonical in canonical_index:
            existing = canonical_index[canonical]
            merged = tuple(
                dict.fromkeys(
                    alias
                    for alias in (*existing.aliases, *tuple(aliases))
                    if alias
                    and alias.strip()
                    and alias.strip() != canonical
                    and (
                        source_kind == "registry"
                        or (
                            normalize_key(alias) not in DEFAULT_BLOCKED_TOKENS
                            and normalize_key(alias) not in PROJECT_BLOCKED_TOKENS
                        )
                    )
                )
            )
            ident = Identity(canonical=canonical, family=existing.family, aliases=merged)
            canonical_index[canonical] = ident
            identities[identities.index(existing)] = ident
        else:
            filtered_aliases = tuple(
                dict.fromkeys(
                    alias.strip()
                    for alias in aliases
                    if alias
                    and alias.strip()
                    and alias.strip() != canonical
                    and (
                        source_kind == "registry"
                        or (
                            normalize_key(alias) not in DEFAULT_BLOCKED_TOKENS
                            and normalize_key(alias) not in PROJECT_BLOCKED_TOKENS
                        )
                    )
                )
            )
            ident = Identity(canonical=canonical, family=family, aliases=filtered_aliases)
            canonical_index[canonical] = ident
            identities.append(ident)
        ident = canonical_index[canonical]
        for alias in {canonical, *ident.aliases}:
            register_alias(alias, ident)

    def add_text_alias_file(path: Path, family: str, *, source_kind: str = "strong") -> None:
        if not path.exists():
            return
        for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "://" in line:
                slug = parse_url_slug(line)
                if not slug:
                    continue
                add_identity(slugify(slug), family, {slug, slugify(slug)}, source_kind=source_kind)
            else:
                cleaned = line.lstrip("u/").lstrip("r/").strip()
                if cleaned:
                    add_identity(slugify(cleaned), family, {cleaned, slugify(cleaned)}, source_kind=source_kind)

    def add_registry_entries(entries: Sequence[dict], family_default: str = "project_registry") -> None:
        for entry in entries:
            canonical = str(entry.get("canonical", "")).strip()
            family = str(entry.get("family", family_default)).strip() or family_default
            aliases = entry.get("aliases") or []
            alias_values = {canonical, *[str(alias).strip() for alias in aliases if str(alias).strip()]}
            add_identity(canonical, family, alias_values, source_kind="registry")

    if PROJECT_REGISTRY_FILE.exists():
        try:
            registry = json.loads(PROJECT_REGISTRY_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            registry = PROJECT_REGISTRY_DEFAULT
    else:
        registry = PROJECT_REGISTRY_DEFAULT
    PROJECT_BLOCKED_TOKENS = {
        normalize_key(token)
        for token in registry.get("blocked_tokens", [])
        if normalize_key(token)
    }
    PROJECT_BLOCKED_TOKENS |= {normalize_key(token) for token in DEFAULT_BLOCKED_TOKENS}
    PROJECT_AMBIGUOUS_TOKENS = {
        normalize_key(token)
        for token in registry.get("ambiguous_tokens", [])
        if normalize_key(token)
    }
    PROJECT_AMBIGUOUS_TOKENS |= {normalize_key(token) for token in DEFAULT_AMBIGUOUS_TOKENS}
    preferred_alias_targets = {
        normalize_key(alias): normalize_key(target)
        for alias, target in registry.get("preferred_alias_targets", {}).items()
        if normalize_key(alias) and normalize_key(target)
    }
    add_registry_entries(registry.get("entries", []))

    # The protected metadaily registry is the authoritative source for
    # confirmed profile folders, display names, and Reddit handles.  It is
    # read-only input; nothing under that store is ever moved by picorg.
    if METADAILY_IDENTITY_ALIASES_FILE.exists():
        try:
            identity_payload = json.loads(
                METADAILY_IDENTITY_ALIASES_FILE.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            identity_payload = {}
        for item in identity_payload.get("identities", []) if isinstance(identity_payload, dict) else []:
            if not isinstance(item, dict) or item.get("status") != "confirmed":
                continue
            canonical = str(item.get("primary_folder") or item.get("id") or "").strip()
            aliases = set()
            is_aggregate = "aggregate identity" in str(item.get("notes", "")).lower()
            alias_keys = ("id", "primary_folder")
            if not is_aggregate:
                alias_keys += ("display_names", "search_terms")
            for key in alias_keys:
                value = item.get(key)
                if isinstance(value, list):
                    aliases.update(str(entry).strip() for entry in value if str(entry).strip())
                elif value:
                    aliases.add(str(value).strip())
            reddit = item.get("reddit") or {}
            if isinstance(reddit, dict):
                for key in ("users", "subreddits"):
                    values = reddit.get(key) or []
                    if isinstance(values, list):
                        aliases.update(str(entry).strip() for entry in values if str(entry).strip())
            if canonical:
                add_identity(canonical, "metadaily", aliases, source_kind="strong")

    if FRIENDS_FILE.exists():
        for line in FRIENDS_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                add_identity(line, "reddit_friends", {line})

    if PSCRAPE_FILE.exists():
        for line in PSCRAPE_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
            slug = parse_url_slug(line)
            if slug:
                add_identity(slug, "pscrape", {slug, line.strip()})

    if IMDB_FILE.exists():
        for actor in parse_imdb_actor(IMDB_FILE):
            add_identity(actor, "imdb", {actor})

    if METADAILY_ACCOUNTS_FILE.exists():
        current_label: Optional[str] = None
        for raw in METADAILY_ACCOUNTS_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("#"):
                label = line.lstrip("#").strip()
                if label and "http" not in label.lower():
                    current_label = label
                    add_identity(slugify(label), "metadaily", {label, slugify(label)})
                continue
            if "://" in line:
                slug = parse_url_slug(line)
                if slug:
                    aliases = {slug}
                    if current_label:
                        aliases.add(current_label)
                    add_identity(slugify(slug), "metadaily", aliases)

    for path in STRONG_TEXT_SOURCE_FILES:
        add_text_alias_file(path, "reddit_subreddit", source_kind="strong")
    for path in WEAK_TEXT_SOURCE_FILES:
        add_text_alias_file(path, "reddit_follow", source_kind="weak")

    for root, family in ((REDDITDAILY_ROOT, "redditdaily"), (PSCRAPE_ROOT, "pscrape")):
        if not root.exists():
            continue
        try:
            with os.scandir(root) as entries:
                for entry in entries:
                    name = entry.name
                    if name in IGNORED_DIR_NAMES or name.startswith("."):
                        continue
                    if family == "redditdaily" and name in {"downloads", "downloads_backup", "cache", "backups", "legacy_backups"}:
                        continue
                    try:
                        if not entry.is_dir(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    add_identity(name, family, {name})
        except OSError:
            continue

    # Add a few useful aliases from a strict subset of redditdaily folder names.
    # This helps files that mention display names rather than canonical folder names.
    for ident in list(identities):
        extra_aliases = set()
        norm = normalize(ident.canonical)
        if "_" in ident.canonical:
            extra_aliases.add(ident.canonical.replace("_", " "))
        if "-" in ident.canonical:
            extra_aliases.add(ident.canonical.replace("-", " "))
        if norm and " " in norm:
            extra_aliases.add(norm)
        if extra_aliases:
            merged = tuple(dict.fromkeys((*ident.aliases, *sorted(extra_aliases))))
            updated = Identity(canonical=ident.canonical, family=ident.family, aliases=merged)
            canonical_index[ident.canonical] = updated
            identities[identities.index(ident)] = updated
            for alias in {updated.canonical, *updated.aliases}:
                register_alias(alias, updated)

    build_identity_scoring_cache(identities)
    write_catalog_cache(cache_file, current_state, identities, preferred_alias_targets)
    return identities, alias_index, canonical_index, token_index, preferred_alias_targets


def tokenize(text: str) -> List[str]:
    return [tok for tok in re.split(r"[^a-z0-9]+", text.lower()) if tok]


def strip_artifacts_from_stem(stem: str) -> str:
    text = DATE_PREFIX.sub("", stem)
    text = text.replace("_", " ").replace("-", " ")
    text = re.sub(r"\b\d{1,4}\b", " ", text)
    tokens = [tok for tok in tokenize(text) if tok not in ARTIFACT_TOKENS]
    return " ".join(tokens)


def title_from_path(path: Path) -> str:
    stem = path.stem
    cleaned = DATE_PREFIX.sub("", stem)
    cleaned = cleaned.replace("_", " ").replace("-", " ")
    cleaned = re.sub(r"\(\d+\)$", " ", cleaned)
    cleaned = re.sub(r"\b(?:copy|final|large|small|thumb|thumbnail)\b$", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def gallery_base_title(title: str) -> str:
    cleaned = title.strip()
    cleaned = re.sub(r"\s*\(\d+\)$", "", cleaned)
    cleaned = re.sub(r"\s*\[\d+\]$", "", cleaned)
    cleaned = re.sub(r"\s+\d+$", "", cleaned)
    cleaned = re.sub(r"(?<=\D)\d{5,}$", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def file_sha256(path: Path, limit_bytes: Optional[int] = None) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024 if limit_bytes is None else min(1024 * 1024, limit_bytes))
            if not chunk:
                break
            h.update(chunk)
            if limit_bytes is not None:
                limit_bytes -= len(chunk)
                if limit_bytes <= 0:
                    break
    return h.hexdigest()


def file_hash(path: Path) -> str:
    return file_sha256(path)


def _read_reddit_sidecar(path: Path) -> Dict[str, List[str]]:
    """Read adjacent downloader metadata without network access or mutation."""
    values = {"metadata_users": [], "metadata_subreddits": [], "metadata_context": [], "reddit_post_ids": []}
    sidecars = (path.with_suffix(path.suffix + ".json"), path.with_suffix(".json"))
    for sidecar in sidecars:
        if not sidecar.is_file():
            continue
        try:
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        for key, raw_value in payload.items():
            key_norm = normalize_key(str(key))
            if not isinstance(raw_value, str):
                continue
            value = raw_value.strip().strip("_-")
            if not value or len(value) > 160:
                continue
            if key_norm in METADATA_USER_KEYS:
                values["metadata_users"].append(value)
            elif key_norm in METADATA_SUBREDDIT_KEYS:
                values["metadata_subreddits"].append(value)
            elif key_norm in {"title", "caption", "permalink"}:
                values["metadata_context"].extend(tokenize(value))
                values["reddit_post_ids"].extend(REDDIT_POST_ID_PATTERN.findall(value))
    return {key: unique_list(items) for key, items in values.items()}


def extract_reddit_context(path: Path) -> Dict[str, List[str]]:
    raw = " / ".join(path.parts)
    matches = {
        "subreddits": [],
        "users": [],
        "context": [],
        "metadata_users": [],
        "metadata_subreddits": [],
        "metadata_context": [],
        "reddit_post_ids": [],
    }
    for pattern, bucket in (
        (REDDIT_SUBREDDIT_PATTERN, "subreddits"),
        (REDDIT_USER_PATTERN, "users"),
        (REDDIT_CONTEXT_PATTERN, "context"),
    ):
        for hit in pattern.findall(raw):
            cleaned = hit.strip().strip("_-")
            if cleaned:
                matches[bucket].append(cleaned)
    title = title_from_path(path)
    if title:
        matches["context"].extend(tokenize(title))
    matches["reddit_post_ids"].extend(REDDIT_POST_ID_PATTERN.findall(raw))
    for key, values in _read_reddit_sidecar(path).items():
        matches[key].extend(values)
    return {key: unique_list(values) for key, values in matches.items()}


def extract_downloader_hints(path: Path) -> List[str]:
    """Extract the human-name segment from common JDownloader filenames."""
    if "jdownloaderscomplete" not in {normalize_key(part) for part in path.parts}:
        return []
    stem = DATE_PREFIX.sub("", path.stem)
    chunks = [chunk for chunk in stem.split("_") if chunk]
    for index, chunk in enumerate(chunks):
        if (
            len(chunk) >= 6
            and re.fullmatch(r"[a-z0-9]+", chunk, re.I)
            and any(char.isdigit() for char in chunk)
        ):
            trailing = chunks[index + 1 :]
            if trailing:
                return [" ".join(trailing), " ".join(trailing[:-1])]
    return []


def infer_source_family(path: Path) -> Optional[str]:
    parts = {normalize_key(part) for part in path.parts}
    if any(part in {"redditdaily", "redditdailydownloads"} for part in parts):
        return "redditdaily"
    if any(part in {"metadaily"} for part in parts):
        return "metadaily"
    if any(part in {"pscrape"} for part in parts):
        return "pscrape"
    if "jdownloaderscomplete" in parts:
        return "jdownloader"
    return None


def guess_expected_identity(path: Path, root: Path, alias_index: Dict[str, Set[Identity]]) -> Optional[str]:
    rel = path.relative_to(root)
    parts = list(rel.parts[:-1]) + [path.stem]
    # First, trust explicit folder names that already exist in the current tree.
    for part in parts:
        exact = alias_index.get(normalize_key(part))
        if exact:
            return sorted(exact, key=lambda ident: (-FAMILY_PRIORITY.get(ident.family, 0), ident.canonical))[0].canonical

    # Then apply path-shape heuristics for manually downloaded / generated labels.
    candidates = [p for p in parts if normalize(p)]
    for part in candidates:
        cleaned = DATE_PREFIX.sub("", part)
        cleaned = cleaned.replace("_", " ").replace("-", " ")
        tokens = [tok for tok in tokenize(cleaned) if tok not in ARTIFACT_TOKENS]
        if not tokens:
            continue
        if len(tokens) == 1:
            token = tokens[0]
            if token not in MONTH_TOKENS:
                hit = alias_index.get(normalize_key(token))
                if hit:
                    return sorted(hit, key=lambda ident: (-FAMILY_PRIORITY.get(ident.family, 0), ident.canonical))[0].canonical
        if len(tokens) >= 2:
            first = tokens[0]
            if first not in MONTH_TOKENS and not DATE_ONLY.match(first):
                hit = alias_index.get(normalize_key(first))
                if hit:
                    return sorted(hit, key=lambda ident: (-FAMILY_PRIORITY.get(ident.family, 0), ident.canonical))[0].canonical
            last = tokens[-1]
            hit = alias_index.get(normalize_key(last))
            if hit:
                return sorted(hit, key=lambda ident: (-FAMILY_PRIORITY.get(ident.family, 0), ident.canonical))[0].canonical
    return None


def match_cache_signature(path: Path, root: Path, reddit_context: Dict[str, List[str]], ocr_joined_key: str) -> str:
    rel = path.relative_to(root)
    parent_key = "/".join(normalize_key(part) for part in rel.parent.parts if normalize_key(part))
    base_title = gallery_base_title(title_from_path(path))
    title_key = normalize_key(base_title)
    return "|".join(
        [
            parent_key,
            title_key,
                ",".join(sorted(reddit_context.get("subreddits", []))),
                ",".join(sorted(reddit_context.get("users", []))),
                ",".join(sorted(reddit_context.get("metadata_subreddits", []))),
                ",".join(sorted(reddit_context.get("metadata_users", []))),
                ",".join(sorted(reddit_context.get("reddit_post_ids", []))),
            ocr_joined_key,
        ]
    )


def best_identity_match(
    path: Path,
    root: Path,
    catalog: List[Identity],
    alias_index: Dict[str, Set[Identity]],
    token_index: Dict[str, Set[Identity]],
    preferred_alias_targets: Optional[Dict[str, str]] = None,
    match_cache: Optional[Dict[str, Tuple[Optional[Identity], float, str]]] = None,
    ocr_text: Optional[str] = None,
) -> Tuple[Optional[Identity], float, str]:
    pieces = []
    rel = path.relative_to(root)
    pieces.extend(rel.parts)
    pieces.append(path.stem)
    pieces.extend(tokenize(path.stem))
    pieces.extend(tokenize(rel.parent.name))
    pieces.extend(extract_downloader_hints(path))

    normalized_pieces = [normalize(piece) for piece in pieces if normalize(piece)]
    joined = " ".join(normalized_pieces)
    joined_key = normalize_key(joined)
    ocr_joined = normalize(ocr_text or "")
    ocr_joined_key = normalize_key(ocr_joined)
    reddit_context = extract_reddit_context(path)
    title_tokens = reddit_context["context"] + reddit_context["metadata_context"]
    title_joined = normalize(" ".join(title_tokens)) if title_tokens else ""
    stem_without_date = DATE_PREFIX.sub("", path.stem)
    stem_tokens = tokenize(stem_without_date[:64])
    cache_key = None
    if match_cache is not None:
        cache_key = match_cache_signature(path, root, reddit_context, ocr_joined_key)
        cached = match_cache.get(cache_key)
        if cached is not None:
            return cached
    best: Tuple[Optional[Identity], float, str] = (None, 0.0, "unmatched")
    candidate_identities: Set[Identity] = set()

    def consider(identity: Identity, confidence: float, rule: str) -> None:
        nonlocal best
        if confidence > best[1]:
            best = (identity, confidence, rule)
            return
        if confidence == best[1]:
            current_best = best[0]
            if current_best and FAMILY_PRIORITY.get(identity.family, 0) > FAMILY_PRIORITY.get(current_best.family, 0):
                best = (identity, confidence, rule)
                return
            if current_best is None:
                best = (identity, confidence, rule)

    def is_ambiguous_key(value: str) -> bool:
        return normalize_key(value) in PROJECT_AMBIGUOUS_TOKENS

    def is_weak_single_token(value: str, key: str, *, canonical: bool = False) -> bool:
        if " " in value or any(char.isdigit() for char in key):
            return False
        minimum = 8 if canonical else 10
        return len(key) < minimum

    for piece in normalized_pieces:
        exact_key = normalize_key(piece)
        exact_hits = alias_index.get(exact_key, set())
        exact_gallery_key = normalize_key(gallery_base_title(title_from_path(path)))
        exact_manual_gallery_hit = (
            exact_key == exact_gallery_key
            and any(identity.family == "manual" for identity in exact_hits)
        )
        if exact_key in PROJECT_AMBIGUOUS_TOKENS or (len(exact_key) < 10 and not exact_manual_gallery_hit):
            continue
        if exact_key in PROJECT_BLOCKED_TOKENS and not exact_hits:
            continue
        preferred_target = (preferred_alias_targets or {}).get(exact_key)
        if preferred_target:
            preferred_hits = [identity for identity in exact_hits if normalize_key(identity.canonical) == preferred_target]
            if preferred_hits:
                chosen = sorted(
                    preferred_hits,
                    key=lambda ident: (-FAMILY_PRIORITY.get(ident.family, 0), ident.canonical),
                )[0]
                consider(chosen, 1.0, f"exact-preferred:{piece}")
                candidate_identities.add(chosen)
                continue
        for identity in exact_hits:
            consider(identity, 1.0, f"exact:{piece}")
            candidate_identities.add(identity)

    search_tokens = set(tokenize(joined))
    if ocr_joined:
        search_tokens.update(tokenize(ocr_joined))
    for token in search_tokens:
        for identity in token_index.get(token, set()):
            candidate_identities.add(identity)

    for subreddit in reddit_context["subreddits"]:
        hits = alias_index.get(normalize_key(subreddit), set())
        for identity in hits:
            bonus = 0.0
            if identity.family == "reddit_subreddit":
                bonus = 0.04
            elif identity.family == "reddit_follow":
                bonus = 0.02
            consider(identity, min(1.0, 0.96 + bonus), f"reddit-subreddit:{subreddit}")

    for user in reddit_context["users"]:
        hits = alias_index.get(normalize_key(user), set())
        for identity in hits:
            bonus = 0.0
            if identity.family == "reddit_friends":
                bonus = 0.04
            elif identity.family == "redditdaily":
                bonus = 0.02
            consider(identity, min(1.0, 0.96 + bonus), f"reddit-user:{user}")
            candidate_identities.add(identity)

    # Downloader sidecars are stronger than caption text, but only the
    # explicit author/uploader fields qualify for this high-confidence path.
    for user in reddit_context["metadata_users"]:
        hits = alias_index.get(normalize_key(user), set())
        for identity in hits:
            consider(identity, 0.99, f"metadata-author:{user}")
            candidate_identities.add(identity)

    def score_identity_pool(pool: Iterable[Identity]) -> None:
        for identity in pool:
            canon_norm, canon_key, alias_pairs = IDENTITY_SCORING_CACHE.get(
                identity,
                (
                    normalize(identity.canonical),
                    normalize_key(identity.canonical),
                    tuple((normalize(alias), normalize_key(alias)) for alias in identity.aliases if normalize(alias)),
                ),
            )
            if is_ambiguous_key(canon_key) or is_weak_single_token(canon_norm, canon_key, canonical=True):
                continue
            if canon_norm and canon_norm in joined:
                consider(identity, 0.95, "contains:canonical")
            for alias_norm, alias_key in alias_pairs:
                if is_ambiguous_key(alias_key) or is_weak_single_token(alias_norm, alias_key):
                    continue
                if alias_key in joined_key:
                    consider(identity, 0.98 if alias_norm in normalized_pieces else 0.92, f"alias:{alias_norm}")
                elif alias_norm in joined:
                    consider(identity, 0.9, f"substring:{alias}")

            # Handle leading-date patterns in filenames, common for downloads.
            if stem_tokens:
                lead = stem_tokens[0]
                if normalize_key(lead) == canon_key:
                    consider(identity, 0.97, "date-prefix+lead-token")
                elif len(stem_tokens) > 1 and normalize_key(stem_tokens[1]) == canon_key:
                    consider(identity, 0.96, "date-prefix+second-token")

            # Title-only Reddit downloads often embed the poster's name in the caption.
            if title_joined:
                if canon_norm in title_joined:
                    consider(identity, 0.91, "title-contains-canonical")
                for alias_norm, alias_key in alias_pairs:
                    if (
                        alias_norm
                        and not is_ambiguous_key(alias_key)
                        and not is_weak_single_token(alias_norm, alias_key)
                        and alias_norm in title_joined
                    ):
                        consider(identity, 0.93, f"title-contains-alias:{alias_norm}")

            if ocr_joined:
                if canon_norm in ocr_joined:
                    consider(identity, 0.87, "ocr-contains-canonical")
                for alias_norm, alias_key in alias_pairs:
                    if alias_key in ocr_joined_key:
                        consider(identity, 0.89 if alias_norm in ocr_joined else 0.86, f"ocr-alias:{alias_norm}")
                    elif alias_norm in ocr_joined:
                        consider(identity, 0.84, f"ocr-substring:{alias_norm}")

    score_identity_pool(candidate_identities)

    if cache_key is not None and match_cache is not None:
        match_cache[cache_key] = best

    return best


def audit_path(run_id: str, audit_root: Path) -> Path:
    return audit_root / f"{run_id}.json"


def unique_list(items: Iterable[str]) -> List[str]:
    return list(dict.fromkeys(item for item in items if item))


def ocr_enabled() -> bool:
    return bool(
        os.environ.get("PICORG_OCR_IMAGE", "").strip()
        or os.environ.get("PICORG_OCR_COMMAND_JSON", "").strip()
    )


def render_ocr_command(path: Path) -> Optional[List[str]]:
    command_json = os.environ.get("PICORG_OCR_COMMAND_JSON", "").strip()
    image = os.environ.get("PICORG_OCR_IMAGE", "").strip()

    if command_json:
        try:
            template = json.loads(command_json)
        except json.JSONDecodeError:
            return None
        if not isinstance(template, list) or not all(isinstance(item, str) for item in template):
            return None
        mapping = {
            "path": str(path),
            "dir": str(path.parent),
            "name": path.name,
            "stem": path.stem,
        }
        return [part.format(**mapping) for part in template]

    if image:
        return [
            "docker",
            "run",
            "--rm",
            "-i",
            "--entrypoint",
            "tesseract",
            "-v",
            f"{path.parent}:/work:ro",
            image,
            f"/work/{path.name}",
            "stdout",
            "--psm",
            "11",
            "--oem",
            "1",
        ]

    return None


def extract_ocr_text(path: Path) -> Optional[str]:
    if path.suffix.lower() not in OCR_SUPPORTED_SUFFIXES:
        return None
    command = render_ocr_command(path)
    if not command:
        return None
    timeout_seconds = int(os.environ.get("PICORG_OCR_TIMEOUT_SECONDS", str(DEFAULT_OCR_TIMEOUT_SECONDS)))
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    text = (completed.stdout or "").strip()
    return text or None


def build_gallery_summary(results: List[MatchResult]) -> List[Dict[str, object]]:
    grouped: Dict[Tuple[str, str, str], Dict[str, object]] = {}
    for result in results:
        title = result.title or title_from_path(Path(result.path))
        base = gallery_base_title(title)
        if not base:
            continue
        identity_key = result.predicted or "unmatched"
        key = (normalize(base), identity_key, result.source_family or "unknown")
        entry = grouped.get(key)
        if entry is None:
            entry = {
                "base_title": base,
                "base_title_key": normalize(base),
                "identity": identity_key,
                "source_families": Counter(),
                "source_roots": Counter(),
                "paths": [],
                "titles": [],
                "results": [],
            }
            grouped[key] = entry
        entry["source_families"][result.source_family or "unknown"] += 1
        entry["source_roots"][result.source_root] += 1
        entry["paths"].append(result.path)
        entry["titles"].append(title)
        entry["results"].append(result)

    summary: List[Dict[str, object]] = []
    for entry in grouped.values():
        items = entry["results"]
        if len(items) < 2:
            continue
        summary.append(
            {
                "base_title": entry["base_title"],
                "base_title_key": entry["base_title_key"],
                "identity": entry["identity"],
                "count": len(items),
                "source_families": dict(sorted(entry["source_families"].items(), key=lambda item: (-item[1], item[0]))),
                "source_roots": dict(sorted(entry["source_roots"].items(), key=lambda item: (-item[1], item[0]))),
                "sample_paths": entry["paths"][:5],
                "sample_titles": unique_list(entry["titles"])[:5],
            }
        )
    summary.sort(key=lambda item: (-item["count"], item["base_title_key"], item["identity"]))
    return summary


def make_run_id() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def run_dry(root_paths: Sequence[Path], apply: bool = False) -> Tuple[List[MatchResult], Dict[str, object]]:
    catalog, alias_index, canonical_index, token_index, preferred_alias_targets = load_identity_catalog()
    ocr_enabled_flag = ocr_enabled()
    cache_state = dry_run_state(root_paths, ocr_enabled_flag)
    cache_file = dry_run_cache_path()
    if not apply:
        cached = load_dry_run_cache(cache_file, cache_state)
        if cached is not None:
            results, report = cached
            report = dict(report)
            report["cached"] = True
            report.setdefault("cached_roots", len(root_paths))
            return results, report
    cache_payload = load_dry_run_cache_payload(cache_file) or {}
    root_cache_payload = cache_payload.get("roots")
    if not isinstance(root_cache_payload, dict):
        root_cache_payload = {}
    results: List[MatchResult] = []
    match_cache: Dict[str, Tuple[Optional[Identity], float, str]] = {}
    metrics = Counter()
    confidence_buckets = Counter()
    family_hits = Counter()
    ground_truth_total = 0
    ground_truth_correct = 0
    ground_truth_predicted = 0
    ground_truth_false_positive = 0
    ground_truth_by_root = Counter()
    ground_truth_correct_by_root = Counter()
    ocr_attempted = 0
    ocr_improved = 0
    ocr_matches = 0
    cached_roots = 0
    root_cache_state_payload: Dict[str, Dict[str, object]] = {}

    def add_summary(summary: Dict[str, object]) -> None:
        nonlocal ground_truth_total, ground_truth_correct, ground_truth_predicted
        nonlocal ground_truth_false_positive, ocr_attempted, ocr_improved, ocr_matches
        metrics["matched"] += int(summary.get("matched", 0))
        metrics["unmatched"] += int(summary.get("unmatched", 0))
        metrics["high_confidence"] += int(summary.get("high_confidence", 0))
        metrics["medium_confidence"] += int(summary.get("medium_confidence", 0))
        metrics["low_confidence"] += int(summary.get("low_confidence", 0))
        confidence_buckets.update(summary.get("confidence_buckets", {}))
        family_hits.update(summary.get("family_hits", {}))
        ground_truth_total += int(summary.get("ground_truth_total", 0))
        ground_truth_correct += int(summary.get("ground_truth_correct", 0))
        ground_truth_predicted += int(summary.get("ground_truth_predicted", 0))
        ground_truth_false_positive += int(summary.get("ground_truth_false_positive", 0))
        ground_truth_by_root.update(summary.get("ground_truth_by_root", {}))
        ground_truth_correct_by_root.update(summary.get("ground_truth_correct_by_root", {}))
        ocr_attempted += int(summary.get("ocr_attempted", 0))
        ocr_matches += int(summary.get("ocr_matches", 0))
        ocr_improved += int(summary.get("ocr_improved", 0))

    for root in root_paths:
        if not root.exists():
            continue
        root_key = str(root)
        root_state = dry_run_root_state(root, ocr_enabled_flag)
        cached_root_payload = root_cache_payload.get(root_key)
        if (
            not apply
            and isinstance(cached_root_payload, dict)
            and cached_root_payload.get("state") == root_state
        ):
            cached_root_results = _results_from_payload(cached_root_payload.get("results") or [])
            cached_root_report = cached_root_payload.get("report")
            if cached_root_results is not None:
                results.extend(cached_root_results)
                cached_roots += 1
                if isinstance(cached_root_report, dict):
                    add_summary(cached_root_report)
                root_cache_state_payload[root_key] = {
                    "state": root_state,
                    "results": [asdict(result) for result in cached_root_results],
                    "report": cached_root_report if isinstance(cached_root_report, dict) else {},
                }
                continue
        root_start = len(results)
        root_metrics = Counter()
        root_confidence_buckets = Counter()
        root_family_hits = Counter()
        root_ground_truth_total = 0
        root_ground_truth_correct = 0
        root_ground_truth_predicted = 0
        root_ground_truth_false_positive = 0
        root_ocr_attempted = 0
        root_ocr_improved = 0
        root_ocr_matches = 0
        for path in walk_media_files(root):
            identity, confidence, rule = best_identity_match(
                path,
                root,
                catalog,
                alias_index,
                token_index,
                preferred_alias_targets=preferred_alias_targets,
                match_cache=match_cache,
            )
            ocr_text = None
            if ocr_enabled_flag and (not identity or confidence < DEFAULT_OCR_TRIGGER_CONFIDENCE):
                ocr_text = extract_ocr_text(path)
                if ocr_text:
                    root_ocr_attempted += 1
                    ocr_identity, ocr_confidence, ocr_rule = best_identity_match(
                        path,
                        root,
                        catalog,
                        alias_index,
                        token_index,
                        preferred_alias_targets=preferred_alias_targets,
                        match_cache=match_cache,
                        ocr_text=ocr_text,
                    )
                    if ocr_identity:
                        root_ocr_matches += 1
                    if ocr_identity and (
                        not identity
                        or ocr_confidence > confidence
                        or (ocr_confidence == confidence and ocr_rule.startswith("ocr"))
                    ):
                        identity, confidence, rule = ocr_identity, ocr_confidence, ocr_rule
                        root_ocr_improved += 1
            expected = guess_expected_identity(path, root, alias_index)
            expected_available = expected is not None
            title = title_from_path(path)
            reddit_context = extract_reddit_context(path)
            source_family = infer_source_family(path) or ("manual" if root == Path("/mnt/desktop/Pictures") else None)
            source_detail = None
            if reddit_context["subreddits"]:
                source_detail = f"subreddit:{reddit_context['subreddits'][0]}"
            elif reddit_context["users"]:
                source_detail = f"user:{reddit_context['users'][0]}"
            result = MatchResult(
                path=str(path),
                source_root=str(root),
                family=identity.family if identity else None,
                canonical=identity.canonical if identity else None,
                confidence=round(confidence, 4),
                rule=rule,
                expected_identity=expected,
                expected_available=expected_available,
                reddit_post_ids=tuple(reddit_context["reddit_post_ids"]),
                metadata_users=tuple(reddit_context["metadata_users"]),
                metadata_subreddits=tuple(reddit_context["metadata_subreddits"]),
                title=title or None,
                source_family=source_family,
                source_detail=source_detail,
                ocr_used=bool(ocr_text),
            )
            results.append(result)

            if identity:
                root_metrics["matched"] += 1
                root_family_hits[identity.family] += 1
                if confidence >= 0.95:
                    root_metrics["high_confidence"] += 1
                elif confidence >= 0.80:
                    root_metrics["medium_confidence"] += 1
                else:
                    root_metrics["low_confidence"] += 1
                root_confidence_buckets[f"{int(confidence * 10) / 10:.1f}"] += 1
            else:
                root_metrics["unmatched"] += 1
            if expected_available:
                root_ground_truth_total += 1
                if identity:
                    root_ground_truth_predicted += 1
                    if normalize_key(identity.canonical) == normalize_key(expected):
                        root_ground_truth_correct += 1
                    else:
                        root_ground_truth_false_positive += 1
        root_summary = {
            "scanned": len(results[root_start:]),
            "matched": root_metrics["matched"],
            "unmatched": root_metrics["unmatched"],
            "high_confidence": root_metrics["high_confidence"],
            "medium_confidence": root_metrics["medium_confidence"],
            "low_confidence": root_metrics["low_confidence"],
            "family_hits": dict(root_family_hits),
            "confidence_buckets": dict(root_confidence_buckets),
            "ground_truth_total": root_ground_truth_total,
            "ground_truth_correct": root_ground_truth_correct,
            "ground_truth_predicted": root_ground_truth_predicted,
            "ground_truth_false_positive": root_ground_truth_false_positive,
            "ground_truth_accuracy": round(root_ground_truth_correct / root_ground_truth_total, 4) if root_ground_truth_total else None,
            "ground_truth_precision": round(root_ground_truth_correct / root_ground_truth_predicted, 4) if root_ground_truth_predicted else None,
            "ground_truth_recall": round(root_ground_truth_correct / root_ground_truth_total, 4) if root_ground_truth_total else None,
            "ground_truth_by_root": {str(root): root_ground_truth_total},
            "ground_truth_correct_by_root": {str(root): root_ground_truth_correct},
            "ocr_enabled": ocr_enabled_flag,
            "ocr_attempted": root_ocr_attempted,
            "ocr_matches": root_ocr_matches,
            "ocr_improved": root_ocr_improved,
        }
        add_summary(root_summary)
        if not apply:
            root_cache_state_payload[root_key] = {
                "state": root_state,
                "results": [asdict(result) for result in results[root_start:]],
                "report": root_summary,
            }
            write_dry_run_cache(cache_file, cache_state, [], {}, root_cache=root_cache_state_payload)

    source_metrics: Dict[str, Dict[str, object]] = {}
    for result in results:
        source = result.source_family or "unknown"
        source_report = source_metrics.setdefault(source, {"scanned": 0, "matched": 0, "unmatched": 0})
        source_report["scanned"] += 1
        if result.canonical:
            source_report["matched"] += 1
        else:
            source_report["unmatched"] += 1
    for source_report in source_metrics.values():
        source_report["coverage"] = round(source_report["matched"] / source_report["scanned"], 4) if source_report["scanned"] else 0.0

    report = {
        "roots": [str(p) for p in root_paths],
        "scanned": len(results),
        "matched": metrics["matched"],
        "unmatched": metrics["unmatched"],
        "high_confidence": metrics["high_confidence"],
        "medium_confidence": metrics["medium_confidence"],
        "low_confidence": metrics["low_confidence"],
        "family_hits": dict(family_hits),
        "confidence_buckets": dict(sorted(confidence_buckets.items())),
        "ground_truth_total": ground_truth_total,
        "ground_truth_correct": ground_truth_correct,
        "ground_truth_predicted": ground_truth_predicted,
        "ground_truth_false_positive": ground_truth_false_positive,
        "ground_truth_accuracy": round(ground_truth_correct / ground_truth_total, 4) if ground_truth_total else None,
        "ground_truth_precision": round(ground_truth_correct / ground_truth_predicted, 4) if ground_truth_predicted else None,
        "ground_truth_recall": round(ground_truth_correct / ground_truth_total, 4) if ground_truth_total else None,
        "match_coverage": round(metrics["matched"] / len(results), 4) if results else 0.0,
        "source_metrics": source_metrics,
        "ground_truth_by_root": dict(ground_truth_by_root),
        "ground_truth_correct_by_root": dict(ground_truth_correct_by_root),
        "ocr_enabled": ocr_enabled_flag,
        "ocr_attempted": ocr_attempted,
        "ocr_matches": ocr_matches,
        "ocr_improved": ocr_improved,
        "gallery_sets": build_gallery_summary(results),
    }

    report["applied"] = False
    report["cached"] = cached_roots > 0
    report["cached_roots"] = cached_roots
    if not apply:
        write_dry_run_cache(cache_file, cache_state, results, report, root_cache=root_cache_state_payload)

    return results, report


def write_audit(
    results: List[MatchResult],
    report: Dict[str, object],
    run_id: str,
    audit_root: Path,
) -> Path:
    audit_root.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": run_id,
        "report": report,
        "results": [asdict(result) for result in results],
    }
    path = audit_path(run_id, audit_root)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def result_to_sidecar(result: MatchResult, source_path: Path, dest_path: Optional[Path], run_id: str) -> Dict[str, object]:
    payload = {
        "run_id": run_id,
        "source_path": str(source_path),
        "source_root": result.source_root,
        "source_family": result.source_family,
        "source_detail": result.source_detail,
        "resolved_family": result.family,
        "resolved_identity": result.canonical,
        "predicted": result.predicted,
        "confidence": result.confidence,
        "rule": result.rule,
        "title": result.title,
        "expected_identity": result.expected_identity,
        "expected_available": result.expected_available,
        "resolver_version": DEFAULT_RESOLVER_VERSION,
    }
    if dest_path is not None:
        payload["dest_path"] = str(dest_path)
    try:
        payload["sha256"] = file_sha256(source_path)
    except OSError:
        payload["sha256"] = None
    return payload


def folder_signature(dir_results: List[MatchResult]) -> str:
    pieces: List[str] = []
    for result in sorted(dir_results, key=lambda item: Path(item.path).name):
        src = Path(result.path)
        try:
            file_sig = file_sha256(src, limit_bytes=128 * 1024)
        except OSError:
            file_sig = "missing"
        pieces.append(f"{src.name}:{file_sig}")
    digest = hashlib.sha256("|".join(pieces).encode("utf-8")).hexdigest()
    return digest


def destination_for(identity: Identity, dest_root: Path = DEST_ROOT) -> Path:
    family = identity.family
    if family in {"reddit_subreddit", "reddit_follow"}:
        family = "redditdaily"
    return dest_root / family / identity.canonical


def resolve_target_path(dest_dir: Path, source_path: Path) -> Path:
    candidate = dest_dir / source_path.name
    if not candidate.exists():
        return candidate
    stem = source_path.stem
    suffix = source_path.suffix
    digest = file_sha256(source_path, limit_bytes=128 * 1024)[:10]
    return dest_dir / f"{stem}__{digest}{suffix}"


def duplicate_path_for(dest_root: Path, identity: Identity, source_path: Path) -> Path:
    family = identity.family
    if family in {"reddit_subreddit", "reddit_follow"}:
        family = "redditdaily"
    digest = file_sha256(source_path, limit_bytes=128 * 1024)[:10]
    return dest_root / "duplicates" / family / identity.canonical / f"{source_path.stem}__{digest}{source_path.suffix}"


def write_sidecar(sidecar_path: Path, payload: Dict[str, object]) -> None:
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def apply_results(
    results: List[MatchResult],
    dest_root: Path,
    audit_root: Path,
    run_id: str,
) -> Dict[str, object]:
    protected = tuple(root.resolve() for root in PROTECTED_SOURCE_ROOTS)
    missing_sources = []
    for result in results:
        for candidate in (Path(result.source_root), Path(result.path)):
            resolved = candidate.resolve()
            if any(resolved == root or root in resolved.parents for root in protected):
                raise RuntimeError(
                    f"Refusing to apply protected social source: {candidate}"
                )
        source_path = Path(result.path)
        if not source_path.is_file():
            missing_sources.append(source_path)
    if missing_sources:
        sample = ", ".join(str(path) for path in missing_sources[:3])
        raise RuntimeError(
            "Refusing non-atomic apply: "
            f"{len(missing_sources)} source paths disappeared before apply; sample: {sample}"
        )
    grouped: Dict[Tuple[Path, Path], List[MatchResult]] = defaultdict(list)
    for result in results:
        grouped[(Path(result.source_root), Path(result.path).parent)].append(result)

    moved_prefixes: Set[Path] = set()
    folders_moved = 0
    duplicate_folders = 0
    applied = 0
    skipped = 0
    duplicates = 0
    unmatched = 0
    weak_skipped = 0

    folder_candidates: List[Tuple[Path, Path, Identity, List[MatchResult]]] = []
    seen_folder_sigs: Dict[Tuple[str, str, str], Path] = {}
    for (source_root, source_dir), dir_results in grouped.items():
        if source_dir == source_root:
            continue
        if not dir_results:
            continue
        if any(
            not r.family
            or not r.canonical
            or r.confidence < DEFAULT_APPLY_MIN_CONFIDENCE
            for r in dir_results
        ):
            continue
        identities = {(r.family, r.canonical) for r in dir_results}
        if len(identities) != 1:
            continue
        family, canonical = next(iter(identities))
        folder_candidates.append((source_root, source_dir, Identity(canonical, family, ()), dir_results))

    folder_candidates.sort(key=lambda item: len(item[1].parts), reverse=True)

    for source_root, source_dir, identity, dir_results in folder_candidates:
        if any(source_dir == moved or str(source_dir).startswith(str(moved) + os.sep) for moved in moved_prefixes):
            continue
        sig = folder_signature(dir_results)
        folder_sig_key = (identity.family, identity.canonical, sig)
        if folder_sig_key in seen_folder_sigs:
            duplicate_folders += 1
            dest_dir = dest_root / "duplicates" / (
                "redditdaily" if identity.family in {"reddit_subreddit", "reddit_follow"} else identity.family
            ) / identity.canonical / source_dir.name
        else:
            dest_dir = destination_for(identity, dest_root=dest_root) / source_dir.name
        if dest_dir.exists():
            continue
        dest_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source_dir), str(dest_dir))
        folder_payload = {
            "run_id": run_id,
            "source_root": str(source_root),
            "source_dir": str(source_dir),
            "dest_dir": str(dest_dir),
            "resolved_family": identity.family,
            "resolved_identity": identity.canonical,
            "file_count": len(dir_results),
            "resolver_version": DEFAULT_RESOLVER_VERSION,
            "files": [Path(r.path).name for r in dir_results],
        }
        write_sidecar(dest_dir / "picorg_folder.json", folder_payload)
        moved_prefixes.add(source_dir)
        folders_moved += 1
        seen_folder_sigs[folder_sig_key] = dest_dir

    for result in results:
        src = Path(result.path)
        if any(str(src).startswith(str(prefix) + os.sep) or src.parent == prefix for prefix in moved_prefixes):
            continue
        if not result.family or not result.canonical:
            unmatched += 1
            continue
        if result.confidence < DEFAULT_APPLY_MIN_CONFIDENCE:
            weak_skipped += 1
            continue
        identity = Identity(result.canonical, result.family, ())
        dest_dir = destination_for(identity, dest_root=dest_root)
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = resolve_target_path(dest_dir, src)
        if target.exists():
            try:
                if file_hash(target) == file_hash(src):
                    duplicate_target = duplicate_path_for(dest_root, identity, src)
                    duplicate_target.parent.mkdir(parents=True, exist_ok=True)
                    duplicate_sidecar = duplicate_target.with_suffix(duplicate_target.suffix + ".json")
                    duplicate_payload = result_to_sidecar(result, src, duplicate_target, run_id)
                    duplicate_payload["duplicate_of"] = str(target)
                    shutil.move(str(src), str(duplicate_target))
                    write_sidecar(duplicate_sidecar, duplicate_payload)
                    duplicates += 1
                    continue
            except OSError:
                pass
            skipped += 1
            continue
        sidecar_payload = result_to_sidecar(result, src, target, run_id)
        shutil.move(str(src), str(target))
        write_sidecar(target.with_suffix(target.suffix + ".json"), sidecar_payload)
        applied += 1
        if target.name != src.name:
            duplicates += 1
    return {
        "applied_files": applied,
        "applied_folders": folders_moved,
        "skipped_existing": skipped,
        "duplicates_renamed": duplicates,
        "duplicates_detected": duplicates,
        "duplicate_folders_detected": duplicate_folders,
        "unmatched_seen": unmatched,
        "weak_skipped": weak_skipped,
        "dest_root": str(dest_root),
        "audit_root": str(audit_root),
        "run_id": run_id,
    }


def print_summary(results: List[MatchResult], report: Dict[str, object], limit: int = 40) -> None:
    print(f"scanned: {report['scanned']}")
    print(f"matched: {report['matched']}")
    print(f"unmatched: {report['unmatched']}")
    print(f"high_confidence: {report['high_confidence']}")
    print(f"ground_truth_accuracy: {report['ground_truth_accuracy']}")
    print(f"cached: {report.get('cached')}")
    print(f"ocr_enabled: {report.get('ocr_enabled')}")
    if report.get("ocr_enabled"):
        print(f"ocr_attempted: {report.get('ocr_attempted')}")
        print(f"ocr_matches: {report.get('ocr_matches')}")
        print(f"ocr_improved: {report.get('ocr_improved')}")
    print("family_hits:")
    for family, count in sorted(report["family_hits"].items(), key=lambda item: (-item[1], item[0])):
        print(f"  {family}: {count}")

    unresolved = [r for r in results if not r.family or not r.canonical]
    weak = [r for r in results if r.family and r.confidence < 0.8]
    print("top_unmatched:")
    for item in unresolved[:limit]:
        print(f"  {item.path} | title={item.title}")
    print("top_weak_matches:")
    for item in weak[:limit]:
        print(f"  {item.path} -> {item.predicted} ({item.confidence:.2f}) rule={item.rule}")
    gallery_sets = report.get("gallery_sets") or []
    if gallery_sets:
        print("top_gallery_sets:")
        for item in gallery_sets[:limit]:
            print(
                f"  {item['count']} files | {item['base_title']} | identity={item['identity']} | "
                f"roots={item['source_roots']} | families={item['source_families']}"
            )


def export_manifest(catalog: List[Identity], output: Path) -> None:
    grouped: Dict[str, List[Dict[str, object]]] = defaultdict(list)
    for identity in catalog:
        grouped[identity.family].append(
            {
                "canonical": identity.canonical,
                "aliases": list(identity.aliases),
                "destination": str(destination_for(identity)),
            }
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(grouped, indent=2, sort_keys=True), encoding="utf-8")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deterministic mixed media organizer")
    sub = parser.add_subparsers(dest="command", required=True)

    dry = sub.add_parser("dry-run", help="Match files without moving them")
    dry.add_argument("--root", action="append", type=Path, help="Override intake roots")
    dry.add_argument("--apply", action="store_true", help="Actually move files")
    dry.add_argument("--dest-root", type=Path, default=DEST_ROOT, help="Canonical destination root")
    dry.add_argument("--audit-out", type=Path, help="Optional audit JSON path")
    dry.add_argument("--audit-root", type=Path, default=DEFAULT_AUDIT_ROOT, help="Directory for automatic audit JSON")
    dry.add_argument("--limit", type=int, default=40, help="Summary row limit")

    manifest = sub.add_parser("manifest", help="Export the identity manifest as JSON")
    manifest.add_argument("--output", type=Path, required=True)

    inspect = sub.add_parser("inspect", help="Print a compact catalog summary")
    inspect.add_argument("--limit", type=int, default=60)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    catalog, alias_index, _, _, _ = load_identity_catalog()

    if args.command == "manifest":
        export_manifest(catalog, args.output)
        print(args.output)
        return 0

    if args.command == "inspect":
        print(f"identities: {len(catalog)}")
        counts = Counter(identity.family for identity in catalog)
        for family, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
            print(f"{family}: {count}")
        for identity in catalog[: args.limit]:
            print(f"{identity.family}/{identity.canonical} | aliases={len(identity.aliases)}")
        return 0

    roots = args.root if args.root else DEFAULT_INTAKE_ROOTS
    run_id = make_run_id()
    # Apply must always use a fresh scan. Cached dry-run results can reference
    # files that were moved or removed after the cache was written.
    results, report = run_dry(roots, apply=args.apply)
    if args.apply:
        apply_report = apply_results(results, args.dest_root, args.audit_root, run_id)
        report["apply"] = apply_report
    audit_file = write_audit(results, report, run_id, args.audit_root)
    report["audit_file"] = str(audit_file)
    if args.audit_out:
        args.audit_out.parent.mkdir(parents=True, exist_ok=True)
        args.audit_out.write_text(
            json.dumps({"report": report, "results": [asdict(r) for r in results]}, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    print_summary(results, report, limit=args.limit)
    print(f"audit: {audit_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
