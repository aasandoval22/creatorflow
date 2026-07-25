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

CreatorFlow's local workflow is ingestion → transcription → candidate analysis
→ batch preview rendering → local human review → timing correction and
re-review. Analyze all eligible downloaded videos with
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
```

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

The server command is optional and must be started intentionally; the review
command never starts a server. Approval is local review state only. It does not
upload, schedule, or publish a clip anywhere.

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
