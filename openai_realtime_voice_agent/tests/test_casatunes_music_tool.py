"""Offline permission and source-control tests for CasaTunes."""
import unittest
from unittest.mock import AsyncMock, patch

from app.casatunes_music_tool import MusicConfig, CasaTunesMusic, get_music_tool_definitions


class ConfigTests(unittest.TestCase):
    def test_disabled_by_default(self):
        self.assertFalse(MusicConfig().valid_room("media_player.office"))

    def test_allowlist_exact_and_supervisor_scope(self):
        config = MusicConfig(enabled=True,allowed_rooms=("media_player.office",),
                             ha_token="fake")
        self.assertTrue(config.valid_room("media_player.office"))
        self.assertFalse(config.valid_room("media_player.kitchen"))
        self.assertFalse(config.valid_room("light.office"))
        self.assertFalse(MusicConfig(
            enabled=True,allowed_rooms=("media_player.office",),
            ha_token="fake",ha_url="http://example.com").valid_room("media_player.office"))

    def test_resolves_plain_spoken_room_from_allowlist_only(self):
        cfg=MusicConfig(enabled=True,
                        allowed_rooms=("media_player.01_living_room","media_player.04_office"),
                        ha_token="fake")
        self.assertEqual(cfg.resolve_room("living room"),"media_player.01_living_room")
        self.assertEqual(cfg.resolve_room("Office"),"media_player.04_office")
        self.assertIsNone(cfg.resolve_room("garage"))
        self.assertIsNone(cfg.resolve_room("living"))

    def test_only_supported_tools_exposed(self):
        names={t["name"] for t in get_music_tool_definitions()}
        self.assertEqual(names,{"casatunes_assign_source"})


class BehaviorTests(unittest.IsolatedAsyncioTestCase):
    async def test_disallowed_room_does_not_call_network(self):
        tool=CasaTunesMusic(MusicConfig(enabled=True,allowed_rooms=("media_player.office",),
                                         ha_token="fake"))
        with patch("app.casatunes_music_tool.aiohttp.ClientSession") as client:
            result=await tool.assign_source("media_player.bedroom","Sam")
            client.assert_not_called()
        self.assertFalse(result["ok"])

    async def test_source_must_be_one_listed_for_room(self):
        tool=CasaTunesMusic(MusicConfig(enabled=True,allowed_rooms=("media_player.office",),
                                         ha_token="fake"))
        tool._room_state=AsyncMock(return_value={
            "state":"off", "attributes":{"source":"Alex","source_list":["Alex","Sam"]}})
        tool._ha=AsyncMock()
        with patch("app.casatunes_music_tool.aiohttp.ClientSession"):
            result=await tool.assign_source("media_player.office","unknown")
        self.assertFalse(result["ok"])
        tool._ha.assert_not_awaited()

    async def test_source_assignment_verifies_state(self):
        tool=CasaTunesMusic(MusicConfig(enabled=True,allowed_rooms=("media_player.office",),
                                         ha_token="fake"))
        tool._room_state=AsyncMock(side_effect=[
            {"state":"on","attributes":{"source":"Alex","source_list":["Alex","Sam"]}},
            {"state":"on","attributes":{"source":"Sam","source_list":["Alex","Sam"]}},
        ])
        tool._ha=AsyncMock()
        with patch("app.casatunes_music_tool.aiohttp.ClientSession"):
            result=await tool.assign_source("media_player.office","Sam")
        self.assertTrue(result["ok"])
        self.assertEqual(tool._ha.await_args.args[1:3],
                         ("POST","/api/services/media_player/select_source"))
        self.assertEqual(tool._ha.await_args.args[3],
                         {"entity_id":"media_player.office","source":"Sam"})


if __name__ == "__main__":
    unittest.main()
