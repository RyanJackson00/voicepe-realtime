"""Contract tests for CasaTunes's registered Home Assistant search service.

All tests are offline. No room or source is actually activated.
"""
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.casatunes_music_tool import MusicConfig
from app.casatunes_native_search import NativeCasaTunesMusic, get_native_music_tool_definition


ROOM = "media_player.office"
CONFIG = MusicConfig(enabled=True, allowed_rooms=(ROOM,), ha_token="test-token")


class NativeMusicTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_query_fails_without_network(self):
        tool = NativeCasaTunesMusic(CONFIG)
        with patch("app.casatunes_native_search.aiohttp.ClientSession") as session:
            result = await tool.request_music(ROOM)
            session.assert_not_called()
        self.assertFalse(result["ok"])

    async def test_bad_mode_fails_without_network(self):
        tool = NativeCasaTunesMusic(CONFIG)
        with patch("app.casatunes_native_search.aiohttp.ClientSession") as session:
            result = await tool.request_music(ROOM, artist="Greta Van Fleet",mode="delete")
            session.assert_not_called()
        self.assertFalse(result["ok"])

    async def test_disallowed_room_never_sent(self):
        tool = NativeCasaTunesMusic(CONFIG)
        with patch("app.casatunes_native_search.aiohttp.ClientSession") as session:
            result = await tool.request_music("media_player.other",artist="Greta Van Fleet")
            session.assert_not_called()
        self.assertFalse(result["ok"])

    async def test_request_uses_verified_service_contract(self):
        tool = NativeCasaTunesMusic(CONFIG)
        tool._room_state = AsyncMock(return_value={
            "state":"off","attributes":{"source":"Dad","source_list":["Dad","Mom"]}})
        tool._ha = AsyncMock(return_value=[])
        with patch("app.casatunes_native_search.aiohttp.ClientSession") as session:
            result = await tool.request_music(ROOM,artist="Greta Van Fleet",
                                              mode="playNow",expected_source="Dad")
            session.assert_called_once()
        self.assertTrue(result["ok"])
        request = tool._ha.await_args
        self.assertEqual(request.args[1:3],
                         ("POST","/api/services/casatunes/search"))
        self.assertEqual(request.args[3],
                         {"entity_id":ROOM,"mode":"playNow",
                          "keyword_artist":"Greta Van Fleet"})

    async def test_mismatched_source_prevents_play(self):
        tool = NativeCasaTunesMusic(CONFIG)
        tool._room_state = AsyncMock(return_value={
            "state":"off","attributes":{"source":"Mom","source_list":["Dad","Mom"]}})
        tool._ha = AsyncMock()
        with patch("app.casatunes_native_search.aiohttp.ClientSession"):
            result = await tool.request_music(ROOM,artist="Greta Van Fleet",
                                              expected_source="Dad")
        self.assertFalse(result["ok"])
        tool._ha.assert_not_awaited()

    def test_tool_is_explicit_about_no_service_guarantee(self):
        description=get_native_music_tool_definition()["description"]
        self.assertIn("NOT guaranteed",description)


if __name__ == "__main__":
    unittest.main()
