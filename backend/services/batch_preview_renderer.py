"""Batch orchestration for local preview rendering and review registration."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from backend.services.clip_review_queue import ClipReviewQueue
from backend.services.clip_context_expander import (
    ClipContextExpander, ContextExpansionConfiguration,
)
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
    candidate_start: float | None = None
    candidate_end: float | None = None
    render_start: float | None = None
    render_end: float | None = None
    render_duration: float | None = None
    lead_in_seconds: float | None = None
    tail_seconds: float | None = None
    start_boundary_method: str | None = None
    end_boundary_method: str | None = None
    expansion_reasons: tuple[str, ...] = ()
    timing_revision: int | None = None


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
        context_configuration: ContextExpansionConfiguration | None = None,
        reapply_context: bool = False,
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
                existing = (
                    self.review_queue.find_by_candidate(video_id, candidate["candidate_id"])
                    if hasattr(self.review_queue, "find_by_candidate") else None
                )
                expansion = None
                timing_source = "candidate"
                profile = None
                reasons: tuple[str, ...] = ()
                methods = (None, None)
                if existing is not None and (
                    existing.get("timing_source") == "manual"
                    or "timing_source" not in existing and existing.get("timing_revision", 0) > 0
                ) and not reapply_context:
                    timing = {
                        "render_start": existing["render_start"],
                        "render_end": existing["render_end"],
                        "timing_revision": existing["timing_revision"],
                    }
                    timing_source = "manual"
                    profile = existing.get("context_profile")
                    reasons = tuple(existing.get("context_reasons", ()))
                elif context_configuration is not None:
                    prepared = self.renderer.prepare(
                        video_id, rank=rank, candidates_path=artifact_path
                    )
                    expansion = ClipContextExpander().expand(
                        candidate["start"], candidate["end"],
                        prepared["source_probe"].duration, prepared["transcript"],
                        context_configuration,
                    )
                    timing = {
                        "render_start": expansion.render_start,
                        "render_end": expansion.render_end,
                        "timing_revision": (
                            existing["timing_revision"] + 1 if existing and (
                                existing["render_start"] != expansion.render_start
                                or existing["render_end"] != expansion.render_end
                            ) else existing["timing_revision"] if existing else 0
                        ),
                    }
                    timing_source = "automatic"
                    profile = context_configuration.profile
                    reasons = expansion.expansion_reasons
                    methods = (
                        expansion.start_boundary_method, expansion.end_boundary_method
                    )
                else:
                    timing = {}
                    if existing is not None:
                        timing = {
                            "render_start": existing["render_start"],
                            "render_end": existing["render_end"],
                            "timing_revision": existing["timing_revision"],
                        }
                        timing_source = existing.get("timing_source", "candidate")
                rendered = self.renderer.render(
                    video_id, rank=rank, candidates_path=artifact_path,
                    force=force, dry_run=dry_run, **timing,
                    timing_source=timing_source, context_profile=profile,
                    context_reasons=reasons,
                    start_boundary_method=methods[0], end_boundary_method=methods[1],
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
                    changed = bool(existing and (
                        existing["render_start"] != rendered.start
                        or existing["render_end"] != rendered.end
                    ))
                    if changed and timing_source == "automatic":
                        review = self.review_queue.update_timing(
                            existing["review_id"], render_start=rendered.start,
                            render_end=rendered.end, preview_path=rendered.output_path,
                            preview_metadata_path=rendered.metadata_path,
                            timing_source="automatic", context_profile=profile,
                            context_reasons=reasons,
                        )
                    else:
                        if context_configuration is None:
                            review = self.review_queue.add_or_update_preview(
                                video_id, candidate, rendered.output_path, rendered.metadata_path
                            )
                        else:
                            review = self.review_queue.add_or_update_preview(
                                video_id, candidate, rendered.output_path, rendered.metadata_path,
                                render_start=rendered.start, render_end=rendered.end,
                                timing_source=timing_source, context_profile=profile,
                                context_reasons=reasons,
                            )
                    review_id = review["review_id"]
                    timing_revision = review.get(
                        "timing_revision", existing.get("timing_revision", 0) if existing else 0
                    )
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
                candidate["start"], candidate["end"], rendered.start, rendered.end,
                rendered.duration, rendered.lead_in_seconds, rendered.tail_seconds,
                rendered.start_boundary_method, rendered.end_boundary_method,
                rendered.expansion_reasons,
                timing_revision if review_id else (
                    existing["timing_revision"] if existing else 0
                ),
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
        if not isinstance(metadata, dict) or metadata.get("version") not in (1, 2, 3):
            raise ValueError("Preview metadata version is invalid.")
        if metadata.get("video_id") != video_id or metadata.get("candidate_id") != candidate_id:
            raise ValueError("Preview metadata identity does not match the candidate.")
        if Path(metadata.get("output_path", "")).resolve() != Path(preview_path).resolve():
            raise ValueError("Preview metadata output path does not match the preview.")
