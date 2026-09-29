# Identity and face-evidence integration

Status: documented design only. No shared face-evidence integration is enabled.

## Current policy

PicOrg continues to rely on the Metadaily identity registry for canonical
identity data:

- `/opt/shared/identity_aliases.json` is the authoritative external
  identity source.
- Redditdaily and other configured source lists contribute aliases and source
  evidence through PicOrg's existing catalog loader.
- The canonical baseline also reads person-named folders under
  `/mnt/assorted` when their names resolve unambiguously through the shared
  registry; these folders remain read-only and are recorded as source
  provenance rather than becoming new identities.
- `/opt/picorg/project_registry.json` remains a local overlay for confirmed
  manual identities, preferred aliases, ambiguous tokens, and generic-token
  blocklists. It does not replace the Metadaily registry.
- `/opt/picorg/identity_face_markers.json` is a PicOrg review ledger. It records
  human decisions and is not currently published to Metadaily or Redditdaily.
- Face databases remain local and private. The compatibility default is still
  `/opt/photo_reorg/data/high_accuracy_faces.db`; PicOrg now also provides an
  opt-in native backend at `.cache/picorg/face_database.sqlite3` using the same
  reader schema. Neither database is a shared registry key store.

Face matches still require explicit review confirmation. A high similarity
score alone does not create a registry identity or automatically publish face
evidence.

## Observed migration baseline

The current marker ledger contains 545 confirmed markers. Normalized alias
matching associates 516 of them with an existing MD/RD catalog identity. The
remaining 29 were previously assigned to the temporary local label
`new_creator`; they have been corrected to the local manual identity
`faye_reagan` and remain unregistered in MD/RD until a human adds or links that
identity there.

These counts are a diagnostic snapshot, not a permanent contract; rerun the
catalog and marker audit after source registries change.

## Deferred integration design

If cross-project face evidence is enabled later, use a separate shared store
rather than adding embeddings to `identity_aliases.json`.

The shared store should use the stable Metadaily identity `id` as its key and
retain the current `primary_folder` as the display/folder canonical name. Each
face reference should include:

- registry ID and canonical name;
- image SHA-256 and face index;
- embedding model ID, detector, vector dimension, and metric;
- quality score and review status;
- provenance, reviewer, and timestamp.

Model namespaces must remain separate: dlib 128-dimensional vectors must never
be compared directly with InsightFace/ArcFace vectors. Absolute source paths
should be optional metadata, not the identity key, so moved files remain
usable.

A future shared SQLite or equivalent sidecar could expose two logical records:

```text
identities(registry_id, canonical, aliases, registry_revision)
face_references(registry_id, image_sha256, face_index, model_id,
                embedding, quality, status, provenance, created_at)
```

PicOrg, Metadaily, and Redditdaily would consume this store read-only during
matching. Only confirmed human review would add or revoke references. Alias
changes would update the registry view without relabeling the evidence.

## Requirements before implementation

Before enabling the deferred design, complete all of the following:

1. Define the registry-ID mapping and an ambiguity workflow; never use fuzzy
   matching to silently link two people.
2. Populate reliable content hashes for reviewed files and retain model and
   detector metadata with every vector.
3. Add import/export validation, duplicate suppression, and rollback snapshots.
4. Keep the store local, access-controlled, and out of the LAN review API by
   default because face embeddings are sensitive biometric data.
5. Require held-out accuracy evidence and a complete validated gallery before
   considering any automatic face-based moves.

Until those requirements are met, keep face markers and face databases local to
PicOrg and use the MD registry only for canonical identity and alias lookup.
