# Photo-reorg face-database rebuild failure

## Question

Why did the rebuild report “completed successfully” while PicOrg received an
invalid database report?

## Sources

| Path | Finding |
|---|---|
| `/opt/photo_reorg/rebuild_face_database.py` | The helper used a relative fallback database path instead of the recognizer's configured path. |
| `/opt/photo_reorg/high_accuracy_face_recognition.py` | The actual schema uses `face_quality` and creates `face_encodings`/`person_metadata`. |
| `/opt/picorg/rebuild_face_data.sh` | PicOrg correctly validates the rebuilt DB and restores the previous backup on failure. |

## Root cause

The photo-reorg helper analyzed a different relative SQLite file, so it logged
`no such table: face_encodings`. Its report query also referenced the obsolete
`quality_score` column, and `json.dump` could not serialize rebuild timestamps.
The helper swallowed those reporting errors and still logged success. PicOrg's
JSON parse then failed, causing the wrapper's restore trap to recover the prior
database.

## Fix applied

The installed helper now:

- uses `face_recognizer.config['face_database_path']` for backup, clearing, and
  analysis;
- queries the schema's `face_quality` column; and
- serializes report timestamps with `default=str`; and
- fails closed when database analysis returns no data instead of logging a
  false successful completion.

Analyze-only validation now produces a valid report showing the restored DB's
four identities and 54 face encodings. PicOrg's validator also passes when run
with minimums of one.

The production rebuild still requires PicOrg's configured minimums (currently
100 faces and 10 identities), so the restored four-identity database is not
production-ready for automatic matching.

## Open gap

The photo-reorg source tree is outside the PicOrg repository. Preserve the
installed change when that project is next versioned, and keep PicOrg's
post-rebuild validation/restore gate enabled.
