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

## Remaining setuptools alerts

Five Dependabot alerts remain: one medium alert on `requirements-face.txt`,
plus two advisories each on `uv.lock` and `requirements-locked.txt`. `pip-audit`
reports three records for the two setuptools advisories.

`setuptools` stays at 80.10.2 because the supported face extra and
`requirements-face.txt` require `<81`; the README documents that
`face_recognition_models` imports the legacy `pkg_resources` API. Setuptools
removed that API in 82.0.0, so the patched versions 83.0.0 and 84.1.0 cannot
satisfy the current face backend. The remaining advisories are platform and
workflow specific: the medium alert concerns source distribution exclusions
on macOS APFS/HFS+ with Unicode-normalized filenames; the low alert concerns
legacy wheel installation on Windows. PicOrg's deployed service runs on Linux
and does not publish source distributions, but those alerts still matter to
developers using the affected build/install workflows.

To remove the alerts without regressing face matching, replace or update the
face model resource-loading dependency so the face extra no longer needs
`pkg_resources`, then verify the face model loader and face matching pipeline
before removing the setuptools cap.

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

Advisories: [CVE-2026-59890 / GHSA-h35f-9h28-mq5c](https://github.com/advisories/GHSA-h35f-9h28-mq5c),
[GHSA-xmj3-9gf7-h52w](https://github.com/advisories/GHSA-xmj3-9gf7-h52w).
The setuptools documentation records `pkg_resources` removal in 82.0.0:
[migration guidance](https://github.com/pypa/setuptools/blob/main/docs/deprecated/pkg_resources.rst).

The active service virtual environment was not modified. The lockfile updates
take effect in deployments after their normal dependency installation and
service restart.
