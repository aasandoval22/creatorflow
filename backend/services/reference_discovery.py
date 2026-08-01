"""Discover and review public gaming Shorts as possible clip references."""

from __future__ import annotations

import argparse
import copy
import fcntl
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import yt_dlp

from backend.services.reference_clip_analyzer import ReferenceClipAnalyzer
from backend.services.reference_clip_library import (
    ReferenceClipError,
    ReferenceClipLibrary,
    load_and_validate_baseline,
)
from backend.services.reference_decision_audit import (
    ReferenceDecisionAuditError,
    ReferenceDecisionAuditLedger,
    configured_reviewer_name,
)
from backend.services.video_manifest import utc_now
from backend.services.youtube_downloader import (
    YtDlpRuntimeConfiguration,
    build_ytdlp_runtime_configuration,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_ROOT = PROJECT_ROOT / "data"
REFERENCE_MEDIA_RELATIVE_ROOT = Path("reference_discovery") / "media"
DEFAULT_ROOT = DEFAULT_DATA_ROOT / "reference_discovery"
DEFAULT_QUEUE_PATH = DEFAULT_ROOT / "candidates.json"
DEFAULT_MEDIA_ROOT = DEFAULT_DATA_ROOT / REFERENCE_MEDIA_RELATIVE_ROOT
DEFAULT_REFERENCE_ROOT = PROJECT_ROOT / "data" / "reference_clips"
DEFAULT_PROFILE_ROOT = PROJECT_ROOT / "data" / "reference_profiles"
DEFAULT_AUDIT_PATH = DEFAULT_ROOT / "decision_events.jsonl"
DEFAULT_DECISION_LOCK = DEFAULT_ROOT / ".decision.lock"
DEFAULT_WITHDRAWAL_RECOVERY_ROOT = DEFAULT_ROOT / "withdrawal_recovery"
DEFAULT_ENVIRONMENT_FILE = Path.home() / ".config" / "creatorflow" / "creatorflow.env"
QUEUE_VERSION = 1
SCORE_VERSION = 2
RELEVANCE_VERSION = 1
SOURCE_QUALITY_VERSION = 1
STATUSES = frozenset({"discovered", "accepted", "rejected", "duplicate"})
LEGAL_ACTIONS = {
    "discovered": frozenset({"accept", "reject", "duplicate"}),
    "accepted": frozenset({"withdraw"}),
    "rejected": frozenset({"reconsider"}),
    "duplicate": frozenset({"reconsider"}),
}
DEFAULT_QUERIES = (
    "gaming funny moments shorts",
    "streamer reaction gaming shorts",
    "competitive gaming clutch shorts",
    "gaming fail funny shorts",
    "gaming challenge shorts",
    "horror game reaction shorts",
)
TOKEN = re.compile(r"[a-z0-9]+")
GAME_TOPIC_ALIASES: dict[str, tuple[str, ...]] = {
    "fnaf": ("five nights at freddy's", "five nights at freddys", "fnaf"),
    "call-of-duty": ("call of duty", "warzone", "cod"),
    "destiny-2": ("destiny 2", "destiny ii", "destiny"),
    "roblox": ("roblox",),
    "fortnite": ("fortnite", "victory royale"),
    "minecraft": ("minecraft",),
    "valorant": ("valorant",),
    "clash-royale": ("clash royale",),
    "grand-theft-auto": ("grand theft auto", "gta 5", "gta v", "gta"),
    "apex-legends": ("apex legends", "apex"),
    "overwatch": ("overwatch 2", "overwatch"),
    "league-of-legends": ("league of legends",),
    "rocket-league": ("rocket league",),
    "counter-strike": ("counter strike 2", "counter-strike 2", "cs2"),
    "marvel-rivals": ("marvel rivals",),
    "dead-by-daylight": ("dead by daylight", "dbd"),
    "among-us": ("among us",),
    "rainbow-six-siege": ("rainbow six siege", "r6 siege"),
}
GAMEPLAY_TERMS = frozenset(
    {
        "boss fight", "clutch", "controller", "esports", "gameplay", "gamer",
        "gaming", "headshot", "killstreak", "let s play", "loadout", "lobby",
        "matchmaking", "multiplayer", "playthrough", "ranked match", "speedrun",
        "speedrunning", "video game",
    }
)
GAMING_TAGS = frozenset(
    {
        "esports", "fps", "gameplay", "gamer", "gaming", "gaming shorts",
        "let s play", "rpg", "speedrun", "stream highlights", "video games",
    }
)
GENERIC_TITLE_WORDS = frozenset(
    {
        "clip", "clips", "even", "funniest", "funny", "gaming", "guess",
        "moment", "moments", "ranking", "reaction", "short", "shorts", "top",
        "video", "viral",
    }
)
COMPILATION_MARKERS = (
    "best of", "clip compilation", "clips compilation", "compilation",
)
RANKING_MARKERS = (
    "ranking ", "ranked from", "top 3", "top three", "top 5", "top five",
    "top 10", "top ten",
)
REPOST_MARKERS = (
    "all credit goes to", "credit to original", "credits to", "not my clip",
    "not mine", "re-upload", "reupload", "repost",
)
MULTI_SOURCE_MARKERS = (
    "clips from", "different creators", "various creators",
)
NON_GAMING_TITLE_MARKERS = (
    "animal", "celebrity", "cooking", "couple", "cricket", "dance",
    "kpop", "makeup", "prank", "recipe", "school", "trampoline", "unboxing",
)


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


def _normalized_text(value: Any) -> str:
    return " ".join(TOKEN.findall(str(value or "").casefold()))


def _metadata_text(candidate: Mapping[str, Any], *, include_query: bool) -> str:
    tags = candidate.get("tags")
    tag_values = tags if isinstance(tags, list) else []
    values = [
        candidate.get("title"),
        candidate.get("description"),
        *tag_values,
    ]
    if include_query:
        values.append(candidate.get("discovery_query"))
    return " ".join(_normalized_text(value) for value in values if value)


def _contains_phrase(text: str, phrase: str) -> bool:
    normalized = _normalized_text(phrase)
    return bool(normalized and f" {normalized} " in f" {text} ")


def infer_topic(
    candidate_or_title: Mapping[str, Any] | str,
    query: str = "",
) -> str:
    """Infer a maintainable game slug, never a generic title word."""

    if isinstance(candidate_or_title, Mapping):
        candidate = candidate_or_title
    else:
        candidate = {
            "title": candidate_or_title,
            "description": "",
            "tags": [],
            "discovery_query": query,
        }
    aliases = sorted(
        (
            (topic, alias)
            for topic, topic_aliases in GAME_TOPIC_ALIASES.items()
            for alias in topic_aliases
        ),
        key=lambda value: len(_normalized_text(value[1])),
        reverse=True,
    )
    tags = candidate.get("tags")
    sources = [
        candidate.get("title"),
        " ".join(tags) if isinstance(tags, list) else "",
        candidate.get("description"),
        candidate.get("discovery_query"),
    ]
    for source in sources:
        text = _normalized_text(source)
        for topic, alias in aliases:
            if _contains_phrase(text, alias):
                return topic
    return "unknown-gaming"


def normalized_title(title: str) -> str:
    return " ".join(
        token for token in TOKEN.findall(title.casefold())
        if token not in GENERIC_TITLE_WORDS
    )


def near_duplicate_title(first: str, second: str) -> bool:
    left = normalized_title(first)
    right = normalized_title(second)
    if not left or not right:
        return False
    if left == right:
        return True
    left_tokens = set(left.split())
    right_tokens = set(right.split())
    overlap = len(left_tokens & right_tokens) / max(
        1, len(left_tokens | right_tokens)
    )
    return (
        min(len(left_tokens), len(right_tokens)) >= 3
        and (
            overlap >= 0.8
            or SequenceMatcher(None, left, right).ratio() >= 0.9
        )
    )


def evaluate_gaming_relevance(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Return deterministic, inspectable metadata evidence for gaming."""

    category_id = str(candidate.get("category_id") or "")
    metadata_text = _metadata_text(candidate, include_query=False)
    title_text = _normalized_text(candidate.get("title"))
    query_text = _normalized_text(candidate.get("discovery_query"))
    topic = infer_topic(candidate)
    recognized_game = topic != "unknown-gaming" and any(
        _contains_phrase(metadata_text, alias)
        for alias in GAME_TOPIC_ALIASES[topic]
    )
    gameplay_terms = sorted(
        term for term in GAMEPLAY_TERMS
        if _contains_phrase(metadata_text, term)
    )
    title_gameplay_terms = sorted(
        term for term in GAMEPLAY_TERMS
        if _contains_phrase(title_text, term)
    )
    title_topic = infer_topic(
        {
            "title": candidate.get("title"),
            "description": "",
            "tags": [],
            "discovery_query": "",
        }
    )
    non_gaming_title_markers = sorted(
        marker for marker in NON_GAMING_TITLE_MARKERS
        if _contains_phrase(title_text, marker)
    )
    tags = candidate.get("tags")
    tag_values = tags if isinstance(tags, list) else []
    normalized_tags = {
        _normalized_text(tag) for tag in tag_values
        if isinstance(tag, str)
    }
    gaming_tags = sorted(
        tag for tag in normalized_tags
        if tag in GAMING_TAGS
        or any(_contains_phrase(tag, term) for term in GAMEPLAY_TERMS)
    )
    gaming_query = any(
        _contains_phrase(query_text, term)
        for term in ("gaming", "gameplay", "game", "streamer")
    )

    positive: list[str] = []
    penalties: list[str] = []
    score = 0.0
    if category_id == "20":
        positive.append("YouTube category 20 (Gaming).")
        score += 12
    elif category_id:
        penalties.append(f"YouTube category {category_id} is not Gaming.")
        score -= 2
    else:
        penalties.append("YouTube category is unavailable.")
        score -= 1
    if recognized_game:
        positive.append(f"Recognized game metadata maps to {topic}.")
        score += 10
    if gameplay_terms:
        positive.append(
            "Gaming terminology in metadata: "
            + ", ".join(gameplay_terms[:5])
            + "."
        )
        score += min(6, 2 + len(gameplay_terms))
    if gaming_tags:
        positive.append(
            "Gaming-specific tags: " + ", ".join(gaming_tags[:5]) + "."
        )
        score += min(5, 2 + len(gaming_tags))
    if gaming_query and (recognized_game or gameplay_terms or gaming_tags):
        positive.append(
            "Gaming-specific query is corroborated by video metadata."
        )
        score += 2
    elif gaming_query:
        penalties.append(
            "Gaming search query has no corroborating gaming metadata."
        )

    metadata_signal = bool(recognized_game or gameplay_terms or gaming_tags)
    eligible = category_id == "20" or metadata_signal
    exclusion_reasons: list[str] = []
    if category_id == "24" and not metadata_signal:
        eligible = False
        exclusion_reasons.append(
            "Entertainment-category result lacks explicit gaming metadata."
        )
    elif not eligible:
        exclusion_reasons.append(
            "No positive gaming evidence exists outside the discovery query."
        )
    if (
        category_id != "20"
        and non_gaming_title_markers
        and title_topic == "unknown-gaming"
        and not title_gameplay_terms
    ):
        eligible = False
        marker_list = ", ".join(non_gaming_title_markers)
        penalties.append(
            f"Explicit non-gaming title subject: {marker_list}."
        )
        exclusion_reasons.append(
            "Non-Gaming-category title has an explicit unrelated subject; "
            "lower-priority tags or description cannot override it."
        )

    return {
        "version": RELEVANCE_VERSION,
        "eligible": eligible,
        "score": round(max(0.0, score), 4),
        "category_id": category_id or None,
        "topic": topic if eligible else None,
        "positive_evidence": positive,
        "penalties": penalties,
        "exclusion_reasons": exclusion_reasons,
        "evidence": " ".join(
            positive + penalties + exclusion_reasons
        ),
    }


def evaluate_source_quality(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Flag derivative-source risk without pretending to prove authorship."""

    metadata_text = _metadata_text(candidate, include_query=False)
    ranking_markers = sorted(
        marker for marker in RANKING_MARKERS
        if _contains_phrase(metadata_text, marker)
    )
    compilation_markers = sorted(
        marker for marker in COMPILATION_MARKERS
        if _contains_phrase(metadata_text, marker)
    )
    repost_markers = sorted(
        marker for marker in REPOST_MARKERS
        if _contains_phrase(metadata_text, marker)
    )
    multi_source_markers = sorted(
        marker for marker in MULTI_SOURCE_MARKERS
        if _contains_phrase(metadata_text, marker)
    )
    penalties: list[dict[str, Any]] = []
    if ranking_markers:
        penalties.append(
            {
                "points": -6.0,
                "reason": "Ranking-format title may aggregate derivative clips.",
            }
        )
    if compilation_markers:
        penalties.append(
            {
                "points": -10.0,
                "reason": "Compilation wording weakens original-source confidence.",
            }
        )
    if repost_markers:
        penalties.append(
            {
                "points": -20.0,
                "reason": "Explicit repost or third-party credit wording detected.",
            }
        )

    exclusion_reasons: list[str] = []
    if repost_markers:
        exclusion_reasons.append(
            "Explicit repost wording indicates this is not a preferred original upload."
        )
    if ranking_markers and multi_source_markers:
        exclusion_reasons.append(
            "Ranking appears assembled from multiple creators."
        )
    penalty = round(
        sum(float(item["points"]) for item in penalties),
        4,
    )
    return {
        "version": SOURCE_QUALITY_VERSION,
        "eligible": not exclusion_reasons,
        "status": (
            "excluded-derivative"
            if exclusion_reasons
            else "derivative-risk" if penalties else "preferred-original"
        ),
        "penalty": penalty,
        "penalties": penalties,
        "exclusion_reasons": exclusion_reasons,
        "evidence": " ".join(
            [item["reason"] for item in penalties] + exclusion_reasons
        ) or "No compilation, ranking, or repost markers detected.",
    }


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
                        "description": str(snippet.get("description") or ""),
                        "tags": [
                            str(tag) for tag in snippet.get("tags", [])
                            if isinstance(tag, str)
                        ] if isinstance(snippet.get("tags"), list) else [],
                        "creator": str(snippet.get("channelTitle") or ""),
                        "channel_title": str(
                            snippet.get("channelTitle") or ""
                        ),
                        "channel_id": str(snippet.get("channelId") or ""),
                        "category_id": str(snippet.get("categoryId") or ""),
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
        deno_path: Path | str | None = None,
    ) -> None:
        self.media_root = Path(media_root)
        self.ffprobe_path = ffprobe_path
        self.runtime_configuration: YtDlpRuntimeConfiguration = (
            build_ytdlp_runtime_configuration(
                deno_path,
                warning_stacklevel=3,
            )
        )
        self.deno_path = self.runtime_configuration.deno_path

    def _download_options(self, output: Path) -> dict[str, Any]:
        return {
            "format": "bestvideo*+bestaudio/best",
            "merge_output_format": "mp4",
            "outtmpl": str(output),
            "noplaylist": True,
            "quiet": True,
            "overwrites": False,
            **self.runtime_configuration.options(),
        }

    def validate(self, candidate: Mapping[str, Any], *, retain: bool = True) -> MediaEvidence:
        self.media_root.mkdir(parents=True, exist_ok=True)
        video_id = candidate["video_id"]
        output = self.media_root / f"{video_id}.mp4"
        try:
            with yt_dlp.YoutubeDL(
                self._download_options(output)
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
                str(completed.resolve()) if retain else None,
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


COHORT_WEIGHTS: dict[str, dict[str, float]] = {
    "established": {
        "raw_views": 30,
        "views_per_day": 8,
        "like_ratio": 12,
        "comment_ratio": 8,
        "recency": 4,
        "duration": 8,
        "vertical": 12,
        "gaming_relevance": 14,
    },
    "breakout": {
        "raw_views": 10,
        "views_per_day": 30,
        "like_ratio": 12,
        "comment_ratio": 8,
        "recency": 16,
        "duration": 8,
        "vertical": 12,
        "gaming_relevance": 14,
    },
}


def _exclusion(
    candidate: Mapping[str, Any],
    stage: str,
    reason: str,
    *,
    evidence: str = "",
) -> dict[str, Any]:
    return {
        "video_id": candidate.get("video_id"),
        "title": candidate.get("title"),
        "creator": candidate.get("creator"),
        "stage": stage,
        "reason": reason,
        "evidence": evidence or reason,
    }


def qualify_metadata_candidates(
    candidates: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Apply relevance, source-quality, and near-duplicate gates."""

    prepared: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for original in candidates:
        item = copy.deepcopy(dict(original))
        relevance = evaluate_gaming_relevance(item)
        source_quality = evaluate_source_quality(item)
        item["gaming_relevance"] = relevance
        item["source_quality"] = source_quality
        item["topic"] = item.get("topic") or relevance.get("topic")
        item["validation_status"] = "metadata-qualified"
        item["media_verification"] = "provisional"
        item["validation_evidence"] = (
            "Gaming relevance is metadata-qualified; vertical, duration, "
            "video, and audio evidence remain provisional until local media "
            "validation."
        )
        if not relevance["eligible"]:
            excluded.append(
                _exclusion(
                    item,
                    "gaming-relevance",
                    "; ".join(relevance["exclusion_reasons"]),
                    evidence=relevance["evidence"],
                )
            )
            continue
        if not source_quality["eligible"]:
            excluded.append(
                _exclusion(
                    item,
                    "source-quality",
                    "; ".join(source_quality["exclusion_reasons"]),
                    evidence=source_quality["evidence"],
                )
            )
            continue
        prepared.append(item)

    preferred = sorted(
        prepared,
        key=lambda item: (
            -float(item["source_quality"]["penalty"]),
            -int(item.get("view_count") or 0),
            str(item.get("video_id") or ""),
        ),
    )
    unique: list[dict[str, Any]] = []
    for item in preferred:
        duplicate = next(
            (
                kept for kept in unique
                if near_duplicate_title(item.get("title", ""), kept.get("title", ""))
            ),
            None,
        )
        if duplicate is not None:
            excluded.append(
                _exclusion(
                    item,
                    "deduplication",
                    f"Near-duplicate title of {duplicate['video_id']}; "
                    "the higher-confidence source was retained.",
                    evidence=(
                        f"Normalized titles are substantially similar. "
                        f"Retained source status: "
                        f"{duplicate['source_quality']['status']}."
                    ),
                )
            )
            continue
        unique.append(item)
    return unique, excluded


def score_candidate(
    candidate: Mapping[str, Any], *, cohort: str = "established",
    now: datetime | None = None,
) -> tuple[float, dict[str, Any]]:
    if cohort not in COHORT_WEIGHTS:
        raise ValueError(f"Unsupported benchmark cohort {cohort!r}.")
    now = now or datetime.now(timezone.utc)
    weights = COHORT_WEIGHTS[cohort]
    views = max(0, int(candidate.get("view_count") or 0))
    daily = views_per_day(views, candidate["published_at"], now=now)
    likes, comments = candidate.get("like_count"), candidate.get("comment_count")
    duration_value = candidate.get("verified_duration")
    if duration_value is None:
        duration_value = candidate.get("duration")
    duration = float(duration_value or 0)
    vertical = candidate.get("verified_vertical")
    age_days = max(
        (now - _utc(candidate["published_at"])).total_seconds() / 86400,
        1,
    )
    relevance = candidate.get("gaming_relevance")
    if not isinstance(relevance, Mapping):
        relevance = evaluate_gaming_relevance(candidate)
    source_quality = candidate.get("source_quality")
    if not isinstance(source_quality, Mapping):
        source_quality = evaluate_source_quality(candidate)
    components = {
        "raw_views": min(
            weights["raw_views"],
            math.log10(views + 1) / 8 * weights["raw_views"],
        ),
        "views_per_day": min(
            weights["views_per_day"],
            math.log10(daily + 1) / 6 * weights["views_per_day"],
        ),
        "like_ratio": min(
            weights["like_ratio"],
            (
                likes / views * weights["like_ratio"] * 20
                if likes is not None and views else 0
            ),
        ),
        "comment_ratio": min(
            weights["comment_ratio"],
            (
                comments / views * weights["comment_ratio"] * 100
                if comments is not None and views else 0
            ),
        ),
        "recency": max(
            0.0,
            weights["recency"] * (1 - min(age_days, 365) / 365),
        ),
        "duration": (
            weights["duration"]
            if 10 <= duration <= 90
            else weights["duration"] / 2 if 5 <= duration <= 180 else 0.0
        ),
        "vertical": weights["vertical"] if vertical is True else 0.0,
        "gaming_relevance": min(
            weights["gaming_relevance"],
            float(relevance.get("score") or 0)
            / 12
            * weights["gaming_relevance"],
        ),
        "creator_diversity": 0.0,
        "topic_diversity": 0.0,
        "source_quality_penalty": float(
            source_quality.get("penalty") or 0
        ),
        "evidence_penalty": -4.0 * sum(
            value is None for value in (likes, comments, vertical)
        ),
    }
    total = round(max(0.0, sum(components.values())), 4)
    verification = candidate.get("validation_status") or "metadata-qualified"
    explanation = {
        "version": SCORE_VERSION,
        "cohort": cohort,
        "total": total,
        "components": {
            name: round(value, 4) for name, value in components.items()
        },
        "evidence": (
            f"{cohort.title()} cohort; {views:,} views; "
            f"{daily:,.1f} views/day; duration {duration:.1f}s; "
            f"vertical={vertical!r}; validation={verification}. "
            f"Gaming evidence: {relevance.get('evidence') or 'unavailable'} "
            f"Source evidence: {source_quality.get('evidence') or 'unavailable'} "
            "This transparent ranking is not a prediction of virality or "
            "objective quality."
        ),
    }
    return total, explanation


@dataclass(frozen=True)
class BenchmarkSelection:
    selected: list[dict[str, Any]]
    excluded: list[dict[str, Any]]


def select_benchmark_cohorts(
    candidates: Sequence[dict[str, Any]], *,
    established_count: int = 10,
    breakout_count: int = 10,
    max_per_creator: int = 2,
    max_per_topic: int = 3,
    now: datetime | None = None,
) -> BenchmarkSelection:
    """Select both cohorts while sharing creator and topic diversity caps."""

    if min(
        established_count,
        breakout_count,
        max_per_creator,
        max_per_topic,
    ) < 0 or max_per_creator < 1 or max_per_topic < 1:
        raise ValueError("Cohort counts must be nonnegative and caps positive.")
    cohort_names = ("established", "breakout")
    ranked: dict[str, list[dict[str, Any]]] = {}
    for cohort in cohort_names:
        values = []
        for original in candidates:
            item = copy.deepcopy(original)
            item["score"], item["ranking"] = score_candidate(
                item,
                cohort=cohort,
                now=now,
            )
            values.append(item)
        ranked[cohort] = sorted(
            values,
            key=lambda item: (-item["score"], item["video_id"]),
        )

    quotas = {
        "established": established_count,
        "breakout": breakout_count,
    }
    creators: dict[str, int] = {}
    topics: dict[str, int] = {}
    selected_ids: set[str] = set()
    selected: list[dict[str, Any]] = []
    progress = True
    while progress and any(quotas.values()):
        progress = False
        for cohort in cohort_names:
            if quotas[cohort] <= 0:
                continue
            item = next(
                (
                    value for value in ranked[cohort]
                    if value["video_id"] not in selected_ids
                    and creators.get(value["creator"].casefold(), 0)
                    < max_per_creator
                    and topics.get(value["topic"], 0) < max_per_topic
                ),
                None,
            )
            if item is None:
                continue
            creator = item["creator"].casefold()
            topic = item["topic"]
            creator_bonus = 2.0 if not creators.get(creator) else 0.0
            topic_bonus = 2.0 if not topics.get(topic) else 0.0
            item["ranking"]["components"]["creator_diversity"] = creator_bonus
            item["ranking"]["components"]["topic_diversity"] = topic_bonus
            item["score"] = round(
                item["score"] + creator_bonus + topic_bonus,
                4,
            )
            item["ranking"]["total"] = item["score"]
            item["cohort"] = cohort
            selected_ids.add(item["video_id"])
            creators[creator] = creators.get(creator, 0) + 1
            topics[topic] = topics.get(topic, 0) + 1
            quotas[cohort] -= 1
            selected.append(item)
            progress = True

    for rank, item in enumerate(selected, 1):
        item["rank"] = rank

    excluded = []
    for item in candidates:
        if item["video_id"] in selected_ids:
            continue
        creator = item["creator"].casefold()
        topic = item["topic"]
        if creators.get(creator, 0) >= max_per_creator:
            reason = f"Creator diversity cap of {max_per_creator} was reached."
        elif topics.get(topic, 0) >= max_per_topic:
            reason = f"Topic diversity cap of {max_per_topic} was reached."
        else:
            reason = "Candidate ranked below the available cohort slots."
        excluded.append(_exclusion(item, "benchmark-selection", reason))
    return BenchmarkSelection(selected, excluded)


def _validation_pool(
    candidates: Sequence[dict[str, Any]], *,
    limit: int,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    ranked: dict[str, list[dict[str, Any]]] = {}
    for cohort in ("established", "breakout"):
        values = []
        for item in candidates:
            score, _ranking = score_candidate(item, cohort=cohort, now=now)
            values.append((score, item))
        ranked[cohort] = [
            item for _score, item in sorted(
                values,
                key=lambda value: (-value[0], value[1]["video_id"]),
            )
        ]
    selected: list[dict[str, Any]] = []
    ids: set[str] = set()
    offset = 0
    while len(selected) < limit:
        progress = False
        for cohort in ("established", "breakout"):
            if offset >= len(ranked[cohort]):
                continue
            item = ranked[cohort][offset]
            if item["video_id"] not in ids:
                selected.append(item)
                ids.add(item["video_id"])
                if len(selected) >= limit:
                    break
            progress = True
        if not progress:
            break
        offset += 1
    return selected


def resolve_cohort_counts(
    *, target_count: int | None,
    established_count: int | None,
    breakout_count: int | None,
) -> tuple[int, int]:
    if established_count is not None or breakout_count is not None:
        if target_count is not None:
            raise ValueError(
                "Use either target_count or explicit cohort counts, not both."
            )
        established = (
            10 if established_count is None else established_count
        )
        breakout = 10 if breakout_count is None else breakout_count
    else:
        total = 20 if target_count is None else target_count
        established = (total + 1) // 2
        breakout = total // 2
    if established < 0 or breakout < 0 or established + breakout < 1:
        raise ValueError("At least one nonnegative cohort count is required.")
    return established, breakout


def select_diverse(
    candidates: Sequence[dict[str, Any]], *, count: int = 20,
    max_per_creator: int = 2, max_per_topic: int = 3,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Compatibility wrapper for balanced established/breakout selection."""

    qualified, _excluded = qualify_metadata_candidates(candidates)
    established, breakout = resolve_cohort_counts(
        target_count=count,
        established_count=None,
        breakout_count=None,
    )
    return select_benchmark_cohorts(
        qualified,
        established_count=established,
        breakout_count=breakout,
        max_per_creator=max_per_creator,
        max_per_topic=max_per_topic,
        now=now,
    ).selected


class ReferenceCandidateQueue:
    def __init__(
        self, path: Path = DEFAULT_QUEUE_PATH, *,
        data_root: Path | None = None,
    ) -> None:
        self.path = Path(path)
        self.data_root = (
            Path(data_root)
            if data_root is not None
            else self._infer_data_root(self.path)
        )
        self.media_root = self.data_root / REFERENCE_MEDIA_RELATIVE_ROOT
        if self.path.exists():
            self._load()

    @staticmethod
    def _infer_data_root(queue_path: Path) -> Path:
        parent = queue_path.parent
        if parent.name == "reference_discovery":
            return parent.parent
        return parent

    @staticmethod
    def empty() -> dict[str, Any]:
        return {"version": QUEUE_VERSION, "updated_at": utc_now(), "items": []}

    @staticmethod
    def revision(item: Mapping[str, Any]) -> int:
        value = item.get("revision", 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ReferenceDiscoveryError(
                f"Reference candidate {item.get('video_id', '<unknown>')} "
                "has an invalid revision."
            )
        return value

    @classmethod
    def _public_item(cls, item: Mapping[str, Any]) -> dict[str, Any]:
        result = copy.deepcopy(dict(item))
        result["revision"] = cls.revision(item)
        return result

    @staticmethod
    def _canonical_relative_path(filename: str) -> str:
        return (REFERENCE_MEDIA_RELATIVE_ROOT / filename).as_posix()

    @staticmethod
    def _relative_media_filename(value: str) -> str | None:
        path = Path(value)
        if path.is_absolute() or path.parts[:2] != (
            "reference_discovery", "media"
        ):
            return None
        if len(path.parts) != 3 or path.name in {"", ".", ".."}:
            return None
        return path.name

    def _legacy_media_location(self, path: Path) -> Path | None:
        """Map known release/current/development paths into persistent data."""

        parts = path.parts
        for index, part in enumerate(parts):
            if part == "clip-factory-production":
                tail_start: int | None = None
                if (
                    index + 2 < len(parts)
                    and parts[index + 1] == "current"
                    and parts[index + 2] == "data"
                ):
                    tail_start = index + 3
                elif (
                    index + 3 < len(parts)
                    and parts[index + 1] == "releases"
                    and re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", parts[index + 2])
                    and parts[index + 3] == "data"
                ):
                    tail_start = index + 4
                if tail_start is not None:
                    relative = Path(*parts[tail_start:])
                    filename = self._relative_media_filename(relative.as_posix())
                    if filename is not None:
                        return self.media_root / filename
            if (
                part == "clip-factory"
                and index + 1 < len(parts)
                and parts[index + 1] == "data"
            ):
                relative = Path(*parts[index + 2:])
                filename = self._relative_media_filename(relative.as_posix())
                if filename is not None:
                    return self.media_root / filename
        return None

    def _media_location(self, value: str) -> tuple[Path, str]:
        filename = self._relative_media_filename(value)
        if filename is not None:
            return (
                self.media_root / filename,
                self._canonical_relative_path(filename),
            )
        path = Path(value)
        if not path.is_absolute():
            raise ReferenceDiscoveryError(
                f"Candidate media path {value!r} is not a canonical path under "
                f"{REFERENCE_MEDIA_RELATIVE_ROOT.as_posix()}/."
            )
        resolved_root = self.media_root.resolve()
        resolved_path = path.resolve()
        if resolved_path.is_relative_to(resolved_root):
            relative = resolved_path.relative_to(resolved_root)
            if len(relative.parts) == 1:
                return (
                    self.media_root / relative.name,
                    self._canonical_relative_path(relative.name),
                )
        legacy = self._legacy_media_location(path)
        if legacy is not None:
            return legacy, self._canonical_relative_path(legacy.name)
        raise ReferenceDiscoveryError(
            f"Candidate media path {value!r} is outside the configured "
            f"persistent media directory {self.media_root}."
        )

    def resolve_media_path(
        self, value: str, *, require_exists: bool = True,
    ) -> Path:
        path, _canonical = self._media_location(value)
        if require_exists and not path.is_file():
            raise ReferenceDiscoveryError(
                f"Candidate media file is missing at {path}. Queue state was "
                "not changed; restore the retained file before retrying."
            )
        resolved = path.resolve()
        resolved_root = self.media_root.resolve()
        if not resolved.is_relative_to(resolved_root):
            raise ReferenceDiscoveryError(
                f"Candidate media path {value!r} resolves outside the configured "
                f"persistent media directory {self.media_root}."
            )
        return resolved

    def canonical_media_path(self, value: str) -> str:
        _path, canonical = self._media_location(value)
        self.resolve_media_path(value)
        return canonical

    def validate_media_paths(self, *, require_canonical: bool = True) -> None:
        problems = []
        for item in self._load()["items"]:
            value = item.get("media_path")
            if value is None:
                continue
            if not isinstance(value, str) or not value:
                problems.append(
                    f"{item.get('video_id', '<unknown>')}: media_path is malformed"
                )
                continue
            try:
                canonical = self.canonical_media_path(value)
            except ReferenceDiscoveryError as error:
                problems.append(f"{item['video_id']}: {error}")
                continue
            if require_canonical and value != canonical:
                problems.append(
                    f"{item['video_id']}: noncanonical media_path {value!r}; "
                    "run migrate-media-paths"
                )
        if problems:
            raise ReferenceDiscoveryError(
                "Reference candidate media validation failed: "
                + "; ".join(problems)
            )

    def migrate_media_paths(self, *, dry_run: bool = False) -> dict[str, Any]:
        document = self._load()
        changes = []
        for item in document["items"]:
            value = item.get("media_path")
            if value is None:
                continue
            if not isinstance(value, str) or not value:
                raise ReferenceDiscoveryError(
                    f"Reference candidate {item.get('video_id', '<unknown>')} "
                    "has a malformed media_path; queue state was not changed."
                )
            canonical = self.canonical_media_path(value)
            if value != canonical:
                changes.append(
                    {
                        "video_id": item["video_id"],
                        "previous_media_path": value,
                        "media_path": canonical,
                    }
                )
                item["media_path"] = canonical
        if changes and not dry_run:
            _atomic_json(self.path, document)
        return {
            "dry_run": dry_run,
            "changed_count": len(changes),
            "changes": changes,
        }

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
            self.revision(item)
            ids.add(item["video_id"])
        return document

    def list(self, *, status: str | None = None) -> list[dict[str, Any]]:
        if status is not None and status not in STATUSES:
            raise ReferenceDiscoveryError(f"Unsupported candidate status {status!r}.")
        return [
            self._public_item(item) for item in self._load()["items"]
            if status is None or item["status"] == status
        ]

    def get(self, video_id: str) -> dict[str, Any]:
        for item in self._load()["items"]:
            if item["video_id"] == video_id:
                return self._public_item(item)
        raise ReferenceDiscoveryError(f"Reference candidate {video_id!r} does not exist.")

    def snapshot(self) -> dict[str, Any]:
        return copy.deepcopy(self._load())

    def restore(self, document: dict[str, Any]) -> None:
        restored = copy.deepcopy(document)
        for item in restored.get("items", []):
            self.revision(item)
        _atomic_json(self.path, restored)

    def transition(
        self,
        video_id: str,
        *,
        expected_revision: int,
        expected_status: str,
        status: str,
        notes: str | None = None,
        category: str | None = None,
        topic: str | None = None,
        accepted_reference_id: str | None = None,
        clear_accepted_reference: bool = False,
    ) -> dict[str, Any]:
        if status not in STATUSES or expected_status not in STATUSES:
            raise ReferenceDiscoveryError("Candidate transition status is invalid.")
        document = self._load()
        for item in document["items"]:
            if item["video_id"] != video_id:
                continue
            revision = self.revision(item)
            if revision != expected_revision:
                raise ReferenceDiscoveryError(
                    f"Stale candidate form: expected revision {expected_revision}, "
                    f"current revision is {revision}. Refresh and retry."
                )
            if item["status"] != expected_status:
                raise ReferenceDiscoveryError(
                    f"Candidate {video_id} is {item['status']}, not "
                    f"{expected_status}; refresh and retry."
                )
            item["status"] = status
            if notes is not None:
                item["notes"] = notes
            if category is not None:
                item["category"] = category
            if topic is not None:
                item["topic"] = topic
                item["topic_manually_corrected"] = True
            if clear_accepted_reference:
                item["accepted_reference_id"] = None
            elif accepted_reference_id is not None:
                item["accepted_reference_id"] = accepted_reference_id
            item["revision"] = revision + 1
            item["updated_at"] = utc_now()
            document["updated_at"] = item["updated_at"]
            _atomic_json(self.path, document)
            return self._public_item(item)
        raise ReferenceDiscoveryError(
            f"Reference candidate {video_id!r} does not exist."
        )

    def upsert_discovered(self, candidates: Sequence[dict[str, Any]]) -> None:
        document = self._load()
        existing = {item["video_id"]: item for item in document["items"]}
        now = utc_now()
        for candidate in candidates:
            previous = existing.get(candidate["video_id"])
            if previous and previous["status"] != "discovered":
                continue
            candidate_copy = copy.deepcopy(candidate)
            media_path = candidate_copy.get("media_path")
            if isinstance(media_path, str):
                candidate_copy["media_path"] = self.canonical_media_path(
                    media_path
                )
            item = {
                **candidate_copy,
                "status": "discovered",
                "notes": (previous or {}).get("notes"),
                "category": (previous or {}).get("category", "gaming_highlight"),
                "topic": (
                    previous.get("topic")
                    if previous
                    and previous.get("topic_manually_corrected")
                    else candidate.get("topic")
                ),
                "topic_manually_corrected": (previous or {}).get(
                    "topic_manually_corrected",
                    False,
                ),
                "accepted_reference_id": (previous or {}).get("accepted_reference_id"),
                "created_at": (previous or {}).get("created_at", now),
                "updated_at": now,
                "origin": "automatic_youtube_discovery",
                "revision": (
                    self.revision(previous) + 1 if previous is not None else 0
                ),
            }
            existing[item["video_id"]] = item
        document["items"] = sorted(existing.values(), key=lambda item: item["video_id"])
        document["updated_at"] = now
        _atomic_json(self.path, document)

    def decide(
        self, video_id: str, status: str, *, notes: str | None = None,
        category: str | None = None, topic: str | None = None,
        accepted_reference_id: str | None = None,
    ) -> dict[str, Any]:
        """Update non-accepted compatibility state.

        Interactive and CLI decisions must use ReferenceDiscoveryService so
        revisions, transition rules, and audit events are enforced.
        """

        if status not in STATUSES:
            raise ReferenceDiscoveryError(f"Unsupported candidate status {status!r}.")
        document = self._load()
        for item in document["items"]:
            if item["video_id"] == video_id:
                if (
                    (item["status"] == "accepted") != (status == "accepted")
                ):
                    raise ReferenceDiscoveryError(
                        "Transitions into or out of accepted require the "
                        "reference decision service."
                    )
                item["status"] = status
                if notes is not None:
                    item["notes"] = notes
                if category is not None:
                    item["category"] = category
                if topic is not None:
                    item["topic"] = topic
                    item["topic_manually_corrected"] = True
                if accepted_reference_id is not None:
                    item["accepted_reference_id"] = accepted_reference_id
                item["revision"] = self.revision(item) + 1
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
            "topic_manually_corrected",
        }
        for item in document["items"]:
            update = updates.get(item["video_id"])
            if update is None:
                continue
            for name, value in update.items():
                if (
                    name not in protected
                    and not (
                        name == "topic"
                        and item.get("topic_manually_corrected")
                    )
                ):
                    item[name] = copy.deepcopy(value)
            item["revision"] = self.revision(item) + 1
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
        profile_root: Path | None = None,
        audit_ledger: ReferenceDecisionAuditLedger | None = None,
        decision_lock_path: Path | None = None,
        withdrawal_recovery_root: Path | None = None,
        reviewer_name: str | None = None,
        move_path: Callable[[Path, Path], None] = os.replace,
    ) -> None:
        self.api = api
        self.queue = queue
        self.media_validator = media_validator or LocalMediaValidator()
        self.reference_library = reference_library or ReferenceClipLibrary()
        self.analyzer_factory = analyzer_factory
        self.reference_root = Path(reference_root)
        self.profile_root = (
            Path(profile_root)
            if profile_root is not None
            else self.reference_root.parent / "reference_profiles"
        )
        self.audit_ledger = audit_ledger or ReferenceDecisionAuditLedger(
            self.queue.path.parent / DEFAULT_AUDIT_PATH.name
        )
        self.decision_lock_path = Path(
            decision_lock_path
            if decision_lock_path is not None
            else self.queue.path.parent / DEFAULT_DECISION_LOCK.name
        )
        self.withdrawal_recovery_root = Path(
            withdrawal_recovery_root
            if withdrawal_recovery_root is not None
            else self.queue.path.parent / DEFAULT_WITHDRAWAL_RECOVERY_ROOT.name
        )
        self.reviewer_name = configured_reviewer_name(reviewer_name)
        self.move_path = move_path

    def discover(
        self, *, target_count: int | None = None, pool_size: int = 100,
        established_count: int | None = None,
        breakout_count: int | None = None,
        publication_days: int = 365, region: str = "US",
        max_per_creator: int = 2, max_per_topic: int = 3,
        queries: Sequence[str] = DEFAULT_QUERIES, dry_run: bool = False,
        retain_media: str = "selected",
    ) -> dict[str, Any]:
        established, breakout = resolve_cohort_counts(
            target_count=target_count,
            established_count=established_count,
            breakout_count=breakout_count,
        )
        total_count = established + breakout
        after = (
            datetime.now(timezone.utc) - timedelta(days=publication_days)
        ).isoformat().replace("+00:00", "Z")
        search = self.api.search(
            queries, pool_size=pool_size, published_after=after, region=region
        )
        hydrated = self.api.hydrate(search)
        qualified, metadata_exclusions = qualify_metadata_candidates(
            [
                {
                    **item,
                    "verified_vertical": None,
                    "verified_duration": None,
                }
                for item in hydrated
            ]
        )
        planned = select_benchmark_cohorts(
            qualified,
            established_count=established,
            breakout_count=breakout,
            max_per_creator=max_per_creator,
            max_per_topic=max_per_topic,
        )
        if dry_run:
            return {
                "dry_run": True,
                "pool_count": len(hydrated),
                "metadata_qualified_count": len(qualified),
                "planned_count": len(planned.selected),
                "cohorts": {
                    "established": sum(
                        item["cohort"] == "established"
                        for item in planned.selected
                    ),
                    "breakout": sum(
                        item["cohort"] == "breakout"
                        for item in planned.selected
                    ),
                },
                "validation_summary": {
                    "metadata_qualified": len(qualified),
                    "media_verified": 0,
                    "rejected_during_media_validation": 0,
                    "media_verification": "provisional",
                },
                "selected": planned.selected,
                "exclusions": metadata_exclusions + planned.excluded,
            }
        prelim = _validation_pool(
            qualified,
            limit=max(total_count * 3, total_count),
        )
        shortlist_ids = {item["video_id"] for item in prelim}
        shortlist_exclusions = [
            _exclusion(
                item,
                "media-validation-shortlist",
                "Candidate ranked below the bounded media-validation shortlist.",
            )
            for item in qualified
            if item["video_id"] not in shortlist_ids
        ]
        verified = []
        media_exclusions = []
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
                "validation_status": (
                    "media-verified"
                    if evidence.valid_short
                    else "rejected during media validation"
                ),
                "media_verification": (
                    "verified" if evidence.valid_short else "rejected"
                ),
                "validation_evidence": (
                    "Local media contains video and audio, is vertical, and "
                    "has a 5–180 second duration."
                    if evidence.valid_short
                    else (
                        "Local media validation rejected the candidate: "
                        f"{evidence.error or 'required media evidence was absent'}."
                    )
                ),
            }
            if evidence.valid_short:
                verified.append(enriched)
            else:
                exclusion = _exclusion(
                    enriched,
                    "media-validation",
                    "Rejected during media validation.",
                    evidence=enriched["validation_evidence"],
                )
                exclusion["validation_status"] = (
                    "rejected during media validation"
                )
                media_exclusions.append(exclusion)
        selected = select_benchmark_cohorts(
            verified,
            established_count=established,
            breakout_count=breakout,
            max_per_creator=max_per_creator,
            max_per_topic=max_per_topic,
        )
        if retain_media == "selected":
            selected_ids = {item["video_id"] for item in selected.selected}
            for item in verified:
                if item["video_id"] not in selected_ids and item.get("media_path"):
                    Path(item["media_path"]).unlink(missing_ok=True)
        self.queue.upsert_discovered(selected.selected)
        return {
            "dry_run": False,
            "pool_count": len(hydrated),
            "metadata_qualified_count": len(qualified),
            "verified_count": len(verified),
            "selected_count": len(selected.selected),
            "cohorts": {
                "established": sum(
                    item["cohort"] == "established"
                    for item in selected.selected
                ),
                "breakout": sum(
                    item["cohort"] == "breakout"
                    for item in selected.selected
                ),
            },
            "validation_summary": {
                "metadata_qualified": len(qualified),
                "media_verified": len(verified),
                "rejected_during_media_validation": len(media_exclusions),
                "media_verification": "complete",
            },
            "selected": selected.selected,
            "exclusions": (
                metadata_exclusions
                + shortlist_exclusions
                + media_exclusions
                + selected.excluded
            ),
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
            updated["gaming_relevance"] = evaluate_gaming_relevance(updated)
            updated["source_quality"] = evaluate_source_quality(updated)
            if not candidate.get("topic_manually_corrected"):
                updated["topic"] = (
                    updated["gaming_relevance"].get("topic")
                    or "unknown-gaming"
                )
            updated["score"], updated["ranking"] = score_candidate(
                updated,
                cohort=updated.get("cohort") or "established",
            )
            refreshed.append(updated)
        self.queue.refresh_metadata(refreshed)
        return len(refreshed)

    @contextmanager
    def _decision_lock(self):
        self.decision_lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.decision_lock_path.open("a+", encoding="utf-8") as stream:
            os.chmod(self.decision_lock_path, 0o600)
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def legal_actions(status: str) -> frozenset[str]:
        try:
            return LEGAL_ACTIONS[status]
        except KeyError as error:
            raise ReferenceDiscoveryError(
                f"Unsupported candidate status {status!r}."
            ) from error

    @staticmethod
    def _expected_revision(
        candidate: Mapping[str, Any], expected_revision: int | None
    ) -> int:
        current = ReferenceCandidateQueue.revision(candidate)
        if expected_revision is None:
            return current
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 0
        ):
            raise ReferenceDiscoveryError("Expected candidate revision is invalid.")
        if expected_revision != current:
            raise ReferenceDiscoveryError(
                f"Stale candidate form: expected revision {expected_revision}, "
                f"current revision is {current}. Refresh and retry."
            )
        return current

    @staticmethod
    def _decision_note(
        notes: str | None, *, required: bool
    ) -> str:
        if notes is None:
            value = ""
        elif not isinstance(notes, str):
            raise ReferenceDiscoveryError("Reviewer note must be text.")
        else:
            value = notes.strip()
        if len(value) > 4_000:
            raise ReferenceDiscoveryError(
                "Reviewer notes are limited to 4000 characters."
            )
        if required and not value:
            raise ReferenceDiscoveryError(
                "A meaningful reviewer note is required for this action."
            )
        return value

    def _event(
        self,
        *,
        candidate: Mapping[str, Any],
        action: str,
        requested_status: str,
        resulting_status: str,
        resulting_revision: int,
        accepted_reference_id_after: str | None,
        result: str,
        note: str,
        request_id: str | None,
        failure_reason: str | None = None,
        recovery_key: str | None = None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        return self.audit_ledger.event(
            video_id=str(candidate["video_id"]),
            action=action,
            previous_status=str(candidate["status"]),
            requested_status=requested_status,
            resulting_status=resulting_status,
            previous_revision=self.queue.revision(candidate),
            resulting_revision=resulting_revision,
            accepted_reference_id_before=candidate.get(
                "accepted_reference_id"
            ),
            accepted_reference_id_after=accepted_reference_id_after,
            result=result,
            failure_reason=failure_reason,
            reviewer=self.reviewer_name,
            note=note,
            request_id=request_id,
            recovery_key=recovery_key,
            event_id=event_id,
        )

    def _record_operational_failure(
        self,
        *,
        candidate: Mapping[str, Any],
        action: str,
        requested_status: str,
        note: str,
        request_id: str | None,
        error: Exception,
    ) -> None:
        if isinstance(error, ReferenceDecisionAuditError):
            return
        try:
            self.audit_ledger.append(
                self._event(
                    candidate=candidate,
                    action=action,
                    requested_status=requested_status,
                    resulting_status=str(candidate["status"]),
                    resulting_revision=self.queue.revision(candidate),
                    accepted_reference_id_after=candidate.get(
                        "accepted_reference_id"
                    ),
                    result="failure",
                    note=note,
                    request_id=request_id,
                    failure_reason=str(error),
                )
            )
        except ReferenceDecisionAuditError:
            pass

    def _require_unused_request(self, request_id: str | None) -> None:
        if request_id is None:
            return
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id):
            raise ReferenceDiscoveryError(
                "Decision request ID is invalid; refresh and retry."
            )
        if any(
            event.get("request_id") == request_id
            for event in self.audit_ledger.history()
        ):
            raise ReferenceDiscoveryError(
                "This decision request was already processed; refresh before "
                "submitting another action."
            )

    def history(
        self, video_id: str, *, limit: int | None = None
    ) -> list[dict[str, Any]]:
        self.queue.get(video_id)
        return self.audit_ledger.history(video_id, limit=limit)

    def transition(
        self,
        video_id: str,
        action: str,
        *,
        notes: str | None,
        expected_revision: int | None,
        category: str | None = None,
        topic: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        targets = {
            "reject": "rejected",
            "duplicate": "duplicate",
            "reconsider": "discovered",
        }
        if action not in targets:
            raise ReferenceDiscoveryError(
                f"Unsupported reference decision action {action!r}."
            )
        note = self._decision_note(
            notes, required=action in {"reject", "duplicate"}
        )
        target = targets[action]
        with self._decision_lock():
            self._require_unused_request(request_id)
            candidate = self.queue.get(video_id)
            revision = self._expected_revision(candidate, expected_revision)
            if action not in self.legal_actions(candidate["status"]):
                instruction = (
                    " Use Withdraw Reference for an accepted candidate."
                    if candidate["status"] == "accepted"
                    else " Refresh the review page and use a legal action."
                )
                raise ReferenceDiscoveryError(
                    f"Cannot {action} a {candidate['status']} candidate."
                    + instruction
                )
            queue_snapshot = self.queue.snapshot()
            try:
                updated = self.queue.transition(
                    video_id,
                    expected_revision=revision,
                    expected_status=candidate["status"],
                    status=target,
                    notes=(
                        note
                        if action != "reconsider" or note
                        else None
                    ),
                    category=category,
                    topic=topic,
                )
                self.audit_ledger.append(
                    self._event(
                        candidate=candidate,
                        action=action,
                        requested_status=target,
                        resulting_status=updated["status"],
                        resulting_revision=updated["revision"],
                        accepted_reference_id_after=updated.get(
                            "accepted_reference_id"
                        ),
                        result="success",
                        note=note,
                        request_id=request_id,
                    )
                )
                return updated
            except Exception as error:
                self.queue.restore(queue_snapshot)
                self._record_operational_failure(
                    candidate=candidate,
                    action=action,
                    requested_status=target,
                    note=note,
                    request_id=request_id,
                    error=error,
                )
                raise

    def accept(
        self, video_id: str, *, category: str, notes: str,
        transcription: bool = False, topic: str | None = None,
        expected_revision: int | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        note = self._decision_note(notes, required=False)
        with self._decision_lock():
            self._require_unused_request(request_id)
            candidate = self.queue.get(video_id)
            revision = self._expected_revision(candidate, expected_revision)
            if "accept" not in self.legal_actions(candidate["status"]):
                raise ReferenceDiscoveryError(
                    f"Cannot accept a {candidate['status']} candidate. "
                    "Only discovered candidates can be accepted."
                )
            if topic is not None:
                candidate["topic"] = topic
                candidate["topic_manually_corrected"] = True
            media_path = candidate.get("media_path")
            if isinstance(media_path, str):
                try:
                    resolved_media_path = self.queue.resolve_media_path(
                        media_path
                    )
                except ReferenceDiscoveryError as error:
                    raise ReferenceDiscoveryError(
                        f"Candidate media is unavailable: {error}"
                    ) from error
            else:
                evidence = self.media_validator.validate(candidate, retain=True)
                if not evidence.valid_short or not evidence.media_path:
                    raise ReferenceDiscoveryError(
                        "Candidate media could not be validated: "
                        f"{evidence.error or 'invalid media'}."
                    )
                resolved_media_path = Path(evidence.media_path)
            reference_id = f"youtube-{video_id}"
            directory = self.reference_root / f"discovered-{video_id}"
            if directory.exists():
                raise ReferenceDiscoveryError(
                    f"Accepted-reference directory already exists for {video_id}; "
                    "inspect it before retrying."
                )
            queue_snapshot = self.queue.snapshot()
            index_existed = self.reference_library.index_path.exists()
            index_snapshot = self.reference_library.snapshot_index()
            directory.mkdir(parents=True)
            target_media = directory / "reference.mp4"
            baseline_path = directory / "baseline.json"
            source_path = directory / "reference.info.json"
            try:
                if resolved_media_path.resolve() != target_media.resolve():
                    _copy_atomic(resolved_media_path, target_media)
                baseline = {
                    "version": 1, "reference_id": reference_id,
                    "source_video_id": video_id,
                    "source_title": candidate["title"],
                    "creator": candidate["creator"], "status": "accepted",
                    "purpose": "creatorflow_baseline",
                    "profile_name": category,
                    "qualities": [
                        "human-accepted discovered benchmark candidate"
                    ],
                    "layout": {
                        "orientation": "vertical", "composition": "unknown",
                        "top_region": "unknown", "bottom_region": "unknown",
                        "facecam_prominence": "unknown",
                    },
                    "story_structure": {
                        "opening_style": "unknown",
                        "setup_requirement": "unknown",
                        "primary_focus": "unknown",
                        "payoff_type": "unknown",
                        "payoff_required": False,
                        "ending_style": "unknown",
                    },
                    "timing_preferences": {
                        "requires_long_lead_in": False,
                        "requires_complete_setup": False,
                        "requires_complete_payoff": False,
                        "preferred_ending": "human review required",
                    },
                    "notes": note or (
                        "Accepted from automatic YouTube benchmark discovery."
                    ),
                }
                _atomic_json(baseline_path, baseline)
                _atomic_json(
                    source_path,
                    {
                        "origin": "automatic_youtube_discovery",
                        "metadata_snapshot": candidate,
                    },
                )
                entry = self.reference_library.register(
                    media_path=target_media,
                    baseline_path=baseline_path,
                    source_info_path=source_path,
                    reference_id=reference_id,
                    profile_name=category,
                )
                self.analyzer_factory(self.reference_library).analyze(
                    reference_id, transcription=transcription
                )
                updated = self.queue.transition(
                    video_id,
                    expected_revision=revision,
                    expected_status="discovered",
                    status="accepted",
                    notes=note if note else None,
                    category=category,
                    topic=topic,
                    accepted_reference_id=reference_id,
                )
                self.audit_ledger.append(
                    self._event(
                        candidate=candidate,
                        action="accept",
                        requested_status="accepted",
                        resulting_status="accepted",
                        resulting_revision=updated["revision"],
                        accepted_reference_id_after=reference_id,
                        result="success",
                        note=note,
                        request_id=request_id,
                    )
                )
                return entry
            except Exception as error:
                self.queue.restore(queue_snapshot)
                if index_existed:
                    self.reference_library.restore_index(index_snapshot)
                else:
                    self.reference_library.index_path.unlink(missing_ok=True)
                for path in (
                    directory / "analysis.json",
                    source_path,
                    baseline_path,
                    target_media,
                ):
                    path.unlink(missing_ok=True)
                try:
                    directory.rmdir()
                except OSError:
                    pass
                self._record_operational_failure(
                    candidate=candidate,
                    action="accept",
                    requested_status="accepted",
                    note=note,
                    request_id=request_id,
                    error=error,
                )
                raise

    def _profiles_using(self, reference_id: str) -> list[Path]:
        profiles = []
        if not self.profile_root.exists():
            return profiles
        for path in sorted(self.profile_root.glob("*.json")):
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise ReferenceDiscoveryError(
                    f"Cannot verify profile {path}: {error}."
                ) from error
            ids = document.get("reference_ids")
            if not isinstance(ids, list) or not all(
                isinstance(value, str) for value in ids
            ):
                raise ReferenceDiscoveryError(
                    f"Cannot verify malformed reference profile {path}."
                )
            if reference_id in ids:
                profiles.append(path)
        return profiles

    def _withdrawal_artifacts(
        self, candidate: Mapping[str, Any]
    ) -> tuple[str, dict[str, Any], Path]:
        video_id = str(candidate["video_id"])
        expected_reference_id = f"youtube-{video_id}"
        reference_id = candidate.get("accepted_reference_id")
        if reference_id != expected_reference_id:
            raise ReferenceDiscoveryError(
                f"Candidate {video_id} does not own expected reference "
                f"{expected_reference_id}; withdrawal was refused."
            )
        try:
            entry = self.reference_library.get(reference_id)
        except ReferenceClipError as error:
            raise ReferenceDiscoveryError(
                f"Accepted reference {reference_id} is missing from the strict "
                "index; repair the inconsistency before withdrawal."
            ) from error
        if entry["profile_name"] != candidate.get("category"):
            raise ReferenceDiscoveryError(
                f"Candidate category {candidate.get('category')!r} does not "
                f"match indexed profile {entry['profile_name']!r}."
            )
        logical_directory = (
            self.reference_root / f"discovered-{video_id}"
        )
        reference_root = self.reference_root.resolve()
        if logical_directory.is_symlink():
            raise ReferenceDiscoveryError(
                f"Reference {reference_id} directory is a symbolic link; "
                "withdrawal was refused."
            )
        directory = logical_directory.resolve()
        if directory.parent != reference_root:
            raise ReferenceDiscoveryError(
                f"Reference {reference_id} directory is outside the configured "
                "reference root; withdrawal was refused."
            )
        expected_directory = Path(entry["media_path"]).resolve().parent
        if expected_directory != directory:
            raise ReferenceDiscoveryError(
                f"Reference {reference_id} is not stored in its expected "
                "discovered-candidate directory; withdrawal was refused."
            )
        expected_artifacts = {
            "media_path": directory / "reference.mp4",
            "baseline_path": directory / "baseline.json",
            "analysis_path": directory / "analysis.json",
            "source_info_path": directory / "reference.info.json",
        }
        paths = {
            name: Path(entry[name]).resolve()
            for name in expected_artifacts
            if entry.get(name)
        }
        if set(paths) != set(expected_artifacts) or any(
            path != expected_artifacts[name]
            or path.parent != directory
            or not path.is_file()
            for name, path in paths.items()
        ):
            raise ReferenceDiscoveryError(
                f"Reference {reference_id} has missing or unexpected artifacts; "
                "withdrawal was refused."
            )
        baseline = load_and_validate_baseline(Path(entry["baseline_path"]))
        if (
            baseline["reference_id"] != reference_id
            or baseline.get("source_video_id") != video_id
            or baseline["profile_name"] != entry["profile_name"]
        ):
            raise ReferenceDiscoveryError(
                f"Reference {reference_id} does not belong to candidate "
                f"{video_id}; withdrawal was refused."
            )
        try:
            self.reference_library.validate_checksum(reference_id)
        except ReferenceClipError as error:
            raise ReferenceDiscoveryError(
                f"Reference checksum validation failed: {error}"
            ) from error
        media_value = candidate.get("media_path")
        if not isinstance(media_value, str):
            raise ReferenceDiscoveryError(
                "Retained discovery media is unavailable; withdrawal was refused."
            )
        retained_media = self.queue.resolve_media_path(media_value)
        if retained_media.resolve() == Path(entry["media_path"]).resolve():
            raise ReferenceDiscoveryError(
                "Accepted media and retained discovery media unexpectedly share "
                "one file; withdrawal was refused."
            )
        profiles = self._profiles_using(reference_id)
        if profiles:
            names = ", ".join(path.stem for path in profiles)
            raise ReferenceDiscoveryError(
                f"Reference {reference_id} is used by profile(s) {names}. "
                "Rebuild those profiles explicitly before withdrawing it."
            )
        return reference_id, entry, directory

    def withdraw(
        self,
        video_id: str,
        *,
        status: str = "rejected",
        notes: str,
        expected_revision: int | None = None,
        confirmed: bool = False,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        if status != "rejected":
            raise ReferenceDiscoveryError(
                "Withdrawal currently supports only accepted → rejected."
            )
        if not confirmed:
            raise ReferenceDiscoveryError(
                "Withdrawal confirmation is required."
            )
        note = self._decision_note(notes, required=True)
        with self._decision_lock():
            self._require_unused_request(request_id)
            candidate = self.queue.get(video_id)
            revision = self._expected_revision(candidate, expected_revision)
            repair_inconsistency = (
                candidate["status"] == "rejected"
                and candidate.get("accepted_reference_id") is not None
            )
            if (
                "withdraw" not in self.legal_actions(candidate["status"])
                and not repair_inconsistency
            ):
                raise ReferenceDiscoveryError(
                    f"Cannot withdraw a {candidate['status']} candidate. "
                    "Refresh and use a legal action."
                )
            reference_id, _entry, directory = self._withdrawal_artifacts(
                candidate
            )
            event_id = uuid.uuid4().hex
            recovery_key = (
                Path("withdrawal_recovery")
                / event_id
                / directory.name
            ).as_posix()
            recovery_directory = (
                self.withdrawal_recovery_root / event_id / directory.name
            )
            if recovery_directory.exists():
                raise ReferenceDiscoveryError(
                    "Withdrawal recovery destination already exists."
                )
            queue_snapshot = self.queue.snapshot()
            index_snapshot = self.reference_library.snapshot_index()
            moved = False
            index_changed = False
            try:
                recovery_directory.parent.mkdir(parents=True, exist_ok=False)
                self.move_path(directory, recovery_directory)
                moved = True
                self.reference_library.remove(reference_id)
                index_changed = True
                updated = self.queue.transition(
                    video_id,
                    expected_revision=revision,
                    expected_status=candidate["status"],
                    status=status,
                    notes=note,
                    clear_accepted_reference=True,
                )
                self.audit_ledger.append(
                    self._event(
                        candidate=candidate,
                        action="withdraw",
                        requested_status=status,
                        resulting_status=updated["status"],
                        resulting_revision=updated["revision"],
                        accepted_reference_id_after=None,
                        result="success",
                        note=note,
                        request_id=request_id,
                        recovery_key=recovery_key,
                        event_id=event_id,
                    )
                )
                return {
                    "candidate": updated,
                    "withdrawn_reference_id": reference_id,
                    "recovery_key": recovery_key,
                }
            except Exception as error:
                rollback_errors = []
                try:
                    self.queue.restore(queue_snapshot)
                except Exception as rollback_error:
                    rollback_errors.append(f"queue restore failed: {rollback_error}")
                if moved and recovery_directory.exists():
                    try:
                        directory.parent.mkdir(parents=True, exist_ok=True)
                        self.move_path(recovery_directory, directory)
                    except Exception as rollback_error:
                        rollback_errors.append(
                            f"artifact restore failed: {rollback_error}"
                        )
                if index_changed:
                    try:
                        self.reference_library.restore_index(index_snapshot)
                    except Exception as rollback_error:
                        rollback_errors.append(
                            f"index restore failed: {rollback_error}"
                        )
                try:
                    recovery_directory.parent.rmdir()
                except OSError:
                    pass
                self._record_operational_failure(
                    candidate=candidate,
                    action="withdraw",
                    requested_status=status,
                    note=note,
                    request_id=request_id,
                    error=error,
                )
                if rollback_errors:
                    raise ReferenceDiscoveryError(
                        f"Withdrawal failed ({error}); rollback was incomplete: "
                        + "; ".join(rollback_errors)
                    ) from error
                raise

    def consistency_problems(self) -> list[str]:
        candidates = self.queue.list()
        entries = {
            entry["reference_id"]: entry
            for entry in self.reference_library.list_references()
        }
        candidate_by_id = {
            candidate["video_id"]: candidate for candidate in candidates
        }
        profile_inputs: dict[str, list[str]] = {}
        if self.profile_root.exists():
            for path in sorted(self.profile_root.glob("*.json")):
                try:
                    profile = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as error:
                    profile_inputs[path.stem] = [
                        f"<malformed:{type(error).__name__}>"
                    ]
                    continue
                values = profile.get("reference_ids")
                profile_inputs[path.stem] = (
                    values if isinstance(values, list) else ["<malformed>"]
                )
        problems = []
        for candidate in candidates:
            video_id = candidate["video_id"]
            expected = f"youtube-{video_id}"
            reference_id = candidate.get("accepted_reference_id")
            status = candidate["status"]
            if status != "accepted":
                for profile_name, ids in profile_inputs.items():
                    if expected in ids:
                        problems.append(
                            f"{video_id}: non-accepted reference {expected} "
                            f"remains in profile {profile_name}; rebuild the "
                            "profile explicitly."
                        )
            if status in {"rejected", "duplicate"} and reference_id is not None:
                problems.append(
                    f"{video_id}: {status} candidate retains "
                    f"accepted_reference_id {reference_id}; use the dedicated "
                    "withdrawal repair workflow."
                )
            if status == "accepted" and reference_id is None:
                problems.append(
                    f"{video_id}: accepted candidate has no accepted_reference_id."
                )
                continue
            if reference_id is None:
                continue
            if reference_id != expected:
                problems.append(
                    f"{video_id}: accepted_reference_id {reference_id} does "
                    f"not match owned reference {expected}."
                )
            entry = entries.get(reference_id)
            if entry is None:
                problems.append(
                    f"{video_id}: accepted_reference_id {reference_id} is "
                    "missing from the strict reference index."
                )
                continue
            if entry["profile_name"] != candidate.get("category"):
                problems.append(
                    f"{video_id}: candidate category "
                    f"{candidate.get('category')!r} does not match indexed "
                    f"profile {entry['profile_name']!r}."
                )
            try:
                self.reference_library.validate_checksum(reference_id)
            except ReferenceClipError as error:
                problems.append(
                    f"{video_id}: accepted reference checksum is invalid: "
                    f"{error}"
                )
            try:
                baseline = load_and_validate_baseline(
                    Path(entry["baseline_path"])
                )
                if (
                    baseline["reference_id"] != reference_id
                    or baseline.get("source_video_id") != video_id
                ):
                    problems.append(
                        f"{video_id}: indexed reference ownership metadata "
                        "does not match the candidate."
                    )
            except (ReferenceClipError, OSError) as error:
                problems.append(
                    f"{video_id}: reference annotations are invalid: {error}"
                )
        for reference_id, entry in entries.items():
            baseline_path = Path(entry["baseline_path"])
            try:
                baseline = load_and_validate_baseline(baseline_path)
            except (ReferenceClipError, OSError):
                continue
            video_id = baseline.get("source_video_id")
            candidate = candidate_by_id.get(video_id)
            if (
                baseline_path.parent.name.startswith("discovered-")
                and candidate is None
            ):
                problems.append(
                    f"{video_id}: strict index lists discovered reference "
                    f"{reference_id}, but its candidate queue record is missing."
                )
            elif (
                baseline_path.parent.name.startswith("discovered-")
                and candidate is not None
                and (
                    candidate["status"] != "accepted"
                    or candidate.get("accepted_reference_id") != reference_id
                )
            ):
                problems.append(
                    f"{video_id}: strict index still lists {reference_id} "
                    f"while candidate status is {candidate['status']}; use "
                    "the dedicated withdrawal repair workflow."
                )
        return problems

    def validate_consistency(self) -> dict[str, Any]:
        self.queue.validate_media_paths(require_canonical=True)
        problems = self.consistency_problems()
        if problems:
            raise ReferenceDiscoveryError(
                "Reference decision consistency validation failed: "
                + " ".join(problems)
            )
        return {
            "candidate_count": len(self.queue.list()),
            "reference_count": len(
                self.reference_library.list_references()
            ),
            "status": "valid",
        }


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
    parser.add_argument(
        "--data-root",
        type=Path,
        help=(
            "Persistent CreatorFlow data root used to resolve relative candidate "
            "media paths. By default it is inferred from --queue-path."
        ),
    )
    parser.add_argument("--reference-root", type=Path, default=DEFAULT_REFERENCE_ROOT)
    parser.add_argument("--reference-index", type=Path)
    parser.add_argument("--profile-directory", type=Path, default=DEFAULT_PROFILE_ROOT)
    parser.add_argument("--audit-path", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    discover = sub.add_parser("discover")
    discover.add_argument("--dry-run", action="store_true")
    discover.add_argument(
        "--target-count",
        type=int,
        help="Balanced total split between established and breakout cohorts.",
    )
    discover.add_argument("--established-count", type=int)
    discover.add_argument("--breakout-count", type=int)
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
    history = sub.add_parser("history")
    history.add_argument("video_id")
    history.add_argument("--limit", type=int)
    withdraw = sub.add_parser("withdraw")
    withdraw.add_argument("video_id")
    withdraw.add_argument("--status", choices=("rejected",), default="rejected")
    withdraw.add_argument("--note", required=True)
    withdraw.add_argument("--expected-revision", type=int)
    migrate = sub.add_parser("migrate-media-paths")
    migrate.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Sequence[str] | None = ()) -> int:
    args = build_parser().parse_args(argv)
    queue = ReferenceCandidateQueue(args.queue_path, data_root=args.data_root)
    try:
        if args.command == "discover":
            service = ReferenceDiscoveryService(YouTubeDataAPI(), queue)
            for value, label in (
                (args.pool_size, "pool size"),
                (args.publication_days, "publication days"),
                (args.max_per_creator, "creator cap"),
                (args.max_per_topic, "topic cap"),
            ):
                if value < 1:
                    raise ReferenceDiscoveryError(f"{label} must be positive.")
            if args.target_count is not None and args.target_count < 1:
                raise ReferenceDiscoveryError("target count must be positive.")
            for value, label in (
                (args.established_count, "established count"),
                (args.breakout_count, "breakout count"),
            ):
                if value is not None and value < 0:
                    raise ReferenceDiscoveryError(
                        f"{label} must be nonnegative."
                    )
            result = service.discover(
                target_count=args.target_count, pool_size=args.pool_size,
                established_count=args.established_count,
                breakout_count=args.breakout_count,
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
            service = ReferenceDiscoveryService(YouTubeDataAPI(), queue)
            print(f"Refreshed {service.refresh_stats()} candidate(s).")
        elif args.command in {"history", "withdraw", "validate"}:
            library = ReferenceClipLibrary(
                args.reference_root,
                args.reference_index
                if args.reference_index is not None
                else args.reference_root / "index.json",
            )
            ledger = ReferenceDecisionAuditLedger(
                args.audit_path
                if args.audit_path is not None
                else queue.path.parent / DEFAULT_AUDIT_PATH.name
            )
            service = ReferenceDiscoveryService(
                YouTubeDataAPI(),
                queue,
                reference_library=library,
                reference_root=args.reference_root,
                profile_root=args.profile_directory,
                audit_ledger=ledger,
            )
            if args.command == "history":
                events = service.history(args.video_id, limit=args.limit)
                print(json.dumps(events, indent=2, sort_keys=True))
                print(f"{len(events)} decision event(s).")
            elif args.command == "withdraw":
                result = service.withdraw(
                    args.video_id,
                    status=args.status,
                    notes=args.note,
                    expected_revision=args.expected_revision,
                    confirmed=True,
                    request_id=uuid.uuid4().hex,
                )
                print(
                    f"Withdrew {result['withdrawn_reference_id']}; "
                    f"candidate is {result['candidate']['status']} at revision "
                    f"{result['candidate']['revision']}."
                )
                print(f"Recovery key: {result['recovery_key']}")
            else:
                result = service.validate_consistency()
                print(
                    "Reference candidate queue and strict index are consistent: "
                    f"{result['candidate_count']} candidate(s), "
                    f"{result['reference_count']} reference(s)."
                )
        elif args.command == "migrate-media-paths":
            result = queue.migrate_media_paths(dry_run=args.dry_run)
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            raise ReferenceDiscoveryError(
                f"Unsupported command {args.command!r}."
            )
    except (
        ReferenceClipError,
        ReferenceDecisionAuditError,
        ReferenceDiscoveryError,
        ValueError,
    ) as error:
        print(f"Reference discovery failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(None))
