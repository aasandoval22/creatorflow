"""Loopback-only interactive HTTP server for the local clip-review queue."""

from __future__ import annotations

import argparse
import hmac
import html
import ipaddress
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
        elif route.path.startswith("/media/"):
            self._media(route.path.removeprefix("/media/"), head=False)
        elif self._write_route(route.path):
            self._text(HTTPStatus.METHOD_NOT_ALLOWED, "This route accepts POST only.")
        else:
            self._text(HTTPStatus.NOT_FOUND, "The requested local resource was not found.")

    def do_HEAD(self) -> None:
        route = urlsplit(self.path)
        if route.path == "/":
            self._index(route.query, head=True)
        elif route.path.startswith("/media/"):
            self._media(route.path.removeprefix("/media/"), head=True)
        elif self._write_route(route.path):
            self._text(HTTPStatus.METHOD_NOT_ALLOWED, "This route accepts POST only.", head=True)
        else:
            self._text(HTTPStatus.NOT_FOUND, "The requested local resource was not found.", head=True)

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
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
            else:
                message = self._reapply_context(review_id, form)
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
            "decision", "adjust", "reset-timing", "reapply-context"
        }:
            review_id = parts[1]
            if review_id and "/" not in review_id and review_id.startswith("review_"):
                return review_id, parts[2]
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
) -> str:
    sections: list[str] = []
    for status in ("pending", "approved", "rejected"):
        cards = [
            _card(item, form_token, maximum_duration)
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
<p>Queue updated: {_e(updated_at)}</p>
<form method="get" action="/"><label>Status <select name="status"><option value="">All</option>
<option>pending</option><option>approved</option><option>rejected</option></select></label>
<label>Video ID <input name="video_id"></label><button type="submit">Filter</button></form></header>
{banner}{empty}{''.join(sections)}
</body></html>
"""


def _card(item: dict[str, Any], token: str, maximum_duration: float) -> str:
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
    return ReviewApplication(queue, service, secrets.token_urlsafe(32), args.maximum_render_duration)


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
