"""Unit tests for the opt-in CasaTunes response delivery adapter."""
import asyncio
from urllib.error import URLError
from unittest.mock import patch

from app.casatunes_bridge import CasaTunesBridge, CasaTunesBridgeConfig


def _config(**overrides):
    values = dict(
        enabled=True,
        office_device_id="office-voice",
        ha_url="http://homeassistant.local:8123",
        ha_token="test-only-token",
    )
    values.update(overrides)
    return CasaTunesBridgeConfig(**values)


def test_disabled_by_default():
    assert not CasaTunesBridge(CasaTunesBridgeConfig()).eligible("office-voice")


def test_only_configured_office_satellite():
    bridge = CasaTunesBridge(_config())
    assert bridge.eligible("office-voice")
    assert not bridge.eligible("living-room")
    assert not bridge.eligible(None)


def test_requires_token_and_url():
    assert not CasaTunesBridge(_config(ha_token="")).eligible("office-voice")
    assert not CasaTunesBridge(_config(ha_url="")).eligible("office-voice")


def test_eligible_delivery():
    bridge = CasaTunesBridge(_config())
    with patch.object(bridge, "_post", return_value=True) as post:
        assert asyncio.run(bridge.speak(device_id="office-voice", text="  Hello  "))
        post.assert_called_once_with("Hello")


def test_failure_returns_false_for_satellite_fallback():
    bridge = CasaTunesBridge(_config())
    with patch.object(bridge, "_post", side_effect=URLError("unreachable")):
        assert not asyncio.run(bridge.speak(device_id="office-voice", text="Hello"))


def test_living_room_never_posts():
    bridge = CasaTunesBridge(_config())
    with patch.object(bridge, "_post") as post:
        assert not asyncio.run(bridge.speak(device_id="living-room", text="Hello"))
        post.assert_not_called()
