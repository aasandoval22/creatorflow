# CreatorFlow

CreatorFlow is an AI-powered content clipping platform that automatically:

- Downloads creator videos
- Detects the best moments using AI
- Creates vertical videos
- Generates captions
- Publishes to TikTok, YouTube Shorts, and Instagram Reels

## Status

🚧 Currently under development.
# creatorflow


## Testing

The offline test suite runs automatically for pull requests targeting `main`
and for pushes to `main`. To run the same suite locally using the existing
Python virtual environment:

```bash
python -m pytest -p no:cacheprovider
```

## Channel ingestion

Check each enabled channel and download up to its three newest videos:

```bash
python -m backend.app.check_channels
```

Discover metadata and update the ingestion manifest without downloading video
or audio:

```bash
python -m backend.app.check_channels --dry-run
```

Change the number of recent videos inspected per channel:

```bash
python -m backend.app.check_channels --max-videos 5
```

Store ingestion records in a custom location:

```bash
python -m backend.app.check_channels --manifest-path /path/to/videos.json
```

The default manifest is `data/manifests/videos.json`. It tracks each video by
video ID. A `discovered` record has metadata but no confirmed local media;
`downloaded` records include the completed download time and local path;
`skipped` means yt-dlp did not download the video, such as when its archive
already contains it; and `failed` includes an error message for a failed
ingestion attempt.

## Manual production cycle

Run one dependable ingestion-to-review cycle for every enabled creator:

```bash
.venv/bin/python -m backend.services.production_runner
```

Preview discovery and the planned work without downloading, transcribing,
rendering, changing the manifest/review queue/production state, or writing a
production log:

```bash
.venv/bin/python -m backend.services.production_runner --dry-run
```

The command reuses the existing channel configuration, YouTube downloader,
manifest, faster-whisper transcription, deterministic candidate analysis,
reaction-context preview renderer, and review queue. The current defaults
inspect three recent videos per enabled creator and render the top three
candidates. Use `--max-videos` and `--top` to set explicit per-run limits.
An enabled channel may set a stricter positive `max_videos_per_cycle` in
`backend/config/channels.json`. The runner inspects only that many newest
uploads for the channel, so a value of `1` skips older backlog when the newest
upload is already processed. The run-wide `--max-videos` value remains an
upper bound, and durable video-ID deduplication still applies.

Durable orchestration state is stored at
`data/production/processing_state.json`. Version 1 contains an `updated_at`
timestamp and a `videos` object keyed by stable source video ID. Each video
records its creator, status, current stage, attempt count, first-seen/update/
completion timestamps, last error, and review-preview count. Writes are atomic.
Completed IDs and existing review-queue items are deduplicated on later runs.
Failed work is retried, and a `processing` entry left by interruption is marked
interrupted before recovery resumes.

`data/production/production.lock` uses a nonblocking process lock, so a second
production cycle exits instead of overlapping the active run. JSON-line events
are printed during every run and real runs are also appended to
`data/logs/production.jsonl`. Creator and video failures are isolated and
included in the final structured summary. Generated state, lock files, logs,
downloads, transcripts, candidates, previews, and review data are ignored by
Git.

This command is manually invoked. It does not install a scheduler, start the
review server, upload media, or publish to YouTube, TikTok, or any other
platform.

## Unattended production services

CreatorFlow includes repository-managed systemd user units for the loopback
review server and scheduled production runner. Live services run from an
isolated, versioned production release, never from the development checkout.
Deploy only from a clean local `main` that exactly matches `origin/main`:

```bash
.venv/bin/python -m backend.services.autoclip_service deploy
```

The deployment command fetches `origin`, refuses dirty or non-`main`
development checkouts, and requires local `HEAD` to equal `origin/main`. It
prepares a detached release at
`~/clip-factory-production/releases/<full-commit>/`, creates that release's
own `.venv`, installs the production and transcription requirements, runs the
complete offline test suite from the release, checks the existing Deno
runtime, and atomically updates `~/clip-factory-production/current`.

Generated systemd units in `~/.config/systemd/user/` use only:

```text
WorkingDirectory=/home/aasandoval/clip-factory-production/current
ExecStart=/home/aasandoval/clip-factory-production/current/.venv/bin/python ...
EnvironmentFile=-/home/aasandoval/.config/creatorflow/creatorflow.env
```

No unit path points into `/home/aasandoval/clip-factory`. The environment file
must already exist with mode `0600`; deployment preserves its exact contents.
The review server remains explicitly bound to `127.0.0.1`.

On the first deployment, the existing ignored `data/` tree is atomically moved
to `~/.local/share/creatorflow/data`, and both development and every release
use a symlink to that persistent location. Downloads, transcripts, candidates,
previews, decisions, processing state, references, profiles, comparisons, and
logs therefore survive release replacement and rollback. No generated data,
environment file, release, or virtual environment belongs in Git.

Deployment records the active and previous commits in
`~/clip-factory-production/deployment.json`. Repeating deployment of the same
commit reuses and revalidates the exact release. A release or dependency
validation failure leaves the previous `current` target unchanged. Services
that were stopped remain stopped; only services active before activation are
restarted and health-checked.

The default production interval is 30 minutes. Set another positive systemd
duration during deployment without editing Python source:

```bash
.venv/bin/python -m backend.services.autoclip_service deploy --interval 2h
```

Optional production-runner and review-server arguments belong in the local
environment file outside the repository:

```text
AUTOCLIP_PRODUCTION_ARGS=--max-videos 2 --top 2
AUTOCLIP_REVIEW_ARGS=--port 8080
```

YouTube discovery and downloads explicitly configure yt-dlp's Deno JavaScript
runtime. CreatorFlow's production downloader and reference-discovery media
validator share the same configuration: they check `AUTOCLIP_DENO_PATH`, then
`~/.deno/bin/deno`, then `PATH`. This gives interactive and systemd-managed
runs identical behavior even when the user service PATH omits the Deno install
directory. Production installs `yt-dlp[default]`, which lets yt-dlp select the
compatible `yt-dlp-ejs` package containing its local challenge-solver scripts.
Remote EJS component downloads are not enabled. If no executable runtime is
available, yt-dlp continues with a clear warning because Deno is optional in
offline development environments.

Keep the review server loopback-bound. The managed unit always supplies
`--host 127.0.0.1`; use an SSH tunnel for remote browser access as described
below. Do not put secrets in the environment file unless the local host
requires them for some separately managed dependency.

Manage the services with one entry point:

```bash
.venv/bin/python -m backend.services.autoclip_service deploy
.venv/bin/python -m backend.services.autoclip_service rollback
.venv/bin/python -m backend.services.autoclip_service start
.venv/bin/python -m backend.services.autoclip_service stop
.venv/bin/python -m backend.services.autoclip_service restart
.venv/bin/python -m backend.services.autoclip_service status
.venv/bin/python -m backend.services.autoclip_service logs
.venv/bin/python -m backend.services.autoclip_service logs --lines 250
.venv/bin/python -m backend.services.autoclip_service run-now
.venv/bin/python -m backend.services.autoclip_service disable
```

`rollback` validates and atomically selects the previously recorded release,
then restarts only services that were active. It swaps the current and previous
commit records, so a second rollback returns to the release that was active
before it.

`start` enables and starts the review service and production timer. The review
service uses `Restart=on-failure` with a five-second delay. The timer waits five
minutes after activation before its first run, then uses the configured
interval. `stop` gracefully stops the timer, an active production unit, and the
review server. `disable` disables only automatic production and leaves the
review server unchanged.
`run-now` invokes the same oneshot production service immediately. Scheduled
and manual service runs retain `production_runner`'s nonblocking file lock, so
they cannot overlap.

`status` reports the deployed commit, current `origin/main` commit, whether
production is behind main, review service and timer states, the next timer
event, the latest systemd production result, the most recent successful and
failed timestamps found in `data/logs/production.jsonl`, the processing-state
update time, the number of pending review items, and user-lingering status.
`logs` reads recent production and review-service diagnostics from the user
journal. Production JSON logs remain in `data/logs/production.jsonl`; both
locations are local and ignored by Git.

For enabled user units to start at boot and continue after logout, the account
must have systemd user lingering enabled. The installer only detects and
reports this state; it never uses sudo or changes it. When it reports
`Linger=no`, an administrator must run this isolated one-time command:

```bash
sudo loginctl enable-linger aasandoval
```

Without lingering, the enabled units start when the user logs in rather than
unattended at boot. No service in this layer publishes clips, changes selection
or rendering behavior, exposes the review server publicly, or modifies SSH,
UFW, WireGuard, router, or Cloudflare configuration.

Pushing a branch never deploys it. After a deployment-isolation change is
merged and local `main` is fast-forwarded, migrate the stopped live services
with:

```bash
git switch main
git pull --ff-only origin main
.venv/bin/python -m pytest -p no:cacheprovider
.venv/bin/python -m backend.services.autoclip_service deploy
.venv/bin/python -m backend.services.autoclip_service status
.venv/bin/python -m backend.services.autoclip_service start
```

Inspect the generated units and status before the final `start`. Deployment
itself does not enable stopped units.

## Local transcription

Install the optional transcription runtime separately from development
dependencies:

```bash
python -m pip install -r backend/requirements-transcription.txt
```

Transcribe eligible downloaded videos with the production defaults:

```bash
python -m backend.app.transcribe_videos
```

The defaults are the English `base.en` model on `cpu` with `int8` compute,
English language selection, word timestamps, VAD filtering, and beam size 5.
The first real use may download the selected model if it is not already
available locally.

Selection and retry examples:

```bash
python -m backend.app.transcribe_videos --video-id VIDEO_ID
python -m backend.app.transcribe_videos --limit 3
python -m backend.app.transcribe_videos --retry-failed
python -m backend.app.transcribe_videos --force
```

Each video produces `transcript.json`, `transcript.txt`, and `subtitles.srt`
under `data/transcripts/<video_id>/`. The JSON artifact contains versioned
segment and word timestamps, the text file contains a clean readable
transcript, and the SRT file contains numbered subtitle blocks.

The regular offline test suite injects fake models and uses temporary local
files. It does not install or load faster-whisper, download model files,
decode real media, or perform real transcription.

## Transcript clip-candidate analysis

CreatorFlow's current local workflow is ingestion → transcription → candidate
analysis → batch preview rendering → interactive local review → timing
correction when needed. Analyze all eligible downloaded videos with
completed transcripts:

```bash
python -m backend.app.analyze_clips
```

Selection and configuration examples:

```bash
python -m backend.app.analyze_clips --video-id VIDEO_ID
python -m backend.app.analyze_clips --video-id VIDEO_ID --force
python -m backend.app.analyze_clips --minimum-duration 25 --target-duration 40 --maximum-duration 55
python -m backend.app.analyze_clips --video-id VIDEO_ID --show-score-breakdown
python -m backend.app.analyze_clips --padding-before 0.15 --padding-after 0.25 --minimum-boundary-confidence 0.6
```

Ranked artifacts are written to
`data/clip_candidates/<video_id>/candidates.json`. Ranking uses transparent,
deterministic text and timing heuristics: identical transcript input and
configuration produce identical candidate boundaries, IDs, and scores.
When complete word timestamps are available, candidates can start and end
inside transcription segments at detected sentence, pause, and grammatical
thought boundaries. Invalid or incomplete word timing falls back safely to
segment boundaries. Candidate artifacts include deterministic start/end
confidence and the major boundary methods used. Small configurable media
padding is applied after selecting the spoken words and clamped to media bounds.
Heuristic scores prioritize potentially self-contained moments; they do not
predict actual popularity or virality. This stage does not cut, render,
upload, or publish video. Add `--show-score-breakdown` to print each returned
candidate's ending classification, component scores, positive reasons, and
penalties. Displayed components sum to the displayed total within a 0.1
rounding tolerance. Normal output is unchanged when the flag is omitted.

## Local vertical preview rendering

Preview rendering requires `ffmpeg` and `ffprobe` on `PATH` (or explicit paths
passed with `--ffmpeg-path` and `--ffprobe-path`). CreatorFlow does not install
these system tools. Render the top-ranked candidate:

```bash
python -m backend.app.render_preview --video-id VIDEO_ID
python -m backend.app.render_preview --video-id VIDEO_ID --rank 2
python -m backend.app.render_preview --video-id VIDEO_ID --candidate-id CANDIDATE_ID
```

Inspect and validate the command without rendering, render without captions,
replace an existing valid preview, or choose a custom even-sized canvas:

```bash
python -m backend.app.render_preview --video-id VIDEO_ID --dry-run
python -m backend.app.render_preview --video-id VIDEO_ID --no-captions
python -m backend.app.render_preview --video-id VIDEO_ID --force
python -m backend.app.render_preview --video-id VIDEO_ID --width 720 --height 1280
```

The default output is
`data/previews/<video_id>/<candidate_id>/preview.mp4`, with verified,
versioned metadata in the adjacent `preview.json`. The 1080×1920 composition
uses a center-cropped, darkened, blurred copy of the source as its background
and centers an uncropped, aspect-ratio-preserving source image in front.
Captions are generated from local transcript timing and burned into the video.
The original audio is retained when present.

Rendering, probing, caption generation, and artifact storage remain entirely
local. This command does not upload, publish, post, or call a platform API.

## Batch previews and local review

Render the top three ranked candidates, a larger top set, or explicit ranks:

```bash
python -m backend.app.render_previews --video-id VIDEO_ID
python -m backend.app.render_previews --video-id VIDEO_ID --top 4
python -m backend.app.render_previews --video-id VIDEO_ID --ranks 1,3
python -m backend.app.render_previews --video-id VIDEO_ID --context-profile compact
python -m backend.app.render_previews --video-id VIDEO_ID --context-profile none
python -m backend.app.render_previews --video-id VIDEO_ID --force --reapply-context
```

Batch rendering treats each analyzed candidate as an immutable anchor, not the
complete final clip. The default `reaction` profile starts with 15 seconds of
lead-in and 12 seconds of tail, normally produces 50–90 second previews, and
aims for 60 seconds. It is intended for gameplay reactions, rank guesses, clip
reviews, funny stream moments, and other setup/payoff sequences. The `compact`
profile starts with 6 seconds on each side, normally produces 35–60 second
previews, and aims for 45 seconds. Explicit `--lead-in`, `--tail`,
`--minimum-final-duration`, `--target-final-duration`, and
`--maximum-final-duration` values override profile defaults. Use
`--context-profile none` for candidate-only batch timing.

Boundaries are approximated deterministically from source timing, transcript
sentences, pauses, questions, answers, result language, and reactions. This
does not visually understand the gameplay or video. Human review remains
required. Automatic context never changes candidate timestamps, text, score,
rank, or ID; preview metadata stores candidate and render ranges separately.
Manual timing continues to win during ordinary batch rerenders. An intentional
`--reapply-context` recalculates automatic timing and, after a verified
replacement, returns a changed review to pending while preserving its note.

Successful previews and valid existing previews are recorded in the versioned
local queue at `data/review_queue/reviews.json`. The queue preserves human
decisions when preview metadata or paths are refreshed. List pending clips and
record decisions:

```bash
python -m backend.app.review_clips list --status pending
python -m backend.app.review_clips approve REVIEW_ID
python -m backend.app.review_clips reject REVIEW_ID --note "Needs a stronger opening"
python -m backend.app.review_clips pending REVIEW_ID
```

Build the read-only local HTML index:

```bash
python -m backend.app.review_clips build-index
python -m http.server 8080 --directory data
```

This static index can be viewed through `python -m http.server`, but its cards
are read-only. For decisions, notes, and timing controls, intentionally start
the interactive standard-library server:

```bash
python -m backend.app.review_server
```

Open `http://127.0.0.1:8080/`. Each card can approve or reject a clip with a
note, return it to pending, rerender relative or absolute timing, or reset the
preview to its immutable candidate timing. It also displays the context
profile, timing source, lead-in, tail, and expansion reasons, and offers a
token-protected **Reapply Automatic Context** action. Rerendering is synchronous and may
take some time; a successful replacement returns the clip to pending review.
These controls reuse the same queue and timing-adjustment services as the CLI.
The CLI and static index remain available.

The interactive server binds to `127.0.0.1` by default and never starts
automatically. Keep it loopback-bound. To review on a remote machine, create an
SSH tunnel and then use the same browser URL locally:

```bash
ssh -L 8080:127.0.0.1:8080 user@server
```

Do not bind this administrative page publicly. `--allow-non-loopback` exists
only for an explicitly secured environment and prints a strong warning. The
random per-process form token protects local write requests, but it is not
authentication and is not a substitute for loopback isolation or an SSH
tunnel. The page loads no external resources and the server performs no
external network requests.

Approval is local review state only. Neither the interactive page, the CLI,
nor the static index uploads, schedules, or publishes a clip anywhere.

## Accepted clip references

Accepted references describe clips that a user considers publishable. They are
deterministic quality and style references—not machine-learning training
samples, and not proof that future clips should copy one duration or structure.
They help inspect whether a preview enters at the earliest comprehensible
moment, preserves the complete story beat and personality reaction, reaches its
payoff, and stops without unnecessary tail.

Register the existing local reference, analyze it, and build its initial
profile:

```bash
python -m backend.app.reference_clips register \
  --reference-directory data/reference_clips/6j_BQCxHn74 \
  --profile personality_reaction
python -m backend.app.reference_clips analyze youtube-6j_BQCxHn74
python -m backend.app.reference_clips build-profile personality_reaction
```

Analysis uses local FFprobe and FFmpeg plus the existing optional
faster-whisper runtime. To replace an existing analysis with word-timed speech
evidence, run:

```bash
.venv/bin/python -m backend.app.reference_clips analyze \
  --reference-id REFERENCE_ID --with-transcription --force
```

The replacement is atomic and advances an analysis revision. A transcription
failure preserves the previous valid analysis. Use `--no-transcription` when
only media, scene-change, and silence measurements are wanted. Scene changes
are pixel-change signals, and transcript hook, reaction, question, payoff, and
ending findings are explicitly heuristic; none proves humor, quality,
originality, or virality.

Accepted-reference inspection and annotation are available at `/references`
on the loopback review server. Each reference page shows its playable media,
checksum, automatic measurements, sanitized analysis, profile contribution,
and local audit history. Structured human fields cover composition, facecam
presence, opening, purpose, pacing, payoff, captions, desired or undesirable
qualities, and reviewer notes. Unset fields remain `unknown`; no annotations
are fabricated. Saves, reanalysis requests, and explicit profile rebuilds are
token-protected POST actions with annotation-revision and replay protection.
The annotation page cannot delete or withdraw a reference.

Human annotations are separate versioned files under
`data/reference_annotations/`, and automatic reanalysis never rewrites them or
the baseline's existing qualities and notes. The optional private
`AUTOCLIP_REVIEWER_NAME` value is recorded only as a display label. Evidence
changes and sanitized failures are appended to
`data/reference_annotations/events.jsonl`; tokens, keys, cookies,
authorization headers, and private environment values are excluded.

The same operations are available from the CLI:

```bash
.venv/bin/python -m backend.app.reference_clips show-annotations REFERENCE_ID
.venv/bin/python -m backend.app.reference_clips annotate REFERENCE_ID \
  --expected-revision 0 --composition full_screen_gameplay \
  --opening-style immediate_action --pacing fast
.venv/bin/python -m backend.app.reference_clips evidence-history REFERENCE_ID
.venv/bin/python -m backend.app.reference_clips build-profile gaming_highlight
```

Profile version 3 records its own UTC build timestamp, category, exact input
references, and every analysis and annotation revision used. It keeps observed
automatic metrics separate from human preferences. Transcript-backed output
includes spoken and media pacing, density, speech start, heuristic hook/payoff
timing, post-payoff and post-speech tails, unresolved endings, questions, and
reactions. Every metric reports contributors, unavailable inputs, median,
range, and evidence type; missing evidence is never averaged as zero. Human
fields require at least two annotated references before aggregation. A later
reanalysis or annotation marks affected profiles stale but never rebuilds
them; rebuilding remains an explicit command. Three-way human preference
splits remain `mixed` instead of manufacturing a majority. Versions 1 and 2
remain readable. Profiles remain descriptive soft evidence and are never
applied to production selection, rendering, timing, captions, or publishing
automatically.

A protected pre-change evidence snapshot can be restored through one audited,
transactional command rather than manual file copies:

```bash
.venv/bin/python -m backend.app.reference_clips restore-evidence \
  --reference-id REFERENCE_ID --profile PROFILE_NAME \
  --snapshot /protected/snapshot/path \
  --reason "Why this exact evidence state is being restored"
```

Recovery validates protected ownership and permissions, strict-index identity,
media and profile checksums, category, and current state. It first retains the
displaced annotation and profile under ignored
`data/reference_evidence_recovery/`, atomically restores only that pair, and
appends a distinct sanitized recovery event. An absent snapshot annotation
moves the active annotation into recovery storage. Verification or persistence
failure rolls active evidence back, and an already-restored retry is a no-op.
Media, index, source metadata, analysis, and other profiles are not changed.

Compare rejected previews for a video:

```bash
python -m backend.app.reference_clips compare \
  --profile personality_reaction --video-id hhQzg7Him1g --status rejected
```

The interactive review page also offers **Compare to Reference Profile**. It
writes a local report and leaves decisions, notes, timing, and previews
unchanged. The report separates known measurements, heuristic findings, and
unavailable evidence.

For comparisons against a changing review queue, capture a stable batch first:

```bash
.venv/bin/python -m backend.app.review_comparisons capture \
  --profile gaming_highlight
.venv/bin/python -m backend.app.review_comparisons run --batch-id BATCH_ID
.venv/bin/python -m backend.app.review_comparisons show --batch-id BATCH_ID
```

The ignored `data/review_comparison_batches/` manifest pins the exact pending
review records, their statuses/timing revisions, and the profile bytes, schema
version, build timestamp, and SHA-256 at capture. A run uses only those pinned
inputs, notes each item's current status and whether it changed, and never
changes decisions, notes, timing, previews, layout, captions, or publishing
state. Completed runs are idempotent; a rebuilt profile requires a new batch.
The review page displays the newest completed batch report with its capture
time, pinned profile hash/version, and post-capture change indicator.

One accepted reference produces a `provisional` profile because one example
does not establish statistical confidence. Add later accepted references in
their own directories with `reference.mp4` and `baseline.json`, register and
analyze them, then rebuild the profile to use medians and observed ranges.
Profiles remain soft priors and never change candidate selection or rendering
defaults automatically.

Reference media, annotations, source metadata, analyses, profiles, recovery
backups, analyzer temporary files, comparison batches, and reports stay under
ignored `data/` paths.
These commands perform no downloading, uploading, publishing, or platform
access. The current workflow is ingestion → transcription → candidate analysis
→ preview rendering → local review → accepted-reference comparison → human
timing/decision changes when warranted.

## Gaming reference discovery

CreatorFlow can search the official YouTube Data API for public gaming Shorts,
hydrate changing engagement statistics, verify shortlisted media locally, and
place a diverse top set into a separate human reference-review queue. Discovery
never accepts a reference, changes a profile, or affects production selection.

Configure an API key only in the existing private environment file:

```text
YOUTUBE_DATA_API_KEY=your-key-value
```

The file is `~/.config/creatorflow/creatorflow.env` and must have mode `0600`.
The CLI also accepts an already-exported `YOUTUBE_DATA_API_KEY`; it never prints
the value. With no configured key, discovery exits with an actionable error.

Commands:

```bash
.venv/bin/python -m backend.services.reference_discovery discover --dry-run
.venv/bin/python -m backend.services.reference_discovery discover
.venv/bin/python -m backend.services.reference_discovery list
.venv/bin/python -m backend.services.reference_discovery list --status rejected
.venv/bin/python -m backend.services.reference_discovery show VIDEO_ID
.venv/bin/python -m backend.services.reference_discovery refresh-stats
.venv/bin/python -m backend.services.reference_discovery validate
.venv/bin/python -m backend.services.reference_discovery history VIDEO_ID
```

Discovery uses several documented default searches: funny gaming moments,
streamer reactions, competitive clutches, gaming failures, gaming challenges,
and horror-game reactions. Search placement is never enough to qualify a
candidate. Hydrated category, title, description, tags, channel, and query
metadata must provide positive gaming evidence. YouTube category 20 is strong
evidence. Entertainment and other categories require a recognized game,
gaming-specific tags or terminology, or a gaming query corroborated by the
video metadata. Query-only animal challenges, generic celebrity challenges,
unrelated reactions, and non-gaming sketches are excluded with visible
reasons. For non-Gaming categories, explicit unrelated title subjects also
override contradictory gaming keywords found only in tags or descriptions.

Configure the source pool, publication window, region, creator/topic caps,
queries, cohort sizes, and media retention with `discover --help`. The default
benchmark plan is 10 established high-view references plus 10 recent breakout
references. Set `--established-count` and `--breakout-count` explicitly, or
use `--target-count` to request a balanced split. Creator and inferred-topic
caps apply across both cohorts: two per creator and three per game/topic by
default.

Ranking model version 2 exposes separate cohort weights. Established ranking
emphasizes total views, engagement, gaming relevance, and verified vertical
composition. Breakout ranking emphasizes views per day, recency, engagement,
gaming relevance, and verified vertical composition. Both expose duration,
missing-evidence, source-quality, and diversity components. Compilation and
ranking markers receive visible penalties; explicit reposts and multi-creator
rankings are excluded. Near-duplicate titles retain the source with stronger
originality evidence. These heuristics help triage public evidence; neither
cohort predicts virality or objectively measures quality.

Topic inference uses a maintainable alias map over title, description, tags,
and query metadata. For example, FNAF maps to `fnaf`, COD to `call-of-duty`,
and recognized Roblox, Fortnite, and Minecraft evidence maps to their named
topics. Metadata-qualified gaming clips with no recognized game use
`unknown-gaming`; generic words such as “guess,” “funniest,” or “reaction”
never become topics.

Dry-run selections are labeled `metadata-qualified` with provisional media
verification. A dry run never downloads media or changes the queue. Normal
discovery locally downloads a bounded shortlist and uses FFprobe to require a
playable video stream, audio stream, approximately vertical composition, and a
5–180 second duration. It then reranks the media-verified pool. Rejected media
validation is reported explicitly, and only `media-verified` candidates enter
the reference-review queue. Search results alone are never treated as proof
that a video is a Short. Metadata snapshots, scores, validation results,
review state, and retained media are stored atomically under ignored
`data/reference_discovery/`.

Retained candidate media is stored under the persistent CreatorFlow data root
and queue records use release-independent paths such as
`reference_discovery/media/VIDEO_ID.mp4`. The queue resolves those paths
against its configured data root, so release replacement and pruning do not
break playback or later acceptance. Legacy absolute paths through a deployment
release, `current`, or the development checkout remain readable but validation
reports them as noncanonical.

After deploying the release that introduces canonical paths, preview and then
perform the atomic metadata-only migration:

```bash
cd /home/aasandoval/clip-factory-production/current
.venv/bin/python -m backend.services.reference_discovery \
  migrate-media-paths --dry-run
.venv/bin/python -m backend.services.reference_discovery \
  migrate-media-paths
.venv/bin/python -m backend.services.reference_discovery validate
```

Migration verifies every referenced persistent media file before replacing the
queue atomically. It rewrites only `media_path`, never copies, moves,
redownloads, or deletes media, and does not change notes, decisions, topics,
rankings, candidate IDs, or timestamps. A missing file aborts without changing
queue state. Repeating migration after success is a no-op.

Open `/reference-candidates` on the existing loopback review server. Each card
shows the public source, local preview when retained, creator, publication and
capture times, statistics, cohort, gaming/source evidence, validation stage,
ranking evidence, media measurements, current revision, recent sanitized
decision events, and any available analysis. A reviewer can leave notes,
correct the inferred game/topic, and use only actions legal for the current
state. Discovered candidates can be accepted, rejected, or marked duplicate.
Rejected and duplicate candidates can be explicitly reconsidered. Accepted
candidates can only be rejected through the confirmed **Withdraw Reference**
operation; ordinary rejection cannot silently detach queue state from the
strict reference index.

Reject, duplicate, and withdrawal require a meaningful reviewer note.
Acceptance notes remain optional. Set an optional display label in the private
service environment:

```text
AUTOCLIP_REVIEWER_NAME=Your display name
```

The label is audit metadata, not an authentication identity. Form submissions
include the candidate's current revision and a request ID. Stale or replayed
forms fail without changing queue state, timestamps, references, or audit
history. GET, HEAD, preview preload, refresh, and ordinary navigation remain
read-only. State-changing requests continue to require the in-memory form
token through a hidden POST field; it is never put in URLs, redirects, media
requests, journals, or audit events.

Manual topic corrections survive rediscovery and statistics refresh.
Acceptance creates a distinguishable
`automatic_youtube_discovery` source snapshot and then uses the existing strict
reference registration and analysis pipeline. Registration, analysis, queue
update, and audit persistence roll back together on failure.

Withdrawal validates reference ownership, checksum, expected artifacts, and
the retained discovery preview. It refuses when any generated profile still
lists the reference. On success it atomically removes the strict-index entry,
clears `accepted_reference_id`, advances the candidate revision, and moves the
accepted-reference directory into ignored local recovery storage under
`data/reference_discovery/withdrawal_recovery/`. The retained discovery media
is not deleted. Profiles are never rebuilt automatically.

The append-only local ledger is
`data/reference_discovery/decision_events.jsonl`. Events contain the action,
previous/requested/resulting state, revisions, reference IDs, outcome, safe
failure reason, reviewer display label, note, and request ID. They never
contain form tokens, API keys, cookies, authorization headers, or environment
values. Historical decisions made before the ledger remain historical; the
system does not invent actor or reason data for them.

Consistency validation is non-destructive and returns nonzero for queue/index
ownership, status, category, or profile-input mismatches:

```bash
.venv/bin/python -m backend.services.reference_discovery validate
```

After explicit human confirmation, an accepted reference can also be withdrawn
through the same service-layer transaction from the CLI:

```bash
.venv/bin/python -m backend.services.reference_discovery withdraw VIDEO_ID \
  --status rejected \
  --note "Does not match the desired clip style"
```

No discovery or review action republishes, remixes, or uploads media.

### Review-time timing correction

Transcript boundaries are useful for finding a spoken moment, but visual
context may happen earlier and a reaction or answer may happen later. A
reviewer can expand the preview without changing the analyzed candidate,
candidate score, or rank:

```bash
# Add visual context before the candidate.
python -m backend.app.review_clips adjust REVIEW_ID --lead-in 10

# Keep a reaction or answer after the candidate.
python -m backend.app.review_clips adjust REVIEW_ID --tail 8

# Add both.
python -m backend.app.review_clips adjust REVIEW_ID --lead-in 12 --tail 4

# Set explicit bounds that contain the original candidate.
python -m backend.app.review_clips adjust REVIEW_ID --render-start 800 --render-end 850

# Validate the source bounds and inspect the FFmpeg command without changing files.
python -m backend.app.review_clips adjust REVIEW_ID --tail 8 --dry-run

# Render the original candidate window again.
python -m backend.app.review_clips reset-timing REVIEW_ID
```

Relative adjustments are always calculated from the original candidate, not
from the previous preview. The default maximum adjusted duration is 60 seconds;
use `--allow-longer` only after reviewing the dry-run output. A verified
replacement increments the timing revision and returns the item to `pending`
for another human review. Existing notes are preserved unless `--note` replaces
one or `--clear-note` clears it. Preview replacement is local and atomic: a
failed render or probe leaves the prior valid preview and queue state intact.
None of the automatic-context controls uploads or publishes content.
