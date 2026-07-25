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
review server and scheduled production runner. Install the units for the
current repository checkout:

```bash
.venv/bin/python -m backend.services.autoclip_service install
```

Installation renders the tracked templates in `deploy/systemd/` into
`~/.config/systemd/user/` using absolute paths to this repository and its
virtual environment. It is atomic and idempotent: rerunning it updates the
same three unit files without creating duplicate services or timers. It also
creates `~/.config/creatorflow/creatorflow.env` with mode `0600` if that local
configuration file does not exist. Existing environment-file content is never
overwritten.

The default production interval is 30 minutes. Set another positive systemd
duration during installation without editing Python source:

```bash
.venv/bin/python -m backend.services.autoclip_service install --interval 2h
```

Optional production-runner and review-server arguments belong in the local
environment file outside the repository:

```text
AUTOCLIP_PRODUCTION_ARGS=--max-videos 2 --top 2
AUTOCLIP_REVIEW_ARGS=--port 8080
```

YouTube discovery and downloads explicitly configure yt-dlp's Deno JavaScript
runtime. CreatorFlow checks `AUTOCLIP_DENO_PATH`, then
`~/.deno/bin/deno`, then `PATH`. This gives interactive and systemd-managed
runs identical behavior even when the user service PATH omits the Deno install
directory. If no executable runtime is available, yt-dlp continues with a
clear warning because Deno is optional in offline development environments.

Keep the review server loopback-bound. The managed unit always supplies
`--host 127.0.0.1`; use an SSH tunnel for remote browser access as described
below. Do not put secrets in the environment file unless the local host
requires them for some separately managed dependency.

Manage the services with one entry point:

```bash
.venv/bin/python -m backend.services.autoclip_service start
.venv/bin/python -m backend.services.autoclip_service stop
.venv/bin/python -m backend.services.autoclip_service restart
.venv/bin/python -m backend.services.autoclip_service status
.venv/bin/python -m backend.services.autoclip_service logs
.venv/bin/python -m backend.services.autoclip_service logs --lines 250
.venv/bin/python -m backend.services.autoclip_service run-now
.venv/bin/python -m backend.services.autoclip_service disable
```

`start` enables and starts the review service and production timer. The review
service uses `Restart=on-failure` with a five-second delay. The timer waits five
minutes after activation before its first run, then uses the configured
interval. `stop` gracefully stops the timer, an active production unit, and the
review server. `disable` disables only automatic production and leaves the
review server unchanged.
`run-now` invokes the same oneshot production service immediately. Scheduled
and manual service runs retain `production_runner`'s nonblocking file lock, so
they cannot overlap.

`status` reports the review service and production timer states, the next timer
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
faster-whisper runtime. Use `--no-transcription` when only media, scene-change,
and silence measurements are wanted. Scene changes are pixel-change signals,
and transcript reaction/payoff findings are heuristics; neither proves humor or
quality.

Compare rejected previews for a video:

```bash
python -m backend.app.reference_clips compare \
  --profile personality_reaction --video-id hhQzg7Him1g --status rejected
```

The interactive review page also offers **Compare to Reference Profile**. It
writes a local report and leaves decisions, notes, timing, and previews
unchanged. The report separates known measurements, heuristic findings, and
unavailable evidence.

One accepted reference produces a `provisional` profile because one example
does not establish statistical confidence. Add later accepted references in
their own directories with `reference.mp4` and `baseline.json`, register and
analyze them, then rebuild the profile to use medians and observed ranges.
Profiles remain soft priors and never change candidate selection or rendering
defaults automatically.

Reference media, annotations, source metadata, analyses, profiles, analyzer
temporary files, and comparison reports stay under ignored `data/` paths.
These commands perform no downloading, uploading, publishing, or platform
access. The current workflow is ingestion → transcription → candidate analysis
→ preview rendering → local review → accepted-reference comparison → human
timing/decision changes when warranted.

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
