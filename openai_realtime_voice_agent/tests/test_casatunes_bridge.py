"""Unit tests for the opt-in CasaTunes response delivery adapter."""
import asyncio
import unittest
from urllib.error import URLError
from unittest.mock import patch

from app.casatunes_bridge import CasaTunesBridge, CasaTunesBridgeConfig


def config(**overrides):
    values = dict(
        enabled=True,
        office_device_id="office-voice",
        ha_url="http://homeassistant.local:8123",
        ha_token="test-only-token",
    )
    values.update(overrides)
    return CasaTunesBridgeConfig(**values)


class CasaTunesBridgeTests(unittest.TestCase):
    def test_disabled_by_default(self):
        self.assertFalse(CasaTunesBridge(CasaTunesBridgeConfig()).eligible("office-voice"))

    def test_only_configured_office_satellite(self):
        bridge = CasaTunesBridge(config())
        self.assertTrue(bridge.eligible("office-voice"))
        self.assertFalse(bridge.eligible("living-room"))
        self.assertFalse(bridge.eligible(None))

    def test_requires_token_and_url(self):
        self.assertFalse(CasaTunesBridge(config(ha_token="")).eligible("office-voice"))
        self.assertFalse(CasaTunesBridge(config(ha_url="")).eligible("office-voice"))

    def test_eligible_delivery(self):
        bridge = CasaTunesBridge(config())
        with patch.object(bridge, "_post", return_value=True) as post:
            self.assertTrue(asyncio.run(bridge.speak(device_id="office-voice", text="  Hello  ")))
            post.assert_called_once_with("Hello")

    def test_failure_returns_false_for_satellite_fallback(self):
        bridge = CasaTunesBridge(config())
        with patch.object(bridge, "_post", side_effect=URLError("unreachable")):
            self.assertFalse(asyncio.run(bridge.speak(device_id="office-voice", text="Hello")))

    def test_living_room_never_posts(self):
        bridge = CasaTunesBridge(config())
        with patch.object(bridge, "_post") as post:
            self.assertFalse(asyncio.run(bridge.speak(device_id="living-room", text="Hello")))
            post.assert_not_called()
