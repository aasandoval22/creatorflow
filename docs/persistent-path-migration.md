# Audited persistent-path migration

CreatorFlow's canonical managed-path representation is a POSIX-style path
relative to `/home/aasandoval/.local/share/creatorflow/data`. A stored value
such as `downloads/CaseOh/video.mp4` is materialized only by validating the
suffix and joining it to that configured root. Opening a file additionally
rejects traversal, symlink components, missing or special files, and unexpected
ownership.

New active writes use relative paths. During migration, readers also understand
an absolute path whose lexical prefix exactly matches an explicitly configured
historical data root. Compatibility is not based on `realpath()` alone. The
legacy and canonical names must identify the same device/inode, and the
canonical target must pass owner, regular-file, symlink, and checksum checks.
Strict `current/data/...` and `releases/<40-hex-commit>/data/...` paths receive
the same identity verification and can then be normalized without opening a
release-local target. Malformed production paths, unrecognized roots, and
overlapping roots with different suffixes are never rewritten automatically.

## Inventory and plan

The inventory classifies active manifests, transcripts, candidate artifacts,
review/timing records, previews, publication records, accepted-reference
indexes, discovery records, comparison data, and immutable cleanup/recovery/
audit history. URLs and immutable identifier archives are distinguished from
managed file paths. Old paths found in append-only history are reported as
historical text and are never rewritten.

From the isolated deployed release, generate a plan without changing active
state:

```bash
cd /home/aasandoval/clip-factory-production/current
.venv/bin/python -m backend.app.path_migration inventory
.venv/bin/python -m backend.app.path_migration plan
.venv/bin/python -m backend.app.path_migration show --plan-id PLAN_ID
.venv/bin/python -m backend.app.path_migration coverage
```

The immutable ignored plan pins its own checksum and every source metadata
document's SHA-256. Each proposed field records its schema and owner identity,
old and proposed values, device/inode, byte size, media checksum, operating-
system owner, safety results, and reason. Planning writes only the plan and a
sanitized local audit event. It does not modify a manifest, queue, decision,
profile, reference, publication record, or media file.

Downloaded files with no manifest mapping are evaluated against transcript,
candidate, and preview lineage plus a matching manifest video identity. A
single corroborated identity is proposed for the ignored orphan-ownership
registry. Conflicting or insufficient evidence remains `orphaned-unverified`
and blocks automatic cleanup. Filename inference alone is never sufficient.
The `adopt-orphan` command is available for an operator who has separately
verified a checksum and explicit JSON evidence paths.

## Apply and restore after merge

Do not reuse a plan after active records or media change. Review all proposed
and manual-review entries first. Then:

```bash
cd /home/aasandoval/clip-factory-production/current
.venv/bin/python -m backend.app.path_migration show --plan-id PLAN_ID
.venv/bin/python -m backend.app.path_migration apply \
  --plan-id PLAN_ID --confirm PLAN_ID
.venv/bin/python -m backend.app.path_migration coverage
```

Apply takes the production, review, publication, cleanup, reference-decision,
reference-audit, and migration locks. It rechecks source hashes, stored old
values, target identity, owner and checksum. Each metadata document has an
ignored recovery copy before atomic replacement. A changed or invalid document
is skipped independently, and reapplying a completed plan is idempotent. Media
is never copied, moved, quarantined, or deleted.

If inspection after apply finds a problem and the records have not since been
edited, restore from the reported recovery ID:

```bash
.venv/bin/python -m backend.app.path_migration restore \
  --recovery-id RECOVERY_ID
```

Restore verifies both the post-migration record and backup checksums before
replacement. It refuses to overwrite subsequent operator/application changes.

## Regenerate a cleanup plan (read-only)

Path normalization changes representation, not retention eligibility. Once a
migration is validated, generate a fresh read-only ownership and cleanup view:

```bash
.venv/bin/python -m backend.app.path_migration coverage
.venv/bin/python -m backend.app.media_cleanup ownership
.venv/bin/python -m backend.app.media_cleanup plan
.venv/bin/python -m backend.app.media_cleanup show --plan-id CLEANUP_PLAN_ID
```

Do not pass cleanup `--execute`. Normalization cannot by itself make media
eligible; review, publication, reference, recovery, retention, identity and
checksum rules remain unchanged. Cleanup scheduling remains a separate,
explicit deployment decision.
