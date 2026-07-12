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
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple


DEFAULT_INTAKE_ROOTS = [
    Path("/mnt/elements16/@mixedpics"),
    Path("/mnt/elements16a/Pron/jdownloaderscomplete"),
    Path("/mnt/desktop/Pictures"),
]

DEST_ROOT = Path("/mnt/elements16/@mixedpics_sorted")
DEFAULT_AUDIT_ROOT = Path("/tmp/picorg_sorted_audit")
DEFAULT_RESOLVER_VERSION = "2026-07-08.1"
DEFAULT_OCR_TIMEOUT_SECONDS = 20
DEFAULT_OCR_TRIGGER_CONFIDENCE = 0.85
OCR_SUPPORTED_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}

FRIENDS_FILE = Path("/opt/redditgrab/friend.txt")
PSCRAPE_FILE = Path("/opt/pscrape/redditors.txt")
IMDB_FILE = Path("/opt/list.imdburl")
METADAILY_ACCOUNTS_FILE = Path("/opt/metadaily/social_accounts.txt")
PROJECT_REGISTRY_FILE = Path("/opt/picorg/project_registry.json")
REDDITDAILY_ROOT = Path("/mnt/elements16a/Pron/redditdaily")
PSCRAPE_ROOT = Path("/mnt/elements16a/Pron/pscrape")
PROJECT_BLOCKED_TOKENS: Set[str] = set()

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
    r"(?i)\b(?:posted in|from|in|subreddit|reddit)\b[:\s_-]+([a-z0-9][a-z0-9_+-]{1,})"
)


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
    title: Optional[str] = None
    source_family: Optional[str] = None
    source_detail: Optional[str] = None
    ocr_used: bool = False

    @property
    def predicted(self) -> Optional[str]:
        if self.family and self.canonical:
            return f"{self.family}/{self.canonical}"
        return None


def normalize(text: str) -> str:
    text = text.lower().strip()
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_key(text: str) -> str:
    return normalize(text).replace(" ", "")


def slugify(text: str) -> str:
    key = normalize(text).replace(" ", "_")
    return key or "unknown"


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
    identities: List[Identity] = []
    alias_index: Dict[str, Set[Identity]] = defaultdict(set)
    canonical_index: Dict[str, Identity] = {}
    token_index: Dict[str, Set[Identity]] = defaultdict(set)
    global PROJECT_BLOCKED_TOKENS
    PROJECT_BLOCKED_TOKENS = set()

    def register_alias(alias: str, identity: Identity) -> None:
        alias_key = normalize_key(alias)
        if not alias_key:
            return
        alias_index[alias_key].add(identity)
        for token in tokenize(normalize(alias)):
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
    preferred_alias_targets = {
        normalize_key(alias): normalize_key(target)
        for alias, target in registry.get("preferred_alias_targets", {}).items()
        if normalize_key(alias) and normalize_key(target)
    }
    add_registry_entries(registry.get("entries", []))

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
        for child in root.iterdir():
            if not child.is_dir():
                continue
            name = child.name
            if name in IGNORED_DIR_NAMES or name.startswith("."):
                continue
            if family == "redditdaily" and name in {"downloads", "downloads_backup", "cache", "backups", "legacy_backups"}:
                continue
            aliases = {name}
            add_identity(name, family, aliases)

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


def extract_reddit_context(path: Path) -> Dict[str, List[str]]:
    raw = " / ".join(path.parts)
    matches = {
        "subreddits": [],
        "users": [],
        "context": [],
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
    return {key: unique_list(values) for key, values in matches.items()}


def infer_source_family(path: Path) -> Optional[str]:
    parts = {normalize_key(part) for part in path.parts}
    if any(part in {"redditdaily", "redditdailydownloads"} for part in parts):
        return "redditdaily"
    if any(part in {"metadaily"} for part in parts):
        return "metadaily"
    if any(part in {"pscrape"} for part in parts):
        return "pscrape"
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

    normalized_pieces = [normalize(piece) for piece in pieces if normalize(piece)]
    joined = " ".join(normalized_pieces)
    joined_key = normalize_key(joined)
    ocr_joined = normalize(ocr_text or "")
    ocr_joined_key = normalize_key(ocr_joined)
    reddit_context = extract_reddit_context(path)
    cache_key = None
    if match_cache is not None:
        ctx_key = "|".join(
            [
                joined_key,
                ",".join(sorted(reddit_context["subreddits"])),
                ",".join(sorted(reddit_context["users"])),
                ocr_joined_key,
            ]
        )
        cache_key = ctx_key
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

    for piece in normalized_pieces:
        exact_key = normalize_key(piece)
        exact_hits = alias_index.get(exact_key, set())
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

    def score_identity_pool(pool: Iterable[Identity]) -> None:
        for identity in pool:
            canon_norm = normalize(identity.canonical)
            if canon_norm and canon_norm in joined:
                consider(identity, 0.95, "contains:canonical")
            for alias in identity.aliases:
                alias_norm = normalize(alias)
                if not alias_norm:
                    continue
                alias_key = normalize_key(alias_norm)
                if alias_key in joined_key:
                    consider(identity, 0.98 if alias_norm in normalized_pieces else 0.92, f"alias:{alias}")
                elif alias_norm in joined:
                    consider(identity, 0.9, f"substring:{alias}")

            # Handle leading-date patterns in filenames, common for downloads.
            stem = path.stem
            without_date = DATE_PREFIX.sub("", stem)
            first_token = tokenize(without_date[:64])
            if first_token:
                lead = first_token[0]
                if normalize_key(lead) == normalize_key(identity.canonical):
                    consider(identity, 0.97, "date-prefix+lead-token")
                elif len(first_token) > 1 and normalize_key(first_token[1]) == normalize_key(identity.canonical):
                    consider(identity, 0.96, "date-prefix+second-token")

            # Title-only Reddit downloads often embed the poster's name in the caption.
            title = reddit_context["context"]
            if title:
                title_joined = normalize(" ".join(title))
                if title_joined:
                    if normalize(identity.canonical) in title_joined:
                        consider(identity, 0.91, "title-contains-canonical")
                    for alias in identity.aliases:
                        alias_norm = normalize(alias)
                        if alias_norm and alias_norm in title_joined:
                            consider(identity, 0.93, f"title-contains-alias:{alias}")

            if ocr_joined:
                if normalize(identity.canonical) in ocr_joined:
                    consider(identity, 0.87, "ocr-contains-canonical")
                for alias in identity.aliases:
                    alias_norm = normalize(alias)
                    if not alias_norm:
                        continue
                    alias_key = normalize_key(alias_norm)
                    if alias_key in ocr_joined_key:
                        consider(identity, 0.89 if alias_norm in ocr_joined else 0.86, f"ocr-alias:{alias}")
                    elif alias_norm in ocr_joined:
                        consider(identity, 0.84, f"ocr-substring:{alias}")

    score_identity_pool(candidate_identities)
    if best[1] < 0.95:
        score_identity_pool(catalog)

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
    results: List[MatchResult] = []
    match_cache: Dict[str, Tuple[Optional[Identity], float, str]] = {}
    metrics = Counter()
    confidence_buckets = Counter()
    family_hits = Counter()
    ground_truth_total = 0
    ground_truth_correct = 0
    ground_truth_by_root = Counter()
    ground_truth_correct_by_root = Counter()
    ocr_attempted = 0
    ocr_improved = 0
    ocr_matches = 0
    ocr_enabled_flag = ocr_enabled()

    for root in root_paths:
        if not root.exists():
            continue
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
                    ocr_attempted += 1
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
                        ocr_matches += 1
                    if ocr_identity and (
                        not identity
                        or ocr_confidence > confidence
                        or (ocr_confidence == confidence and ocr_rule.startswith("ocr"))
                    ):
                        identity, confidence, rule = ocr_identity, ocr_confidence, ocr_rule
                        ocr_improved += 1
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
                title=title or None,
                source_family=source_family,
                source_detail=source_detail,
                ocr_used=bool(ocr_text),
            )
            results.append(result)

            if identity:
                metrics["matched"] += 1
                family_hits[identity.family] += 1
                if confidence >= 0.95:
                    metrics["high_confidence"] += 1
                elif confidence >= 0.80:
                    metrics["medium_confidence"] += 1
                else:
                    metrics["low_confidence"] += 1
                confidence_buckets[f"{int(confidence * 10) / 10:.1f}"] += 1
            else:
                metrics["unmatched"] += 1
            if expected_available:
                ground_truth_total += 1
                ground_truth_by_root[str(root)] += 1
                if identity and normalize_key(identity.canonical) == normalize_key(expected):
                    ground_truth_correct += 1
                    ground_truth_correct_by_root[str(root)] += 1

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
        "ground_truth_accuracy": round(ground_truth_correct / ground_truth_total, 4) if ground_truth_total else None,
        "ground_truth_by_root": dict(ground_truth_by_root),
        "ground_truth_correct_by_root": dict(ground_truth_correct_by_root),
        "ocr_enabled": ocr_enabled_flag,
        "ocr_attempted": ocr_attempted,
        "ocr_matches": ocr_matches,
        "ocr_improved": ocr_improved,
        "gallery_sets": build_gallery_summary(results),
    }

    report["applied"] = False

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

    folder_candidates: List[Tuple[Path, Path, Identity, List[MatchResult]]] = []
    seen_folder_sigs: Dict[Tuple[str, str, str], Path] = {}
    for (source_root, source_dir), dir_results in grouped.items():
        if source_dir == source_root:
            continue
        if not dir_results:
            continue
        if any(not r.family or not r.canonical for r in dir_results):
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
    results, report = run_dry(roots, apply=False)
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
