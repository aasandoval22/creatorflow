from __future__ import annotations

import http.client
import io
import json
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from backend.app import review_server
from backend.services.clip_review_queue import ClipReviewQueue, ReviewQueueError
from backend.services.reference_discovery import ReferenceCandidateQueue


def candidate(identifier: str = "candidate_1", *, rank: int = 1, score: float = 90) -> dict:
    return {
        "candidate_id": identifier, "rank": rank, "score": score,
        "start": 10.0, "end": 20.0, "duration": 10.0,
        "text": 'Complete <candidate> & "text"',
    }


class FakeTimingService:
    maximum_duration = 60.0

    def __init__(self, queue: ClipReviewQueue, *, fail: bool = False) -> None:
        self.queue = queue
        self.fail = fail
        self.calls = []

    def adjust(self, review_id: str, **kwargs):
        self.calls.append(("adjust", review_id, kwargs))
        if self.fail:
            raise ReviewQueueError("synthetic render failure")
        item = self.queue.find_by_review_id(review_id)
        lead = kwargs.get("lead_in")
        tail = kwargs.get("tail")
        start = kwargs.get("render_start")
        end = kwargs.get("render_end")
        if (lead is not None or tail is not None) and (start is not None or end is not None):
            raise ReviewQueueError("Relative and absolute timing adjustments are mutually exclusive.")
        if lead is not None or tail is not None:
            start = item["candidate_start"] - (lead or 0)
            end = item["candidate_end"] + (tail or 0)
        if start is None or end is None:
            raise ReviewQueueError("At least one timing adjustment is required.")
        updated = self.queue.update_timing(
            review_id, render_start=start, render_end=end,
            preview_path=item["preview_path"], preview_metadata_path=item["preview_metadata_path"],
            note=kwargs.get("note"), clear_note=kwargs.get("clear_note", False),
        )
        return SimpleNamespace(item=updated, render_start=start, render_end=end)

    def reset(self, review_id: str, **kwargs):
        item = self.queue.find_by_review_id(review_id)
        return self.adjust(
            review_id, render_start=item["candidate_start"], render_end=item["candidate_end"],
            **kwargs,
        )


@contextmanager
def running_server(tmp_path: Path, *, items: int = 1, fail_timing: bool = False):
    queue = ClipReviewQueue(tmp_path / "reviews.json")
    for index in range(items):
        preview = tmp_path / f"preview-{index}.mp4"
        preview.write_bytes(b"0123456789abcdef")
        queue.add_or_update_preview(
            f"video_{index}", candidate(f"candidate_{index}", rank=index + 1, score=90 - index),
            preview, tmp_path / f"preview-{index}.json",
        )
    service = FakeTimingService(queue, fail=fail_timing)
    app = review_server.ReviewApplication(queue, service, "test-token", 60)
    server = review_server.ReviewHTTPServer(("127.0.0.1", 0), app)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield server, queue, service
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def request(server, method: str, path: str, body=None, headers=None):
    connection = http.client.HTTPConnection(*server.server_address)
    connection.request(method, path, body=body, headers=headers or {})
    response = connection.getresponse()
    data = response.read()
    result = response.status, dict(response.getheaders()), data
    connection.close()
    return result


def post(server, path: str, fields: list[tuple[str, str]] | dict[str, str]):
    body = urlencode(fields)
    return request(
        server, "POST", path, body,
        {"Content-Type": "application/x-www-form-urlencoded", "Content-Length": str(len(body))},
    )


def review_id(queue):
    return queue.list_items()[0]["review_id"]


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost", "127.12.4.9"])
def test_loopback_hosts_are_accepted(host):
    assert review_server.is_loopback_host(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.2", "example.test"])
def test_non_loopback_hosts_are_rejected(host):
    assert not review_server.is_loopback_host(host)


def test_parser_defaults_and_explicit_argv():
    args = review_server.build_parser().parse_args([])
    assert (args.host, args.port, args.maximum_render_duration) == ("127.0.0.1", 8080, 60)
    assert review_server.build_parser().parse_args(["--port", "9090"]).port == 9090


@pytest.mark.parametrize("port", ["0", "-1", "65536", "not-a-port"])
def test_invalid_ports(port):
    with pytest.raises(SystemExit):
        review_server.build_parser().parse_args(["--port", port])


def test_main_refuses_non_loopback(monkeypatch):
    with pytest.raises(SystemExit):
        review_server.main(["--host", "0.0.0.0"])


def test_main_warns_for_explicit_non_loopback(monkeypatch, capsys):
    class Server:
        server_address = ("0.0.0.0", 8080)
        def serve_forever(self): raise KeyboardInterrupt
        def server_close(self): pass
    monkeypatch.setattr(review_server, "create_application", lambda args: object())
    monkeypatch.setattr(review_server, "ReviewHTTPServer", lambda address, app: Server())
    assert review_server.main(["--host", "0.0.0.0", "--allow-non-loopback"]) == 0
    assert "WARNING: NON-LOOPBACK" in capsys.readouterr().err


def test_empty_index_and_security_headers(tmp_path):
    with running_server(tmp_path, items=0) as (server, _, _):
        status, headers, body = request(server, "GET", "/")
    text = body.decode()
    assert status == 200
    assert "No reviews match" in text
    assert "Pending" in text and "Approved" in text and "Rejected" in text
    assert headers["Cache-Control"] == "no-store"
    assert "default-src 'self'" in headers["Content-Security-Policy"]
    assert headers["X-Frame-Options"] == "DENY"


def test_index_metadata_escaping_forms_and_no_external_resources(tmp_path):
    with running_server(tmp_path) as (server, queue, _):
        status, _, body = request(server, "GET", "/")
        item = queue.list_items()[0]
    text = body.decode()
    assert status == 200
    assert "Complete &lt;candidate&gt; &amp; &quot;text&quot;" in text
    for label in ("Original candidate", "Current render", "Timing revision",
                  "Preview metadata path", "Review note", "Lead-in", "Tail"):
        assert label in text
        assert text.count('name="form_token" value="test-token"') == 5
    assert "autoplay" not in text and "https://" not in text and "<script" not in text
    assert f"/media/{item['review_id']}" in text
    assert "Compare to Reference Profile" in text


class FakeComparator:
    def __init__(self, root: Path, *, fail: bool = False):
        self.root = root
        self.fail = fail

    def report_path(self, profile, review_id):
        return self.root / profile / f"{review_id}.json"

    def compare(self, profile, item, *, write=False):
        if self.fail:
            raise ValueError("synthetic comparison failure")
        report = {
            "review_id": item["review_id"], "profile_name": profile,
            "profile_confidence": "provisional", "findings": {
                name: {"status": "known", "evidence": f"{name} evidence"}
                for name in ("duration_fit", "opening_context", "payoff_completion",
                             "ending_tail", "layout")
            },
        }
        if write:
            path = self.report_path(profile, item["review_id"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(report), encoding="utf-8")
        return report


def test_reference_comparison_post_preserves_review_state_and_displays_report(tmp_path):
    with running_server(tmp_path) as (server, queue, _):
        server.app.reference_comparator = FakeComparator(tmp_path / "comparisons")
        rid = review_id(queue)
        before = queue.find_by_review_id(rid)
        status, headers, _ = post(
            server, f"/reviews/{rid}/compare-reference",
            {"form_token": "test-token"},
        )
        after = queue.find_by_review_id(rid)
        _, _, body = request(server, "GET", "/")
    assert status == 303 and "compared+locally" in headers["Location"]
    assert before == after
    text = body.decode()
    assert "personality_reaction" in text and "provisional" in text
    assert "duration_fit evidence" in text


def test_failed_reference_comparison_preserves_review_state(tmp_path):
    with running_server(tmp_path) as (server, queue, _):
        server.app.reference_comparator = FakeComparator(tmp_path, fail=True)
        rid = review_id(queue)
        before = queue.find_by_review_id(rid)
        status, headers, _ = post(
            server, f"/reviews/{rid}/compare-reference",
            {"form_token": "test-token"},
        )
        after = queue.find_by_review_id(rid)
    assert status == 303 and "error=" in headers["Location"]
    assert before == after


class FakeReferenceDiscovery:
    def __init__(self, queue):
        self.queue = queue
        self.accepted = []

    def accept(self, video_id, *, category, notes, transcription, topic=None):
        self.accepted.append(
            (video_id, category, notes, transcription, topic)
        )
        self.queue.decide(
            video_id, "accepted", notes=notes, category=category,
            topic=topic,
            accepted_reference_id=f"youtube-{video_id}",
        )


def reference_candidate(tmp_path):
    media = (
        tmp_path / "reference_discovery" / "media"
        / "reference-candidate.mp4"
    )
    media.parent.mkdir(parents=True, exist_ok=True)
    media.write_bytes(b"reference-media")
    return {
        "video_id": "short_one",
        "title": "Gaming & reaction", "creator": "Creator",
        "published_at": "2026-07-20T00:00:00Z",
        "captured_at": "2026-07-25T00:00:00Z",
        "view_count": 1000, "like_count": 50, "comment_count": 4,
        "duration": 42, "verified_duration": 42, "width": 1080,
        "height": 1920, "frame_rate": 60, "has_video": True,
        "has_audio": True, "source_url": "https://www.youtube.com/watch?v=short_one",
        "discovery_query": "gaming shorts", "topic": "gaming",
        "cohort": "established", "validation_status": "media-verified",
        "media_verification": "verified",
        "gaming_relevance": {"evidence": "YouTube category 20 (Gaming)."},
        "source_quality": {"evidence": "No derivative markers."},
        "score": 80, "rank": 1,
        "ranking": {"evidence": "Transparent ranking evidence."},
        "media_path": str(media),
    }


def test_reference_candidate_page_token_media_and_decisions(tmp_path):
    with running_server(tmp_path) as (server, _, _):
        queue = ReferenceCandidateQueue(tmp_path / "reference-candidates.json")
        queue.upsert_discovered([reference_candidate(tmp_path)])
        assert queue.get("short_one")["media_path"] == (
            "reference_discovery/media/reference-candidate.mp4"
        )
        service = FakeReferenceDiscovery(queue)
        server.app.reference_candidate_queue = queue
        server.app.reference_discovery_service = service
        status, _, body = request(server, "GET", "/reference-candidates")
        media_status, _, media_body = request(
            server, "GET", "/reference-media/short_one"
        )
        rejected, headers, _ = post(
            server, "/reference-candidates/short_one/decision",
            {
                "form_token": "test-token", "action": "reject",
                "category": "gaming_highlight", "topic": "manual-game",
                "note": "repost",
            },
        )
        assert queue.get("short_one")["status"] == "rejected"
        assert queue.get("short_one")["topic"] == "manual-game"
        queue.decide("short_one", "discovered")
        accepted, _, _ = post(
            server, "/reference-candidates/short_one/decision",
            {
                "form_token": "test-token", "action": "accept",
                "category": "personality_reaction", "topic": "roblox",
                "note": "complete beat",
            },
        )
    text = body.decode()
    assert status == 200
    assert "Gaming &amp; reaction" in text
    assert 'name="form_token" value="test-token"' in text
    assert "Transparent ranking evidence" in text
    assert "media-verified" in text
    assert 'name="topic" value="gaming"' in text
    assert media_status == 200 and media_body == b"reference-media"
    assert rejected == 303 and "reference-candidates" in headers["Location"]
    assert accepted == 303
    assert service.accepted == [
        ("short_one", "personality_reaction", "complete beat", False, "roblox")
    ]
    assert queue.get("short_one")["status"] == "accepted"
    assert queue.get("short_one")["topic"] == "roblox"


def test_reference_candidate_form_rejects_bad_token(tmp_path):
    with running_server(tmp_path) as (server, _, _):
        queue = ReferenceCandidateQueue(tmp_path / "reference-candidates.json")
        queue.upsert_discovered([reference_candidate(tmp_path)])
        server.app.reference_candidate_queue = queue
        status, _, _ = post(
            server, "/reference-candidates/short_one/decision",
            {"form_token": "wrong", "action": "reject"},
        )
    assert status == 403
    assert queue.get("short_one")["status"] == "discovered"


def test_reference_candidate_form_rejects_invalid_manual_topic(tmp_path):
    with running_server(tmp_path) as (server, _, _):
        queue = ReferenceCandidateQueue(tmp_path / "reference-candidates.json")
        queue.upsert_discovered([reference_candidate(tmp_path)])
        server.app.reference_candidate_queue = queue
        status, _, _ = post(
            server, "/reference-candidates/short_one/decision",
            {
                "form_token": "test-token",
                "action": "reject",
                "topic": "../not-a-topic",
            },
        )
    assert status == 400
    assert queue.get("short_one")["status"] == "discovered"


def test_reference_media_cannot_escape_discovery_directory(tmp_path):
    outside = tmp_path.parent / "outside-reference-candidate.mp4"
    outside.write_bytes(b"must-not-be-served")
    try:
        with running_server(tmp_path) as (server, _, _):
            queue = ReferenceCandidateQueue(tmp_path / "reference-candidates.json")
            item = reference_candidate(tmp_path)
            item["media_path"] = str(outside)
            queue.path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "updated_at": "2026-07-25T00:00:00+00:00",
                        "items": [{**item, "status": "discovered"}],
                    }
                ),
                encoding="utf-8",
            )
            server.app.reference_candidate_queue = queue
            status, _, body = request(
                server, "GET", "/reference-media/short_one"
            )
        assert status == 404
        assert b"must-not-be-served" not in body
    finally:
        outside.unlink(missing_ok=True)


def test_sections_sort_and_filters(tmp_path):
    with running_server(tmp_path, items=3) as (server, queue, _):
        ids = [item["review_id"] for item in queue.list_items()]
        queue.approve(ids[1])
        queue.reject(ids[2])
        _, _, body = request(server, "GET", "/?status=approved")
        text = body.decode()
        assert ids[1] in text and ids[0] not in text and ids[2] not in text
        _, _, body = request(server, "GET", "/?video_id=video_0")
        assert ids[0] in body.decode() and ids[1] not in body.decode()


@pytest.mark.parametrize("action,status", [
    ("approve", "approved"), ("reject", "rejected"), ("pending", "pending"),
])
def test_decisions_redirect_and_preserve_metadata(tmp_path, action, status):
    with running_server(tmp_path) as (server, queue, _):
        rid = review_id(queue)
        before = queue.find_by_review_id(rid)
        result, headers, _ = post(
            server, f"/reviews/{rid}/decision",
            {"form_token": "test-token", "action": action, "note": "reason"},
        )
        after = queue.find_by_review_id(rid)
    assert result == 303 and headers["Location"].startswith("/?success=")
    assert after["status"] == status and after["review_note"] == "reason"
    assert (after["reviewed_at"] is None) == (status == "pending")
    for field in ("candidate_start", "candidate_end", "preview_path", "render_start", "timing_revision"):
        assert after[field] == before[field]


def test_note_preserve_replace_and_clear(tmp_path):
    with running_server(tmp_path) as (server, queue, _):
        rid = review_id(queue)
        queue.update_note(rid, "old")
        post(server, f"/reviews/{rid}/decision", {
            "form_token": "test-token", "action": "approve", "note": "",
        })
        assert queue.find_by_review_id(rid)["review_note"] == "old"
        post(server, f"/reviews/{rid}/decision", {
            "form_token": "test-token", "action": "reject", "note": "new",
        })
        assert queue.find_by_review_id(rid)["review_note"] == "new"
        post(server, f"/reviews/{rid}/decision", {
            "form_token": "test-token", "action": "pending", "clear_note": "1",
        })
        assert queue.find_by_review_id(rid)["review_note"] is None


@pytest.mark.parametrize("fields,expected", [
    ({"action": "approve"}, 403),
    ({"action": "approve", "form_token": "wrong"}, 403),
    ({"action": "invalid", "form_token": "test-token"}, 400),
])
def test_invalid_decision_forms(tmp_path, fields, expected):
    with running_server(tmp_path) as (server, queue, _):
        status, _, _ = post(server, f"/reviews/{review_id(queue)}/decision", fields)
    assert status == expected


def test_duplicate_token_missing_review_content_type_and_oversize(tmp_path):
    with running_server(tmp_path) as (server, queue, _):
        rid = review_id(queue)
        assert post(server, f"/reviews/{rid}/decision", [
            ("form_token", "test-token"), ("form_token", "test-token"), ("action", "approve"),
        ])[0] == 400
        assert post(server, "/reviews/review_missing/decision", {
            "form_token": "test-token", "action": "approve",
        })[0] == 404
        assert request(server, "POST", f"/reviews/{rid}/decision", "x", {
            "Content-Type": "application/json", "Content-Length": "1",
        })[0] == 415
        assert request(server, "POST", f"/reviews/{rid}/decision", "", {
            "Content-Type": "application/x-www-form-urlencoded",
            "Content-Length": str(review_server.MAX_BODY + 1),
        })[0] == 413


@pytest.mark.parametrize("fields,start,end", [
    ({"lead_in": "2"}, 8, 20), ({"tail": "3"}, 10, 23),
    ({"lead_in": "2", "tail": "3"}, 8, 23),
    ({"render_start": "9", "render_end": "22"}, 9, 22),
])
def test_timing_adjustments(tmp_path, fields, start, end):
    with running_server(tmp_path) as (server, queue, service):
        rid = review_id(queue)
        form = {"form_token": "test-token", "maximum_duration": "60", **fields}
        status, headers, _ = post(server, f"/reviews/{rid}/adjust", form)
        item = queue.find_by_review_id(rid)
    assert status == 303 and headers["Location"].startswith("/?success=")
    assert (item["render_start"], item["render_end"], item["status"]) == (start, end, "pending")
    assert item["timing_revision"] == 1 and service.calls


def test_timing_validation_reset_note_and_failure_preservation(tmp_path):
    with running_server(tmp_path) as (server, queue, _):
        rid = review_id(queue)
        queue.update_note(rid, "keep")
        both = {"form_token": "test-token", "maximum_duration": "60", "lead_in": "1",
                "render_start": "9", "render_end": "20"}
        assert post(server, f"/reviews/{rid}/adjust", both)[0] == 303
        assert queue.find_by_review_id(rid)["timing_revision"] == 0
        assert post(server, f"/reviews/{rid}/adjust", {
            "form_token": "test-token", "maximum_duration": "5", "tail": "1",
        })[0] == 400
        post(server, f"/reviews/{rid}/adjust", {
            "form_token": "test-token", "maximum_duration": "5", "tail": "1",
            "allow_longer": "1",
        })
        assert queue.find_by_review_id(rid)["review_note"] == "keep"
        assert post(server, f"/reviews/{rid}/reset-timing", {
            "form_token": "test-token",
        })[0] == 303
        item = queue.find_by_review_id(rid)
        assert (item["render_start"], item["render_end"]) == (10, 20)
    with running_server(tmp_path / "failure", fail_timing=True) as (server, queue, _):
        rid = review_id(queue)
        before = queue.find_by_review_id(rid)
        assert post(server, f"/reviews/{rid}/adjust", {
            "form_token": "test-token", "maximum_duration": "60", "tail": "1",
        })[0] == 303
        assert queue.find_by_review_id(rid) == before


def test_media_full_head_and_ranges(tmp_path):
    with running_server(tmp_path) as (server, queue, _):
        path = f"/media/{review_id(queue)}"
        status, headers, body = request(server, "GET", path)
        assert (status, body) == (200, b"0123456789abcdef")
        assert headers["Content-Type"] == "video/mp4" and headers["Accept-Ranges"] == "bytes"
        status, headers, body = request(server, "HEAD", path)
        assert status == 200 and body == b"" and headers["Content-Length"] == "16"
        for value, expected, content_range in [
            ("bytes=2-5", b"2345", "bytes 2-5/16"),
            ("bytes=10-", b"abcdef", "bytes 10-15/16"),
            ("bytes=-4", b"cdef", "bytes 12-15/16"),
        ]:
            status, headers, body = request(server, "GET", path, headers={"Range": value})
            assert status == 206 and body == expected and headers["Content-Range"] == content_range
        for value in ("bytes=99-100", "units=0-1", "bytes=2-1", "bytes=0-1,3-4"):
            status, headers, _ = request(server, "GET", path, headers={"Range": value})
            assert status == 416 and headers["Content-Range"] == "bytes */16"


def test_media_only_serves_queue_references_and_methods(tmp_path):
    with running_server(tmp_path) as (server, queue, _):
        rid = review_id(queue)
        assert request(server, "GET", "/media/review_missing")[0] == 404
        assert request(server, "GET", f"/media/{rid}/../../README.md")[0] == 404
        assert request(server, "POST", f"/media/{rid}")[0] == 405
        Path(queue.find_by_review_id(rid)["preview_path"]).unlink()
        assert request(server, "GET", f"/media/{rid}")[0] == 404


def test_write_routes_reject_get_and_unknown_routes(tmp_path):
    with running_server(tmp_path) as (server, queue, _):
        rid = review_id(queue)
        assert request(server, "GET", f"/reviews/{rid}/decision")[0] == 405
        assert request(server, "GET", "/unknown")[0] == 404
