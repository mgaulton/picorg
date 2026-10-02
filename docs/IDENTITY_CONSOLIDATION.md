# Linked identity consolidation

PicOrg reads `/opt/shared/identity_aliases.json` as a read-only authority. Only
records with `status: "confirmed"` participate in identity matching.

## Canonical identity and source aliases

- `id` is the shared canonical name used in PicOrg's identity catalog and
  holding paths.
- `primary_folder` and `metadaily_user_group` remain MetaDaily-scoped names.
- `reddit.users` and `reddit.subreddits` remain Reddit-scoped names.
- `display_names` and `search_terms` remain searchable labels. `sources` is
  retained as provenance. `source_only_terms` is not imported because the
  current registry entries do not provide a source key for those terms.
- Enabled `metadaily_collection_sources` add their `clean_identifier` and
  `identifier` as platform-scoped aliases. Disabled/legacy sources and
  aggregate identities are excluded to avoid stale or group-to-member matches.
- Confirmed records that describe aggregate groups continue to omit
  `display_names` and `search_terms` from matching, as before.
- If one source-scoped alias belongs to multiple confirmed IDs, PicOrg does not
  use it as a preferred target. Matching can still report the ambiguity.

The UI exposes the common ID as the identity name and includes the retained
source aliases and provenance in catalog data. Source folders, the registry,
and RD/MD databases are not modified by catalog loading or report generation.

## Holding paths and review manifest

Linked identities use `<review-root>/<id>` directly. Existing family folders
remain readable during review. To inventory media already under the PicOrg
sorted tree and write a report-only proposal:

```sh
python3 picorg_sorter.py consolidation-manifest \
  --root /mnt/elements16/@mixedpics_sorted \
  --dest-root /mnt/elements16/@mixedpics_sorted \
  --output /tmp/picorg-consolidation-manifest.json
```

The manifest lists source and proposed target paths, identity IDs, byte sizes,
and statuses for unresolved identities, existing targets, and collisions. It
hashes only colliding files and proposes deterministic hash-suffixed target
names; exact-content duplicates are called out separately. It never moves,
renames, or deletes media. Review every unresolved identity and duplicate before
any future migration is designed.

## Later RD/MD migration

Promotion into platform trees is a separate feature. It needs source-specific
adapters that map the shared ID to the platform's expected folder and database
rows, validate conflicts, record idempotency keys, and provide rollback. Until
then, shared-ID holding folders are PicOrg review destinations only; no
`metadaily` or `redditdaily` database writes are implied.
