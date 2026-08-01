import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_FILE = PROJECT_ROOT / "backend" / "config" / "channels.json"
CHANNEL_VIDEO_LIMIT_FIELD = "max_videos_per_cycle"


def normalize_youtube_channel_url(channel_url: str) -> str:
    """Validate and normalize a supported YouTube channel URL."""

    if not isinstance(channel_url, str) or not channel_url.strip():
        raise ValueError("must be a non-blank string")

    parsed = urlsplit(channel_url.strip())

    if parsed.scheme != "https":
        raise ValueError("must use https")

    if parsed.hostname not in {"youtube.com", "www.youtube.com"}:
        raise ValueError("must use the youtube.com host")

    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("contains an invalid port") from error

    if parsed.username is not None or parsed.password is not None or port:
        raise ValueError("must not contain credentials or a port")

    path_parts = [part for part in parsed.path.split("/") if part]

    if path_parts and path_parts[-1].lower() == "videos":
        path_parts.pop()

    valid_handle = (
        len(path_parts) == 1
        and path_parts[0].startswith("@")
        and len(path_parts[0]) > 1
    )
    valid_legacy_path = (
        len(path_parts) == 2
        and path_parts[0] in {"channel", "c", "user"}
        and bool(path_parts[1].strip())
    )

    if not (valid_handle or valid_legacy_path):
        raise ValueError(
            "must identify a channel using /@handle, /channel/ID, "
            "/c/name, or /user/name"
        )

    normalized_path = "/" + "/".join(path_parts)
    return urlunsplit(("https", "www.youtube.com", normalized_path, "", ""))


class ChannelManager:
    """Load and validate CreatorFlow's YouTube channel configuration."""

    def __init__(self, config_file: Path = CONFIG_FILE) -> None:
        self.config_file = config_file

    def load_channels(self) -> list[dict[str, Any]]:
        """Return all valid channels from the configuration file."""

        if not self.config_file.exists():
            raise FileNotFoundError(
                f"Channel configuration file not found: {self.config_file}"
            )

        try:
            with self.config_file.open("r", encoding="utf-8") as file:
                config = json.load(file)
        except json.JSONDecodeError as error:
            raise ValueError(
                f"Invalid JSON in {self.config_file}: {error}"
            ) from error

        if not isinstance(config, dict):
            raise ValueError(
                "Channel configuration must be a JSON object containing "
                "a 'channels' list."
            )

        channels = config.get("channels")

        if not isinstance(channels, list):
            raise ValueError(
                "Channel configuration must contain a 'channels' list."
            )

        validated_channels = []
        seen_names: dict[str, int] = {}
        seen_urls: dict[str, int] = {}

        for index, channel in enumerate(channels, start=1):
            normalized_url = self._validate_channel(channel, index)
            normalized_name = channel["name"].strip().casefold()

            if normalized_name in seen_names:
                raise ValueError(
                    f"Channel entry {index} duplicates the name from "
                    f"entry {seen_names[normalized_name]}: {channel['name']!r}."
                )

            if normalized_url in seen_urls:
                raise ValueError(
                    f"Channel entry {index} duplicates the YouTube URL from "
                    f"entry {seen_urls[normalized_url]}: "
                    f"{channel['youtube_url']!r}."
                )

            seen_names[normalized_name] = index
            seen_urls[normalized_url] = index
            validated_channels.append(channel)

        return validated_channels

    def get_enabled_channels(self) -> list[dict[str, Any]]:
        """Return only channels whose enabled field is true."""

        channels = self.load_channels()

        return [
            channel
            for channel in channels
            if channel.get("enabled", False)
        ]

    @staticmethod
    def _validate_channel(channel: Any, index: int) -> str:
        """Validate one channel entry and return its normalized URL."""

        if not isinstance(channel, dict):
            raise ValueError(
                f"Channel entry {index} must be a JSON object."
            )

        required_fields = {
            "name": str,
            "youtube_url": str,
            "enabled": bool,
        }

        for field_name, expected_type in required_fields.items():
            if field_name not in channel:
                raise ValueError(
                    f"Channel entry {index} is missing "
                    f"the '{field_name}' field."
                )

            if not isinstance(channel[field_name], expected_type):
                raise ValueError(
                    f"Channel entry {index} field '{field_name}' "
                    f"must be a {expected_type.__name__}."
                )

        if not channel["name"].strip():
            raise ValueError(
                f"Channel entry {index} has an empty name."
            )

        if CHANNEL_VIDEO_LIMIT_FIELD in channel:
            limit = channel[CHANNEL_VIDEO_LIMIT_FIELD]
            if (
                isinstance(limit, bool)
                or not isinstance(limit, int)
                or limit < 1
            ):
                raise ValueError(
                    f"Channel entry {index} field "
                    f"'{CHANNEL_VIDEO_LIMIT_FIELD}' must be a positive int."
                )

        try:
            return normalize_youtube_channel_url(channel["youtube_url"])
        except ValueError as error:
            raise ValueError(
                f"Channel entry {index} field 'youtube_url' is invalid: "
                f"{error}."
            ) from error


def channel_video_limit(channel: dict[str, Any], run_limit: int) -> int:
    """Return the stricter run-wide or optional per-channel discovery limit."""

    if (
        isinstance(run_limit, bool)
        or not isinstance(run_limit, int)
        or run_limit < 1
    ):
        raise ValueError("run video limit must be a positive integer")
    configured = channel.get(CHANNEL_VIDEO_LIMIT_FIELD, run_limit)
    if (
        isinstance(configured, bool)
        or not isinstance(configured, int)
        or configured < 1
    ):
        raise ValueError(
            f"channel field {CHANNEL_VIDEO_LIMIT_FIELD!r} must be a "
            "positive integer"
        )
    return min(run_limit, configured)


def main() -> None:
    manager = ChannelManager()

    try:
        channels = manager.get_enabled_channels()
    except (FileNotFoundError, ValueError) as error:
        print(f"Configuration error: {error}")
        raise SystemExit(1) from error

    if not channels:
        print("No enabled channels were found.")
        return

    print(f"Found {len(channels)} enabled channel(s):")

    for channel in channels:
        print(f"- {channel['name']}: {channel['youtube_url']}")


if __name__ == "__main__":
    main()
