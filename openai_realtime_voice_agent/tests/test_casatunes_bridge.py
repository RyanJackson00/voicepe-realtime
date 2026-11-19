"""Unit tests for the opt-in CasaTunes response delivery adapter."""
import asyncio
import unittest
from unittest.mock import MagicMock, patch

from aiohttp import ClientConnectorError, ServerTimeoutError
from aiohttp import web
from aiohttp.test_utils import TestServer

from app.casatunes_bridge import CasaTunesBridge, CasaTunesBridgeConfig, DeliveryResult


def config(**overrides):
    values = dict(
        enabled=True,
        office_device_id="office-voice",
        ha_url="https://homeassistant.example",
        ha_token="test-only-token",
    )
    values.update(overrides)
    return CasaTunesBridgeConfig(**values)


class CasaTunesBridgeTests(unittest.TestCase):
    def test_custom_url_never_inherits_supervisor_token(self):
        with patch.dict("os.environ", {
            "CASATUNES_HA_URL": "https://homeassistant.example",
            "SUPERVISOR_TOKEN": "fixture-token",
        }, clear=True):
            self.assertEqual(CasaTunesBridgeConfig.from_env().ha_token, "")
        with patch.dict("os.environ", {"SUPERVISOR_TOKEN": "fixture-token"}, clear=True):
            self.assertEqual(CasaTunesBridgeConfig.from_env().ha_token, "fixture-token")

    def test_endpoint_rejects_unsafe_urls(self):
        for url in (
            "http://homeassistant.example", "http://supervisor/other",
            "https://user@localhost", "https://homeassistant.example/?next=x",
            "https://homeassistant.example:65536", "https://homeassistant.example/#fragment",
        ):
            with self.subTest(url=url):
                self.assertFalse(CasaTunesBridge(config(ha_url=url)).eligible("office-voice"))

    def test_response_timeout_is_not_a_definitive_rejection(self):
        bridge = CasaTunesBridge(config())
        with patch.object(bridge, "_post", side_effect=ServerTimeoutError()):
            self.assertIs(asyncio.run(bridge.speak(device_id="office-voice", text="hello")),
                          DeliveryResult.UNCERTAIN)

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
        with patch.object(bridge, "_post", return_value=DeliveryResult.ACCEPTED) as post:
            self.assertIs(asyncio.run(bridge.speak(device_id="office-voice", text="  hello  ")),
                          DeliveryResult.ACCEPTED)
            post.assert_awaited_once_with("hello")

    def test_connection_failure_permits_satellite_fallback(self):
        bridge = CasaTunesBridge(config())
        with patch.object(bridge, "_post", side_effect=ClientConnectorError(MagicMock(), OSError())):
            self.assertIs(asyncio.run(bridge.speak(device_id="office-voice", text="hello")),
                          DeliveryResult.REJECTED)

    def test_living_room_never_posts(self):
        bridge = CasaTunesBridge(config())
        with patch.object(bridge, "_post") as post:
            self.assertIs(asyncio.run(bridge.speak(device_id="living-room", text="hello")),
                          DeliveryResult.REJECTED)
            post.assert_not_called()


class CasaTunesHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_service_request_and_redirect_fallback(self):
        received = []
        redirects = []
        status = 200
        upstream_status = 202

        async def speak(request):
            received.append((
                request.headers["Authorization"],
                await request.json(),
                "return_response" in request.query,
            ))
            if status == 307:
                raise web.HTTPTemporaryRedirect("/redirect-target")
            return web.json_response(
                {"service_response": {"status": upstream_status}}, status=status,
            )

        async def redirect_target(request):
            redirects.append(request.path)
            return web.Response()

        app = web.Application()
        app.router.add_post("/core/api/services/rest_command/casatunes_speak", speak)
        app.router.add_post("/redirect-target", redirect_target)
        async with TestServer(app) as server:
            bridge = CasaTunesBridge(config(ha_url=str(server.make_url("/core"))))
            self.assertIs(await bridge.speak(device_id="office-voice", text="test reply"),
                          DeliveryResult.ACCEPTED)
            upstream_status = 503
            self.assertIs(await bridge.speak(device_id="office-voice", text="test reply"),
                          DeliveryResult.REJECTED)
            upstream_status = None
            self.assertIs(await bridge.speak(device_id="office-voice", text="test reply"),
                          DeliveryResult.UNCERTAIN)
            status = 307
            self.assertIs(await bridge.speak(device_id="office-voice", text="test reply"),
                          DeliveryResult.REJECTED)
            status = 503
            self.assertIs(await bridge.speak(device_id="office-voice", text="test reply"),
                          DeliveryResult.UNCERTAIN)
        self.assertEqual(
            received, [("Bearer test-only-token", {"message": "test reply"}, True)] * 5,
        )
        self.assertEqual(redirects, [])
