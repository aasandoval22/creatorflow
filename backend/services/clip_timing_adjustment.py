"""Transactional review-time preview timing adjustments."""

from __future__ import annotations

import os
import math
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.services.clip_review_queue import ClipReviewQueue, ReviewQueueError
from backend.services.clip_context_expander import (
    ClipContextExpander, ContextExpansionConfiguration,
)
from backend.services.video_preview_renderer import PreviewResult, PreviewResultStatus, VideoPreviewRenderer


@dataclass(frozen=True)
class TimingAdjustmentResult:
    item: dict[str, Any]
    preview: PreviewResult
    render_start: float
    render_end: float
    dry_run: bool = False


class ClipTimingAdjustmentService:
    def __init__(
        self, queue: ClipReviewQueue, renderer: VideoPreviewRenderer,
        *, maximum_duration: float = 60.0,
    ) -> None:
        if (
            isinstance(maximum_duration, bool)
            or not isinstance(maximum_duration, (int, float))
            or not math.isfinite(maximum_duration) or maximum_duration <= 0
        ):
            raise ReviewQueueError("Maximum render duration must be positive.")
        self.queue = queue
        self.renderer = renderer
        self.maximum_duration = float(maximum_duration)

    def adjust(
        self, review_id: str, *, lead_in: float | None = None,
        tail: float | None = None, render_start: float | None = None,
        render_end: float | None = None, allow_longer: bool = False,
        note: str | None = None, clear_note: bool = False,
        dry_run: bool = False, force: bool = True,
        timing_source: str = "manual", context_profile: str | None = None,
        context_reasons: tuple[str, ...] | list[str] = (),
        start_boundary_method: str | None = None,
        end_boundary_method: str | None = None,
    ) -> TimingAdjustmentResult:
        with self.queue.locked():
            return self._adjust_locked(
                review_id, lead_in=lead_in, tail=tail,
                render_start=render_start, render_end=render_end,
                allow_longer=allow_longer, note=note, clear_note=clear_note,
                dry_run=dry_run, force=force,
                timing_source=timing_source, context_profile=context_profile,
                context_reasons=context_reasons,
                start_boundary_method=start_boundary_method,
                end_boundary_method=end_boundary_method,
            )

    def _adjust_locked(
        self, review_id: str, *, lead_in: float | None = None,
        tail: float | None = None, render_start: float | None = None,
        render_end: float | None = None, allow_longer: bool = False,
        note: str | None = None, clear_note: bool = False,
        dry_run: bool = False, force: bool = True,
        timing_source: str = "manual", context_profile: str | None = None,
        context_reasons: tuple[str, ...] | list[str] = (),
        start_boundary_method: str | None = None,
        end_boundary_method: str | None = None,
    ) -> TimingAdjustmentResult:
        item = self.queue.find_by_review_id(review_id)
        if item is None:
            raise ReviewQueueError(f"Review ID {review_id!r} was not found.")
        relative = lead_in is not None or tail is not None
        absolute = render_start is not None or render_end is not None
        if not relative and not absolute:
            raise ReviewQueueError("At least one timing adjustment is required.")
        if relative and absolute:
            raise ReviewQueueError("Relative and absolute timing adjustments are mutually exclusive.")
        if absolute and (render_start is None or render_end is None):
            raise ReviewQueueError("Absolute render start and end must be supplied together.")
        if relative:
            lead = 0.0 if lead_in is None else self._nonnegative(lead_in, "Lead-in")
            extra_tail = 0.0 if tail is None else self._nonnegative(tail, "Tail")
            proposed_start = item["candidate_start"] - lead
            proposed_end = item["candidate_end"] + extra_tail
        else:
            proposed_start = self._number(render_start, "Render start")
            proposed_end = self._number(render_end, "Render end")
        duration = proposed_end - proposed_start
        if duration > self.maximum_duration and not allow_longer:
            raise ReviewQueueError(
                f"Proposed duration {duration:.3f}s exceeds maximum "
                f"{self.maximum_duration:.3f}s; use --allow-longer to override."
            )
        context = self.renderer.prepare(
            item["video_id"], candidate_id=item["candidate_id"],
            render_start=proposed_start, render_end=proposed_end,
        )
        proposed_start = context["render"]["start"]
        proposed_end = context["render"]["end"]
        try:
            old_video = self.queue.paths.resolve(
                item["preview_path"], must_exist=True, regular=True
            )
            old_metadata = self.queue.paths.resolve(
                item["preview_metadata_path"], must_exist=True, regular=True
            )
        except ValueError as error:
            raise ReviewQueueError(
                "Stored preview paths are missing or outside persistent storage."
            ) from error
        backups: list[tuple[Path, Path]] = []
        if not dry_run:
            for original in (old_video, old_metadata):
                if original.is_file():
                    descriptor, name = tempfile.mkstemp(
                        prefix=f".{original.name}.rollback.", dir=original.parent
                    )
                    os.close(descriptor)
                    shutil.copy2(original, name)
                    backups.append((original, Path(name)))
        try:
            preview = self.renderer.render(
                item["video_id"], candidate_id=item["candidate_id"], force=force,
                dry_run=dry_run, output_path=old_video,
                render_start=proposed_start, render_end=proposed_end,
                timing_revision=item["timing_revision"] + 1,
                timing_source=timing_source, context_profile=context_profile,
                context_reasons=context_reasons,
                start_boundary_method=start_boundary_method,
                end_boundary_method=end_boundary_method,
            )
            if preview.status is PreviewResultStatus.FAILED:
                raise ReviewQueueError(preview.message)
            if dry_run:
                return TimingAdjustmentResult(item, preview, proposed_start, proposed_end, True)
            updated = self.queue.update_timing(
                review_id, render_start=proposed_start, render_end=proposed_end,
                preview_path=preview.output_path or old_video,
                preview_metadata_path=preview.metadata_path or old_metadata,
                note=note, clear_note=clear_note,
                timing_source=timing_source, context_profile=context_profile,
                context_reasons=context_reasons,
            )
        except Exception:
            for original, backup in backups:
                shutil.copy2(backup, original)
            raise
        finally:
            for _, backup in backups:
                backup.unlink(missing_ok=True)
        return TimingAdjustmentResult(updated, preview, proposed_start, proposed_end)

    def reset(self, review_id: str, **kwargs: Any) -> TimingAdjustmentResult:
        item = self.queue.find_by_review_id(review_id)
        if item is None:
            raise ReviewQueueError(f"Review ID {review_id!r} was not found.")
        return self.adjust(
            review_id, render_start=item["candidate_start"],
            render_end=item["candidate_end"], timing_source="candidate", **kwargs,
        )

    def reapply_context(
        self, review_id: str, *, profile: str = "reaction",
        configuration: ContextExpansionConfiguration | None = None,
        **kwargs: Any,
    ) -> TimingAdjustmentResult:
        item = self.queue.find_by_review_id(review_id)
        if item is None:
            raise ReviewQueueError(f"Review ID {review_id!r} was not found.")
        prepared = self.renderer.prepare(
            item["video_id"], candidate_id=item["candidate_id"]
        )
        config = configuration or ContextExpansionConfiguration.for_profile(profile)
        expanded = ClipContextExpander().expand(
            item["candidate_start"], item["candidate_end"],
            prepared["source_probe"].duration, prepared["transcript"], config,
        )
        return self.adjust(
            review_id, render_start=expanded.render_start,
            render_end=expanded.render_end,
            allow_longer=config.allow_longer or expanded.render_duration > self.maximum_duration,
            timing_source="automatic", context_profile=config.profile,
            context_reasons=expanded.expansion_reasons,
            start_boundary_method=expanded.start_boundary_method,
            end_boundary_method=expanded.end_boundary_method,
            **kwargs,
        )

    @staticmethod
    def _nonnegative(value: float, label: str) -> float:
        parsed = ClipTimingAdjustmentService._number(value, label)
        if parsed < 0:
            raise ReviewQueueError(f"{label} seconds must be nonnegative.")
        return parsed

    @staticmethod
    def _number(value: float, label: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ReviewQueueError(f"{label} must be numeric.")
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ReviewQueueError(f"{label} must be finite.")
        return parsed
