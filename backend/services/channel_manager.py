import json
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_FILE = PROJECT_ROOT / "backend" / "config" / "channels.json"


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

        channels = config.get("channels")

        if not isinstance(channels, list):
            raise ValueError(
                "Channel configuration must contain a 'channels' list."
            )

        validated_channels = []

        for index, channel in enumerate(channels, start=1):
            self._validate_channel(channel, index)
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
    def _validate_channel(channel: Any, index: int) -> None:
        """Validate one channel entry from the JSON configuration."""

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

        if not channel["youtube_url"].startswith(
            ("https://www.youtube.com/", "https://youtube.com/")
        ):
            raise ValueError(
                f"Channel entry {index} does not contain "
                "a valid YouTube URL."
            )


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
