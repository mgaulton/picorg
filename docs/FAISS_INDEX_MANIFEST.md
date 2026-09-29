# Optional FAISS index manifest

This is a design for a future exact/ bounded-candidate retrieval cache. It is
not enabled by the production matcher. The current SQLite database remains the
source of truth and the calibrated verifier must run after retrieval.

## Required manifest fields

```json
{
  "schema_version": 1,
  "index_type": "IndexFlatL2",
  "database_sha256": "<sha256 of the validated face database>",
  "gallery_sha256": "<ordered hash of identity, path, and embedding rows>",
  "model_id": "dlib-face-recognition-small-v1",
  "dimensions": 128,
  "metric": "L2",
  "normalized": false,
  "identity_count": 488,
  "vector_count": 29106,
  "retrieval_k": 2048,
  "built_at": "<UTC timestamp>"
}
```

`database_sha256`, `gallery_sha256`, model ID, dimension, metric, and
normalization are compatibility keys. The gallery hash covers the ordered
identity, reference path, quality, and embedding bytes. A mismatch must
invalidate the index; never silently search an index built from a different
model, quality map, or gallery.

## Safe build/swap

1. Read the validated SQLite database without mutation.
2. Build the FAISS index and identity row map in a temporary directory on the
   same filesystem.
3. Write the manifest, flush and close both files, then atomically rename the
   temporary directory to a versioned directory.
4. Atomically replace a small `current` pointer/manifest only after all hashes
   and row counts validate.
5. Keep the previous version until the next successful build and provide a
   read-only parity report before enabling it.

## Acceptance gates

- Image-disjoint reviewed pairs must preserve the complete current identity
  top-k candidate list at the selected `retrieval_k`.
- Any parity loss routes the item to the existing exact fallback; it must not
  become an automatic move.
- The existing distance, quality, margin, multi-face, and production-readiness
  gates remain authoritative after retrieval.
- Index build and replacement must be resumable and leave the prior index
  usable after interruption.
