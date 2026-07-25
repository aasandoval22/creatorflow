"""Discover and review public gaming Shorts as possible clip references."""

from __future__ import annotations

import argparse
import copy
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import yt_dlp

from backend.services.reference_clip_analyzer import ReferenceClipAnalyzer
from backend.services.reference_clip_library import ReferenceClipLibrary
from backend.services.video_manifest import utc_now


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ROOT = PROJECT_ROOT / "data" / "reference_discovery"
DEFAULT_QUEUE_PATH = DEFAULT_ROOT / "candidates.json"
DEFAULT_MEDIA_ROOT = DEFAULT_ROOT / "media"
DEFAULT_REFERENCE_ROOT = PROJECT_ROOT / "data" / "reference_clips"
DEFAULT_ENVIRONMENT_FILE = Path.home() / ".config" / "creatorflow" / "creatorflow.env"
QUEUE_VERSION = 1
SCORE_VERSION = 1
STATUSES = frozenset({"discovered", "accepted", "rejected", "duplicate"})
DEFAULT_QUERIES = (
    "gaming funny moments shorts",
    "streamer reaction gaming shorts",
    "competitive gaming clutch shorts",
    "gaming fail funny shorts",
    "gaming challenge shorts",
    "horror game reaction shorts",
)
TOPIC_HINTS = (
    "fortnite", "minecraft", "valorant", "call of duty", "cod", "roblox",
    "clash royale", "gta", "grand theft auto", "apex", "overwatch",
    "league of legends", "rocket league", "counter strike", "cs2",
)
TOKEN = re.compile(r"[a-z0-9]+")


class ReferenceDiscoveryError(RuntimeError):
    """Discovery input, state, or local analysis needs user action."""


def _atomic_json(path: Path, document: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.",
            suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(document, stream, indent=2, sort_keys=True, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise ReferenceDiscoveryError(f"Invalid UTC timestamp: {value!r}.") from error
    if parsed.tzinfo is None:
        raise ReferenceDiscoveryError(f"Timestamp has no timezone: {value!r}.")
    return parsed.astimezone(timezone.utc)


def parse_iso_duration(value: str) -> float:
    match = re.fullmatch(
        r"P(?:(?P<days>\d+)D)?T(?:(?P<hours>\d+)H)?"
        r"(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+(?:\.\d+)?)S)?",
        value,
    )
    if not match:
        raise ReferenceDiscoveryError(f"Unsupported YouTube duration {value!r}.")
    parts = {name: float(number or 0) for name, number in match.groupdict().items()}
    return (
        parts["days"] * 86400 + parts["hours"] * 3600
        + parts["minutes"] * 60 + parts["seconds"]
    )


def infer_topic(title: str, query: str) -> str:
    haystack = f"{title} {query}".casefold()
    for topic in TOPIC_HINTS:
        if topic in haystack:
            return topic.replace(" ", "_")
    tokens = [token for token in TOKEN.findall(title.casefold()) if len(token) > 3]
    ignored = {"shorts", "gaming", "funny", "moment", "moments", "video", "game"}
    return next((token for token in tokens if token not in ignored), "general_gaming")


def normalized_title(title: str) -> str:
    ignored = {"short", "shorts", "gaming", "clip", "viral"}
    return " ".join(token for token in TOKEN.findall(title.casefold()) if token not in ignored)


def views_per_day(views: int, published_at: str, *, now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    age = max((now - _utc(published_at)).total_seconds() / 86400, 1.0)
    return views / age


class YouTubeDataAPI:
    endpoint = "https://www.googleapis.com/youtube/v3"

    def __init__(
        self, api_key: str | None = None, *,
        opener: Callable[..., Any] = urlopen,
        environment_file: Path = DEFAULT_ENVIRONMENT_FILE,
    ) -> None:
        self.api_key = (
            api_key or os.environ.get("YOUTUBE_DATA_API_KEY")
            or _private_environment_value(
                Path(environment_file), "YOUTUBE_DATA_API_KEY"
            )
        )
        self.opener = opener

    def _get(self, resource: str, parameters: Mapping[str, Any]) -> dict[str, Any]:
        if not self.api_key:
            raise ReferenceDiscoveryError(
                "YouTube Data API key is not configured. Set YOUTUBE_DATA_API_KEY "
                "in ~/.config/creatorflow/creatorflow.env (mode 0600), then restart "
                "the invoking shell or service. The key is never printed."
            )
        query = urlencode({**parameters, "key": self.api_key})
        request = Request(f"{self.endpoint}/{resource}?{query}")
        try:
            with self.opener(request, timeout=30) as response:
                document = json.load(response)
        except Exception as error:
            raise ReferenceDiscoveryError(
                f"YouTube Data API {resource} request failed "
                f"({type(error).__name__}). Check API enablement, quota, key "
                "restrictions, and network access."
            ) from error
        if not isinstance(document, dict):
            raise ReferenceDiscoveryError("YouTube Data API returned malformed JSON.")
        if isinstance(document.get("error"), dict):
            detail = document["error"].get("message")
            raise ReferenceDiscoveryError(
                f"YouTube Data API rejected the {resource} request"
                + (f": {detail}" if isinstance(detail, str) else ".")
            )
        return document

    def search(
        self, queries: Sequence[str], *, pool_size: int,
        published_after: str, region: str,
    ) -> list[dict[str, str]]:
        per_query = max(5, min(50, math.ceil(pool_size / max(1, len(queries)))))
        found: dict[str, dict[str, str]] = {}
        for query in queries:
            document = self._get(
                "search",
                {
                    "part": "snippet", "type": "video", "videoDuration": "short",
                    "order": "viewCount", "maxResults": per_query, "q": query,
                    "publishedAfter": published_after, "regionCode": region,
                    "safeSearch": "moderate",
                },
            )
            for item in document.get("items", []):
                video_id = (item.get("id") or {}).get("videoId")
                snippet = item.get("snippet") or {}
                if isinstance(video_id, str) and video_id and video_id not in found:
                    found[video_id] = {
                        "video_id": video_id,
                        "discovery_query": query,
                        "search_title": str(snippet.get("title") or ""),
                    }
                if len(found) >= pool_size:
                    return list(found.values())
        return list(found.values())

    def hydrate(self, search_items: Sequence[dict[str, str]]) -> list[dict[str, Any]]:
        queries = {item["video_id"]: item["discovery_query"] for item in search_items}
        hydrated: list[dict[str, Any]] = []
        ids = list(queries)
        for offset in range(0, len(ids), 50):
            document = self._get(
                "videos",
                {
                    "part": "snippet,contentDetails,statistics",
                    "id": ",".join(ids[offset:offset + 50]),
                },
            )
            for item in document.get("items", []):
                video_id = item.get("id")
                snippet = item.get("snippet") or {}
                statistics = item.get("statistics") or {}
                details = item.get("contentDetails") or {}
                if not isinstance(video_id, str) or video_id not in queries:
                    continue
                try:
                    duration = parse_iso_duration(str(details.get("duration")))
                    views = int(statistics.get("viewCount", 0))
                except (TypeError, ValueError, ReferenceDiscoveryError):
                    continue
                hydrated.append(
                    {
                        "video_id": video_id,
                        "source_url": f"https://www.youtube.com/watch?v={video_id}",
                        "title": str(snippet.get("title") or ""),
                        "creator": str(snippet.get("channelTitle") or ""),
                        "channel_id": str(snippet.get("channelId") or ""),
                        "published_at": str(snippet.get("publishedAt") or ""),
                        "view_count": views,
                        "like_count": _optional_int(statistics.get("likeCount")),
                        "comment_count": _optional_int(statistics.get("commentCount")),
                        "duration": duration,
                        "discovery_query": queries[video_id],
                        "captured_at": utc_now(),
                    }
                )
        return hydrated


def _optional_int(value: Any) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _private_environment_value(path: Path, name: str) -> str | None:
    if not path.exists():
        return None
    try:
        if path.stat().st_mode & 0o077:
            raise ReferenceDiscoveryError(
                f"Private environment file {path} must have mode 0600."
            )
        for line in path.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() == name:
                cleaned = value.strip().strip("'\"")
                return cleaned or None
    except OSError as error:
        raise ReferenceDiscoveryError(
            f"Cannot read private environment file {path}: {error}."
        ) from error
    return None


@dataclass(frozen=True)
class MediaEvidence:
    downloadable: bool
    duration: float | None = None
    width: int | None = None
    height: int | None = None
    frame_rate: float | None = None
    has_video: bool = False
    has_audio: bool = False
    media_path: str | None = None
    error: str | None = None

    @property
    def vertical(self) -> bool | None:
        if not self.width or not self.height:
            return None
        return self.height / self.width >= 1.25

    @property
    def valid_short(self) -> bool:
        return bool(
            self.downloadable and self.has_video and self.has_audio
            and self.vertical is True and self.duration is not None
            and 5 <= self.duration <= 180
        )


class LocalMediaValidator:
    def __init__(
        self, media_root: Path = DEFAULT_MEDIA_ROOT, *,
        ffprobe_path: str = "ffprobe",
    ) -> None:
        self.media_root = Path(media_root)
        self.ffprobe_path = ffprobe_path

    def validate(self, candidate: Mapping[str, Any], *, retain: bool = True) -> MediaEvidence:
        self.media_root.mkdir(parents=True, exist_ok=True)
        video_id = candidate["video_id"]
        output = self.media_root / f"{video_id}.mp4"
        try:
            with yt_dlp.YoutubeDL(
                {
                    "format": "bestvideo*+bestaudio/best",
                    "merge_output_format": "mp4",
                    "outtmpl": str(output),
                    "noplaylist": True,
                    "quiet": True,
                    "overwrites": False,
                }
            ) as downloader:
                downloader.download([candidate["source_url"]])
            completed = output if output.is_file() else next(
                self.media_root.glob(f"{video_id}.*")
            )
            probe = subprocess.run(
                [
                    self.ffprobe_path, "-v", "error", "-show_streams",
                    "-show_format", "-of", "json", str(completed),
                ],
                check=False, capture_output=True, text=True,
            )
            if probe.returncode:
                raise ReferenceDiscoveryError(probe.stderr.strip() or "FFprobe failed.")
            document = json.loads(probe.stdout)
            streams = document.get("streams", [])
            video = next((item for item in streams if item.get("codec_type") == "video"), None)
            audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
            if not isinstance(video, dict):
                raise ReferenceDiscoveryError("Downloaded media has no playable video stream.")
            duration = _float_or_none((document.get("format") or {}).get("duration"))
            rate = _frame_rate(video.get("avg_frame_rate") or video.get("r_frame_rate"))
            evidence = MediaEvidence(
                True, duration, _optional_int(video.get("width")),
                _optional_int(video.get("height")), rate, True, audio is not None,
                str(completed) if retain else None,
            )
            if not retain:
                completed.unlink(missing_ok=True)
            return evidence
        except Exception as error:
            for partial in self.media_root.glob(f"{video_id}.*"):
                partial.unlink(missing_ok=True)
            return MediaEvidence(False, error=str(error))


def _float_or_none(value: Any) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _frame_rate(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    numerator, separator, denominator = value.partition("/")
    try:
        return float(numerator) / float(denominator) if separator else float(value)
    except (ValueError, ZeroDivisionError):
        return None


def score_candidate(
    candidate: Mapping[str, Any], *, now: datetime | None = None,
) -> tuple[float, dict[str, Any]]:
    now = now or datetime.now(timezone.utc)
    views = max(0, int(candidate.get("view_count") or 0))
    daily = views_per_day(views, candidate["published_at"], now=now)
    likes, comments = candidate.get("like_count"), candidate.get("comment_count")
    duration = float(candidate.get("verified_duration") or candidate.get("duration") or 0)
    vertical = candidate.get("verified_vertical")
    age_days = max((now - _utc(candidate["published_at"])).total_seconds() / 86400, 1)
    components = {
        "raw_views": min(22.0, math.log10(views + 1) / 8 * 22),
        "views_per_day": min(24.0, math.log10(daily + 1) / 6 * 24),
        "like_ratio": min(12.0, (likes / views * 240) if likes is not None and views else 0),
        "comment_ratio": min(
            8.0, (comments / views * 800) if comments is not None and views else 0
        ),
        "recency": max(0.0, 12.0 * (1 - min(age_days, 365) / 365)),
        "duration": 10.0 if 10 <= duration <= 90 else (5.0 if 5 <= duration <= 180 else 0.0),
        "vertical": 10.0 if vertical is True else 0.0,
        "creator_diversity": 0.0,
        "topic_diversity": 0.0,
        "evidence_penalty": -4.0 * sum(
            value is None for value in (likes, comments, vertical)
        ),
    }
    total = round(max(0.0, sum(components.values())), 4)
    explanation = {
        "version": SCORE_VERSION, "total": total,
        "components": {name: round(value, 4) for name, value in components.items()},
        "evidence": (
            f"{views:,} views; {daily:,.1f} views/day; "
            f"duration {duration:.1f}s; vertical={vertical!r}. "
            "This transparent ranking is not a prediction of virality or objective quality."
        ),
    }
    return total, explanation


def select_diverse(
    candidates: Sequence[dict[str, Any]], *, count: int = 20,
    max_per_creator: int = 2, max_per_topic: int = 3,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    ranked = []
    seen_titles: set[str] = set()
    for original in candidates:
        item = copy.deepcopy(original)
        title_identity = normalized_title(item["title"])
        if title_identity and title_identity in seen_titles:
            continue
        seen_titles.add(title_identity)
        item["topic"] = item.get("topic") or infer_topic(
            item["title"], item["discovery_query"]
        )
        item["score"], item["ranking"] = score_candidate(item, now=now)
        ranked.append(item)
    ranked.sort(key=lambda item: (-item["score"], item["video_id"]))
    creators: dict[str, int] = {}
    topics: dict[str, int] = {}
    selected = []
    remaining = list(ranked)
    while remaining and len(selected) < count:
        eligible = [
            item for item in remaining
            if creators.get(item["creator"].casefold(), 0) < max_per_creator
            and topics.get(item["topic"], 0) < max_per_topic
        ]
        if not eligible:
            break
        def adjusted(item: dict[str, Any]) -> tuple[float, str]:
            creator_bonus = 2.0 if not creators.get(item["creator"].casefold()) else 0.0
            topic_bonus = 2.0 if not topics.get(item["topic"]) else 0.0
            return item["score"] + creator_bonus + topic_bonus, item["video_id"]
        item = max(eligible, key=lambda value: (adjusted(value)[0], adjusted(value)[1]))
        remaining.remove(item)
        creator = item["creator"].casefold()
        topic = item["topic"]
        creator_bonus = 2.0 if not creators.get(creator) else 0.0
        topic_bonus = 2.0 if not topics.get(topic) else 0.0
        item["ranking"]["components"]["creator_diversity"] = creator_bonus
        item["ranking"]["components"]["topic_diversity"] = topic_bonus
        item["score"] = round(item["score"] + creator_bonus + topic_bonus, 4)
        item["ranking"]["total"] = item["score"]
        creators[creator] = creators.get(creator, 0) + 1
        topics[topic] = topics.get(topic, 0) + 1
        item["rank"] = len(selected) + 1
        selected.append(item)
    return selected


class ReferenceCandidateQueue:
    def __init__(self, path: Path = DEFAULT_QUEUE_PATH) -> None:
        self.path = Path(path)
        if self.path.exists():
            self._load()

    @staticmethod
    def empty() -> dict[str, Any]:
        return {"version": QUEUE_VERSION, "updated_at": utc_now(), "items": []}

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self.empty()
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ReferenceDiscoveryError(
                f"Reference candidate queue {self.path} is unreadable: {error}."
            ) from error
        if (
            not isinstance(document, dict)
            or set(document) != {"version", "updated_at", "items"}
            or document["version"] != QUEUE_VERSION
            or not isinstance(document["items"], list)
        ):
            raise ReferenceDiscoveryError("Reference candidate queue is malformed.")
        ids = set()
        for item in document["items"]:
            if (
                not isinstance(item, dict)
                or item.get("status") not in STATUSES
                or not isinstance(item.get("video_id"), str)
                or item["video_id"] in ids
            ):
                raise ReferenceDiscoveryError("Reference candidate queue has an invalid item.")
            ids.add(item["video_id"])
        return document

    def list(self, *, status: str | None = None) -> list[dict[str, Any]]:
        if status is not None and status not in STATUSES:
            raise ReferenceDiscoveryError(f"Unsupported candidate status {status!r}.")
        return [
            copy.deepcopy(item) for item in self._load()["items"]
            if status is None or item["status"] == status
        ]

    def get(self, video_id: str) -> dict[str, Any]:
        for item in self._load()["items"]:
            if item["video_id"] == video_id:
                return copy.deepcopy(item)
        raise ReferenceDiscoveryError(f"Reference candidate {video_id!r} does not exist.")

    def upsert_discovered(self, candidates: Sequence[dict[str, Any]]) -> None:
        document = self._load()
        existing = {item["video_id"]: item for item in document["items"]}
        now = utc_now()
        for candidate in candidates:
            previous = existing.get(candidate["video_id"])
            if previous and previous["status"] != "discovered":
                continue
            item = {
                **copy.deepcopy(candidate),
                "status": "discovered",
                "notes": (previous or {}).get("notes"),
                "category": (previous or {}).get("category", "gaming_highlight"),
                "accepted_reference_id": (previous or {}).get("accepted_reference_id"),
                "created_at": (previous or {}).get("created_at", now),
                "updated_at": now,
                "origin": "automatic_youtube_discovery",
            }
            existing[item["video_id"]] = item
        document["items"] = sorted(existing.values(), key=lambda item: item["video_id"])
        document["updated_at"] = now
        _atomic_json(self.path, document)

    def decide(
        self, video_id: str, status: str, *, notes: str | None = None,
        category: str | None = None, accepted_reference_id: str | None = None,
    ) -> dict[str, Any]:
        if status not in STATUSES:
            raise ReferenceDiscoveryError(f"Unsupported candidate status {status!r}.")
        document = self._load()
        for item in document["items"]:
            if item["video_id"] == video_id:
                item["status"] = status
                if notes is not None:
                    item["notes"] = notes
                if category is not None:
                    item["category"] = category
                if accepted_reference_id is not None:
                    item["accepted_reference_id"] = accepted_reference_id
                item["updated_at"] = utc_now()
                document["updated_at"] = item["updated_at"]
                _atomic_json(self.path, document)
                return copy.deepcopy(item)
        raise ReferenceDiscoveryError(f"Reference candidate {video_id!r} does not exist.")

    def refresh_metadata(self, candidates: Sequence[dict[str, Any]]) -> None:
        document = self._load()
        updates = {item["video_id"]: item for item in candidates}
        now = utc_now()
        protected = {
            "status", "notes", "category", "accepted_reference_id",
            "created_at", "origin", "media_path", "validation_error",
            "verified_duration", "verified_vertical", "width", "height",
            "frame_rate", "has_video", "has_audio", "downloadable",
        }
        for item in document["items"]:
            update = updates.get(item["video_id"])
            if update is None:
                continue
            for name, value in update.items():
                if name not in protected:
                    item[name] = copy.deepcopy(value)
            item["updated_at"] = now
        document["updated_at"] = now
        _atomic_json(self.path, document)


class ReferenceDiscoveryService:
    def __init__(
        self, api: YouTubeDataAPI, queue: ReferenceCandidateQueue, *,
        media_validator: Any | None = None,
        reference_library: ReferenceClipLibrary | None = None,
        analyzer_factory: Callable[[ReferenceClipLibrary], Any] = ReferenceClipAnalyzer,
        reference_root: Path = DEFAULT_REFERENCE_ROOT,
    ) -> None:
        self.api = api
        self.queue = queue
        self.media_validator = media_validator or LocalMediaValidator()
        self.reference_library = reference_library or ReferenceClipLibrary()
        self.analyzer_factory = analyzer_factory
        self.reference_root = Path(reference_root)

    def discover(
        self, *, target_count: int = 20, pool_size: int = 100,
        publication_days: int = 365, region: str = "US",
        max_per_creator: int = 2, max_per_topic: int = 3,
        queries: Sequence[str] = DEFAULT_QUERIES, dry_run: bool = False,
        retain_media: str = "selected",
    ) -> dict[str, Any]:
        after = (
            datetime.now(timezone.utc) - timedelta(days=publication_days)
        ).isoformat().replace("+00:00", "Z")
        search = self.api.search(
            queries, pool_size=pool_size, published_after=after, region=region
        )
        hydrated = self.api.hydrate(search)
        prelim = sorted(
            hydrated,
            key=lambda item: score_candidate(
                {**item, "verified_vertical": None}
            )[0],
            reverse=True,
        )[:max(target_count * 3, target_count)]
        planned = select_diverse(
            [{**item, "verified_vertical": None} for item in prelim],
            count=target_count, max_per_creator=max_per_creator,
            max_per_topic=max_per_topic,
        )
        if dry_run:
            return {
                "dry_run": True, "pool_count": len(hydrated),
                "planned_count": len(planned), "selected": planned,
            }
        verified = []
        for item in prelim:
            evidence = self.media_validator.validate(
                item, retain=retain_media in {"selected", "all"}
            )
            enriched = {
                **item,
                "downloadable": evidence.downloadable,
                "verified_duration": evidence.duration,
                "width": evidence.width, "height": evidence.height,
                "frame_rate": evidence.frame_rate,
                "has_video": evidence.has_video, "has_audio": evidence.has_audio,
                "verified_vertical": evidence.vertical,
                "media_path": evidence.media_path,
                "validation_error": evidence.error,
            }
            if evidence.valid_short:
                verified.append(enriched)
        selected = select_diverse(
            verified, count=target_count, max_per_creator=max_per_creator,
            max_per_topic=max_per_topic,
        )
        if retain_media == "selected":
            selected_ids = {item["video_id"] for item in selected}
            for item in verified:
                if item["video_id"] not in selected_ids and item.get("media_path"):
                    Path(item["media_path"]).unlink(missing_ok=True)
        self.queue.upsert_discovered(selected)
        return {
            "dry_run": False, "pool_count": len(hydrated),
            "verified_count": len(verified), "selected_count": len(selected),
            "selected": selected,
        }

    def refresh_stats(self) -> int:
        candidates = self.queue.list()
        if not candidates:
            return 0
        hydrated = self.api.hydrate(
            [
                {"video_id": item["video_id"], "discovery_query": item["discovery_query"]}
                for item in candidates
            ]
        )
        refreshed = []
        by_id = {item["video_id"]: item for item in hydrated}
        for candidate in candidates:
            stats = by_id.get(candidate["video_id"])
            if not stats:
                continue
            updated = {**candidate, **stats}
            updated["score"], updated["ranking"] = score_candidate(updated)
            refreshed.append(updated)
        self.queue.refresh_metadata(refreshed)
        return len(refreshed)

    def accept(
        self, video_id: str, *, category: str, notes: str,
        transcription: bool = False,
    ) -> dict[str, Any]:
        candidate = self.queue.get(video_id)
        if candidate["status"] != "discovered":
            raise ReferenceDiscoveryError("Only discovered candidates can be accepted.")
        media_path = candidate.get("media_path")
        if not isinstance(media_path, str) or not Path(media_path).is_file():
            evidence = self.media_validator.validate(candidate, retain=True)
            if not evidence.valid_short or not evidence.media_path:
                raise ReferenceDiscoveryError(
                    f"Candidate media could not be validated: {evidence.error or 'invalid media'}."
                )
            media_path = evidence.media_path
        reference_id = f"youtube-{video_id}"
        directory = self.reference_root / f"discovered-{video_id}"
        directory.mkdir(parents=True, exist_ok=True)
        target_media = directory / "reference.mp4"
        if Path(media_path).resolve() != target_media.resolve():
            _copy_atomic(Path(media_path), target_media)
        baseline = {
            "version": 1, "reference_id": reference_id,
            "source_video_id": video_id, "source_title": candidate["title"],
            "creator": candidate["creator"], "status": "accepted",
            "purpose": "creatorflow_baseline", "profile_name": category,
            "qualities": ["human-accepted discovered benchmark candidate"],
            "layout": {
                "orientation": "vertical", "composition": "unknown",
                "top_region": "unknown", "bottom_region": "unknown",
                "facecam_prominence": "unknown",
            },
            "story_structure": {
                "opening_style": "unknown", "setup_requirement": "unknown",
                "primary_focus": "unknown", "payoff_type": "unknown",
                "payoff_required": False, "ending_style": "unknown",
            },
            "timing_preferences": {
                "requires_long_lead_in": False,
                "requires_complete_setup": False,
                "requires_complete_payoff": False,
                "preferred_ending": "human review required",
            },
            "notes": notes or "Accepted from automatic YouTube benchmark discovery.",
        }
        baseline_path = directory / "baseline.json"
        source_path = directory / "reference.info.json"
        _atomic_json(baseline_path, baseline)
        _atomic_json(
            source_path,
            {
                "origin": "automatic_youtube_discovery",
                "metadata_snapshot": candidate,
            },
        )
        registered = False
        try:
            entry = self.reference_library.register(
                media_path=target_media, baseline_path=baseline_path,
                source_info_path=source_path, reference_id=reference_id,
                profile_name=category,
            )
            registered = True
            self.analyzer_factory(self.reference_library).analyze(
                reference_id, transcription=transcription
            )
        except Exception:
            if registered:
                self.reference_library.remove(reference_id)
            for path in (
                directory / "analysis.json", source_path, baseline_path, target_media
            ):
                path.unlink(missing_ok=True)
            try:
                directory.rmdir()
            except OSError:
                pass
            raise
        self.queue.decide(
            video_id, "accepted", notes=notes, category=category,
            accepted_reference_id=reference_id,
        )
        return entry


def _copy_atomic(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with source.open("rb") as source_stream, tempfile.NamedTemporaryFile(
            "wb", dir=destination.parent, prefix=f".{destination.name}.",
            suffix=".tmp", delete=False,
        ) as target:
            temporary = Path(target.name)
            while block := source_stream.read(1024 * 1024):
                target.write(block)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Discover public gaming Shorts for human reference review."
    )
    parser.add_argument("--queue-path", type=Path, default=DEFAULT_QUEUE_PATH)
    sub = parser.add_subparsers(dest="command", required=True)
    discover = sub.add_parser("discover")
    discover.add_argument("--dry-run", action="store_true")
    discover.add_argument("--target-count", type=int, default=20)
    discover.add_argument("--pool-size", type=int, default=100)
    discover.add_argument("--publication-days", type=int, default=365)
    discover.add_argument("--region", default="US")
    discover.add_argument("--max-per-creator", type=int, default=2)
    discover.add_argument("--max-per-topic", type=int, default=3)
    discover.add_argument("--retain-media", choices=("selected", "none", "all"), default="selected")
    discover.add_argument("--query", action="append", dest="queries")
    listing = sub.add_parser("list")
    listing.add_argument("--status", choices=sorted(STATUSES))
    show = sub.add_parser("show")
    show.add_argument("video_id")
    sub.add_parser("refresh-stats")
    sub.add_parser("validate")
    return parser


def main(argv: Sequence[str] | None = ()) -> int:
    args = build_parser().parse_args(argv)
    queue = ReferenceCandidateQueue(args.queue_path)
    service = ReferenceDiscoveryService(YouTubeDataAPI(), queue)
    try:
        if args.command == "discover":
            for value, label in (
                (args.target_count, "target count"), (args.pool_size, "pool size"),
                (args.publication_days, "publication days"),
                (args.max_per_creator, "creator cap"),
                (args.max_per_topic, "topic cap"),
            ):
                if value < 1:
                    raise ReferenceDiscoveryError(f"{label} must be positive.")
            result = service.discover(
                target_count=args.target_count, pool_size=args.pool_size,
                publication_days=args.publication_days, region=args.region,
                max_per_creator=args.max_per_creator,
                max_per_topic=args.max_per_topic,
                queries=tuple(args.queries or DEFAULT_QUERIES),
                dry_run=args.dry_run, retain_media=args.retain_media,
            )
            print(json.dumps(result, indent=2, sort_keys=True))
        elif args.command == "list":
            print(json.dumps(queue.list(status=args.status), indent=2, sort_keys=True))
        elif args.command == "show":
            print(json.dumps(queue.get(args.video_id), indent=2, sort_keys=True))
        elif args.command == "refresh-stats":
            print(f"Refreshed {service.refresh_stats()} candidate(s).")
        else:
            items = queue.list()
            missing_media = sum(
                bool(item.get("media_path"))
                and not Path(item["media_path"]).is_file()
                for item in items
            )
            if missing_media:
                raise ReferenceDiscoveryError(
                    f"Queue is valid, but {missing_media} retained media file(s) are missing."
                )
            print(f"Reference candidate queue is valid: {len(items)} item(s).")
    except ReferenceDiscoveryError as error:
        print(f"Reference discovery failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(None))
