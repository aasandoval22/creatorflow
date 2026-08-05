"""Disabled-by-default official TikTok inbox-upload integration."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import hmac
import http.client
import json
import mimetypes
import os
import secrets
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from backend.services.clip_review_queue import ClipReviewQueue
from backend.services.publication import (
    PublicationError,
    PublicationState,
    PublicationStore,
    TERMINAL_STATES,
    safe_publication_text,
    sha256_file,
)
from backend.services.video_manifest import VideoManifest, utc_now


AUTHORIZE_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
REVOKE_URL = "https://open.tiktokapis.com/v2/oauth/revoke/"
USER_INFO_URL = "https://open.tiktokapis.com/v2/user/info/"
INBOX_INITIALIZE_URL = (
    "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/"
)
STATUS_URL = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"
REQUIRED_SCOPES = frozenset({"user.info.basic", "video.upload"})
RETRYABLE_FAILURES = frozenset({"internal", "video_pull_failed"})
DEFAULT_TOKEN_PATH = Path.home() / ".config" / "creatorflow" / "tiktok_tokens.json"
DEFAULT_OAUTH_STATE_PATH = (
    Path.home() / ".config" / "creatorflow" / "tiktok_oauth_states.json"
)


class TikTokError(PublicationError):
    """A TikTok operation failed without exposing credentials."""

    def __init__(
        self, message: str, *, retryable: bool = False,
        ambiguous: bool = False,
    ) -> None:
        super().__init__(safe_publication_text(message) or "TikTok request failed.")
        self.retryable = retryable
        self.ambiguous = ambiguous


def _enabled(value: str | None) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _positive(value: str | None, default: int, label: str) -> int:
    if value in (None, ""):
        return default
    try:
        parsed = int(value)
    except ValueError as error:
        raise TikTokError(f"{label} must be a positive integer.") from error
    if parsed < 1:
        raise TikTokError(f"{label} must be a positive integer.")
    return parsed


@dataclass(frozen=True)
class TikTokConfiguration:
    enabled: bool = False
    client_key: str | None = None
    client_secret: str | None = None
    redirect_uri: str | None = None
    daily_limit: int = 1
    maximum_pending: int = 1
    transport: str = "FILE_UPLOAD"
    verified_media_base_url: str | None = None
    media_signing_key: str | None = None
    token_path: Path = DEFAULT_TOKEN_PATH
    oauth_state_path: Path = DEFAULT_OAUTH_STATE_PATH
    maximum_retries: int = 3
    reconciliation_interval_seconds: int = 300

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str] | None = None,
    ) -> "TikTokConfiguration":
        values = os.environ if environment is None else environment
        verified_base = values.get("AUTOCLIP_TIKTOK_VERIFIED_MEDIA_BASE_URL")
        requested_transport = values.get("AUTOCLIP_TIKTOK_TRANSPORT")
        transport = requested_transport or (
            "PULL_FROM_URL" if verified_base else "FILE_UPLOAD"
        )
        config = cls(
            enabled=_enabled(values.get("AUTOCLIP_TIKTOK_ENABLED")),
            client_key=values.get("TIKTOK_CLIENT_KEY") or None,
            client_secret=values.get("TIKTOK_CLIENT_SECRET") or None,
            redirect_uri=values.get("TIKTOK_REDIRECT_URI") or None,
            daily_limit=_positive(
                values.get("AUTOCLIP_TIKTOK_DAILY_LIMIT"), 1,
                "AUTOCLIP_TIKTOK_DAILY_LIMIT",
            ),
            maximum_pending=_positive(
                values.get("AUTOCLIP_TIKTOK_MAX_PENDING"), 1,
                "AUTOCLIP_TIKTOK_MAX_PENDING",
            ),
            transport=transport,
            verified_media_base_url=verified_base,
            media_signing_key=values.get("AUTOCLIP_TIKTOK_MEDIA_SIGNING_KEY") or None,
            token_path=Path(
                values.get("AUTOCLIP_TIKTOK_TOKEN_PATH") or DEFAULT_TOKEN_PATH
            ),
            oauth_state_path=Path(
                values.get("AUTOCLIP_TIKTOK_OAUTH_STATE_PATH")
                or DEFAULT_OAUTH_STATE_PATH
            ),
            maximum_retries=_positive(
                values.get("AUTOCLIP_TIKTOK_MAX_RETRIES"), 3,
                "AUTOCLIP_TIKTOK_MAX_RETRIES",
            ),
            reconciliation_interval_seconds=_positive(
                values.get("AUTOCLIP_TIKTOK_RECONCILE_SECONDS"), 300,
                "AUTOCLIP_TIKTOK_RECONCILE_SECONDS",
            ),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if self.transport not in {"FILE_UPLOAD", "PULL_FROM_URL"}:
            raise TikTokError(
                "AUTOCLIP_TIKTOK_TRANSPORT must be FILE_UPLOAD or PULL_FROM_URL."
            )
        if self.enabled and not all(
            (self.client_key, self.client_secret, self.redirect_uri)
        ):
            raise TikTokError(
                "Enabled TikTok integration requires client key, client secret, "
                "and redirect URI configuration."
            )
        if self.redirect_uri:
            parsed = urllib.parse.urlsplit(self.redirect_uri)
            loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
            valid_scheme = parsed.scheme == "https" or (
                parsed.scheme == "http" and loopback and parsed.port is not None
            )
            if (
                not valid_scheme or not parsed.netloc or parsed.query
                or parsed.fragment
            ):
                raise TikTokError(
                    "TikTok redirect URI must be static HTTPS, or an HTTP loopback "
                    "desktop redirect with an explicit port."
                )
        if self.transport == "PULL_FROM_URL":
            parsed = urllib.parse.urlsplit(self.verified_media_base_url or "")
            if parsed.scheme != "https" or not parsed.netloc:
                raise TikTokError(
                    "PULL_FROM_URL requires a verified HTTPS media base URL."
                )
            if not self.media_signing_key or len(self.media_signing_key) < 32:
                raise TikTokError(
                    "PULL_FROM_URL requires a strong media signing key."
                )

    @property
    def desktop_oauth(self) -> bool:
        parsed = urllib.parse.urlsplit(self.redirect_uri or "")
        return parsed.hostname in {"127.0.0.1", "localhost", "::1"}


def _atomic_private_json(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    temporary: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(document, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class _PrivateFileLock:
    _guard = threading.Lock()
    _locks: dict[Path, threading.RLock] = {}
    _local = threading.local()

    def __init__(self, path: Path) -> None:
        self.path = path.with_name(f"{path.name}.lock")
        with self._guard:
            self.process_lock = self._locks.setdefault(
                self.path.resolve(), threading.RLock()
            )

    @contextmanager
    def locked(self) -> Iterator[None]:
        with self.process_lock:
            key = str(self.path.resolve())
            depths = getattr(self._local, "depths", {})
            depth = depths.get(key, 0)
            stream = None
            try:
                if depth == 0:
                    self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    os.chmod(self.path.parent, 0o700)
                    stream = self.path.open("a+")
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                depths[key] = depth + 1
                self._local.depths = depths
                yield
            finally:
                if depth == 0:
                    depths.pop(key, None)
                    if stream is not None:
                        try:
                            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                        finally:
                            stream.close()
                else:
                    depths[key] = depth


class TikTokTokenStore:
    """Filesystem-protected token state; no project encryption facility exists."""

    FIELDS = {
        "version", "open_id", "display_name", "access_token", "refresh_token",
        "scopes", "access_expires_at", "refresh_expires_at", "updated_at",
    }

    def __init__(self, path: Path = DEFAULT_TOKEN_PATH) -> None:
        self.path = Path(path)
        self._file_lock = _PrivateFileLock(self.path)
        if self.path.exists():
            self._validate(self._read())

    def _read(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise TikTokError("Stored TikTok authorization is corrupt.") from error
        self._validate(value)
        return value

    @classmethod
    def _validate(cls, value: Any) -> None:
        if not isinstance(value, dict) or set(value) != cls.FIELDS or value["version"] != 1:
            raise TikTokError("Stored TikTok authorization has an invalid structure.")
        for field in (
            "open_id", "display_name", "access_token", "refresh_token",
            "access_expires_at", "refresh_expires_at", "updated_at",
        ):
            if not isinstance(value[field], str) or not value[field]:
                raise TikTokError("Stored TikTok authorization is incomplete.")
        if not isinstance(value["scopes"], list) or any(
            not isinstance(scope, str) or not scope for scope in value["scopes"]
        ):
            raise TikTokError("Stored TikTok authorization scopes are invalid.")
        if not REQUIRED_SCOPES.issubset(set(value["scopes"])):
            raise TikTokError("Stored TikTok authorization lacks required scopes.")
        for field in ("access_expires_at", "refresh_expires_at", "updated_at"):
            try:
                parsed = datetime.fromisoformat(value[field].replace("Z", "+00:00"))
            except ValueError as error:
                raise TikTokError("Stored TikTok authorization timestamps are invalid.") from error
            if parsed.tzinfo is None:
                raise TikTokError("Stored TikTok authorization timestamps need timezone.")

    def read(self) -> dict[str, Any] | None:
        with self.locked():
            return self._read() if self.path.exists() else None

    def write(self, value: Mapping[str, Any]) -> None:
        with self.locked():
            document = dict(value)
            self._validate(document)
            _atomic_private_json(self.path, document)

    def remove(self) -> None:
        with self.locked():
            if self.path.exists():
                self.path.unlink()

    def locked(self) -> Any:
        return self._file_lock.locked()

    def public_account(self) -> dict[str, Any] | None:
        value = self.read()
        if value is None:
            return None
        return {
            "open_id": value["open_id"],
            "display_name": value["display_name"],
            "scopes": list(value["scopes"]),
            "access_expires_at": value["access_expires_at"],
        }


class OAuthStateStore:
    """One-time OAuth state and optional desktop PKCE verifier."""

    def __init__(self, path: Path = DEFAULT_OAUTH_STATE_PATH) -> None:
        self.path = Path(path)
        self._file_lock = _PrivateFileLock(self.path)

    def create(self, *, desktop: bool, ttl_seconds: int = 600) -> dict[str, str | None]:
        with self._file_lock.locked():
            state = secrets.token_urlsafe(32)
            verifier = secrets.token_urlsafe(64) if desktop else None
            expires = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
            document = self._read()
            document["states"] = {
                key: value for key, value in document["states"].items()
                if value["expires_at"] > utc_now()
            }
            document["states"][state] = {
                "expires_at": expires.isoformat(),
                "code_verifier": verifier,
            }
            _atomic_private_json(self.path, document)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode("ascii").rstrip("=")
            if verifier else None
        )
        return {"state": state, "code_verifier": verifier, "code_challenge": challenge}

    def consume(self, supplied: str) -> dict[str, Any]:
        with self._file_lock.locked():
            document = self._read()
            key = next(
                (value for value in document["states"]
                 if hmac.compare_digest(value, supplied)),
                None,
            )
            if key is None:
                raise TikTokError("TikTok authorization state is missing or invalid.")
            value = document["states"].pop(key)
            _atomic_private_json(self.path, document)
        expires = datetime.fromisoformat(value["expires_at"])
        if expires <= datetime.now(timezone.utc):
            raise TikTokError("TikTok authorization state expired; connect again.")
        return value

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "states": {}}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise TikTokError("Stored TikTok OAuth state is corrupt.") from error
        if (
            not isinstance(value, dict) or set(value) != {"version", "states"}
            or value["version"] != 1 or not isinstance(value["states"], dict)
        ):
            raise TikTokError("Stored TikTok OAuth state is invalid.")
        for state, record in value["states"].items():
            if (
                not isinstance(state, str) or not isinstance(record, dict)
                or set(record) != {"expires_at", "code_verifier"}
                or not isinstance(record["expires_at"], str)
                or record["code_verifier"] is not None
                and not isinstance(record["code_verifier"], str)
            ):
                raise TikTokError("Stored TikTok OAuth state is invalid.")
            try:
                expires = datetime.fromisoformat(record["expires_at"].replace("Z", "+00:00"))
            except ValueError as error:
                raise TikTokError("Stored TikTok OAuth state is invalid.") from error
            if expires.tzinfo is None:
                raise TikTokError("Stored TikTok OAuth state is invalid.")
        return value


class JSONHTTPClient:
    """Small injectable JSON client used only when integration is enabled."""

    def request(
        self, method: str, url: str, *, headers: Mapping[str, str] | None = None,
        json_body: Mapping[str, Any] | None = None,
        form_body: Mapping[str, Any] | None = None, timeout: float = 30.0,
    ) -> tuple[int, dict[str, Any]]:
        if json_body is not None and form_body is not None:
            raise TikTokError("HTTP request body is ambiguous.")
        request_headers = dict(headers or {})
        data = None
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            request_headers["Content-Type"] = "application/json; charset=UTF-8"
        elif form_body is not None:
            data = urllib.parse.urlencode(form_body).encode("utf-8")
            request_headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = urllib.request.Request(
            url, data=data, headers=request_headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                status = response.status
        except urllib.error.HTTPError as error:
            raw = error.read()
            status = error.code
        except (OSError, urllib.error.URLError) as error:
            raise TikTokError("TikTok network request failed.", retryable=True) from error
        try:
            value = json.loads(raw or b"{}")
        except json.JSONDecodeError as error:
            raise TikTokError("TikTok returned malformed JSON.", retryable=status >= 500) from error
        if not isinstance(value, dict):
            raise TikTokError("TikTok returned an invalid response.")
        return status, value


def file_chunk_plan(size: int) -> tuple[int, int, list[tuple[int, int]]]:
    if isinstance(size, bool) or not isinstance(size, int) or size < 1:
        raise TikTokError("TikTok upload media must be a nonempty file.")
    minimum = 5 * 1024 * 1024
    nominal = min(64 * 1024 * 1024, size)
    if size < minimum:
        nominal = size
        count = 1
    else:
        count = max(1, size // nominal)
    chunks = []
    for index in range(count):
        start = index * nominal
        end = size if index == count - 1 else start + nominal
        chunks.append((start, end))
    if chunks[-1][1] - chunks[-1][0] > 128 * 1024 * 1024:
        raise TikTokError("TikTok upload chunk planning exceeded the final-chunk limit.")
    return nominal, count, chunks


class FileUploadTransport:
    """Stream official FILE_UPLOAD chunks without loading the media into memory."""

    def __init__(
        self,
        uploader: Callable[[str, Path, int, int, int, str], None] | None = None,
    ) -> None:
        self.uploader = uploader or self._upload_chunk

    @staticmethod
    def source_info(path: Path) -> dict[str, Any]:
        size = path.stat().st_size
        chunk_size, count, _ = file_chunk_plan(size)
        return {
            "source": "FILE_UPLOAD",
            "video_size": size,
            "chunk_size": chunk_size,
            "total_chunk_count": count,
        }

    def transfer(self, upload_url: str, path: Path) -> None:
        parsed = urllib.parse.urlsplit(upload_url)
        if parsed.scheme != "https" or parsed.hostname != "open-upload.tiktokapis.com":
            raise TikTokError("TikTok returned an untrusted media upload URL.")
        size = path.stat().st_size
        _, _, chunks = file_chunk_plan(size)
        content_type = mimetypes.guess_type(path.name)[0] or "video/mp4"
        if content_type not in {"video/mp4", "video/quicktime", "video/webm"}:
            content_type = "video/mp4"
        for start, end in chunks:
            self.uploader(upload_url, path, start, end, size, content_type)

    @staticmethod
    def _upload_chunk(
        upload_url: str, path: Path, start: int, end: int,
        total: int, content_type: str,
    ) -> None:
        parsed = urllib.parse.urlsplit(upload_url)
        connection = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443, timeout=60)
        target = urllib.parse.urlunsplit(("", "", parsed.path, parsed.query, ""))
        length = end - start
        try:
            connection.putrequest("PUT", target)
            connection.putheader("Content-Type", content_type)
            connection.putheader("Content-Length", str(length))
            connection.putheader("Content-Range", f"bytes {start}-{end - 1}/{total}")
            connection.endheaders()
            with path.open("rb") as stream:
                stream.seek(start)
                remaining = length
                while remaining:
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise TikTokError(
                            "Rendered media ended during TikTok transfer.",
                            ambiguous=True,
                        )
                    connection.send(chunk)
                    remaining -= len(chunk)
            response = connection.getresponse()
            response.read()
            if response.status < 200 or response.status >= 300:
                raise TikTokError(
                    f"TikTok media transfer returned HTTP {response.status}.",
                    retryable=response.status >= 500 or response.status == 429,
                    ambiguous=True,
                )
        except TikTokError:
            raise
        except OSError as error:
            raise TikTokError(
                "TikTok media transfer ended without a confirmed result.",
                retryable=True, ambiguous=True,
            ) from error
        finally:
            connection.close()


class SignedMediaURLService:
    """Create and validate opaque short-lived grants for a future HTTPS handler."""

    def __init__(self, base_url: str, signing_key: str) -> None:
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise TikTokError("Signed media grants require an HTTPS base URL.")
        if len(signing_key) < 32:
            raise TikTokError("Signed media grants require a strong signing key.")
        self.base_url = base_url.rstrip("/")
        self.key = signing_key.encode("utf-8")

    def create(self, attempt: Mapping[str, Any], *, ttl_seconds: int = 900) -> str:
        payload = {
            "attempt_id": attempt["attempt_id"],
            "checksum": attempt["rendered_media_sha256"],
            "expires": int(time.time()) + ttl_seconds,
            "nonce": secrets.token_urlsafe(24),
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        signature = hmac.new(self.key, encoded.encode(), hashlib.sha256).hexdigest()
        return f"{self.base_url}/tiktok-media/{encoded}.{signature}"

    def resolve(
        self, token: str, attempt_lookup: Callable[[str], Mapping[str, Any]],
        *, now: int | None = None,
    ) -> Path:
        encoded, separator, supplied = token.partition(".")
        expected = hmac.new(self.key, encoded.encode(), hashlib.sha256).hexdigest()
        if not separator or not hmac.compare_digest(expected, supplied):
            raise TikTokError("Signed media grant is invalid.")
        try:
            padding = "=" * (-len(encoded) % 4)
            payload = json.loads(base64.urlsafe_b64decode(encoded + padding))
        except (ValueError, json.JSONDecodeError) as error:
            raise TikTokError("Signed media grant is invalid.") from error
        if set(payload) != {"attempt_id", "checksum", "expires", "nonce"}:
            raise TikTokError("Signed media grant is invalid.")
        if payload["expires"] < (int(time.time()) if now is None else now):
            raise TikTokError("Signed media grant expired.")
        attempt = attempt_lookup(payload["attempt_id"])
        if attempt["rendered_media_sha256"] != payload["checksum"]:
            raise TikTokError("Signed media checksum identity changed.")
        path = Path(attempt["rendered_media_path"])
        if path.is_symlink() or not path.is_file():
            raise TikTokError("Signed media is unavailable or unsafe.")
        if sha256_file(path) != payload["checksum"]:
            raise TikTokError("Signed media checksum validation failed.")
        return path

    def range_response(
        self, token: str, attempt_lookup: Callable[[str], Mapping[str, Any]],
        range_header: str | None = None,
    ) -> dict[str, Any]:
        """Resolve one grant into bounded streaming metadata for a future handler."""

        path = self.resolve(token, attempt_lookup)
        size = path.stat().st_size
        start, end = 0, size - 1
        status = 200
        if range_header:
            if not range_header.startswith("bytes=") or "," in range_header:
                raise TikTokError("Signed media range request is invalid.")
            raw_start, separator, raw_end = range_header[6:].partition("-")
            if not separator:
                raise TikTokError("Signed media range request is invalid.")
            try:
                if raw_start:
                    start = int(raw_start)
                    end = int(raw_end) if raw_end else size - 1
                else:
                    suffix = int(raw_end)
                    if suffix < 1:
                        raise ValueError
                    start = max(0, size - suffix)
                    end = size - 1
            except ValueError as error:
                raise TikTokError("Signed media range request is invalid.") from error
            if start < 0 or end < start or start >= size:
                raise TikTokError("Signed media range is outside the file.")
            end = min(end, size - 1)
            status = 206

        def stream() -> Any:
            with path.open("rb") as media:
                media.seek(start)
                remaining = end - start + 1
                while remaining:
                    chunk = media.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise TikTokError("Signed media ended during range transfer.")
                    remaining -= len(chunk)
                    yield chunk

        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(end - start + 1),
            "Content-Type": mimetypes.guess_type(path.name)[0] or "video/mp4",
            "Cache-Control": "private, no-store",
        }
        if status == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        return {"status": status, "headers": headers, "body": stream()}


class PullFromURLTransport:
    def __init__(self, signed_urls: SignedMediaURLService) -> None:
        self.signed_urls = signed_urls

    def source_info(self, attempt: Mapping[str, Any]) -> dict[str, str]:
        return {
            "source": "PULL_FROM_URL",
            "video_url": self.signed_urls.create(attempt),
        }


class TikTokAPI:
    def __init__(
        self, config: TikTokConfiguration, token_store: TikTokTokenStore,
        *, http: JSONHTTPClient | Any | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.tokens = token_store
        self.http = http or JSONHTTPClient()
        self.sleeper = sleeper

    def _require_enabled(self) -> None:
        if not self.config.enabled:
            raise TikTokError("TikTok integration is disabled.")

    def _request(
        self, method: str, url: str, *, maximum_attempts: int | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        last_error: TikTokError | None = None
        attempts = maximum_attempts or self.config.maximum_retries
        for attempt in range(attempts):
            try:
                status, value = self.http.request(method, url, **kwargs)
            except TikTokError as error:
                last_error = error
                if not error.retryable or attempt + 1 >= attempts:
                    raise
            else:
                error = value.get("error")
                code = error.get("code") if isinstance(error, dict) else None
                if status < 400 and code in (None, "ok"):
                    return value
                retryable = status >= 500 or status == 429 or code in {
                    "rate_limit_exceeded", "internal_error",
                }
                message = code or value.get("error_description") or "TikTok request failed"
                last_error = TikTokError(message, retryable=retryable)
                if not retryable or attempt + 1 >= attempts:
                    raise last_error
            self.sleeper(min(2 ** attempt, 4))
        raise last_error or TikTokError("TikTok request failed.")

    def authorization_url(self, oauth_states: OAuthStateStore) -> str:
        self._require_enabled()
        state = oauth_states.create(desktop=self.config.desktop_oauth)
        query = {
            "client_key": self.config.client_key,
            "response_type": "code",
            "scope": ",".join(sorted(REQUIRED_SCOPES)),
            "redirect_uri": self.config.redirect_uri,
            "state": state["state"],
        }
        if state["code_challenge"]:
            query.update(
                code_challenge=state["code_challenge"],
                code_challenge_method="S256",
            )
        return AUTHORIZE_URL + "?" + urllib.parse.urlencode(query)

    def complete_oauth(
        self, *, code: str, state: str, oauth_states: OAuthStateStore,
    ) -> dict[str, Any]:
        self._require_enabled()
        if not code or len(code) > 2048:
            raise TikTokError("TikTok authorization response is invalid.")
        stored = oauth_states.consume(state)
        form = {
            "client_key": self.config.client_key,
            "client_secret": self.config.client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": self.config.redirect_uri,
        }
        if stored["code_verifier"]:
            form["code_verifier"] = stored["code_verifier"]
        response = self._request("POST", TOKEN_URL, form_body=form)
        token = self._token_document(response, display_name="Pending profile lookup")
        granted = set(token["scopes"])
        if not REQUIRED_SCOPES.issubset(granted):
            raise TikTokError("TikTok did not grant all required scopes.")
        profile = self.user_info(access_token=token["access_token"])
        if profile["open_id"] != token["open_id"]:
            raise TikTokError("TikTok account identity changed during authorization.")
        token["display_name"] = profile["display_name"]
        token["updated_at"] = utc_now()
        self.tokens.write(token)
        return self.tokens.public_account() or {}

    def _token_document(
        self, response: Mapping[str, Any], *, display_name: str,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        try:
            for field in ("open_id", "access_token", "refresh_token", "scope"):
                if not isinstance(response[field], str) or not response[field].strip():
                    raise ValueError
            expires_in = int(response["expires_in"])
            refresh_expires_in = int(response["refresh_expires_in"])
            if expires_in < 1 or refresh_expires_in < 1:
                raise ValueError
            return {
                "version": 1,
                "open_id": response["open_id"],
                "display_name": display_name,
                "access_token": response["access_token"],
                "refresh_token": response["refresh_token"],
                "scopes": sorted(
                    scope.strip() for scope in str(response["scope"]).split(",")
                    if scope.strip()
                ),
                "access_expires_at": (
                    now + timedelta(seconds=expires_in)
                ).isoformat(),
                "refresh_expires_at": (
                    now + timedelta(seconds=refresh_expires_in)
                ).isoformat(),
                "updated_at": now.isoformat(),
            }
        except (KeyError, TypeError, ValueError) as error:
            raise TikTokError("TikTok returned an incomplete token response.") from error

    def access_token(self) -> str:
        self._require_enabled()
        with self.tokens.locked():
            current = self.tokens.read()
            if current is None:
                raise TikTokError("TikTok is not connected.")
            expires = datetime.fromisoformat(current["access_expires_at"])
            if expires > datetime.now(timezone.utc) + timedelta(minutes=5):
                return current["access_token"]
            response = self._request(
                "POST", TOKEN_URL,
                form_body={
                    "client_key": self.config.client_key,
                    "client_secret": self.config.client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": current["refresh_token"],
                },
            )
            refreshed = self._token_document(
                response, display_name=current["display_name"]
            )
            if refreshed["open_id"] != current["open_id"]:
                raise TikTokError("TikTok account identity changed during token refresh.")
            self.tokens.write(refreshed)
            return refreshed["access_token"]

    def user_info(self, *, access_token: str | None = None) -> dict[str, str]:
        token = access_token or self.access_token()
        response = self._request(
            "GET", USER_INFO_URL + "?" + urllib.parse.urlencode({
                "fields": "open_id,display_name",
            }),
            headers={"Authorization": f"Bearer {token}"},
        )
        try:
            user = response["data"]["user"]
            return {
                "open_id": str(user["open_id"]),
                "display_name": str(user["display_name"]),
            }
        except (KeyError, TypeError) as error:
            raise TikTokError("TikTok returned incomplete account information.") from error

    def disconnect(self) -> None:
        with self.tokens.locked():
            current = self.tokens.read()
            if current is None:
                return
            self._request(
                "POST", REVOKE_URL,
                form_body={
                    "client_key": self.config.client_key,
                    "client_secret": self.config.client_secret,
                    "token": current["access_token"],
                },
            )
            self.tokens.remove()

    def initialize(self, source_info: Mapping[str, Any]) -> dict[str, str]:
        token = self.access_token()
        try:
            response = self._request(
                "POST", INBOX_INITIALIZE_URL, maximum_attempts=1,
                headers={"Authorization": f"Bearer {token}"},
                json_body={"source_info": dict(source_info)},
            )
        except TikTokError as error:
            if not error.retryable:
                raise
            raise TikTokError(
                "TikTok upload initialization did not return a confirmed result.",
                retryable=True, ambiguous=True,
            ) from error
        try:
            data = response["data"]
            if not isinstance(data["publish_id"], str) or not data["publish_id"].strip():
                raise TypeError
            result = {"publish_id": data["publish_id"]}
            if "upload_url" in data:
                if not isinstance(data["upload_url"], str):
                    raise TypeError
                result["upload_url"] = data["upload_url"]
            return result
        except (KeyError, TypeError) as error:
            raise TikTokError("TikTok upload initialization response is incomplete.") from error

    def fetch_status(self, publish_id: str) -> dict[str, Any]:
        token = self.access_token()
        response = self._request(
            "POST", STATUS_URL,
            headers={"Authorization": f"Bearer {token}"},
            json_body={"publish_id": publish_id},
        )
        try:
            data = response["data"]
            status = data["status"]
            if not isinstance(status, str) or not status:
                raise TypeError
            post_ids = data.get("publicaly_available_post_id", [])
            share_urls = data.get("share_urls", [])
            if not isinstance(post_ids, list) or not isinstance(share_urls, list):
                raise TypeError
        except (KeyError, TypeError) as error:
            raise TikTokError("TikTok status response is incomplete.") from error
        return {
            "status": status,
            "fail_reason": safe_publication_text(data.get("fail_reason")) or None,
            "post_ids": [
                str(value) for value in post_ids
            ],
            "share_urls": [
                str(value) for value in share_urls
                if isinstance(value, str)
            ],
        }


class TikTokPublicationService:
    """Consent, idempotency, transfer, and remote-status orchestration."""

    def __init__(
        self, config: TikTokConfiguration, store: PublicationStore,
        queue: ClipReviewQueue, manifest: VideoManifest, api: TikTokAPI,
        token_store: TikTokTokenStore, oauth_states: OAuthStateStore,
        *, file_transport: FileUploadTransport | None = None,
        pull_transport: PullFromURLTransport | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.queue = queue
        self.manifest = manifest
        self.api = api
        self.tokens = token_store
        self.oauth_states = oauth_states
        self.file_transport = file_transport or FileUploadTransport()
        self.pull_transport = pull_transport
        self.now = now or (lambda: datetime.now(timezone.utc))

    @classmethod
    def from_environment(
        cls, queue: ClipReviewQueue, manifest: VideoManifest,
        *, store: PublicationStore | None = None,
        environment: Mapping[str, str] | None = None,
        http: Any | None = None,
    ) -> "TikTokPublicationService":
        config = TikTokConfiguration.from_environment(environment)
        token_store = TikTokTokenStore(config.token_path)
        oauth_states = OAuthStateStore(config.oauth_state_path)
        publication_store = store or PublicationStore()
        api = TikTokAPI(config, token_store, http=http)
        pull = None
        if config.transport == "PULL_FROM_URL":
            pull = PullFromURLTransport(SignedMediaURLService(
                config.verified_media_base_url or "",
                config.media_signing_key or "",
            ))
        return cls(
            config, publication_store, queue, manifest, api,
            token_store, oauth_states, pull_transport=pull,
        )

    def connection(self) -> dict[str, Any]:
        return {
            "enabled": self.config.enabled,
            "connected": self.tokens.read() is not None,
            "account": self.tokens.public_account(),
        }

    def authorization_url(self) -> str:
        return self.api.authorization_url(self.oauth_states)

    def complete_oauth(self, *, code: str, state: str) -> dict[str, Any]:
        return self.api.complete_oauth(
            code=code, state=state, oauth_states=self.oauth_states
        )

    def disconnect(self) -> None:
        self.api.disconnect()

    def review_context(self, review: Mapping[str, Any]) -> dict[str, Any]:
        connection = self.connection()
        attempt = self.store.latest_for_review(str(review["review_id"]))
        checksum = None
        path = Path(str(review.get("preview_path") or ""))
        if review.get("status") == "approved" and path.is_file():
            checksum = sha256_file(path)
        creator = None
        record = self.manifest.get(str(review["video_id"]))
        if record:
            creator = record.get("channel_name") or record.get("uploader")
        cleanup_eligible = bool(
            checksum and self.store.successful_attempt(
                str(review["review_id"]), int(review["timing_revision"]), checksum
            )
        )
        return {
            **connection,
            "attempt": attempt,
            "checksum": checksum,
            "source_creator": creator,
            "cleanup_eligible": cleanup_eligible,
        }

    def prepare(
        self, review_id: str, *, caption: str, source_attribution: str,
        rights_confirmed: bool,
    ) -> dict[str, Any]:
        if not self.config.enabled:
            raise TikTokError("TikTok integration is disabled.")
        account = self.tokens.public_account()
        if account is None:
            raise TikTokError("Connect TikTok before preparing a draft.")
        review = self.queue.find_by_review_id(review_id)
        if review is None:
            raise PublicationError("Review was not found.")
        if float(review["render_duration"]) > 600:
            raise PublicationError("TikTok inbox upload supports videos up to 10 minutes.")
        try:
            path = self.queue.paths.resolve(
                review["preview_path"], must_exist=True, regular=True
            )
        except ValueError as error:
            raise PublicationError(
                "The approved rendered media is unavailable or unsafe."
            ) from error
        checksum = sha256_file(path)
        return self.store.prepare(
            review=review, media_path=path, media_sha256=checksum,
            platform="tiktok", destination_account_id=account["open_id"],
            destination_account_name=account["display_name"], caption=caption,
            source_attribution=source_attribution,
            transport=self.config.transport, rights_confirmed=rights_confirmed,
        )

    def send(self, attempt_id: str, *, confirmed: bool) -> dict[str, Any]:
        if not confirmed:
            raise PublicationError("Explicit upload confirmation is required.")
        with self.queue.locked(), self.store.locked():
            attempt = self.store.get(attempt_id)
            if attempt["state"] != PublicationState.AWAITING_CONSENT.value:
                raise PublicationError("This publication is not awaiting consent.")
            review = self.queue.find_by_review_id(attempt["review_id"])
            if review is None:
                raise PublicationError("Review was not found.")
            self.store.assert_fresh(attempt, review)
            account = self.tokens.public_account()
            if account is None or account["open_id"] != attempt["destination_account_id"]:
                raise TikTokError("The explicitly authorized TikTok account changed.")
            today = self.now().astimezone(timezone.utc).date().isoformat()
            if self.store.daily_count(
                "tiktok", account["open_id"], today
            ) >= self.config.daily_limit:
                raise TikTokError("AutoClip's daily TikTok draft limit has been reached.")
            unresolved_others = sum(
                item["attempt_id"] != attempt_id
                and item["state"] not in TERMINAL_STATES
                for item in self.store.list_attempts(platform="tiktok")
                if item["destination_account_id"] == account["open_id"]
            )
            if unresolved_others >= self.config.maximum_pending:
                raise TikTokError(
                    "AutoClip already has the maximum unresolved TikTok draft."
                )
            attempt = self.store.transition(attempt_id, PublicationState.QUEUED)
        return self._execute(attempt)

    def _execute(self, attempt: Mapping[str, Any]) -> dict[str, Any]:
        attempt = self.store.transition(
            attempt["attempt_id"], PublicationState.INITIALIZING,
        )
        media = Path(attempt["rendered_media_path"])
        try:
            if attempt["transport"] == "PULL_FROM_URL":
                if self.pull_transport is None:
                    raise TikTokError("PULL_FROM_URL transport is not configured.")
                source_info = self.pull_transport.source_info(attempt)
            else:
                source_info = self.file_transport.source_info(media)
            initialized = self.api.initialize(source_info)
            attempt = self.store.transition(
                attempt["attempt_id"], PublicationState.TRANSFERRING,
                remote_publish_id=initialized["publish_id"],
                next_reconcile_at=(
                    self.now() + timedelta(
                        seconds=self.config.reconciliation_interval_seconds
                    )
                ).isoformat(),
                event="upload_initialized",
            )
            if attempt["transport"] == "FILE_UPLOAD":
                upload_url = initialized.get("upload_url")
                if not upload_url:
                    raise TikTokError("TikTok did not return a media upload URL.")
                self.file_transport.transfer(upload_url, media)
            return self.store.transition(
                attempt["attempt_id"], PublicationState.PROCESSING,
                event="transfer_completed",
            )
        except TikTokError as error:
            return self.store.transition(
                attempt["attempt_id"],
                PublicationState.FAILED_RETRYABLE
                if error.retryable or error.ambiguous
                else PublicationState.FAILED_TERMINAL,
                error_reason=str(error), transfer_uncertain=error.ambiguous,
                event="transfer_failed",
            )

    def refresh(self, attempt_id: str) -> dict[str, Any]:
        with self.store.locked():
            attempt = self.store.get(attempt_id)
            if attempt["state"] in TERMINAL_STATES:
                return attempt
            if not attempt["remote_publish_id"]:
                raise PublicationError("TikTok has not assigned a publish ID yet.")
            status = self.api.fetch_status(attempt["remote_publish_id"])
            self.store.record_status_check(attempt_id)
            remote = status["status"]
            if remote in {"PROCESSING_UPLOAD", "PROCESSING_DOWNLOAD"}:
                return self.store.transition(
                    attempt_id, PublicationState.PROCESSING,
                    next_reconcile_at=(
                        self.now() + timedelta(
                            seconds=self.config.reconciliation_interval_seconds
                        )
                    ).isoformat(),
                    event="remote_processing",
                )
            if remote == "SEND_TO_USER_INBOX":
                delivered = self.store.transition(
                    attempt_id, PublicationState.INBOX_DELIVERED,
                    event="inbox_delivered",
                )
                return self.store.transition(
                    delivered["attempt_id"], PublicationState.AWAITING_CREATOR_POST,
                    event="awaiting_creator_post",
                )
            if remote == "PUBLISH_COMPLETE":
                return self.store.transition(
                    attempt_id, PublicationState.PUBLISH_COMPLETE,
                    remote_post_ids=status["post_ids"], share_urls=status["share_urls"],
                    event="publish_complete_verified",
                )
            if remote == "FAILED":
                retryable = status["fail_reason"] in RETRYABLE_FAILURES
                return self.store.transition(
                    attempt_id,
                    PublicationState.FAILED_RETRYABLE
                    if retryable else PublicationState.FAILED_TERMINAL,
                    error_reason=status["fail_reason"] or "TikTok reported failure",
                    transfer_uncertain=False, event="remote_failed",
                )
            raise TikTokError("TikTok returned an unknown publication status.")

    def cancel(self, attempt_id: str) -> dict[str, Any]:
        attempt = self.store.get(attempt_id)
        if attempt["state"] not in {
            PublicationState.AWAITING_CONSENT.value,
            PublicationState.QUEUED.value,
        } or attempt["remote_publish_id"]:
            raise PublicationError("Only an uninitialized queued attempt can be cancelled.")
        return self.store.transition(attempt_id, PublicationState.CANCELLED)

    def retry(self, attempt_id: str) -> dict[str, Any]:
        with self.store.locked():
            attempt = self.store.get(attempt_id)
            if attempt["state"] != PublicationState.FAILED_RETRYABLE.value:
                raise PublicationError("Only a verified retryable failure can be retried.")
            if attempt["retry_count"] >= self.config.maximum_retries:
                raise PublicationError("The bounded TikTok retry limit has been reached.")
            if attempt["remote_publish_id"]:
                reconciled = self.refresh(attempt_id)
                if reconciled["state"] != PublicationState.FAILED_RETRYABLE.value:
                    return reconciled
            elif attempt["transfer_uncertain"]:
                raise PublicationError(
                    "An uncertain TikTok request has no remote publish ID to reconcile; "
                    "automatic retry is unsafe."
                )
            queued = self.store.transition(
                attempt_id, PublicationState.QUEUED,
                increment_retry=True, event="retry_queued"
            )
        return self._execute(queued)

    def reconcile_pending(self, *, limit: int = 1) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise PublicationError("Reconciliation limit must be positive.")
        now = self.now()
        candidates = [
            attempt for attempt in self.store.list_attempts(platform="tiktok")
            if attempt["state"] not in TERMINAL_STATES
            and attempt["remote_publish_id"]
            and (
                attempt["next_reconcile_at"] is None
                or datetime.fromisoformat(attempt["next_reconcile_at"]) <= now
            )
        ]
        results = []
        for attempt in sorted(candidates, key=lambda item: item["updated_at"])[:limit]:
            try:
                results.append(self.refresh(attempt["attempt_id"]))
            except TikTokError:
                continue
        return results

    def mark_review_stale(self, review_id: str) -> int:
        return self.store.mark_stale(
            review_id, "The approved render or timing revision changed after preparation."
        )


class TikTokWebhookVerifier:
    """Authenticated replay-resistant verifier for a future unexposed endpoint."""

    def __init__(self, client_secret: str, *, maximum_age: int = 300) -> None:
        self.secret = client_secret.encode("utf-8")
        self.maximum_age = maximum_age
        self.seen: set[str] = set()

    def verify(
        self, body: bytes, signature_header: str, *, now: int | None = None,
    ) -> dict[str, Any]:
        values = dict(
            part.split("=", 1) for part in signature_header.split(",") if "=" in part
        )
        timestamp = values.get("t")
        supplied = values.get("s")
        if not timestamp or not supplied:
            raise TikTokError("TikTok webhook signature is missing.")
        try:
            received = int(timestamp)
        except ValueError as error:
            raise TikTokError("TikTok webhook timestamp is invalid.") from error
        current = int(time.time()) if now is None else now
        if abs(current - received) > self.maximum_age:
            raise TikTokError("TikTok webhook is outside the replay window.")
        expected = hmac.new(
            self.secret, timestamp.encode() + b"." + body, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, supplied):
            raise TikTokError("TikTok webhook signature is invalid.")
        identity = hashlib.sha256(signature_header.encode() + b"\0" + body).hexdigest()
        if identity in self.seen:
            raise TikTokError("TikTok webhook replay was rejected.")
        self.seen.add(identity)
        try:
            value = json.loads(body)
        except json.JSONDecodeError as error:
            raise TikTokError("TikTok webhook body is malformed.") from error
        if not isinstance(value, dict):
            raise TikTokError("TikTok webhook body is invalid.")
        return value
