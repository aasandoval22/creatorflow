import json

import pytest

from backend.services.channel_manager import ChannelManager


def write_config(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def channel(name="Creator", url="https://www.youtube.com/@creator", enabled=True):
    return {"name": name, "youtube_url": url, "enabled": enabled}


def test_loads_valid_channels_and_filters_enabled(tmp_path):
    config = write_config(
        tmp_path / "channels.json",
        {"channels": [channel(), channel("Other", "https://youtube.com/@other", False)]},
    )
    manager = ChannelManager(config)

    assert len(manager.load_channels()) == 2
    assert manager.get_enabled_channels() == [channel()]


@pytest.mark.parametrize("value", [[], "channels", 42, None])
def test_rejects_non_object_top_level(tmp_path, value):
    config = write_config(tmp_path / "channels.json", value)

    with pytest.raises(ValueError, match="must be a JSON object"):
        ChannelManager(config).load_channels()


@pytest.mark.parametrize("value", [{}, {"channels": {}}, {"channels": "all"}])
def test_rejects_missing_or_incorrectly_typed_channel_list(tmp_path, value):
    config = write_config(tmp_path / "channels.json", value)

    with pytest.raises(ValueError, match="must contain a 'channels' list"):
        ChannelManager(config).load_channels()


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ("creator", "Channel entry 1 must be a JSON object"),
        ({"youtube_url": "https://youtube.com/@x", "enabled": True}, "missing the 'name'"),
        (channel(name=3), "field 'name' must be a str"),
        (channel(enabled="yes"), "field 'enabled' must be a bool"),
        (channel(name="  "), "has an empty name"),
    ],
)
def test_rejects_malformed_channel_entries(tmp_path, entry, message):
    config = write_config(tmp_path / "channels.json", {"channels": [entry]})

    with pytest.raises(ValueError, match=message):
        ChannelManager(config).load_channels()


@pytest.mark.parametrize(
    "url",
    [
        "http://youtube.com/@creator",
        "https://example.com/@creator",
        "https://youtube.com/watch?v=123",
        "https://youtube.com/@",
        "not-a-url",
    ],
)
def test_rejects_invalid_youtube_channel_urls(tmp_path, url):
    config = write_config(tmp_path / "channels.json", {"channels": [channel(url=url)]})

    with pytest.raises(ValueError, match="field 'youtube_url' is invalid"):
        ChannelManager(config).load_channels()


def test_rejects_duplicate_names_case_insensitively(tmp_path):
    config = write_config(
        tmp_path / "channels.json",
        {"channels": [channel("Creator"), channel(" creator ", "https://youtube.com/@other")]},
    )

    with pytest.raises(ValueError, match="duplicates the name from entry 1"):
        ChannelManager(config).load_channels()


def test_rejects_duplicate_normalized_urls(tmp_path):
    config = write_config(
        tmp_path / "channels.json",
        {"channels": [channel(), channel("Other", "https://www.youtube.com/@creator/videos?view=0#top")]},
    )

    with pytest.raises(ValueError, match="duplicates the YouTube URL from entry 1"):
        ChannelManager(config).load_channels()
