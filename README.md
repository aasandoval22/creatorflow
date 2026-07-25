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
