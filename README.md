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

CreatorFlow's local workflow is ingestion → transcription → clip-candidate
generation. Analyze all eligible downloaded videos with completed transcripts:

```bash
python -m backend.app.analyze_clips
```

Selection and configuration examples:

```bash
python -m backend.app.analyze_clips --video-id VIDEO_ID
python -m backend.app.analyze_clips --video-id VIDEO_ID --force
python -m backend.app.analyze_clips --minimum-duration 25 --target-duration 40 --maximum-duration 55
python -m backend.app.analyze_clips --video-id VIDEO_ID --show-score-breakdown
```

Ranked artifacts are written to
`data/clip_candidates/<video_id>/candidates.json`. Ranking uses transparent,
deterministic text and timing heuristics: identical transcript input and
configuration produce identical candidate boundaries, IDs, and scores.
Heuristic scores prioritize potentially self-contained moments; they do not
predict actual popularity or virality. This stage does not cut, render,
upload, or publish video. Add `--show-score-breakdown` to print each returned
candidate's ending classification, component scores, positive reasons, and
penalties. Displayed components sum to the displayed total within a 0.1
rounding tolerance. Normal output is unchanged when the flag is omitted.
