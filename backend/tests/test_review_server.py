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
from backend.services.reference_annotations import default_annotation_values
from backend.services.reference_discovery import ReferenceCandidateQueue
from backend.tests.test_reference_evidence import environment as evidence_environment


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


class FakeTikTokPublications:
    def __init__(self):
        self.calls = []
        self.attempt = None

    def connection(self):
        return {
            "enabled": True, "connected": True,
            "account": {"open_id": "account", "display_name": "Safe Account"},
        }

    def review_context(self, item):
        return {
            **self.connection(), "attempt": self.attempt,
            "checksum": "a" * 64, "source_creator": "Creator",
            "cleanup_eligible": False,
        }

    def prepare(self, review_id, **kwargs):
        self.calls.append(("prepare", review_id, kwargs))
        self.attempt = {
            "attempt_id": "publication_" + "a" * 24,
            "state": "awaiting_consent", "updated_at": "2026-08-01T00:00:00Z",
            "caption": kwargs["caption"],
            "source_attribution": kwargs["source_attribution"],
            "rendered_media_sha256": "a" * 64, "remote_publish_id": None,
            "error_reason": None, "stale": False,
        }
        return self.attempt

    def send(self, attempt_id, *, confirmed):
        self.calls.append(("send", attempt_id, confirmed))
        if not confirmed:
            raise ValueError("confirmation required")
        return {**self.attempt, "state": "processing"}

    def refresh(self, attempt_id):
        self.calls.append(("refresh", attempt_id))
        return {**self.attempt, "state": "awaiting_creator_post"}

    def cancel(self, attempt_id):
        self.calls.append(("cancel", attempt_id))
        return {**self.attempt, "state": "cancelled"}

    def retry(self, attempt_id):
        self.calls.append(("retry", attempt_id))
        return {**self.attempt, "state": "processing"}

    def mark_review_stale(self, review_id):
        self.calls.append(("stale", review_id))
        return 1

    def authorization_url(self):
        self.calls.append(("connect",))
        return "https://www.tiktok.com/v2/auth/authorize/?state=opaque"

    def disconnect(self):
        self.calls.append(("disconnect",))

    def complete_oauth(self, **kwargs):
        self.calls.append(("oauth", kwargs))
        return {"display_name": "Safe Account"}


def test_approved_review_shows_consent_driven_tiktok_controls(tmp_path):
    with running_server(tmp_path) as (server, queue, _):
        rid = review_id(queue)
        queue.approve(rid)
        publisher = FakeTikTokPublications()
        server.app.tiktok_publications = publisher
        status, _, body = request(server, "GET", "/")
    text = body.decode()
    assert status == 200
    for value in (
        "Safe Account", "Final-render SHA-256", "Prepare only — no upload",
        "authorized to republish", "Inbox delivery is not a public post",
    ):
        assert value in text
    assert publisher.calls == []


def test_prepare_and_send_are_separate_token_protected_posts(tmp_path):
    with running_server(tmp_path) as (server, queue, _):
        rid = review_id(queue)
        queue.approve(rid)
        publisher = FakeTikTokPublications()
        server.app.tiktok_publications = publisher
        denied, _, _ = post(
            server, f"/reviews/{rid}/tiktok-prepare",
            {"caption": "Caption", "source_attribution": "Source",
             "rights_confirmed": "yes"},
        )
        prepared, _, _ = post(
            server, f"/reviews/{rid}/tiktok-prepare",
            {"form_token": "test-token", "caption": "Caption #tag",
             "source_attribution": "Source: Creator", "rights_confirmed": "yes"},
        )
        _, _, confirmation = request(server, "GET", "/")
        sent, _, _ = post(
            server, f"/reviews/{rid}/tiktok-send",
            {"form_token": "test-token", "attempt_id": publisher.attempt["attempt_id"],
             "confirm_upload": "yes"},
        )
    assert denied == 403 and prepared == 303 and sent == 303
    text = confirmation.decode()
    assert "Immediate upload confirmation" in text
    assert "Safe Account" in text and "Caption #tag" in text
    assert [call[0] for call in publisher.calls] == ["prepare", "send"]


def test_timing_change_marks_prepared_publication_stale(tmp_path):
    with running_server(tmp_path) as (server, queue, _):
        rid = review_id(queue)
        queue.approve(rid)
        publisher = FakeTikTokPublications()
        server.app.tiktok_publications = publisher
        status, _, _ = post(
            server, f"/reviews/{rid}/adjust",
            {"form_token": "test-token", "render_start": "9", "render_end": "21",
             "maximum_duration": "60"},
        )
    assert status == 303
    assert ("stale", rid) in publisher.calls


def test_tiktok_connect_disconnect_and_oauth_completion_use_post(tmp_path):
    with running_server(tmp_path) as (server, _, _):
        publisher = FakeTikTokPublications()
        server.app.tiktok_publications = publisher
        get_status, _, _ = request(server, "GET", "/tiktok/connect")
        connected, headers, _ = post(
            server, "/tiktok/connect", {"form_token": "test-token"}
        )
        callback, _, body = request(
            server, "GET", "/tiktok/oauth/callback?code=opaque-code&state=opaque-state"
        )
        completed, _, _ = post(
            server, "/tiktok/oauth/complete",
            {"form_token": "test-token", "code": "opaque-code", "state": "opaque-state"},
        )
        disconnected, _, _ = post(
            server, "/tiktok/disconnect", {"form_token": "test-token"}
        )
    assert get_status in {404, 405}
    assert connected == 303 and headers["Location"].startswith("https://www.tiktok.com/")
    assert callback == 200 and b"Complete TikTok connection" in body
    assert completed == disconnected == 303


class FakeReferenceDiscovery:
    def __init__(self, queue):
        self.queue = queue
        self.accepted = []

    def accept(
        self, video_id, *, category, notes, transcription, topic=None,
        expected_revision=None, request_id=None,
    ):
        self.accepted.append(
            (video_id, category, notes, transcription, topic)
        )
        self.queue.transition(
            video_id, expected_revision=expected_revision,
            expected_status="discovered", status="accepted",
            notes=notes, category=category, topic=topic,
            accepted_reference_id=f"youtube-{video_id}",
        )

    def transition(
        self, video_id, action, *, notes, expected_revision,
        category=None, topic=None, request_id=None,
    ):
        target = {
            "reject": "rejected",
            "duplicate": "duplicate",
            "reconsider": "discovered",
        }[action]
        return self.queue.transition(
            video_id, expected_revision=expected_revision,
            expected_status=self.queue.get(video_id)["status"],
            status=target, notes=notes, category=category, topic=topic,
        )

    def withdraw(
        self, video_id, *, status, notes, expected_revision,
        confirmed, request_id=None,
    ):
        if not confirmed:
            raise ValueError("confirmation required")
        updated = self.queue.transition(
            video_id, expected_revision=expected_revision,
            expected_status="accepted", status=status, notes=notes,
            clear_accepted_reference=True,
        )
        return {
            "candidate": updated,
            "withdrawn_reference_id": f"youtube-{video_id}",
        }

    def history(self, video_id, *, limit=None):
        return []


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
                "note": "repost", "expected_revision": "0",
                "request_id": "reject-request",
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
                "expected_revision": str(queue.get("short_one")["revision"]),
                "request_id": "accept-request",
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


def test_reference_candidate_page_renders_only_legal_actions(tmp_path):
    item = reference_candidate(tmp_path) | {
        "status": "discovered", "revision": 4, "notes": "",
    }
    discovered = review_server.render_reference_candidates(
        [item], "test-token"
    )
    assert 'value="accept"' in discovered
    assert 'value="reject"' in discovered
    assert 'value="duplicate"' in discovered
    assert 'value="withdraw"' not in discovered
    assert 'value="reconsider"' not in discovered
    assert 'name="expected_revision" value="4"' in discovered

    accepted = review_server.render_reference_candidates(
        [item | {
            "status": "accepted",
            "accepted_reference_id": "youtube-short_one",
        }],
        "test-token",
    )
    assert 'value="withdraw"' in accepted
    assert "Withdraw Reference" in accepted
    assert 'name="confirm_withdrawal" value="yes" required' in accepted
    assert 'value="accept"' not in accepted
    assert 'value="reject"' not in accepted

    for status in ("rejected", "duplicate"):
        rendered = review_server.render_reference_candidates(
            [item | {"status": status}], "test-token"
        )
        assert 'value="reconsider"' in rendered
        assert 'value="accept"' not in rendered
        assert 'value="reject"' not in rendered
        assert 'value="withdraw"' not in rendered

    inconsistent = review_server.render_reference_candidates(
        [
            item | {
                "status": "rejected",
                "accepted_reference_id": "youtube-short_one",
            }
        ],
        "test-token",
    )
    assert 'value="withdraw"' in inconsistent
    assert 'value="reconsider"' not in inconsistent


def test_reference_candidate_history_is_sanitized_and_token_stays_in_body(
    tmp_path,
):
    item = reference_candidate(tmp_path) | {
        "status": "rejected", "revision": 1, "notes": "",
    }
    event = {
        "timestamp": "2026-07-25T00:00:00+00:00",
        "action": "reject", "result": "success",
        "previous_status": "discovered", "resulting_status": "rejected",
        "previous_revision": 0, "resulting_revision": 1,
        "reviewer": "Local reviewer", "note": "Weak setup",
        "failure_reason": None,
    }
    rendered = review_server.render_reference_candidates(
        [item], "test-token", histories={"short_one": [event]}
    )
    assert "Local reviewer" in rendered and "Weak setup" in rendered
    assert 'name="form_token" value="test-token"' in rendered
    assert "test-token" not in (
        "/reference-candidates/short_one/decision"
        + "/reference-media/short_one"
    )


def test_reference_candidate_withdraw_requires_confirmation(tmp_path):
    with running_server(tmp_path) as (server, _, _):
        queue = ReferenceCandidateQueue(
            tmp_path / "reference-candidates.json"
        )
        queue.upsert_discovered([reference_candidate(tmp_path)])
        queue.transition(
            "short_one", expected_revision=0,
            expected_status="discovered", status="accepted",
            accepted_reference_id="youtube-short_one",
        )
        service = FakeReferenceDiscovery(queue)
        server.app.reference_candidate_queue = queue
        server.app.reference_discovery_service = service
        before = queue.path.read_bytes()
        status, headers, _ = post(
            server,
            "/reference-candidates/short_one/decision",
            {
                "form_token": "test-token", "action": "withdraw",
                "note": "Not representative",
                "expected_revision": str(
                    queue.get("short_one")["revision"]
                ),
                "request_id": "withdraw-request",
            },
        )
    assert status == 303 and "error=" in headers["Location"]
    assert queue.path.read_bytes() == before


def test_reference_candidate_stale_revision_is_nonmutating(tmp_path):
    with running_server(tmp_path) as (server, _, _):
        queue = ReferenceCandidateQueue(
            tmp_path / "reference-candidates.json"
        )
        queue.upsert_discovered([reference_candidate(tmp_path)])
        service = FakeReferenceDiscovery(queue)
        server.app.reference_candidate_queue = queue
        server.app.reference_discovery_service = service
        before = queue.path.read_bytes()
        status, headers, _ = post(
            server,
            "/reference-candidates/short_one/decision",
            {
                "form_token": "test-token", "action": "reject",
                "note": "Not representative", "expected_revision": "9",
                "request_id": "stale-request",
            },
        )
    assert status == 303 and "error=" in headers["Location"]
    assert queue.path.read_bytes() == before


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
                "expected_revision": "0",
                "request_id": "invalid-topic",
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


def test_accepted_reference_page_controls_media_analysis_and_post_only(tmp_path):
    _, entries, annotations, _, _, evidence = evidence_environment(
        tmp_path / "evidence", count=1
    )
    reference_id = entries[0]["reference_id"]
    with running_server(tmp_path / "server") as (server, _, _):
        server.app.reference_evidence_service = evidence
        status, _, body = request(server, "GET", "/references")
        text = body.decode()
        assert status == 200
        assert reference_id in text
        assert "Human style annotations" in text
        assert "Reanalyze with transcription" in text
        assert "Rebuild gaming_highlight profile" in text
        assert "Inspect complete sanitized analysis" in text
        assert 'name="expected_annotation_revision" value="0"' in text
        assert text.count('name="form_token" value="test-token"') == 3
        assert "Withdraw Reference" not in text and "Delete reference" not in text
        assert request(
            server, "GET", f"/references/{reference_id}/annotations"
        )[0] == 405
        assert request(
            server, "HEAD", f"/references/{reference_id}/reanalyze"
        )[0] == 405
        analysis_status, _, analysis_body = request(
            server, "GET", f"/references/{reference_id}/analysis"
        )
        assert analysis_status == 200
        assert b"sanitized analysis" in analysis_body
        media_status, headers, media_body = request(
            server, "GET", f"/accepted-reference-media/{reference_id}",
            headers={"Range": "bytes=0-3"},
        )
        assert media_status == 206 and len(media_body) == 4
        assert headers["Content-Range"].startswith("bytes 0-3/")
    assert not annotations.exists(reference_id)


def test_accepted_reference_annotation_stale_reanalysis_and_rebuild_posts(tmp_path):
    _, entries, annotations, audit, builder, evidence = evidence_environment(
        tmp_path / "evidence", count=1
    )
    reference_id = entries[0]["reference_id"]
    values = default_annotation_values()
    values.update({
        "composition": "full_screen_gameplay",
        "facecam_presence": "none",
        "opening_style": "immediate_action",
        "clip_purpose": "clutch_highlight",
        "pacing": "fast",
        "payoff_type": "gameplay_result",
        "caption_style": "phrase_captions",
    })
    form = {
        "form_token": "test-token",
        "expected_annotation_revision": "0",
        "request_id": "web-annotation",
        **{name: str(values[name]) for name in (
            "composition", "facecam_presence", "opening_style", "clip_purpose",
            "pacing", "payoff_type", "caption_style",
        )},
        "desired_qualities": "complete result\nclear action",
        "undesirable_qualities": "dead air",
        "reviewer_notes": "Keep the payoff.",
    }
    with running_server(tmp_path / "server") as (server, _, _):
        server.app.reference_evidence_service = evidence
        status, headers, _ = post(
            server, f"/references/{reference_id}/annotations", form
        )
        assert status == 303 and "success=" in headers["Location"]
        assert annotations.read(reference_id)["revision"] == 1
        annotation_before = annotations.path(reference_id).read_bytes()
        audit_before = audit.path.read_bytes()
        stale = dict(form, request_id="web-stale")
        status, headers, _ = post(
            server, f"/references/{reference_id}/annotations", stale
        )
        assert status == 303 and "error=" in headers["Location"]
        assert annotations.path(reference_id).read_bytes() == annotation_before
        assert audit.path.read_bytes() != audit_before
        assert audit.history(reference_id=reference_id)[-1]["result"] == "failure"
        status, headers, _ = post(
            server,
            f"/references/{reference_id}/reanalyze",
            {
                "form_token": "test-token",
                "expected_annotation_revision": "1",
                "request_id": "web-reanalyze",
            },
        )
        assert status == 303 and "success=" in headers["Location"]
        assert annotations.path(reference_id).read_bytes() == annotation_before
        status, headers, _ = post(
            server,
            f"/references/{reference_id}/rebuild-profile",
            {
                "form_token": "test-token",
                "expected_annotation_revision": "1",
                "request_id": "web-profile",
            },
        )
        assert status == 303 and "success=" in headers["Location"]
    assert builder.read("gaming_highlight")["staleness"]["status"] == "current"
    assert [event["action"] for event in audit.history(reference_id=reference_id)] == [
        "annotation_update", "annotation_update", "reanalyze", "profile_rebuild",
    ]
    assert audit.history(profile_name="gaming_highlight")[-1]["action"] == "profile_rebuild"
