from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from backend.services.clip_review_queue import ClipReviewQueue
from backend.services.publication import PublicationError, PublicationStore
from backend.services.tiktok import (
    INBOX_INITIALIZE_URL, STATUS_URL, TOKEN_URL, USER_INFO_URL,
    FileUploadTransport, OAuthStateStore, TikTokAPI, TikTokConfiguration,
    SignedMediaURLService, TikTokError, TikTokPublicationService, TikTokTokenStore,
    TikTokWebhookVerifier, file_chunk_plan,
)
from backend.services.video_manifest import VideoManifest


def token_document(*, access: str = "access-one", refresh: str = "refresh-one"):
    now = datetime.now(timezone.utc)
    return {
        "version": 1, "open_id": "account-1", "display_name": "My TikTok",
        "access_token": access, "refresh_token": refresh,
        "scopes": ["user.info.basic", "video.upload"],
        "access_expires_at": (now + timedelta(hours=1)).isoformat(),
        "refresh_expires_at": (now + timedelta(days=30)).isoformat(),
        "updated_at": now.isoformat(),
    }


class FakeAPI:
    def __init__(self):
        self.initialized = []
        self.statuses = []

    def initialize(self, source_info):
        self.initialized.append(source_info)
        return {
            "publish_id": f"publish-{len(self.initialized)}",
            "upload_url": "https://open-upload.tiktokapis.com/video/one",
        }

    def fetch_status(self, publish_id):
        assert publish_id.startswith("publish-")
        return self.statuses.pop(0)

    def authorization_url(self, states):
        return "https://www.tiktok.com/v2/auth/authorize/?state=safe"

    def complete_oauth(self, **kwargs):
        return {"open_id": "account-1", "display_name": "My TikTok"}

    def disconnect(self):
        return None


class FakeTransfer:
    def __init__(self, error=None):
        self.paths = []
        self.error = error

    def source_info(self, path):
        return {"source": "FILE_UPLOAD", "video_size": path.stat().st_size,
                "chunk_size": path.stat().st_size, "total_chunk_count": 1}

    def transfer(self, upload_url, path):
        self.paths.append((upload_url, path))
        if self.error:
            raise self.error


def setup_service(tmp_path: Path, *, daily=1, pending=1, transfer=None):
    queue = ClipReviewQueue(tmp_path / "reviews.json")
    media = tmp_path / "preview.mp4"
    media.write_bytes(b"video" * 100)
    candidate = {
        "candidate_id": "candidate-1", "rank": 1, "score": 90,
        "start": 0.0, "end": 10.0, "duration": 10.0, "text": "reaction",
    }
    review = queue.add_or_update_preview(
        "video-1", candidate, media, tmp_path / "preview.json"
    )
    review = queue.approve(review["review_id"])
    manifest = VideoManifest(tmp_path / "manifest.json")
    config = TikTokConfiguration(
        enabled=True, client_key="client", client_secret="private",
        redirect_uri="http://127.0.0.1:8765/tiktok/oauth/callback",
        daily_limit=daily, maximum_pending=pending,
        token_path=tmp_path / "tokens.json",
        oauth_state_path=tmp_path / "states.json",
    )
    tokens = TikTokTokenStore(config.token_path)
    tokens.write(token_document())
    api = FakeAPI()
    store = PublicationStore(tmp_path / "publication.json", tmp_path / "audit.jsonl")
    service = TikTokPublicationService(
        config, store, queue, manifest, api, tokens,
        OAuthStateStore(config.oauth_state_path),
        file_transport=transfer or FakeTransfer(),
    )
    return service, queue, review, media, api


def test_disabled_by_default_and_environment_names(tmp_path):
    config = TikTokConfiguration.from_environment({
        "AUTOCLIP_TIKTOK_TOKEN_PATH": str(tmp_path / "token"),
        "AUTOCLIP_TIKTOK_OAUTH_STATE_PATH": str(tmp_path / "state"),
    })
    assert not config.enabled
    assert config.daily_limit == config.maximum_pending == 1
    assert config.transport == "FILE_UPLOAD"


def test_verified_https_configuration_prefers_pull_from_url(tmp_path):
    config = TikTokConfiguration.from_environment({
        "AUTOCLIP_TIKTOK_VERIFIED_MEDIA_BASE_URL": "https://media.example.test",
        "AUTOCLIP_TIKTOK_MEDIA_SIGNING_KEY": "x" * 32,
        "AUTOCLIP_TIKTOK_TOKEN_PATH": str(tmp_path / "token"),
        "AUTOCLIP_TIKTOK_OAUTH_STATE_PATH": str(tmp_path / "state"),
    })
    assert config.transport == "PULL_FROM_URL"


def test_protected_atomic_token_storage_and_removal(tmp_path):
    path = tmp_path / "private" / "tokens.json"
    store = TikTokTokenStore(path)
    store.write(token_document())
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    store.write(token_document(access="rotated"))
    assert store.read()["access_token"] == "rotated"
    assert not list(path.parent.glob("*.tmp"))
    store.remove()
    assert not path.exists()


def test_oauth_state_pkce_is_one_time_and_csrf_safe(tmp_path):
    states = OAuthStateStore(tmp_path / "private" / "states.json")
    created = states.create(desktop=True)
    assert created["state"] and created["code_verifier"]
    assert "+" not in created["code_challenge"] and "=" not in created["code_challenge"]
    consumed = states.consume(created["state"])
    assert consumed["code_verifier"] == created["code_verifier"]
    with pytest.raises(TikTokError, match="missing or invalid"):
        states.consume(created["state"])


class FakeHTTP:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


def test_oauth_exchange_profile_and_refresh_rotation(tmp_path):
    config = TikTokConfiguration(
        enabled=True, client_key="key", client_secret="secret",
        redirect_uri="http://127.0.0.1:8765/callback",
        token_path=tmp_path / "tokens", oauth_state_path=tmp_path / "states",
    )
    tokens = TikTokTokenStore(config.token_path)
    states = OAuthStateStore(config.oauth_state_path)
    created = states.create(desktop=True)
    base = {
        "open_id": "account-1", "access_token": "access-1",
        "refresh_token": "refresh-1", "scope": "video.upload,user.info.basic",
        "expires_in": 3600, "refresh_expires_in": 86400,
    }
    http = FakeHTTP([
        (200, base),
        (200, {"data": {"user": {"open_id": "account-1", "display_name": "Name"}}}),
    ])
    api = TikTokAPI(config, tokens, http=http, sleeper=lambda _: None)
    account = api.complete_oauth(code="one-time", state=created["state"], oauth_states=states)
    assert account["display_name"] == "Name"
    saved = tokens.read()
    saved["access_expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    tokens.write(saved)
    rotated = dict(base, access_token="access-2", refresh_token="refresh-2")
    http.responses.append((200, rotated))
    assert api.access_token() == "access-2"
    assert tokens.read()["refresh_token"] == "refresh-2"
    assert http.calls[0][1] == TOKEN_URL and http.calls[1][1].startswith(USER_INFO_URL)


def test_inbox_initialization_uses_only_mocked_official_endpoint(tmp_path):
    config = TikTokConfiguration(
        enabled=True, client_key="key", client_secret="secret",
        redirect_uri="http://127.0.0.1:8765/callback",
        token_path=tmp_path / "tokens", oauth_state_path=tmp_path / "states",
    )
    tokens = TikTokTokenStore(config.token_path)
    tokens.write(token_document())
    http = FakeHTTP([(200, {"data": {
        "publish_id": "publish-1",
        "upload_url": "https://open-upload.tiktokapis.com/video/one",
    }})])
    api = TikTokAPI(config, tokens, http=http, sleeper=lambda _: None)
    result = api.initialize({
        "source": "FILE_UPLOAD", "video_size": 100,
        "chunk_size": 100, "total_chunk_count": 1,
    })
    assert result["publish_id"] == "publish-1"
    method, url, kwargs = http.calls[0]
    assert method == "POST" and url == INBOX_INITIALIZE_URL
    assert kwargs["json_body"]["source_info"]["source"] == "FILE_UPLOAD"


def test_prepare_does_not_upload_and_send_requires_immediate_consent(tmp_path):
    service, _, review, _, api = setup_service(tmp_path)
    attempt = service.prepare(
        review["review_id"], caption="Caption #tag",
        source_attribution="Source: Creator", rights_confirmed=True,
    )
    assert attempt["state"] == "awaiting_consent" and api.initialized == []
    with pytest.raises(PublicationError, match="confirmation"):
        service.send(attempt["attempt_id"], confirmed=False)
    sent = service.send(attempt["attempt_id"], confirmed=True)
    assert sent["state"] == "processing"
    assert api.initialized[0]["source"] == "FILE_UPLOAD"


def test_rejected_clip_and_changed_render_never_upload(tmp_path):
    service, queue, review, media, api = setup_service(tmp_path)
    queue.reject(review["review_id"])
    with pytest.raises(PublicationError, match="Only an approved"):
        service.prepare(
            review["review_id"], caption="caption", source_attribution="source",
            rights_confirmed=True,
        )
    queue.approve(review["review_id"])
    attempt = service.prepare(
        review["review_id"], caption="caption", source_attribution="source",
        rights_confirmed=True,
    )
    media.write_bytes(b"replacement")
    with pytest.raises(PublicationError, match="checksum changed"):
        service.send(attempt["attempt_id"], confirmed=True)
    assert api.initialized == []


def test_authorized_destination_account_cannot_change_before_send(tmp_path):
    service, _, review, _, api = setup_service(tmp_path)
    attempt = service.prepare(
        review["review_id"], caption="caption", source_attribution="source",
        rights_confirmed=True,
    )
    changed = token_document()
    changed["open_id"] = "different-account"
    changed["display_name"] = "Different account"
    service.tokens.write(changed)
    with pytest.raises(TikTokError, match="account changed"):
        service.send(attempt["attempt_id"], confirmed=True)
    assert api.initialized == []


def test_inbox_and_publish_complete_status_are_distinct(tmp_path):
    service, _, review, _, api = setup_service(tmp_path)
    attempt = service.prepare(
        review["review_id"], caption="caption", source_attribution="source",
        rights_confirmed=True,
    )
    sent = service.send(attempt["attempt_id"], confirmed=True)
    api.statuses.append({"status": "SEND_TO_USER_INBOX", "fail_reason": None,
                         "post_ids": [], "share_urls": []})
    inbox = service.refresh(sent["attempt_id"])
    assert inbox["state"] == "awaiting_creator_post"
    assert inbox["publish_completed_at"] is None
    api.statuses.append({"status": "PUBLISH_COMPLETE", "fail_reason": None,
                         "post_ids": ["post-1"], "share_urls": ["https://safe"]})
    complete = service.refresh(sent["attempt_id"])
    assert complete["state"] == "publish_complete"
    assert complete["remote_post_ids"] == ["post-1"]


def test_background_reconciliation_is_bounded_and_skips_terminal(tmp_path):
    service, _, review, _, api = setup_service(tmp_path)
    baseline = datetime.now(timezone.utc)
    service.now = lambda: baseline
    attempt = service.prepare(
        review["review_id"], caption="caption", source_attribution="source",
        rights_confirmed=True,
    )
    sent = service.send(attempt["attempt_id"], confirmed=True)
    api.statuses.append({"status": "PUBLISH_COMPLETE", "fail_reason": None,
                         "post_ids": [], "share_urls": []})
    service.now = lambda: baseline + timedelta(minutes=10)
    results = service.reconcile_pending(limit=1)
    assert [item["state"] for item in results] == ["publish_complete"]
    assert service.reconcile_pending(limit=1) == []
    assert service.store.get(sent["attempt_id"])["state"] == "publish_complete"


def test_uncertain_transfer_reconciles_before_retry(tmp_path):
    transfer = FakeTransfer(TikTokError("connection lost", retryable=True, ambiguous=True))
    service, _, review, _, api = setup_service(tmp_path, transfer=transfer)
    attempt = service.prepare(
        review["review_id"], caption="caption", source_attribution="source",
        rights_confirmed=True,
    )
    failed = service.send(attempt["attempt_id"], confirmed=True)
    assert failed["state"] == "failed_retryable" and failed["transfer_uncertain"]
    api.statuses.append({"status": "PROCESSING_UPLOAD", "fail_reason": None,
                         "post_ids": [], "share_urls": []})
    reconciled = service.retry(failed["attempt_id"])
    assert reconciled["state"] == "processing"
    assert len(api.initialized) == 1


def test_daily_limit_prevents_second_initialization(tmp_path):
    service, queue, review, _, api = setup_service(tmp_path, pending=10)
    first = service.prepare(
        review["review_id"], caption="one", source_attribution="source",
        rights_confirmed=True,
    )
    service.send(first["attempt_id"], confirmed=True)
    media = Path(review["preview_path"]).with_name("second.mp4")
    media.write_bytes(b"second")
    candidate = {"candidate_id": "candidate-2", "rank": 2, "score": 80,
                 "start": 2.0, "end": 8.0, "duration": 6.0, "text": "two"}
    second_review = queue.add_or_update_preview(
        "video-2", candidate, media, media.with_suffix(".json")
    )
    second_review = queue.approve(second_review["review_id"])
    second = service.prepare(
        second_review["review_id"], caption="two", source_attribution="source",
        rights_confirmed=True,
    )
    with pytest.raises(TikTokError, match="daily"):
        service.send(second["attempt_id"], confirmed=True)
    assert len(api.initialized) == 1


def test_pending_limit_prevents_second_unresolved_share(tmp_path):
    service, queue, review, _, api = setup_service(tmp_path, daily=10, pending=1)
    first = service.prepare(
        review["review_id"], caption="one", source_attribution="source",
        rights_confirmed=True,
    )
    media = Path(review["preview_path"]).with_name("second-pending.mp4")
    media.write_bytes(b"second")
    candidate = {"candidate_id": "candidate-pending", "rank": 2, "score": 80,
                 "start": 2.0, "end": 8.0, "duration": 6.0, "text": "two"}
    second_review = queue.add_or_update_preview(
        "video-pending", candidate, media, media.with_suffix(".json")
    )
    second_review = queue.approve(second_review["review_id"])
    second = service.prepare(
        second_review["review_id"], caption="two", source_attribution="source",
        rights_confirmed=True,
    )
    with pytest.raises(TikTokError, match="maximum unresolved"):
        service.send(second["attempt_id"], confirmed=True)
    assert service.store.get(first["attempt_id"])["state"] == "awaiting_consent"
    assert api.initialized == []


def test_file_upload_chunking_and_no_direct_post_endpoint():
    chunk, count, ranges = file_chunk_plan(11 * 1024 * 1024)
    assert count >= 1 and ranges[0][0] == 0 and ranges[-1][1] == 11 * 1024 * 1024
    assert INBOX_INITIALIZE_URL.endswith("/inbox/video/init/")
    module = Path(__file__).parents[1] / "services" / "tiktok.py"
    assert "/post/publish/video/init/" not in module.read_text()


def test_signed_pull_url_is_opaque_checksum_pinned_and_range_capable(tmp_path):
    media = tmp_path / "approved.mp4"
    media.write_bytes(b"0123456789")
    attempt = {
        "attempt_id": "publication_" + "a" * 24,
        "rendered_media_path": str(media),
        "rendered_media_sha256": hashlib.sha256(media.read_bytes()).hexdigest(),
    }
    signer = SignedMediaURLService("https://media.example.test", "k" * 32)
    url = signer.create(attempt, ttl_seconds=60)
    token = url.rsplit("/", 1)[1]
    response = signer.range_response(
        token, lambda attempt_id: attempt, "bytes=2-5"
    )
    assert response["status"] == 206
    assert response["headers"]["Content-Range"] == "bytes 2-5/10"
    assert b"".join(response["body"]) == b"2345"
    media.write_bytes(b"changed")
    with pytest.raises(TikTokError, match="checksum"):
        signer.resolve(token, lambda attempt_id: attempt)


def test_webhook_verification_rejects_tampering_and_replay():
    body = json.dumps({"event": "status"}).encode()
    now = 1_800_000_000
    signature = __import__("hmac").new(
        b"secret", str(now).encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    header = f"t={now},s={signature}"
    verifier = TikTokWebhookVerifier("secret")
    assert verifier.verify(body, header, now=now)["event"] == "status"
    with pytest.raises(TikTokError, match="replay"):
        verifier.verify(body, header, now=now)
