"""Batch orchestration for local preview rendering and review registration."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from backend.services.clip_review_queue import ClipReviewQueue
from backend.services.video_preview_renderer import (
    PreviewResultStatus,
    VideoPreviewRenderer,
)


@dataclass(frozen=True)
class BatchCandidateResult:
    rank: int
    candidate_id: str | None
    status: str
    message: str
    preview_path: str | None = None
    metadata_path: str | None = None
    review_id: str | None = None


@dataclass(frozen=True)
class BatchPreviewResult:
    video_id: str
    items: tuple[BatchCandidateResult, ...]

    @property
    def successful(self) -> int:
        return sum(item.status == PreviewResultStatus.SUCCESS.value for item in self.items)

    @property
    def skipped(self) -> int:
        return sum(item.status == PreviewResultStatus.SKIPPED.value for item in self.items)

    @property
    def failed(self) -> int:
        return sum(item.status == PreviewResultStatus.FAILED.value for item in self.items)


class BatchPreviewRenderer:
    def __init__(
        self, renderer: VideoPreviewRenderer, review_queue: ClipReviewQueue,
    ) -> None:
        self.renderer = renderer
        self.review_queue = review_queue

    def render(
        self, video_id: str, *, top: int = 3, ranks: Sequence[int] | None = None,
        candidates_path: Path | None = None, force: bool = False,
        dry_run: bool = False, maximum_preview_count: int | None = None,
    ) -> BatchPreviewResult:
        if isinstance(top, bool) or not isinstance(top, int) or top < 1:
            raise ValueError("Top must be a positive integer.")
        if maximum_preview_count is not None and (
            isinstance(maximum_preview_count, bool)
            or not isinstance(maximum_preview_count, int) or maximum_preview_count < 1
        ):
            raise ValueError("Maximum preview count must be a positive integer.")
        requested = list(ranks) if ranks is not None else None
        if requested is not None:
            if any(isinstance(rank, bool) or not isinstance(rank, int) or rank < 1 for rank in requested):
                raise ValueError("Candidate ranks must be positive integers.")
            if len(set(requested)) != len(requested):
                raise ValueError("Candidate ranks must not contain duplicates.")

        record = self.renderer.manifest.get(video_id)
        if record is None:
            raise ValueError(f"Video {video_id!r} was not found in the manifest.")
        analysis = record.get("clip_analysis", {})
        if analysis.get("status") != "completed":
            raise ValueError(f"Clip analysis for {video_id!r} is not completed.")
        artifact_path = Path(candidates_path or analysis.get("candidates_json_path", ""))
        artifact = self.renderer._read_json(artifact_path.expanduser().resolve(), "candidate artifact")
        if artifact.get("version") != 1 or artifact.get("video_id") != video_id:
            raise ValueError("Candidate artifact version or video ID is invalid.")
        raw_candidates = artifact.get("candidates")
        if not isinstance(raw_candidates, list):
            raise ValueError("Candidate artifact candidates must be a list.")
        candidates = [self.renderer._validate_candidate(item) for item in raw_candidates]
        ranks_seen: set[int] = set()
        ids_seen: set[str] = set()
        for candidate in candidates:
            score = candidate.get("score")
            if (
                isinstance(score, bool) or not isinstance(score, (int, float))
                or not math.isfinite(score) or not 0 <= score <= 100
            ):
                raise ValueError(
                    f"Candidate {candidate['candidate_id']!r} score must be "
                    "numeric and between 0 and 100."
                )
            if candidate["rank"] in ranks_seen:
                raise ValueError(f"Candidate artifact contains duplicate rank {candidate['rank']}.")
            if candidate["candidate_id"] in ids_seen:
                raise ValueError(
                    f"Candidate artifact contains duplicate ID {candidate['candidate_id']!r}."
                )
            ranks_seen.add(candidate["rank"])
            ids_seen.add(candidate["candidate_id"])
        by_rank = {item["rank"]: item for item in candidates}
        selected_ranks = requested if requested is not None else sorted(by_rank)[:top]
        if maximum_preview_count is not None:
            selected_ranks = selected_ranks[:maximum_preview_count]

        results: list[BatchCandidateResult] = []
        for rank in selected_ranks:
            candidate = by_rank.get(rank)
            if candidate is None:
                results.append(BatchCandidateResult(
                    rank, None, "failed", f"No candidate with rank {rank} was found."
                ))
                continue
            try:
                rendered = self.renderer.render(
                    video_id, rank=rank, candidates_path=artifact_path,
                    force=force, dry_run=dry_run,
                )
            except (OSError, ValueError) as error:
                results.append(BatchCandidateResult(
                    rank, candidate["candidate_id"], "failed",
                    f"Preview rendering failed: {error}",
                ))
                continue
            review_id = None
            message = rendered.message
            if rendered.status in (PreviewResultStatus.SUCCESS, PreviewResultStatus.SKIPPED) and not dry_run:
                try:
                    self._verify_preview(rendered.output_path, rendered.metadata_path, video_id, candidate["candidate_id"])
                    review = self.review_queue.add_or_update_preview(
                        video_id, candidate, rendered.output_path, rendered.metadata_path
                    )
                    review_id = review["review_id"]
                except (OSError, ValueError, json.JSONDecodeError) as error:
                    rendered_status = "failed"
                    message = f"Preview verification failed: {error}"
                else:
                    rendered_status = rendered.status.value
            else:
                rendered_status = rendered.status.value
            results.append(BatchCandidateResult(
                rank, candidate["candidate_id"], rendered_status, message,
                rendered.output_path, rendered.metadata_path, review_id,
            ))
        return BatchPreviewResult(video_id, tuple(results))

    @staticmethod
    def _verify_preview(
        preview_path: str | None, metadata_path: str | None,
        video_id: str, candidate_id: str,
    ) -> None:
        if not preview_path or not Path(preview_path).is_file():
            raise ValueError("Preview media path does not exist.")
        if not metadata_path or not Path(metadata_path).is_file():
            raise ValueError("Preview metadata path does not exist.")
        with Path(metadata_path).open(encoding="utf-8") as stream:
            metadata = json.load(stream)
        if not isinstance(metadata, dict) or metadata.get("version") != 1:
            raise ValueError("Preview metadata version is invalid.")
        if metadata.get("video_id") != video_id or metadata.get("candidate_id") != candidate_id:
            raise ValueError("Preview metadata identity does not match the candidate.")
        if Path(metadata.get("output_path", "")).resolve() != Path(preview_path).resolve():
            raise ValueError("Preview metadata output path does not match the preview.")
