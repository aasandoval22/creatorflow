# Media lifecycle and TikTok inbox drafts

This milestone adds two independent safety layers: an ownership-aware local
media lifecycle and a consent-driven publication ledger. It does not enable
cleanup, configure TikTok credentials, upload a clip, publish a post, expose a
public media server, or change candidate selection and rendering.

## Ownership graph

`backend.services.media_lifecycle.MediaOwnershipGraph` joins the source
manifest, review queue, publication ledger, reference index, comparison
batches, and recovery roots by stable video, candidate, review, timing-revision,
and publication-attempt identities.

| Data | Role | Ordinary cleanup |
| --- | --- | --- |
| Source manifest, review decisions/notes, timing revisions | Authoritative local state | Retained |
| Publication records, checksums, rights confirmations, audit events | Authoritative audit state | Retained |
| Reference index, baseline and human annotations | Authoritative reference state | Retained |
| Downloaded source media | Derived, but operationally required until all dependencies are terminal | Eligible only under the source policy |
| Transcript, word timings, subtitle and candidate artifacts | Derived metadata used for reproducibility | Retained by current policy |
| Rendered preview/final render | Derived from source plus immutable candidate/timing metadata | Eligible only under the rendered-media policy |
| Comparison batches and reports | Evaluation evidence | Retained; active batches also block related cleanup |
| Reference media | Protected quality evidence | Never eligible |
| Withdrawal/evidence-recovery directories | Recovery evidence | Never eligible |
| Cleanup plans, quarantine manifests and audit ledger | Recovery and deletion evidence | Retained and ignored by Git |

Run the graph locally with:

```bash
.venv/bin/python -m backend.app.media_cleanup ownership
```

## Retention rules

The tracked policy at `backend/config/media_retention.json` defaults to:

```json
{
  "source_media_retention_days": 7,
  "rejected_preview_retention_days": 14,
  "published_media_retention_days": 30,
  "quarantine_retention_days": 7,
  "retain_metadata": true,
  "retain_transcripts": true,
  "retain_audits": true
}
```

Source media is retained until every dependent review is terminal, every
approved current render has a checksum-matching verified `publish_complete`
attempt, no comparison/publication/retry work is active, the source is not
reference or recovery evidence, and the source retention period has elapsed.

Rejected previews become eligible after 14 days. An approved current render is
never eligible until 30 days after verified publication completion. Pending,
approved-unpublished, stale, queued, transferring, processing, failed, and
unknown publication states block cleanup. Metadata, transcripts, comparison
reports, references, recovery evidence, checksums, publication records and
audits are not cleanup targets.

## Plan, quarantine, restore and purge

Planning writes an immutable checksum-pinned plan but does not move media:

```bash
.venv/bin/python -m backend.app.media_cleanup plan
.venv/bin/python -m backend.app.media_cleanup show --plan-id PLAN_ID
```

The first apply command is deliberately a dry run:

```bash
.venv/bin/python -m backend.app.media_cleanup apply \
  --plan-id PLAN_ID --confirm PLAN_ID
```

After manually reviewing every listed path, reason, size and retention time,
the same plan can be quarantined explicitly:

```bash
.venv/bin/python -m backend.app.media_cleanup apply \
  --plan-id PLAN_ID --confirm PLAN_ID --execute
```

Application holds the production, review, publication and cleanup locks,
revalidates each item and checksum, refuses traversal, symlinks, special files
and unexpected owners, then atomically preserves the relative path under the
ignored quarantine root. A changed or invalid item is skipped independently.

Restore remains possible while quarantine media exists:

```bash
.venv/bin/python -m backend.app.media_cleanup restore \
  --quarantine-id QUARANTINE_ID
```

Restore verifies the checksum and refuses an occupied or unsafe destination.
Permanent deletion requires a different command, matching confirmation and the
seven-day grace period:

```bash
.venv/bin/python -m backend.app.media_cleanup purge \
  --quarantine-id QUARANTINE_ID --confirm QUARANTINE_ID
```

Purge is never performed by `run-eligible` or the future timer. Audit events
contain identities, results and byte totals, never file contents or secrets.

Optional future cleanup units can be installed only through an explicit
post-merge operation:

```bash
.venv/bin/python -m backend.services.autoclip_service install-cleanup
```

That command still leaves the timer disabled and stopped. Normal `install`,
`deploy` and `start` do not install or enable cleanup. The optional daily timer
is persistent, has a randomized delay, generates a fresh plan, and quarantines
only already-eligible media; it never purges automatically.

## Publication model

The platform-neutral ledger preserves the full lifecycle:

`not_approved → approved → publish_ready → awaiting_consent → queued →
initializing → transferring → inbox_delivered → awaiting_creator_post →
processing → publish_complete`, with explicit `failed_retryable`,
`failed_terminal`, and `cancelled` outcomes.

An attempt records review/source/candidate identity, timing revision, rendered
SHA-256, platform, destination account, intended editable caption, source
attribution, rights-confirmation time, transport, idempotency key, remote IDs,
status timestamps and sanitized history. The implemented preparation starts at
`awaiting_consent`; earlier states describe review readiness and do not create
an attempt. Rerendering, timing changes, or moving a review away from approved
marks a prepared attempt stale. Current revision, path and checksum are checked
again immediately before transfer.

The idempotency key binds review ID, revision, checksum, platform and account.
An ambiguous transfer with a remote `publish_id` must be reconciled before any
retry. An ambiguous initialization without a usable remote ID is not retried
automatically. Retries and polling are bounded, terminal states are not polled,
and unresolved or failed states block media cleanup.

## TikTok developer setup after merge

Do not place secrets in Git. In the TikTok developer portal:

1. Create or select the CreatorFlow application and use the current official
   Login Kit and Content Posting API products.
2. Request only `user.info.basic` and `video.upload` for this inbox workflow.
3. Register the exact static callback. A desktop/local setup may use an HTTP
   loopback URI with an explicit port; a web deployment must use HTTPS.
4. Complete TikTok's required application review and comply with its content,
   account, rights, rate-limit and user-consent requirements. Do not bypass an
   unavailable scope or restricted account.
5. Put configuration only in the server's protected local environment file.

Environment variable names (values intentionally omitted):

```text
AUTOCLIP_TIKTOK_ENABLED
TIKTOK_CLIENT_KEY
TIKTOK_CLIENT_SECRET
TIKTOK_REDIRECT_URI
AUTOCLIP_TIKTOK_DAILY_LIMIT
AUTOCLIP_TIKTOK_MAX_PENDING
AUTOCLIP_TIKTOK_TRANSPORT
AUTOCLIP_TIKTOK_VERIFIED_MEDIA_BASE_URL
AUTOCLIP_TIKTOK_MEDIA_SIGNING_KEY
AUTOCLIP_TIKTOK_TOKEN_PATH
AUTOCLIP_TIKTOK_OAUTH_STATE_PATH
AUTOCLIP_TIKTOK_MAX_RETRIES
AUTOCLIP_TIKTOK_RECONCILE_SECONDS
```

Defaults are disabled, one export per UTC day, one unresolved inbox share, and
`FILE_UPLOAD`. When a verified HTTPS media property plus a strong signing key
is configured, `PULL_FROM_URL` is preferred. Signed URLs are opaque,
short-lived, checksum-pinned, non-enumerable and range-capable. This milestone
provides the transport boundary but deliberately exposes no public handler;
do not select PULL_FROM_URL until a reviewed authenticated HTTPS delivery
milestone connects that boundary. The loopback review server must never be
made public.

Tokens are stored server-side with atomic replacement, file mode `0600`, and a
`0700` parent directory. Refresh-token rotation replaces both tokens together.
No suitable project encryption mechanism exists, so no weak custom encryption
was invented; protect the operating-system account and storage. OAuth state is
one-time and expiring, desktop authorization uses PKCE, callback errors are
generic, and the review access log omits callback query values.

## Controlled first inbox upload after merge

1. Deploy the reviewed merge and confirm services are healthy and the review
   server is still `127.0.0.1` only.
2. Configure the variables above with `AUTOCLIP_TIKTOK_ENABLED=false`; verify
   the review page and existing production behavior first.
3. Complete TikTok application review, set the exact callback, then change only
   `AUTOCLIP_TIKTOK_ENABLED=true` and restart the review service.
4. Through the SSH-tunneled loopback review page, select **Connect TikTok** and
   verify the displayed account identifier/name.
5. Choose exactly one approved clip. Review its creator, duration, revision and
   SHA-256; edit the intended caption/hashtags and attribution; affirm that you
   are authorized to republish it; then select **Prepare TikTok draft**. This
   step uploads nothing.
6. On the separate confirmation view, recheck the named TikTok account, clip,
   caption, timing revision and checksum. Only then choose **Send to TikTok
   inbox**.
7. Refresh status manually. Open TikTok and complete or abandon the final draft
   there. Inbox delivery is not public publication and the intended caption is
   audit context; finalize platform-visible text in TikTok.
8. Continue status refresh until TikTok returns `PUBLISH_COMPLETE` or a clear
   failure. Do not delete media while status is unresolved. The CLI background
   path can reconcile at most a bounded number at a time:

   ```bash
   .venv/bin/python -m backend.app.publications reconcile --limit 1
   ```

No Direct Post endpoint, unattended posting, password automation, browser
scraping, platform publishing credential, or public webhook endpoint exists.
The webhook verifier is an authenticated replay-resistant boundary for a
future separately reviewed deployment only.
