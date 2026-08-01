"""Loopback-only interactive HTTP server for the local clip-review queue."""

from __future__ import annotations

import argparse
import hmac
import html
import ipaddress
import json
import re
import secrets
import socket
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from backend.app.render_preview import crf_value, even_integer, positive_integer, positive_number
from backend.app.review_clips import _sorted
from backend.services.clip_review_queue import (
    DEFAULT_REVIEW_QUEUE_PATH, REVIEW_STATUSES, ClipReviewQueue, ReviewQueueError,
)
from backend.services.clip_timing_adjustment import ClipTimingAdjustmentService
from backend.services.reference_clip_comparator import ReferenceClipComparator
from backend.services.reference_clip_analyzer import ReferenceAnalysisError
from backend.services.reference_clip_library import ReferenceClipLibrary
from backend.services.reference_annotations import (
    ENUM_FIELDS, ReferenceAnnotationError, ReferenceAnnotationStore,
)
from backend.services.reference_decision_audit import ReferenceDecisionAuditError
from backend.services.reference_evidence_audit import ReferenceEvidenceAuditError
from backend.services.reference_evidence_service import (
    ReferenceEvidenceError, ReferenceEvidenceService,
)
from backend.services.reference_profile_builder import (
    ReferenceProfileBuilder, ReferenceProfileError,
)
from backend.services.reference_discovery import (
    ReferenceCandidateQueue, ReferenceDiscoveryError, ReferenceDiscoveryService,
    YouTubeDataAPI,
)
from backend.services.video_manifest import DEFAULT_MANIFEST_PATH
from backend.services.video_preview_renderer import (
    DEFAULT_PREVIEW_DIRECTORY, SAFE_PRESETS, CaptionConfiguration,
    RenderConfiguration, VideoPreviewRenderer,
)

MAX_BODY = 64 * 1024
MAX_NOTE = 4_000
CHUNK_SIZE = 64 * 1024
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'none'; style-src 'self' 'unsafe-inline'; "
        "media-src 'self'; img-src 'self'; object-src 'none'; base-uri 'none'; "
        "form-action 'self'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}


def port_number(value: str) -> int:
    parsed = positive_integer(value)
    if parsed > 65535:
        raise argparse.ArgumentTypeError("must be at most 65535")
    return parsed


def is_loopback_host(host: str) -> bool:
    normalized = host.strip().lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


@dataclass
class ReviewApplication:
    queue: ClipReviewQueue
    timing_service: ClipTimingAdjustmentService
    form_token: str
    maximum_duration: float = 60.0
    reference_comparator: ReferenceClipComparator | None = None
    reference_profile: str = "personality_reaction"
    reference_candidate_queue: ReferenceCandidateQueue | None = None
    reference_discovery_service: ReferenceDiscoveryService | None = None
    reference_evidence_service: ReferenceEvidenceService | None = None


class ReviewHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], app: ReviewApplication) -> None:
        self.app = app
        try:
            if ipaddress.ip_address(address[0]).version == 6:
                self.address_family = socket.AF_INET6
        except ValueError:
            pass
        super().__init__(address, ReviewRequestHandler)


class ReviewRequestHandler(BaseHTTPRequestHandler):
    server: ReviewHTTPServer

    def _headers(
        self, status: int, content_type: str, length: int, *,
        cache: bool = True, extra: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        if cache:
            self.send_header("Cache-Control", "no-store")
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()

    def _text(self, status: int, message: str, *, head: bool = False) -> None:
        body = (
            "<!doctype html><html><head><meta charset=\"utf-8\"><title>"
            f"{status}</title></head><body><h1>{status}</h1>"
            f"<p>{html.escape(message)}</p><p><a href=\"/\">Return to reviews</a></p>"
            "</body></html>"
        ).encode()
        self._headers(status, "text/html; charset=utf-8", len(body))
        if not head:
            self.wfile.write(body)

    def do_GET(self) -> None:
        route = urlsplit(self.path)
        if route.path == "/":
            self._index(route.query, head=False)
        elif route.path == "/references":
            self._accepted_references(route.query, head=False)
        elif self._reference_analysis_route(route.path) is not None:
            self._reference_analysis(
                self._reference_analysis_route(route.path) or "", head=False
            )
        elif route.path.startswith("/accepted-reference-media/"):
            self._accepted_reference_media(
                route.path.removeprefix("/accepted-reference-media/"), head=False
            )
        elif route.path == "/reference-candidates":
            self._reference_candidates(route.query, head=False)
        elif route.path.startswith("/reference-media/"):
            self._reference_media(
                route.path.removeprefix("/reference-media/"), head=False
            )
        elif route.path.startswith("/media/"):
            self._media(route.path.removeprefix("/media/"), head=False)
        elif self._write_route(route.path) or self._evidence_write_route(route.path):
            self._text(HTTPStatus.METHOD_NOT_ALLOWED, "This route accepts POST only.")
        else:
            self._text(HTTPStatus.NOT_FOUND, "The requested local resource was not found.")

    def do_HEAD(self) -> None:
        route = urlsplit(self.path)
        if route.path == "/":
            self._index(route.query, head=True)
        elif route.path == "/references":
            self._accepted_references(route.query, head=True)
        elif self._reference_analysis_route(route.path) is not None:
            self._reference_analysis(
                self._reference_analysis_route(route.path) or "", head=True
            )
        elif route.path.startswith("/accepted-reference-media/"):
            self._accepted_reference_media(
                route.path.removeprefix("/accepted-reference-media/"), head=True
            )
        elif route.path == "/reference-candidates":
            self._reference_candidates(route.query, head=True)
        elif route.path.startswith("/reference-media/"):
            self._reference_media(
                route.path.removeprefix("/reference-media/"), head=True
            )
        elif route.path.startswith("/media/"):
            self._media(route.path.removeprefix("/media/"), head=True)
        elif self._write_route(route.path) or self._evidence_write_route(route.path):
            self._text(HTTPStatus.METHOD_NOT_ALLOWED, "This route accepts POST only.", head=True)
        else:
            self._text(HTTPStatus.NOT_FOUND, "The requested local resource was not found.", head=True)

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        evidence_route = self._evidence_write_route(path)
        if evidence_route is not None:
            self._reference_evidence_action(*evidence_route)
            return
        reference_route = self._reference_write_route(path)
        if reference_route is not None:
            self._reference_decision(*reference_route)
            return
        if path.startswith("/media/"):
            self._text(HTTPStatus.METHOD_NOT_ALLOWED, "Preview media is read-only.")
            return
        route = self._write_route(path)
        if route is None:
            self._text(HTTPStatus.NOT_FOUND, "The requested local resource was not found.")
            return
        review_id, action = route
        try:
            form = self._form()
            self._require_token(form)
            if self.server.app.queue.find_by_review_id(review_id) is None:
                self._text(HTTPStatus.NOT_FOUND, "That review no longer exists.")
                return
            if action == "decision":
                message = self._decision(review_id, form)
            elif action == "adjust":
                message = self._adjust(review_id, form)
            elif action == "reset-timing":
                message = self._reset(review_id, form)
            elif action == "reapply-context":
                message = self._reapply_context(review_id, form)
            else:
                message = self._compare_reference(review_id)
        except RequestError as error:
            self._text(error.status, error.message)
            return
        except (ReviewQueueError, OSError, ValueError) as error:
            self.log_error("request failed: %s", error)
            self._redirect(
                "error",
                "The request failed; the previous review and preview were preserved. "
                "Check the server log for details.",
            )
            return
        except Exception as error:  # browser responses must never expose tracebacks
            self.log_error("unexpected request failure: %s", error)
            self._text(HTTPStatus.INTERNAL_SERVER_ERROR, "The local request failed unexpectedly.")
            return
        self._redirect("success", message)

    def _method_not_allowed(self) -> None:
        self._text(HTTPStatus.METHOD_NOT_ALLOWED, "This HTTP method is not allowed.")

    do_DELETE = _method_not_allowed
    do_OPTIONS = _method_not_allowed
    do_PATCH = _method_not_allowed
    do_PUT = _method_not_allowed

    @staticmethod
    def _write_route(path: str) -> tuple[str, str] | None:
        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "reviews" and parts[2] in {
            "decision", "adjust", "reset-timing", "reapply-context",
            "compare-reference",
        }:
            review_id = parts[1]
            if review_id and "/" not in review_id and review_id.startswith("review_"):
                return review_id, parts[2]
        return None

    @staticmethod
    def _evidence_write_route(path: str) -> tuple[str, str] | None:
        parts = path.strip("/").split("/")
        if (
            len(parts) == 3
            and parts[0] == "references"
            and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", parts[1])
            and parts[2] in {"annotations", "reanalyze", "rebuild-profile"}
        ):
            return parts[1], parts[2]
        return None

    @staticmethod
    def _reference_analysis_route(path: str) -> str | None:
        parts = path.strip("/").split("/")
        if (
            len(parts) == 3
            and parts[0] == "references"
            and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", parts[1])
            and parts[2] == "analysis"
        ):
            return parts[1]
        return None

    @staticmethod
    def _reference_write_route(path: str) -> tuple[str, str] | None:
        parts = path.strip("/").split("/")
        if (
            len(parts) == 3 and parts[0] == "reference-candidates"
            and parts[2] == "decision"
            and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", parts[1])
        ):
            return parts[1], parts[2]
        return None

    def _form(self) -> dict[str, list[str]]:
        content_type = self.headers.get("Content-Type", "")
        if content_type.split(";", 1)[0].strip().lower() != "application/x-www-form-urlencoded":
            raise RequestError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Forms must use URL encoding.")
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length or "")
        except ValueError as error:
            raise RequestError(HTTPStatus.BAD_REQUEST, "A valid Content-Length is required.") from error
        if length < 0:
            raise RequestError(HTTPStatus.BAD_REQUEST, "Invalid request body length.")
        if length > MAX_BODY:
            raise RequestError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "The form body is too large.")
        body = self.rfile.read(length)
        try:
            return parse_qs(body.decode("utf-8"), keep_blank_values=True, strict_parsing=True)
        except (UnicodeDecodeError, ValueError) as error:
            raise RequestError(HTTPStatus.BAD_REQUEST, "The form body is malformed.") from error

    def _one(self, form: dict[str, list[str]], name: str, *, required: bool = False) -> str | None:
        values = form.get(name)
        if not values:
            if required:
                raise RequestError(HTTPStatus.BAD_REQUEST, f"Missing form field: {name}.")
            return None
        if len(values) != 1:
            raise RequestError(HTTPStatus.BAD_REQUEST, f"Duplicate form field: {name}.")
        return values[0]

    def _require_token(self, form: dict[str, list[str]]) -> None:
        values = form.get("form_token")
        if not values:
            raise RequestError(HTTPStatus.FORBIDDEN, "The form token is missing or invalid.")
        supplied = self._one(form, "form_token")
        if not hmac.compare_digest(supplied or "", self.server.app.form_token):
            raise RequestError(HTTPStatus.FORBIDDEN, "The form token is missing or invalid.")

    def _note(self, form: dict[str, list[str]]) -> tuple[str | None, bool]:
        note = self._one(form, "note")
        clear = self._one(form, "clear_note") is not None
        if note is not None and len(note) > MAX_NOTE:
            raise RequestError(HTTPStatus.BAD_REQUEST, f"Review notes are limited to {MAX_NOTE} characters.")
        if clear:
            return None, True
        # An omitted/empty note preserves the existing note.
        return (note if note else None), False

    def _decision(self, review_id: str, form: dict[str, list[str]]) -> str:
        action = self._one(form, "action", required=True)
        if action not in {"approve", "reject", "pending"}:
            raise RequestError(HTTPStatus.BAD_REQUEST, "Invalid review action.")
        note, clear = self._note(form)
        queue = self.server.app.queue
        with queue.locked():
            if action == "approve":
                item = queue.approve(review_id, note)
                if clear:
                    item = queue.update_note(review_id, None)
            elif action == "reject":
                item = queue.reject(review_id, note)
                if clear:
                    item = queue.update_note(review_id, None)
            else:
                item = queue.return_to_pending(review_id, note, clear_note=clear)
        return f"{item['review_id']} is now {item['status']}."

    @staticmethod
    def _float(value: str | None, label: str) -> float | None:
        if value in (None, ""):
            return None
        try:
            return float(value)
        except ValueError as error:
            raise RequestError(HTTPStatus.BAD_REQUEST, f"{label} must be a number.") from error

    def _adjust(self, review_id: str, form: dict[str, list[str]]) -> str:
        lead = self._float(self._one(form, "lead_in"), "Lead-in")
        tail = self._float(self._one(form, "tail"), "Tail")
        start = self._float(self._one(form, "render_start"), "Render start")
        end = self._float(self._one(form, "render_end"), "Render end")
        maximum = self._float(self._one(form, "maximum_duration"), "Maximum duration")
        if maximum is None or maximum <= 0:
            raise RequestError(HTTPStatus.BAD_REQUEST, "Maximum duration must be positive.")
        if maximum > self.server.app.maximum_duration:
            raise RequestError(
                HTTPStatus.BAD_REQUEST,
                f"Maximum duration cannot exceed the server limit of {self.server.app.maximum_duration:g}s.",
            )
        allow_longer = self._one(form, "allow_longer") is not None
        item = self.server.app.queue.find_by_review_id(review_id)
        if item is None:
            raise RequestError(HTTPStatus.NOT_FOUND, "That review no longer exists.")
        relative = lead is not None or tail is not None
        absolute = start is not None or end is not None
        if relative and not absolute:
            duration = (
                item["candidate_duration"] + (lead or 0.0) + (tail or 0.0)
            )
            if duration > maximum and not allow_longer:
                raise RequestError(
                    HTTPStatus.BAD_REQUEST,
                    f"Proposed duration {duration:.3f}s exceeds maximum {maximum:.3f}s.",
                )
        elif absolute and start is not None and end is not None:
            if end - start > maximum and not allow_longer:
                raise RequestError(
                    HTTPStatus.BAD_REQUEST,
                    f"Proposed duration {end - start:.3f}s exceeds maximum {maximum:.3f}s.",
                )
        note, clear = self._note(form)
        service = self.server.app.timing_service
        result = service.adjust(
            review_id, lead_in=lead, tail=tail, render_start=start, render_end=end,
            allow_longer=allow_longer, note=note, clear_note=clear, force=True,
        )
        return (
            f"Preview rerendered at revision {result.item['timing_revision']}: "
            f"{result.render_start:.3f}–{result.render_end:.3f}s. It is pending review."
        )

    def _reset(self, review_id: str, form: dict[str, list[str]]) -> str:
        note, clear = self._note(form)
        result = self.server.app.timing_service.reset(
            review_id, note=note, clear_note=clear, force=True
        )
        return (
            f"Preview reset to candidate timing at revision {result.item['timing_revision']}: "
            f"{result.render_start:.3f}–{result.render_end:.3f}s. It is pending review."
        )

    def _reapply_context(self, review_id: str, form: dict[str, list[str]]) -> str:
        note, clear = self._note(form)
        result = self.server.app.timing_service.reapply_context(
            review_id, profile="reaction", note=note, clear_note=clear, force=True
        )
        return (
            f"Automatic reaction context reapplied at revision "
            f"{result.item['timing_revision']}: {result.render_start:.3f}–"
            f"{result.render_end:.3f}s. It is pending review."
        )

    def _compare_reference(self, review_id: str) -> str:
        comparator = self.server.app.reference_comparator
        if comparator is None:
            raise ReviewQueueError("Reference comparison is not configured.")
        item = self.server.app.queue.find_by_review_id(review_id)
        if item is None:
            raise RequestError(HTTPStatus.NOT_FOUND, "That review no longer exists.")
        comparator.compare(self.server.app.reference_profile, item, write=True)
        return (
            f"{review_id} compared locally against "
            f"{self.server.app.reference_profile}; review state was unchanged."
        )

    def _reference_decision(self, video_id: str, _route: str) -> None:
        queue = self.server.app.reference_candidate_queue
        service = self.server.app.reference_discovery_service
        if queue is None:
            self._text(HTTPStatus.NOT_FOUND, "Reference discovery is not configured.")
            return
        try:
            form = self._form()
            self._require_token(form)
            action = self._one(form, "action", required=True)
            note = self._one(form, "note") or ""
            revision_value = self._one(
                form, "expected_revision", required=True
            )
            try:
                expected_revision = int(revision_value or "")
            except ValueError as error:
                raise RequestError(
                    HTTPStatus.BAD_REQUEST,
                    "Candidate revision is invalid; refresh and retry.",
                ) from error
            request_id = self._one(form, "request_id", required=True)
            category = self._one(form, "category") or "gaming_highlight"
            topic = self._one(form, "topic")
            if len(note) > MAX_NOTE:
                raise RequestError(
                    HTTPStatus.BAD_REQUEST,
                    f"Review notes are limited to {MAX_NOTE} characters.",
                )
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", category):
                raise RequestError(HTTPStatus.BAD_REQUEST, "Invalid reference category.")
            if topic is not None and not re.fullmatch(
                r"[a-z0-9][a-z0-9_-]{0,63}",
                topic,
            ):
                raise RequestError(
                    HTTPStatus.BAD_REQUEST,
                    "Invalid reference topic.",
                )
            if action == "accept":
                if service is None:
                    raise ReferenceDiscoveryError(
                        "Reference acceptance is not configured."
                    )
                service.accept(
                    video_id, category=category, notes=note,
                    transcription=False, topic=topic,
                    expected_revision=expected_revision,
                    request_id=request_id,
                )
                message = f"{video_id} was accepted and registered as a reference."
            elif action in {"reject", "duplicate", "reconsider"}:
                if service is None:
                    raise ReferenceDiscoveryError(
                        "Reference decisions are not configured."
                    )
                updated = service.transition(
                    video_id,
                    action,
                    notes=note,
                    expected_revision=expected_revision,
                    category=category,
                    topic=topic,
                    request_id=request_id,
                )
                message = f"{video_id} is now {updated['status']}."
            elif action == "withdraw":
                if service is None:
                    raise ReferenceDiscoveryError(
                        "Reference withdrawal is not configured."
                    )
                confirmed = self._one(form, "confirm_withdrawal") == "yes"
                result = service.withdraw(
                    video_id,
                    status="rejected",
                    notes=note,
                    expected_revision=expected_revision,
                    confirmed=confirmed,
                    request_id=request_id,
                )
                message = (
                    f"{result['withdrawn_reference_id']} was withdrawn; "
                    f"{video_id} is now rejected."
                )
            else:
                raise RequestError(HTTPStatus.BAD_REQUEST, "Invalid reference action.")
        except RequestError as error:
            self._text(error.status, error.message)
            return
        except (
            ReferenceDecisionAuditError,
            ReferenceDiscoveryError,
            OSError,
            ValueError,
        ) as error:
            self.log_error("reference candidate request failed: %s", error)
            self._reference_redirect(
                "error", "Reference action failed; existing state was preserved."
            )
            return
        self._reference_redirect("success", message)

    def _reference_evidence_action(self, reference_id: str, action: str) -> None:
        service = self.server.app.reference_evidence_service
        if service is None:
            self._text(HTTPStatus.NOT_FOUND, "Accepted-reference evidence is not configured.")
            return
        try:
            form = self._form()
            self._require_token(form)
            request_id = self._one(form, "request_id", required=True)
            revision_value = self._one(
                form, "expected_annotation_revision", required=True
            )
            try:
                expected_revision = int(revision_value or "")
            except ValueError as error:
                raise RequestError(
                    HTTPStatus.BAD_REQUEST,
                    "Annotation revision is invalid; refresh and retry.",
                ) from error
            if action == "annotations":
                values: dict[str, Any] = {}
                for name in ENUM_FIELDS:
                    values[name] = self._one(form, name, required=True)
                values["desired_qualities"] = self._lines(
                    self._one(form, "desired_qualities") or ""
                )
                values["undesirable_qualities"] = self._lines(
                    self._one(form, "undesirable_qualities") or ""
                )
                values["reviewer_notes"] = self._one(
                    form, "reviewer_notes"
                ) or ""
                updated = service.update_annotations(
                    reference_id,
                    expected_revision=expected_revision,
                    values=values,
                    request_id=request_id,
                )
                message = (
                    f"{reference_id} annotations saved at revision "
                    f"{updated['revision']}."
                )
            elif action == "reanalyze":
                result = service.reanalyze(
                    reference_id,
                    transcription=True,
                    force=True,
                    expected_annotation_revision=expected_revision,
                    request_id=request_id,
                )
                message = (
                    f"{reference_id} reanalyzed with transcription at analysis "
                    f"revision {result.get('analysis_revision', 0)}."
                )
            else:
                entry = service.accepted_entry(reference_id)
                profile = service.rebuild_profile(
                    entry["profile_name"],
                    trigger_reference_id=reference_id,
                    expected_annotation_revision=expected_revision,
                    request_id=request_id,
                )
                message = (
                    f"{entry['profile_name']} rebuilt explicitly from "
                    f"{profile['reference_count']} accepted reference(s)."
                )
        except RequestError as error:
            self._text(error.status, error.message)
            return
        except (
            ReferenceAnnotationError,
            ReferenceAnalysisError,
            ReferenceEvidenceAuditError,
            ReferenceEvidenceError,
            ReferenceProfileError,
            OSError,
            ValueError,
        ) as error:
            self.log_error(
                "accepted reference evidence request failed: %s",
                type(error).__name__,
            )
            self._accepted_reference_redirect(
                "error", "Reference evidence action failed; prior state was preserved."
            )
            return
        self._accepted_reference_redirect("success", message)

    @staticmethod
    def _lines(value: str) -> list[str]:
        return [line.strip() for line in value.splitlines() if line.strip()]

    def _accepted_reference_redirect(self, kind: str, message: str) -> None:
        location = "/references?" + urlencode({kind: message})
        self.send_response(HTTPStatus.SEE_OTHER)
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _reference_redirect(self, kind: str, message: str) -> None:
        location = "/reference-candidates?" + urlencode({kind: message})
        self.send_response(HTTPStatus.SEE_OTHER)
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _redirect(self, kind: str, message: str) -> None:
        location = "/?" + urlencode({kind: message})
        self.send_response(HTTPStatus.SEE_OTHER)
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _index(self, query: str, *, head: bool) -> None:
        try:
            parameters = parse_qs(query, keep_blank_values=False)
            status = parameters.get("status", [None])[0]
            video_id = parameters.get("video_id", [None])[0]
            if status is not None and status not in REVIEW_STATUSES:
                raise ReviewQueueError("Unknown status filter.")
            with self.server.app.queue.locked():
                document = self.server.app.queue._load()
            items = [
                item for item in document["items"]
                if (status is None or item["status"] == status)
                and (video_id is None or item["video_id"] == video_id)
            ]
            body = render_index(
                _sorted(items), document["updated_at"], self.server.app.form_token,
                maximum_duration=self.server.app.maximum_duration,
                comparison_reports=self._comparison_reports(items),
                notice=parameters.get("success", parameters.get("error", [None]))[0],
                notice_error="error" in parameters,
            ).encode()
        except (ReviewQueueError, OSError, ValueError) as error:
            self.log_error("index failed: %s", error)
            self._text(HTTPStatus.BAD_REQUEST, str(error), head=head)
            return
        self._headers(HTTPStatus.OK, "text/html; charset=utf-8", len(body))
        if not head:
            self.wfile.write(body)

    def _comparison_reports(self, items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        comparator = self.server.app.reference_comparator
        if comparator is None:
            return {}
        reports = {}
        for item in items:
            try:
                path = comparator.report_path(
                    self.server.app.reference_profile, item["review_id"]
                )
                if path.is_file():
                    value = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(value, dict) and value.get("review_id") == item["review_id"]:
                        reports[item["review_id"]] = value
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        return reports

    def _reference_candidates(self, query: str, *, head: bool) -> None:
        queue = self.server.app.reference_candidate_queue
        if queue is None:
            self._text(
                HTTPStatus.NOT_FOUND, "Reference discovery is not configured.",
                head=head,
            )
            return
        try:
            parameters = parse_qs(query)
            status = parameters.get("status", [None])[0]
            items = queue.list(status=status)
            histories = {}
            if (
                self.server.app.reference_discovery_service is not None
                and hasattr(
                    self.server.app.reference_discovery_service, "history"
                )
            ):
                histories = {
                    item["video_id"]: (
                        self.server.app.reference_discovery_service.history(
                            item["video_id"], limit=5
                        )
                    )
                    for item in items
                }
            body = render_reference_candidates(
                items, self.server.app.form_token,
                histories=histories,
                notice=parameters.get("success", parameters.get("error", [None]))[0],
                notice_error="error" in parameters,
            ).encode()
        except (
            ReferenceDecisionAuditError,
            ReferenceDiscoveryError,
            OSError,
            ValueError,
        ) as error:
            self._text(HTTPStatus.BAD_REQUEST, str(error), head=head)
            return
        self._headers(HTTPStatus.OK, "text/html; charset=utf-8", len(body))
        if not head:
            self.wfile.write(body)

    def _accepted_references(self, query: str, *, head: bool) -> None:
        service = self.server.app.reference_evidence_service
        if service is None:
            self._text(
                HTTPStatus.NOT_FOUND, "Accepted-reference evidence is not configured.",
                head=head,
            )
            return
        try:
            parameters = parse_qs(query)
            profile_name = parameters.get("profile", [None])[0]
            if profile_name is not None and not re.fullmatch(
                r"[a-z0-9][a-z0-9_-]{0,63}", profile_name
            ):
                raise ReferenceEvidenceError("Invalid profile filter.")
            items = service.list_accepted(profile_name=profile_name)
            body = render_accepted_references(
                items,
                self.server.app.form_token,
                notice=parameters.get(
                    "success", parameters.get("error", [None])
                )[0],
                notice_error="error" in parameters,
            ).encode()
        except (
            ReferenceAnnotationError,
            ReferenceEvidenceAuditError,
            ReferenceEvidenceError,
            ReferenceProfileError,
            OSError,
            ValueError,
        ) as error:
            self.log_error(
                "accepted reference page failed: %s", type(error).__name__
            )
            self._text(HTTPStatus.BAD_REQUEST, str(error), head=head)
            return
        self._headers(HTTPStatus.OK, "text/html; charset=utf-8", len(body))
        if not head:
            self.wfile.write(body)

    def _reference_analysis(self, reference_id: str, *, head: bool) -> None:
        service = self.server.app.reference_evidence_service
        if service is None:
            self._text(HTTPStatus.NOT_FOUND, "Reference analysis was not found.", head=head)
            return
        try:
            analysis = service.inspect(reference_id)["analysis"]
            if analysis is None:
                raise ReferenceEvidenceError("Reference has no analysis.")
            rendered = html.escape(json.dumps(analysis, indent=2, sort_keys=True))
            body = (
                "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
                "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
                f"<title>{_e(reference_id)} analysis</title></head><body>"
                f"<h1>{_e(reference_id)} sanitized analysis</h1>"
                "<p>Transcript findings are heuristics, not proof of humor, quality, "
                "originality, or virality.</p><p><a href=\"/references\">Return to "
                f"accepted references</a></p><pre>{rendered}</pre></body></html>"
            ).encode()
        except (
            ReferenceEvidenceAuditError,
            ReferenceEvidenceError,
            OSError,
            ValueError,
        ) as error:
            self.log_error(
                "accepted reference analysis failed: %s", type(error).__name__
            )
            self._text(HTTPStatus.NOT_FOUND, str(error), head=head)
            return
        self._headers(HTTPStatus.OK, "text/html; charset=utf-8", len(body))
        if not head:
            self.wfile.write(body)

    def _accepted_reference_media(self, reference_id: str, *, head: bool) -> None:
        service = self.server.app.reference_evidence_service
        if service is None or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", reference_id):
            self._text(HTTPStatus.NOT_FOUND, "Reference media was not found.", head=head)
            return
        try:
            entry = service.accepted_entry(reference_id)
            path = Path(entry["media_path"])
            if not path.is_file():
                raise OSError
            size = path.stat().st_size
        except (ReferenceEvidenceError, OSError):
            self._text(HTTPStatus.NOT_FOUND, "Reference media was not found.", head=head)
            return
        self._send_file(path, size, head=head)

    def _reference_media(self, video_id: str, *, head: bool) -> None:
        queue = self.server.app.reference_candidate_queue
        if queue is None or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", video_id):
            self._text(HTTPStatus.NOT_FOUND, "Candidate media was not found.", head=head)
            return
        try:
            item = queue.get(video_id)
            path_value = item.get("media_path")
            if not isinstance(path_value, str):
                raise OSError
            path = queue.resolve_media_path(path_value)
            size = path.stat().st_size
        except (ReferenceDiscoveryError, OSError):
            self._text(HTTPStatus.NOT_FOUND, "Candidate media was not found.", head=head)
            return
        self._send_file(path, size, head=head)

    def _send_file(self, path: Path, size: int, *, head: bool) -> None:
        start, end, partial = 0, max(0, size - 1), False
        range_header = self.headers.get("Range")
        if range_header:
            try:
                start, end = parse_range(range_header, size)
                partial = True
            except (ValueError, OverflowError):
                self._headers(
                    HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, "video/mp4", 0,
                    extra={"Content-Range": f"bytes */{size}"},
                )
                return
        length = end - start + 1
        extra = {"Accept-Ranges": "bytes"}
        if partial:
            extra["Content-Range"] = f"bytes {start}-{end}/{size}"
        self._headers(
            HTTPStatus.PARTIAL_CONTENT if partial else HTTPStatus.OK,
            "video/mp4", length, cache=False, extra=extra,
        )
        if not head:
            with path.open("rb") as stream:
                stream.seek(start)
                remaining = length
                while remaining:
                    chunk = stream.read(min(CHUNK_SIZE, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)

    def _media(self, review_id: str, *, head: bool) -> None:
        item = self.server.app.queue.find_by_review_id(review_id)
        if item is None:
            self._text(HTTPStatus.NOT_FOUND, "That review or preview was not found.", head=head)
            return
        path = Path(item["preview_path"])
        try:
            if not path.is_file():
                raise OSError
            size = path.stat().st_size
        except OSError:
            self._text(HTTPStatus.NOT_FOUND, "That review or preview was not found.", head=head)
            return
        start, end, partial = 0, max(0, size - 1), False
        range_header = self.headers.get("Range")
        if range_header:
            try:
                start, end = parse_range(range_header, size)
                partial = True
            except ValueError:
                self._headers(
                    HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE,
                    "text/plain; charset=utf-8", 0, cache=False,
                    extra={"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"},
                )
                return
        length = 0 if size == 0 else end - start + 1
        extra = {"Accept-Ranges": "bytes"}
        if partial:
            extra["Content-Range"] = f"bytes {start}-{end}/{size}"
        content_type = "video/mp4" if path.suffix.lower() == ".mp4" else "application/octet-stream"
        self._headers(
            HTTPStatus.PARTIAL_CONTENT if partial else HTTPStatus.OK,
            content_type, length, cache=False, extra=extra,
        )
        if head or length == 0:
            return
        try:
            with path.open("rb") as stream:
                stream.seek(start)
                remaining = length
                while remaining:
                    chunk = stream.read(min(CHUNK_SIZE, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (OSError, ConnectionError):
            return


class RequestError(ValueError):
    def __init__(self, status: HTTPStatus, message: str) -> None:
        self.status = status
        self.message = message
        super().__init__(message)


def parse_range(value: str, size: int) -> tuple[int, int]:
    if not value.startswith("bytes=") or "," in value or size <= 0:
        raise ValueError("invalid range")
    spec = value[6:]
    if "-" not in spec:
        raise ValueError("invalid range")
    first, last = spec.split("-", 1)
    if first:
        start = int(first)
        end = int(last) if last else size - 1
        if start < 0 or start >= size or end < start:
            raise ValueError("unsatisfiable range")
        return start, min(end, size - 1)
    suffix = int(last)
    if suffix <= 0:
        raise ValueError("invalid suffix")
    return max(0, size - suffix), size - 1


def _e(value: Any) -> str:
    return html.escape(str(value if value is not None else "—"), quote=True)


def render_index(
    items: list[dict[str, Any]], updated_at: str, form_token: str, *,
    maximum_duration: float = 60.0,
    notice: str | None = None, notice_error: bool = False,
    comparison_reports: dict[str, dict[str, Any]] | None = None,
) -> str:
    comparison_reports = comparison_reports or {}
    sections: list[str] = []
    for status in ("pending", "approved", "rejected"):
        cards = [
            _card(
                item, form_token, maximum_duration,
                comparison_reports.get(item["review_id"]),
            )
            for item in items if item["status"] == status
        ]
        sections.append(
            f'<section class="{status}"><h2>{status.title()}</h2>'
            f'{"".join(cards) or "<p>No clips in this section.</p>"}</section>'
        )
    empty = "<p class=\"empty\">No reviews match the selected filters.</p>" if not items else ""
    banner = (
        f'<p class="notice {"error" if notice_error else "success"}">{_e(notice)}</p>'
        if notice else ""
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CreatorFlow interactive local review</title>
<style>
body{{font:16px system-ui,sans-serif;max-width:1200px;margin:auto;padding:1rem;background:#f5f7fa;color:#17202a}}
header,.card{{background:white;border:1px solid #ccd3da;border-radius:.6rem;padding:1rem;margin:1rem 0}}
.card{{display:grid;grid-template-columns:minmax(240px,360px) 1fr;gap:1rem;border-left:8px solid #d6a700}}
.approved .card{{border-left-color:#22863a}}.rejected .card{{border-left-color:#c62828}}
video{{display:block;width:100%;aspect-ratio:9/16;background:#111}}textarea,input{{max-width:100%;box-sizing:border-box}}
textarea{{width:100%;min-height:5rem}}fieldset{{margin:.8rem 0;padding:.8rem}}label{{display:block;margin:.4rem 0}}
.timing-grid{{display:grid;grid-template-columns:repeat(2,minmax(8rem,1fr));gap:.5rem}}button{{padding:.55rem;margin:.2rem}}
.danger{{color:#a00}}.notice{{padding:.8rem;border:2px solid}}.success{{border-color:#22863a}}.error{{border-color:#c62828}}
@media(max-width:700px){{.card{{grid-template-columns:1fr}}.timing-grid{{grid-template-columns:1fr}}}}
</style></head><body>
<header><h1>CreatorFlow interactive local review</h1>
<p><a href="/reference-candidates">Review discovered reference candidates</a></p>
<p><a href="/references">Inspect and annotate accepted references</a></p>
<p>Queue updated: {_e(updated_at)}</p>
<form method="get" action="/"><label>Status <select name="status"><option value="">All</option>
<option>pending</option><option>approved</option><option>rejected</option></select></label>
<label>Video ID <input name="video_id"></label><button type="submit">Filter</button></form></header>
{banner}{empty}{''.join(sections)}
</body></html>
"""


def render_reference_candidates(
    items: list[dict[str, Any]], token: str, *,
    histories: dict[str, list[dict[str, Any]]] | None = None,
    notice: str | None = None, notice_error: bool = False,
) -> str:
    banner = (
        f'<p class="notice {"error" if notice_error else "success"}">{_e(notice)}</p>'
        if notice else ""
    )
    cards = []
    histories = histories or {}
    for item in sorted(items, key=lambda value: (value.get("rank", 999), value["video_id"])):
        video_id = quote(item["video_id"], safe="")
        revision = item.get("revision", 0)
        history = histories.get(item["video_id"], [])
        history_html = "".join(
            "<li>"
            f"{_e(event.get('timestamp'))}: "
            f"{_e(event.get('action'))} — {_e(event.get('result'))}; "
            f"{_e(event.get('previous_status'))} → "
            f"{_e(event.get('resulting_status'))}; "
            f"revision {_e(event.get('previous_revision'))} → "
            f"{_e(event.get('resulting_revision'))}; "
            f"reviewer {_e(event.get('reviewer') or 'not configured')}; "
            f"note {_e(event.get('note') or '—')}"
            + (
                f"; reason {_e(event.get('failure_reason'))}"
                if event.get("failure_reason") else ""
            )
            + "</li>"
            for event in history
        ) or "<li>No durable decision history exists yet.</li>"
        common = (
            f'<input type="hidden" name="form_token" value="{_e(token)}">'
            f'<input type="hidden" name="expected_revision" value="{_e(revision)}">'
        )
        category_topic = (
            '<label>Reference category'
            f'<input name="category" value="{_e(item.get("category") or "gaming_highlight")}">'
            '</label><label>Game/topic'
            f'<input name="topic" value="{_e(item.get("topic") or "unknown-gaming")}">'
            "</label>"
        )
        status = item.get("status")
        if status == "discovered":
            accept_request = secrets.token_urlsafe(16)
            decision_request = secrets.token_urlsafe(16)
            actions = f"""
<form method="post" action="/reference-candidates/{video_id}/decision">
{common}<input type="hidden" name="request_id" value="{_e(accept_request)}">
{category_topic}
<label>Acceptance note (optional)<textarea name="note" maxlength="{MAX_NOTE}"></textarea></label>
<button name="action" value="accept">Accept as reference</button>
</form>
<form method="post" action="/reference-candidates/{video_id}/decision">
{common}<input type="hidden" name="request_id" value="{_e(decision_request)}">
{category_topic}
<label>Decision reason (required)<textarea name="note" maxlength="{MAX_NOTE}" required></textarea></label>
<button name="action" value="reject">Reject</button>
<button name="action" value="duplicate">Mark duplicate</button>
</form>"""
        elif status == "accepted" or (
            status == "rejected"
            and item.get("accepted_reference_id") is not None
        ):
            request_id = secrets.token_urlsafe(16)
            actions = f"""
<form method="post" action="/reference-candidates/{video_id}/decision">
{common}<input type="hidden" name="request_id" value="{_e(request_id)}">
<fieldset><legend>Withdraw Reference</legend>
<p class="danger">Withdrawal removes this accepted reference from the strict
reference index, moves its accepted artifacts to local recovery storage, and
prevents future profile use. It does not delete the discovery preview.</p>
<label>Withdrawal reason (required)<textarea name="note" maxlength="{MAX_NOTE}" required></textarea></label>
<label><input type="checkbox" name="confirm_withdrawal" value="yes" required>
I confirm that this accepted reference should be withdrawn.</label>
<button name="action" value="withdraw">Withdraw Reference</button>
</fieldset></form>"""
        elif status in {"rejected", "duplicate"}:
            request_id = secrets.token_urlsafe(16)
            actions = f"""
<form method="post" action="/reference-candidates/{video_id}/decision">
{common}<input type="hidden" name="request_id" value="{_e(request_id)}">
{category_topic}
<p>Reconsider returns this candidate to discovered without accepting it.</p>
<button name="action" value="reconsider">Reconsider</button>
</form>"""
        else:
            actions = "<p>No state-changing action is available.</p>"
        media = (
            f'<video controls preload="metadata" src="/reference-media/{video_id}"></video>'
            if item.get("media_path") else
            '<p>No retained local preview. Use the source link for review.</p>'
        )
        ranking = item.get("ranking") or {}
        evidence = ranking.get("evidence") or "Ranking evidence unavailable."
        relevance = item.get("gaming_relevance") or {}
        source_quality = item.get("source_quality") or {}
        cards.append(
            f"""<article class="card"><div>{media}</div><div>
<h2>Rank {_e(item.get('rank'))}: {_e(item.get('title'))}</h2>
<p><strong>Status:</strong> {_e(item.get('status'))}<br>
<strong>Creator:</strong> {_e(item.get('creator'))}<br>
<strong>Published:</strong> {_e(item.get('published_at'))}<br>
<strong>Captured:</strong> {_e(item.get('captured_at'))}<br>
<strong>Views:</strong> {_e(item.get('view_count'))} ·
<strong>Likes:</strong> {_e(item.get('like_count'))} ·
<strong>Comments:</strong> {_e(item.get('comment_count'))}<br>
<strong>Topic:</strong> {_e(item.get('topic'))} ·
<strong>Cohort:</strong> {_e(item.get('cohort'))}<br>
<strong>Query:</strong> {_e(item.get('discovery_query'))}<br>
<strong>Score:</strong> {_e(item.get('score'))}<br>
<strong>Validation:</strong> {_e(item.get('validation_status'))} ·
media evidence={_e(item.get('media_verification'))}</p>
<p><strong>Gaming relevance:</strong> {_e(relevance.get('evidence') or 'Unavailable')}</p>
<p><strong>Source quality:</strong> {_e(source_quality.get('evidence') or 'Unavailable')}</p>
<p><strong>Why it ranked:</strong> {_e(evidence)}</p>
<p><strong>Media:</strong> {_e(item.get('verified_duration'))}s,
{_e(item.get('width'))}×{_e(item.get('height'))},
{_e(item.get('frame_rate'))} fps · video={_e(item.get('has_video'))} ·
audio={_e(item.get('has_audio'))}</p>
<p><strong>Transcript/structure:</strong> {_e(item.get('analysis_summary') or 'Unavailable until accepted analysis')}</p>
<p><a href="{_e(item.get('source_url'))}" rel="noreferrer">Open public source</a></p>
<p><strong>Revision:</strong> {_e(revision)}</p>
<section><h3>Recent decision history</h3><ul>{history_html}</ul></section>
{actions}</div></article>"""
        )
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CreatorFlow reference candidates</title>
<style>body{{font:16px system-ui;max-width:1200px;margin:auto;padding:1rem}}
.card{{display:grid;grid-template-columns:minmax(240px,360px) 1fr;gap:1rem;
border:1px solid #ccc;padding:1rem;margin:1rem 0}}video{{width:100%;aspect-ratio:9/16;
background:#111}}textarea{{display:block;width:100%;min-height:5rem}}
label{{display:block;margin:.6rem 0}}button{{padding:.5rem;margin:.2rem}}
.notice{{border:2px solid;padding:.7rem}}.error{{border-color:#b00}}
@media(max-width:700px){{.card{{grid-template-columns:1fr}}}}</style></head>
<body><header><h1>Discovered gaming reference candidates</h1>
<p><a href="/">Return to generated clip reviews</a></p>
<p><a href="/references">Inspect accepted references</a></p>
<p>Discovery scores are transparent heuristics, not measurements of humor,
quality, or virality. Nothing here influences profiles until a human accepts it.</p>
<form method="get"><label>Status <select name="status"><option value="">All</option>
<option>discovered</option><option>accepted</option><option>rejected</option>
<option>duplicate</option></select></label><button>Filter</button></form></header>
{banner}{''.join(cards) or '<p>No reference candidates match.</p>'}</body></html>"""


def render_accepted_references(
    items: list[dict[str, Any]], token: str, *,
    notice: str | None = None, notice_error: bool = False,
) -> str:
    banner = (
        f'<p class="notice {"error" if notice_error else "success"}">{_e(notice)}</p>'
        if notice else ""
    )
    labels = {
        "composition": "Composition",
        "facecam_presence": "Facecam presence",
        "opening_style": "Opening style",
        "clip_purpose": "Clip purpose",
        "pacing": "Pacing",
        "payoff_type": "Payoff type",
        "caption_style": "Caption style",
    }
    cards = []
    for item in sorted(
        items, key=lambda value: value["entry"]["reference_id"]
    ):
        entry = item["entry"]
        reference_id = entry["reference_id"]
        encoded_id = quote(reference_id, safe="")
        analysis = item.get("analysis") or {}
        media = analysis.get("media") or {}
        speech = analysis.get("speech") or {}
        transcription = analysis.get("transcription") or {}
        visual = analysis.get("visual_timing") or {}
        audio = analysis.get("audio_timing") or {}
        annotation = item["annotation"]
        values = annotation["annotations"]
        select_fields = []
        for name, choices in ENUM_FIELDS.items():
            options = "".join(
                f'<option value="{_e(choice)}"'
                f'{" selected" if values[name] == choice else ""}>'
                f'{_e(choice.replace("_", " ").title())}</option>'
                for choice in sorted(choices)
            )
            select_fields.append(
                f'<label>{_e(labels[name])}<select name="{_e(name)}" required>'
                f'{options}</select></label>'
            )
        common = (
            f'<input type="hidden" name="form_token" value="{_e(token)}">'
            f'<input type="hidden" name="expected_annotation_revision" '
            f'value="{_e(annotation["revision"])}">'
        )
        membership = item.get("profile_membership") or []
        membership_html = "".join(
            "<li>"
            f"{_e(value['profile_name'])}: "
            f"{_e(value.get('staleness', {}).get('status', 'unavailable'))}"
            "</li>"
            for value in membership
        ) or "<li>Not present in a built profile.</li>"
        history = item.get("history") or []
        history_html = "".join(
            "<li>"
            f"{_e(event.get('timestamp'))}: {_e(event.get('action'))} — "
            f"{_e(event.get('result'))}; annotation "
            f"{_e(event.get('previous_annotation_revision'))} → "
            f"{_e(event.get('new_annotation_revision'))}; analysis "
            f"{_e(event.get('previous_analysis_revision'))} → "
            f"{_e(event.get('new_analysis_revision'))}; reviewer "
            f"{_e(event.get('reviewer') or 'not configured')}"
            + (
                f"; reason {_e(event.get('failure_reason'))}"
                if event.get("failure_reason") else ""
            )
            + "</li>"
            for event in history
        ) or "<li>No evidence audit events yet.</li>"
        hook = speech.get("likely_hook") or {}
        payoff = speech.get("likely_payoff") or {}
        source_link = (
            f'<a href="{_e(item["source_url"])}" rel="noreferrer">Open source</a>'
            if item.get("source_url") else "Source URL unavailable"
        )
        annotation_request = secrets.token_urlsafe(16)
        analysis_request = secrets.token_urlsafe(16)
        profile_request = secrets.token_urlsafe(16)
        cards.append(f"""<article class="card">
<div><video controls preload="metadata"
src="/accepted-reference-media/{encoded_id}"></video></div><div>
<h2>{_e(item['baseline'].get('source_title') or reference_id)}</h2>
<p><strong>Reference:</strong> {_e(reference_id)} ·
<strong>Category:</strong> {_e(entry['profile_name'])}<br>
<strong>Creator:</strong> {_e(entry['creator'])} · {source_link}<br>
<strong>Media:</strong> {_e(media.get('duration'))}s,
{_e(media.get('width'))}×{_e(media.get('height'))},
{_e(media.get('frame_rate'))} fps<br>
<strong>Checksum:</strong> {_e('valid' if item['checksum_valid'] else 'invalid')}<br>
<strong>Analysis revision:</strong> {_e(analysis.get('analysis_revision', 0))} ·
<strong>Annotation revision:</strong> {_e(annotation['revision'])}</p>
<section><h3>Automatic evidence</h3>
<p><strong>Transcript:</strong> {_e(transcription.get('status', 'legacy/unavailable'))} ·
language={_e(speech.get('language'))} · words={_e(speech.get('word_count'))} ·
speech start={_e(speech.get('first_word_start'))} · end={_e(speech.get('last_word_end'))} ·
spoken span={_e(speech.get('spoken_duration'))}s ·
words/media-s={_e(speech.get('words_per_second'))} ·
words/spoken-s={_e(speech.get('words_per_spoken_second'))} ·
density={_e(speech.get('speech_density'))}</p>
<p><strong>Excerpt:</strong> {_e(speech.get('transcript_excerpt') or 'Unavailable')}</p>
<p><strong>Hook heuristic:</strong> {_e(hook.get('status', 'unavailable'))} at
{_e(hook.get('timestamp'))}s — {_e(hook.get('evidence', 'Unavailable'))}<br>
<strong>Payoff heuristic:</strong> {_e(payoff.get('status', 'unavailable'))} at
{_e(payoff.get('timestamp'))}s — {_e(payoff.get('evidence', 'Unavailable'))}<br>
<strong>Unresolved ending indicators:</strong>
{_e(', '.join(speech.get('unresolved_ending_indicators', [])) or 'none detected/unavailable')}<br>
<strong>Post-speech tail:</strong> {_e(speech.get('post_speech_tail'))}s ·
<strong>Post-payoff tail:</strong> {_e(speech.get('post_payoff_tail'))}s</p>
<p><strong>Meaningful speech pauses:</strong>
{_e(len(speech.get('meaningful_pauses', [])))} ·
<strong>Scene-change signals:</strong>
{_e(len(visual.get('scene_change_timestamps', [])))} ·
<strong>Silence intervals:</strong> {_e(len(audio.get('silence_intervals', [])))}</p>
<p>Transcript and timing findings are heuristic evidence. They do not measure humor,
quality, originality, or virality.</p>
<p><a href="/references/{encoded_id}/analysis">Inspect complete sanitized analysis</a></p>
</section>
<section><h3>Profile contribution</h3><ul>{membership_html}</ul></section>
<form method="post" action="/references/{encoded_id}/annotations">
{common}<input type="hidden" name="request_id" value="{_e(annotation_request)}">
<fieldset><legend>Human style annotations</legend>
{''.join(select_fields)}
<label>Desired qualities, one per line<textarea name="desired_qualities">{_e(chr(10).join(values['desired_qualities']))}</textarea></label>
<label>Undesirable qualities, one per line<textarea name="undesirable_qualities">{_e(chr(10).join(values['undesirable_qualities']))}</textarea></label>
<label>Reviewer notes<textarea name="reviewer_notes" maxlength="{MAX_NOTE}">{_e(values['reviewer_notes'])}</textarea></label>
<button>Save annotations</button></fieldset></form>
<form method="post" action="/references/{encoded_id}/reanalyze">
{common}<input type="hidden" name="request_id" value="{_e(analysis_request)}">
<p>Reanalysis uses local faster-whisper word timestamps and atomically preserves the
current analysis if it fails.</p><button>Reanalyze with transcription</button></form>
<form method="post" action="/references/{encoded_id}/rebuild-profile">
{common}<input type="hidden" name="request_id" value="{_e(profile_request)}">
<p>Profile rebuilding is explicit and does not change production defaults.</p>
<button>Rebuild {_e(entry['profile_name'])} profile</button></form>
<section><h3>Recent evidence history</h3><ul>{history_html}</ul></section>
</div></article>""")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CreatorFlow accepted references</title><style>
body{{font:16px system-ui;max-width:1280px;margin:auto;padding:1rem}}
.card{{display:grid;grid-template-columns:minmax(240px,360px) 1fr;gap:1rem;
border:1px solid #ccc;padding:1rem;margin:1rem 0}}video{{width:100%;aspect-ratio:9/16;
background:#111}}label{{display:block;margin:.6rem 0}}select,textarea{{display:block;
width:100%;max-width:44rem;box-sizing:border-box}}textarea{{min-height:5rem}}
button{{padding:.55rem;margin:.25rem}}.notice{{border:2px solid;padding:.7rem}}
.error{{border-color:#b00}}.success{{border-color:#285}}
@media(max-width:700px){{.card{{grid-template-columns:1fr}}}}</style></head>
<body><header><h1>Accepted reference evidence</h1>
<p><a href="/">Generated clip reviews</a> ·
<a href="/reference-candidates">Discovered reference candidates</a></p>
<p>Human annotations remain separate from automatic analysis. Profile rebuilds are
explicit and never alter production selection or rendering defaults.</p></header>
{banner}{''.join(cards) or '<p>No accepted references match.</p>'}</body></html>"""


def _card(
    item: dict[str, Any], token: str, maximum_duration: float,
    comparison: dict[str, Any] | None = None,
) -> str:
    review_id = quote(str(item["review_id"]), safe="")
    adjusted = bool(
        item["timing_revision"]
        and (item["render_start"] != item["candidate_start"] or item["render_end"] != item["candidate_end"])
    )
    pending_adjusted = (
        "<p><strong>Adjusted preview pending another review.</strong></p>"
        if adjusted and item["status"] == "pending" else ""
    )
    hidden = f'<input type="hidden" name="form_token" value="{_e(token)}">'
    comparison_html = _comparison_section(comparison)
    return f"""<article class="card"><div>
<video controls preload="metadata" src="/media/{review_id}">Your browser cannot play this local preview.</video>
</div><div><h3>Rank {_e(item['candidate_rank'])} · score {_e(item['candidate_score'])}</h3>
<p><strong>Review ID:</strong> {_e(item['review_id'])}<br>
<strong>Status:</strong> {_e(item['status'])}<br><strong>Video ID:</strong> {_e(item['video_id'])}</p>
<p><strong>Complete candidate text:</strong> {_e(item['candidate_text'])}</p>
<p><strong>Original candidate:</strong> {_e(item['candidate_start'])}–{_e(item['candidate_end'])}s
({_e(item['candidate_duration'])}s)<br><strong>Current render:</strong>
{_e(item['render_start'])}–{_e(item['render_end'])}s ({_e(item['render_duration'])}s)<br>
<strong>Lead-in:</strong> {_e(item['lead_in_seconds'])}s · <strong>Tail:</strong> {_e(item['tail_seconds'])}s<br>
<strong>Timing revision:</strong> {_e(item['timing_revision'])} ·
<strong>Timing updated:</strong> {_e(item['timing_updated_at'])}<br>
<strong>Timing source:</strong> {_e(item['timing_source'])} ·
<strong>Context profile:</strong> {_e(item['context_profile'])}<br>
<strong>Expansion reasons:</strong> {_e('; '.join(item['context_reasons']) or '—')}<br>
<strong>Timing-adjusted:</strong> {'Yes' if adjusted else 'No'} ·
<strong>Later manually adjusted:</strong> {'Yes' if item['timing_source'] == 'manual' else 'No'}<br>
<strong>Preview metadata path:</strong> {_e(item['preview_metadata_path'])}</p>{pending_adjusted}
{comparison_html}
<form method="post" action="/reviews/{review_id}/compare-reference"><fieldset>
<legend>Reference comparison</legend>{hidden}
<p>Runs measurable and transcript-heuristic comparison locally. It does not rerender or change review state.</p>
<button type="submit">Compare to Reference Profile</button></fieldset></form>
<form method="post" action="/reviews/{review_id}/decision"><fieldset><legend>Review decision</legend>{hidden}
<label>Review note (maximum {MAX_NOTE} characters)
<textarea name="note" maxlength="{MAX_NOTE}">{_e(item['review_note'] or '')}</textarea></label>
<label><input type="checkbox" name="clear_note" value="1"> Clear the existing note</label>
<button name="action" value="approve">Approve</button>
<button name="action" value="reject">Reject</button>
<span class="danger">Reject marks this clip unsuitable.</span>
<button name="action" value="pending">Return to Pending</button></fieldset></form>
<form method="post" action="/reviews/{review_id}/adjust"><fieldset><legend>Timing adjustment</legend>{hidden}
<p>Rendering runs synchronously and may take some time. Relative values use immutable candidate timing.
Use either relative or absolute fields, not both.</p><div class="timing-grid">
<label>Lead-in seconds <input name="lead_in" type="number" min="0" step="0.001"></label>
<label>Tail seconds <input name="tail" type="number" min="0" step="0.001"></label>
<label>Absolute render start <input name="render_start" type="number" min="0" step="0.001"></label>
<label>Absolute render end <input name="render_end" type="number" min="0" step="0.001"></label>
<label>Maximum duration <input name="maximum_duration" type="number" min="0.001" step="0.001" value="{_e(maximum_duration)}"></label>
<label><input type="checkbox" name="allow_longer" value="1"> Allow longer than maximum</label></div>
<button type="submit">Rerender with adjusted timing</button></fieldset></form>
<form method="post" action="/reviews/{review_id}/reset-timing"><fieldset><legend>Reset timing</legend>{hidden}
<p class="danger">This rerenders and replaces the preview using the original candidate range.</p>
<button type="submit">Reset to candidate timing</button></fieldset></form>
<form method="post" action="/reviews/{review_id}/reapply-context"><fieldset>
<legend>Automatic context</legend>{hidden}
<p>Rerenders synchronously using the reaction profile and returns this item to pending.</p>
<button type="submit">Reapply Automatic Context</button></fieldset></form>
</div></article>"""


def _comparison_section(report: dict[str, Any] | None) -> str:
    if not report:
        return "<section><h4>Reference comparison</h4><p>No local comparison report exists.</p></section>"
    findings = report.get("findings", {})
    labels = (
        ("duration_fit", "Duration"), ("opening_context", "Opening context"),
        ("payoff_completion", "Payoff completion"), ("ending_tail", "Ending tail"),
        ("layout", "Layout"),
    )
    rows = "".join(
        f"<li><strong>{_e(label)}:</strong> {_e(findings.get(key, {}).get('status'))}"
        f" — {_e(findings.get(key, {}).get('evidence'))}</li>"
        for key, label in labels
    )
    return (
        "<section><h4>Reference comparison</h4>"
        f"<p><strong>Reference profile:</strong> {_e(report.get('profile_name'))}<br>"
        f"<strong>Profile confidence:</strong> {_e(report.get('profile_confidence'))}</p>"
        f"<ul>{rows}</ul></section>"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve the interactive local clip-review page.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=port_number, default=8080)
    parser.add_argument("--allow-non-loopback", action="store_true")
    parser.add_argument("--review-queue-path", type=Path, default=DEFAULT_REVIEW_QUEUE_PATH)
    parser.add_argument("--manifest-path", type=Path, default=DEFAULT_MANIFEST_PATH)
    parser.add_argument("--output-directory", type=Path, default=DEFAULT_PREVIEW_DIRECTORY)
    parser.add_argument("--maximum-render-duration", type=positive_number, default=60)
    parser.add_argument("--no-captions", action="store_true")
    parser.add_argument("--width", type=even_integer, default=1080)
    parser.add_argument("--height", type=even_integer, default=1920)
    parser.add_argument("--frame-rate", type=positive_number, default=30)
    parser.add_argument("--crf", type=crf_value, default=20)
    parser.add_argument("--preset", choices=SAFE_PRESETS, default="medium")
    parser.add_argument("--caption-font", default="DejaVu Sans")
    parser.add_argument("--caption-font-size", type=positive_integer, default=62)
    parser.add_argument("--caption-max-words", type=positive_integer, default=6)
    parser.add_argument("--caption-max-characters", type=positive_integer, default=34)
    parser.add_argument("--caption-max-duration", type=positive_number, default=2.5)
    parser.add_argument("--ffmpeg-path", default="ffmpeg")
    parser.add_argument("--ffprobe-path", default="ffprobe")
    parser.add_argument("--reference-profile", default="personality_reaction")
    return parser


def create_application(args: argparse.Namespace) -> ReviewApplication:
    queue = ClipReviewQueue(args.review_queue_path)
    renderer = VideoPreviewRenderer(
        manifest_path=args.manifest_path, output_directory=args.output_directory,
        configuration=RenderConfiguration(
            width=args.width, height=args.height, frame_rate=args.frame_rate,
            crf=args.crf, preset=args.preset, captions_enabled=not args.no_captions,
        ),
        caption_configuration=CaptionConfiguration(
            font_name=args.caption_font, font_size=args.caption_font_size,
            maximum_words=args.caption_max_words,
            maximum_characters=args.caption_max_characters,
            maximum_duration_seconds=args.caption_max_duration,
        ),
        ffmpeg_path=args.ffmpeg_path, ffprobe_path=args.ffprobe_path,
    )
    service = ClipTimingAdjustmentService(
        queue, renderer, maximum_duration=args.maximum_render_duration
    )
    reference_library = ReferenceClipLibrary()
    annotation_store = ReferenceAnnotationStore()
    profile_builder = ReferenceProfileBuilder(
        reference_library, annotation_store=annotation_store
    )
    comparator = ReferenceClipComparator(profile_builder)
    candidate_queue = ReferenceCandidateQueue()
    discovery_service = ReferenceDiscoveryService(
        YouTubeDataAPI(), candidate_queue
    )
    evidence_service = ReferenceEvidenceService(
        reference_library,
        annotations=annotation_store,
        profile_builder=profile_builder,
    )
    return ReviewApplication(
        queue, service, secrets.token_urlsafe(32), args.maximum_render_duration,
        comparator, args.reference_profile, candidate_queue, discovery_service,
        evidence_service,
    )


def main(argv: Sequence[str] | None = ()) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not is_loopback_host(args.host) and not args.allow_non_loopback:
        parser.error("refusing a non-loopback bind; use --allow-non-loopback only with care")
    if not is_loopback_host(args.host):
        print(
            "WARNING: NON-LOOPBACK BIND ENABLED. This local administrative tool has no "
            "authentication; do not expose it publicly.",
            file=sys.stderr,
        )
    try:
        app = create_application(args)
        server = ReviewHTTPServer((args.host, args.port), app)
    except (OSError, ReviewQueueError, ValueError) as error:
        print(f"Review server configuration failed: {error}", file=sys.stderr)
        return 1
    host, port = server.server_address[:2]
    display_host = f"[{host}]" if ":" in host else host
    print(f"CreatorFlow review server: http://{display_host}:{port}/")
    print(f"SSH tunnel example: ssh -L 8080:{args.host}:{port} user@server")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping CreatorFlow review server.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(None))
