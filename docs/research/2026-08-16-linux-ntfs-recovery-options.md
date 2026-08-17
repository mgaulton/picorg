# Linux NTFS recovery options

## Question

What Linux-native recovery path is safest for `/dev/sde1` after `ntfsfix` succeeds but one NTFS file still returns `EIO`?

## Sources

| URL/path | What it proved | Retrieved |
|---|---|---|
| https://www.tuxera.com/community/ntfs%20-3g-faq/ | ntfs-3g recommends a proprietary checker such as Windows `chkdsk` when a check or fix is required | 2026-08-16 |
| https://www.gnu.org/software/ddrescue/manual/ddrescue_manual.html | Do not rescue a mounted device; image a failing drive first and repair the copy, not the original | 2026-08-16 |
| https://www.cgsecurity.org/testdisk_doc/presentation.html | TestDisk can inspect/repair NTFS boot sectors and MFT; PhotoRec can carve files from damaged filesystems | 2026-08-16 |
| https://www.smartmontools.org/static/doxygen/os__generic_8cpp_source.html | smartctl supports SMART reports and short/long self-tests | 2026-08-16 |
| `/opt/picorg` live checks | `/dev/sde1` is NTFS via ntfs-3g; `ntfsfix` passed, but the target file still returns `EIO` and appears as `-?????????` | 2026-08-16 |
| `smartctl -x /dev/sde` | WD Ultrastar HC560 SMART overall status passed; zero reallocated/pending/uncorrectable sectors, but one historical ICRC/ABRT error and one interface CRC error are logged | 2026-08-16 |

## Findings

- `[S5]` The failure is file-record/read-path specific, not a simple dirty-volume flag: MFT/MFTMirr and the alternate boot sector passed `ntfsfix`, while one file remains unreadable.
- `[S1]` `ntfsfix` is not a full Linux replacement for `chkdsk`; repeated NTFS file-record problems should be checked with Windows `chkdsk` or an equivalent offline repair.
- `[S2]` If there may be physical read errors, do not repeatedly mount and repair the original. GNU ddrescue recommends imaging an unmounted source first and repairing the image/copy.
- `[S3]` TestDisk is appropriate for a second-line inspection of NTFS boot sectors/MFT. It should write only after an image or verified backup exists.
- `[S3]` PhotoRec is a recovery fallback when filesystem metadata cannot be repaired; it recovers content to another disk and generally loses original filenames/folder structure.
- `[S4]` SMART data and self-tests can distinguish a logical NTFS issue from a failing disk, but a passing SMART result does not prove every sector is readable.
- `[S5]` Current SMART evidence is more consistent with an interface/transport incident than a confirmed media failure: no pending, reallocated, or offline-uncorrectable sectors are reported, but one ICRC error is logged. This is an inference, not a guarantee.

## Recommendation

**High confidence:** replace/check the SATA or USB data path and power connection, then run a SMART short test. Regardless of the apparently good SMART attributes, keep `/dev/sde1` offline for writes, make a sector-level image to a different disk with GNU ddrescue, and perform repairs against the image. If a full image is not practical, copy the highest-value directories to another disk while logging failures. Use TestDisk/PhotoRec only against the image or a clone. Do not resume PicOrg intake until the affected source is readable and stable.

Suggested guarded sequence:

```bash
# Device identity and health (read-only queries)
smartctl -x /dev/sde
smartctl -t short /dev/sde
smartctl -l selftest /dev/sde

# After attaching a destination disk larger than /dev/sde1:
umount /dev/sde1 2>/dev/null || true
ddrescue --ask -f -n /dev/sde1 /recovery/sde1.img /recovery/sde1.map
ddrescue --ask -f -r3 /dev/sde1 /recovery/sde1.img /recovery/sde1.map
```

Never substitute a destination path without verifying the model/serial and free space. The ddrescue destination will be overwritten.

## Open gaps

- SMART attributes/self-test results for `/dev/sde` have not yet been collected.
- The size and availability of a separate recovery destination are unknown.
- It is not yet known whether the affected file's clusters are physically unreadable or only its NTFS metadata is damaged.
- Full Windows `chkdsk /f` remains the simplest filesystem repair path, but it requires attaching the drive to Windows and should follow an image/backup when data is important.
