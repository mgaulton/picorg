# Dependency alert remediation (2026-10-27)

The GitHub Dependabot API reported 94 open alerts across `uv.lock` and its
hash-pinned `requirements-locked.txt` export. The duplicate entries reflect
the same dependency advisories being attached to both manifests.

## Updated

- Pillow: 12.3.0 → 13.0.0
- Werkzeug: 3.1.8 → 3.1.9
- urllib3: 2.7.0 → 2.8.0
- certifi: 2026.7.22 → 2026.10.23 (found by `pip-audit` during verification)

After these updates, `pip-audit` reported no findings for those four packages.
The lock export retains its distribution hashes and passes
`verify_dependency_lock.sh`.

## Remaining setuptools blocker

Four Dependabot alerts remain (two setuptools advisories in each lock
manifest). `setuptools` stays at 80.10.2 because the supported face extra and
`requirements-face.txt` require `<81`; the README documents that
`face_recognition_models` imports the legacy `pkg_resources` API. The patched
setuptools releases reported by the advisories are 83.0.0 and 84.1.0, which
cannot satisfy that compatibility constraint. `pip-audit` currently reports
three records for those two advisories.

To unblock the upgrade, replace or update the face model resource-loading
dependency so the face extra no longer needs `pkg_resources`, then verify the
face model loader and face matching pipeline before removing the setuptools
cap. Until then, retain the working face pipeline and track these alerts as an
explicit compatibility exception.

## Verification

- `uv lock --upgrade-package pillow --upgrade-package werkzeug --upgrade-package urllib3`
- `uv lock --upgrade-package certifi`
- `uv export --frozen --all-extras --format requirements.txt --no-header --no-annotate --output-file requirements-locked.txt`
- `bash ./verify_dependency_lock.sh` — passed.
- `pip-audit -r requirements-locked.txt --progress-spinner off` — only the
  documented setuptools findings remain.
- Pillow 13.0.0 focused image tests — 19 passed.
- Review UI tests with Werkzeug 3.1.9 — 50 passed, 3 failed on existing
  review UI and identity-group assertions. The same three tests fail at the
  unchanged baseline commit `70fb8ab` with the same Werkzeug version, so these
  failures predate the dependency patch and were left unchanged.

The active service virtual environment was not modified. The lockfile updates
take effect in deployments after their normal dependency installation and
service restart.
