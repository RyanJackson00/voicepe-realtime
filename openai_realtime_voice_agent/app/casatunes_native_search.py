"""CasaTunes voice music actions backed by the installed Home Assistant service.

Uses the verified casatunes.search schema (keyword_artist, keyword_album,
keyword_track_name, mode) instead of guessing a CasaTunes server REST route.
Opt-in only; the owner must allowlist exact media_player entities.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

from app.casatunes_music_tool import MusicConfig, CasaTunesMusic, MusicError, _SOURCE_RE

LOG = logging.getLogger(__name__)
MODES = frozenset({"playNow", "playShuffle", "playUnshuffle", "add", "addplay"})


def get_native_music_tool_definition() -> dict[str, Any]:
    return {
        "type": "function", "name": "casatunes_request_music",
        "description": (
            "Find an artist, song or album using the installed CasaTunes search "
            "integration and add or play it in an allowed room's CURRENT source. "
            "This changes playback/queue; call only for an explicit user music request. "
            "Amazon Music service-specific filtering is NOT guaranteed by this tool; "
            "do not claim Amazon was selected without verified service evidence."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "room": {"type":"string","description":"Spoken room label such as living room, or exact allowed CasaTunes media_player ID"},
                "artist": {"type":"string","description":"Artist name, e.g., Greta Van Fleet"},
                "album": {"type":"string","description":"Album title, optional"},
                "track": {"type":"string","description":"Song title, optional"},
                "mode": {"type":"string","enum":["playNow","playShuffle","playUnshuffle","add","addplay"],
                         "description":"playNow replaces queue; add appends; addplay queues and starts. Default playNow."},
                "expected_source": {"type":"string","description":"Optional exact CasaTunes source selected on this room, e.g., Dad"},
            },
            "required":["room"],
        },
    }


class NativeCasaTunesMusic(CasaTunesMusic):
    async def request_music(
        self, room: str, artist: str = "", album: str = "", track: str = "",
        mode: str = "playNow", expected_source: str = "",
    ) -> dict:
        room = self.config.resolve_room(room) or ""
        if not self.config.valid_room(room):
            return {"ok": False, "error": "CasaTunes music is not enabled for this room"}
        if mode not in MODES:
            return {"ok": False, "error": "Invalid CasaTunes queue mode"}
        terms = {"keyword_artist":artist.strip(), "keyword_album":album.strip(),
                 "keyword_track_name":track.strip()}
        if not any(terms.values()) or any(len(v) > 120 for v in terms.values()):
            return {"ok": False, "error": "Specify a valid artist, album, or song"}
        if expected_source and not _SOURCE_RE.fullmatch(expected_source):
            return {"ok": False, "error": "Invalid source name"}
        timeout = aiohttp.ClientTimeout(total=self.config.timeout)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                current = await self._room_state(session, room)
                if current.get("state") in ("unavailable", "unknown"):
                    raise MusicError("Room is unavailable")
                attrs = current.get("attributes") or {}
                source = attrs.get("source")
                if expected_source and source != expected_source:
                    return {"ok": False, "error": (
                        "Room source differs from requested source; assign the source first"
                    ), "current_source":source}
                # The source is not always reported while the room is powered off.
                # CasaTunes will decide the source as the underlying integration does.
                # Fail closed on a source in use by any OTHER enabled room when
                # we are replacing its playlist.
                if source and mode in {"playNow","playShuffle","playUnshuffle"}:
                    for other in self.config.allowed_rooms:
                        if other == room or not self.config.valid_room(other):
                            continue
                        state = await self._room_state(session, other)
                        other_attrs = state.get("attributes") or {}
                        if state.get("state") == "playing" and other_attrs.get("source") == source:
                            return {"ok": False, "error": (
                                "That source is already playing in another enabled room. "
                                "Choose another source or request shared-source playback explicitly."
                            )}
                data = {"entity_id": room, "mode":mode}
                data.update({k:v for k,v in terms.items() if v})
                # CasaTunes's registered entity service does not report a rich
                # result list. A 2xx only means HA accepted the service call.
                await self._ha(session, "POST", "/api/services/casatunes/search", data)
                return {"ok": True, "room": room, "source": source,
                        "request": {"artist":bool(terms["keyword_artist"]),
                                    "album":bool(terms["keyword_album"]),
                                    "track":bool(terms["keyword_track_name"]),
                                    "mode":mode},
                        "status":"CasaTunes search/play request accepted by Home Assistant",
                        "warning":"Specific music match, Amazon service, and audible playback not verified"}
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, MusicError) as exc:
            LOG.warning("CasaTunes music command failed (%s)", type(exc).__name__)
            return {"ok":False,"error":str(exc) if isinstance(exc,MusicError)
                    else "CasaTunes request failed or may have an uncertain outcome; do not retry automatically"}


def register_native_music_tool(service: Any, music: NativeCasaTunesMusic) -> None:
    async def handler(params):
        a = params.arguments or {}
        result = await music.request_music(
            room=str(a.get("room","")), artist=str(a.get("artist","")),
            album=str(a.get("album","")), track=str(a.get("track","")),
            mode=str(a.get("mode","playNow")),
            expected_source=str(a.get("expected_source","")))
        await params.result_callback(result)
    service.register_function("casatunes_request_music", handler)
