"""Safe contract tests for the opt-in CasaTunes music tools.

No live HA / CasaTunes requests and no credentials. Search is intentionally
disabled until the homeowner verifies the endpoint in CasaDev.
"""
import unittest
from unittest.mock import AsyncMock, patch

from app.casatunes_music_tool import CasaTunesMusic, MusicConfig, get_music_tool_definitions


class MusicConfigTests(unittest.TestCase):
    def test_default_off(self):
        cfg = MusicConfig()
        self.assertFalse(cfg.valid_room("media_player.office"))
        self.assertFalse(cfg.search_ready())

    def test_allowed_room_exact(self):
        cfg = MusicConfig(enabled=True, allowed_rooms=("media_player.office",),
                          ha_token="fake", casa_url="http://casatunes.local:8735",
                          search_route="/api/v1/music/search")
        self.assertTrue(cfg.valid_room("media_player.office"))
        self.assertFalse(cfg.valid_room("media_player.not_office"))
        self.assertFalse(cfg.valid_room("light.office"))
        self.assertTrue(cfg.search_ready())

    def test_search_route_needs_administrator_configuration(self):
        cfg = MusicConfig(enabled=True, allowed_rooms=("media_player.office",),
                          ha_token="fake", casa_url="http://casatunes.local:8735")
        self.assertFalse(cfg.search_ready())
        for route in ("https://evil.test", "//evil.test/api/v1/", "/api/v1/../a",
                      "/api/v1/music/search?x=1"):
            bad = MusicConfig(enabled=True, ha_token="fake",
                              casa_url="http://casatunes.local:8735", search_route=route)
            self.assertFalse(bad.search_ready())

    def test_search_does_not_accept_credentials_in_base_url(self):
        cfg = MusicConfig(enabled=True, ha_token="fake",
                          casa_url="http://user:pass@host:8735",
                          search_route="/api/v1/music/search")
        self.assertFalse(cfg.search_ready())

    def test_tool_definitions_present(self):
        names = {tool["name"] for tool in get_music_tool_definitions()}
        self.assertEqual(names, {"casatunes_search_music","casatunes_play_music","casatunes_assign_source"})


class MusicBehaviorTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_without_server_contract_never_requests_network(self):
        music = CasaTunesMusic(MusicConfig(
            enabled=True, allowed_rooms=("media_player.office",),
            ha_token="fake"))
        with patch("app.casatunes_music_tool.aiohttp.ClientSession") as sess:
            result = await music.search("media_player.office", "Greta Van Fleet")
            sess.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertIn("not configured", result["error"])

    async def test_play_rejects_unverified_selection(self):
        music = CasaTunesMusic(MusicConfig(
            enabled=True, allowed_rooms=("media_player.office",),
            ha_token="fake"))
        with patch("app.casatunes_music_tool.aiohttp.ClientSession") as sess:
            result = await music.play("media_player.office", "1")
            sess.assert_not_called()
        self.assertFalse(result["ok"])

    async def test_assign_source_wrong_room_fails_closed(self):
        music = CasaTunesMusic(MusicConfig(
            enabled=True, allowed_rooms=("media_player.office",),
            ha_token="fake"))
        with patch("app.casatunes_music_tool.aiohttp.ClientSession") as sess:
            result = await music.assign_source("media_player.outside", "Dad")
            sess.assert_not_called()
        self.assertFalse(result["ok"])

    async def test_search_wrong_room_fails_closed(self):
        music = CasaTunesMusic(MusicConfig(
            enabled=True, allowed_rooms=("media_player.office",),
            ha_token="fake"))
        result = await music.search("media_player.living_room", "Greta Van Fleet")
        self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main()
